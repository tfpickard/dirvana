"""Low-level file IO for the shadow tree: tolerant JSONL reading, atomic JSON writes, locks."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, cast


def as_dict(value: object) -> dict[str, Any]:
    """``value`` if it is a JSON object, else an empty dict (parsed JSON is untrusted)."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        # mypy narrows to list[Any] (a cast would be "redundant"); pyright to list[Unknown].
        return value  # pyright: ignore[reportUnknownVariableType]
    return []


def decode(data: bytes) -> str:
    """Observations may carry arbitrary bytes from the shell; never fail on them."""
    return data.decode("utf-8", errors="surrogateescape")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from ``path``.

    A trailing line without a newline is a write still in flight (or a torn write) and is
    skipped; malformed lines are skipped too. A missing file yields nothing.
    """
    try:
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return
    lines = data.split(b"\n")
    for raw in lines[:-1]:
        if not raw.strip():
            continue
        try:
            obj = json.loads(decode(raw))
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield cast(dict[str, Any], obj)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(decode(path.read_bytes()))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    return cast(dict[str, Any], obj) if isinstance(obj, dict) else None


def dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_text_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def write_json_atomic(path: Path, obj: object) -> bool:
    """Write ``obj`` as pretty JSON; return False (and skip the write) if unchanged."""
    text = dumps(obj)
    try:
        if decode(path.read_bytes()) == text:
            return False
    except (FileNotFoundError, NotADirectoryError):
        pass
    write_text_atomic(path, text)
    return True


@contextlib.contextmanager
def flock(path: Path, *, shared: bool = False) -> Generator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
