"""Enrichment: turn a node's canonical data into derived LLM context, only when it changed.

A node is dirty when the fingerprint of its deterministic enrichment input differs from the
one recorded with its derived context. Missing derived context is always dirty (so deleting
``<root>/derived`` regenerates everything); otherwise ``min_interval`` and, when only the
command lists changed, ``min_new_observations`` hold re-enrichment back. A run with nothing
changed makes zero provider calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dirvana.budget import Budget
from dirvana.config import Config
from dirvana.context import NodeView, load_view
from dirvana.egress import EgressDenied, EgressGuard
from dirvana.paths import CONTEXT_FILE, CONTEXT_META_FILE, Dirs, real_from_shadow
from dirvana.policy import resolve
from dirvana.prompts import ENRICH_TEMPLATE, enrich_input, fingerprints, render_enrich
from dirvana.providers.base import Provider, ProviderError
from dirvana.providers.registry import build
from dirvana.store.ingest import Store, iter_nodes
from dirvana.store.io import write_json_atomic, write_text_atomic


class ProviderPool:
    """Adapters by instance name, created lazily and kept warm (the daemon holds one)."""

    def __init__(self, config: Config, dirs: Dirs, **overrides: Any) -> None:
        self.config = config
        self.dirs = dirs
        self._overrides = overrides
        self._live: dict[str, Provider] = {}

    def get(self, name: str) -> Provider:
        if name not in self._live:
            self._live[name] = build(
                self.config.providers[name], self.dirs, **self._overrides.get(name, {})
            )
        return self._live[name]

    async def aclose(self) -> None:
        for p in self._live.values():
            with contextlib.suppress(Exception):  # closing is best effort
                await p.aclose()
        self._live.clear()


@dataclass(slots=True)
class Report:
    considered: int = 0
    enriched: list[str] = field(default_factory=list[str])
    skipped: dict[str, str] = field(default_factory=dict[str, str])
    calls: int = 0
    failures: list[str] = field(default_factory=list[str])


def _today(now: float) -> Any:
    return datetime.fromtimestamp(now, UTC).date()


def candidates(store: Store, config: Config) -> list[str]:
    out: list[str] = []
    for d in iter_nodes(store.dirs.system):
        real = real_from_shadow(store.dirs.system, d)
        if real is not None and not store.ignored(real):
            out.append(real)
    return sorted(out)


def _decide(
    view: NodeView, full: str, struct: str, config: Config, now: float, *, force: bool
) -> str | None:
    """``None`` to enrich now, else the reason to skip."""
    meta = view.derived_meta
    if force or not view.derived or not meta:
        return None
    if meta.get("fingerprint") == full:
        return "clean"
    created = float(meta.get("created", 0) or 0)
    if now - created < config.duration("enrich", "min_interval", "6h"):
        return "changed, waiting for min_interval"
    if meta.get("structural") != struct:
        return None
    new = view.obs_count - int(meta.get("obs_count", 0) or 0)
    if new < config.num("enrich", "min_new_observations", 5):
        return f"only {new} new observations"
    return None


async def enrich(
    store: Store,
    config: Config,
    guard: EgressGuard,
    pool: ProviderPool,
    budget: Budget,
    *,
    paths: list[str] | None = None,
    force: bool = False,
    explicit: bool = False,
    dry_run: bool = False,
    now: float | None = None,
) -> Report:
    """Enrich ``paths`` (default: every candidate node) where dirty. ``explicit`` marks a user
    request, which also covers ``enrich=sync`` nodes."""
    now = now if now is not None else time.time()
    report = Report()
    limit = int(config.num("enrich", "max_nodes_per_run", 25))
    min_obs = int(config.num("enrich", "min_observations", 3))
    for path in paths if paths is not None else candidates(store, config):
        report.considered += 1
        mode = resolve(path, store.rules).enrich
        if mode == "off" or (mode == "sync" and not explicit):
            report.skipped[path] = f"enrich={mode}"
            continue
        view = load_view(store, path, today=_today(now), ranking=config.ranking(), live_out=False)
        established = [e for e in view.out_edges + view.in_edges if not e.tentative]
        if not explicit and len(view.commands) < min_obs and not established:
            report.skipped[path] = "too little activity"
            continue
        data = enrich_input(view, guard)
        full, struct = fingerprints(data)
        reason = _decide(view, full, struct, config, now, force=force)
        if reason:
            report.skipped[path] = reason
            continue
        if len(report.enriched) >= limit:
            report.skipped[path] = "max_nodes_per_run reached"
            continue
        instances = guard.allowed(path)
        if not instances:
            report.skipped[path] = "no provider allowed by policy"
            continue
        if dry_run:
            report.enriched.append(path)
            continue
        done = False
        for inst in instances:
            pc = config.providers[inst]
            why = budget.exhausted(pc)
            if why:
                report.skipped[path] = why
                continue
            rendered = render_enrich(
                data,
                inst,
                guard,
                max_tokens=int(config.num("enrich", "max_output_tokens", 900)),
                timeout=60.0,
            )
            try:
                req = guard.check_request(path, inst, rendered.request)
                report.calls += 1
                result = await pool.get(inst).complete(req)
            except (ProviderError, EgressDenied) as e:
                budget.record(
                    pc,
                    model=pc.model,
                    purpose="enrich",
                    usage=None,
                    ok=False,
                    node=path,
                    reason=str(e),
                )
                report.failures.append(f"{path}: {e}")
                continue
            budget.record(
                pc, model=result.model, purpose="enrich", usage=result.usage, ok=True, node=path
            )
            ddir = store.derived_dir(path)
            write_text_atomic(ddir / CONTEXT_FILE, result.text.rstrip() + "\n")
            write_json_atomic(
                ddir / CONTEXT_META_FILE,
                {
                    "v": 1,
                    "fingerprint": full,
                    "structural": struct,
                    "template": ENRICH_TEMPLATE,
                    "provider": inst,
                    "model": result.model,
                    "created": round(now, 3),
                    "obs_count": view.obs_count,
                    "mentions": list(rendered.mentions),
                    "usage": {
                        "unit": result.usage.unit,
                        "input": result.usage.input,
                        "output": result.usage.output,
                        "requests": result.usage.requests,
                    },
                },
            )
            report.enriched.append(path)
            report.skipped.pop(path, None)
            done = True
            break
        if not done and path not in report.skipped:
            report.skipped[path] = "all allowed providers failed"
    return report


def run_enrich(dirs: Dirs, config: Config, store: Store, guard: EgressGuard, **kw: Any) -> Report:
    pool = ProviderPool(config, dirs)
    budget = Budget(dirs.var / "usage.jsonl")

    async def go() -> Report:
        try:
            return await enrich(store, config, guard, pool, budget, **kw)
        finally:
            await pool.aclose()

    return asyncio.run(go())
