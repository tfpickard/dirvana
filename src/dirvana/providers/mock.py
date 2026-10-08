"""A deterministic provider for tests and offline development.

Output is a pure function of the request, so regenerated derived context is byte-identical.
For ``suggest`` it builds candidates from the paths in the payload, which lets tests check
that a suggestion uses an edge target's resolved path. Every request is appended to the file
named by ``$DIRVANA_MOCK_CAPTURE`` (tagged with the instance name) so tests can assert exactly
what would have left the machine.

Config knobs (in the instance's table): ``delay`` (seconds), ``fail`` (raise), ``down``
(connection refused), ``output`` (fixed text).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import time

from dirvana._meta import env
from dirvana.config import ProviderConfig
from dirvana.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
    ProviderStatus,
    Usage,
)

_PATH = re.compile(r'"(?:abs|rel)":\s*"([^"]+)"')


def _capture(name: str, req: CompletionRequest) -> None:
    path = os.environ.get(env("MOCK_CAPTURE"))
    if not path:
        return
    rec = {"provider": name, "purpose": req.purpose, "system": req.system, "user": req.user}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(rec) + "\n").encode())
    finally:
        os.close(fd)


def render(req: CompletionRequest) -> str:
    digest = hashlib.sha256((req.system + "\0" + req.user).encode()).hexdigest()[:12]
    if req.purpose == "suggest":
        paths = list(dict.fromkeys(_PATH.findall(req.user)))
        rels = [p for p in paths if not p.startswith("/")] or paths
        cands = [{"cmd": f"diff -ru {p} .", "why": f"mock {digest}: compare"} for p in rels[:2]]
        cands += [{"cmd": f"ls {p}", "why": f"mock {digest}: look"} for p in rels[:2]]
        cands.append({"cmd": "touch MOCK_SHOULD_NEVER_RUN", "why": "inserted, never executed"})
        return json.dumps({"candidates": cands})
    if req.purpose == "brief":
        return f"mock brief {digest}: this directory has history."
    return (
        f"## What this is\nmock context {digest}\n\n## What you do here\n-\n\n"
        f"## Places you reach from here\n-\n\n## Places that reach here\n-\n\n"
        f"## Watch out for\n-\n"
    )


class MockProvider:
    kind = "mock"

    def __init__(self, cfg: ProviderConfig) -> None:
        self.name = cfg.name
        self.cfg = cfg

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        start = time.monotonic()
        if self.cfg.raw.get("down"):
            raise ProviderError(f"{self.name}: connection refused (mock down)")
        _capture(self.name, req)
        delay = self.cfg.get_float("delay", 0.0)
        if delay:
            await asyncio.sleep(delay)
        if self.cfg.raw.get("fail"):
            raise ProviderError(f"{self.name}: mock failure")
        fixed = self.cfg.raw.get("output")
        text = str(fixed) if isinstance(fixed, str) else render(req)
        usage = Usage(unit="tokens", input=len(req.system + req.user) // 4, output=len(text) // 4)
        return CompletionResult(text, f"mock-{self.name}", usage, time.monotonic() - start)

    async def check(self) -> ProviderStatus:
        if self.cfg.raw.get("down"):
            return ProviderStatus(False, "mock down")
        return ProviderStatus(True, "mock", ("mock",))

    async def aclose(self) -> None:
        return None
