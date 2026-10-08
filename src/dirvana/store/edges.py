"""Edge aggregation: fold observations into per-peer, per-machine, per-verb evidence.

The aggregate keeps counts, first/last day, distinct sessions, a bounded per-day histogram
and a few example file names. Recency decay is computed at read time (:mod:`dirvana.rank`),
so the stored data never depends on the half-life.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from dirvana.store.io import as_dict, as_list

VERBS: Final = (
    "list",
    "read",
    "search",
    "copy-from",
    "copy-to",
    "move-from",
    "move-to",
    "diff",
    "edit",
    "cd",
    "run",
    "ref",
)
MAX_DAYS: Final = 90
MAX_EXAMPLES: Final = 5


def day_of(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).date().isoformat()


@dataclass(slots=True)
class VerbStats:
    n: int = 0
    first: str = ""
    last: str = ""
    sessions: set[str] = field(default_factory=set[str])
    days: dict[str, int] = field(default_factory=dict[str, int])
    older: int = 0

    def add(self, day: str, sid: str) -> None:
        self.n += 1
        self.first = min(self.first, day) if self.first else day
        self.last = max(self.last, day)
        if sid:
            self.sessions.add(sid)
        self.days[day] = self.days.get(day, 0) + 1

    def to_json(self) -> dict[str, Any]:
        days = sorted(self.days.items(), reverse=True)
        keep, drop = days[:MAX_DAYS], days[MAX_DAYS:]
        return {
            "n": self.n,
            "first": self.first,
            "last": self.last,
            "sessions": len(self.sessions),
            "days": dict(sorted(keep)),
            "older": self.older + sum(c for _, c in drop),
        }


@dataclass(slots=True)
class MachineStats:
    verbs: dict[str, VerbStats] = field(default_factory=dict[str, VerbStats])
    examples: list[tuple[float, str]] = field(default_factory=list[tuple[float, str]])

    def to_json(self) -> dict[str, Any]:
        seen: dict[str, float] = {}
        for t, ex in self.examples:
            seen[ex] = max(t, seen.get(ex, 0.0))
        recent = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_EXAMPLES]
        return {
            "verbs": {v: s.to_json() for v, s in sorted(self.verbs.items())},
            "examples": [ex for ex, _ in recent],
        }


def aggregate(
    observations: Iterable[Mapping[str, Any]],
    *,
    self_path: str,
    skip_peer: Callable[[str], bool],
) -> dict[str, dict[str, MachineStats]]:
    """Fold observations recorded in node ``self_path`` into ``peer -> mid -> MachineStats``."""
    out: dict[str, dict[str, MachineStats]] = {}

    def bump(peer: str, verb: str, obs: Mapping[str, Any], example: str | None) -> None:
        if not peer or peer == self_path or verb not in VERBS or skip_peer(peer):
            return
        t = obs.get("t")
        if not isinstance(t, (int, float)):
            return
        mid = str(obs.get("mid") or "unknown")
        ms = out.setdefault(peer, {}).setdefault(mid, MachineStats())
        ms.verbs.setdefault(verb, VerbStats()).add(day_of(float(t)), str(obs.get("sid") or ""))
        if example:
            ms.examples.append((float(t), example))

    for obs in observations:
        kind = obs.get("k")
        if kind == "cd":
            bump(str(obs.get("to") or ""), "cd", obs, None)
        elif kind == "cmd":
            counted: set[tuple[str, str]] = set()
            for raw in as_list(obs.get("paths")):
                p = as_dict(raw)
                node, verb, abs_ = (
                    str(p.get("node") or ""),
                    str(p.get("verb") or ""),
                    str(p.get("abs") or ""),
                )
                example = None
                if abs_ and node and abs_ != node and abs_.startswith(node.rstrip("/") + "/"):
                    example = os.path.relpath(abs_, node)
                if (node, verb) in counted:
                    if example:
                        ms = out.get(node, {}).get(str(obs.get("mid") or "unknown"))
                        if ms is not None:
                            ms.examples.append((float(obs.get("t") or 0.0), example))
                    continue
                counted.add((node, verb))
                bump(node, verb, obs, example)
    return out


def to_document(
    agg: Mapping[str, Mapping[str, MachineStats]],
    identities: Mapping[str, str | None],
) -> dict[str, Any]:
    edges = []
    for peer in sorted(agg):
        edges.append(
            {
                "peer": peer,
                "peer_identity": identities.get(peer),
                "peer_hint": peer.rstrip("/").rsplit("/", 1)[-1] or "/",
                "by_machine": {mid: ms.to_json() for mid, ms in sorted(agg[peer].items())},
            }
        )
    return {"v": 1, "edges": edges}


def entries_by_peer(doc: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not doc:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw in as_list(doc.get("edges")):
        e = as_dict(raw)
        if "peer" in e:
            out[str(e["peer"])] = e
    return out
