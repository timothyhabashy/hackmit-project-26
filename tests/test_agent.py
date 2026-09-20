from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from precedent.agent import (
    MAX_IDENTICAL_INVALID_TOOLS,
    InvestigationError,
    investigate_case,
)
from precedent.cli import main
from precedent.config import detect_repo_root, load_settings
from precedent.db import (
    get_case,
    get_invoice,
    get_payment,
    get_run,
    insert_case,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_run,
    insert_workspace,
    list_run_events,
    open_database,
)
from precedent.fixtures import (
    CUST_HARBOR,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    load_package,
)
from precedent.models import (
    SCHEMA_VERSION,
    AgentOutcome,
    BudgetLimits,
    CaseRecord,
    CaseState,
    CustomerRecord,
    ErrorCode,
    ExecutionMode,
    InvoiceRecord,
    MemoryMode,
    PaymentRecord,
    ProviderTurn,
    RunEventKind,
    RunRecord,
    RunState,
    ToolCall,
    Usage,
    WorkspaceRecord,
    hash_model,
)
from precedent.providers import ProviderError, ScriptedProvider
from precedent.services import investigate
from precedent.tools import empty_memory_snapshot, initial_user_message, investigator_prompt_text

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
WS = "WS-AGENT-1"
T03_CASE_ID = "CASE-1CFA9FEE848F"


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AdvancingProvider:
    def __init__(self, inner: ScriptedProvider, clock: ManualClock, step: float) -> None:
        self.inner = inner
        self.clock = clock
        self.step = step

    def generate(self, *args: Any, **kwargs: Any) -> ProviderTurn:
        turn = self.inner.generate(*args, **kwargs)
        self.clock.advance(self.step)
        return turn


def _settings(tmp_path: Path, **env: str):
    repo = detect_repo_root()
    merged = {
        "PRECEDENT_DATA_DIR": str(repo / "data"),
        "PRECEDENT_DB_PATH": str(tmp_path / "precedent.sqlite3"),
        "PRECEDENT_ENABLE_LIVE": "false",
    }
    merged.update(env)
    return load_settings(environ=merged, dotenv_path=tmp_path / "missing.env", repo_root=tmp_path)


def _open(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "precedent.sqlite3")


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


def _call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments={} if arguments is None else arguments)


def _t03_payload() -> dict[str, object]:
    return {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": "SINGLE_WITH_BANK_FEE",
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": "Harbor remittance DOC-R201 and fee notice DOC-F201 support INV-1042.",
        "precedent_ids_used": [],
    }


def _review_payload() -> dict[str, object]:
    return {
        "reason_code": "MISSING_FEE_NOTICE",
        "message": "PAY-201 is short of INV-1042 and no matching bank fee notice was opened.",
        "needed_information": "Obtain the bank fee notice for the current transfer.",
        "evidence_document_ids": [T03_REMIT_ID],
        "candidate_invoice_ids": [T03_INVOICE_ID],
    }


