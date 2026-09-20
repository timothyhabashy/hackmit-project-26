"""Bounded multi-turn investigation loop.

The host owns validation, persistence, budgets, and termination. The provider
chooses tool calls; this module does not hardcode fixture IDs or action order.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from precedent.config import Settings, live_readiness_code
from precedent.db import (
    get_application_by_payment,
    get_case,
    get_run,
    get_workspace,
    list_run_events,
)
from precedent.models import (
    AgentOutcome,
    ApplicationRecord,
    ApplicationStatus,
    BudgetLimits,
    CaseState,
    ErrorCode,
    ErrorDetail,
    ExecutionMode,
    MemoryEligibilityNote,
    MemoryMode,
    MemorySnapshot,
    ProviderTurn,
    ReviewRequest,
    RunContext,
    RunEventKind,
    RunResult,
    RunState,
    ToolCall,
    ToolError,
    ToolResult,
    Usage,
    canonical_json,
    utc_now_iso,
)
from precedent.providers import (
    MIN_REQUEST_SECONDS,
    Provider,
    ProviderError,
    ScriptedProvider,
    select_provider,
    tool_result_user_message,
)
from precedent.tools import (
    TERMINAL_TOOLS,
    TRACE_STRING_MAX,
    dispatch_tool,
    empty_memory_snapshot,
    initial_user_message,
    investigator_prompt_text,
    record_run_event,
    record_run_finished,
    start_run,
    tool_definitions,
)

REMINDER_TEXT = "Finish through a terminal tool. Free text alone does not complete the case."
MAX_IDENTICAL_INVALID_TOOLS = 3
PUBLIC_TRACE_MAX = TRACE_STRING_MAX
_READINESS_MESSAGES = {
    ErrorCode.LIVE_DISABLED: "Live mode is disabled (PRECEDENT_ENABLE_LIVE is false).",
    ErrorCode.MISSING_CREDENTIALS: "ANTHROPIC_API_KEY is absent.",
    ErrorCode.MODEL_UNAVAILABLE: "PRECEDENT_MODEL is unset.",
}
_NORMAL_EMPTY_STOPS = frozenset({None, "end_turn", "stop_sequence"})
_TRUNCATED_STOPS = frozenset({"max_tokens"})


class InvestigationError(Exception):
    """Input or configuration failure before a run starts. Safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def investigate_case(
    connection: sqlite3.Connection,
    settings: Settings,
    workspace_id: str,
    case_id: str,
    *,
    mode: ExecutionMode,
    memory_mode: MemoryMode,
    provider: Provider | None = None,
    run_id: str | None = None,
    memory_snapshot: MemorySnapshot | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    perf_counter: Callable[[], float] = time.perf_counter,
) -> RunResult:
    """Investigate one case until a terminal outcome, budget limit, or error."""
    if mode is ExecutionMode.HUMAN:
        raise InvestigationError("INVALID_INPUT", "HUMAN mode is not a model investigation.")
    selected = _resolve_provider(settings, mode, provider)
    workspace = get_workspace(connection, workspace_id)
    if workspace is None:
        raise InvestigationError("INVALID_INPUT", f"workspace {workspace_id} was not found")
    _reconcile_interrupted(connection, workspace_id, case_id)
    case = get_case(connection, workspace_id, case_id)
    if case is None:
        raise InvestigationError("INVALID_INPUT", f"case {case_id} was not found")
    if case.state is CaseState.RESOLVED:
        raise InvestigationError(
            "INVALID_INPUT",
            "Resolved cases cannot be rerun against the same mutable ledger.",
        )
    resolved_run_id = run_id or f"RUN-{uuid.uuid4().hex}"
    if memory_mode not in (MemoryMode.ON, MemoryMode.OFF):
        raise InvestigationError("INVALID_INPUT", f"unsupported memory mode {memory_mode!r}")
    eligibility: list[MemoryEligibilityNote] = []
    if memory_snapshot is not None:
        # Evaluator-only injection. Ordinary callers omit this and use memberships.
        snapshot = memory_snapshot
    elif memory_mode is MemoryMode.ON:
        from precedent.learning import load_attached_memory_snapshot

        snapshot, eligibility = load_attached_memory_snapshot(connection, workspace, settings)
    else:
        snapshot = empty_memory_snapshot()
    context = RunContext(
        workspace_id=workspace_id,
        case_id=case_id,
        run_id=resolved_run_id,
        company_id=workspace.company_id,
        policy_id=workspace.policy.policy_id,
        policy_hash=workspace.policy_hash,
        dataset_hash=workspace.dataset_hash,
        initial_ledger_revision=workspace.ledger_revision,
        execution_mode=mode,
        actor="investigator",
        opened_documents=[],
        retrieved_precedent_version_ids=[],
        memory_snapshot=snapshot,
        budgets=_budgets_from_settings(settings),
        memory_mode=memory_mode,
        memory_eligibility=list(eligibility),
    )
    start_run(connection, context)
    _bind_provider(connection, context.run_id, settings, mode)
    _set_case_running(connection, workspace_id, case_id, context.run_id)
    started_mono = monotonic()
    started_perf = perf_counter()
    tracker = _UsageTracker()
    try:
        return _run_loop(
            connection,
            settings,
            context,
            selected,
            tracker=tracker,
            started_mono=started_mono,
            started_perf=started_perf,
            monotonic=monotonic,
            perf_counter=perf_counter,
        )
    except ProviderError as exc:
        committed = _committed_application(connection, context)
        if committed is not None:
            return _result_from_committed(
                connection,
                context,
                committed,
                tracker,
                elapsed_ms=_elapsed_ms(started_perf, perf_counter),
            )
        return _fail_run(
            connection,
            context,
            tracker,
            exc.code,
            exc.message,
            elapsed_ms=_elapsed_ms(started_perf, perf_counter),
        )
    except Exception as exc:
        committed = _committed_application(connection, context)
        if committed is not None:
            return _result_from_committed(
                connection,
                context,
                committed,
                tracker,
                elapsed_ms=_elapsed_ms(started_perf, perf_counter),
            )
        return _fail_run(
            connection,
            context,
            tracker,
            ErrorCode.INTERNAL_ERROR,
            f"Unexpected investigation error ({type(exc).__name__}).",
            elapsed_ms=_elapsed_ms(started_perf, perf_counter),
        )


