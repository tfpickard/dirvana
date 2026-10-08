"""Subcommand implementations."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dirvana._meta import NAME, env
from dirvana.paths import (
    CONTEXT_FILE,
    OBS_FILE,
    Dirs,
    home,
    portable,
)
from dirvana.policy import PolicyError, explain, load_rules, resolve
from dirvana.rank import EdgeView, rank
from dirvana.store.ingest import Store, iter_nodes
from dirvana.store.io import as_dict, read_json, write_json_atomic


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _today() -> Any:
    now = os.environ.get(env("NOW"), "")
    ts = float(now) if now else datetime.now(UTC).timestamp()
    return datetime.fromtimestamp(ts, UTC).date()


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _open() -> Store:
    dirs = Dirs.from_env()
    try:
        rules = load_rules(dirs)
    except PolicyError as e:
        raise CliError(f"policy: {e}") from e
    return Store(dirs, rules, str(home()))


def _rel(path: str, base: str) -> str:
    """Shortest of: relative to base, ~-portable, absolute."""
    rel = os.path.relpath(path, base)
    port = portable(path)
    return min((rel, port, path), key=len)


def _edge_line(e: EdgeView, base: str) -> str:
    verbs = " ".join(f"{v}×{n}" for v, n in e.verbs.items())
    mark = "?" if e.tentative else " "
    return f" {mark} {_rel(e.peer, base):<40} {verbs}  (last {e.last}, score {e.score:.2f})"


def _edge_json(e: EdgeView) -> dict[str, Any]:
    return {
        "peer": e.peer,
        "direction": e.direction,
        "verbs": dict(e.verbs),
        "n": e.n,
        "sessions": e.sessions,
        "first": e.first,
        "last": e.last,
        "score": round(e.score, 4),
        "tentative": e.tentative,
        "examples": list(e.examples),
        "peer_identity": e.peer_identity,
    }


def _edges(store: Store, real: str) -> tuple[list[EdgeView], list[EdgeView]]:
    kw: dict[str, Any] = {"today": _today(), "self_path": real, "home_dir": str(home())}
    return rank(store.edges_out(real), "out", **kw), rank(store.edges_in(real), "in", **kw)


# -- commands ---------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    store = _open()
    dirs = store.dirs
    nodes = 0
    records = 0
    last = 0.0
    for d in iter_nodes(dirs.system):
        nodes += 1
        obs = d / OBS_FILE
        try:
            st = obs.stat()
        except FileNotFoundError:
            continue
        last = max(last, st.st_mtime)
        with obs.open("rb") as f:
            records += sum(1 for _ in f)
    mid = ""
    with contextlib.suppress(FileNotFoundError):
        mid = (dirs.state / "machine-id").read_text(encoding="ascii").strip()[:12]
    info: dict[str, Any] = {
        "root": str(dirs.root),
        "config": str(dirs.config),
        "state": str(dirs.state),
        "machine_id": mid or None,
        "nodes": nodes,
        "observations": records,
        "last_activity": datetime.fromtimestamp(last, UTC).isoformat() if last else None,
        "globally_paused": (dirs.var / "paused").exists(),
    }
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    for k, v in info.items():
        print(f"{k.replace('_', ' '):<16} {v if v is not None else '-'}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = _open()
    store.dirs.ensure_root()
    report = store.ingest(full=args.full)
    print(
        f"scanned {report.scanned} nodes, updated {len(report.changed)}, "
        f"purged {len(report.purged)}"
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _open()
    real = _real(args.dir)
    if store.dirs.system.is_dir():
        store.ingest()
    obs = store.observations(real)
    cmds = [o for o in obs if o.get("k") == "cmd"]
    recon = store.recon(real) or {}
    node = store.node_doc(real)
    out, inbound = _edges(store, real)
    eff = resolve(real, store.rules)
    ctx_path = store.derived_dir(real) / CONTEXT_FILE
    context = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else None
    top = Counter(str(o.get("cmd", "")) for o in cmds).most_common(8)
    sessions = {o.get("sid") for o in obs}
    if args.json:
        print(
            json.dumps(
                {
                    "path": real,
                    "node": str(store.node_dir(real)),
                    "identity": node.get("identity"),
                    "aliases": node.get("aliases", []),
                    "labels": node.get("labels", {}),
                    "notes": node.get("notes", ""),
                    "recon": recon,
                    "commands": len(cmds),
                    "sessions": len(sessions),
                    "top_commands": [{"cmd": c, "count": n} for c, n in top],
                    "edges_out": [_edge_json(e) for e in out],
                    "edges_in": [_edge_json(e) for e in inbound],
                    "policy": {k: s.value for k, s in eff.settings.items()},
                    "context": context,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    print(real)
    print(f"  node      {store.node_dir(real)}")
    if not obs and not recon and not node:
        print("  (nothing recorded here yet)")
    if node.get("identity"):
        print(f"  identity  {node['identity']}")
    git = as_dict(recon.get("git"))
    if git.get("toplevel"):
        remotes = ", ".join(f"{k} {v}" for k, v in as_dict(git.get("remotes")).items())
        print(
            f"  git       {git.get('branch') or '?'} · {remotes or 'no remote'} · {git['toplevel']}"
        )
    elif git.get("error"):
        print(f"  git       ({git['error']})")
    files = as_dict(recon.get("files"))
    if files:
        print(f"  files     {' '.join(sorted(files))}")
    if obs:
        last = max(float(o.get("t", 0)) for o in obs)
        print(
            f"  activity  {len(cmds)} commands, {len(sessions)} sessions, "
            f"last {datetime.fromtimestamp(last, UTC).date()}"
        )
    for c, n in top:
        print(f"    {n:>4}  {c}")
    if node.get("notes"):
        print(f"  notes     {node['notes']}")
    if node.get("labels"):
        print("  labels    " + " ".join(f"{k}={v}" for k, v in node["labels"].items()))
    for title, views in (("out", out), ("in", inbound)):
        if views:
            print(f"  edges {title}")
            for e in views[:10]:
                print(_edge_line(e, real))
    pol = " ".join(f"{k}={s.value}" for k, s in eff.settings.items())
    print(f"  policy    {pol or '(defaults)'}")
    print(f"  context   {'(see below)' if context else '(not enriched yet)'}")
    if context:
        print()
        print(context.rstrip())
    return 0


def cmd_edges(args: argparse.Namespace) -> int:
    store = _open()
    real = _real(args.dir)
    if store.dirs.system.is_dir():
        store.ingest()
    out, inbound = _edges(store, real)
    want = [("out", out), ("in", inbound)]
    if args.direction:
        want = [w for w in want if w[0] == args.direction]
    if args.json:
        print(json.dumps({d: [_edge_json(e) for e in v] for d, v in want}, indent=2))
        return 0
    for title, views in want:
        print(f"{title}bound ({len(views)})" if views else f"{title}bound: none")
        for e in views:
            print(_edge_line(e, real))
    if any(e.tentative for _, v in want for e in v):
        print("? = tentative: too little evidence yet; never sent to a provider")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    store = _open()
    real = _real(args.dir)
    print(store.derived_dir(real) if args.derived else store.node_dir(real))
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_forget(args: argparse.Namespace) -> int:
    from dirvana.store.forget import forget

    store = _open()
    real = _real(args.dir)
    what = f"{real} and everything below it" if args.recursive else real
    if not args.yes and not _confirm(f"forget {what}?"):
        raise CliError("not confirmed (use -y)", 2)
    removed = forget(store, real, recursive=args.recursive)
    print(f"forgot {len(removed)} node(s)")
    return 0


def _update_node(store: Store, real: str, fn: Any) -> None:
    path = store.node_dir(real) / "%node.json"
    doc = read_json(path) or {"v": 1, "labels": {}, "notes": ""}
    fn(doc)
    store.dirs.ensure_root()
    write_json_atomic(path, doc)


def cmd_note(args: argparse.Namespace) -> int:
    store = _open()
    text = " ".join(args.text).strip()

    def apply(doc: dict[str, Any]) -> None:
        doc["notes"] = text

    _update_node(store, _real(args.dir), apply)
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    store = _open()
    pairs: list[tuple[str, str]] = []
    for item in args.labels:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError(f"labels are KEY=VALUE: {item!r}", 2)
        pairs.append((key, value))

    def apply(doc: dict[str, Any]) -> None:
        labels = doc.setdefault("labels", {})
        for k, v in pairs:
            if v:
                labels[k] = v
            else:
                labels.pop(k, None)

    _update_node(store, _real(args.dir), apply)
    return 0


def _global_flag(args: argparse.Namespace, verb: str) -> Path:
    if not args.global_:
        raise CliError(
            f"`{verb}` without --global is a session command handled by the zsh plugin "
            f"(is it loaded? see `{NAME} init zsh`)",
            2,
        )
    dirs = Dirs.from_env()
    dirs.ensure_root()
    return dirs.var / "paused"


def cmd_pause(args: argparse.Namespace) -> int:
    _global_flag(args, "pause").touch()
    print(f"{NAME}: paused in all shells")
    return 0


cmd_incognito = cmd_pause


def cmd_resume(args: argparse.Namespace) -> int:
    _global_flag(args, "resume").unlink(missing_ok=True)
    print(f"{NAME}: recording")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    dirs = Dirs.from_env()
    if args.policy_command == "check":
        try:
            rules = load_rules(dirs)
        except PolicyError as e:
            raise CliError(str(e)) from e
        print(f"ok: {len(rules)} rules")
        return 0
    if args.policy_command == "explain":
        try:
            rules = load_rules(dirs)
        except PolicyError as e:
            raise CliError(str(e)) from e
        for line in explain(resolve(_real(args.dir), rules)):
            print(line)
        return 0
    return _policy_edit(dirs, args.dir)


def _policy_edit(dirs: Dirs, target: str | None) -> int:
    if target is None:
        path = dirs.config / "policy"
        template = (
            "# Global dirvana policy. One rule per line: PATTERN key[=value] ...\n"
            "# Keys: ignore[=true|false] retention=eternal|ephemeral ttl=7d\n"
            "#       enrich=async|sync|off llm=none|<provider>[,<provider>...]\n"
            "# Patterns: ~/x, /abs/x, name (any depth), * ? [..] within a name, ** across.\n"
            "#\n"
            "# ~/work/acme   llm=copilot\n"
            "# ~/scratch     llm=none\n"
        )
    else:
        real = _real(target)
        slug = portable(real).strip("/~").replace("/", "_") or "root"
        path = dirs.config / "policy.d" / f"{slug}.policy"
        template = (
            f"@root {portable(real)}\n"
            "# Patterns below are relative to the root above.\n"
            "# **   llm=copilot\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(template, encoding="utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim"
    return subprocess.call([*shlex.split(editor), str(path)])


def _plugin_path() -> Path:
    import dirvana

    pkg = Path(dirvana.__file__).resolve().parent
    for candidate in (pkg / "shell" / "dirvana.plugin.zsh", pkg.parents[1] / "dirvana.plugin.zsh"):
        if candidate.is_file():
            return candidate
    raise CliError("cannot locate the zsh plugin in this installation")


def cmd_init(args: argparse.Namespace) -> int:
    print(f"source {shlex.quote(str(_plugin_path()))}")
    return 0
