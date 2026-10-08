"""Anthropic Messages API adapter (``pip install dirvana[anthropic]``)."""

from __future__ import annotations

import time
from typing import Any

from dirvana.config import ProviderConfig
from dirvana.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
    ProviderStatus,
    ProviderTimeout,
    Usage,
)


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, cfg: ProviderConfig, *, http_client: Any = None) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError(
                "install the anthropic extra: uv tool install 'dirvana[anthropic]'"
            ) from e
        self.name = cfg.name
        self.cfg = cfg
        self._sdk = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key(),
            base_url=cfg.base_url,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        start = time.monotonic()
        sdk = self._sdk
        try:
            msg = await self._client.with_options(timeout=req.timeout_s).messages.create(
                model=self.cfg.model,
                max_tokens=req.max_output_tokens,
                system=req.system,
                messages=[{"role": "user", "content": req.user}],
            )
        except sdk.APITimeoutError as e:
            raise ProviderTimeout(f"{self.name}: timed out") from e
        except sdk.APIError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        text = "".join(getattr(b, "text", "") for b in msg.content if b.type == "text")
        usage = Usage(
            unit="tokens",
            input=msg.usage.input_tokens,
            output=msg.usage.output_tokens,
            raw={"stop_reason": str(msg.stop_reason)},
        )
        return CompletionResult(text, msg.model, usage, time.monotonic() - start)

    async def check(self) -> ProviderStatus:
        problem = self.cfg.problem()
        if problem:
            return ProviderStatus(False, problem)
        try:
            await self._client.with_options(timeout=10).models.retrieve(self.cfg.model)
        except self._sdk.APIError as e:
            return ProviderStatus(False, f"{type(e).__name__}: {e}")
        return ProviderStatus(True, f"model {self.cfg.model} reachable", (self.cfg.model,))

    async def aclose(self) -> None:
        await self._client.close()