def _run_loop(
    connection: sqlite3.Connection,
    settings: Settings,
    context: RunContext,
    provider: Provider,
    *,
    tracker: _UsageTracker,
    started_mono: float,
    started_perf: float,
    monotonic: Callable[[], float],
    perf_counter: Callable[[], float],
) -> RunResult:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message(context.case_id)}
    ]
    definitions = tool_definitions()
    system_prompt = investigator_prompt_text()
    reminded = False
    last_public: str | None = None
    last_proposal_id: str | None = None
    last_application_id: str | None = None
    last_review: ReviewRequest | None = None
    source_ids: list[str] = []
    identical_invalid = 0
    last_invalid_signature: str | None = None
    terminal_outcome: AgentOutcome | None = None

    while terminal_outcome is None:
        remaining = _remaining_seconds(context.budgets, started_mono, monotonic)
        budget_error = _budget_error(context.budgets, tracker, remaining)
        if budget_error is not None:
            return _fail_run(
                connection,
                context,
                tracker,
                budget_error,
                "Investigation budget was exhausted before a terminal action.",
                elapsed_ms=_elapsed_ms(started_perf, perf_counter),
                proposal_id=last_proposal_id,
                source_ids=source_ids,
            )
        turn = provider.generate(
            system_prompt,
            messages,
            definitions,
            settings,
            remaining_seconds=remaining,
        )
        tracker.add_model_turn(turn.usage)
        last_public = turn.public_text or last_public
        _record_model_response(connection, context.run_id, turn)
        assistant_message = _assistant_message(turn)
        messages.append(assistant_message)

        if not turn.tool_calls:
            empty_error = _empty_turn_error(turn.stop_reason, reminded)
            if empty_error is None:
                messages.append({"role": "user", "content": REMINDER_TEXT})
                reminded = True
                continue
            return _fail_run(
                connection,
                context,
                tracker,
                empty_error,
                _empty_turn_message(empty_error),
                elapsed_ms=_elapsed_ms(started_perf, perf_counter),
                proposal_id=last_proposal_id,
                source_ids=source_ids,
            )

        results: list[tuple[str, ToolResult]] = []
        reached_terminal = False
        for call in turn.tool_calls:
            remaining = _remaining_seconds(context.budgets, started_mono, monotonic)
            if reached_terminal:
                skipped = _skipped_result("TERMINAL_REACHED", call)
                _record_skipped(connection, context.run_id, call, "TERMINAL_REACHED")
                results.append((call.call_id, skipped))
                continue
            if tracker.tool_calls >= context.budgets.max_tool_calls or remaining < 0:
                skipped = _skipped_result(ErrorCode.BUDGET_EXHAUSTED.value, call)
                _record_skipped(connection, context.run_id, call, ErrorCode.BUDGET_EXHAUSTED.value)
                results.append((call.call_id, skipped))
                messages.append(tool_result_user_message(results))
                return _fail_run(
                    connection,
                    context,
                    tracker,
                    ErrorCode.BUDGET_EXHAUSTED,
                    "Investigation budget was exhausted before a terminal action.",
                    elapsed_ms=_elapsed_ms(started_perf, perf_counter),
                    proposal_id=last_proposal_id,
                    source_ids=source_ids,
                )
            result = dispatch_tool(
                connection,
                context,
                call.name,
                call.arguments,
                call_id=call.call_id,
            )
            tracker.tool_calls += 1
            results.append((call.call_id, result))
            _accumulate_sources(source_ids, result.source_ids)
            if result.ok and call.name == "validate_resolution" and isinstance(result.data, dict):
                proposal = result.data.get("proposal_id")
                if isinstance(proposal, str) and proposal:
                    last_proposal_id = proposal
            signature = _invalid_signature(call, result)
            if signature is None:
                identical_invalid = 0
                last_invalid_signature = None
            else:
                if signature == last_invalid_signature:
                    identical_invalid += 1
                else:
                    identical_invalid = 1
                    last_invalid_signature = signature
                if identical_invalid >= MAX_IDENTICAL_INVALID_TOOLS:
                    messages.append(tool_result_user_message(results))
                    code = ErrorCode.INVALID_TOOL_ARGUMENTS
                    error_code = result.error.code if result.error is not None else ""
                    if error_code == ErrorCode.UNKNOWN_TOOL.value:
                        code = ErrorCode.UNKNOWN_TOOL
                    return _fail_run(
                        connection,
                        context,
                        tracker,
                        code,
                        "Repeated identical invalid tool requests were stopped.",
                        elapsed_ms=_elapsed_ms(started_perf, perf_counter),
                        proposal_id=last_proposal_id,
                        source_ids=source_ids,
                    )
            terminal = _terminal_from_result(call.name, result)
            if terminal is not None:
                reached_terminal = True
                terminal_outcome = terminal.outcome
                last_application_id = terminal.application_id or last_application_id
                last_proposal_id = terminal.proposal_id or last_proposal_id
                last_review = terminal.review or last_review

        messages.append(tool_result_user_message(results))

    elapsed_ms = _elapsed_ms(started_perf, perf_counter)
    usage = tracker.finish(elapsed_ms)
    _persist_usage(connection, context.run_id, usage)
    record_run_finished(connection, context.run_id, terminal_outcome)
    summary = _success_summary(
        terminal_outcome,
        proposal_id=last_proposal_id,
        application_id=last_application_id,
        review=last_review,
        source_ids=source_ids,
        public_text=last_public,
    )
    return RunResult(
        run_id=context.run_id,
        workspace_id=context.workspace_id,
        case_id=context.case_id,
        execution_mode=context.execution_mode,
        terminal_outcome=terminal_outcome,
        application_id=last_application_id,
        proposal_id=last_proposal_id,
        review=last_review,
        error=None,
        summary=summary,
        usage=usage,
        trace_reference=f"run:{context.run_id}",
    )


