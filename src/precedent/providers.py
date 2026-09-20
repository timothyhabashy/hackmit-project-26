"""One Anthropic provider adapter and an explicit scripted test double.

Live mode never falls back to ``ScriptedProvider``. Recorded-run viewing is not
implemented here and makes no provider calls. Session 09 owns the investigation
loop; this module only performs one generate turn and formats tool results.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from precedent.config import Settings, live_readiness_code
from precedent.models import (
    ErrorCode,
    ProviderTurn,
    ToolCall,
    ToolResult,
    Usage,
    canonical_json,
)

SHORT_RETRY_DELAY_SECONDS = 0.5
MIN_REQUEST_SECONDS = 0.1
PUBLIC_TEXT_MAX = 8000
SMOKE_MAX_OUTPUT_TOKENS = 128
PREFLIGHT_TOOL_NAME = "preflight_ping"
PREFLIGHT_TOOL: dict[str, Any] = {
    "name": PREFLIGHT_TOOL_NAME,
    "description": "Acknowledge the live preflight probe. Call with empty arguments.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
PREFLIGHT_SYSTEM = (
    "You are a connectivity probe for Precedent. Call preflight_ping once "
    "with empty arguments. Do not invent other tools."
)
_READINESS_MESSAGES = {
    ErrorCode.LIVE_DISABLED: "Live mode is disabled (PRECEDENT_ENABLE_LIVE is false).",
    ErrorCode.MISSING_CREDENTIALS: "ANTHROPIC_API_KEY is absent.",
    ErrorCode.MODEL_UNAVAILABLE: "PRECEDENT_MODEL is unset.",
}
_TRANSIENT_CODES = frozenset(
    {
        ErrorCode.PROVIDER_RATE_LIMIT,
        ErrorCode.PROVIDER_TIMEOUT,
        ErrorCode.PROVIDER_UNAVAILABLE,
    }
)


class ProviderError(Exception):
    """Provider/runtime failure. Safe to print; never includes secrets."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class Provider(Protocol):
    def generate(
        self,
        system_prompt: str,
        messages: Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]],
        settings: Settings,
        *,
        remaining_seconds: float | None = None,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> ProviderTurn: ...


@dataclass(frozen=True)
class LiveSmokeResult:
    model: str
    tool_name: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None


class ScriptedProvider:
    """Deterministic test double. Never selected as a live-mode fallback."""

    def __init__(self, script: Sequence[ProviderTurn | ProviderError]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]],
        settings: Settings,
        *,
        remaining_seconds: float | None = None,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> ProviderTurn:
        self.requests.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "tool_definitions": list(tool_definitions),
                "model": settings.model,
                "remaining_seconds": remaining_seconds,
                "tool_choice": None if tool_choice is None else dict(tool_choice),
            }
        )
        if not self._script:
            raise ProviderError(
                ErrorCode.INTERNAL_ERROR,
                "ScriptedProvider has no remaining turns.",
            )
        item = self._script.pop(0)
        if isinstance(item, ProviderError):
            raise item
        return item


