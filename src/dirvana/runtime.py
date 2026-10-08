"""Everything a command needs, opened once: dirs, config, policy, store, egress guard, budget."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from dirvana import config as configmod
from dirvana._meta import env
from dirvana.budget import Budget
from dirvana.config import Config
from dirvana.egress import EgressGuard
from dirvana.paths import Dirs, home
from dirvana.policy import Rule, load_rules
from dirvana.store.ingest import Store


@dataclass
class Runtime:
    dirs: Dirs
    config: Config
    rules: list[Rule]
    store: Store
    guard: EgressGuard
    budget: Budget
    home: str

    @classmethod
    def open(cls) -> Runtime:
        dirs = Dirs.from_env()
        h = str(home())
        cfg = configmod.load(dirs)
        rules = load_rules(dirs, h)
        return cls(
            dirs=dirs,
            config=cfg,
            rules=rules,
            store=Store(dirs, rules, h),
            guard=EgressGuard(cfg, rules, h),
            budget=Budget(dirs.var / "usage.jsonl"),
            home=h,
        )

    def reload_policy(self) -> None:
        """Re-read policy files (the daemon does this every tick, so edits apply live)."""
        self.rules = load_rules(self.dirs, self.home)
        self.store.rules = self.rules
        self.guard = EgressGuard(self.config, self.rules, self.home)

    @property
    def socket_path(self) -> str:
        return str(self.dirs.run / "daemon.sock")


def now() -> float:
    """Wall clock, overridable with ``DIRVANA_NOW`` for deterministic tests."""
    value = os.environ.get(env("NOW"), "")
    return float(value) if value else time.time()
