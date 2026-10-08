"""M1 acceptance scenarios: real interactive zsh, hermetic HOME, no LLM.

Numbers refer to the "Done when" scenarios in the build plan.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from dirvana.paths import OBS_FILE, RECON_FILE
from tests.conftest import Env, Spawn
from tests.scenarios.snapshot import diff, snapshot


def _git(cwd: Path, env: Env, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env.vars, check=True, capture_output=True, text=True
    ).stdout


def _obs(env: Env, real: Path) -> list[dict[str, Any]]:
    path = env.system(real) / OBS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _everything_under(root: Path) -> str:
    """All bytes stored under root, for leak checks."""
    chunks: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            chunks.append(p.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def test_s01_recon_in_git_repo_without_touching_it(env: Env, interactive: Spawn) -> None:
    repo = env.home / "src" / "acme-build-config-2026q3-variant-a"
    repo.mkdir(parents=True)
    _git(repo, env, "init", "-q", "-b", "main")
    _git(repo, env, "remote", "add", "origin", "git@github.com:Acme/Build-Config.git")
    (repo / "README.md").write_text("# Acme build config\nVariant A.\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Run make test.\nAPI_TOKEN=abc123\n", encoding="utf-8")
    (repo / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "init")
    status_before = _git(repo, env, "--no-optional-locks", "status", "--porcelain")
    before = snapshot(repo)

    sh = interactive(env.home)
    sh.run(f"cd {repo}")
    sh.run("ls")
    sh.close()

    assert diff(before, snapshot(repo)) == []
    assert _git(repo, env, "--no-optional-locks", "status", "--porcelain") == status_before

    node = env.system(repo)
    assert node == env.root / "system" / str(repo).lstrip("/")
    recon = json.loads((node / RECON_FILE).read_text(encoding="utf-8"))
    git = recon["git"]
    assert git["toplevel"] == str(repo)
    assert git["branch"] == "main"
    assert git["remotes"] == {"origin": "git@github.com:Acme/Build-Config.git"}
    assert len(git["root"]) == 1 and len(git["root"][0]) == 40
    assert set(recon["files"]) >= {"README.md", "AGENTS.md", "Makefile"}
    assert "Variant A." in recon["files"]["README.md"]["excerpt"]
    assert "API_TOKEN=<redacted>" in recon["files"]["AGENTS.md"]["excerpt"]
    assert "abc123" not in (node / RECON_FILE).read_text(encoding="utf-8")

    shown = json.loads(env.cli("show", "--json", str(repo)))
    assert shown["identity"] == "git-remote:github.com/acme/build-config:"


def test_s02_etc_node_without_writes(env: Env, interactive: Spawn) -> None:
    before = snapshot(Path("/etc"))
    sh = interactive(env.home)
    sh.run("cd /etc")
    sh.run("ls hosts >/dev/null")
    sh.close()
    assert diff(before, snapshot(Path("/etc"))) == []
    node = env.system("/etc")
    assert (node / RECON_FILE).exists()
    assert [o["cmd"] for o in _obs(env, Path("/etc"))] == ["ls hosts >/dev/null"]


def test_s03_edges_with_verbs_and_inbound(env: Env, interactive: Spawn) -> None:
    a = env.home / "src" / "acme-build-config-2026q3-variant-a"
    b = env.home / "src" / "acme-build-config-2026q3-variant-b"
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "f").write_text(d.name, encoding="utf-8")
    (b / "x").write_text("x", encoding="utf-8")

    sh = interactive(a)
    sh.run(f"ls ../{b.name}")
    sh.run(f"cp ../{b.name}/x .")
    sh.run(f"diff ../{b.name}/f f")
    sh.close()

    out = json.loads(env.cli("edges", "--json", "--out", str(a)))["out"]
    assert [e["peer"] for e in out] == [str(b)]
    assert set(out[0]["verbs"]) == {"list", "copy-from", "diff"}
    assert out[0]["examples"] and set(out[0]["examples"]) <= {"x", "f"}
    inbound = json.loads(env.cli("edges", "--json", "--in", str(b)))["in"]
    assert [e["peer"] for e in inbound] == [str(a)]
    assert set(inbound[0]["verbs"]) == {"list", "copy-from", "diff"}


def test_s07_leading_space_and_secret_never_stored(env: Env, interactive: Spawn) -> None:
    work = env.home / "work"
    secret_dir = env.home / "hush-hush-dir"
    for d in (work, secret_dir):
        d.mkdir()
    sh = interactive(work)
    sh.run(" echo leading-space-marker-91f3")
    # A leading-space cd leaves no edge and triggers no recon; leaving the same way keeps
    # the directory out of the record entirely (commands typed normally there would count).
    sh.run(f" cd {secret_dir}")
    sh.run(f" cd {work}")
    sh.run("export GITHUB_TOKEN=ghp_FAKEfakeFAKEfakeFAKEfake0123")
    sh.run("curl -s -H 'Authorization: Bearer sekrit-bearer-77' http://127.0.0.1:9 || true")
    sh.run("echo visible-marker")
    sh.close()
    stored = _everything_under(env.root)
    assert "visible-marker" in stored
    for leak in ("leading-space-marker-91f3", "ghp_FAKEfake", "sekrit-bearer-77", "hush-hush-dir"):
        assert leak not in stored, leak
    assert "GITHUB_TOKEN=<redacted>" in stored
    assert not env.system(secret_dir).exists()


def test_s14_root_removed_mid_session(env: Env, interactive: Spawn) -> None:
    a = env.home / "a"
    b = env.home / "b"
    a.mkdir()
    b.mkdir()
    sh = interactive(a)
    sh.run("echo before")
    assert _obs(env, a)
    shutil.rmtree(env.root)
    sh.run("echo after-reset")
    sh.run(f"ls ../{b.name}")
    sh.close()
    cmds = [o["cmd"] for o in _obs(env, a)]
    assert cmds == ["echo after-reset", f"ls ../{b.name}"]
    assert stat.S_IMODE(os.stat(env.root).st_mode) == 0o700
    edges = json.loads(env.cli("edges", "--json", "--out", str(a)))["out"]
    assert [e["peer"] for e in edges] == [str(b)]


def test_pause_resume_incognito(env: Env, interactive: Spawn) -> None:
    w = env.home / "w"
    w.mkdir()
    sh = interactive(w)
    sh.run("echo one")
    sh.run("dirvana pause")
    sh.run("echo paused-marker")
    sh.run("dirvana resume")
    sh.run("echo two")
    sh.run("dirvana incognito")
    sh.run("echo incognito-marker")
    sh.run("dirvana resume")
    sh.run("dirvana pause --global")
    sh.run("echo global-marker")
    sh.run("dirvana resume --global")
    sh.run("echo three")
    sh.close()
    cmds = [o["cmd"] for o in _obs(env, w)]
    assert "echo one" in cmds and "echo two" in cmds and "echo three" in cmds
    for marker in ("paused-marker", "incognito-marker", "global-marker"):
        assert not any(marker in c for c in cmds), marker


def test_ignored_directory_records_nothing(env: Env, interactive: Spawn) -> None:
    ssh = env.home / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host x\n", encoding="utf-8")
    (env.config).mkdir(parents=True)
    (env.config / "policy").write_text("~/private ignore\n", encoding="utf-8")
    private = env.home / "private"
    private.mkdir()
    sh = interactive(env.home)
    sh.run("cat ~/.ssh/config")
    sh.run("cd ~/.ssh")
    sh.run("ls")
    sh.run("cd ~/private")
    sh.run("ls")
    sh.run("cd ~")
    sh.close()
    assert not env.system(ssh).exists()
    assert not env.system(private).exists()
    home_cmds = [o.get("cmd") for o in _obs(env, env.home) if o["k"] == "cmd"]
    assert home_cmds == ["cat <ignored>", "cd <ignored>"]
    stored = _everything_under(env.root)
    assert ".ssh" not in stored and "/private" not in stored


def test_plugin_load_registers_hooks_once(env: Env, interactive: Spawn) -> None:
    sh = interactive(env.home)
    out = sh.run("print -l $preexec_functions $precmd_functions $chpwd_functions")
    assert out.count("_dirvana_preexec") == 1
    out = sh.run(
        "dirvana_plugin_unload; print ${+functions[_dirvana_preexec]} ${#preexec_functions}"
    )
    assert "0 0" in out
    sh.close()
