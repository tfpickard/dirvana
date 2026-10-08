from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dirvana.budget import Budget
from dirvana.client import Candidate, local_candidates, parse_candidates, parse_input, validate
from dirvana.config import from_dict
from dirvana.context import NodeView
from dirvana.egress import PATH_MASK, EgressDenied, EgressGuard
from dirvana.paths import Dirs
from dirvana.policy import load_rules, parse_policy
from dirvana.prompts import command_allowed, enrich_input, fingerprints, render_enrich
from dirvana.providers.base import CompletionRequest, ProviderError, Usage
from dirvana.rank import EdgeView

HOME = "/home/tom"


def _guard(policy: str, providers: tuple[str, ...] = ("anthropic", "copilot")) -> EgressGuard:
    cfg = from_dict(
        {"providers": {"order": list(providers), **{p: {"kind": "mock"} for p in providers}}}
    )
    return EgressGuard(cfg, parse_policy(policy, "g", "/", HOME), HOME)


def test_allowed_respects_pins_and_none() -> None:
    g = _guard("~/work llm=copilot\n~/scratch llm=none\n")
    assert g.allowed(f"{HOME}/src/x") == ["anthropic", "copilot"]
    assert g.allowed(f"{HOME}/work/a") == ["copilot"]
    assert g.allowed(f"{HOME}/scratch/a") == []


def test_ignored_paths_allow_nothing(tmp_path: Path) -> None:
    dirs = Dirs(tmp_path / "r", tmp_path / "c", tmp_path / "s")
    cfg = from_dict({"providers": {"order": ["anthropic"], "anthropic": {"kind": "mock"}}})
    g = EgressGuard(cfg, load_rules(dirs, HOME), HOME)
    assert g.allowed(f"{HOME}/.ssh/keys") == []
    assert g.decision(f"{HOME}/.ssh") == "none"


def test_scrub_masks_forbidden_paths_and_secrets() -> None:
    g = _guard("~/scratch llm=none\n")
    text = (
        f"see {HOME}/scratch/plan.txt and ~/scratch/x but keep {HOME}/src/ok "
        "and https://example.com/a/b; export API_TOKEN=abc"
    )
    out = g.scrub(text, "anthropic")
    assert f"{HOME}/scratch" not in out and "~/scratch" not in out
    assert out.count(PATH_MASK) == 2
    assert f"{HOME}/src/ok" in out and "https://example.com/a/b" in out
    assert "API_TOKEN=<redacted>" in out


def test_check_request_refuses_subject() -> None:
    g = _guard("~/work llm=copilot\n")
    req = CompletionRequest("s", "u", 10, 1.0, "suggest")
    with pytest.raises(EgressDenied):
        g.check_request(f"{HOME}/work/x", "anthropic", req)
    assert g.check_request(f"{HOME}/work/x", "copilot", req).user == "u"


def test_command_allowed_resolves_relative_paths() -> None:
    g = _guard("~/secret llm=none\n")
    cwd = f"{HOME}/proj"
    assert command_allowed("ls ../src", cwd, "anthropic", g)
    assert not command_allowed("cat ../secret/x", cwd, "anthropic", g)
    assert not command_allowed("cat ~/secret/x", cwd, "anthropic", g)
    assert not command_allowed("ls", f"{HOME}/secret/a", "anthropic", g)


def _edge(peer: str, *, tentative: bool = False, n: int = 4) -> EdgeView:
    return EdgeView(
        peer,
        "out",
        {"diff": n},
        n,
        2,
        "2026-09-01",
        "2026-10-01",
        1.0,
        tentative,
        ("Makefile",),
        None,
        ("m",),
    )


def _view(path: str, edges: list[EdgeView]) -> NodeView:
    return NodeView(
        path=path,
        identity="path:~/proj",
        recon={},
        notes="",
        labels={},
        commands=[],
        out_edges=edges,
        in_edges=[],
        derived=None,
        derived_meta={},
        obs_count=0,
    )