def _import_t03(connection: sqlite3.Connection, workspace_id: str = WS) -> None:
    package = load_package(detect_repo_root() / "data" / "source" / T03_CASE_ID)
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name="T03 agent",
            company_id=package.company.company_id,
            period_label="2026-01",
            period_start=package.company.period_start,
            period_end=package.company.period_end,
            dataset_hash=HASH_A,
            policy=package.policy,
            policy_hash=hash_model(package.policy),
            ledger_revision=0,
            schema_version=SCHEMA_VERSION,
            created_at=CREATED,
        ),
    )
    for customer in package.customers:
        insert_customer(
            connection,
            CustomerRecord(
                workspace_id=workspace_id,
                customer_id=customer.customer_id,
                legal_name=customer.legal_name,
                display_name=customer.display_name,
            ),
        )
    for invoice in package.invoices:
        insert_invoice(
            connection,
            InvoiceRecord(
                workspace_id=workspace_id,
                invoice_id=invoice.invoice_id,
                customer_id=invoice.customer_id,
                currency=invoice.currency,
                issued_date=invoice.issued_date,
                due_date=invoice.due_date,
                original_cents=invoice.original_cents,
                opening_outstanding_cents=invoice.opening_outstanding_cents,
                outstanding_cents=invoice.opening_outstanding_cents,
                status=invoice.status,
            ),
        )
    for payment in package.payments:
        insert_payment(
            connection,
            PaymentRecord(
                workspace_id=workspace_id,
                payment_id=payment.payment_id,
                bank_transaction_id=payment.bank_transaction_id,
                bank_account_id=payment.bank_account_id,
                posted_date=payment.posted_date,
                currency=payment.currency,
                amount_cents=payment.amount_cents,
                channel=payment.channel,
                payer_text=payment.payer_text,
                bank_reference=payment.bank_reference,
                applied=False,
            ),
        )
    for document in package.documents:
        insert_document(connection, document.to_persisted(workspace_id))
    insert_case(
        connection,
        CaseRecord(
            workspace_id=workspace_id,
            case_id=package.case_id,
            payment_id=package.payments[0].payment_id,
            state=CaseState.OPEN,
        ),
    )


def _resolve_script() -> list[ProviderTurn]:
    payload = _t03_payload()
    return [
        _turn(tool_calls=[_call("toolu_case", "get_case")]),
        _turn(
            tool_calls=[
                _call(
                    "toolu_search",
                    "search_documents",
                    {"kind": "REMITTANCE", "reference": "BR-201"},
                )
            ]
        ),
        _turn(
            tool_calls=[
                _call("toolu_remit", "read_document", {"document_id": T03_REMIT_ID}),
                _call("toolu_fee", "read_document", {"document_id": T03_FEE_ID}),
            ]
        ),
        _turn(tool_calls=[_call("toolu_validate", "validate_resolution", payload)]),
        _turn(
            text="Documented receiving-wire fee settles INV-1042.",
            tool_calls=[_call("toolu_submit", "submit_resolution", {"proposal_id": "PENDING"})],
        ),
    ]


class ProposalAwareProvider:
    """Scripted turns, rewriting submit_resolution to the stored proposal id."""

    def __init__(self, script: list[ProviderTurn | ProviderError]) -> None:
        self.inner = ScriptedProvider(script)
        self.requests: list[dict[str, Any]] = []

    def generate(self, *args: Any, **kwargs: Any) -> ProviderTurn:
        turn = self.inner.generate(*args, **kwargs)
        self.requests = self.inner.requests
        rewritten: list[ToolCall] = []
        content: list[dict[str, Any]] = []
        if turn.public_text:
            content.append({"type": "text", "text": turn.public_text})
        for call in turn.tool_calls:
            arguments = dict(call.arguments)
            if call.name == "submit_resolution" and arguments.get("proposal_id") == "PENDING":
                proposal_id = _proposal_id_from_messages(kwargs.get("messages") or args[1])
                arguments["proposal_id"] = proposal_id
            new_call = ToolCall(call_id=call.call_id, name=call.name, arguments=arguments)
            rewritten.append(new_call)
            content.append(
                {
                    "type": "tool_use",
                    "id": new_call.call_id,
                    "name": new_call.name,
                    "input": new_call.arguments,
                }
            )
        return turn.model_copy(
            update={
                "tool_calls": rewritten,
                "provider_assistant_message": {"role": "assistant", "content": content},
            }
        )


def _proposal_id_from_messages(messages: Any) -> str:
    for message in reversed(list(messages)):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                proposal_id = data.get("proposal_id")
                if isinstance(proposal_id, str) and proposal_id:
                    return proposal_id
    raise AssertionError("scripted submit did not find a stored proposal id")


