from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from precedent.config import detect_repo_root, load_settings
from precedent.db import list_invoices, list_run_events, open_database
from precedent.evaluation import (
    IsolatedEpisode,
    deterministic_lesson_checks,
    execute_isolated_case,
    financial_snapshot,
    load_dev_suite,
    load_split_suite,
    score_episode,
)
from precedent.fixtures import CUST_HARBOR, DocumentKind, SourcePackage
from precedent.models import (
    AgentOutcome,
    Allocation,
    ApplicationRecord,
    DatasetSplit,
    EpisodeDisposition,
    EvaluationRequest,
    EvaluationState,
    ExecutionMode,
    OracleRecord,
    ProviderTurn,
    ResolutionType,
    ReviewRequest,
    RunResult,
    ToolCall,
    Usage,
    WireFeeLookupHint,
)
from precedent.providers import ProviderError, ScriptedProvider
from precedent.services import demo_new, run_evaluation
from precedent.tools import empty_memory_snapshot

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"


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
                arguments["proposal_id"] = _proposal_id_from_messages(
                    kwargs.get("messages") or args[1]
                )
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


def _settings(tmp_path: Path, **env: str):
    repo = detect_repo_root()
    merged = {
        "PRECEDENT_DATA_DIR": str(repo / "data"),
        "PRECEDENT_DB_PATH": str(tmp_path / "precedent.sqlite3"),
        "PRECEDENT_ENABLE_LIVE": "false",
    }
    merged.update(env)
    return load_settings(environ=merged, dotenv_path=tmp_path / "missing.env", repo_root=tmp_path)


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


def scripted_dev_provider(
    package: SourcePackage,
    oracle: OracleRecord,
    arm: str,
    *,
    force_fee_on_review: bool = False,
) -> ProposalAwareProvider:
    payment = package.payments[0]
    remittance = next(item for item in package.documents if item.kind is DocumentKind.REMITTANCE)
    fee = next(
        (item for item in package.documents if item.kind is DocumentKind.BANK_FEE_NOTICE),
        None,
    )
    dispute = next(
        (item for item in package.documents if item.kind is DocumentKind.DISPUTE_NOTICE),
        None,
    )
    customer_id = remittance.facts.customer_id
    turns = [
        _turn(tool_calls=[_call("c1", "get_case")]),
        _turn(
            tool_calls=[
                _call(
                    "c2",
                    "search_documents",
                    {"kind": "REMITTANCE", "reference": payment.bank_reference},
                )
            ]
        ),
        _turn(tool_calls=[_call("c3", "read_document", {"document_id": remittance.document_id})]),
    ]
    index = 4
    if fee is not None and (
        oracle.expected_outcome is AgentOutcome.RESOLVED or force_fee_on_review
    ):
        turns.append(
            _turn(
                tool_calls=[_call(f"c{index}", "read_document", {"document_id": fee.document_id})]
            )
        )
        index += 1
    if dispute is not None and not force_fee_on_review:
        turns.append(
            _turn(
                tool_calls=[
                    _call(f"c{index}", "read_document", {"document_id": dispute.document_id})
                ]
            )
        )
        index += 1
    turns.append(
        _turn(tool_calls=[_call(f"c{index}", "retrieve_precedents", {"customer_id": customer_id})])
    )
    index += 1
    if oracle.expected_outcome is AgentOutcome.RESOLVED or force_fee_on_review:
        invoice_id = (
            oracle.expected_allocations[0].invoice_id
            if oracle.expected_allocations
            else next(iter(oracle.expected_ending_balances))
        )
        cash = (
            oracle.expected_allocations[0].cash_cents
            if oracle.expected_allocations
            else payment.amount_cents
        )
        if oracle.expected_allocations:
            fee_cents = oracle.expected_allocations[0].fee_cents
        else:
            fee_cents = 3500
        evidence = [remittance.document_id]
        if fee is not None:
            evidence.append(fee.document_id)
        payload = {
            "payment_id": payment.payment_id,
            "customer_id": customer_id,
            "resolution_type": "SINGLE_WITH_BANK_FEE",
            "allocations": [{"invoice_id": invoice_id, "cash_cents": cash, "fee_cents": fee_cents}],
            "evidence_document_ids": evidence,
            "explanation": "Scripted candidate-development resolution.",
            "precedent_ids_used": [],
        }
        turns.append(_turn(tool_calls=[_call(f"c{index}", "validate_resolution", payload)]))
        index += 1
        turns.append(
            _turn(tool_calls=[_call(f"c{index}", "submit_resolution", {"proposal_id": "PENDING"})])
        )
    else:
        invoice_ids = list(oracle.expected_ending_balances)
        turns.append(
            _turn(
                tool_calls=[
                    _call(
                        f"c{index}",
                        "request_review",
                        {
                            "reason_code": oracle.allowed_review_codes[0].value,
                            "message": f"Business review required for {package.case_id}.",
                            "needed_information": "Controller review of the current sources.",
                            "evidence_document_ids": [remittance.document_id],
                            "candidate_invoice_ids": invoice_ids,
                        },
                    )
                ]
            )
        )
    return ProposalAwareProvider(turns)