class AnthropicProvider:
    """Official Anthropic Messages/tool adapter. SDK automatic retries are off."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleeper = sleeper
        self.last_model: str | None = None

    def generate(
        self,
        system_prompt: str,
        messages: Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]],
        settings: Settings,
        *,
        remaining_seconds: float | None = None,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> ProviderTurn:
        _require_live_ready(settings)
        started = time.perf_counter()
        remaining = (
            float(settings.max_case_seconds)
            if remaining_seconds is None
            else float(remaining_seconds)
        )
        deadline = time.monotonic() + remaining
        attempts = 0
        last_error: ProviderError | None = None
        raw_message: Any = None
        while True:
            left = deadline - time.monotonic()
            if left < MIN_REQUEST_SECONDS:
                if last_error is not None:
                    raise last_error
                raise ProviderError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    "Remaining case time cannot fit a provider request.",
                )
            timeout = min(float(settings.request_timeout_seconds), left)
            attempts += 1
            try:
                raw_message = self._create_message(
                    system_prompt=system_prompt,
                    messages=messages,
                    tool_definitions=tool_definitions,
                    settings=settings,
                    timeout=timeout,
                    tool_choice=tool_choice,
                )
                break
            except ProviderError as exc:
                last_error = exc
                if attempts >= 2 or exc.code not in _TRANSIENT_CODES:
                    raise
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else SHORT_RETRY_DELAY_SECONDS
                )
                leftover = deadline - time.monotonic()
                if delay < 0 or leftover - delay < MIN_REQUEST_SECONDS:
                    raise ProviderError(
                        exc.code,
                        (
                            "Retry-After cannot fit the remaining case budget."
                            if exc.retry_after_seconds is not None
                            else exc.message
                        ),
                        retry_after_seconds=exc.retry_after_seconds,
                    ) from exc
                self._sleeper(delay)
        elapsed_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
        turn = normalize_provider_message(
            raw_message,
            settings=settings,
            model_attempts=attempts,
            elapsed_ms=elapsed_ms,
        )
        self.last_model = _message_model(raw_message, settings.model)
        return turn

    def _create_message(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]],
        settings: Settings,
        timeout: float,
        tool_choice: Mapping[str, Any] | None,
    ) -> Any:
        from anthropic import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            CredentialsError,
            InternalServerError,
            NotFoundError,
            OverloadedError,
            PermissionDeniedError,
            RateLimitError,
            RequestTooLargeError,
            ServiceUnavailableError,
            UnprocessableEntityError,
        )

        if settings.model is None:
            raise ProviderError(ErrorCode.MODEL_UNAVAILABLE, "PRECEDENT_MODEL is unset.")
        client = self._client if self._client is not None else self._build_client(settings)
        kwargs: dict[str, Any] = {
            "model": settings.model,
            "max_tokens": int(settings.max_output_tokens),
            "messages": [dict(message) for message in messages],
            "timeout": timeout,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        tools = _anthropic_tools(tool_definitions)
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = dict(tool_choice)
        try:
            return client.messages.create(**kwargs)
        except AuthenticationError as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_AUTH, "Provider rejected the credentials."
            ) from exc
        except PermissionDeniedError as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_AUTH,
                "Provider denied access to the configured model.",
            ) from exc
        except CredentialsError as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_AUTH, "Provider credentials could not be used."
            ) from exc
        except NotFoundError as exc:
            raise ProviderError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Configured model is not available to this account.",
            ) from exc
        except RateLimitError as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_RATE_LIMIT,
                "Provider rate-limited the request.",
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        except APITimeoutError as exc:
            raise ProviderError(ErrorCode.PROVIDER_TIMEOUT, "Provider request timed out.") from exc
        except (InternalServerError, ServiceUnavailableError, OverloadedError) as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "Provider server or overload error.",
            ) from exc
        except APIConnectionError as exc:
            raise ProviderError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "Network or connection error reaching the provider.",
            ) from exc
        except (BadRequestError, UnprocessableEntityError, RequestTooLargeError) as exc:
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider rejected the request as invalid.",
            ) from exc
        except APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and status >= 500:
                raise ProviderError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    f"Provider returned HTTP {status}.",
                ) from exc
            raise ProviderError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"Provider returned HTTP {status}."
                if status is not None
                else "Provider returned an error.",
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected provider error ({type(exc).__name__}).",
            ) from exc

    def _build_client(self, settings: Settings) -> Any:
        from anthropic import Anthropic

        if settings.anthropic_api_key is None:
            raise ProviderError(ErrorCode.MISSING_CREDENTIALS, "ANTHROPIC_API_KEY is absent.")
        return Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,
            timeout=settings.request_timeout_seconds,
        )


def select_provider(
    settings: Settings,
    *,
    scripted: ScriptedProvider | None = None,
) -> Provider:
    """Return the live Anthropic adapter or an explicit scripted test double.

    Live mode never selects ``ScriptedProvider``.
    """
    if settings.enable_live:
        if scripted is not None:
            raise ProviderError(
                ErrorCode.INTERNAL_ERROR,
                "ScriptedProvider is never selected as a live-mode fallback.",
            )
        return AnthropicProvider(settings)
    if scripted is not None:
        return scripted
    raise ProviderError(
        ErrorCode.LIVE_DISABLED,
        "Live mode is disabled and no scripted provider was supplied.",
    )


def live_anthropic_provider(settings: Settings) -> AnthropicProvider:
    _require_live_ready(settings)
    return AnthropicProvider(settings)


def live_tool_smoke_test(
    settings: Settings,
    *,
    provider: AnthropicProvider | None = None,
) -> LiveSmokeResult:
    adapter = provider if provider is not None else live_anthropic_provider(settings)
    smoke_tokens = min(int(settings.max_output_tokens), SMOKE_MAX_OUTPUT_TOKENS)
    smoke_settings = settings.model_copy(update={"max_output_tokens": smoke_tokens})
    remaining = float(min(settings.request_timeout_seconds, settings.max_case_seconds))
    turn = adapter.generate(
        PREFLIGHT_SYSTEM,
        [{"role": "user", "content": "Call the preflight_ping tool once with empty arguments."}],
        [PREFLIGHT_TOOL],
        smoke_settings,
        remaining_seconds=remaining,
        tool_choice={"type": "tool", "name": PREFLIGHT_TOOL_NAME},
    )
    names = [call.name for call in turn.tool_calls]
    if PREFLIGHT_TOOL_NAME not in names:
        raise ProviderError(
            ErrorCode.MALFORMED_PROVIDER_RESPONSE,
            "Live smoke test did not receive a preflight_ping tool call.",
        )
    model = adapter.last_model or settings.model
    if not model:
        raise ProviderError(ErrorCode.MODEL_UNAVAILABLE, "PRECEDENT_MODEL is unset.")
    return LiveSmokeResult(
        model=model,
        tool_name=PREFLIGHT_TOOL_NAME,
        stop_reason=turn.stop_reason,
        input_tokens=turn.usage.input_tokens,
        output_tokens=turn.usage.output_tokens,
    )


def tool_result_block(
    call_id: str,
    result: ToolResult | str,
    *,
    is_error: bool | None = None,
) -> dict[str, Any]:
    if isinstance(result, ToolResult):
        content = canonical_json(result.model_dump(mode="json"))
        error_flag = not result.ok if is_error is None else is_error
    else:
        content = result
        error_flag = bool(is_error)
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": content,
        "is_error": error_flag,
    }


def tool_result_user_message(results: Sequence[tuple[str, ToolResult]]) -> dict[str, Any]:
    """Format tool results for the next Anthropic user message.

    Tool-result blocks occupy the entire content array and keep original call IDs.
    """
    return {
        "role": "user",
        "content": [tool_result_block(call_id, result) for call_id, result in results],
    }


def normalize_provider_message(
    raw: Any,
    *,
    settings: Settings,
    model_attempts: int,
    elapsed_ms: int,
) -> ProviderTurn:
    payload = _message_as_dict(raw)
    content = payload.get("content")
    if not isinstance(content, list):
        raise ProviderError(
            ErrorCode.MALFORMED_PROVIDER_RESPONSE,
            "Provider response content is missing or not a list.",
        )
    public_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    retained: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider response contained a non-object content block.",
            )
        retained.append(dict(block))
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                public_parts.append(text)
            continue
        if block_type != "tool_use":
            continue
        call_id = block.get("id")
        name = block.get("name")
        arguments = _coerce_tool_arguments(block.get("input"))
        if not isinstance(call_id, str) or not call_id.strip():
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider tool_use block is missing a call id.",
            )
        if not isinstance(name, str) or not name.strip():
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider tool_use block is missing a tool name.",
            )
        if arguments is None:
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider tool_use block has non-object arguments.",
            )
        try:
            tool_calls.append(ToolCall(call_id=call_id, name=name, arguments=arguments))
        except Exception as exc:
            raise ProviderError(
                ErrorCode.MALFORMED_PROVIDER_RESPONSE,
                "Provider tool_use block could not be normalized.",
            ) from exc
    public_text = "".join(public_parts)
    if len(public_text) > PUBLIC_TEXT_MAX:
        public_text = public_text[:PUBLIC_TEXT_MAX]
    stop_reason = payload.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        stop_reason = str(stop_reason)
    if isinstance(stop_reason, str) and len(stop_reason) > 80:
        stop_reason = stop_reason[:80]
    request_id = payload.get("id")
    if request_id is not None and not isinstance(request_id, str):
        request_id = str(request_id)
    if isinstance(request_id, str) and len(request_id) > 200:
        request_id = request_id[:200]
    input_tokens, output_tokens = _usage_tokens(payload.get("usage"))
    cost, provenance = _estimate_cost(settings, input_tokens, output_tokens)
    return ProviderTurn(
        tool_calls=tool_calls,
        public_text=public_text or None,
        stop_reason=stop_reason,
        provider_assistant_message={"role": "assistant", "content": retained},
        usage=Usage(
            model_attempts=model_attempts,
            tool_calls=len(tool_calls),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=elapsed_ms,
            estimated_cost_usd=cost,
            price_rate_provenance=provenance,
        ),
        provider_request_id=request_id,
    )


def _require_live_ready(settings: Settings) -> None:
    reason = live_readiness_code(settings)
    if reason is None:
        return
    code = ErrorCode(reason)
    raise ProviderError(code, _READINESS_MESSAGES.get(code, reason))


def _anthropic_tools(definitions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for item in definitions:
        name = item.get("name")
        schema = item.get("input_schema")
        if not isinstance(name, str) or not name.strip() or not isinstance(schema, dict):
            raise ProviderError(
                ErrorCode.INTERNAL_ERROR,
                "Tool definitions must include name and object input_schema.",
            )
        tool: dict[str, Any] = {"name": name, "input_schema": schema}
        description = item.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        tools.append(tool)
    return tools


def _message_as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json", exclude_none=True)
        if isinstance(payload, dict):
            return payload
    raise ProviderError(
        ErrorCode.MALFORMED_PROVIDER_RESPONSE,
        "Provider response could not be read as a message object.",
    )


def _message_model(raw: Any, fallback: str | None) -> str | None:
    if isinstance(raw, Mapping):
        model = raw.get("model")
        return model if isinstance(model, str) and model else fallback
    model = getattr(raw, "model", None)
    return model if isinstance(model, str) and model else fallback


def _coerce_tool_arguments(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _usage_tokens(raw: Any) -> tuple[int | None, int | None]:
    if not isinstance(raw, Mapping):
        return None, None
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
    )


def _estimate_cost(
    settings: Settings,
    input_tokens: int | None,
    output_tokens: int | None,
) -> tuple[Decimal | None, str | None]:
    if (
        settings.input_usd_per_million is None
        or settings.output_usd_per_million is None
        or input_tokens is None
        or output_tokens is None
    ):
        return None, None
    million = Decimal("1000000")
    cost = (Decimal(input_tokens) * settings.input_usd_per_million / million) + (
        Decimal(output_tokens) * settings.output_usd_per_million / million
    )
    return cost, "configured_usd_per_million"


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


__all__ = [
    "AnthropicProvider",
    "LiveSmokeResult",
    "PREFLIGHT_TOOL",
    "PREFLIGHT_TOOL_NAME",
    "Provider",
    "ProviderError",
    "SHORT_RETRY_DELAY_SECONDS",
    "ScriptedProvider",
    "live_anthropic_provider",
    "live_tool_smoke_test",
    "normalize_provider_message",
    "select_provider",
    "tool_result_block",
    "tool_result_user_message",
]
