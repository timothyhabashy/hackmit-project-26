from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import APITimeoutError, AuthenticationError, InternalServerError, RateLimitError
from anthropic.types import Message

from precedent.config import load_settings
from precedent.fixtures import T03_FEE_ID, T03_INVOICE_ID, T03_PAYMENT_ID, T03_REMIT_ID
from precedent.models import ErrorCode, ProviderTurn, ToolCall, ToolResult, Usage
from precedent.providers import (
    PREFLIGHT_TOOL_NAME,
    SHORT_RETRY_DELAY_SECONDS,
    AnthropicProvider,
    ProviderError,
    ScriptedProvider,
    live_tool_smoke_test,
    select_provider,
    tool_result_user_message,
)
from precedent.tools import tool_definitions

T03_CASE_ID = "CASE-1CFA9FEE848F"
CONFIGURED_MODEL = "claude-test-model"


class FakeMessagesClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("Fake client has no remaining outcomes")
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _settings(tmp_path: Path, **env: str):
    merged = {
        "PRECEDENT_ENABLE_LIVE": "true",
        "ANTHROPIC_API_KEY": "secret-test-key",
        "PRECEDENT_MODEL": CONFIGURED_MODEL,
    }
    merged.update(env)
    return load_settings(environ=merged, dotenv_path=tmp_path / "missing.env", repo_root=tmp_path)


def _usage(*, attempts: int = 1, tools: int = 0, elapsed_ms: int = 1) -> Usage:
    return Usage(
        model_attempts=attempts,
        tool_calls=tools,
        input_tokens=8,
        output_tokens=4,
        elapsed_ms=elapsed_ms,
        estimated_cost_usd=None,
        price_rate_provenance=None,
    )


