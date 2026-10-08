"""Usage log and budget caps, per provider instance, in the provider's native unit.

``var/usage.jsonl`` records every call (tokens for Anthropic/OpenAI, premium requests for
Copilot). Before a call, the day's usage is summed and compared with the instance's
``max_*_per_day``; a run (one daemon tick or one CLI invocation) has its own ``max_*_per_run``.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dirvana.config import ProviderConfig
from dirvana.providers.base import Usage
from dirvana.store.io import read_jsonl


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).date().isoformat()


def _spent(u: Usage) -> float:
    if u.unit == "premium_requests":
        return float(u.requests or 0.0)
    return float((u.input or 0) + (u.output or 0))


@dataclass(slots=True)
class Budget:
    path: Path
    run_spent: dict[str, float] = field(default_factory=dict[str, float])

    def limits(self, pc: ProviderConfig) -> tuple[float | None, float | None]:
        if pc.kind == "copilot":
            keys = ("max_requests_per_run", "max_requests_per_day")
        else:
            keys = ("max_tokens_per_run", "max_tokens_per_day")
        run, day = (pc.raw.get(k) for k in keys)
        return (
            float(run) if isinstance(run, (int, float)) else None,
            float(day) if isinstance(day, (int, float)) else None,
        )

    def spent_today(self, name: str, now: float | None = None) -> float:
        today = _day(now if now is not None else time.time())
        total = 0.0
        for rec in read_jsonl(self.path):
            if rec.get("provider") == name and _day(float(rec.get("t", 0))) == today:
                total += float(rec.get("spent", 0.0))
        return total

    def exhausted(self, pc: ProviderConfig) -> str | None:
        """Why ``pc`` may not be called now, or ``None``."""
        run, day = self.limits(pc)
        if run is not None and self.run_spent.get(pc.name, 0.0) >= run:
            return f"{pc.name}: per-run budget reached"
        if day is not None and self.spent_today(pc.name) >= day:
            return f"{pc.name}: daily budget reached"
        return None

    def record(
        self,
        pc: ProviderConfig,
        *,
        model: str,
        purpose: str,
        usage: Usage | None,
        ok: bool,
        node: str | None,
        reason: str | None = None,
    ) -> None:
        spent = _spent(usage) if usage else 0.0
        self.run_spent[pc.name] = self.run_spent.get(pc.name, 0.0) + spent
        rec = {
            "t": round(time.time(), 3),
            "provider": pc.name,
            "kind": pc.kind,
            "model": model,
            "purpose": purpose,
            "unit": usage.unit if usage else None,
            "input": usage.input if usage else None,
            "output": usage.output if usage else None,
            "requests": usage.requests if usage else None,
            "cost_multiplier": usage.cost_multiplier if usage else None,
            "spent": spent,
            "node": node,
            "ok": ok,
            "reason": reason,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
