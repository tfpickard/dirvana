"""M2 acceptance scenarios: daemon, enrichment, egress and the hotkey, with mock providers that
record every payload (``$DIRVANA_MOCK_CAPTURE``).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from dirvana import daemon as daemonmod
from dirvana.paths import CONTEXT_FILE, CONTEXT_META_FILE, shadow_dir
from tests.conftest import BIN, Env, Spawn

MOCKS = """
[providers]
order = ["anthropic", "openai", "copilot"]
[providers.anthropic]
kind = "mock"
[providers.openai]
kind = "mock"
enabled = false
[providers.copilot]
kind = "mock"
[hotkey]
timeout = 2.0
[ranking]
min_sessions = 1
[enrich]
min_interval = "0s"
min_new_observations = 1
min_observations = 1
"""


def _config(env: Env, extra: str = "") -> None:
    env.config.mkdir(parents=True, exist_ok=True)
    (env.config / "config.toml").write_text(MOCKS + extra, encoding="utf-8")


def _captured(env: Env) -> list[dict[str, Any]]:
    path = Path(env.vars["DIRVANA_MOCK_CAPTURE"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def mocked(env: Env) -> Env:
    env.vars["DIRVANA_MOCK_CAPTURE"] = str(env.tmp / "capture.jsonl")
    os.environ["DIRVANA_MOCK_CAPTURE"] = env.vars["DIRVANA_MOCK_CAPTURE"]
    _config(env)
    return env


def _siblings(env: Env) -> tuple[Path, Path]:
    a = env.home / "src" / "acme-build-config-2026q3-variant-a"
    b = env.home / "src" / "acme-build-config-2026q3-variant-b"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "Makefile").write_text(d.name + "\n", encoding="utf-8")
    return a, b


def _session(interactive: Spawn, cwd: Path, *cmds: str, rc: str = "") -> None:
    sh = interactive(cwd, rc)
    for c in cmds:
        sh.run(c)
    sh.close()


def _suggest(
    env: Env,
    cwd: Path,
    buffer: str = "",
    ring: list[dict[str, str]] | None = None,
    cmd: str = "suggest",
) -> tuple[int, list[list[str]], float]:
    payload = json.dumps({"cwd": str(cwd), "buffer": buffer, "ring": ring or [], "sid": "t:1:1"})
    t0 = time.monotonic()
    proc = subprocess.run(
        [str(BIN / "dirvana"), cmd],
        input=payload,
        env=env.vars,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    elapsed = time.monotonic() - t0
    rows = [line.split("\t") for line in proc.stdout.splitlines() if line]
    return proc.returncode, rows, elapsed


def _oneshot(env: Env) -> dict[str, Any]:
    out = env.cli("daemon", "--oneshot")
    return json.loads(out.strip().splitlines()[-1])


# -- 4: enrich only dirty nodes; a second run makes zero calls


def test_s04_daemon_enriches_dirty_nodes_only(mocked: Env, interactive: Spawn) -> None:
    env = mocked
    a, b = _siblings(env)
    _session(interactive, a, f"ls ../{b.name}", f"diff ../{b.name}/Makefile Makefile")
    _session(interactive, b, "make -n", "ls")
    first = _oneshot(env)
    assert sorted(first["enriched"]) == sorted([str(a), str(b)])
    assert first["calls"] == 2
    calls = len(_captured(env))
    second = _oneshot(env)
    assert second == {**second, "enriched": [], "calls": 0}
    assert len(_captured(env)) == calls
    # New activity in A only: exactly A is re-enriched.
    _session(interactive, a, "make -n")
    third = _oneshot(env)
    assert third["enriched"] == [str(a)] and third["calls"] == 1
    context = (shadow_dir(env.root / "derived", str(a)) / CONTEXT_FILE).read_text(encoding="utf-8")
    assert "## Places you reach from here" in context


# -- 13: derived data is a cache: delete it, regenerate it exactly


def test_s13_derived_regenerated_exactly(mocked: Env, interactive: Spawn) -> None:
    env = mocked
    a, b = _siblings(env)
    _session(interactive, a, f"ls ../{b.name}", f"cp ../{b.name}/Makefile ./M2")
    _oneshot(env)
    derived = env.root / "derived"
    before = {p.relative_to(derived): p.read_bytes() for p in derived.rglob("%context.md")}
    meta_before = {
        p.relative_to(derived): {
            k: v for k, v in json.loads(p.read_text()).items() if k != "created"
        }
        for p in derived.rglob(CONTEXT_META_FILE)
    }
    assert before
    shutil.rmtree(derived)
    report = _oneshot(env)
    assert report["calls"] == len(before)
    after = {p.relative_to(derived): p.read_bytes() for p in derived.rglob("%context.md")}
    meta_after = {
        p.relative_to(derived): {
            k: v for k, v in json.loads(p.read_text()).items() if k != "created"
        }
        for p in derived.rglob(CONTEXT_META_FILE)
    }
    assert after == before
    assert meta_after == meta_before


# -- 5: hotkey uses B's resolved path; inserting executes nothing


def _dump_rc(env: Env, picker: str) -> str:
    return (
        f"zstyle ':dirvana:widget' picker {picker}\n"
        # fzf --height needs a real terminal's cursor-position reply; pexpect is not one.
        "zstyle ':dirvana:widget' fzf-height full\n"
        f'_t_dump() {{ print -r -- "$BUFFER" > {env.tmp}/buffer }}\n'
        "zle -N _t_dump\nbindkey '^Xd' _t_dump\n"
    )


@pytest.mark.parametrize("picker", ["builtin", "fzf"])
def test_s05_hotkey_inserts_edge_path_and_runs_nothing(
    mocked: Env, interactive: Spawn, picker: str
) -> None:
    env = mocked
    a, b = _siblings(env)
    sh = interactive(a, _dump_rc(env, picker))
    for c in (f"ls ../{b.name}", f"diff ../{b.name}/Makefile Makefile"):
        sh.run(c)
    sh.child.send("\x18j")
    if picker == "builtin":
        sh.child.expect("to insert", timeout=15)
        sh.child.send("1")
    else:
        sh.child.expect(r"\d+/\d+", timeout=15)  # fzf finished reading candidates
        time.sleep(0.2)
        sh.child.send("\r")
    sh.child.expect("diff -ru", timeout=15)  # the line editor shows the inserted command
    time.sleep(0.2)
    sh.child.send("\x18d")
    deadline = time.monotonic() + 10
    while not (env.tmp / "buffer").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    buffer = (env.tmp / "buffer").read_text(encoding="utf-8").strip()
    assert buffer == f"diff -ru ../{b.name} ."
    sh.child.send("\x15")  # discard the line: nothing is run
    sh.run("")
    sh.close()
    obs = (env.system(a) / "%obs.jsonl").read_text(encoding="utf-8")
    assert "diff -ru" not in [json.loads(x).get("cmd", "")[:7] for x in obs.splitlines()]
    assert not (a / "MOCK_SHOULD_NEVER_RUN").exists()
    picks = [json.loads(x) for x in obs.splitlines() if '"k":"pick"' in x]
    assert picks and picks[-1]["cmd"] == buffer and picks[-1]["source"] == "llm"
    assert [c["provider"] for c in _captured(env)] == ["anthropic"]


# -- 6: provider down or slow -> local fallback within the timeout


class _Blackhole:
    """Accepts TCP connections and never answers (a hung provider)."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)

    def close(self) -> None:
        # close() alone does not wake a thread blocked in accept() on Linux; shutdown() does.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join(timeout=5)
        for c in self.conns:
            c.close()