def _run(
    connection: sqlite3.Connection,
    settings,
    provider,
    *,
    run_id: str = "RUN-AGENT-1",
    memory_mode: MemoryMode = MemoryMode.OFF,
    monotonic=None,
    perf_counter=None,
):
    kwargs: dict[str, Any] = {
        "mode": ExecutionMode.TEST,
        "memory_mode": memory_mode,
        "provider": provider,
        "run_id": run_id,
    }
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if perf_counter is not None:
        kwargs["perf_counter"] = perf_counter
    return investigate_case(connection, settings, WS, T03_CASE_ID, **kwargs)


def test_agent_source_does_not_hardcode_fixture_ids() -> None:
    source = Path(investigate_case.__code__.co_filename).read_text(encoding="utf-8")
    assert "ST-8721" not in source
    assert T03_PAYMENT_ID not in source
    assert T03_REMIT_ID not in source
    assert T03_FEE_ID not in source
    prompt = investigator_prompt_text()
    assert T03_CASE_ID not in prompt
    message = initial_user_message(T03_CASE_ID)
    assert message.startswith(f"Investigate case {T03_CASE_ID}.")
    assert "INV-1042" not in message


def test_scripted_t03_resolves_and_mutates_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ProposalAwareProvider(_resolve_script())
    result = _run(connection, settings, provider)
    assert result.terminal_outcome is AgentOutcome.RESOLVED
    assert result.error is None
    assert result.application_id is not None
    assert T03_REMIT_ID in result.summary
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    case = get_case(connection, WS, T03_CASE_ID)
    assert invoice is not None and invoice.outstanding_cents == 0
    assert payment is not None and payment.applied is True
    assert case is not None and case.state is CaseState.RESOLVED
    events = list_run_events(connection, result.run_id)
    kinds = [event.event_kind for event in events]
    assert RunEventKind.MODEL_RESPONSE in kinds
    assert RunEventKind.APPLICATION_COMMITTED in kinds
    assert kinds[-1] is RunEventKind.RUN_FINISHED
    run = get_run(connection, result.run_id)
    assert run is not None
    assert run.provider == "scripted"
    assert run.usage is not None
    assert run.usage.model_attempts >= 5
    assert run.usage.tool_calls >= 6
    first = provider.requests[0]
    assert "Investigate case CASE-1CFA9FEE848F" in first["messages"][0]["content"]
    assert first["remaining_seconds"] is not None
    connection.close()


def test_services_investigate_uses_same_scripted_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    result = investigate(
        settings,
        WS,
        T03_CASE_ID,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.OFF,
        provider=ProposalAwareProvider(_resolve_script()),
        run_id="RUN-SERVICE-1",
    )
    assert result.terminal_outcome is AgentOutcome.RESOLVED
    connection = _open(tmp_path)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    assert payment is not None and payment.applied is True
    connection.close()