def test_enrich_input_is_deterministic_and_filters_per_provider() -> None:
    g = _guard("~/secret llm=none\n")
    view = _view(
        f"{HOME}/proj",
        [_edge(f"{HOME}/src/b"), _edge(f"{HOME}/secret/s"), _edge(f"{HOME}/src/c", tentative=True)],
    )
    data = enrich_input(view, g)
    assert [e["abs"] for e in data["outbound"]] == [f"{HOME}/secret/s", f"{HOME}/src/b"]
    assert fingerprints(data) == fingerprints(json.loads(json.dumps(data)))
    rendered = render_enrich(data, "anthropic", g, max_tokens=10, timeout=1)
    assert "secret" not in rendered.request.user
    assert rendered.mentions == (f"{HOME}/src/b",)


def test_fingerprint_changes_with_policy_but_not_with_provider() -> None:
    view = _view(f"{HOME}/proj", [_edge(f"{HOME}/src/b")])
    a = fingerprints(enrich_input(view, _guard("")))
    b = fingerprints(enrich_input(view, _guard("", providers=("copilot",))))
    c = fingerprints(enrich_input(view, _guard("~/src llm=copilot\n")))
    assert a == b
    assert a[1] != c[1]


def test_validate_repairs_near_miss_and_drops_inventions(tmp_path: Path) -> None:
    a = tmp_path / "acme-build-config-2026q3-variant-a"
    b = tmp_path / "acme-build-config-2026q3-variant-b"
    a.mkdir()
    b.mkdir()
    known = [str(b), "../acme-build-config-2026q3-variant-b"]
    cands = [
        Candidate("diff -ru ../acme-build-config-2026q3-variant-b .", "", "llm"),
        Candidate("diff -ru ../acme-build-config-2026q3-varaint-b .", "", "llm"),  # typo
        Candidate("ls ../completely/made-up", "", "llm"),
        Candidate("cp Makefile ../acme-build-config-2026q3-variant-b/Makefile.new", "", "llm"),
        Candidate("make test", "", "llm"),
    ]
    got = [c.cmd for c in validate(cands, str(a), known, "/home/x")]
    assert got == [
        "diff -ru ../acme-build-config-2026q3-variant-b .",
        "diff -ru ../acme-build-config-2026q3-variant-b .",
        "cp Makefile ../acme-build-config-2026q3-variant-b/Makefile.new",
        "make test",
    ]


def test_parse_candidates() -> None:
    text = 'Sure! {"candidates": [{"cmd": "ls ../b", "why": "look"}, {"cmd": "a\\nb"}]}'
    assert parse_candidates(text) == [Candidate("ls ../b", "look", "llm")]
    with pytest.raises(ProviderError):
        parse_candidates("no json here")


def test_parse_input_incognito_drops_history() -> None:
    req = parse_input(
        json.dumps({"cwd": "/", "ring": [{"cmd": "x", "cwd": "/"}], "incognito": True})
    )
    assert req.ring == [] and req.incognito


def test_local_candidates_use_edges_and_buffer(tmp_path: Path) -> None:
    b = tmp_path / "b"
    b.mkdir()
    view = _view(str(tmp_path / "a"), [_edge(str(b))])
    cands = local_candidates(view, "", 5, lambda p: Path(p).is_dir())
    assert cands[0].cmd == "diff ../b/Makefile Makefile" and cands[0].source == "local"
    only = local_candidates(view, "ls", 5, lambda p: Path(p).is_dir())
    assert only[0].cmd.startswith("ls")


def test_budget_caps(tmp_path: Path) -> None:
    cfg = from_dict(
        {"providers": {"x": {"kind": "mock", "max_tokens_per_run": 100, "max_tokens_per_day": 150}}}
    )
    pc = cfg.providers["x"]
    budget = Budget(tmp_path / "usage.jsonl")
    assert budget.exhausted(pc) is None
    budget.record(
        pc, model="m", purpose="enrich", usage=Usage("tokens", 60, 50), ok=True, node=None
    )
    assert "per-run" in (budget.exhausted(pc) or "")
    fresh = Budget(tmp_path / "usage.jsonl")
    assert fresh.exhausted(pc) is None
    fresh.record(pc, model="m", purpose="enrich", usage=Usage("tokens", 30, 20), ok=True, node=None)
    assert "daily" in (Budget(tmp_path / "usage.jsonl").exhausted(pc) or "")
    assert fresh.spent_today("x", time.time() + 86400 * 2) == 0
