"""Shared fixtures: hermetic environments and zsh drivers.

Every test gets its own HOME, root, config and state directories under ``tmp_path``. Nothing
touches the real home directory or the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "dirvana.plugin.zsh"
ADAPTER = REPO / "shell" / "zsh" / "dirvana.zsh"
VECTORS = REPO / "tests" / "vectors"
BIN = Path(sys.executable).parent


@dataclass
class Env:
    """A hermetic dirvana environment."""

    tmp: Path
    home: Path
    root: Path
    config: Path
    state: Path
    vars: dict[str, str]

    def system(self, real: str | Path) -> Path:
        """Shadow node directory for a real absolute path."""
        from dirvana.paths import shadow_dir

        return shadow_dir(self.root / "system", os.path.realpath(real))

    def cli(self, *args: str, check: bool = True, input: str | None = None) -> str:
        proc = subprocess.run(
            [str(BIN / "dirvana"), *args],
            env=self.vars,
            capture_output=True,
            text=True,
            input=input,
            check=False,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"dirvana {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout


def make_env(tmp: Path) -> Env:
    tmp = Path(os.path.realpath(tmp))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = Env(tmp, home, tmp / "root", tmp / "cfg", tmp / "state", {})
    env.vars = {
        "HOME": str(home),
        "PATH": f"{BIN}{os.pathsep}/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "USER": os.environ.get("USER", "tester"),
        "ZDOTDIR": str(tmp / "zdot"),
        "DIRVANA_ROOT": str(env.root),
        "DIRVANA_CONFIG_DIR": str(env.config),
        "DIRVANA_STATE_DIR": str(env.state),
        "DIRVANA_RECON_SYNC": "1",
        "DIRVANA_GRACE": "0",
        "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return env


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    e = make_env(tmp_path)
    for k, v in e.vars.items():
        monkeypatch.setenv(k, v)
    return e


def zsh(
    script: str,
    env: Env,
    *,
    cwd: Path | None = None,
    input: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> str:
    """Run ``script`` in a non-interactive ``zsh -f`` with the adapter sourced."""
    proc = subprocess.run(
        # Internal helpers expect the options the hooks set (emulate zsh + extended_glob).
        ["zsh", "-f", "-c", f"source {ADAPTER}\nemulate zsh\nsetopt extended_glob\n{script}"],
        env={**env.vars, **(extra or {})},
        cwd=cwd or env.home,
        capture_output=True,
        input=input,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"zsh failed ({proc.returncode}): {proc.stderr}\n{proc.stdout}")
    return proc.stdout


class Interactive:
    """A real interactive zsh in a pty with the plugin loaded, as a user would have it."""

    PROMPT = "DVPROMPT> "

    def __init__(self, env: Env, cwd: Path, rc_extra: str = "") -> None:
        import pexpect

        zdot = Path(env.vars["ZDOTDIR"])
        zdot.mkdir(parents=True, exist_ok=True)
        (zdot / ".zshrc").write_text(
            f"PS1='{self.PROMPT}'\nRPS1=\nHISTFILE={env.tmp}/histfile\n"
            f"source {PLUGIN}\n{rc_extra}\n",
            encoding="utf-8",
        )
        self.child = pexpect.spawn(
            "zsh",
            ["-d", "-i"],
            env=env.vars,
            cwd=str(cwd),
            encoding="utf-8",
            timeout=20,
            dimensions=(40, 200),
        )
        self.child.expect_exact(self.PROMPT)

    def run(self, line: str) -> str:
        self.child.sendline(line)
        self.child.expect_exact(self.PROMPT)
        return cast(str, self.child.before)

    def close(self) -> None:
        self.child.sendline("exit")
        self.child.expect(__import__("pexpect").EOF)
        self.child.close()


class Spawn(Protocol):
    def __call__(self, cwd: Path, rc_extra: str = "") -> Interactive: ...


@pytest.fixture
def interactive(env: Env) -> Iterator[Spawn]:
    """Factory for interactive shells bound to the test's environment; all are reaped."""
    started: list[Interactive] = []

    def spawn(cwd: Path, rc_extra: str = "") -> Interactive:
        sh = Interactive(env, cwd, rc_extra)
        started.append(sh)
        return sh

    yield spawn
    for sh in started:
        if sh.child.isalive():
            sh.child.terminate(force=True)
