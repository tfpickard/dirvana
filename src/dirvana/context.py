"""A provider-independent view of one directory, built only from the shadow tree.

Used for enrichment prompts (where it must be deterministic: no "now", no host lookups), for
the hotkey payload, and for local fallbacks.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from dirvana.paths import CONTEXT_FILE, CONTEXT_META_FILE, OBS_FILE
from dirvana.rank import EdgeView, RankConfig, rank
from dirvana.store import edges as edgeagg
from dirvana.store.ingest import Store
from dirvana.store.io import as_dict, read_json, read_jsonl


@dataclass(frozen=True, slots=True)
class Command:
    cmd: str
    st: int
    t: float
    sid: str
    cwd: str


@dataclass(slots=True)
class NodeView:
    path: str
    identity: str | None
    recon: dict[str, Any]
    notes: str
    labels: dict[str, str]
    commands: list[Command]
    out_edges: list[EdgeView]
    in_edges: list[EdgeView]
    derived: str | None
    derived_meta: dict[str, Any]
    obs_count: int
    sessions: int = 0
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def top_commands(self, k: int = 12) -> list[tuple[str, int]]:
        counts = Counter(c.cmd for c in self.commands)
        last: dict[str, float] = {}
        for c in self.commands:
            last[c.cmd] = max(last.get(c.cmd, 0.0), c.t)
        return sorted(counts.items(), key=lambda kv: (-kv[1], -last[kv[0]], kv[0]))[:k]

    def recent(self, k: int = 12) -> list[Command]:
        seen: set[str] = set()
        out: list[Command] = []
        for c in sorted(self.commands, key=lambda c: -c.t):
            if c.cmd not in seen:
                seen.add(c.cmd)
                out.append(c)
            if len(out) >= k:
                break
        return out


def load_view(
    store: Store,
    path: str,
    *,
    today: date,
    ranking: RankConfig,
    live_out: bool = True,
) -> NodeView:
    """Read a node. With ``live_out`` the outbound edges are recomputed from the journal in
    memory (no lock, no writes) so a hotkey never waits for the daemon's ingest."""
    d = store.node_dir(path)
    obs = list(read_jsonl(d / OBS_FILE))
    commands = [
        Command(
            str(o.get("cmd", "")),
            int(o.get("st", 0) or 0),
            float(o.get("t", 0) or 0.0),
            str(o.get("sid", "")),
            str(o.get("cwd", "")),
        )
        for o in obs
        if o.get("k") == "cmd" and o.get("cmd")
    ]
    if live_out:
        agg = edgeagg.aggregate(obs, self_path=path, skip_peer=store.ignored)
        out_doc: dict[str, Any] | None = edgeagg.to_document(agg, {})
    else:
        out_doc = store.edges_out(path)
    kw: dict[str, Any] = {
        "today": today,
        "self_path": path,
        "home_dir": str(store.home_dir or ""),
        "cfg": ranking,
    }
    node = store.node_doc(path)
    ddir = store.derived_dir(path)
    derived = (
        (ddir / CONTEXT_FILE).read_text(encoding="utf-8")
        if (ddir / CONTEXT_FILE).exists()
        else None
    )
    return NodeView(
        path=path,
        identity=node.get("identity"),
        recon=store.recon(path) or {},
        notes=str(node.get("notes") or ""),
        labels={str(k): str(v) for k, v in as_dict(node.get("labels")).items()},
        commands=commands,
        out_edges=rank(out_doc, "out", **kw),
        in_edges=rank(store.edges_in(path), "in", **kw),
        derived=derived,
        derived_meta=read_json(ddir / CONTEXT_META_FILE) or {},
        obs_count=len(obs),
        sessions=len({c.sid for c in commands}),
    )


def git_headline(recon: dict[str, Any]) -> dict[str, Any]:
    git = as_dict(recon.get("git"))
    if not git.get("toplevel"):
        return {"error": git.get("error")} if git.get("error") else {}
    return {
        "toplevel": git.get("toplevel"),
        "branch": git.get("branch"),
        "remotes": as_dict(git.get("remotes")),
    }


def excerpts(recon: dict[str, Any], limit: int = 1200) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, info in sorted(as_dict(recon.get("files")).items()):
        text = str(as_dict(info).get("excerpt") or "")
        if text:
            out[name] = text[:limit]
    return out


def day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).date().isoformat()


def week(d: str) -> str:
    """The Monday of ``d``'s ISO week: coarse on purpose, so fingerprints stay stable."""
    try:
        dt = date.fromisoformat(d)
    except ValueError:
        return d
    return date.fromordinal(dt.toordinal() - dt.weekday()).isoformat()


def rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path
