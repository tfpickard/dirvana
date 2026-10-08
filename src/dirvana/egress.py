"""The egress guard: the only path from canonical data to a provider.

* **Subject refusal**: the directory a request is *about* must allow the provider instance,
  or nothing is sent at all (local fallback, never another provider).
* **Item filtering**: every piece of context that names another directory (edges, session
  history, derived text that mentions peers) is kept only if *that* directory allows the
  instance. Prompt builders ask :meth:`EgressGuard.allows` per item.
* **Final scrub**: every absolute or ``~/`` path token in the outgoing text is checked again,
  and tokens into forbidden or ignored subtrees become ``<redacted:path>``; then secret
  redaction runs once more. The daemon repeats this check on whatever it receives.

Policy: ``llm=none`` forbids every instance; ``llm=a,b`` allows exactly those; unset allows
every *configured* instance. Ignored subtrees allow nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from dirvana.config import Config
from dirvana.paths import expand_home
from dirvana.policy import Rule, is_ignored, resolve
from dirvana.providers.base import CompletionRequest
from dirvana.redact import redact

PATH_MASK = "<redacted:path>"

# Absolute or home-relative path tokens, stopping at shell metacharacters and quotes.
_PATH_TOKEN = re.compile(
    r"""(?<![\w.~/:-])(~(?=/|\s|$|['"`])/?[^\s'"`;|&<>(){}]*|/[^\s'"`;|&<>(){},]+)"""
)


class EgressDenied(Exception):
    """The subject directory does not allow this provider instance."""


@dataclass
class EgressGuard:
    config: Config
    rules: Sequence[Rule]
    home_dir: str
    _cache: dict[str, tuple[str, ...] | None] = field(
        default_factory=dict[str, tuple[str, ...] | None]
    )

    def policy_llm(self, path: str) -> tuple[str, ...] | None:
        """``None``: unrestricted. ``()``: nothing (``llm=none`` or ignored)."""
        if path in self._cache:
            return self._cache[path]
        if is_ignored(path, self.rules):
            value: tuple[str, ...] | None = ()
        else:
            value = resolve(path, self.rules).llm
        self._cache[path] = value
        return value

    def allows(self, path: str, instance: str) -> bool:
        llm = self.policy_llm(path)
        if llm is None:
            return instance in self.config.providers
        return instance in llm

    def allowed(self, subject: str) -> list[str]:
        """Configured instances the subject allows, in preference order."""
        return [i for i in self.config.configured() if self.allows(subject, i)]

    def decision(self, path: str) -> str:
        """A provider-independent label of the policy for ``path`` (fingerprint input)."""
        llm = self.policy_llm(path)
        return "any" if llm is None else ",".join(llm) or "none"

    def scrub(self, text: str, instance: str) -> str:
        def repl(m: re.Match[str]) -> str:
            token = m.group(1)
            path = os.path.normpath(expand_home(token, self.home_dir)) if token else token
            if not path.startswith("/") or self.allows(path, instance):
                return token
            return PATH_MASK

        return redact(_PATH_TOKEN.sub(repl, text))

    def check_request(
        self, subject: str, instance: str, req: CompletionRequest
    ) -> CompletionRequest:
        """Refuse if the subject forbids ``instance``; otherwise return the scrubbed request."""
        if instance not in self.allowed(subject):
            raise EgressDenied(f"{subject}: policy does not allow {instance}")
        return CompletionRequest(
            system=self.scrub(req.system, instance),
            user=self.scrub(req.user, instance),
            max_output_tokens=req.max_output_tokens,
            timeout_s=req.timeout_s,
            purpose=req.purpose,
        )
