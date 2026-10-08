"""Per-subtree policy: parsing, matching, resolution and explanation.

Policy files live in the config directory, never in the data root, so they survive
``rm -rf <root>`` and can be kept in dotfiles:

* ``<config>/policy``: the global file. Its base is ``/``; patterns may start with ``~/``.
* ``<config>/policy.d/*.policy``: per-subtree files. The first non-comment line must be
  ``@root DIR``; patterns are relative to DIR.

Each rule line is ``PATTERN key[=value] ...``. Pattern semantics are shared with the zsh hook
(``_dirvana_glob2pat``) and pinned by ``tests/vectors/ignore.json``. A rule that matches a
directory applies to everything below it as well.

Resolution is field by field: built-ins, then the global file, then policy.d files from the
shallowest ``@root`` to the deepest (ties by file name, byte order); within a file, later lines
win. The last setter of a field wins overall. Layers are consulted by :mod:`dirvana.overlay`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from dirvana.paths import Dirs, expand_home

FIELDS: Final = ("ignore", "retention", "ttl", "enrich", "llm")
RETENTIONS: Final = ("eternal", "ephemeral")
ENRICH_MODES: Final = ("async", "sync", "off")

Retention = Literal["eternal", "ephemeral"]
EnrichMode = Literal["async", "sync", "off"]


class PolicyError(ValueError):
    """A malformed policy file or rule."""


@dataclass(frozen=True, slots=True)
class Source:
    """Where a rule came from, for ``policy explain``."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True, slots=True)
class Rule:
    pattern: str
    base: str
    regex: re.Pattern[str]
    settings: dict[str, str]
    source: Source

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None


@dataclass(frozen=True, slots=True)
class Setting:
    value: str
    source: Source


@dataclass(slots=True)
class Effective:
    """The resolved policy for one directory, with provenance per field."""

    path: str
    settings: dict[str, Setting] = field(default_factory=dict[str, Setting])

    def get(self, key: str) -> str | None:
        s = self.settings.get(key)
        return s.value if s else None

    @property
    def ignored(self) -> bool:
        return self.get("ignore") == "true"

    @property
    def retention(self) -> Retention:
        return "ephemeral" if self.get("retention") == "ephemeral" else "eternal"

    @property
    def ttl_seconds(self) -> int | None:
        value = self.get("ttl")
        return parse_duration(value) if value else None

    @property
    def enrich(self) -> EnrichMode:
        value = self.get("enrich")
        if value == "sync":
            return "sync"
        if value == "off":
            return "off"
        return "async"

    @property
    def llm(self) -> tuple[str, ...] | None:
        """Allowed provider instances; ``()`` means none; ``None`` means unrestricted."""
        value = self.get("llm")
        if value is None:
            return None
        if value == "none":
            return ()
        return tuple(v for v in value.split(",") if v)


_DURATION = re.compile(r"(\d+)([smhdw])")
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """Parse ``90s``, ``15m``, ``6h``, ``7d``, ``2w`` or combinations like ``1d12h``."""
    pos = 0
    total = 0
    for m in _DURATION.finditer(text):
        if m.start() != pos:
            break
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or not text:
        raise PolicyError(f"bad duration: {text!r}")
    return total


