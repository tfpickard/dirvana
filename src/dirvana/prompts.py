"""Prompt construction. Enrichment input is deterministic (fingerprinted); hotkey prompts are
built per request. Every builder consults the egress guard per item and reports which peer
directories it mentioned."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dirvana.context import NodeView, day, excerpts, git_headline, rel, week
from dirvana.egress import EgressGuard
from dirvana.providers.base import CompletionRequest
from dirvana.rank import EdgeView
from dirvana.store.io import as_list

ENRICH_TEMPLATE = "enrich/1"

ENRICH_SYSTEM = """\
You write a short, factual briefing about one directory on a developer's machine, for a
command-line assistant that will later suggest shell commands there. You are given recon
(git facts, file excerpts), what the user runs there, and the directories they habitually
reach from it (outbound) and arrive from (inbound), with verbs like diff, copy-from, edit.

Write Markdown with exactly these sections, each a few terse bullets:
## What this is
## What you do here
## Places you reach from here      (for each outbound edge: relative path, what for, typical files)
## Places that reach here          (for each inbound edge: relative path, what for)
## Watch out for
Use paths exactly as given (prefer the "rel" form). Do not invent directories, files or
commands. If a section has nothing, write "- none". No preamble, no closing remarks."""

SUGGEST_SYSTEM = """\
You suggest shell commands (zsh) for the user's current directory. You are given a briefing
about the directory, ranked related directories ("edges", with exact absolute and relative
paths), the user's partially typed command line (may be empty) and recent commands from this
session. Reply with JSON only: {"candidates": [{"cmd": "...", "why": "..."}]} with at most
N candidates, best first. Each cmd is one line, ready to run. Use edge paths exactly as given
(prefer "rel"); never invent paths. If the command line is non-empty, complete or improve it.
Prefer read-only and comparison commands; never suggest destructive commands (rm -rf, force
pushes, overwriting without -i) unless the command line asks for it. "why" is under 12 words."""

BRIEF_SYSTEM = """\
You brief a developer who just returned to a directory: what it is, what changed since they
were last here, and what they are most likely about to do (with 1-3 concrete commands using
the exact paths given). Plain text, at most 8 short lines, no Markdown headings."""


def _edge(e: EdgeView, base: str) -> dict[str, Any]:
    return {
        "abs": e.peer,
        "rel": rel(e.peer, base),
        "verbs": dict(e.verbs),
        "n": e.n,
        "first_week": week(e.first),
        "last_week": week(e.last),
        "examples": list(e.examples[:5]),
    }


def enrich_input(view: NodeView, guard: EgressGuard) -> dict[str, Any]:
    """Provider-independent, deterministic input for the enrichment prompt."""

    def edges(views: Sequence[EdgeView]) -> list[dict[str, Any]]:
        established = [e for e in views if not e.tentative]
        established.sort(key=lambda e: (-e.n, e.peer))
        return [{**_edge(e, view.path), "policy": guard.decision(e.peer)} for e in established[:10]]

    return {
        "template": ENRICH_TEMPLATE,
        "directory": view.path,
        "identity": view.identity,
        "git": git_headline(view.recon),
        "files": excerpts(view.recon),
        "notes": view.notes,
        "labels": dict(sorted(view.labels.items())),
        "outbound": edges(view.out_edges),
        "inbound": edges(view.in_edges),
        "commands": {
            "total": len(view.commands),
            "top": [[c, n] for c, n in view.top_commands(12)],
            "recent": sorted({c.cmd for c in view.recent(12)}),
        },
    }


def fingerprints(data: dict[str, Any]) -> tuple[str, str]:
    """(full, structural): structural ignores the command lists, so commands alone can be
    held back by ``min_new_observations`` while recon, edge or policy changes are not."""

    def h(obj: object) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()

    structural = {k: v for k, v in data.items() if k != "commands"}
    return h(data), h(structural)


@dataclass(frozen=True, slots=True)
class Rendered:
    request: CompletionRequest
    mentions: tuple[str, ...]


def render_enrich(
    data: dict[str, Any], instance: str, guard: EgressGuard, *, max_tokens: int, timeout: float
) -> Rendered:
    body = json.loads(json.dumps(data))
    mentions: list[str] = []
    for key in ("outbound", "inbound"):
        kept = []
        for e in body[key]:
            if guard.allows(e["abs"], instance):
                e.pop("policy", None)
                kept.append(e)
                mentions.append(e["abs"])
        body[key] = kept
    directory = str(body["directory"])
    cmds = body["commands"]
    cmds["top"] = [t for t in cmds["top"] if command_allowed(t[0], directory, instance, guard)]
    cmds["recent"] = [c for c in cmds["recent"] if command_allowed(c, directory, instance, guard)]
    body.pop("template", None)
    user = "Directory facts (JSON):\n" + json.dumps(body, indent=1, sort_keys=True)
    req = CompletionRequest(ENRICH_SYSTEM, user, max_tokens, timeout, "enrich")
    return Rendered(req, tuple(sorted(set(mentions))))


def command_allowed(cmd: str, cwd: str, instance: str, guard: EgressGuard, home: str = "") -> bool:
    """A command may be shared only if its cwd and every path it names (relative ones
    resolved against the cwd) allow the instance."""
    home = home or guard.home_dir
    if not cwd or not guard.allows(cwd, instance):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    for w in words:
        if w.startswith("~"):
            w = home + w[1:]
        if w.startswith("/") or "/" in w or w == "..":
            p = os.path.normpath(w if w.startswith("/") else os.path.join(cwd, w))
            if not guard.allows(p, instance):
                return False
    return True


def derived_allowed(view: NodeView, instance: str, guard: EgressGuard) -> bool:
    if not view.derived or "mentions" not in view.derived_meta:
        return False
    return all(guard.allows(str(m), instance) for m in as_list(view.derived_meta["mentions"]))


def _summary(view: NodeView, instance: str, guard: EgressGuard) -> dict[str, Any]:
    return {
        "git": git_headline(view.recon),
        "files": sorted(excerpts(view.recon)),
        "top_commands": [
            [c, n]
            for c, n in view.top_commands(10)
            if command_allowed(c, view.path, instance, guard)
        ],
        "notes": view.notes,
    }


def _hotkey_edges(
    view: NodeView, instance: str, guard: EgressGuard, exists: Callable[[str], bool]
) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    mentions: list[str] = []
    for direction, views, limit in (("out", view.out_edges, 8), ("in", view.in_edges, 4)):
        kept = 0
        for e in views:
            if e.tentative or kept >= limit:
                continue
            if not guard.allows(e.peer, instance) or not exists(e.peer):
                continue
            out.append({"direction": direction, **_edge(e, view.path), "last": e.last})
            mentions.append(e.peer)
            kept += 1
    return out, mentions


def render_suggest(
    view: NodeView,
    *,
    buffer: str,
    ring: Sequence[tuple[str, str]],
    n: int,
    instance: str,
    guard: EgressGuard,
    exists: Callable[[str], bool],
    home: str,
    timeout: float,
) -> Rendered:
    edges, mentions = _hotkey_edges(view, instance, guard, exists)
    recent = [
        {"cmd": c, "cwd": rel(w, view.path)}
        for c, w in ring
        if command_allowed(c, w, instance, guard, home)
    ][-12:]
    payload: dict[str, Any] = {
        "cwd": view.path,
        "command_line": buffer,
        "max_candidates": n,
        "briefing": view.derived if derived_allowed(view, instance, guard) else None,
        "summary": _summary(view, instance, guard),
        "edges": edges,
        "recent_session_commands": recent,
    }
    user = json.dumps(payload, indent=1, ensure_ascii=False)
    system = SUGGEST_SYSTEM.replace("at most\nN", f"at most\n{n}")
    req = CompletionRequest(system, user, 700, timeout, "suggest")
    return Rendered(req, tuple(mentions))


def render_brief(
    view: NodeView,
    *,
    ring: Sequence[tuple[str, str]],
    instance: str,
    guard: EgressGuard,
    exists: Callable[[str], bool],
    home: str,
    now: float,
    timeout: float,
    max_tokens: int,
    current_sid: str,
) -> Rendered:
    edges, mentions = _hotkey_edges(view, instance, guard, exists)
    previous = [c for c in view.commands if c.sid != current_sid]
    last_visit = max((c.t for c in previous), default=None)
    payload: dict[str, Any] = {
        "cwd": view.path,
        "today": day(now),
        "last_visit": day(last_visit) if last_visit else None,
        "briefing": view.derived if derived_allowed(view, instance, guard) else None,
        "summary": _summary(view, instance, guard),
        "recent_here": [
            {"cmd": c.cmd, "day": day(c.t), "exit": c.st}
            for c in view.recent(10)
            if command_allowed(c.cmd, view.path, instance, guard)
        ],
        "edges": edges,
        "new_arrivals_since_last_visit": [
            e["rel"]
            for e in edges
            if e["direction"] == "in" and last_visit and e["last"] > day(last_visit)
        ],
        "recent_session_commands": [
            {"cmd": c, "cwd": rel(w, view.path)}
            for c, w in ring
            if command_allowed(c, w, instance, guard, home)
        ][-8:],
    }
    user = json.dumps(payload, indent=1, ensure_ascii=False)
    return Rendered(
        CompletionRequest(BRIEF_SYSTEM, user, max_tokens, timeout, "brief"), tuple(mentions)
    )