def scripted_split_provider(
    package: SourcePackage,
    oracle: OracleRecord,
    arm: str,
) -> ProposalAwareProvider:
    payment = package.payments[0]
    remittance = next(item for item in package.documents if item.kind is DocumentKind.REMITTANCE)
    fee = next(
        (item for item in package.documents if item.kind is DocumentKind.BANK_FEE_NOTICE),
        None,
    )
    dispute = next(
        (item for item in package.documents if item.kind is DocumentKind.DISPUTE_NOTICE),
        None,
    )
    customer_id = remittance.facts.customer_id
    turns = [
        _turn(tool_calls=[_call("c1", "get_case")]),
        _turn(
            tool_calls=[
                _call(
                    "c2",
                    "search_documents",
                    {"kind": "REMITTANCE", "reference": payment.bank_reference},
                )
            ]
        ),
        _turn(tool_calls=[_call("c3", "read_document", {"document_id": remittance.document_id})]),
    ]
    index = 4
    if fee is not None and oracle.expected_outcome is AgentOutcome.RESOLVED:
        turns.append(
            _turn(
                tool_calls=[_call(f"c{index}", "read_document", {"document_id": fee.document_id})]
            )
        )
        index += 1
    if dispute is not None:
        turns.append(
            _turn(
                tool_calls=[
                    _call(f"c{index}", "read_document", {"document_id": dispute.document_id})
                ]
            )
        )
        index += 1
    turns.append(
        _turn(tool_calls=[_call(f"c{index}", "retrieve_precedents", {"customer_id": customer_id})])
    )
    index += 1
    if oracle.expected_outcome is AgentOutcome.RESOLVED:
        evidence = [remittance.document_id]
        if fee is not None and any(item.fee_cents > 0 for item in oracle.expected_allocations):
            evidence.append(fee.document_id)
        if any(item.fee_cents > 0 for item in oracle.expected_allocations):
            resolution_type = ResolutionType.SINGLE_WITH_BANK_FEE.value
        elif len(oracle.expected_allocations) == 1:
            resolution_type = ResolutionType.EXACT_SINGLE.value
        else:
            resolution_type = ResolutionType.EXACT_BUNDLE.value
        payload = {
            "payment_id": payment.payment_id,
            "customer_id": customer_id,
            "resolution_type": resolution_type,
            "allocations": [
                {
                    "invoice_id": item.invoice_id,
                    "cash_cents": item.cash_cents,
                    "fee_cents": item.fee_cents,
                }
                for item in oracle.expected_allocations
            ],
            "evidence_document_ids": evidence,
            "explanation": "Scripted split evaluation resolution.",
            "precedent_ids_used": [],
        }
        turns.append(_turn(tool_calls=[_call(f"c{index}", "validate_resolution", payload)]))
        index += 1
        turns.append(
            _turn(tool_calls=[_call(f"c{index}", "submit_resolution", {"proposal_id": "PENDING"})])
        )
    else:
        invoice_ids = list(oracle.expected_ending_balances)
        turns.append(
            _turn(
                tool_calls=[
                    _call(
                        f"c{index}",
                        "request_review",
                        {
                            "reason_code": oracle.allowed_review_codes[0].value,
                            "message": f"Business review required for {package.case_id}.",
                            "needed_information": "Controller review of the current sources.",
                            "evidence_document_ids": [remittance.document_id],
                            "candidate_invoice_ids": invoice_ids,
                        },
                    )
                ]
            )
        )
    return ProposalAwareProvider(turns)


