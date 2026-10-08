"""Hook record format: what the zsh side writes must round-trip through the Python reader."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dirvana.paths import OBS_FILE
from dirvana.store.io import read_jsonl
from tests.conftest import ADAPTER, Env, zsh


def _record(env: Env, cwd: Path, cmd: bytes) -> list[dict[str, object]]:
    """Run one command through preexec/precmd (as an interactive shell would)."""
    script = (
        "_dirvana_enter\n"
        "local c\n"
        "IFS= read -r -d '' c\n"
        '_dirvana_preexec "$c" "$c" "$c"\n'
        "_dirvana_precmd\n"
    )
    proc = subprocess.run(
        ["zsh", "-f", "-c", f"source {ADAPTER}\n{script}"],
        env=env.vars,
        cwd=cwd,
        input=cmd + b"\0",
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return list(read_jsonl(env.system(cwd) / OBS_FILE))


def test_json_escaping_roundtrip(env: Env) -> None:
    cmd = "printf \"a\\tb\\\\c\" 'q\"uote' $'\x01' héllo\tTAB"
    [rec] = _record(env, env.home, cmd.encode())
    assert rec["cmd"] == cmd
    assert rec["k"] == "cmd" and rec["st"] == 0 and rec["cwd"] == str(env.home)


def test_long_commands_are_truncated_and_flagged(env: Env) -> None:
    [rec] = _record(env, env.home, b"echo " + b"a" * 1500)
    cmd = rec["cmd"]
    assert isinstance(cmd, str) and len(cmd) == 1024
    assert rec["trunc"] is True


def test_non_utf8_bytes_survive(env: Env) -> None:
    [rec] = _record(env, env.home, b"echo caf\xe9")
    assert rec["cmd"] == "echo caf\udce9"


def test_secret_redacted_before_write(env: Env) -> None:
    [rec] = _record(env, env.home, b"export AWS_SECRET_ACCESS_KEY=hunter2hunter2")
    assert rec["cmd"] == "export AWS_SECRET_ACCESS_KEY=<redacted>"
    raw = (env.system(env.home) / OBS_FILE).read_bytes()
    assert b"hunter2" not in raw


def test_hook_is_silent(env: Env) -> None:
    out = zsh('_dirvana_enter; _dirvana_preexec "ls" "ls" "ls"; _dirvana_precmd', env)
    assert out == ""
