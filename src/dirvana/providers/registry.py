"""Construct provider adapters from config.

Only :mod:`dirvana.egress` (and the daemon, which re-checks egress) should call this; every
request must pass the egress guard before it reaches an adapter.
"""

from __future__ import annotations

from typing import Any

from dirvana.config import ProviderConfig
from dirvana.paths import Dirs
from dirvana.providers.base import Provider, ProviderError


def build(cfg: ProviderConfig, dirs: Dirs, **overrides: Any) -> Provider:
    if cfg.kind == "mock":
        from dirvana.providers.mock import MockProvider

        return MockProvider(cfg)
    if cfg.kind == "anthropic":
        from dirvana.providers.anthropic import AnthropicProvider

        return AnthropicProvider(cfg, **overrides)
    if cfg.kind == "openai":
        from dirvana.providers.openai import OpenAIProvider

        return OpenAIProvider(cfg, **overrides)
    if cfg.kind == "copilot":
        from dirvana.providers.copilot import CopilotProvider

        return CopilotProvider(cfg, base_directory=dirs.var / "copilot", **overrides)
    raise ProviderError(f"unknown provider kind {cfg.kind!r}")
