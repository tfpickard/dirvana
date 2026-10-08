"""Scenario 15: every adapter passes one contract suite against mocked responses, and the
Copilot session has zero tools and zero MCP servers."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx2
import pytest

from dirvana.config import ProviderConfig
from dirvana.providers import copilot as cp
from dirvana.providers.anthropic import AnthropicProvider
from dirvana.providers.base import (
    CompletionRequest,
    Provider,
    ProviderError,
    ProviderTimeout,
)
from dirvana.providers.mock import MockProvider
from dirvana.providers.openai import OpenAIProvider

REQ = CompletionRequest(
    system="SYSTEM-TEXT", user="USER-TEXT", max_output_tokens=64, timeout_s=2.0, purpose="suggest"
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- mocked transports -----------------------------------------------------------------------


def _anthropic(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicProvider:
    cfg = ProviderConfig("anthropic", "anthropic", {"model": "claude-test", "api_key_env": "X"})
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicProvider(cfg, http_client=client)


def _anthropic_ok(seen: list[dict[str, Any]]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "OUTPUT-TEXT"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )

    return handler


def _openai(handler: Callable[[httpx2.Request], httpx2.Response], **raw: Any) -> OpenAIProvider:
    cfg = ProviderConfig("openai", "openai", {"model": "gpt-test", "api_key_env": "X", **raw})
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAIProvider(cfg, http_client=client)


def _openai_ok(seen: list[dict[str, Any]]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append({"path": request.url.path, **body})
        if request.url.path.endswith("/responses"):
            return httpx2.Response(
                200,
                json={
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 0,
                    "model": "gpt-test",
                    "status": "completed",
                    "parallel_tool_calls": False,
                    "tool_choice": "none",
                    "tools": [],
                    "output": [
                        {
                            "type": "message",
                            "id": "m1",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "OUTPUT-TEXT", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 7,
                        "total_tokens": 18,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "OUTPUT-TEXT"},
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    return handler


def _timeout(_request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("slow")


def _http_500(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500, json={"error": {"type": "api_error", "message": "boom"}})


# -- fake Copilot runtime --------------------------------------------------------------------


class _Ev:
    def __init__(self, type_: str, data: Any) -> None:
        self.type = type_
        self.data = data


class _Data:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class FakeSession:
    def __init__(self, sid: str, on_event: Any, behaviour: str) -> None:
        self.session_id = sid
        self.on_event = on_event
        self.behaviour = behaviour
        self.disconnected = False

    async def send_and_wait(self, prompt: str, timeout: float = 60.0) -> Any:
        self.prompt = prompt
        if self.behaviour == "slow":
            await asyncio.sleep(timeout + 5)
        if self.behaviour == "tool":
            self.on_event(_Ev("tool.execution_start", _Data(tool_name="bash")))
        self.on_event(_Ev("assistant.usage", _Data(input_tokens=11, output_tokens=7, cost=1.0)))
        return _Ev("assistant.message", _Data(content="OUTPUT-TEXT"))

    async def abort(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeCopilotClient:
    instances: ClassVar[list[FakeCopilotClient]] = []
    behaviour = "ok"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sessions: list[FakeSession] = []
        self.session_kwargs: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        FakeCopilotClient.instances.append(self)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def list_models(self) -> list[Any]:
        return [_Data(id="gpt-x"), _Data(id="claude-sonnet-5-5")]

    async def get_auth_status(self) -> Any:
        return _Data(isAuthenticated=True, login="tom", statusMessage="")

    async def create_session(self, **kwargs: Any) -> FakeSession:
        self.session_kwargs.append(kwargs)
        s = FakeSession(f"s{len(self.sessions)}", kwargs["on_event"], self.behaviour)
        self.sessions.append(s)
        return s

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


def _copilot(tmp_path: Path, behaviour: str = "ok", **raw: Any) -> cp.CopilotProvider:
    FakeCopilotClient.behaviour = behaviour
    cfg = ProviderConfig("copilot", "copilot", {"models": ["claude-sonnet-5-5"], **raw})
    return cp.CopilotProvider(
        cfg, base_directory=tmp_path / "copilot", client_factory=FakeCopilotClient
    )


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("X", "sk-test-key")


# -- the shared contract ---------------------------------------------------------------------


def _ok(kind: str, tmp_path: Path, seen: list[dict[str, Any]]) -> Provider:
    if kind == "anthropic":
        return _anthropic(_anthropic_ok(seen))
    if kind == "openai-responses":
        return _openai(_openai_ok(seen))
    if kind == "openai-chat":
        return _openai(_openai_ok(seen), base_url="http://127.0.0.1:9/v1")
    if kind == "copilot":
        return _copilot(tmp_path)
    return MockProvider(ProviderConfig("mock", "mock", {"output": "OUTPUT-TEXT"}))


KINDS = ["anthropic", "openai-responses", "openai-chat", "copilot", "mock"]


@pytest.mark.parametrize("kind", KINDS)
def test_contract_text_in_text_out(kind: str, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    p = _ok(kind, tmp_path, seen)
    res = _run(p.complete(REQ))
    assert res.text == "OUTPUT-TEXT"
    assert res.latency_s >= 0
    if kind == "copilot":
        assert res.usage.unit == "premium_requests" and res.usage.requests == 1.0
    else:
        assert res.usage.unit == "tokens" and (res.usage.input or 0) > 0
    for body in seen:  # nothing but text goes out: no tools, functions or attachments
        flat = json.dumps(body)
        assert "SYSTEM-TEXT" in flat and "USER-TEXT" in flat
        assert not body.get("tools") and "functions" not in body and "tool_choice" not in body
    _run(p.aclose())


@pytest.mark.parametrize("kind", ["anthropic", "openai-responses", "openai-chat", "copilot"])
def test_contract_timeout_is_a_provider_timeout(kind: str, tmp_path: Path) -> None:
    if kind == "anthropic":
        p: Provider = _anthropic(_timeout)
    elif kind.startswith("openai"):
        p = _openai(
            _timeout, **({"base_url": "http://127.0.0.1:9/v1"} if kind.endswith("chat") else {})
        )
    else:
        p = _copilot(tmp_path, "slow")
    req = CompletionRequest("s", "u", 16, 0.2, "suggest")
    with pytest.raises(ProviderTimeout):
        _run(p.complete(req))


@pytest.mark.parametrize("kind", ["anthropic", "openai-responses"])
def test_contract_server_error_is_a_provider_error(kind: str) -> None:
    p: Provider = _anthropic(_http_500) if kind == "anthropic" else _openai(_http_500)
    with pytest.raises(ProviderError):
        _run(p.complete(REQ))


def test_openai_picks_api_by_base_url(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    _run(_openai(_openai_ok(seen)).complete(REQ))
    _run(_openai(_openai_ok(seen), base_url="http://127.0.0.1:11434/v1").complete(REQ))
    assert [s["path"].rsplit("/", 1)[-1] for s in seen] == ["responses", "completions"]


# -- Copilot lockdown --------------------------------------------------------------------------


def test_copilot_session_config_has_no_tools_and_no_mcp(tmp_path: Path) -> None:
    from copilot import CopilotClient

    kw = cp.session_kwargs("claude-sonnet-5-5", str(tmp_path), on_event=None)
    # Every key must be a real parameter of the installed SDK (catches renames on upgrade).
    inspect.signature(CopilotClient.create_session).bind(None, **kw)
    assert kw["available_tools"] == []
    assert kw["tools"] == []
    assert kw["mcp_servers"] == {}
    assert kw["custom_agents"] == []
    assert set(cp.BUILTIN_MCP_SERVERS) <= set(kw["disabled_mcp_servers"])
    assert sorted(kw["excluded_tools"].to_list()) == ["builtin:*", "custom:*", "mcp:*"]
    for flag in (
        "enable_skills",
        "enable_config_discovery",
        "enable_on_demand_instruction_discovery",
        "enable_file_hooks",
        "enable_host_git_operations",
        "enable_session_store",
        "enable_mcp_apps",
    ):
        assert kw[flag] is False, flag
    assert kw["skip_custom_instructions"] is True
    assert kw["infinite_sessions"] == {"enabled": False}
    assert (
        kw["skill_directories"] == kw["plugin_directories"] == kw["instruction_directories"] == []
    )
    assert kw["system_message"]["mode"] == "replace"
    assert kw["hooks"]["on_pre_tool_use"](None, {})["permissionDecision"] == "deny"
    from copilot.rpc import PermissionDecisionReject

    assert isinstance(kw["on_permission_request"](None, None), PermissionDecisionReject)


def test_copilot_client_is_empty_mode(tmp_path: Path) -> None:
    from copilot import CopilotClient

    cfg = ProviderConfig("copilot", "copilot", {})
    kw = cp.client_kwargs(cfg, str(tmp_path))
    inspect.signature(CopilotClient.__init__).bind(None, **kw)
    assert kw["mode"] == "empty" and kw["base_directory"] == str(tmp_path)
    assert kw["use_logged_in_user"] is False and kw["github_token"] == "github_pat_test"


def test_copilot_runtime_receives_the_locked_config(tmp_path: Path) -> None:
    FakeCopilotClient.instances.clear()
    p = _copilot(tmp_path)
    _run(p.complete(REQ))
    client = FakeCopilotClient.instances[-1]
    assert client.kwargs["mode"] == "empty"
    used = client.session_kwargs[0]
    assert used["available_tools"] == [] and used["mcp_servers"] == {}
    assert used["model"] == "claude-sonnet-5-5"
    assert "SYSTEM-TEXT" in client.sessions[0].prompt and "USER-TEXT" in client.sessions[0].prompt
    # Single use: the session was disconnected and deleted; a fresh one is pre-warmed.
    assert client.sessions[0].disconnected and client.deleted == ["s0"]
    _run(p.aclose())


def test_copilot_tool_event_aborts(tmp_path: Path) -> None:
    p = _copilot(tmp_path, "tool")
    with pytest.raises(cp.CopilotToolViolation):
        _run(p.complete(REQ))


def test_copilot_check_reports_auth_and_models(tmp_path: Path) -> None:
    status = _run(_copilot(tmp_path).check())
    assert status.ok and "tom" in status.detail and "claude-sonnet-5-5" in status.models