class _UsageTracker:
    def __init__(self) -> None:
        self.model_attempts = 0
        self.tool_calls = 0
        self._input: list[int | None] = []
        self._output: list[int | None] = []
        self._costs: list[Decimal | None] = []
        self._provenance: list[str | None] = []

    def add_model_turn(self, usage: Usage) -> None:
        self.model_attempts += max(1, usage.model_attempts)
        self._input.append(usage.input_tokens)
        self._output.append(usage.output_tokens)
        self._costs.append(usage.estimated_cost_usd)
        self._provenance.append(usage.price_rate_provenance)

    def finish(self, elapsed_ms: int) -> Usage:
        provenances = {item for item in self._provenance if item is not None}
        provenance = next(iter(provenances)) if len(provenances) == 1 else None
        return Usage(
            model_attempts=self.model_attempts,
            tool_calls=self.tool_calls,
            input_tokens=_sum_optional(self._input),
            output_tokens=_sum_optional(self._output),
            elapsed_ms=elapsed_ms,
            estimated_cost_usd=_sum_costs(self._costs),
            price_rate_provenance=provenance,
        )


def _resolve_provider(
    settings: Settings, mode: ExecutionMode, provider: Provider | None
) -> Provider:
    if mode is ExecutionMode.LIVE:
        if isinstance(provider, ScriptedProvider):
            raise InvestigationError(
                ErrorCode.INTERNAL_ERROR.value,
                "ScriptedProvider is never selected as a live-mode fallback.",
            )
        reason = live_readiness_code(settings)
        if reason is not None:
            code = ErrorCode(reason)
            raise InvestigationError(code.value, _READINESS_MESSAGES.get(code, reason))
        try:
            return select_provider(settings, scripted=None)
        except ProviderError as exc:
            raise InvestigationError(exc.code.value, exc.message) from exc
    scripted = provider if isinstance(provider, ScriptedProvider) else None
    if scripted is None:
        if provider is not None:
            if settings.enable_live:
                raise InvestigationError(
                    ErrorCode.INTERNAL_ERROR.value,
                    "ScriptedProvider is never selected as a live-mode fallback.",
                )
            return provider
        raise InvestigationError(
            ErrorCode.LIVE_DISABLED.value,
            "TEST investigation requires an explicit ScriptedProvider.",
        )
    try:
        return select_provider(settings, scripted=scripted)
    except ProviderError as exc:
        raise InvestigationError(exc.code.value, exc.message) from exc


