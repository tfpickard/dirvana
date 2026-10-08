"""Single source of truth for the program's name and derived identifiers.

Renaming the project means changing ``NAME`` here and ``_DIRVANA_NAME`` in the zsh plugin
(a test asserts they agree). Every directory, environment variable, socket and unit name is
derived from it.
"""

from __future__ import annotations

from typing import Final

NAME: Final = "dirvana"
VERSION: Final = "0.1.0.dev0"
FORMAT_VERSION: Final = 1
ENV_PREFIX: Final = NAME.upper()


def env(suffix: str) -> str:
    """Return the environment variable name for ``suffix`` (``env("ROOT") -> "DIRVANA_ROOT"``)."""
    return f"{ENV_PREFIX}_{suffix}"
