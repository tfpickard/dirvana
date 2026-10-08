"""OpenAI adapter (``pip install dirvana[openai]``).

Uses the Responses API against api.openai.com. With ``base_url`` set (Ollama, llama.cpp,
LM Studio and other OpenAI-compatible servers) it uses Chat Completions, the surface those
servers all implement; ``api = "responses"|"chat"`` overrides the choice.
"""

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


class OpenAIProvider:
    kind = "openai"

    def __init__(self, cfg: ProviderConfig, *, http_client: Any = None) -> None:
        try:
            import openai
        except ImportError as e:
            raise ProviderError(
                "install the openai extra: uv tool install 'dirvana[openai]'"
            ) from e
        self.name = cfg.name
        self.cfg = cfg
        self._sdk = openai
        api = cfg.raw.get("api")
        self.api = (
            str(api) if api in ("responses", "chat") else ("chat" if cfg.base_url else "responses")
        )
        self._client = openai.AsyncOpenAI(
            api_key=cfg.api_key() or ("unused" if cfg.base_url else None),
            base_url=cfg.base_url,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        start = time.monotonic()
        sdk = self._sdk
        client = self._client.with_options(timeout=req.timeout_s)
        try:
            if self.api == "responses":
                r = await client.responses.create(
                    model=self.cfg.model,
                    instructions=req.system,
                    input=req.user,
                    max_output_tokens=req.max_output_tokens,
                )
                text = r.output_text
                u = r.usage
                usage = Usage(
                    unit="tokens",
                    input=u.input_tokens if u else None,
                    output=u.output_tokens if u else None,
                )
                model = r.model
            else:
                c = await client.chat.completions.create(
                    model=self.cfg.model,
                    messages=[
                        {"role": "system", "content": req.system},
                        {"role": "user", "content": req.user},
                    ],
                    max_tokens=req.max_output_tokens,
                )
                text = c.choices[0].message.content or "" if c.choices else ""
                cu = c.usage
                usage = Usage(
                    unit="tokens",
                    input=cu.prompt_tokens if cu else None,
                    output=cu.completion_tokens if cu else None,
                )
                model = c.model
        except sdk.APITimeoutError as e:
            raise ProviderTimeout(f"{self.name}: timed out") from e
        except sdk.APIError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        return CompletionResult(text, model, usage, time.monotonic() - start)

    async def check(self) -> ProviderStatus:
        problem = self.cfg.problem()
        if problem:
            return ProviderStatus(False, problem)
        try:
            page = await self._client.with_options(timeout=10).models.list()
            ids = tuple(m.id for m in page.data)
        except self._sdk.APIError as e:
            return ProviderStatus(False, f"{type(e).__name__}: {e}")
        if ids and self.cfg.model not in ids:
            return ProviderStatus(False, f"model {self.cfg.model} not offered", ids)
        return ProviderStatus(True, f"model {self.cfg.model} available ({self.api} API)", ids)

    async def aclose(self) -> None:
        await self._client.close()
