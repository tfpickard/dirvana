"""Lock-free rewriting of a node's observation journal.

The hook appends to ``%obs.jsonl`` without locks, so the journal is never rewritten in place.
Instead it is renamed aside (atomic), left alone for a short grace period so any append that
opened the old inode has landed, transformed, and appended back to a (possibly new)
``%obs.jsonl``. Compaction (M3) and ``forget`` both use this.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dirvana._meta import env
from dirvana.paths import OBS_FILE
from dirvana.store.io import decode

Transform = Callable[[dict[str, Any]], dict[str, Any] | None]


def grace_seconds() -> float:
    value = os.environ.get(env("GRACE"), "")
    try:
        return float(value) if value else 2.0
    except ValueError:
        return 2.0


def rewrite_observations(node_dir: Path, transform: Transform) -> int:
    """Apply ``transform`` to every record (``None`` drops it); return how many changed."""
    obs = node_dir / OBS_FILE
    aside = node_dir / f"%obs.rewrite.{os.getpid()}"
    try:
        obs.rename(aside)
    except FileNotFoundError:
        return 0
    time.sleep(grace_seconds())
    data = aside.read_bytes()
    out: list[bytes] = []
    changed = 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            orig = json.loads(decode(raw))
        except ValueError:
            out.append(raw)
            continue
        if not isinstance(orig, dict):
            out.append(raw)
            continue
        new = transform(json.loads(decode(raw)))
        if new is None:
            changed += 1
        elif new == orig:
            out.append(raw)
        else:
            changed += 1
            text = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
            out.append(text.encode("utf-8", errors="surrogateescape"))
    if out:
        fd = os.open(obs, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"\n".join(out) + b"\n")
        finally:
            os.close(fd)
    aside.unlink()
    return changed
