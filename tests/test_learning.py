from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from precedent.cli import main
from precedent.config import detect_repo_root, load_settings
from precedent.db import (
    get_application_by_payment,
    get_correction,
    get_invoice,
    get_payment,
    get_precedent,
    get_run,
    get_workspace,
    insert_candidate_test,
    insert_case,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_workspace,
    list_run_events,
    list_workspace_memory,
    open_database,
    update_precedent_lifecycle,
)
from precedent.evaluation import load_dev_suite
from precedent.fixtures import (
    CUST_CEDAR,
    CUST_HARBOR,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    load_package,
)
from precedent.learning import (
    LearningError,
    activate_lesson_record,
    apply_correction_record,
    format_lesson_draft,
    load_attached_memory_snapshot,
    open_evidence_record,
    propose_lesson_record,
    redraft_lesson_record,
    reject_lesson_record,
    retire_lesson_record,
    save_correction_record,
)
from precedent.learning import (
    test_lesson_record as run_lesson_test_record,
)
from precedent.models import (
    SCHEMA_VERSION,
    AgentOutcome,
    ApplicationStatus,
    CaseRecord,
    CaseState,
    CorrectionInput,
    CustomerRecord,
    DatasetSplit,
    ErrorCode,
    ExecutionMode,
    InvoiceRecord,
    LessonDraftStatus,
    LessonState,
    MemoryMode,
    PaymentRecord,
    ProviderTurn,
    ResolutionType,
    RunEventKind,
    ToolCall,
    Usage,
    WorkspaceRecord,
    hash_model,
)
from precedent.providers import ScriptedProvider
from precedent.services import (
    apply_correction,
    demo_init,
    demo_new,
    investigate,
    open_evidence,
    propose_lesson,
    save_correction,
)
from precedent.services import (
    test_lesson as run_lesson_test,
)
from test_evaluation import scripted_dev_provider

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
WS = "WS-LEARN-1"
T03_CASE_ID = "CASE-1CFA9FEE848F"
T01_PAYMENT_ID = "PAY-877D88D817A6"
TEACHING_TEXT = (
    "For Harbor's wire remittances, look up the settlement_ticket in the bank notice "
    "transfer_reference. This ticket links the current invoice to the current bank fee "
    "notice. Use the documented gross, net, and fee amounts and the existing company fee "
    "policy; never infer a fee merely from a short payment."
)
WRITEOFF_TEXT = "Write off all short payments and treat every customer the same."


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


def _usage(*, attempts: int = 1, tools: int = 0) -> Usage:
    return Usage(
        model_attempts=attempts,
        tool_calls=tools,
        input_tokens=8,
        output_tokens=4,
        elapsed_ms=1,
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


def _fee_proposal() -> dict[str, object]:
    return {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": "SINGLE_WITH_BANK_FEE",
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": (
            "The settlement ticket links the remittance and bank advice; "
            "the documented amounts agree exactly."
        ),
        "precedent_ids_used": [],
    }


def _teaching_input(**overrides: object) -> CorrectionInput:
    payload: dict[str, object] = {
        "text": TEACHING_TEXT,
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "corrected_proposal": _fee_proposal(),
        "review_reason_code": None,
    }
    payload.update(overrides)
    return CorrectionInput.model_validate(payload)


def _harbor_hint_args(*, customer_id: str = CUST_HARBOR) -> dict[str, object]:
    return {
        "title": "Follow Harbor's settlement ticket to the bank notice",
        "scope": {
            "customer_id": customer_id,
            "bank_account_id": "CASH-US-01",
            "currency": "USD",
            "channel": "WIRE",
        },
        "lookup": {
            "remittance_reference_field": "settlement_ticket",
            "bank_notice_reference_field": "transfer_reference",
            "search_terms": ["bank notice", "receiving fee"],
        },
        "summary": (
            "Use the current remittance ticket to locate the current bank notice, "
            "then check the fixed company policy and exact gross/net amounts."
        ),
    }