def _turn(*, text: str | None = None, tool_calls: list[ToolCall] | None = None) -> ProviderTurn:
    calls = tool_calls or []
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in calls:
        content.append(
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return ProviderTurn(
        tool_calls=calls,
        public_text=text,
        stop_reason="end_turn" if not calls else "tool_use",
        provider_assistant_message={"role": "assistant", "content": content},
        usage=_usage(tools=len(calls)),
        provider_request_id="msg_scripted",
    )


def _sdk_message(
    *,
    text: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    model: str = CONFIGURED_MODEL,
    input_tokens: int = 11,
    output_tokens: int = 7,
    extra_content: list[dict[str, Any]] | None = None,
) -> Message:
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tool in tools or []:
        content.append(
            {
                "type": "tool_use",
                "id": tool["id"],
                "name": tool["name"],
                "input": tool.get("input", {}),
            }
        )
    if extra_content:
        content.extend(extra_content)
    return Message.model_validate(
        {
            "id": "msg_01Aq9w938a90dw8q",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def _http_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _http_response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, request=_http_request())


def test_scripted_provider_returns_text_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_ENABLE_LIVE="false")
    provider = ScriptedProvider([_turn(text="Need a remittance before settling.")])
    turn = provider.generate("system", [{"role": "user", "content": T03_CASE_ID}], [], settings)
    assert turn.public_text == "Need a remittance before settling."
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"


def test_scripted_provider_one_and_multiple_tool_calls(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_ENABLE_LIVE="false")
    provider = ScriptedProvider(
        [
            _turn(
                tool_calls=[
                    ToolCall(call_id="toolu_get_case", name="get_case", arguments={}),
                ]
            ),
            _turn(
                tool_calls=[
                    ToolCall(
                        call_id="toolu_remit",
                        name="read_document",
                        arguments={"document_id": T03_REMIT_ID},
                    ),
                    ToolCall(
                        call_id="toolu_fee",
                        name="read_document",
                        arguments={"document_id": T03_FEE_ID},
                    ),
                ]
            ),
        ]
    )
    first = provider.generate(
        "sys",
        [{"role": "user", "content": T03_CASE_ID}],
        tool_definitions(),
        settings,
    )
    assert [call.name for call in first.tool_calls] == ["get_case"]
    second = provider.generate(
        "sys",
        [{"role": "user", "content": "continue"}],
        tool_definitions(),
        settings,
    )
    assert [call.name for call in second.tool_calls] == ["read_document", "read_document"]
    assert [call.arguments["document_id"] for call in second.tool_calls] == [
        T03_REMIT_ID,
        T03_FEE_ID,
    ]
    retained = second.provider_assistant_message["content"]
    assert retained[0]["type"] == "tool_use"
    assert retained[1]["id"] == "toolu_fee"


def test_tool_result_message_keeps_call_ids_and_error_flag() -> None:
    ok = ToolResult(
        ok=True,
        data={"payment_id": T03_PAYMENT_ID, "invoice_id": T03_INVOICE_ID},
        error=None,
    )
    failed = ToolResult(
        ok=False,
        data=None,
        error={"code": "UNKNOWN_TOOL", "message": "shell is not a registered tool"},
    )
    message = tool_result_user_message(
        [("toolu_get_case", ok), ("toolu_shell", failed)],
    )
    assert message["role"] == "user"
    assert message["content"][0]["type"] == "tool_result"
    assert message["content"][0]["tool_use_id"] == "toolu_get_case"
    assert message["content"][0]["is_error"] is False
    assert T03_PAYMENT_ID in message["content"][0]["content"]
    assert message["content"][1]["tool_use_id"] == "toolu_shell"
    assert message["content"][1]["is_error"] is True


def test_select_provider_never_uses_scripted_in_live_mode(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ProviderError, match="never selected") as exc_info:
        select_provider(settings, scripted=ScriptedProvider([]))
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    selected = select_provider(settings)
    assert isinstance(selected, AnthropicProvider)


def test_anthropic_text_and_tool_turns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            _sdk_message(text="Inspect Harbor's remittance before applying PAY-201."),
            _sdk_message(
                text="Opening the remittance and fee notice.",
                tools=[
                    {
                        "id": "toolu_r201",
                        "name": "read_document",
                        "input": {"document_id": T03_REMIT_ID},
                    },
                    {
                        "id": "toolu_f201",
                        "name": "read_document",
                        "input": {"document_id": T03_FEE_ID},
                    },
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    provider = AnthropicProvider(settings, client=client)
    text_turn = provider.generate(
        "Investigate T03.",
        [{"role": "user", "content": T03_CASE_ID}],
        [],
        settings,
    )
    assert text_turn.public_text == "Inspect Harbor's remittance before applying PAY-201."
    assert text_turn.tool_calls == []
    assert text_turn.usage.input_tokens == 11
    assert text_turn.usage.estimated_cost_usd is None
    tool_turn = provider.generate(
        "Investigate T03.",
        [{"role": "user", "content": T03_CASE_ID}],
        tool_definitions(),
        settings,
    )
    assert tool_turn.stop_reason == "tool_use"
    assert [call.call_id for call in tool_turn.tool_calls] == ["toolu_r201", "toolu_f201"]
    retained = tool_turn.provider_assistant_message
    assert retained["role"] == "assistant"
    assert retained["content"][1]["type"] == "tool_use"
    assert retained["content"][1]["id"] == "toolu_r201"
    assert "temperature" not in client.calls[0]
    assert "thinking" not in client.calls[0]
    assert "cache_control" not in client.calls[0]
    assert client.calls[1]["tools"][0]["name"] == "get_case"
    assert "cache_control" not in client.calls[1]["tools"][0]


def test_anthropic_malformed_tool_use(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            {
                "id": "msg_bad",
                "model": CONFIGURED_MODEL,
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "get_case", "input": {}}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        ]
    )
    provider = AnthropicProvider(settings, client=client)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate(
            "sys",
            [{"role": "user", "content": T03_CASE_ID}],
            tool_definitions(),
            settings,
        )
    assert exc_info.value.code == ErrorCode.MALFORMED_PROVIDER_RESPONSE


def test_anthropic_timeout(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            APITimeoutError(request=_http_request()),
            APITimeoutError(request=_http_request()),
        ]
    )
    provider = AnthropicProvider(settings, client=client, sleeper=lambda _: None)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert exc_info.value.code == ErrorCode.PROVIDER_TIMEOUT
    assert len(client.calls) == 2


def test_anthropic_rate_limit_retry_then_success(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    slept: list[float] = []
    client = FakeMessagesClient(
        [
            RateLimitError(
                "rate limited",
                response=_http_response(429, {"retry-after": "1"}),
                body={"type": "error", "error": {"type": "rate_limit_error"}},
            ),
            _sdk_message(text="ok"),
        ]
    )
    provider = AnthropicProvider(settings, client=client, sleeper=slept.append)
    turn = provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert turn.public_text == "ok"
    assert turn.usage.model_attempts == 2
    assert slept == [1.0]


def test_anthropic_long_retry_after_does_not_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    slept: list[float] = []
    client = FakeMessagesClient(
        [
            RateLimitError(
                "rate limited",
                response=_http_response(429, {"retry-after": "120"}),
                body={"type": "error", "error": {"type": "rate_limit_error"}},
            )
        ]
    )
    provider = AnthropicProvider(settings, client=client, sleeper=slept.append)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate(
            "sys",
            [{"role": "user", "content": "x"}],
            [],
            settings,
            remaining_seconds=30,
        )
    assert exc_info.value.code == ErrorCode.PROVIDER_RATE_LIMIT
    assert "Retry-After" in exc_info.value.message
    assert slept == []
    assert len(client.calls) == 1


def test_anthropic_auth_error_is_not_retried(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            AuthenticationError(
                "bad key",
                response=_http_response(401),
                body={"type": "error", "error": {"type": "authentication_error"}},
            )
        ]
    )
    provider = AnthropicProvider(settings, client=client, sleeper=lambda _: None)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert exc_info.value.code == ErrorCode.PROVIDER_AUTH
    assert len(client.calls) == 1


def test_missing_credentials_does_not_call_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ANTHROPIC_API_KEY="")
    client = FakeMessagesClient([_sdk_message(text="should not run")])
    provider = AnthropicProvider(settings, client=client)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert exc_info.value.code == ErrorCode.MISSING_CREDENTIALS
    assert client.calls == []
    assert "secret" not in exc_info.value.message.lower()


