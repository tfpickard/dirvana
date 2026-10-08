"""Configuration: packaged defaults merged with ``<config>/config.toml``.

Model names and other provider defaults live only in ``data/default-config.toml``, never in
code. A provider instance is *configured* when it is enabled, has a model (Copilot picks one
at runtime) and has credentials; only configured instances are ever used.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from dirvana.paths import Dirs
from dirvana.policy import parse_duration
from dirvana.rank import RankConfig
from dirvana.store.io import as_dict, as_list

Kind = Literal["anthropic", "openai", "copilot", "mock"]
KINDS: tuple[Kind, ...] = ("anthropic", "openai", "copilot", "mock")


class ConfigError(ValueError):
    pass


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        sub_base, sub_over = as_dict(out.get(k)), as_dict(v)
        out[k] = _merge(sub_base, sub_over) if sub_base and sub_over else v
    return out


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    kind: Kind
    raw: dict[str, Any]

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True))

    @property
    def model(self) -> str:
        return str(self.raw.get("model") or "")

    @property
    def models(self) -> list[str]:
        return [str(m) for m in as_list(self.raw.get("models"))]

    @property
    def base_url(self) -> str | None:
        value = self.raw.get("base_url")
        return str(value) if value else None

    def get_int(self, key: str, default: int) -> int:
        value = self.raw.get(key, default)
        return int(value) if isinstance(value, (int, float)) else default

    def get_float(self, key: str, default: float) -> float:
        value = self.raw.get(key, default)
        return float(value) if isinstance(value, (int, float)) else default

    def api_key(self) -> str | None:
        """Resolve the API key from ``api_key_env`` or ``api_key_cmd`` (never stored)."""
        env_name = self.raw.get("api_key_env")
        if isinstance(env_name, str) and os.environ.get(env_name):
            return os.environ[env_name]
        cmd = self.raw.get("api_key_cmd")
        if isinstance(cmd, str) and cmd:
            try:
                out = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=10, check=True
                )
            except (subprocess.SubprocessError, OSError):
                return None
            return out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
        return None

    def problem(self) -> str | None:
        """Why this instance cannot be used, or ``None`` if it can."""
        if not self.enabled:
            return "disabled"
        if self.kind == "mock":
            return None
        if self.kind in ("anthropic", "openai") and not self.model:
            return "no model configured"
        if self.kind == "openai" and self.base_url:
            return None  # local OpenAI-compatible servers usually need no key
        if self.kind in ("anthropic", "openai") and not self.api_key():
            env_name = self.raw.get("api_key_env") or "api_key_env/api_key_cmd"
            return f"no API key ({env_name} unset)"
        return None


@dataclass(frozen=True, slots=True)
class Config:
    raw: dict[str, Any]
    providers: dict[str, ProviderConfig] = field(default_factory=dict[str, ProviderConfig])
    order: tuple[str, ...] = ()

    def section(self, name: str) -> dict[str, Any]:
        return as_dict(self.raw.get(name))

    def num(self, section: str, key: str, default: float) -> float:
        value = self.section(section).get(key, default)
        return float(value) if isinstance(value, (int, float)) else default

    def duration(self, section: str, key: str, default: str) -> int:
        value = self.section(section).get(key, default)
        if isinstance(value, (int, float)):
            return int(value)
        return parse_duration(str(value))

    def configured(self) -> list[str]:
        """Usable instances in preference order (copilot is checked lazily by its adapter)."""
        names = list(self.order) + sorted(n for n in self.providers if n not in self.order)
        return [n for n in names if n in self.providers and self.providers[n].problem() is None]

    def ranking(self) -> RankConfig:
        r = self.section("ranking")
        return RankConfig(
            half_life_days=float(r.get("half_life_days", 30)),
            min_evidence=int(r.get("min_evidence", 2)),
            min_sessions=int(r.get("min_sessions", 2)),
        )


def load(dirs: Dirs) -> Config:
    default = tomllib.loads(
        resources.files("dirvana").joinpath("data/default-config.toml").read_text("utf-8")
    )
    raw = default
    user = dirs.config / "config.toml"
    if user.is_file():
        try:
            raw = _merge(default, tomllib.loads(user.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{user}: {e}") from e
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> Config:
    providers: dict[str, ProviderConfig] = {}
    psec = as_dict(raw.get("providers"))
    order = tuple(str(x) for x in as_list(psec.get("order")))
    for name, body_raw in psec.items():
        body = as_dict(body_raw)
        if not body:
            continue
        kind = body.get("kind", name)
        if kind not in KINDS:
            raise ConfigError(f"providers.{name}: unknown kind {kind!r}")
        providers[name] = ProviderConfig(name, kind, body)
    return Config(raw=raw, providers=providers, order=order)


def default_path(dirs: Dirs) -> Path:
    return dirs.config / "config.toml"
