from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from precedent.cli import main
from precedent.config import detect_repo_root, load_settings
from precedent.db import (
    get_application_by_payment,
    get_invoice,
    get_payment,
    get_run,
    insert_run,
    open_database,
)
from precedent.evidence import read_document
from precedent.fixtures import (
    CUST_HARBOR,
    DEFAULT_SEED,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    load_manifest,
)
from precedent.ledger import store_proposal
from precedent.models import (
    ApplicationStatus,
    BudgetLimits,
    CaseState,
    DatasetSplit,
    ExecutionMode,
    MemoryMode,
    MemorySnapshot,
    ProviderTurn,
    ResolutionType,
    RunContext,
    RunRecord,
    RunState,
    ToolCall,
    Usage,
    ValidationCode,
)
from precedent.policies import POLICY_ID
from precedent.providers import ScriptedProvider
from precedent.services import (
    apply_stored_proposal,
    demo_init,
    demo_new,
    export_recorded_run,
    get_case_detail,
    initialize_demo,
    investigate,
    list_cases,
    list_recorded_runs,
    queue_counts,
    snapshot_case,
)

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
T03_CASE_ID = "CASE-1CFA9FEE848F"
MANIFEST = load_manifest(detect_repo_root() / "data" / "manifests" / "fixtures-seed-42.json")


def _settings(tmp_path: Path):
    repo = detect_repo_root()
    return load_settings(
        environ={
            "PRECEDENT_DATA_DIR": str(repo / "data"),
            "PRECEDENT_DB_PATH": str(tmp_path / "var" / "precedent.sqlite3"),
        },
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )


def _context(workspace_id: str, case_id: str, run_id: str, revision: int = 0) -> RunContext:
    return RunContext(
        workspace_id=workspace_id,
        case_id=case_id,
        run_id=run_id,
        company_id="NORTHSTAR",
        policy_id=POLICY_ID,
        policy_hash=HASH_A,
        dataset_hash=HASH_A,
        initial_ledger_revision=revision,
        execution_mode=ExecutionMode.TEST,
        actor="investigator",
        opened_documents=[],
        retrieved_precedent_version_ids=[],
        memory_snapshot=MemorySnapshot(snapshot_hash=HASH_A),
        budgets=BudgetLimits(
            max_model_calls=10,
            max_tool_calls=30,
            max_case_seconds=120,
            request_timeout_seconds=30,
            max_output_tokens=1500,
        ),
    )


def _t03_payload() -> dict[str, object]:
    return {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": (
            "The remittance identifies INV-1042; ticket ST-8721 matches the bank fee notice."
        ),
        "precedent_ids_used": [],
    }


def _insert_run(connection: sqlite3.Connection, context: RunContext) -> None:
    insert_run(
        connection,
        RunRecord(
            run_id=context.run_id,
            workspace_id=context.workspace_id,
            case_id=context.case_id,
            mode=context.execution_mode,
            provider=None,
            model=None,
            prompt_hash=HASH_A,
            tool_hash=HASH_A,
            memory_snapshot=context.memory_snapshot,
            state=RunState.RUNNING,
            started_at=CREATED,
            finished_at=None,
            budgets=context.budgets,
            usage=None,
            terminal_result=None,
        ),
    )


def _keep_evaluation(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO evaluation_runs (
          experiment_id, status, config_json, config_hash, snapshot_ids_json,
          dataset_hash, split_hash, counts_json, metrics_json, artifact_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "EXP-KEEP",
            "COMPLETE",
            "{}",
            "a" * 64,
            "[]",
            "b" * 64,
            "c" * 64,
            "{}",
            "{}",
            "artifacts/evaluations/keep.json",
            CREATED,
        ),
    )


def _t10_entry():
    return next(item for item in MANIFEST.cases if item.authoring_id == "T10")


def test_import_services_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import precedent.services as services

    assert callable(services.apply_stored_proposal)
    assert callable(services.snapshot_case)
    assert callable(services.demo_new)


