from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from dirvana.rank import RankConfig, rank

TODAY = date(2026, 10, 7)
SELF = "/home/tom/src/a"
HOME = "/home/tom"


def _edge(peer: str, verb: str, days: dict[int, int], sessions: int) -> dict[str, Any]:
    d = {(TODAY - timedelta(days=ago)).isoformat(): n for ago, n in days.items()}
    total = sum(days.values())
    return {
        "peer": peer,
        "by_machine": {
            "m1": {
                "verbs": {
                    verb: {
                        "n": total,
                        "first": min(d),
                        "last": max(d),
                        "sessions": sessions,
                        "days": d,
                        "older": 0,
                    }
                },
                "examples": [],
            }
        },
    }


def _rank(*edges: dict[str, Any]) -> list[str]:
    views = rank({"edges": list(edges)}, "out", today=TODAY, self_path=SELF, home_dir=HOME)
    return [v.peer for v in views]


def test_single_fresh_observation_loses_to_habit() -> None:
    stray = _edge("/home/tom/src/stray", "diff", {1: 1}, 1)
    habit = _edge("/home/tom/src/b", "list", dict.fromkeys(range(0, 21, 2), 1) | {20: 2}, 8)
    assert _rank(stray, habit) == ["/home/tom/src/b", "/home/tom/src/stray"]


def test_evidence_floor_marks_tentative() -> None:
    views = rank(
        {
            "edges": [
                _edge("/x", "diff", {0: 1}, 1),
                _edge("/y", "diff", {0: 5}, 1),
                _edge("/z", "diff", {0: 2, 3: 1}, 2),
            ]
        },
        "out",
        today=TODAY,
        self_path=SELF,
        home_dir=HOME,
    )
    tentative = {v.peer: v.tentative for v in views}
    assert tentative == {"/x": True, "/y": True, "/z": False}
    assert views[0].peer == "/z"


def test_recency_decay() -> None:
    old = _edge("/old", "diff", {120: 6}, 6)
    new = _edge("/new", "diff", {2: 6}, 6)
    assert _rank(old, new) == ["/new", "/old"]


def test_half_life_is_configurable_at_read_time() -> None:
    e = _edge("/p", "diff", {30: 4}, 4)
    s30 = rank({"edges": [e]}, "out", today=TODAY, self_path=SELF, home_dir=HOME)[0].score
    s15 = rank(
        {"edges": [e]},
        "out",
        today=TODAY,
        self_path=SELF,
        home_dir=HOME,
        cfg=RankConfig(half_life_days=15),
    )[0].score
    assert abs(s30 / s15 - 2.0) < 1e-9


def test_navigation_targets_are_downweighted() -> None:
    home = _edge(HOME, "cd", {0: 10}, 10)
    parent = _edge("/home/tom/src", "list", {0: 10}, 10)
    peer = _edge("/home/tom/src/b", "list", {0: 10}, 10)
    assert _rank(home, parent, peer)[0] == "/home/tom/src/b"
