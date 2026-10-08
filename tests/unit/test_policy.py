from __future__ import annotations

from pathlib import Path

import pytest

from dirvana.paths import Dirs
from dirvana.policy import (
    PolicyError,
    explain,
    is_ignored,
    load_config_rules,
    load_rules,
    parse_duration,
    parse_policy,
    resolve,
)
from tests.conftest import Env, zsh

HOME = "/home/tom"


def test_duration() -> None:
    assert parse_duration("90s") == 90
    assert parse_duration("1d12h") == 129600
    for bad in ("", "7", "d7", "7x", "1d 2h"):
        with pytest.raises(PolicyError):
            parse_duration(bad)


def test_parse_errors() -> None:
    with pytest.raises(PolicyError, match="unknown key"):
        parse_policy("~/x colour=blue", "f", "/", HOME)
    with pytest.raises(PolicyError, match="no settings"):
        parse_policy("~/x", "f", "/", HOME)
    with pytest.raises(PolicyError, match="before @root"):
        parse_policy("x ignore", "f", None, HOME)
    with pytest.raises(PolicyError, match="retention"):
        parse_policy("~/x retention=forever", "f", "/", HOME)
    with pytest.raises(PolicyError, match="llm"):
        parse_policy("~/x llm=a;b", "f", "/", HOME)


def test_comments_and_later_lines_win() -> None:
    rules = parse_policy(
        "# header\n~/work/** llm=copilot   # pin\n~/work/oss llm=anthropic,openai\n\n",
        "global",
        "/",
        HOME,
    )
    assert resolve(f"{HOME}/work/acme", rules).llm == ("copilot",)
    oss = resolve(f"{HOME}/work/oss/lib", rules)
    assert oss.llm == ("anthropic", "openai")
    assert str(oss.settings["llm"].source) == "global:3"


def test_llm_none_and_unset() -> None:
    rules = parse_policy("~/scratch llm=none", "g", "/", HOME)
    assert resolve(f"{HOME}/scratch/x", rules).llm == ()
    assert resolve(f"{HOME}/other", rules).llm is None


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_policy_d_order_deeper_root_wins(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    _write(cfg / "policy", "~/work llm=anthropic\n")
    _write(cfg / "policy.d" / "z-shallow.policy", "@root ~/work\n** llm=openai\n")
    _write(
        cfg / "policy.d" / "a-deep.policy",
        "# comment first\n@root ~/work/acme\n** llm=copilot\nvendor ignore\n",
    )
    rules = load_config_rules(cfg, HOME)
    assert resolve(f"{HOME}/work/x", rules).llm == ("openai",)
    eff = resolve(f"{HOME}/work/acme/src", rules)
    assert eff.llm == ("copilot",)
    assert "a-deep.policy:3" in str(eff.settings["llm"].source)
    assert is_ignored(f"{HOME}/work/acme/src/vendor/lib", rules)
    assert not is_ignored(f"{HOME}/work/vendor", rules)


def test_policy_d_requires_root(tmp_path: Path) -> None:
    _write(tmp_path / "policy.d" / "x.policy", "** llm=none\n")
    with pytest.raises(PolicyError, match="@root"):
        load_config_rules(tmp_path, HOME)


def test_builtins(tmp_path: Path) -> None:
    dirs = Dirs(tmp_path / "root", tmp_path / "cfg", tmp_path / "state")
    rules = load_rules(dirs, HOME)
    assert is_ignored(f"{HOME}/.ssh/config", rules)
    assert is_ignored(str(tmp_path / "root" / "system"), rules)
    tmp = resolve("/tmp/scratch", rules)
    assert tmp.retention == "ephemeral"
    assert tmp.ttl_seconds == 7 * 86400
    assert not tmp.ignored


def test_ignore_false_overrides(tmp_path: Path) -> None:
    dirs = Dirs(tmp_path / "root", tmp_path / "cfg", tmp_path / "state")
    _write(dirs.config / "policy", "~/.aws ignore=false\n")
    assert not is_ignored(f"{HOME}/.aws", load_rules(dirs, HOME))


def test_explain_lists_sources_and_providers() -> None:
    rules = parse_policy("~/work llm=copilot\n", "g", "/", HOME)
    lines = explain(resolve(f"{HOME}/work/a", rules), ["anthropic", "copilot"])
    text = "\n".join(lines)
    assert "llm        copilot" in text
    assert "g:1" in text
    assert "providers  copilot" in text
    lines = explain(resolve("/elsewhere", rules), ["anthropic"])
    assert "providers  anthropic" in "\n".join(lines)


def test_zsh_and_python_agree_on_ignore(env: Env) -> None:
    """Same policy files, same ignore decisions, including policy.d ordering."""
    _write(
        env.config / "policy", "~/secret ignore\n~/secret/public ignore=false\n**/private ignore\n"
    )
    _write(env.config / "policy.d" / "b.policy", "@root ~/proj\nbuild ignore\n")
    _write(env.config / "policy.d" / "a.policy", "@root ~/proj/sub\nbuild ignore=false\n")
    h = str(env.home)
    paths = [
        f"{h}/secret",
        f"{h}/secret/x",
        f"{h}/secret/public",
        f"{h}/secret/public/y",
        f"{h}/a/private",
        f"{h}/a/privately",
        f"{h}/proj/build",
        f"{h}/proj/x/build/z",
        f"{h}/proj/sub/build",
        f"{h}/.ssh",
        f"{h}/src",
        str(env.root / "system"),
    ]
    script = "\n".join(f"_dirvana_ignored '{p}' && print 1 || print 0" for p in paths)
    got = [line == "1" for line in zsh(script, env).splitlines()]
    dirs = Dirs(env.root, env.config, env.state)
    want = [is_ignored(p, load_rules(dirs, h)) for p in paths]
    assert got == want
    assert want == [True, True, False, False, True, False, True, True, False, True, False, True]
