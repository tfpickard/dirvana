"""Secret redaction.

This is one of two implementations of the same rules; the other is ``_dirvana_redact`` in the
zsh plugin, which applies them *before* anything is stored. Python re-applies them at egress as
defense in depth. Both are checked against ``tests/vectors/redact.json``; change the rules in
both places and add vectors.
"""

from __future__ import annotations

import re
from typing import Final

MASK: Final = "<redacted>"

_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"
_SECRET_WORD = r"(?:key|secret|token|passw(?:or)?d)"

# (pattern, replacement) in application order. Replacements use \g<n> backrefs.
_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "<redacted:private-key>",
    ),
    (
        re.compile(
            r"(?i)(authorization[ \t]*:[ \t]*)((?:bearer|basic|token|digest)[ \t]+|)[^\s\"']+"
        ),
        rf"\g<1>\g<2>{MASK}",
    ),
    (re.compile(r"(?i)(bearer[ \t]+)[A-Za-z0-9._~+/=-]+"), rf"\g<1>{MASK}"),
    (
        re.compile(rf"(?i)(--[A-Za-z0-9-]*{_SECRET_WORD}[A-Za-z0-9-]*)(=|[ \t]+){_VALUE}"),
        rf"\g<1>\g<2>{MASK}",
    ),
    (re.compile(rf"(?i)([A-Za-z0-9_]*{_SECRET_WORD}[A-Za-z0-9_]*=){_VALUE}"), rf"\g<1>{MASK}"),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), rf"\g<1>{MASK}@"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"AKIA[A-Z0-9]{16}"), MASK),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}"), MASK),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), MASK),
)

# Cheap prefilter: if none of these appear, no rule can match.
_TRIGGER: Final = re.compile(
    r"(?i)auth|bearer|key|secret|token|passw|://|gh[pousr]_|github_pat|sk-|xox|akia|aiza"
    r"|glpat|eyj|-----begin"
)


def redact(text: str) -> str:
    """Return ``text`` with secrets replaced by ``<redacted>`` markers."""
    if not _TRIGGER.search(text):
        return text
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def contains_secret(text: str) -> bool:
    """True if :func:`redact` would change ``text`` (used for post-hoc audits)."""
    return redact(text) != text
