"""Directory layout and the real-path <-> shadow-path mapping.

The shadow tree mirrors absolute paths under ``<root>/system``. Node files live inside the
mirrored directory and start with a single ``%``. A real directory entry whose name starts
with ``%`` is stored with the ``%`` doubled, so a single leading ``%`` always means metadata
and the mapping is a bijection. ``docs/hook-protocol.md`` is the normative description; the
zsh implementation must agree (see ``tests/vectors/shadow.json``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dirvana._meta import NAME, env

META_PREFIX: Final = "%"
OBS_FILE: Final = "%obs.jsonl"
RECON_FILE: Final = "%recon.json"
NODE_FILE: Final = "%node.json"
EDGES_OUT_FILE: Final = "%edges.out.json"
EDGES_IN_FILE: Final = "%edges.in.json"
ROLLUP_FILE: Final = "%rollup.json"
CONTEXT_FILE: Final = "%context.md"
CONTEXT_META_FILE: Final = "%context.meta.json"

SYSTEM_DIR: Final = "system"
DERIVED_DIR: Final = "derived"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative values are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def home() -> Path:
    """The home directory paths are portable against.

    Inside the Docker container the daemon must use the *host's* home, which compose passes
    as ``DIRVANA_HOME``; natively this is just ``$HOME``.
    """
    value = os.environ.get(env("HOME"), "")
    return Path(value) if value else Path.home()


@dataclass(frozen=True, slots=True)
class Dirs:
    """Resolved locations for one invocation."""

    root: Path
    config: Path
    state: Path

    @classmethod
    def from_env(cls) -> Dirs:
        root = os.environ.get(env("ROOT"), "")
        config = os.environ.get(env("CONFIG_DIR"), "")
        state = os.environ.get(env("STATE_DIR"), "")
        return cls(
            root=Path(root) if root else _xdg("XDG_DATA_HOME", ".local/share") / NAME,
            config=Path(config) if config else _xdg("XDG_CONFIG_HOME", ".config") / NAME,
            state=Path(state) if state else _xdg("XDG_STATE_HOME", ".local/state") / NAME,
        )

    @property
    def system(self) -> Path:
        return self.root / SYSTEM_DIR

    @property
    def derived(self) -> Path:
        return self.root / DERIVED_DIR

    @property
    def var(self) -> Path:
        return self.root / "var"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def layers(self) -> Path:
        return self.root / "layers.d"

    def ensure_root(self) -> None:
        """Create the root skeleton with private permissions (idempotent)."""
        self.root.parent.mkdir(parents=True, exist_ok=True)
        for d in (self.root, self.system, self.derived, self.var, self.run):
            d.mkdir(mode=0o700, exist_ok=True)
        fmt = self.root / "FORMAT"
        if not fmt.exists():
            fmt.write_text("1\n", encoding="ascii")


def escape_component(name: str) -> str:
    return META_PREFIX + name if name.startswith(META_PREFIX) else name


def unescape_component(name: str) -> str | None:
    """Return the real name for a shadow entry, or ``None`` if the entry is node metadata."""
    if name.startswith(META_PREFIX + META_PREFIX):
        return name[1:]
    if name.startswith(META_PREFIX):
        return None
    return name


def shadow_rel(real: str) -> str:
    """Map an absolute real path to its path relative to the shadow tree base (``""`` for /)."""
    if not real.startswith("/"):
        raise ValueError(f"not an absolute path: {real!r}")
    parts = [p for p in real.split("/") if p]
    return "/".join(escape_component(p) for p in parts)


def shadow_dir(base: Path, real: str) -> Path:
    """The shadow directory for ``real`` under ``base`` (``<root>/system`` or ``/derived``)."""
    rel = shadow_rel(real)
    return base / rel if rel else base


def real_from_shadow(base: Path, shadow: Path) -> str | None:
    """Inverse of :func:`shadow_dir`; ``None`` if ``shadow`` is not a node directory under base."""
    try:
        rel = shadow.relative_to(base)
    except ValueError:
        return None
    parts: list[str] = []
    for comp in rel.parts:
        name = unescape_component(comp)
        if name is None:
            return None
        parts.append(name)
    return "/" + "/".join(parts)


def portable(path: str, home_dir: str | None = None) -> str:
    """Replace the home prefix with ``~`` so paths compare across machines."""
    h = (home_dir or str(home())).rstrip("/")
    if path == h:
        return "~"
    if h and path.startswith(h + "/"):
        return "~" + path[len(h) :]
    return path


def expand_home(path: str, home_dir: str | None = None) -> str:
    h = (home_dir or str(home())).rstrip("/")
    if path == "~":
        return h
    if path.startswith("~/"):
        return h + path[1:]
    return path