@pytest.mark.parametrize("mode", ["slow", "down", "hung-http"])
def test_s06_fallback_within_timeout(mocked: Env, interactive: Spawn, mode: str) -> None:
    env = mocked
    a, b = _siblings(env)
    _session(interactive, a, f"ls ../{b.name}", f"diff ../{b.name}/Makefile Makefile")
    blackhole: _Blackhole | None = None
    if mode == "slow":
        (env.config / "config.toml").write_text(
            MOCKS.replace(
                '[providers.anthropic]\nkind = "mock"',
                '[providers.anthropic]\nkind = "mock"\ndelay = 30',
            )
        )
    elif mode == "down":
        (env.config / "config.toml").write_text(
            MOCKS.replace(
                '[providers.anthropic]\nkind = "mock"',
                '[providers.anthropic]\nkind = "mock"\ndown = true',
            )
        )
    else:
        blackhole = _Blackhole()  # started after the pty sessions: no threads while forking
        (env.config / "config.toml").write_text(
            MOCKS.replace(
                '[providers.anthropic]\nkind = "mock"',
                f'[providers.anthropic]\nkind = "anthropic"\nmodel = "claude-test"\n'
                f'api_key_env = "FAKE_KEY"\nbase_url = "http://127.0.0.1:{blackhole.port}"',
            )
        )
        env.vars["FAKE_KEY"] = "sk-ant-test"
    try:
        code, rows, elapsed = _suggest(env, a)
    finally:
        if blackhole is not None:
            blackhole.close()
    assert code == 3
    assert rows and all(r[2] == "local" for r in rows)
    assert any(b.name in r[0] for r in rows)
    assert elapsed < 2.0 + 1.5, elapsed  # the deadline plus interpreter start-up


# -- 7 (payload half): leading-space commands and secrets never reach a provider


