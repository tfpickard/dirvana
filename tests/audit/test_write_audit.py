"""Linux-only syscall audit (``make test-audit``): every write the hook makes lands in dirvana's
own directories. Complements the stat snapshots in the scenarios, which cannot see a write
that is undone, and the fact that this container runs tests as root.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.conftest import PLUGIN, Env

_WRITE_OPEN = re.compile(r'open(?:at)?\((?:AT_FDCWD, )?"([^"]+)", ([A-Z_|]+)')
_PATH_CALL = re.compile(
    r"(?:mkdir|mkdirat|rename|renameat2?|unlink|unlinkat|rmdir|creat|truncate|link|symlink)"
    r'\((?:AT_FDCWD|\d+)?,? ?"([^"]+)"'
)


def test_hook_writes_only_to_its_own_dirs(env: Env, tmp_path: Path) -> None:
    assert shutil.which("strace"), "make test-audit needs strace (Linux): paru -S strace"
    repo = env.home / "repo"
    other = env.home / "other"
    for d in (repo, other):
        d.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=env.vars, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    script = tmp_path / "session.zsh"
    script.write_text(
        f"source {PLUGIN}\n"
        "_dirvana_enter\n"
        f"cd {repo}; _dirvana_chpwd\n"
        f'c="ls ../other"; _dirvana_preexec "$c" "$c" "$c"; _dirvana_precmd\n'
        f"cd {other}; _dirvana_chpwd\n"
        f'c="cat ../repo/README.md"; _dirvana_preexec "$c" "$c" "$c"; _dirvana_precmd\n',
        encoding="utf-8",
    )
    log = tmp_path / "strace.log"
    subprocess.run(
        ["strace", "-f", "-qq", "-o", str(log), "-e", "trace=%file", "zsh", "-f", str(script)],
        env=env.vars,
        cwd=env.home,
        check=True,
        capture_output=True,
    )
    allowed = (str(env.root), str(env.state), "/dev/")
    offenders: list[str] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "= -1" in line:
            continue  # failed calls changed nothing
        m = _WRITE_OPEN.search(line)
        if m and re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND", m.group(2)):
            if not m.group(1).startswith(allowed):
                offenders.append(line)
            continue
        m = _PATH_CALL.search(line)
        if m and not m.group(1).startswith(allowed):
            offenders.append(line)
    assert offenders == []
    assert (env.system(other) / "%obs.jsonl").exists()