def _glob_component(comp: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(comp):
        c = comp[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = comp.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                cls = comp[i + 1 : end]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls.replace("\\", "\\\\") + "]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def compile_glob(base: str, pattern: str, home_dir: str | None = None) -> re.Pattern[str]:
    """Compile a policy pattern relative to ``base`` into a regex over absolute paths.

    The result matches the named directory and everything below it.
    """
    pat = pattern.rstrip("/") if pattern != "/" else ""
    if pat.endswith("/**"):
        pat = pat[: -len("/**")]
    if pat == "**":
        pat = ""
    if pat == "~" or pat.startswith("~/"):
        base = expand_home("~", home_dir)
        pat = pat[1:].lstrip("/")
    elif pat.startswith("/"):
        pat = pat[1:]
    elif pat and "/" not in pat:
        pat = "**/" + pat
    out = re.escape(base.rstrip("/"))
    for comp in pat.split("/"):
        if not comp:
            continue
        if comp == "**":
            out += "(?:/.*)?"
        else:
            out += "/" + _glob_component(comp)
    return re.compile(out + "(?:/.*)?")


def _parse_settings(tokens: Sequence[str], source: Source) -> dict[str, str]:
    settings: dict[str, str] = {}
    for tok in tokens:
        key, sep, value = tok.partition("=")
        if key not in FIELDS:
            raise PolicyError(f"{source}: unknown key {key!r}")
        if key == "ignore":
            value = value or "true"
            if value not in ("true", "false"):
                raise PolicyError(f"{source}: ignore must be true or false")
        elif not sep or not value:
            raise PolicyError(f"{source}: {key} needs a value")
        elif key == "retention" and value not in RETENTIONS:
            raise PolicyError(f"{source}: retention must be one of {', '.join(RETENTIONS)}")
        elif key == "enrich" and value not in ENRICH_MODES:
            raise PolicyError(f"{source}: enrich must be one of {', '.join(ENRICH_MODES)}")
        elif key == "ttl":
            parse_duration(value)
        elif (
            key == "llm"
            and value != "none"
            and not re.fullmatch(r"[A-Za-z0-9_-]+(,[A-Za-z0-9_-]+)*", value)
        ):
            raise PolicyError(f"{source}: llm must be none or a comma-separated list of providers")
        settings[key] = value
    if not settings:
        raise PolicyError(f"{source}: rule has no settings")
    return settings


def parse_policy(
    text: str, label: str, base: str | None, home_dir: str | None = None
) -> list[Rule]:
    """Parse one policy file. ``base`` is ``/`` for the global file and ``None`` for policy.d
    files, which must declare ``@root``."""
    rules: list[Rule] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        src = Source(label, lineno)
        if words[0] == "@root":
            if len(words) != 2:
                raise PolicyError(f"{src}: @root takes exactly one directory")
            base = expand_home(words[1], home_dir).rstrip("/") or "/"
            continue
        if base is None:
            raise PolicyError(f"{src}: rules before @root")
        settings = _parse_settings(words[1:], src)
        rules.append(Rule(words[0], base, compile_glob(base, words[0], home_dir), settings, src))
    return rules


def _root_of(text: str, home_dir: str | None) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if words[0] == "@root" and len(words) == 2:
            return expand_home(words[1], home_dir).rstrip("/") or "/"
        return None
    return None


def builtin_rules(dirs: Dirs, home_dir: str | None = None) -> list[Rule]:
    """The non-negotiable floor plus defaults. Mirrors ``_dirvana_policy_load``."""
    h = expand_home("~", home_dir)
    ignored = [
        f"{h}/.ssh",
        f"{h}/.gnupg",
        f"{h}/.password-store",
        os.environ.get("PASSWORD_STORE_DIR", ""),
        f"{h}/.local/share/gopass",
        f"{h}/.aws",
        f"{h}/.config/gcloud",
        f"{h}/.kube",
        f"{h}/.docker",
        str(dirs.root),
        str(dirs.config),
        str(dirs.state),
    ]
    rules: list[Rule] = []
    for n, p in enumerate(ignored, start=1):
        if not p:
            continue
        p = p.rstrip("/")
        rules.append(
            Rule(
                p,
                "/",
                re.compile(re.escape(p) + "(?:/.*)?"),
                {"ignore": "true"},
                Source("<builtin>", n),
            )
        )
    for n, tmp in enumerate(("/tmp", "/private/tmp", "/var/tmp", "/private/var/folders"), 100):
        rules.append(
            Rule(
                tmp,
                "/",
                compile_glob("/", tmp),
                {"retention": "ephemeral", "ttl": "7d"},
                Source("<builtin>", n),
            )
        )
    return rules


def load_rules(dirs: Dirs, home_dir: str | None = None) -> list[Rule]:
    """All local rules in precedence order (lowest first)."""
    return builtin_rules(dirs, home_dir) + load_config_rules(dirs.config, home_dir)


def load_config_rules(config: Path, home_dir: str | None = None) -> list[Rule]:
    rules: list[Rule] = []
    glob = config / "policy"
    if glob.is_file():
        rules += parse_policy(glob.read_text(encoding="utf-8"), str(glob), "/", home_dir)
    keyed: list[tuple[int, bytes, Path, str]] = []
    pdir = config / "policy.d"
    if pdir.is_dir():
        for f in pdir.glob("*.policy"):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            root = _root_of(text, home_dir)
            if root is None:
                raise PolicyError(f"{f}:1: a policy.d file must start with @root DIR")
            depth = len([c for c in root.split("/") if c])
            keyed.append((depth, os.fsencode(str(f)), f, text))
    for _, _, f, text in sorted(keyed, key=lambda k: (k[0], k[1])):
        rules += parse_policy(text, str(f), None, home_dir)
    return rules


def resolve(path: str, rules: Iterable[Rule]) -> Effective:
    """Resolve the effective policy for ``path`` from rules in precedence order."""
    eff = Effective(path)
    for rule in rules:
        if rule.matches(path):
            for key, value in rule.settings.items():
                eff.settings[key] = Setting(value, rule.source)
    return eff


def is_ignored(path: str, rules: Iterable[Rule]) -> bool:
    ignored = False
    for rule in rules:
        value = rule.settings.get("ignore")
        if value is not None and rule.matches(path):
            ignored = value == "true"
    return ignored


def explain(eff: Effective, configured: Sequence[str] | None = None) -> list[str]:
    """Human-readable lines: each field, its value and where it came from."""
    lines = [f"policy for {eff.path}"]
    defaults = {"ignore": "false", "retention": "eternal", "enrich": "async", "llm": "(any)"}
    for key in FIELDS:
        s = eff.settings.get(key)
        if s:
            lines.append(f"  {key:<10} {s.value:<24} {s.source}")
        elif key in defaults:
            lines.append(f"  {key:<10} {defaults[key]:<24} <default>")
    if configured is not None:
        allowed = eff.llm
        usable = list(configured) if allowed is None else [p for p in configured if p in allowed]
        if eff.ignored:
            usable = []
        lines.append(f"  providers  {', '.join(usable) if usable else '(none: stays local)'}")
    return lines