def _budgets_from_settings(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        max_model_calls=settings.max_model_calls,
        max_tool_calls=settings.max_tool_calls,
        max_case_seconds=settings.max_case_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_output_tokens=settings.max_output_tokens,
    )


def _bind_provider(
    connection: sqlite3.Connection,
    run_id: str,
    settings: Settings,
    mode: ExecutionMode,
) -> None:
    label = "anthropic" if mode is ExecutionMode.LIVE else "scripted"
    connection.execute(
        "UPDATE runs SET provider = ?, model = ? WHERE run_id = ?",
        (label, settings.model, run_id),
    )


def _set_case_running(
    connection: sqlite3.Connection, workspace_id: str, case_id: str, run_id: str
) -> None:
    connection.execute(
        """
        UPDATE cases
        SET state = ?, current_run_id = ?, latest_run_id = ?
        WHERE workspace_id = ? AND case_id = ?
        """,
        (CaseState.RUNNING.value, run_id, run_id, workspace_id, case_id),
    )


def _reconcile_interrupted(connection: sqlite3.Connection, workspace_id: str, case_id: str) -> None:
    case = get_case(connection, workspace_id, case_id)
    if case is None or case.state is not CaseState.RUNNING:
        return
    finished_at = utc_now_iso()
    run_id = case.current_run_id
    if run_id:
        run = get_run(connection, run_id)
        if run is not None and run.state is RunState.RUNNING:
            connection.execute(
                """
                UPDATE runs
                SET state = ?, finished_at = ?, terminal_result = ?
                WHERE run_id = ?
                """,
                (
                    RunState.INTERRUPTED.value,
                    finished_at,
                    AgentOutcome.ERROR.value,
                    run_id,
                ),
            )
            record_run_event(
                connection,
                run_id,
                RunEventKind.RUN_FAILED,
                {
                    "error_code": ErrorCode.INTERRUPTED.value,
                    "message": "The previous investigation was interrupted.",
                },
                created_at=finished_at,
            )
            record_run_finished(connection, run_id, AgentOutcome.ERROR)
    connection.execute(
        """
        UPDATE cases
        SET state = ?, current_run_id = NULL
        WHERE workspace_id = ? AND case_id = ?
        """,
        (CaseState.ERROR.value, workspace_id, case_id),
    )