def test_validation_recovery_then_submit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    payload = _t03_payload()
    provider = ProposalAwareProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "read_document", {"document_id": T03_REMIT_ID})]),
            _turn(tool_calls=[_call("c3", "validate_resolution", payload)]),
            _turn(tool_calls=[_call("c4", "read_document", {"document_id": T03_FEE_ID})]),
            _turn(tool_calls=[_call("c5", "validate_resolution", payload)]),
            _turn(tool_calls=[_call("c6", "submit_resolution", {"proposal_id": "PENDING"})]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-RECOVER")
    assert result.terminal_outcome is AgentOutcome.RESOLVED
    events = list_run_events(connection, result.run_id)
    assert any(event.event_kind is RunEventKind.PROPOSAL_REJECTED for event in events)
    assert any(event.event_kind is RunEventKind.PROPOSAL_VALIDATED for event in events)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    assert invoice is not None and invoice.outstanding_cents == 0
    connection.close()


def test_review_on_missing_fee_notice_does_not_mutate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ScriptedProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "read_document", {"document_id": T03_REMIT_ID})]),
            _turn(tool_calls=[_call("c3", "request_review", _review_payload())]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-REVIEW")
    assert result.terminal_outcome is AgentOutcome.REVIEW
    assert result.review is not None
    assert result.review.reason_code.value == "MISSING_FEE_NOTICE"
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    case = get_case(connection, WS, T03_CASE_ID)
    assert payment is not None and payment.applied is False
    assert invoice is not None and invoice.outstanding_cents == 1_000_000
    assert case is not None and case.state is CaseState.NEEDS_REVIEW
    events = list_run_events(connection, result.run_id)
    assert any(event.event_kind is RunEventKind.REVIEW_REQUESTED for event in events)
    connection.close()


def test_budget_exhausted_on_model_calls_is_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_MAX_MODEL_CALLS="1")
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ScriptedProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "read_document", {"document_id": T03_REMIT_ID})]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-BUDGET-M")
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(provider.requests) == 1
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    assert payment is not None and payment.applied is False
    case = get_case(connection, WS, T03_CASE_ID)
    assert case is not None and case.state is CaseState.ERROR
    events = [event.event_kind for event in list_run_events(connection, result.run_id)]
    assert RunEventKind.RUN_FAILED in events
    connection.close()


def test_budget_exhausted_on_tool_calls_is_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_MAX_TOOL_CALLS="1")
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ScriptedProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "read_document", {"document_id": T03_REMIT_ID})]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-BUDGET-T")
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None and result.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(provider.requests) == 1
    connection.close()


def test_budget_exhausted_on_elapsed_time_is_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, PRECEDENT_MAX_CASE_SECONDS="1")
    connection = _open(tmp_path)
    _import_t03(connection)
    clock = ManualClock()
    inner = ScriptedProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "read_document", {"document_id": T03_REMIT_ID})]),
        ]
    )
    provider = AdvancingProvider(inner, clock, 2.0)
    result = _run(
        connection,
        settings,
        provider,
        run_id="RUN-BUDGET-S",
        monotonic=clock.time,
        perf_counter=clock.time,
    )
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None and result.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert result.usage.elapsed_ms >= 2000
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    assert payment is not None and payment.applied is False
    connection.close()


