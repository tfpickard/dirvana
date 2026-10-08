"""Stat-only filesystem snapshots, used to prove dirvana never writes in observed directories.

ctime changes on any write, chmod, link or rename, so an unchanged (inode, size, mtime, ctime,
mode) tuple for every entry means nothing was modified, created or removed.
"""

from __future__ import annotations

import os
from pathlib import Path

Entry = tuple[int, int, int, int, int]


def snapshot(root: Path, *, max_entries: int = 50_000) -> dict[str, Entry]:
    out: dict[str, Entry] = {}

    def add(path: str) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return
        out[path] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)

    add(str(root))
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in dirnames + filenames:
            add(os.path.join(dirpath, name))
            if len(out) >= max_entries:
                return out
    return out


def diff(before: dict[str, Entry], after: dict[str, Entry]) -> list[str]:
    changes = [f"removed {p}" for p in before.keys() - after.keys()]
    changes += [f"created {p}" for p in after.keys() - before.keys()]
    changes += [f"modified {p}" for p in before.keys() & after.keys() if before[p] != after[p]]
    return sorted(changes)