def _remaining_seconds(
    budgets: BudgetLimits, started_mono: float, monotonic: Callable[[], float]
) -> float:
    return float(budgets.max_case_seconds) - (monotonic() - started_mono)


def _budget_error(
    budgets: BudgetLimits, tracker: _UsageTracker, remaining: float
) -> ErrorCode | None:
    if remaining < MIN_REQUEST_SECONDS:
        return ErrorCode.BUDGET_EXHAUSTED
    if tracker.model_attempts >= budgets.max_model_calls:
        return ErrorCode.BUDGET_EXHAUSTED
    if tracker.tool_calls >= budgets.max_tool_calls:
        return ErrorCode.BUDGET_EXHAUSTED
    return None


def _record_model_response(connection: sqlite3.Connection, run_id: str, turn: ProviderTurn) -> None:
    public = turn.public_text
    if isinstance(public, str) and len(public) > PUBLIC_TRACE_MAX:
        public = public[:PUBLIC_TRACE_MAX] + "..."
    record_run_event(
        connection,
        run_id,
        RunEventKind.MODEL_RESPONSE,
        {
            "provider_request_id": turn.provider_request_id,
            "public_text": public,
            "stop_reason": turn.stop_reason,
            "tool_names": [call.name for call in turn.tool_calls],
            "usage": {
                "elapsed_ms": turn.usage.elapsed_ms,
                "input_tokens": turn.usage.input_tokens,
                "model_attempts": turn.usage.model_attempts,
                "output_tokens": turn.usage.output_tokens,
            },
        },
    )


