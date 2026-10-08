"""Edge ranking: recency decay plus an evidence floor.

``score = conf(n) * sum_verb w_verb * sum_day count_day * 2 ** (-age_days / half_life)`` with
``conf(n) = n / (n + k)``. Edges with too little evidence (fewer than ``min_evidence``
observations or ``min_sessions`` sessions) are *tentative*: listed separately and never sent to
a provider, so one stray command cannot outrank a well-evidenced habit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from dirvana.store.io import as_dict, as_list

DEFAULT_VERB_WEIGHTS: Final[Mapping[str, float]] = {
    "diff": 3.0,
    "copy-from": 3.0,
    "copy-to": 3.0,
    "move-from": 2.0,
    "move-to": 2.0,
    "edit": 2.0,
    "run": 2.0,
    "read": 1.5,
    "search": 1.5,
    "list": 1.0,
    "cd": 0.5,
    "ref": 0.5,
}


@dataclass(frozen=True, slots=True)
class RankConfig:
    half_life_days: float = 30.0
    k: float = 3.0
    min_evidence: int = 2
    min_sessions: int = 2
    downweight: float = 0.3
    verb_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_VERB_WEIGHTS))


@dataclass(frozen=True, slots=True)
class EdgeView:
    peer: str
    direction: Literal["out", "in"]
    verbs: Mapping[str, int]
    n: int
    sessions: int
    first: str
    last: str
    score: float
    tentative: bool
    examples: tuple[str, ...]
    peer_identity: str | None
    machines: tuple[str, ...]


def _age_days(day: str, today: date) -> float:
    try:
        return max(0.0, float((today - date.fromisoformat(day)).days))
    except ValueError:
        return 365.0


def _is_ancestor(peer: str, path: str) -> bool:
    return peer == "/" or path.startswith(peer.rstrip("/") + "/")


def view(
    entry: Mapping[str, Any],
    direction: Literal["out", "in"],
    *,
    today: date,
    self_path: str,
    home_dir: str,
    cfg: RankConfig,
) -> EdgeView:
    peer = str(entry.get("peer", ""))
    verbs: dict[str, int] = {}
    n = 0
    sessions = 0
    first = ""
    last = ""
    weighted = 0.0
    examples: list[str] = []
    machines = as_dict(entry.get("by_machine"))
    for ms_raw in machines.values():
        ms = as_dict(ms_raw)
        m_sessions = 0
        for verb, st_raw in as_dict(ms.get("verbs")).items():
            st = as_dict(st_raw)
            vn = int(st.get("n", 0))
            verbs[verb] = verbs.get(verb, 0) + vn
            n += vn
            m_sessions = max(m_sessions, int(st.get("sessions", 0)))
            f, la = str(st.get("first", "")), str(st.get("last", ""))
            first = min(first, f) if first and f else (first or f)
            last = max(last, la)
            w = cfg.verb_weights.get(verb, 0.5)
            days = {str(k): int(v) for k, v in as_dict(st.get("days")).items()}
            for day, count in days.items():
                weighted += w * count * 2.0 ** (-_age_days(day, today) / cfg.half_life_days)
            older = int(st.get("older", 0))
            if older:
                oldest = min(days) if days else f
                weighted += w * older * 2.0 ** (-_age_days(oldest, today) / cfg.half_life_days)
        sessions += m_sessions
        for ex in as_list(ms.get("examples")):
            if str(ex) not in examples:
                examples.append(str(ex))
    score = n / (n + cfg.k) * weighted if n else 0.0
    # Going "home" or "up" is navigation, not a relation worth suggesting.
    if peer == home_dir.rstrip("/") or _is_ancestor(peer, self_path):
        score *= cfg.downweight
    tentative = n < cfg.min_evidence or sessions < cfg.min_sessions
    pid = entry.get("peer_identity")
    return EdgeView(
        peer=peer,
        direction=direction,
        verbs=dict(sorted(verbs.items(), key=lambda kv: (-kv[1], kv[0]))),
        n=n,
        sessions=sessions,
        first=first,
        last=last,
        score=score,
        tentative=tentative,
        examples=tuple(examples),
        peer_identity=str(pid) if pid else None,
        machines=tuple(sorted(machines)),
    )


def rank(
    doc: Mapping[str, Any] | None,
    direction: Literal["out", "in"],
    *,
    today: date,
    self_path: str,
    home_dir: str,
    cfg: RankConfig | None = None,
) -> list[EdgeView]:
    """All edges of one direction, best first; tentative edges sort after established ones."""
    cfg = cfg or RankConfig()
    edges = as_list((doc or {}).get("edges"))
    entries = [d for d in map(as_dict, edges) if d]
    views = [
        view(e, direction, today=today, self_path=self_path, home_dir=home_dir, cfg=cfg)
        for e in entries
    ]
    views.sort(key=lambda v: (v.tentative, -v.score, v.peer))
    return views