def test_s07_no_secret_or_hidden_command_in_payloads(mocked: Env, interactive: Spawn) -> None:
    env = mocked
    a, b = _siblings(env)
    _session(
        interactive,
        a,
        " echo hidden-marker-5521",
        "export DB_PASSWORD=hunter2hunter2",
        f"diff ../{b.name}/Makefile Makefile",
    )
    _oneshot(env)
    ring = [
        {"cmd": "export DB_PASSWORD=hunter2hunter2", "cwd": str(a)},
        {"cmd": "curl -H 'Authorization: Bearer tok-abc-123' https://x", "cwd": str(a)},
    ]
    _suggest(env, a, ring=ring)
    _suggest(env, a, ring=ring, cmd="brief")
    payloads = json.dumps(_captured(env))
    assert len(_captured(env)) >= 3
    for leak in ("hidden-marker-5521", "hunter2hunter2", "tok-abc-123"):
        assert leak not in payloads, leak


# -- 8: llm=none never leaves; llm=copilot goes only to copilot, even when it is down


def test_s08_egress_pins(mocked: Env, interactive: Spawn) -> None:
    env = mocked
    proj = env.home / "proj"
    secret = env.home / "secret-plans" / "s1"
    work = env.home / "work" / "acme"
    for d in (proj, secret, work):
        d.mkdir(parents=True)
        (d / "notes.txt").write_text("x\n", encoding="utf-8")
    (env.config / "policy").write_text(
        "~/secret-plans llm=none\n~/work llm=copilot\n", encoding="utf-8"
    )
    _session(
        interactive,
        proj,
        "cat ../secret-plans/s1/notes.txt",
        "diff ../secret-plans/s1/notes.txt notes.txt",
        "ls ../work/acme",
        "cat ../work/acme/notes.txt",
    )
    _session(interactive, secret, "cat notes.txt", "ls ../../proj")
    _session(interactive, work, "cat notes.txt", "make -n", "ls ../../proj")
    _oneshot(env)
    ring = [
        {"cmd": "cat notes.txt", "cwd": str(secret)},
        {"cmd": "ls ../secret-plans/s1", "cwd": str(proj)},
        {"cmd": "ls", "cwd": str(proj)},
    ]
    _suggest(env, proj, ring=ring)
    _suggest(env, work, ring=ring)
    cap = _captured(env)
    for rec in cap:
        text = rec["system"] + rec["user"]
        assert "secret-plans" not in text, rec["purpose"]
        assert str(secret) not in text
        if str(work) in text or '"cwd": "' + str(work) in rec["user"]:
            assert rec["provider"] == "copilot"

    def about(p: Path, rec: dict[str, Any]) -> bool:
        return f'"directory": "{p}"' in rec["user"] or f'"cwd": "{p}"' in rec["user"]

    assert not [r for r in cap if about(secret, r)]  # never a subject
    work_recs = [r for r in cap if about(work, r)]
    assert work_recs and {r["provider"] for r in work_recs} == {"copilot"}
    assert {r["provider"] for r in cap if about(proj, r)} == {"anthropic"}
    # Copilot down: the pinned subtree falls back locally, never to another provider.
    (env.config / "config.toml").write_text(
        MOCKS.replace(
            '[providers.copilot]\nkind = "mock"', '[providers.copilot]\nkind = "mock"\ndown = true'
        )
    )
    before = len(_captured(env))
    code, rows, _ = _suggest(env, work)
    assert code == 3 and rows and all(r[2] == "local" for r in rows)
    assert len(_captured(env)) == before
    explain = env.cli("policy", "explain", str(work))
    assert "llm        copilot" in explain


# -- 14 (daemon half): the daemon survives rm -rf <root> and serves hotkeys


def test_s14_daemon_survives_root_removal(mocked: Env, interactive: Spawn) -> None:
    env = mocked
    a, b = _siblings(env)
    (env.config / "config.toml").write_text(MOCKS + "\n[daemon]\ninterval = 1\n")
    _session(interactive, a, f"ls ../{b.name}", f"diff ../{b.name}/Makefile Makefile")
    proc = subprocess.Popen(
        [str(BIN / "dirvana"), "daemon"],
        env=env.vars,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sock = str(env.root / "run" / "daemon.sock")
    try:
        _wait(lambda: daemonmod.request(sock, {"op": "ping"}, 1) is not None)
        code, rows, _ = _suggest(env, a)
        assert code == 0 and rows[0][2] == "llm"
        before = len(_captured(env))
        shutil.rmtree(env.root)
        _wait(lambda: daemonmod.request(sock, {"op": "ping"}, 1) is not None, timeout=15)
        _session(interactive, a, f"ls ../{b.name}")
        code, rows, _ = _suggest(env, a)
        assert code == 0 and rows[0][2] == "llm"
        assert len(_captured(env)) > before
        usage = (env.root / "var" / "usage.jsonl").read_text(encoding="utf-8")
        assert '"purpose": "suggest"' in usage
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert not Path(sock).exists()


def _wait(cond: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")