def test_text_only_then_reminder_then_no_terminal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ScriptedProvider(
        [
            _turn(text="Looking at the payment now."),
            _turn(text="Still thinking in prose."),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-NOTERM")
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.NO_TERMINAL_ACTION
    assert "Finish through a terminal tool" in provider.requests[1]["messages"][-1]["content"]
    case = get_case(connection, WS, T03_CASE_ID)
    assert case is not None and case.state is CaseState.ERROR
    connection.close()


def test_terminal_skips_remaining_same_turn_calls(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    payload = _t03_payload()
    provider = ProposalAwareProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(
                tool_calls=[
                    _call("c2", "read_document", {"document_id": T03_REMIT_ID}),
                    _call("c3", "read_document", {"document_id": T03_FEE_ID}),
                ]
            ),
            _turn(tool_calls=[_call("c4", "validate_resolution", payload)]),
            _turn(
                tool_calls=[
                    _call("c5", "submit_resolution", {"proposal_id": "PENDING"}),
                    _call("c6", "request_review", _review_payload()),
                ]
            ),
            _turn(tool_calls=[_call("c7", "get_case")]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-SKIP")
    assert result.terminal_outcome is AgentOutcome.RESOLVED
    assert len(provider.inner.requests) == 4
    events = list_run_events(connection, result.run_id)
    skipped = [
        event
        for event in events
        if event.payload.get("error_code") == "TERMINAL_REACHED"
        and event.payload.get("status") == "skipped"
    ]
    assert skipped
    case = get_case(connection, WS, T03_CASE_ID)
    assert case is not None and case.state is CaseState.RESOLVED
    connection.close()


def test_provider_error_is_error_not_review(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    provider = ScriptedProvider(
        [ProviderError(ErrorCode.PROVIDER_UNAVAILABLE, "Provider server or overload error.")]
    )
    result = _run(connection, settings, provider, run_id="RUN-PROV")
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert result.review is None
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    assert payment is not None and payment.applied is False
    connection.close()


def test_repeated_identical_invalid_tools_stop_as_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    bad = _call("same", "execute_sql", {"sql": "SELECT 1"})
    turns = [_turn(tool_calls=[bad]) for _ in range(MAX_IDENTICAL_INVALID_TOOLS)]
    provider = ScriptedProvider(turns)
    result = _run(connection, settings, provider, run_id="RUN-REPEAT")
    assert result.terminal_outcome is AgentOutcome.ERROR
    assert result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN_TOOL
    connection.close()


def test_interrupted_running_case_is_reconciled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    insert_run(
        connection,
        RunRecord(
            run_id="RUN-OLD",
            workspace_id=WS,
            case_id=T03_CASE_ID,
            mode=ExecutionMode.TEST,
            provider=None,
            model=None,
            prompt_hash=HASH_A,
            tool_hash=HASH_A,
            memory_snapshot=empty_memory_snapshot(),
            state=RunState.RUNNING,
            started_at=CREATED,
            finished_at=None,
            budgets=BudgetLimits(
                max_model_calls=10,
                max_tool_calls=30,
                max_case_seconds=120,
                request_timeout_seconds=30,
                max_output_tokens=1500,
            ),
            usage=None,
            terminal_result=None,
        ),
    )
    connection.execute(
        """
        UPDATE cases SET state = ?, current_run_id = ?, latest_run_id = ?
        WHERE workspace_id = ? AND case_id = ?
        """,
        (CaseState.RUNNING.value, "RUN-OLD", "RUN-OLD", WS, T03_CASE_ID),
    )
    provider = ScriptedProvider(
        [
            _turn(tool_calls=[_call("c1", "get_case")]),
            _turn(tool_calls=[_call("c2", "request_review", _review_payload())]),
        ]
    )
    result = _run(connection, settings, provider, run_id="RUN-NEW")
    old = get_run(connection, "RUN-OLD")
    assert old is not None
    assert old.state is RunState.INTERRUPTED
    assert old.terminal_result is AgentOutcome.ERROR
    assert result.terminal_outcome is AgentOutcome.REVIEW
    connection.close()


def test_resolved_case_cannot_rerun(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    first = _run(
        connection,
        settings,
        ProposalAwareProvider(_resolve_script()),
        run_id="RUN-FIRST",
    )
    assert first.terminal_outcome is AgentOutcome.RESOLVED
    with pytest.raises(InvestigationError) as exc:
        _run(
            connection,
            settings,
            ScriptedProvider([_turn(tool_calls=[_call("c1", "get_case")])]),
            run_id="RUN-SECOND",
        )
    assert exc.value.code == "INVALID_INPUT"
    connection.close()


def test_live_mode_refuses_scripted_provider(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PRECEDENT_ENABLE_LIVE="true",
        ANTHROPIC_API_KEY="secret-test-key",
        PRECEDENT_MODEL="claude-test-model",
    )
    connection = _open(tmp_path)
    _import_t03(connection)
    with pytest.raises(InvestigationError) as exc:
        investigate_case(
            connection,
            settings,
            WS,
            T03_CASE_ID,
            mode=ExecutionMode.LIVE,
            memory_mode=MemoryMode.OFF,
            provider=ScriptedProvider([_turn(text="no")]),
            run_id="RUN-LIVE",
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR.value
    connection.close()


def test_cli_run_live_disabled_is_not_a_mock_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _load():
        return _settings(tmp_path)

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(
        [
            "run",
            "--workspace",
            "WS-TEACH-001",
            "--case",
            T03_CASE_ID,
            "--memory",
            "off",
            "--live",
        ]
    )
    assert code == 2
