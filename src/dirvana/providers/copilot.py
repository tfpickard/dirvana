"""GitHub Copilot adapter via the official Copilot SDK (``pip install dirvana[copilot]``).

The SDK drives the Copilot CLI runtime over JSON-RPC and by default exposes every first-party
agent tool (filesystem, shell, git, web) plus built-in MCP servers. dirvana runs it as pure
text in, text out, with several independent layers of lockdown:

1. ``CopilotClient(mode="empty")``: no ambient OS tools, skills, memory, file hooks, host git
   operations or instruction discovery; state confined to ``base_directory``.
2. ``available_tools=[]`` *and* every built-in, MCP and custom tool excluded.
3. No MCP servers, built-in MCP servers disabled, no custom agents, no skills, no config or
   instruction discovery, no infinite sessions, an empty private working directory.
4. A permission handler that rejects everything and a pre-tool-use hook that denies.
5. Any ``tool.execution_start`` event aborts the request with :class:`CopilotToolViolation`.
6. Each session is used for exactly one request and then deleted, so no prompt text
   accumulates and nothing crosses between directories.

``tests/contract`` binds :func:`session_kwargs` against the installed SDK's real signature
and asserts every one of these settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
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

# Built-in MCP servers the CLI ships; disabled by name in addition to the wildcard exclusion.
BUILTIN_MCP_SERVERS = ("github-mcp-server", "playwright", "fetch", "time")

SYSTEM = (
    "You are a text-only assistant embedded in a command-line tool. You have no tools and no "
    "access to any filesystem, network or shell; never try to use any. Read the task and "
    "context in the user message and reply with exactly the requested output format."
)


class CopilotToolViolation(ProviderError):
    """The runtime started a tool despite the lockdown. The request is aborted."""


def deny_permission(_request: Any, _invocation: Any) -> Any:
    from copilot.rpc import PermissionDecisionReject

    return PermissionDecisionReject(feedback="dirvana: tools are disabled")


def deny_tool_hook(_input: Any, _ctx: Any) -> dict[str, str]:
    return {"permissionDecision": "deny", "permissionDecisionReason": "dirvana: tools are disabled"}


def excluded_tools() -> Any:
    from copilot import ToolSet

    return ToolSet().add_builtin("*").add_mcp("*").add_custom("*")


def session_kwargs(model: str | None, workdir: str, on_event: Any = None) -> dict[str, Any]:
    """Everything passed to ``CopilotClient.create_session``. Pure, so tests can inspect it."""
    return {
        "model": model,
        "system_message": {"mode": "replace", "content": SYSTEM},
        "available_tools": [],
        "excluded_tools": excluded_tools(),
        "tools": [],
        "mcp_servers": {},
        "disabled_mcp_servers": list(BUILTIN_MCP_SERVERS),
        "custom_agents": [],
        "enable_skills": False,
        "skill_directories": [],
        "plugin_directories": [],
        "instruction_directories": [],
        "enable_config_discovery": False,
        "enable_on_demand_instruction_discovery": False,
        "skip_custom_instructions": True,
        "enable_file_hooks": False,
        "enable_host_git_operations": False,
        "enable_session_store": False,
        "enable_mcp_apps": False,
        "infinite_sessions": {"enabled": False},
        "working_directory": workdir,
        "additional_directories": [],
        "on_permission_request": deny_permission,
        "hooks": {"on_pre_tool_use": deny_tool_hook},
        "on_event": on_event,
        "streaming": False,
    }


def client_kwargs(cfg: ProviderConfig, base_directory: str) -> dict[str, Any]:
    auth = str(cfg.raw.get("auth", "env"))
    kw: dict[str, Any] = {"mode": "empty", "base_directory": base_directory, "log_level": "error"}
    if auth == "logged-in":
        kw["use_logged_in_user"] = True
        return kw
    kw["use_logged_in_user"] = False
    token: str | None = None
    if auth == "gh":
        try:
            token = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError) as e:
            raise ProviderError(f"{cfg.name}: `gh auth token` failed: {e}") from e
    else:
        for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(var):
                token = os.environ[var]
                break
    if not token:
        raise ProviderError(
            f'{cfg.name}: no GitHub token (set COPILOT_GITHUB_TOKEN or auth = "gh")'
        )
    kw["github_token"] = token
    return kw


def _event_type(ev: Any) -> str:
    t = getattr(ev, "type", "")
    return str(getattr(t, "value", t))


class CopilotProvider:
    kind = "copilot"

    def __init__(
        self, cfg: ProviderConfig, *, base_directory: Path, client_factory: Any = None
    ) -> None:
        self.name = cfg.name
        self.cfg = cfg
        self.base_directory = base_directory
        self._client_factory = client_factory
        self._client: Any = None
        self._model: str | None = None
        self._lock = asyncio.Lock()
        self._warm: asyncio.Task[tuple[Any, dict[str, Any]]] | None = None
        self._workdir = tempfile.mkdtemp(prefix="dirvana-copilot-")
        os.chmod(self._workdir, 0o700)

    async def _ensure_client(self) -> Any:
        async with self._lock:
            if self._client is not None:
                return self._client
            self.base_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            kw = client_kwargs(self.cfg, str(self.base_directory))
            if self._client_factory is not None:
                client = self._client_factory(**kw)
            else:
                try:
                    from copilot import CopilotClient
                except ImportError as e:
                    raise ProviderError(
                        "install the copilot extra: uv tool install 'dirvana[copilot]'"
                    ) from e
                client = CopilotClient(**kw)
            await client.start()
            self._client = client
            self._model = await self._pick_model(client)
            return client

    async def _pick_model(self, client: Any) -> str | None:
        models = await client.list_models()
        ids = [str(getattr(m, "id", "")) for m in models]
        for want in self.cfg.models:
            if want in ids:
                return want
        return ids[0] if ids else None

    async def _new_session(self) -> tuple[Any, dict[str, Any]]:
        client = await self._ensure_client()
        state: dict[str, Any] = {"violation": None, "usage": None}

        def on_event(ev: Any) -> None:
            kind = _event_type(ev)
            if kind.startswith("tool.") or kind.startswith("skill."):
                state["violation"] = kind
            elif kind == "assistant.usage":
                state["usage"] = ev.data

        session = await client.create_session(
            **session_kwargs(self._model, self._workdir, on_event)
        )
        return session, state

    def _prewarm(self) -> None:
        if self._warm is None or self._warm.done():
            self._warm = asyncio.ensure_future(self._new_session())

    async def warm(self) -> None:
        """Start the runtime and pre-create one fresh session (daemon start-up)."""
        self._prewarm()
        assert self._warm is not None
        await asyncio.shield(self._warm)

    async def _take_session(self) -> tuple[Any, dict[str, Any]]:
        task, self._warm = self._warm, None
        if task is not None:
            try:
                return await task
            except Exception:  # an expired or broken warm session; make a fresh one
                pass
        return await self._new_session()

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        start = time.monotonic()
        session, state = await self._take_session()
        self._prewarm()
        prompt = f"{req.system}\n\n{req.user}"
        try:
            ev = await asyncio.wait_for(
                session.send_and_wait(prompt, timeout=req.timeout_s), req.timeout_s + 1
            )
        except TimeoutError as e:
            with contextlib.suppress(Exception):
                await session.abort()
            raise ProviderTimeout(f"{self.name}: timed out") from e
        except Exception as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        finally:
            await self._discard(session)
        if state["violation"]:
            raise CopilotToolViolation(f"{self.name}: runtime emitted {state['violation']}")
        text = str(getattr(getattr(ev, "data", None), "content", "") or "") if ev else ""
        if not text:
            raise ProviderError(f"{self.name}: empty response")
        u = state["usage"]
        usage = Usage(
            unit="premium_requests",
            input=getattr(u, "input_tokens", None),
            output=getattr(u, "output_tokens", None),
            requests=float(getattr(u, "cost", 1.0) or 1.0),
            cost_multiplier=getattr(u, "cost", None),
        )
        return CompletionResult(text, self._model or "copilot", usage, time.monotonic() - start)

    async def _discard(self, session: Any) -> None:
        with contextlib.suppress(Exception):
            await session.disconnect()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.delete_session(session.session_id)

    async def check(self) -> ProviderStatus:
        try:
            client = await self._ensure_client()
            auth = await client.get_auth_status()
            if not getattr(auth, "isAuthenticated", False):
                return ProviderStatus(
                    False, f"not authenticated: {getattr(auth, 'statusMessage', '')}"
                )
            models = tuple(str(getattr(m, "id", "")) for m in await client.list_models())
        except ProviderError as e:
            return ProviderStatus(False, str(e))
        except Exception as e:
            return ProviderStatus(False, f"runtime unavailable: {type(e).__name__}: {e}")
        if not models:
            return ProviderStatus(False, "authenticated, but no models (Copilot subscription?)")
        who = getattr(auth, "login", "?")
        return ProviderStatus(True, f"authenticated as {who}; using {self._model}", models)

    async def aclose(self) -> None:
        if self._warm is not None:
            with contextlib.suppress(Exception):
                session, _ = await self._warm
                await self._discard(session)
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.stop()
        with contextlib.suppress(OSError):
            os.rmdir(self._workdir)
