"""Hotkey clients: ``dirvana suggest`` and ``dirvana brief``.

Reads ``{"cwd", "buffer", "ring": [{"cmd", "cwd"}], "sid", "incognito"}`` on stdin (never
argv, so commands do not show up in ``ps``), builds an egress-filtered payload, and asks the
daemon over its socket to run the provider call. Only if the socket cannot be *connected*
does it call Anthropic/OpenAI in-process; a slow daemon never causes a second call. Whatever
happens, it answers before the deadline: the provider call runs in a worker thread and the
main thread prints local results when time is up, then exits hard so a call blocked in DNS
or TLS cannot hold the prompt.

suggest output: one candidate per line, ``cmd<TAB>why<TAB>source`` (source: llm or local).
Exit status 0 when the provider answered, 3 when the local fallback was used.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import queue
import shlex
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dirvana.context import NodeView, git_headline, load_view, rel
from dirvana.prompts import Rendered, render_brief, render_suggest
from dirvana.providers.base import CompletionRequest, CompletionResult, ProviderError, Usage
from dirvana.rank import EdgeView
from dirvana.runtime import Runtime, now
from dirvana.store.io import as_dict, as_list

EXIT_FALLBACK = 3


@dataclass(frozen=True, slots=True)
class Candidate:
    cmd: str
    why: str
    source: str


# -- input -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Request:
    cwd: str
    buffer: str
    ring: list[tuple[str, str]]
    sid: str
    incognito: bool


def parse_input(text: str) -> Request:
    data = as_dict(json.loads(text)) if text.strip() else {}
    cwd = os.path.realpath(str(data.get("cwd") or os.getcwd()))
    ring = [
        (str(as_dict(r).get("cmd", "")), str(as_dict(r).get("cwd", "")))
        for r in as_list(data.get("ring"))
    ]
    incognito = bool(data.get("incognito"))
    return Request(
        cwd=cwd,
        buffer=str(data.get("buffer") or ""),
        ring=[] if incognito else [r for r in ring if r[0]],
        sid=str(data.get("sid") or ""),
        incognito=incognito,
    )


# -- local fallback ----------------------------------------------------------------------------


def _q(path: str) -> str:
    return shlex.quote(path)


def edge_templates(e: EdgeView, base: str) -> list[tuple[str, str]]:
    r = rel(e.peer, base)
    r = r if len(r) <= len(e.peer) else e.peer
    ex = e.examples[0] if e.examples else None
    out: list[tuple[str, str]] = []
    for verb in e.verbs:
        if verb == "diff":
            out.append(
                (
                    f"diff {_q(r + '/' + ex)} {_q(ex)}" if ex else f"diff -ru {_q(r)} .",
                    "you diff against it",
                )
            )
        elif verb == "copy-from" and ex:
            out.append((f"cp {_q(r + '/' + ex)} .", "you copy from it"))
        elif verb == "copy-to" and ex:
            out.append((f"cp {_q(ex)} {_q(r)}/", "you copy into it"))
        elif verb == "edit" and ex:
            out.append((f"nvim {_q(r + '/' + ex)}", "you edit files there"))
        elif verb == "read" and ex:
            out.append((f"less {_q(r + '/' + ex)}", "you read files there"))
        elif verb == "run":
            out.append((f"make -C {_q(r)}", "you build there"))
        elif verb == "cd":
            out.append((f"cd {_q(r)}", "you go there"))
    out.append((f"ls {_q(r)}", f"related: {', '.join(e.verbs)}"))
    return out


def local_candidates(
    view: NodeView, buffer: str, n: int, exists: Callable[[str], bool]
) -> list[Candidate]:
    pool: list[tuple[str, str]] = []
    established = [e for e in view.out_edges if not e.tentative and exists(e.peer)]
    edges = established or [e for e in view.out_edges if exists(e.peer)]
    for e in edges[:4]:
        pool.extend(edge_templates(e, view.path)[:2])
    for cmd, count in view.top_commands(n):
        pool.append((cmd, f"you ran this here {count}×"))
    for e in edges[4:8]:
        pool.extend(edge_templates(e, view.path)[:1])
    unique = list({c: (c, w) for c, w in reversed(pool)}.values())[::-1]
    if buffer.strip():
        starts = [p for p in unique if p[0].startswith(buffer)]
        contains = [p for p in unique if buffer.strip() in p[0] and p not in starts]
        unique = starts + contains or unique
    return [Candidate(c, w, "local") for c, w in unique[:n]]


def local_brief(view: NodeView, req: Request, today: str) -> str:
    lines = [view.path]
    git = git_headline(view.recon)
    if git.get("toplevel"):
        remote = next(iter(as_dict(git.get("remotes")).values()), "no remote")
        lines.append(f"git: {git.get('branch')} · {remote}")
    if view.notes:
        lines.append(f"note: {view.notes}")
    previous = [c for c in view.commands if c.sid != req.sid]
    if previous:
        last = max(c.t for c in previous)
        lines.append(f"last here: {time.strftime('%Y-%m-%d', time.gmtime(last))}")
    top = ", ".join(c for c, _ in view.top_commands(3))
    if top:
        lines.append(f"you usually: {top}")
    outs = [f"{rel(e.peer, view.path)} ({'/'.join(e.verbs)})" for e in view.out_edges[:3]]
    if outs:
        lines.append(f"you reach: {'; '.join(outs)}")
    ins = [rel(e.peer, view.path) for e in view.in_edges[:3]]
    if ins:
        lines.append(f"arrive from: {', '.join(ins)}")
    if view.derived:
        first = next((ln for ln in view.derived.splitlines() if ln.startswith("- ")), "")
        if first:
            lines.append(f"context: {first[2:]}")
    if len(lines) == 1:
        lines.append(f"nothing recorded here yet (as of {today})")
    return "\n".join(lines)


# -- validation --------------------------------------------------------------------------------


def _path_tokens(cmd: str) -> list[str]:
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    return [
        w for w in words if not w.startswith("-") and ("/" in w or w.startswith("~") or w == "..")
    ]


def validate(cands: list[Candidate], cwd: str, known: list[str], home: str) -> list[Candidate]:
    """Keep candidates whose paths exist (or whose parent exists); repair near-misses of
    payload paths (one wrong character in a 40-character sibling name); drop the rest."""
    out: list[Candidate] = []
    for c in cands:
        cmd = c.cmd
        ok = True
        for tok in _path_tokens(cmd):
            path = home + tok[1:] if tok.startswith("~") else tok
            absolute = os.path.normpath(path if path.startswith("/") else os.path.join(cwd, path))
            if os.path.exists(absolute):
                continue
            # A near-miss of a known path is a typo (40-character sibling names), even though
            # its parent exists; repair it before allowing "a new file in an existing dir".
            match = difflib.get_close_matches(tok.rstrip("/"), known, n=1, cutoff=0.9)
            if match:
                cmd = cmd.replace(tok.rstrip("/"), match[0])
                continue
            if os.path.isdir(os.path.dirname(absolute)):
                continue
            ok = False
            break
        if ok:
            out.append(Candidate(cmd.replace("\t", " ").replace("\n", " "), c.why, c.source))
    return out


def parse_candidates(text: str) -> list[Candidate]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ProviderError("no JSON in response")
    try:
        data = as_dict(json.loads(text[start : end + 1]))
    except ValueError as e:
        raise ProviderError(f"bad JSON in response: {e}") from e
    out: list[Candidate] = []
    for raw in as_list(data.get("candidates")):
        item = as_dict(raw)
        cmd = str(item.get("cmd") or "").strip()
        if cmd and "\n" not in cmd:
            out.append(Candidate(cmd, str(item.get("why") or "").replace("\t", " "), "llm"))
    if not out:
        raise ProviderError("no candidates in response")
    return out


# -- transport ---------------------------------------------------------------------------------


class NoDaemon(Exception):
    pass


def via_daemon(
    sock_path: str, instance: str, subject: str, req: CompletionRequest, deadline: float
) -> CompletionResult:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(max(0.05, min(0.5, deadline - time.monotonic())))
        try:
            s.connect(sock_path)
        except OSError as e:
            raise NoDaemon(str(e)) from e
        msg = {
            "v": 1,
            "op": "complete",
            "instance": instance,
            "subject": subject,
            "request": {
                "system": req.system,
                "user": req.user,
                "max_output_tokens": req.max_output_tokens,
                "timeout_s": req.timeout_s,
                "purpose": req.purpose,
            },
        }
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("daemon did not answer in time")
            s.settimeout(remaining)
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = as_dict(json.loads(buf or b"{}"))
    if not resp.get("ok"):
        raise ProviderError(str(resp.get("error") or "daemon error"))
    u = as_dict(resp.get("usage"))
    usage = Usage(
        unit=u.get("unit", "tokens"),
        input=u.get("input"),
        output=u.get("output"),
        requests=u.get("requests"),
    )
    return CompletionResult(str(resp.get("text", "")), str(resp.get("model", "")), usage, 0.0)


def in_process(rt: Runtime, instance: str, req: CompletionRequest) -> CompletionResult:
    from dirvana.providers.registry import build

    pc = rt.config.providers[instance]
    if pc.kind == "copilot":
        raise ProviderError("copilot needs the daemon (its runtime takes seconds to start)")
    provider = build(pc, rt.dirs)

    async def go() -> CompletionResult:
        try:
            return await provider.complete(req)
        finally:
            await provider.aclose()

    result = asyncio.run(go())
    rt.budget.record(
        pc, model=result.model, purpose=req.purpose, usage=result.usage, ok=True, node=None
    )
    return result


def call_with_deadline(
    rt: Runtime, instance: str, subject: str, req: CompletionRequest, deadline: float
) -> CompletionResult | Exception:
    """Run the provider call in a daemon thread; return its result, its error, or a
    ``TimeoutError`` once ``deadline`` passes (the thread is abandoned)."""
    box: queue.Queue[CompletionResult | Exception] = queue.Queue(1)

    def work() -> None:
        try:
            try:
                box.put(via_daemon(rt.socket_path, instance, subject, req, deadline))
            except NoDaemon:
                box.put(in_process(rt, instance, req))
        except Exception as e:
            box.put(e)

    threading.Thread(target=work, daemon=True).start()
    try:
        return box.get(timeout=max(0.0, deadline - time.monotonic()))
    except queue.Empty:
        return TimeoutError("deadline reached")


# -- commands ----------------------------------------------------------------------------------


def _exists(path: str) -> bool:
    return os.path.isdir(path)


def _pick_instance(rt: Runtime, subject: str) -> tuple[str | None, str]:
    allowed = rt.guard.allowed(subject)
    if not allowed:
        return None, "policy allows no provider here"
    for inst in allowed:
        why = rt.budget.exhausted(rt.config.providers[inst])
        if not why:
            return inst, ""
    return None, "budget exhausted"


def _finish(lines: list[str], code: int) -> int:
    sys.stdout.write("".join(line + "\n" for line in lines))
    sys.stdout.flush()
    if code == EXIT_FALLBACK:
        # A provider thread may still be blocked in DNS/TLS; never let it hold the prompt.
        os._exit(code)
    return code


def suggest(stdin_text: str, *, n: int | None = None) -> int:
    t0 = time.monotonic()
    rt = Runtime.open()
    req = parse_input(stdin_text)
    timeout = rt.config.num("hotkey", "timeout", 2.5)
    deadline = t0 + timeout
    n = n or int(rt.config.num("hotkey", "candidates", 8))
    today = time.gmtime(now())
    view = load_view(rt.store, req.cwd, today=_date(today), ranking=rt.config.ranking())
    local = local_candidates(view, req.buffer, n, _exists)
    fallback = [f"{c.cmd}\t{c.why}\t{c.source}" for c in local]
    instance, why = _pick_instance(rt, req.cwd)
    if instance is None:
        return _finish(fallback or [f"\t{why}\tlocal"], EXIT_FALLBACK)
    rendered: Rendered = render_suggest(
        view,
        buffer=req.buffer,
        ring=req.ring,
        n=n,
        instance=instance,
        guard=rt.guard,
        exists=_exists,
        home=rt.home,
        timeout=max(0.1, deadline - time.monotonic() - 0.05),
    )
    try:
        checked = rt.guard.check_request(req.cwd, instance, rendered.request)
    except Exception:
        return _finish(fallback, EXIT_FALLBACK)
    result = call_with_deadline(rt, instance, req.cwd, checked, deadline - 0.05)
    if isinstance(result, Exception):
        return _finish(fallback, EXIT_FALLBACK)
    try:
        cands = parse_candidates(result.text)
    except ProviderError:
        return _finish(fallback, EXIT_FALLBACK)
    known: list[str] = []
    for e in view.out_edges + view.in_edges:
        known += [e.peer, rel(e.peer, req.cwd)]
    good = validate(cands, req.cwd, known, rt.home)
    if not good:
        return _finish(fallback, EXIT_FALLBACK)
    lines = [f"{c.cmd}\t{c.why}\t{c.source}" for c in good[:n]]
    have = {c.cmd for c in good}
    lines += [f"{c.cmd}\t{c.why}\tlocal" for c in local if c.cmd not in have][
        : max(0, n - len(lines))
    ]
    return _finish(lines, 0)


def brief(stdin_text: str) -> int:
    t0 = time.monotonic()
    rt = Runtime.open()
    req = parse_input(stdin_text)
    deadline = t0 + rt.config.num("brief", "timeout", 4.0)
    ts = now()
    view = load_view(rt.store, req.cwd, today=_date(time.gmtime(ts)), ranking=rt.config.ranking())
    fallback = local_brief(view, req, time.strftime("%Y-%m-%d", time.gmtime(ts)))
    instance, _why = _pick_instance(rt, req.cwd)
    if instance is None:
        return _finish([fallback], EXIT_FALLBACK)
    rendered = render_brief(
        view,
        ring=req.ring,
        instance=instance,
        guard=rt.guard,
        exists=_exists,
        home=rt.home,
        now=ts,
        timeout=max(0.1, deadline - time.monotonic() - 0.05),
        max_tokens=int(rt.config.num("brief", "max_output_tokens", 500)),
        current_sid=req.sid,
    )
    try:
        checked = rt.guard.check_request(req.cwd, instance, rendered.request)
    except Exception:
        return _finish([fallback], EXIT_FALLBACK)
    result = call_with_deadline(rt, instance, req.cwd, checked, deadline - 0.05)
    if isinstance(result, Exception) or not result.text.strip():
        return _finish([fallback], EXIT_FALLBACK)
    return _finish([result.text.strip()], 0)


def _date(t: time.struct_time) -> Any:
    import datetime as dt

    return dt.date(t.tm_year, t.tm_mon, t.tm_mday)


def stdin_payload() -> str:
    return sys.stdin.read() if not sys.stdin.isatty() else ""


__all__ = ["Candidate", "brief", "local_candidates", "parse_input", "suggest", "validate"]