def _eval_factory(suite):
    packages = {entry.case_id: (package, oracle) for entry, package, oracle in suite}

    def _factory(case_id: str, arm: str) -> ProposalAwareProvider:
        package, oracle = packages[case_id]
        return scripted_split_provider(package, oracle, arm)

    return _factory


def test_schema_rejects_threshold_and_writeoff_fields() -> None:
    checks = deterministic_lesson_checks()
    assert checks.validator_suite_passed is True
    assert checks.lifecycle_suite_passed is True
    with pytest.raises(ValidationError):
        WireFeeLookupHint.model_validate(
            {
                "title": "Illegal threshold rule",
                "scope": {
                    "customer_id": CUST_HARBOR,
                    "bank_account_id": "CASH-US-01",
                    "currency": "USD",
                    "channel": "WIRE",
                },
                "lookup": {
                    "remittance_reference_field": "settlement_ticket",
                    "bank_notice_reference_field": "transfer_reference",
                    "search_terms": ["bank notice"],
                },
                "summary": "Write off short payments above a threshold.",
                "threshold_cents": 3500,
            }
        )


def test_scorer_rejects_fee_on_dispute_gap(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    suite = load_dev_suite(settings.data_dir)
    v02 = next(item for item in suite if item[0].authoring_id == "V02")
    _entry, package, oracle = v02
    invoice_id = next(iter(oracle.expected_ending_balances))
    episode = IsolatedEpisode(
        case_id=package.case_id,
        arm="candidate",
        workspace_id="WS-CTEST-001",
        payment_applied=True,
        invoice_balances={invoice_id: 0},
        opening_invoice_balances={invoice_id: 160000},
        new_applications=[
            ApplicationRecord(
                application_id="APP-FAKE",
                workspace_id="WS-CTEST-001",
                payment_id=package.payments[0].payment_id,
                proposal_id="PRP-FAKE",
                seeded=False,
                idempotency_key="submit:PRP-FAKE",
                payload_hash=HASH_A,
                actor="investigator",
                allocations=[Allocation(invoice_id=invoice_id, cash_cents=156500, fee_cents=3500)],
                created_at=CREATED,
            )
        ],
        validator_rejection_codes=[],
        duration_ms=10,
        technical_error=False,
        order_index=0,
    )
    score = score_episode(oracle, episode)
    assert score.correct is False
    assert any("application" in reason.lower() or "Outcome" in reason for reason in score.reasons)


def test_isolated_executor_does_not_load_oracles_into_the_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection = open_database(settings.db_path)
    suite = load_dev_suite(settings.data_dir)
    entry, package, oracle = next(item for item in suite if item[0].authoring_id == "V01")
    provider = scripted_dev_provider(package, oracle, "baseline")
    episode = execute_isolated_case(
        connection,
        settings,
        package,
        arm="baseline",
        mode=ExecutionMode.TEST,
        memory_snapshot=empty_memory_snapshot(),
        provider=provider,
        order_index=0,
    )
    assert episode.run_result is not None
    assert episode.run_result.terminal_outcome is AgentOutcome.RESOLVED
    events = list_run_events(connection, episode.run_result.run_id)
    blob = json.dumps([event.model_dump(mode="json") for event in events])
    assert "expected_outcome" not in blob
    assert "allowed_review_codes" not in blob
    invoices = {
        item.invoice_id: item.outstanding_cents
        for item in list_invoices(connection, episode.workspace_id)
    }
    score = score_episode(oracle, episode)
    assert score.correct is True
    for invoice_id, cents in oracle.expected_ending_balances.items():
        assert invoices[invoice_id] == cents
    connection.close()
    del entry


def test_cedar_control_oracle_forbids_harbor_scope() -> None:
    suite = load_dev_suite(detect_repo_root() / "data")
    _entry, _package, oracle = next(item for item in suite if item[0].authoring_id == "V03")
    assert oracle.forbidden_precedent_scopes
    assert oracle.forbidden_precedent_scopes[0].customer_id == CUST_HARBOR


def test_scorer_accepts_allowed_review_without_mutation() -> None:
    suite = load_dev_suite(detect_repo_root() / "data")
    _entry, package, oracle = next(item for item in suite if item[0].authoring_id == "V02")
    invoice_id = next(iter(oracle.expected_ending_balances))
    result = RunResult(
        run_id="RUN-REVIEW-OK",
        workspace_id="WS-CTEST-002",
        case_id=package.case_id,
        execution_mode=ExecutionMode.TEST,
        terminal_outcome=AgentOutcome.REVIEW,
        review=ReviewRequest(
            reason_code=oracle.allowed_review_codes[0],
            message="Open dispute remains unresolved.",
            needed_information="Controller review of the dispute notice.",
            evidence_document_ids=[package.documents[0].document_id],
            candidate_invoice_ids=[invoice_id],
        ),
        summary="Business review required.",
        usage=_usage(),
    )
    episode = IsolatedEpisode(
        case_id=package.case_id,
        arm="baseline",
        workspace_id="WS-CTEST-002",
        run_result=result,
        payment_applied=False,
        invoice_balances={invoice_id: 160000},
        opening_invoice_balances={invoice_id: 160000},
        duration_ms=4,
        order_index=0,
    )
    score = score_episode(oracle, episode)
    assert score.correct is True
    assert score.disposition is EpisodeDisposition.COMPLETED


def test_paired_evaluation_scores_isolated_teaching_smoke(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    demo = demo_new(settings, dataset=DatasetSplit.TEACHING)
    connection = open_database(settings.db_path)
    before = financial_snapshot(connection, demo.workspace_id)
    suite = load_split_suite(settings.data_dir, DatasetSplit.TEACHING, limit=2)
    request = EvaluationRequest(
        source_workspace_id=demo.workspace_id,
        split=DatasetSplit.TEACHING,
        case_limit=2,
        mode=ExecutionMode.TEST,
        deadline_seconds=120,
        output_dir=tmp_path / "artifacts" / "evaluations",
    )
    report = run_evaluation(settings, request, provider_factory=_eval_factory(suite))
    after = financial_snapshot(connection, demo.workspace_id)
    connection.close()
    assert report.mode is ExecutionMode.TEST
    assert report.state is EvaluationState.COMPLETE
    assert report.scheduled_count == 4
    assert report.completed_count == 4
    assert report.not_run_count == 0
    assert report.metrics.baseline is not None
    assert report.metrics.memory is not None
    assert report.metrics.baseline.correct_autonomous_resolutions.value == 2
    assert report.metrics.memory.correct_autonomous_resolutions.value == 2
    assert report.metrics.negative_transfer_case_ids == []
    authoring = {entry.case_id: entry.authoring_id for entry, _package, _oracle in suite}
    assert set(authoring.values()) == {"T01", "T02"}
    for row in report.per_case_paired_scores:
        assert row.baseline_correct is True
        assert row.memory_correct is True
    assert before == after
    report_json = Path(report.artifact_paths["report_json"]).read_text(encoding="utf-8")
    episodes = Path(report.artifact_paths["episodes"]).read_text(encoding="utf-8")
    assert "expected_allocations" not in report_json
    assert "allowed_review_codes" not in episodes
    assert "TEST SIMULATION" in report_json
    assert Path(report.artifact_paths["report_md"]).is_file()
    repeat = run_evaluation(settings, request, provider_factory=_eval_factory(suite))
    assert repeat.metrics.baseline.correct_autonomous_resolutions == (
        report.metrics.baseline.correct_autonomous_resolutions
    )
    assert repeat.metrics.memory.correct_autonomous_resolutions == (
        report.metrics.memory.correct_autonomous_resolutions
    )
    assert repeat.experiment_id != report.experiment_id


def test_paired_evaluation_arms_start_from_identical_ledgers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    demo = demo_new(settings, dataset=DatasetSplit.TEACHING)
    suite = load_split_suite(settings.data_dir, DatasetSplit.TEACHING, limit=2)
    request = EvaluationRequest(
        source_workspace_id=demo.workspace_id,
        split=DatasetSplit.TEACHING,
        case_limit=2,
        mode=ExecutionMode.TEST,
        deadline_seconds=120,
        output_dir=tmp_path / "artifacts" / "eval-iso",
    )
    report = run_evaluation(settings, request, provider_factory=_eval_factory(suite))
    by_case: dict[str, dict[str, str]] = {}
    for line in Path(report.artifact_paths["episodes"]).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        by_case.setdefault(row["case_id"], {})[row["arm"]] = row["opening_ledger_fingerprint"]
    assert by_case
    for fingerprints in by_case.values():
        assert fingerprints["baseline"]
        assert fingerprints["baseline"] == fingerprints["memory"]


def test_budget_interruption_writes_honest_partial_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    demo = demo_new(settings, dataset=DatasetSplit.TEACHING)
    suite = load_split_suite(settings.data_dir, DatasetSplit.TEACHING, limit=2)
    request = EvaluationRequest(
        source_workspace_id=demo.workspace_id,
        split=DatasetSplit.TEACHING,
        case_limit=2,
        mode=ExecutionMode.TEST,
        deadline_seconds=120,
        call_cap=1,
        output_dir=tmp_path / "artifacts" / "eval-partial",
    )
    report = run_evaluation(settings, request, provider_factory=_eval_factory(suite))
    assert report.state is EvaluationState.PARTIAL
    assert report.completed_count >= 1
    assert report.not_run_count >= 1
    assert report.budget_skipped_count == report.not_run_count
    skipped_rows = [
        row
        for row in report.per_case_paired_scores
        if row.baseline_correct is None or row.memory_correct is None
    ]
    assert skipped_rows
    assert report.metrics.memory is not None
    assert report.metrics.memory.budget_skipped.value >= 1
    markdown = Path(report.artifact_paths["report_md"]).read_text(encoding="utf-8")
    assert "not-run" in markdown or "budget-skipped" in markdown
    assert "PARTIAL" in markdown


def test_cli_evaluate_live_disabled_is_not_a_mock_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    demo = demo_new(settings, dataset=DatasetSplit.TEACHING)

    def _load():
        return settings

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    from precedent.cli import main

    code = main(
        [
            "evaluate",
            "--workspace",
            demo.workspace_id,
            "--split",
            "teaching",
            "--limit",
            "2",
            "--live",
        ]
    )
    assert code == 2
    artifacts = tmp_path / "artifacts" / "evaluations"
    assert not artifacts.exists() or not any(artifacts.iterdir())


def test_cli_evaluate_without_live_flag_is_invalid() -> None:
    from precedent.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["evaluate", "--workspace", "WS-TEACH-001", "--split", "teaching", "--limit", "2"])
    assert exc.value.code == 2