def test_demo_new_does_not_corrupt_applied_workspace_or_reports(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = open_database(settings.db_path)
    _keep_evaluation(connection)
    connection.close()

    teaching = demo_init(settings, seed=DEFAULT_SEED)
    assert teaching.workspace_id == "WS-TEACH-001"
    t10 = _t10_entry()
    connection = open_database(settings.db_path)
    t10_payment_before = get_payment(connection, teaching.workspace_id, t10.payment_id)
    t10_application_before = get_application_by_payment(
        connection, teaching.workspace_id, t10.payment_id
    )
    assert t10_payment_before is not None
    assert t10_payment_before.applied is True
    assert t10_application_before is not None
    assert t10_application_before.seeded is True
    t10_invoice_before = get_invoice(connection, teaching.workspace_id, t10.invoice_ids[0])
    assert t10_invoice_before is not None
    assert t10_invoice_before.outstanding_cents == 0

    context = _context(teaching.workspace_id, T03_CASE_ID, "RUN-SVC-T03")
    opened_remit = read_document(connection, context, {"document_id": T03_REMIT_ID})
    opened_fee = read_document(connection, context, {"document_id": T03_FEE_ID})
    assert opened_remit.ok is True
    assert opened_fee.ok is True
    _insert_run(connection, context)
    stored = store_proposal(connection, context, _t03_payload())
    connection.close()

    applied = apply_stored_proposal(
        settings, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    assert applied.status is ApplicationStatus.APPLIED
    assert applied.ledger_revision == 1
    snapshot = snapshot_case(settings, teaching.workspace_id, T03_CASE_ID)
    assert snapshot.payment.payment_id == T03_PAYMENT_ID
    assert snapshot.payment.applied is True
    assert snapshot.ledger_revision == 1
    assert snapshot.summary.case_state is CaseState.RESOLVED
    harbor = next(item for item in snapshot.invoices if item.invoice_id == T03_INVOICE_ID)
    assert harbor.outstanding_cents == 0

    replayed = apply_stored_proposal(
        settings,
        context.model_copy(update={"initial_ledger_revision": 0}),
        stored.proposal_id,
        idempotency_key="APPLY-T03",
        actor="investigator",
    )
    assert replayed.status is ApplicationStatus.REPLAYED
    assert replayed.application_id == applied.application_id

    candidate = demo_new(settings, dataset=DatasetSplit.CANDIDATE, seed=DEFAULT_SEED)
    second_teaching = demo_new(settings, seed=DEFAULT_SEED)
    assert candidate.workspace_id == "WS-CAND-001"
    assert second_teaching.workspace_id == "WS-TEACH-002"

    after = snapshot_case(settings, teaching.workspace_id, T03_CASE_ID)
    assert after.ledger_revision == 1
    assert after.payment.applied is True
    assert after.summary.case_state is CaseState.RESOLVED
    after_invoice = next(item for item in after.invoices if item.invoice_id == T03_INVOICE_ID)
    assert after_invoice.outstanding_cents == 0

    fresh = snapshot_case(settings, second_teaching.workspace_id, T03_CASE_ID)
    assert fresh.payment.applied is False
    assert fresh.ledger_revision == 0
    fresh_invoice = next(item for item in fresh.invoices if item.invoice_id == T03_INVOICE_ID)
    assert fresh_invoice.outstanding_cents == 1_000_000

    connection = open_database(settings.db_path)
    kept = connection.execute(
        "SELECT experiment_id FROM evaluation_runs WHERE experiment_id = 'EXP-KEEP'"
    ).fetchone()
    assert kept is not None
    t10_payment_after = get_payment(connection, teaching.workspace_id, t10.payment_id)
    t10_application_after = get_application_by_payment(
        connection, teaching.workspace_id, t10.payment_id
    )
    t10_invoice_after = get_invoice(connection, teaching.workspace_id, t10.invoice_ids[0])
    assert t10_payment_after is not None
    assert t10_payment_after.applied is True
    assert t10_application_after is not None
    assert t10_application_after.application_id == t10_application_before.application_id
    assert t10_invoice_after is not None
    assert t10_invoice_after.outstanding_cents == 0
    run = get_run(connection, context.run_id)
    assert run is not None
    assert run.terminal_result is not None
    connection.close()


def test_seeded_payment_rejects_a_new_apply_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    teaching = demo_init(settings, seed=DEFAULT_SEED)
    t10 = _t10_entry()
    connection = open_database(settings.db_path)
    context = _context(teaching.workspace_id, t10.case_id, "RUN-SVC-T10")
    payload = {
        "payment_id": t10.payment_id,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.EXACT_SINGLE.value,
        "allocations": [{"invoice_id": t10.invoice_ids[0], "cash_cents": 50_000, "fee_cents": 0}],
        "evidence_document_ids": [T03_REMIT_ID],
        "explanation": "Do not re-apply a seeded historical payment.",
        "precedent_ids_used": [],
    }
    _insert_run(connection, context)
    stored = store_proposal(connection, context, payload)
    before = get_application_by_payment(connection, teaching.workspace_id, t10.payment_id)
    invoice_before = get_invoice(connection, teaching.workspace_id, t10.invoice_ids[0])
    connection.close()
    rejected = apply_stored_proposal(
        settings, context, stored.proposal_id, idempotency_key="APPLY-T10", actor="investigator"
    )
    assert rejected.status is ApplicationStatus.REJECTED
    assert ValidationCode.PAYMENT_ALREADY_APPLIED in [issue.code for issue in rejected.issues]
    connection = open_database(settings.db_path)
    after = get_application_by_payment(connection, teaching.workspace_id, t10.payment_id)
    invoice_after = get_invoice(connection, teaching.workspace_id, t10.invoice_ids[0])
    assert before is not None and after is not None
    assert after.application_id == before.application_id
    assert after.seeded is True
    assert invoice_before is not None and invoice_after is not None
    assert invoice_after.outstanding_cents == invoice_before.outstanding_cents == 0
    connection.close()


def test_list_cases_and_detail_use_generated_t03_ids(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    summary = initialize_demo(settings, new_workspace=False)
    assert summary.workspace_id == "WS-TEACH-001"
    cases = list_cases(settings, summary.workspace_id)
    counts = queue_counts(settings, summary.workspace_id)
    t03 = next(item for item in cases if item.case_id == T03_CASE_ID)
    assert t03.payment_id == T03_PAYMENT_ID
    assert t03.amount_cents == 996_500
    assert t03.payer_text == "Harbor Treasury"
    assert t03.case_state is CaseState.OPEN
    assert counts.open == 10
    assert counts.resolved == 0
    assert counts.runs == 0
    detail = get_case_detail(settings, summary.workspace_id, T03_CASE_ID)
    assert detail.snapshot.payment.payment_id == T03_PAYMENT_ID
    assert any(item.document_id == T03_REMIT_ID for item in detail.documents)
    assert any(item.document_id == T03_FEE_ID for item in detail.documents)
    assert detail.application is None
    assert detail.latest_run is None


T01_CASE_ID = next(item.case_id for item in MANIFEST.cases if item.authoring_id == "T01")


def _usage() -> Usage:
    return Usage(
        model_attempts=1,
        tool_calls=1,
        input_tokens=8,
        output_tokens=4,
        elapsed_ms=1,
        estimated_cost_usd=None,
        price_rate_provenance=None,
    )


def _call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments={} if arguments is None else arguments)


def _turn(tool_calls: list[ToolCall]) -> ProviderTurn:
    content = [
        {"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}
        for call in tool_calls
    ]
    return ProviderTurn(
        tool_calls=tool_calls,
        public_text=None,
        stop_reason="tool_use",
        provider_assistant_message={"role": "assistant", "content": content},
        usage=_usage(),
        provider_request_id="msg_scripted",
    )


def _review_script() -> ScriptedProvider:
    return ScriptedProvider(
        [
            _turn([_call("c1", "get_case")]),
            _turn(
                [
                    _call(
                        "c2",
                        "request_review",
                        {
                            "reason_code": "CUSTOMER_NOT_ESTABLISHED",
                            "message": "Payer label is not verified customer identity.",
                            "needed_information": "Confirm the customer from remittance.",
                            "evidence_document_ids": [],
                            "candidate_invoice_ids": [],
                        },
                    )
                ]
            ),
        ]
    )


def _bind_cli_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "PRECEDENT_MODEL",
        "PRECEDENT_ENABLE_LIVE",
        "PRECEDENT_DB_PATH",
        "PRECEDENT_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRECEDENT_DATA_DIR", str(detect_repo_root() / "data"))
    monkeypatch.setenv("PRECEDENT_DB_PATH", str(tmp_path / "var" / "precedent.sqlite3"))

    def _load():
        return load_settings(
            environ=os.environ,
            dotenv_path=tmp_path / "missing.env",
            repo_root=tmp_path,
        )

    monkeypatch.setattr("precedent.cli.load_settings", _load)


def test_recorded_run_export_reconstructs_opening_and_survives_reset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report_path = tmp_path / "artifacts" / "evaluations" / "EXP-KEEP" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text('{"keep": true}\n', encoding="utf-8")
    planted = tmp_path / "artifacts" / "recorded-runs" / "RUN-PLANTED.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text('{"keep": true}\n', encoding="utf-8")

    teaching = demo_init(settings, seed=DEFAULT_SEED)
    reviewed = investigate(
        settings,
        teaching.workspace_id,
        T01_CASE_ID,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.OFF,
        provider=_review_script(),
        run_id="RUN-REVIEW-T01",
    )
    review_path = export_recorded_run(settings, reviewed.run_id)
    review_manifest = next(
        item for item in list_recorded_runs(settings) if item.run_id == reviewed.run_id
    )
    assert review_manifest.mode is ExecutionMode.TEST
    assert review_manifest.input_snapshot.payment.applied is False
    assert review_manifest.final_financial_snapshot.payment.applied is False
    assert review_manifest.input_snapshot.ledger_revision == (
        review_manifest.final_financial_snapshot.ledger_revision
    )
    assert any(event.event_kind.value == "tool_called" for event in review_manifest.public_trace)
    review_blob = review_path.read_text(encoding="utf-8")
    assert "sk-ant-" not in review_blob
    assert "ANTHROPIC_API_KEY" not in review_blob

    connection = open_database(settings.db_path)
    context = _context(teaching.workspace_id, T03_CASE_ID, "RUN-SVC-T03")
    assert read_document(connection, context, {"document_id": T03_REMIT_ID}).ok is True
    assert read_document(connection, context, {"document_id": T03_FEE_ID}).ok is True
    _insert_run(connection, context)
    stored = store_proposal(connection, context, _t03_payload())
    connection.close()
    applied = apply_stored_proposal(
        settings, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    assert applied.status is ApplicationStatus.APPLIED
    before_export = snapshot_case(settings, teaching.workspace_id, T03_CASE_ID)
    path = export_recorded_run(settings, "RUN-SVC-T03")
    after_export = snapshot_case(settings, teaching.workspace_id, T03_CASE_ID)
    assert after_export.payment.applied is True
    assert after_export.ledger_revision == before_export.ledger_revision
    manifest = next(item for item in list_recorded_runs(settings) if item.run_id == "RUN-SVC-T03")
    opening_invoice = next(
        item for item in manifest.input_snapshot.invoices if item.invoice_id == T03_INVOICE_ID
    )
    final_invoice = next(
        item
        for item in manifest.final_financial_snapshot.invoices
        if item.invoice_id == T03_INVOICE_ID
    )
    assert manifest.input_snapshot.payment.applied is False
    assert opening_invoice.outstanding_cents == 1_000_000
    assert manifest.final_financial_snapshot.payment.applied is True
    assert final_invoice.outstanding_cents == 0
    assert "sk-ant-" not in path.read_text(encoding="utf-8")

    demo_new(settings, seed=DEFAULT_SEED)
    assert report_path.is_file()
    assert planted.is_file()
    assert path.is_file()
    assert review_path.is_file()
    kept = snapshot_case(settings, teaching.workspace_id, T03_CASE_ID)
    assert kept.payment.applied is True
    kept_invoice = next(item for item in kept.invoices if item.invoice_id == T03_INVOICE_ID)
    assert kept_invoice.outstanding_cents == 0


def test_cli_cases_list_show_and_replay_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path)
    demo_init(settings, seed=DEFAULT_SEED)
    _bind_cli_settings(tmp_path, monkeypatch)

    code = main(["cases", "list", "--workspace", "WS-TEACH-001"])
    listed = capsys.readouterr().out
    assert code == 0
    assert "CASE-1CFA9FEE848F" in listed
    assert "PAY-201" in listed
    assert "Result: PASS" in listed

    code = main(["case", "show", "--workspace", "WS-TEACH-001", "--case", "CASE-1CFA9FEE848F"])
    shown = capsys.readouterr().out
    assert code == 0
    assert "INV-1042" in shown
    assert "DOC-R201" in shown
    assert "DOC-F201" in shown
    assert "$9,965.00" in shown
    assert "$10,000.00" in shown

    code = main(["replay", "export", "--run", "RUN-MISSING"])
    missing = capsys.readouterr().out
    assert code == 2
    assert "RUN-MISSING" in missing
    assert "Result: FAIL" in missing

    reviewed = investigate(
        settings,
        "WS-TEACH-001",
        T01_CASE_ID,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.OFF,
        provider=_review_script(),
        run_id="RUN-CLI-T01",
    )
    code = main(["replay", "export", "--run", reviewed.run_id])
    exported = capsys.readouterr().out
    assert code == 0
    assert reviewed.run_id in exported
    assert "Recorded run - no live model calls" in exported
    assert "apply_proposal" in exported
    saved = tmp_path / "artifacts" / "recorded-runs" / f"{reviewed.run_id}.json"
    assert saved.is_file()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["mode"] == "TEST"
    assert payload["run_id"] == reviewed.run_id
    assert "sk-ant-" not in saved.read_text(encoding="utf-8")