def test_live_disabled_does_not_call_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_ENABLE_LIVE="false")
    client = FakeMessagesClient([_sdk_message(text="should not run")])
    provider = AnthropicProvider(settings, client=client)
    with pytest.raises(ProviderError) as exc_info:
        provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert exc_info.value.code == ErrorCode.LIVE_DISABLED
    assert client.calls == []


def test_timeout_is_capped_to_remaining_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient([_sdk_message(text="ok")])
    provider = AnthropicProvider(settings, client=client)
    provider.generate(
        "sys",
        [{"role": "user", "content": "x"}],
        [],
        settings,
        remaining_seconds=9,
    )
    assert client.calls[0]["timeout"] == pytest.approx(9, abs=0.05)
    assert client.calls[0]["timeout"] < settings.request_timeout_seconds


def test_server_error_retries_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            InternalServerError(
                "boom",
                response=_http_response(500),
                body={"type": "error", "error": {"type": "api_error"}},
            ),
            _sdk_message(text="recovered"),
        ]
    )
    provider = AnthropicProvider(settings, client=client, sleeper=lambda _: None)
    turn = provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert turn.public_text == "recovered"
    assert turn.usage.model_attempts == 2
    assert len(client.calls) == 2


def test_cost_uses_configured_rates_only(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PRECEDENT_INPUT_USD_PER_MILLION="3",
        PRECEDENT_OUTPUT_USD_PER_MILLION="15",
    )
    client = FakeMessagesClient(
        [_sdk_message(text="priced", input_tokens=1_000_000, output_tokens=1_000_000)]
    )
    provider = AnthropicProvider(settings, client=client)
    turn = provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert turn.usage.estimated_cost_usd == Decimal("18")
    assert turn.usage.price_rate_provenance == "configured_usd_per_million"


def test_live_smoke_requires_forced_ping_tool(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeMessagesClient(
        [
            _sdk_message(
                tools=[{"id": "toolu_ping", "name": PREFLIGHT_TOOL_NAME, "input": {}}],
                stop_reason="tool_use",
                model="claude-test-model-actual",
            )
        ]
    )
    result = live_tool_smoke_test(settings, provider=AnthropicProvider(settings, client=client))
    assert result.model == "claude-test-model-actual"
    assert result.tool_name == PREFLIGHT_TOOL_NAME
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": PREFLIGHT_TOOL_NAME}
    assert client.calls[0]["max_tokens"] == 128
    assert client.calls[0]["tools"][0]["name"] == PREFLIGHT_TOOL_NAME


def test_sdk_client_disables_automatic_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, Any] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.messages = FakeMessagesClient([_sdk_message(text="ok")])

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)
    provider = AnthropicProvider(settings)
    turn = provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert turn.public_text == "ok"
    assert captured["max_retries"] == 0
    assert captured["api_key"] == "secret-test-key"
    assert "secret-test-key" not in repr(settings)


def test_scripted_timeout_error_is_explicit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_ENABLE_LIVE="false")
    provider = ScriptedProvider(
        [ProviderError(ErrorCode.PROVIDER_TIMEOUT, "Provider request timed out.")]
    )
    with pytest.raises(ProviderError) as exc_info:
        provider.generate("sys", [{"role": "user", "content": "x"}], [], settings)
    assert exc_info.value.code == ErrorCode.PROVIDER_TIMEOUT


def test_short_retry_constant_is_bounded() -> None:
    assert SHORT_RETRY_DELAY_SECONDS == 0.5
