"""The one interface every LLM provider adapter implements.

Text in, text out. There is deliberately no way to pass tools, files or anything else: a
provider cannot be given capabilities through this interface. Adapters are never handed out
directly; callers go through :class:`dirvana.egress.EgressGuard`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

Purpose = Literal["enrich", "suggest", "brief", "smoke"]


class ProviderError(Exception):
    """The provider failed (down, timed out, refused, bad output). Callers fall back."""


class ProviderTimeout(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    system: str
    user: str
    max_output_tokens: int
    timeout_s: float
    purpose: Purpose


@dataclass(frozen=True, slots=True)
class Usage:
    """Usage in the provider's native unit: tokens, or Copilot premium requests."""

    unit: Literal["tokens", "premium_requests"]
    input: int | None = None
    output: int | None = None
    requests: float | None = None
    cost_multiplier: float | None = None
    raw: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    model: str
    usage: Usage
    latency_s: float


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    ok: bool
    detail: str
    models: tuple[str, ...] = ()


class Provider(Protocol):
    name: str
    kind: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...

    async def check(self) -> ProviderStatus: ...

    async def aclose(self) -> None: ...