def _import_t03(connection: sqlite3.Connection, workspace_id: str = WS) -> None:
    package = load_package(detect_repo_root() / "data" / "source" / T03_CASE_ID)
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name="T03 learning",
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
            case_id=T03_CASE_ID,
            payment_id=T03_PAYMENT_ID,
            state=CaseState.OPEN,
        ),
    )


def _scripted_success() -> ScriptedProvider:
    return ScriptedProvider(
        [
            _turn(
                tool_calls=[_call("c1", "propose_precedent", _harbor_hint_args())],
            )
        ]
    )


def test_open_evidence_records_human_access(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    opened = open_evidence(settings, WS, T03_CASE_ID, T03_REMIT_ID, actor="demo_controller")
    assert opened.document_id == T03_REMIT_ID
    assert opened.actor == "demo_controller"
    connection = _open(tmp_path)
    again = open_evidence_record(
        connection, settings, WS, T03_CASE_ID, T03_FEE_ID, actor="demo_controller"
    )
    assert again.run_id == opened.run_id
    connection.close()


def test_save_t03_correction_is_eligible_and_does_not_apply(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    record = save_correction(settings, WS, T03_CASE_ID, _teaching_input())
    assert record.lesson_eligibility is True
    assert record.verified_resolved_proposal is not None
    assert record.verified_resolved_proposal.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE
    assert T03_REMIT_ID in record.cited_document_ids
    assert T03_FEE_ID in record.cited_document_ids
    connection = _open(tmp_path)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    assert payment is not None and payment.applied is False
    assert invoice is not None and invoice.outstanding_cents == 1_000_000
    connection.close()


def test_writeoff_correction_is_saved_but_ineligible(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    record = save_correction_record(
        connection,
        settings,
        WS,
        T03_CASE_ID,
        _teaching_input(text=WRITEOFF_TEXT),
        actor="demo_controller",
    )
    assert record.lesson_eligibility is False
    assert record.eligibility_reason is not None
    assert "Write-off" in record.eligibility_reason or "short-payment" in record.eligibility_reason
    provider = _scripted_success()
    result = propose_lesson_record(
        connection,
        settings,
        record.correction_id,
        mode=ExecutionMode.TEST,
        provider=provider,
    )
    assert result.status is LessonDraftStatus.INELIGIBLE
    assert provider.requests == []
    assert get_precedent(connection, "PRC-missing") is None
    connection.close()


def test_apply_correction_resolves_t03_as_controller(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    saved = save_correction(settings, WS, T03_CASE_ID, _teaching_input(), actor="controller")
    result = apply_correction(
        settings,
        saved.correction_id,
        idempotency_key="APPLY-T03-HUMAN",
        actor="controller",
    )
    assert result.status is ApplicationStatus.APPLIED
    connection = _open(tmp_path)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    correction = get_correction(connection, saved.correction_id)
    assert payment is not None and payment.applied is True
    assert invoice is not None and invoice.outstanding_cents == 0
    assert correction is not None and correction.application_id == result.application_id
    replay = apply_correction_record(
        connection,
        settings,
        saved.correction_id,
        idempotency_key="APPLY-T03-HUMAN",
        actor="controller",
    )
    assert replay.status is ApplicationStatus.REPLAYED
    assert replay.application_id == result.application_id
    connection.close()


def test_explanatory_correction_on_resolved_case_does_not_reapply(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    first = save_correction(settings, WS, T03_CASE_ID, _teaching_input())
    applied = apply_correction(
        settings, first.correction_id, idempotency_key="APPLY-ONCE", actor="controller"
    )
    assert applied.status is ApplicationStatus.APPLIED
    comment = save_correction(
        settings,
        WS,
        T03_CASE_ID,
        _teaching_input(corrected_proposal=None),
    )
    assert comment.lesson_eligibility is True
    connection = _open(tmp_path)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    assert payment is not None and payment.applied is True
    assert invoice is not None and invoice.outstanding_cents == 0
    connection.close()


def test_propose_lesson_creates_inspectable_draft(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    result = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    assert result.status is LessonDraftStatus.CREATED
    assert result.precedent_id is not None
    precedent = get_precedent(connection, result.precedent_id)
    assert precedent is not None
    assert precedent.status is LessonState.DRAFT
    assert precedent.hint.scope.customer_id == CUST_HARBOR
    assert precedent.hint.scope.bank_account_id == "CASH-US-01"
    assert precedent.hint.lookup.remittance_reference_field.value == "settlement_ticket"
    assert precedent.correction_id == saved.correction_id
    assert precedent.compiler_metadata.get("prompt_version") == "lesson_compiler_v1"
    readable = format_lesson_draft(saved, result, precedent)
    assert TEACHING_TEXT in readable
    assert "CUST-HARBOR" in readable
    assert T03_REMIT_ID in readable
    membership = connection.execute("SELECT COUNT(*) AS n FROM workspace_memory").fetchone()
    assert membership["n"] == 0
    connection.close()


def test_widened_scope_is_rejected_without_fabricating_a_draft(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    provider = ScriptedProvider(
        [
            _turn(
                tool_calls=[
                    _call("c1", "propose_precedent", _harbor_hint_args(customer_id=CUST_CEDAR))
                ]
            ),
            _turn(
                tool_calls=[
                    _call("c2", "propose_precedent", _harbor_hint_args(customer_id=CUST_CEDAR))
                ]
            ),
        ]
    )
    result = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=provider,
    )
    assert result.status is LessonDraftStatus.ERROR
    assert result.precedent_id is None
    assert "Scope" in result.reason
    connection.close()


def test_compiler_cannot_generalize_yields_ineligible(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    provider = ScriptedProvider(
        [
            _turn(
                tool_calls=[
                    _call(
                        "c1",
                        "cannot_generalize",
                        {"reason": "The instruction cannot be represented by the lookup template."},
                    )
                ]
            )
        ]
    )
    result = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=provider,
    )
    assert result.status is LessonDraftStatus.INELIGIBLE
    assert result.precedent_id is None
    connection.close()


def test_scope_and_provenance_persist_across_reopen(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    result = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    payload_hash = get_precedent(connection, result.precedent_id).payload_hash  # type: ignore[union-attr]
    correction_id = saved.correction_id
    precedent_id = result.precedent_id
    connection.close()

    reopened = _open(tmp_path)
    correction = get_correction(reopened, correction_id)
    precedent = get_precedent(reopened, precedent_id)
    assert correction is not None
    assert correction.text == TEACHING_TEXT
    assert correction.cited_document_ids == [T03_REMIT_ID, T03_FEE_ID]
    assert precedent is not None
    assert precedent.payload_hash == payload_hash
    assert precedent.hint.scope.customer_id == CUST_HARBOR
    assert precedent.correction_id == correction_id
    assert precedent.status is LessonState.DRAFT
    reopened.close()


def test_saving_a_draft_does_not_affect_another_investigation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    teaching = demo_init(settings)
    saved = save_correction(settings, teaching.workspace_id, T03_CASE_ID, _teaching_input())
    result = propose_lesson(
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    assert result.status is LessonDraftStatus.CREATED
    connection = open_database(settings.db_path)
    t01_payment = get_payment(connection, teaching.workspace_id, T01_PAYMENT_ID)
    t03_payment = get_payment(connection, teaching.workspace_id, T03_PAYMENT_ID)
    attached = connection.execute(
        "SELECT COUNT(*) AS n FROM workspace_memory WHERE workspace_id = ?",
        (teaching.workspace_id,),
    ).fetchone()
    drafts = connection.execute(
        "SELECT status FROM precedents WHERE precedent_id = ?",
        (result.precedent_id,),
    ).fetchone()
    assert t01_payment is not None and t01_payment.applied is False
    assert t03_payment is not None and t03_payment.applied is False
    assert attached["n"] == 0
    assert drafts["status"] == LessonState.DRAFT.value
    connection.close()


def test_live_propose_refuses_scripted_provider(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        PRECEDENT_ENABLE_LIVE="true",
        ANTHROPIC_API_KEY="secret-test-key",
        PRECEDENT_MODEL="claude-test-model",
    )
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    with pytest.raises(LearningError) as exc:
        propose_lesson_record(
            connection,
            settings,
            saved.correction_id,
            mode=ExecutionMode.LIVE,
            provider=_scripted_success(),
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR.value
    connection.close()


def test_cli_lesson_propose_live_disabled_is_not_a_mock_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    connection.close()

    def _load():
        return settings

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(["lesson", "propose", "--correction", saved.correction_id, "--live"])
    assert code == 2


def test_cli_correction_save_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    connection.close()
    payload_path = tmp_path / "t03-correction.json"
    payload_path.write_text(
        json.dumps(
            {
                "text": TEACHING_TEXT,
                "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
                "corrected_proposal": _fee_proposal(),
                "review_reason_code": None,
            }
        ),
        encoding="utf-8",
    )

    def _load():
        return settings

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(
        [
            "correction",
            "save",
            "--workspace",
            WS,
            "--case",
            T03_CASE_ID,
            "--file",
            str(payload_path),
        ]
    )
    assert code == 0
    connection = _open(tmp_path)
    rows = connection.execute(
        "SELECT correction_id, lesson_eligibility FROM corrections"
    ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["lesson_eligibility"]) == 1
    connection.close()


def _dev_provider_factory(settings, *, fee_on_v02_candidate: bool = False):
    suite = load_dev_suite(settings.data_dir)
    by_case = {entry.case_id: (entry, package, oracle) for entry, package, oracle in suite}

    def factory(case_id: str, arm: str):
        entry, package, oracle = by_case[case_id]
        force = fee_on_v02_candidate and arm == "candidate" and entry.authoring_id == "V02"
        return scripted_dev_provider(package, oracle, arm, force_fee_on_review=force)

    return factory


def _draft_lesson(tmp_path: Path):
    settings = _settings(tmp_path)
    connection = _open(tmp_path)
    _import_t03(connection)
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    result = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    connection.close()
    assert result.precedent_id is not None
    return settings, result.precedent_id


def _bind_live_report(connection, result) -> None:
    live_id = f"RPT-{uuid.uuid4().hex}"
    live = result.report.model_copy(update={"report_id": live_id, "mode": ExecutionMode.LIVE})
    insert_candidate_test(
        connection,
        live,
        outcomes={
            "report": live.model_dump(mode="json"),
            "episodes": [item.model_dump(mode="json") for item in result.episodes],
        },
        score_summary={"passed_limited_checks": True, "failed_reasons": []},
    )
    record = get_precedent(connection, result.precedent_id)
    assert record is not None
    update_precedent_lifecycle(
        connection,
        record.model_copy(update={"test_report_id": live_id, "status": LessonState.PASSED}),
    )


def _activated_lesson(tmp_path: Path):
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    result = run_lesson_test_record(
        connection,
        settings,
        precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    assert result.passed_limited_checks is True
    _bind_live_report(connection, result)
    activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    connection.close()
    return settings, precedent_id


def _retrieved_ids(connection: sqlite3.Connection, run_id: str) -> list[str]:
    ids: list[str] = []
    for event in list_run_events(connection, run_id):
        if event.event_kind is not RunEventKind.PRECEDENT_RETRIEVED:
            continue
        raw = event.payload.get("precedent_ids")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str) and item and item not in ids:
                ids.append(item)
    return ids


def _excluded_reasons(connection: sqlite3.Connection, run_id: str) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for event in list_run_events(connection, run_id):
        if event.event_kind is not RunEventKind.PRECEDENT_RETRIEVED:
            continue
        raw = event.payload.get("excluded")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("precedent_id"), str):
                reasons[item["precedent_id"]] = str(item.get("reason") or "")
    return reasons


def test_cannot_activate_untested_or_scripted_report(tmp_path: Path) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    with pytest.raises(LearningError) as exc:
        activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    assert "PASSED" in exc.value.message or "untested" in exc.value.message
    result = run_lesson_test_record(
        connection,
        settings,
        precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    assert result.passed_limited_checks is True
    assert result.report.mode is ExecutionMode.TEST
    assert len(result.episodes) == 10
    with pytest.raises(LearningError) as live_exc:
        activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    assert "live-tested" in live_exc.value.message
    connection.close()


def test_scripted_candidate_suite_and_explicit_activation(tmp_path: Path) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    owner_payment = get_payment(connection, WS, T03_PAYMENT_ID)
    owner_invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    assert owner_payment is not None and owner_payment.applied is False
    assert owner_invoice is not None and owner_invoice.outstanding_cents == 1_000_000
    result = run_lesson_test_record(
        connection,
        settings,
        precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    assert result.passed_limited_checks is True
    assert result.report.state.value == "PASSED"
    v01 = next(item.case_id for item in result.report.paired_scores if item.case_id)
    suite = load_dev_suite(settings.data_dir)
    v01_id = next(entry.case_id for entry, _p, _o in suite if entry.authoring_id == "V01")
    v02_id = next(entry.case_id for entry, _p, _o in suite if entry.authoring_id == "V02")
    v03_id = next(entry.case_id for entry, _p, _o in suite if entry.authoring_id == "V03")
    v01_candidate = next(
        item for item in result.episodes if item.case_id == v01_id and item.arm == "candidate"
    )
    v02_candidate = next(
        item for item in result.episodes if item.case_id == v02_id and item.arm == "candidate"
    )
    v03_candidate = next(
        item for item in result.episodes if item.case_id == v03_id and item.arm == "candidate"
    )
    assert v01_candidate.correct is True
    assert v01_candidate.retrieved_precedent_ids == [precedent_id]
    assert v02_candidate.correct is True
    assert v02_candidate.outcome is AgentOutcome.REVIEW
    assert v02_candidate.new_application_count == 0
    assert v03_candidate.correct is True
    assert v03_candidate.retrieved_precedent_ids == []
    after_payment = get_payment(connection, WS, T03_PAYMENT_ID)
    after_invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    assert after_payment is not None and after_payment.applied is False
    assert after_invoice is not None and after_invoice.outstanding_cents == 1_000_000
    _bind_live_report(connection, result)
    activated = activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    assert activated.status is LessonState.ACTIVE
    stored = get_precedent(connection, precedent_id)
    assert stored is not None
    assert stored.activation_actor == "demo_controller"
    assert stored.payload_hash == result.report.candidate_payload_hash
    assert list_workspace_memory(connection, WS) == [precedent_id]
    connection.close()
    del v01


def test_fee_on_same_gap_dispute_fails_candidate_suite(tmp_path: Path) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    result = run_lesson_test_record(
        connection,
        settings,
        precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings, fee_on_v02_candidate=True),
    )
    assert result.passed_limited_checks is False
    assert result.report.state.value == "FAILED"
    stored = get_precedent(connection, precedent_id)
    assert stored is not None and stored.status is LessonState.FAILED
    with pytest.raises(LearningError):
        activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    connection.close()


def test_redraft_and_wrong_version_cannot_inherit_approval(tmp_path: Path) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    result = run_lesson_test_record(
        connection,
        settings,
        precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    _bind_live_report(connection, result)
    activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    redraft = redraft_lesson_record(connection, settings, precedent_id)
    assert redraft.status is LessonState.DRAFT
    assert redraft.precedent_id != precedent_id
    source = get_precedent(connection, precedent_id)
    draft = get_precedent(connection, redraft.precedent_id)
    assert source is not None and source.status is LessonState.ACTIVE
    assert draft is not None and draft.test_report_id is None
    with pytest.raises(LearningError) as exc:
        activate_lesson_record(connection, settings, redraft.precedent_id, actor="demo_controller")
    assert "PASSED" in exc.value.message or "untested" in exc.value.message
    connection.close()


def test_failed_activation_does_not_retire_working_version(tmp_path: Path) -> None:
    settings, first_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    first = run_lesson_test_record(
        connection,
        settings,
        first_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    _bind_live_report(connection, first)
    activate_lesson_record(connection, settings, first_id, actor="demo_controller")
    saved = save_correction_record(connection, settings, WS, T03_CASE_ID, _teaching_input())
    second_draft = propose_lesson_record(
        connection,
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    assert second_draft.precedent_id is not None
    with pytest.raises(LearningError):
        activate_lesson_record(
            connection, settings, second_draft.precedent_id, actor="demo_controller"
        )
    active = get_precedent(connection, first_id)
    untested = get_precedent(connection, second_draft.precedent_id)
    assert active is not None and active.status is LessonState.ACTIVE
    assert untested is not None and untested.status is LessonState.DRAFT
    connection.close()


def test_replacement_retires_previous_active_version(tmp_path: Path) -> None:
    settings, first_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    first = run_lesson_test_record(
        connection,
        settings,
        first_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    _bind_live_report(connection, first)
    activate_lesson_record(connection, settings, first_id, actor="demo_controller")
    redraft = redraft_lesson_record(connection, settings, first_id)
    second = run_lesson_test_record(
        connection,
        settings,
        redraft.precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    _bind_live_report(connection, second)
    replaced = activate_lesson_record(
        connection, settings, redraft.precedent_id, actor="demo_controller"
    )
    assert replaced.previous_precedent_id == first_id
    previous = get_precedent(connection, first_id)
    current = get_precedent(connection, redraft.precedent_id)
    assert previous is not None and previous.status is LessonState.RETIRED
    assert current is not None and current.status is LessonState.ACTIVE
    connection.close()


def test_teaching_workspace_balances_unchanged_after_candidate_test(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    teaching = demo_init(settings)
    saved = save_correction(settings, teaching.workspace_id, T03_CASE_ID, _teaching_input())
    draft = propose_lesson(
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_scripted_success(),
    )
    assert draft.precedent_id is not None
    connection = open_database(settings.db_path)
    t01_before = get_payment(connection, teaching.workspace_id, T01_PAYMENT_ID)
    t03_before = get_payment(connection, teaching.workspace_id, T03_PAYMENT_ID)
    assert t01_before is not None and t01_before.applied is False
    assert t03_before is not None and t03_before.applied is False
    connection.close()
    result = run_lesson_test(
        settings,
        draft.precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    assert result.passed_limited_checks is True
    connection = open_database(settings.db_path)
    t01_after = get_payment(connection, teaching.workspace_id, T01_PAYMENT_ID)
    t03_after = get_payment(connection, teaching.workspace_id, T03_PAYMENT_ID)
    attached = list_workspace_memory(connection, teaching.workspace_id)
    stored = get_precedent(connection, draft.precedent_id)
    assert t01_after is not None and t01_after.applied is False
    assert t03_after is not None and t03_after.applied is False
    assert attached == []
    assert stored is not None and stored.status is LessonState.PASSED
    connection.close()


def test_reject_and_retire_lifecycle(tmp_path: Path) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)
    connection = _open(tmp_path)
    rejected = reject_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    assert rejected.status is LessonState.REJECTED
    with pytest.raises(LearningError):
        activate_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    redraft = redraft_lesson_record(connection, settings, precedent_id)
    tested = run_lesson_test_record(
        connection,
        settings,
        redraft.precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_provider_factory(settings),
    )
    _bind_live_report(connection, tested)
    activate_lesson_record(connection, settings, redraft.precedent_id, actor="demo_controller")
    retired = retire_lesson_record(
        connection, settings, redraft.precedent_id, actor="demo_controller"
    )
    assert retired.status is LessonState.RETIRED
    stored = get_precedent(connection, redraft.precedent_id)
    assert stored is not None and stored.retirement_actor == "demo_controller"
    connection.close()


def test_cli_lesson_test_live_disabled_is_not_a_mock_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)

    def _load():
        return settings

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(["lesson", "test", "--id", precedent_id, "--live"])
    assert code == 2
    connection = _open(tmp_path)
    stored = get_precedent(connection, precedent_id)
    assert stored is not None and stored.status is LessonState.DRAFT
    connection.close()


def test_cli_activate_untested_draft_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, precedent_id = _draft_lesson(tmp_path)

    def _load():
        return settings

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(["lesson", "activate", "--id", precedent_id, "--actor", "demo_controller"])
    assert code == 2


def test_active_memory_persists_across_reopen_and_follow_up_workspace(tmp_path: Path) -> None:
    settings, precedent_id = _activated_lesson(tmp_path)
    connection = _open(tmp_path)
    owner = get_workspace(connection, WS)
    assert owner is not None
    snapshot, notes = load_attached_memory_snapshot(connection, owner, settings)
    assert [item.precedent_id for item in snapshot.hints] == [precedent_id]
    assert any(note.included for note in notes)
    stored = get_precedent(connection, precedent_id)
    assert stored is not None
    assert stored.hint.lookup.remittance_reference_field.value == "settlement_ticket"
    connection.close()

    connection = _open(tmp_path)
    owner = get_workspace(connection, WS)
    assert owner is not None
    reopened, _notes = load_attached_memory_snapshot(connection, owner, settings)
    assert [item.precedent_id for item in reopened.hints] == [precedent_id]
    connection.close()

    follow = demo_new(settings, dataset=DatasetSplit.CANDIDATE, memory_from=WS, seed=42)
    plain = demo_new(settings, dataset=DatasetSplit.CANDIDATE, seed=42)
    off_ws = demo_new(settings, dataset=DatasetSplit.CANDIDATE, memory_from=WS, seed=42)
    assert follow.attached_precedent_ids == (precedent_id,)
    assert plain.attached_precedent_ids == ()
    assert off_ws.attached_precedent_ids == (precedent_id,)

    suite = load_dev_suite(settings.data_dir)
    by_authoring = {
        entry.authoring_id: (entry, package, oracle) for entry, package, oracle in suite
    }
    v01_entry, v01_package, v01_oracle = by_authoring["V01"]
    v02_entry, v02_package, v02_oracle = by_authoring["V02"]
    v03_entry, v03_package, v03_oracle = by_authoring["V03"]
    v04_entry, v04_package, v04_oracle = by_authoring["V04"]

    v01 = investigate(
        settings,
        follow.workspace_id,
        v01_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.ON,
        provider=scripted_dev_provider(v01_package, v01_oracle, "candidate"),
        run_id="RUN-V01-ON",
    )
    v02 = investigate(
        settings,
        follow.workspace_id,
        v02_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.ON,
        provider=scripted_dev_provider(v02_package, v02_oracle, "candidate"),
        run_id="RUN-V02-ON",
    )
    v03 = investigate(
        settings,
        follow.workspace_id,
        v03_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.ON,
        provider=scripted_dev_provider(v03_package, v03_oracle, "candidate"),
        run_id="RUN-V03-ON",
    )
    v04 = investigate(
        settings,
        follow.workspace_id,
        v04_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.ON,
        provider=scripted_dev_provider(v04_package, v04_oracle, "candidate"),
        run_id="RUN-V04-ON",
    )
    v01_off = investigate(
        settings,
        off_ws.workspace_id,
        v01_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.OFF,
        provider=scripted_dev_provider(v01_package, v01_oracle, "candidate"),
        run_id="RUN-V01-OFF",
    )
    v01_plain = investigate(
        settings,
        plain.workspace_id,
        v01_entry.case_id,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.ON,
        provider=scripted_dev_provider(v01_package, v01_oracle, "candidate"),
        run_id="RUN-V01-PLAIN",
    )

    assert v01.terminal_outcome is AgentOutcome.RESOLVED
    assert v02.terminal_outcome is AgentOutcome.REVIEW
    assert v03.terminal_outcome is AgentOutcome.RESOLVED
    assert v04.terminal_outcome is AgentOutcome.REVIEW
    assert v01_off.terminal_outcome is AgentOutcome.RESOLVED
    assert v01_plain.terminal_outcome is AgentOutcome.RESOLVED

    connection = _open(tmp_path)
    assert _retrieved_ids(connection, v01.run_id) == [precedent_id]
    assert _retrieved_ids(connection, v02.run_id) == [precedent_id]
    assert _retrieved_ids(connection, v03.run_id) == []
    assert _excluded_reasons(connection, v03.run_id)[precedent_id] == (
        "customer_id does not match lesson scope"
    )
    assert _retrieved_ids(connection, v04.run_id) == [precedent_id]
    assert _retrieved_ids(connection, v01_off.run_id) == []
    assert _retrieved_ids(connection, v01_plain.run_id) == []
    assert get_application_by_payment(connection, follow.workspace_id, v02_entry.payment_id) is None
    assert get_application_by_payment(connection, follow.workspace_id, v04_entry.payment_id) is None
    v02_payment = get_payment(connection, follow.workspace_id, v02_entry.payment_id)
    v04_payment = get_payment(connection, follow.workspace_id, v04_entry.payment_id)
    assert v02_payment is not None and v02_payment.applied is False
    assert v04_payment is not None and v04_payment.applied is False
    on_run = get_run(connection, v01.run_id)
    off_run = get_run(connection, v01_off.run_id)
    assert on_run is not None and off_run is not None
    assert on_run.prompt_hash == off_run.prompt_hash
    assert on_run.tool_hash == off_run.tool_hash
    assert on_run.memory_snapshot.hints[0].precedent_id == precedent_id
    assert off_run.memory_snapshot.hints == []
    started = [
        event
        for event in list_run_events(connection, v01.run_id)
        if event.event_kind is RunEventKind.RUN_STARTED
    ]
    assert started
    assert started[0].payload["memory_mode"] == "on"
    assert precedent_id in started[0].payload["attached_precedent_ids"]
    connection.close()

    connection = _open(tmp_path)
    retire_lesson_record(connection, settings, precedent_id, actor="demo_controller")
    connection.close()
    connection = _open(tmp_path)
    owner = get_workspace(connection, WS)
    assert owner is not None
    retired_snapshot, retired_notes = load_attached_memory_snapshot(connection, owner, settings)
    assert retired_snapshot.hints == []
    assert any("RETIRED" in note.reason for note in retired_notes)
    assert _retrieved_ids(connection, v01.run_id) == [precedent_id]
    connection.close()


def test_incompatible_behavior_fingerprint_is_excluded_after_reopen(tmp_path: Path) -> None:
    settings, precedent_id = _activated_lesson(tmp_path)
    other = _settings(tmp_path, PRECEDENT_MODEL="other-model")
    connection = _open(tmp_path)
    owner = get_workspace(connection, WS)
    assert owner is not None
    snapshot, notes = load_attached_memory_snapshot(connection, owner, other)
    assert snapshot.hints == []
    assert any("behavior fingerprint" in note.reason for note in notes)
    stored = get_precedent(connection, precedent_id)
    assert stored is not None and stored.status is LessonState.ACTIVE
    connection.close()
