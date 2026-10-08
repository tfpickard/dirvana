"""Golden vectors: the zsh and Python implementations must agree on shared semantics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from dirvana import policy, redact
from dirvana._meta import NAME
from dirvana.paths import shadow_rel
from tests.conftest import VECTORS, Env, zsh


def _load(name: str) -> Any:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def _nul_batch(env: Env, body: str, inputs: list[str]) -> list[str]:
    """Feed NUL-separated inputs to a zsh loop; collect NUL-separated outputs."""
    script = (
        "local x\n"
        "while IFS= read -r -d $'\\0' x; do\n"
        f"{body}\n"
        "  print -rn -- \"$REPLY\"$'\\0'\n"
        "done\n"
    )
    out = zsh(script, env, input="".join(s + "\0" for s in inputs))
    return out.split("\0")[:-1]


REDACT = _load("redact.json")


@pytest.mark.parametrize("case", REDACT, ids=[c["name"] for c in REDACT])
def test_redact_python(case: dict[str, str]) -> None:
    assert redact.redact(case["in"]) == case["out"]


def test_redact_zsh(env: Env) -> None:
    got = _nul_batch(env, '  _dirvana_redact "$x"', [c["in"] for c in REDACT])
    mismatches = [
        (c["name"], c["out"], g) for c, g in zip(REDACT, got, strict=True) if c["out"] != g
    ]
    assert mismatches == []


IGNORE = _load("ignore.json")


@pytest.mark.parametrize(
    "case", IGNORE["cases"], ids=[f"{c['pattern']}~{c['path']}" for c in IGNORE["cases"]]
)
def test_ignore_python(case: dict[str, Any]) -> None:
    rx = policy.compile_glob(case["base"], case["pattern"], IGNORE["home"])
    assert (rx.fullmatch(case["path"]) is not None) == case["match"]


def test_ignore_zsh(env: Env) -> None:
    lines: list[str] = []
    for c in IGNORE["cases"]:
        lines.append(
            f"_dirvana_glob2pat {_q(c['base'])} {_q(c['pattern'])}; "
            f"[[ {_q(c['path'])} == ${{~REPLY}} ]] && print 1 || print 0"
        )
    out = zsh(f"HOME={IGNORE['home']}\nsetopt extended_glob\n" + "\n".join(lines), env)
    got = [line == "1" for line in out.splitlines()]
    want = [c["match"] for c in IGNORE["cases"]]
    bad = [c for c, g, w in zip(IGNORE["cases"], got, want, strict=True) if g != w]
    assert bad == []


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


SHADOW = _load("shadow.json")


@pytest.mark.parametrize("case", SHADOW, ids=[c["path"] for c in SHADOW])
def test_shadow_python(case: dict[str, str]) -> None:
    assert shadow_rel(case["path"]) == case["shadow"]


def test_shadow_zsh(env: Env) -> None:
    got = _nul_batch(env, '  _dirvana_shadow "$x"', [c["path"] for c in SHADOW])
    base = f"{env.root}/system"
    want = [base + ("/" + c["shadow"] if c["shadow"] else "") for c in SHADOW]
    assert got == want


def test_machine_id_is_created_once_and_stable(env: Env) -> None:
    first = zsh("print -r -- $_dirvana_mid", env).strip()
    stored = (env.state / "machine-id").read_text(encoding="ascii").strip()
    assert len(stored) == 32 and stored.startswith(first) and len(first) == 12
    assert zsh("print -r -- $_dirvana_mid", env).strip() == first
    assert (env.state / "machine-id").read_text(encoding="ascii").strip() == stored


def test_name_constant_matches(env: Env) -> None:
    assert zsh("print -r -- $_DIRVANA_NAME", env).strip() == NAME


CAPTURE = _load("capture.json")


def _build_tree(base: Path, tree: dict[str, Any]) -> None:
    for d in tree["dirs"]:
        (base / d).mkdir(parents=True, exist_ok=True)
    for f in tree["files"]:
        (base / f).write_text("x\n", encoding="utf-8")
    for link, target in tree["symlinks"]:
        (base / link).symlink_to(target)


def _rel(node: str, base: Path) -> str:
    return "" if node == str(base) else os.path.relpath(node, base)


def test_capture_zsh(env: Env, tmp_path: Path) -> None:
    base = Path(os.path.realpath(tmp_path / "t"))
    _build_tree(base, CAPTURE["tree"])
    cwd = base / CAPTURE["cwd"]
    home = base / CAPTURE["home"]
    body = (
        '  _dirvana_capture "$x"\n'
        '  REPLY="[$_dirvana_paths]"$\'\\x1f\'"${(j:\\x1e:)_dirvana_ignored_words}"'
    )
    script = (
        "_dirvana_policy_load\n_dirvana_enter\nlocal x\n"
        "while IFS= read -r -d $'\\0' x; do\n"
        f"{body}\n"
        "  print -rn -- \"$REPLY\"$'\\0'\n"
        "done\n"
    )
    cases = CAPTURE["cases"]
    out = zsh(
        script,
        env,
        cwd=cwd,
        input="".join(c["cmd"] + "\0" for c in cases),
        extra={"HOME": str(home)},
    )
    results = out.split("\0")[:-1]
    assert len(results) == len(cases)
    failures = []
    for case, res in zip(cases, results, strict=True):
        paths_json, _, ignored = res.partition("\x1f")
        edges = json.loads(paths_json)
        got = sorted([e["verb"], _rel(e["node"], base)] for e in edges)
        want = sorted([v, n] for v, n in case["edges"])
        got_ignored = [w for w in ignored.split("\x1e") if w]
        if got != want or got_ignored != case.get("ignored_words", []):
            failures.append((case["cmd"], want, got, got_ignored))
    assert failures == []