def _assistant_message(turn: ProviderTurn) -> dict[str, Any]:
    message = turn.provider_assistant_message
    if isinstance(message, dict) and message.get("role") == "assistant":
        return dict(message)
    content: list[dict[str, Any]] = []
    if turn.public_text:
        content.append({"type": "text", "text": turn.public_text})
    for call in turn.tool_calls:
        content.append(
            {
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return {"role": "assistant", "content": content}


def _empty_turn_error(stop_reason: str | None, reminded: bool) -> ErrorCode | None:
    if stop_reason in _TRUNCATED_STOPS:
        return ErrorCode.MALFORMED_PROVIDER_RESPONSE
    if stop_reason in _NORMAL_EMPTY_STOPS:
        if reminded:
            return ErrorCode.NO_TERMINAL_ACTION
        return None
    if stop_reason == "tool_use":
        return ErrorCode.MALFORMED_PROVIDER_RESPONSE
    if reminded:
        return ErrorCode.NO_TERMINAL_ACTION
    return ErrorCode.MALFORMED_PROVIDER_RESPONSE


def _empty_turn_message(code: ErrorCode) -> str:
    if code is ErrorCode.NO_TERMINAL_ACTION:
        return "The provider did not finish through a terminal tool."
    if code is ErrorCode.MALFORMED_PROVIDER_RESPONSE:
        return "The provider response was truncated or missing tool calls."
    return "The investigation ended without a terminal action."


def _skipped_result(code: str, call: ToolCall) -> ToolResult:
    if code == "TERMINAL_REACHED":
        message = f"{call.name} was skipped because a terminal action already completed."
    else:
        message = f"{call.name} was skipped because the investigation budget was exhausted."
    return ToolResult(
        ok=False,
        data=None,
        error=ToolError(code=code, message=message),
        source_ids=[],
    )


def _record_skipped(
    connection: sqlite3.Connection, run_id: str, call: ToolCall, reason: str
) -> None:
    record_run_event(
        connection,
        run_id,
        RunEventKind.TOOL_FAILED,
        {
            "arguments": call.arguments,
            "call_id": call.call_id,
            "error_code": reason,
            "status": "skipped",
            "summary": f"{call.name} skipped ({reason})",
            "tool": call.name,
        },
    )


def _accumulate_sources(collected: list[str], incoming: list[str]) -> None:
    seen = set(collected)
    for item in incoming:
        if item not in seen:
            collected.append(item)
            seen.add(item)


def _invalid_signature(call: ToolCall, result: ToolResult) -> str | None:
    if result.ok or result.error is None:
        return None
    return canonical_json(
        {"arguments": call.arguments, "code": result.error.code, "name": call.name}
    )


class _TerminalCapture:
    def __init__(
        self,
        outcome: AgentOutcome,
        *,
        application_id: str | None = None,
        proposal_id: str | None = None,
        review: ReviewRequest | None = None,
    ) -> None:
        self.outcome = outcome
        self.application_id = application_id
        self.proposal_id = proposal_id
        self.review = review


def _terminal_from_result(name: str, result: ToolResult) -> _TerminalCapture | None:
    if not result.ok or name not in TERMINAL_TOOLS:
        return None
    if name == "submit_resolution":
        if not isinstance(result.data, dict):
            return None
        status = result.data.get("status")
        if status not in {ApplicationStatus.APPLIED.value, ApplicationStatus.REPLAYED.value}:
            return None
        application_id = result.data.get("application_id")
        proposal_id = result.data.get("proposal_id")
        return _TerminalCapture(
            AgentOutcome.RESOLVED,
            application_id=application_id if isinstance(application_id, str) else None,
            proposal_id=proposal_id if isinstance(proposal_id, str) else None,
        )
    if name == "request_review":
        review = None
        if isinstance(result.data, dict):
            try:
                review = ReviewRequest.model_validate(result.data)
            except Exception:
                review = None
        return _TerminalCapture(AgentOutcome.REVIEW, review=review)
    return None


def _fail_run(
    connection: sqlite3.Connection,
    context: RunContext,
    tracker: _UsageTracker,
    code: ErrorCode,
    message: str,
    *,
    elapsed_ms: int,
    proposal_id: str | None = None,
    source_ids: list[str] | None = None,
) -> RunResult:
    committed = _committed_application(connection, context)
    if committed is not None:
        return _result_from_committed(
            connection, context, committed, tracker, elapsed_ms=elapsed_ms
        )
    usage = tracker.finish(elapsed_ms)
    finished_at = utc_now_iso()
    record_run_event(
        connection,
        context.run_id,
        RunEventKind.RUN_FAILED,
        {"error_code": code.value, "message": message[:PUBLIC_TRACE_MAX]},
        created_at=finished_at,
    )
    record_run_finished(connection, context.run_id, AgentOutcome.ERROR)
    connection.execute(
        """
        UPDATE runs
        SET state = ?, finished_at = ?, terminal_result = ?, usage_json = ?
        WHERE run_id = ?
        """,
        (
            RunState.FAILED.value,
            finished_at,
            AgentOutcome.ERROR.value,
            canonical_json(usage.model_dump(mode="json")),
            context.run_id,
        ),
    )
    case = get_case(connection, context.workspace_id, context.case_id)
    if case is not None and case.state not in {CaseState.RESOLVED, CaseState.NEEDS_REVIEW}:
        connection.execute(
            """
            UPDATE cases
            SET state = ?, current_run_id = NULL, latest_run_id = ?
            WHERE workspace_id = ? AND case_id = ?
            """,
            (
                CaseState.ERROR.value,
                context.run_id,
                context.workspace_id,
                context.case_id,
            ),
        )
    cited = ", ".join((source_ids or [])[:8])
    summary = f"Run failed ({code.value}): {message}"
    if cited:
        summary = f"{summary} Sources: {cited}."
    return RunResult(
        run_id=context.run_id,
        workspace_id=context.workspace_id,
        case_id=context.case_id,
        execution_mode=context.execution_mode,
        terminal_outcome=AgentOutcome.ERROR,
        application_id=None,
        proposal_id=proposal_id,
        review=None,
        error=ErrorDetail(code=code, message=message[:1000]),
        summary=summary[:2000],
        usage=usage,
        trace_reference=f"run:{context.run_id}",
    )


def _persist_usage(connection: sqlite3.Connection, run_id: str, usage: Usage) -> None:
    connection.execute(
        "UPDATE runs SET usage_json = ? WHERE run_id = ?",
        (canonical_json(usage.model_dump(mode="json")), run_id),
    )


def _committed_application(
    connection: sqlite3.Connection, context: RunContext
) -> ApplicationRecord | None:
    case = get_case(connection, context.workspace_id, context.case_id)
    if case is None or case.state is not CaseState.RESOLVED:
        return None
    application = get_application_by_payment(connection, context.workspace_id, case.payment_id)
    if application is None or application.seeded:
        return None
    return application


def _result_from_committed(
    connection: sqlite3.Connection,
    context: RunContext,
    application: ApplicationRecord,
    tracker: _UsageTracker,
    *,
    elapsed_ms: int,
) -> RunResult:
    usage = tracker.finish(elapsed_ms)
    _persist_usage(connection, context.run_id, usage)
    record_run_finished(connection, context.run_id, AgentOutcome.RESOLVED)
    events = list_run_events(connection, context.run_id)
    cited: list[str] = []
    for event in events:
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        for item in payload.get("source_ids", []):
            if isinstance(item, str) and item not in cited:
                cited.append(item)
    return RunResult(
        run_id=context.run_id,
        workspace_id=context.workspace_id,
        case_id=context.case_id,
        execution_mode=context.execution_mode,
        terminal_outcome=AgentOutcome.RESOLVED,
        application_id=application.application_id,
        proposal_id=application.proposal_id,
        review=None,
        error=None,
        summary=_success_summary(
            AgentOutcome.RESOLVED,
            proposal_id=application.proposal_id,
            application_id=application.application_id,
            review=None,
            source_ids=cited,
            public_text=None,
        ),
        usage=usage,
        trace_reference=f"run:{context.run_id}",
    )


def _success_summary(
    outcome: AgentOutcome,
    *,
    proposal_id: str | None,
    application_id: str | None,
    review: ReviewRequest | None,
    source_ids: list[str],
    public_text: str | None,
) -> str:
    cited = ", ".join(source_ids[:8])
    brief = (public_text or "").strip()
    if brief:
        brief = brief.splitlines()[0].strip()[:240]
    if outcome is AgentOutcome.RESOLVED:
        text = brief or f"Applied proposal {proposal_id or 'unknown'}."
        if application_id and application_id not in text:
            text = f"{text} Application {application_id}."
    elif outcome is AgentOutcome.REVIEW and review is not None:
        text = brief or f"Requested review ({review.reason_code.value}): {review.message}"
    else:
        text = brief or f"Investigation finished as {outcome.value}."
    if cited:
        text = f"{text} Sources: {cited}."
    return text[:2000]


def _elapsed_ms(started_perf: float, perf_counter: Callable[[], float]) -> int:
    return max(0, int(round((perf_counter() - started_perf) * 1000)))


def _sum_optional(values: list[int | None]) -> int | None:
    if not values or any(item is None for item in values):
        return None
    return sum(values)


def _sum_costs(values: list[Decimal | None]) -> Decimal | None:
    if not values or any(item is None for item in values):
        return None
    total = Decimal("0")
    for item in values:
        assert item is not None
        total += item
    return total


__all__ = [
    "InvestigationError",
    "MAX_IDENTICAL_INVALID_TOOLS",
    "REMINDER_TEXT",
    "investigate_case",
]
