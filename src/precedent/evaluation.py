"""Isolated-case executor, outcome scorer, and paired evaluation runner.

The executor never loads grading labels into ``RunContext``, tool results, or
model messages. The scorer reads evaluator-only oracles after a run returns.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from precedent.agent import InvestigationError, investigate_case
from precedent.config import Settings, live_readiness_code
from precedent.db import (
    get_case,
    get_payment,
    get_proposal,
    get_workspace,
    insert_evaluation_run,
    list_applications,
    list_invoices,
    list_payments,
    list_proposals_for_run,
    list_run_events,
    update_evaluation_run,
)
from precedent.fixtures import (
    DEFAULT_SEED,
    FixtureCaseManifest,
    SourcePackage,
    default_manifest_path,
    load_manifest,
    load_oracle,
    load_package,
    package_fingerprint,
)
from precedent.models import (
    AgentOutcome,
    Allocation,
    ApplicationRecord,
    BudgetLimits,
    CandidateCheckResults,
    CandidateEpisodeRecord,
    DatasetSplit,
    EpisodeDisposition,
    ErrorCode,
    EvaluationArmMetrics,
    EvaluationMetrics,
    EvaluationReport,
    EvaluationRequest,
    EvaluationState,
    ExecutionMode,
    ForbiddenPrecedentScope,
    FrozenEvaluationManifest,
    HintScope,
    LessonState,
    MemoryHintRef,
    MemoryMode,
    MemorySnapshot,
    MetricCount,
    OracleRecord,
    PairedCaseScore,
    PrecedentRecord,
    RunEventKind,
    RunResult,
    StrictModel,
    Usage,
    ValidationCode,
    WireFeeLookupHint,
    WorkspaceRecord,
    canonical_json,
    hash_canonical,
    sha256_utf8,
    utc_now_iso,
)
from precedent.providers import SHORT_RETRY_DELAY_SECONDS, Provider, ScriptedProvider
from precedent.tools import (
    empty_memory_snapshot,
    freeze_memory_snapshot,
    investigator_prompt_hash,
    tool_schema_hash,
)

SCORER_VERSION = "outcome_scorer_v1"
EVALUATION_ARTIFACT_VERSION = "evaluation_report_v1"
EVAL_WORKSPACE_PREFIX = "CTEST"
EXPERIMENT_WORKSPACE_PREFIX = "EVAL"
DEFAULT_EVALUATION_DEADLINE_SECONDS = 5400
SOLVABLE_DEV_AUTHORING_IDS = frozenset({"V01", "V03"})
REVIEW_DEV_AUTHORING_IDS = frozenset({"V02", "V04", "V05"})
CEDAR_CONTROL_AUTHORING_ID = "V03"
_READINESS_MESSAGES = {
    ErrorCode.LIVE_DISABLED: "Live mode is disabled (PRECEDENT_ENABLE_LIVE is false).",
    ErrorCode.MISSING_CREDENTIALS: "ANTHROPIC_API_KEY is absent.",
    ErrorCode.MODEL_UNAVAILABLE: "PRECEDENT_MODEL is unset.",
}
_BEHAVIOR_FILES = (
    "src/precedent/agent.py",
    "src/precedent/tools.py",
    "src/precedent/evidence.py",
    "src/precedent/ledger.py",
    "src/precedent/policies.py",
    "src/precedent/prompts/investigator.md",
)
EpisodeProviderFactory = Callable[[str, str], Provider]


class EvaluationError(Exception):
    """Invalid evaluation request or live-mode refusal. Safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IsolatedEpisode(StrictModel):
    case_id: str
    arm: Literal["baseline", "candidate"]
    workspace_id: str
    run_result: RunResult | None = None
    payment_applied: bool = False
    invoice_balances: dict[str, int] = Field(default_factory=dict)
    opening_invoice_balances: dict[str, int] = Field(default_factory=dict)
    opening_payment_applied: bool = False
    opening_application_count: int = 0
    opening_ledger_revision: int = 0
    opening_ledger_fingerprint: str = ""
    closing_ledger_fingerprint: str = ""
    new_applications: list[ApplicationRecord] = Field(default_factory=list)
    retrieved_precedent_ids: list[str] = Field(default_factory=list)
    retrieved_hints: list[WireFeeLookupHint] = Field(default_factory=list)
    used_precedent_ids: list[str] = Field(default_factory=list)
    snapshot_hints: list[MemoryHintRef] = Field(default_factory=list)
    validator_rejection_codes: list[ValidationCode] = Field(default_factory=list)
    duration_ms: int = 0
    technical_error: bool = False
    error_message: str | None = None
    order_index: int = 0
    disposition: EpisodeDisposition = EpisodeDisposition.COMPLETED
    skip_reason: str | None = None
    authoring_id: str | None = None
    complete_evidence_application_count: int = 0


class EpisodeScore(StrictModel):
    case_id: str
    arm: Literal["baseline", "candidate"]
    correct: bool | None = None
    reasons: list[str] = Field(default_factory=list)
    scope_violation: bool = False
    invalid_proposal: bool = False
    technical_error: bool = False
    outcome: AgentOutcome | None = None
    review_code: ValidationCode | None = None
    disposition: EpisodeDisposition = EpisodeDisposition.COMPLETED
    expected_outcome: AgentOutcome | None = None


def behavior_fingerprint(settings: Settings, *, policy_hash: str) -> str:
    """Hash investigator behavior, schemas, implementation, model, and budgets."""
    files: dict[str, str] = {}
    for relative in _BEHAVIOR_FILES:
        path = settings.repo_root / relative
        files[relative] = (
            sha256_utf8(path.read_text(encoding="utf-8")) if path.is_file() else "missing"
        )
    return hash_canonical(
        {
            "behavior_files": files,
            "budgets": {
                "max_case_seconds": settings.max_case_seconds,
                "max_model_calls": settings.max_model_calls,
                "max_output_tokens": settings.max_output_tokens,
                "max_tool_calls": settings.max_tool_calls,
                "request_timeout_seconds": settings.request_timeout_seconds,
            },
            "investigator_prompt_hash": investigator_prompt_hash(),
            "model": settings.model,
            "policy_hash": policy_hash,
            "retry_delay_seconds": SHORT_RETRY_DELAY_SECONDS,
            "scorer_version": SCORER_VERSION,
            "tool_schema_hash": tool_schema_hash(),
        }
    )


def load_dev_suite(
    data_dir: Path, seed: int = DEFAULT_SEED
) -> list[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]]:
    """Load candidate-development packages and their isolated oracles."""
    manifest = load_manifest(default_manifest_path(data_dir, seed))
    suite: list[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]] = []
    for entry in manifest.cases:
        if entry.split is not DatasetSplit.CANDIDATE:
            continue
        package = load_package(data_dir / entry.package_relpath)
        oracle = load_oracle(data_dir / "grading" / f"{entry.case_id}.json")
        suite.append((entry, package, oracle))
    if len(suite) != 5:
        raise ValueError(f"candidate-development suite must contain 5 cases, got {len(suite)}")
    return suite


def dev_suite_hash(data_dir: Path, seed: int = DEFAULT_SEED) -> str:
    items = []
    for entry, package, oracle in load_dev_suite(data_dir, seed):
        items.append(
            {
                "authoring_id": entry.authoring_id,
                "case_id": entry.case_id,
                "oracle": oracle.model_dump(mode="json"),
                "package": package_fingerprint(package),
                "source_hash": entry.source_hash,
            }
        )
    return hash_canonical({"items": items, "scorer_version": SCORER_VERSION, "seed": seed})


def candidate_memory_snapshot(
    record: PrecedentRecord, workspace: WorkspaceRecord
) -> MemorySnapshot:
    """Evaluator-only snapshot containing exactly this candidate."""
    return freeze_memory_snapshot(
        [
            MemoryHintRef(
                precedent_id=record.precedent_id,
                version=record.version,
                payload_hash=record.payload_hash,
                hint=record.hint,
            )
        ],
        source_workspace_id=record.owner_workspace_id,
        company_id=record.company_id,
        policy_id=workspace.policy.policy_id,
        policy_hash=record.policy_hash or workspace.policy_hash,
    )


def paired_arm_order(case_index: int) -> tuple[Literal["baseline", "candidate"], ...]:
    """Alternate which arm runs first by case index. Even: baseline first."""
    if case_index % 2 == 0:
        return ("baseline", "candidate")
    return ("candidate", "baseline")


def execute_isolated_case(
    connection: sqlite3.Connection,
    settings: Settings,
    package: SourcePackage,
    *,
    arm: Literal["baseline", "candidate"],
    mode: ExecutionMode,
    memory_snapshot: MemorySnapshot,
    provider: Provider | None = None,
    order_index: int = 0,
    workspace_prefix: str = EVAL_WORKSPACE_PREFIX,
    authoring_id: str | None = None,
) -> IsolatedEpisode:
    """Run one case in a fresh workspace. Does not load oracle labels."""
    from precedent.services import allocate_workspace_id, import_source_bundle, merge_packages

    bundle = merge_packages([package])
    workspace_id = allocate_workspace_id(connection, workspace_prefix)
    import_source_bundle(
        connection,
        workspace_id,
        f"Evaluation {package.case_id} {arm}",
        bundle,
    )
    opening_snap = financial_snapshot(connection, workspace_id)
    opening = dict(opening_snap["invoices"])
    opening_applied = any(item["applied"] for item in opening_snap["payments"].values())
    try:
        result = investigate_case(
            connection,
            settings,
            workspace_id,
            package.case_id,
            mode=mode,
            # Evaluator injection supplies the snapshot; do not load workspace memberships.
            memory_mode=MemoryMode.OFF,
            provider=provider,
            memory_snapshot=memory_snapshot,
        )
    except (InvestigationError, Exception) as exc:
        message = str(exc)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            message = f"{code}: {message}"
        return IsolatedEpisode(
            case_id=package.case_id,
            arm=arm,
            workspace_id=workspace_id,
            opening_invoice_balances=opening,
            invoice_balances=opening,
            opening_payment_applied=opening_applied,
            opening_application_count=len(opening_snap["applications"]),
            opening_ledger_revision=int(opening_snap["ledger_revision"]),
            opening_ledger_fingerprint=snapshot_fingerprint(opening_snap),
            closing_ledger_fingerprint=snapshot_fingerprint(opening_snap),
            technical_error=True,
            error_message=message[:1000],
            order_index=order_index,
            disposition=EpisodeDisposition.FAILED,
            authoring_id=authoring_id,
        )
    return _episode_from_run(
        connection,
        package.case_id,
        arm=arm,
        workspace_id=workspace_id,
        result=result,
        opening=opening,
        opening_snap=opening_snap,
        memory_snapshot=memory_snapshot,
        order_index=order_index,
        authoring_id=authoring_id,
    )


def score_episode(oracle: OracleRecord, episode: IsolatedEpisode) -> EpisodeScore:
    """Grade one finished episode against an evaluator-only oracle."""
    reasons: list[str] = []
    result = episode.run_result
    outcome = None if result is None else result.terminal_outcome
    review_code = None if result is None or result.review is None else result.review.reason_code
    technical = episode.technical_error or outcome is AgentOutcome.ERROR
    invalid_proposal = bool(episode.validator_rejection_codes)
    scope_violation = _scope_violation(oracle, episode)

    if technical:
        reasons.append(episode.error_message or "Episode ended in a technical error.")
    if outcome != oracle.expected_outcome:
        actual = None if outcome is None else outcome.value
        reasons.append(f"Outcome {actual} != {oracle.expected_outcome.value}.")
    if oracle.expected_outcome is AgentOutcome.REVIEW:
        if review_code is None or review_code not in oracle.allowed_review_codes:
            allowed = ", ".join(code.value for code in oracle.allowed_review_codes) or "(none)"
            actual = "(none)" if review_code is None else review_code.value
            reasons.append(f"Review code {actual} is not in {allowed}.")
        if episode.new_applications:
            reasons.append("Review case created a new financial application.")
    else:
        if result is None or result.application_id is None:
            reasons.append("Expected a committed resolution application.")
        allocations = _sorted_allocations(episode)
        expected = _sorted_oracle_allocations(oracle.expected_allocations)
        if allocations != expected:
            reasons.append("Committed allocations do not match the oracle.")

    for invoice_id, expected_cents in oracle.expected_ending_balances.items():
        actual = episode.invoice_balances.get(invoice_id)
        if actual != expected_cents:
            reasons.append(f"Invoice {invoice_id} ending balance {actual} != {expected_cents}.")
    if len(episode.new_applications) != oracle.expected_new_application_count:
        reasons.append(
            "New application count "
            f"{len(episode.new_applications)} != {oracle.expected_new_application_count}."
        )
    if episode.payment_applied is not oracle.expected_payment_applied:
        reasons.append(
            f"Payment applied {episode.payment_applied} != {oracle.expected_payment_applied}."
        )
    if oracle.must_preserve_other_records:
        for invoice_id, opening in episode.opening_invoice_balances.items():
            if invoice_id in oracle.expected_ending_balances:
                continue
            actual = episode.invoice_balances.get(invoice_id)
            if actual != opening:
                reasons.append(
                    f"Unrelated invoice {invoice_id} changed from {opening} to {actual}."
                )
    if scope_violation:
        reasons.append("An out-of-scope lesson was returned or used.")
    if invalid_proposal:
        codes = ", ".join(code.value for code in episode.validator_rejection_codes)
        reasons.append(f"Invalid financial proposal(s): {codes}.")

    correct = not reasons
    disposition = episode.disposition
    if episode.skip_reason:
        disposition = EpisodeDisposition.BUDGET_SKIPPED
        correct = None
    elif technical:
        disposition = _error_disposition(episode)
    elif outcome in {AgentOutcome.RESOLVED, AgentOutcome.REVIEW}:
        disposition = EpisodeDisposition.COMPLETED
    return EpisodeScore(
        case_id=episode.case_id,
        arm=episode.arm,
        correct=correct,
        reasons=reasons,
        scope_violation=scope_violation,
        invalid_proposal=invalid_proposal,
        technical_error=technical,
        outcome=outcome,
        review_code=review_code,
        disposition=disposition,
        expected_outcome=oracle.expected_outcome,
    )


def score_candidate_suite(
    *,
    authoring_by_case: dict[str, str],
    baseline_scores: dict[str, EpisodeScore],
    candidate_scores: dict[str, EpisodeScore],
    check_results: CandidateCheckResults,
    finished_without_error: bool,
) -> tuple[bool, list[str]]:
    """Return whether the draft passed the limited G4 candidate checks."""
    reasons: list[str] = []
    if not finished_without_error:
        reasons.append("Not all ten episodes finished without technical error.")
    for case_id, authoring_id in authoring_by_case.items():
        candidate = candidate_scores[case_id]
        baseline = baseline_scores[case_id]
        if authoring_id in SOLVABLE_DEV_AUTHORING_IDS and not candidate.correct:
            reasons.append(f"Candidate arm did not correctly resolve {authoring_id}.")
        if authoring_id in REVIEW_DEV_AUTHORING_IDS and not candidate.correct:
            reasons.append(f"Candidate arm did not correctly review {authoring_id}.")
        if authoring_id == CEDAR_CONTROL_AUTHORING_ID and candidate.scope_violation:
            reasons.append("Harbor lesson was returned for the different-customer case.")
        if candidate.invalid_proposal:
            reasons.append(f"Candidate arm made an invalid financial proposal on {authoring_id}.")
        if baseline.correct and not candidate.correct:
            reasons.append(f"Candidate arm regressed a correct baseline result on {authoring_id}.")
    if not check_results.validator_suite_passed:
        reasons.append("Deterministic validator checks are not recorded as passing.")
    if not check_results.lifecycle_suite_passed:
        reasons.append("Deterministic lifecycle checks are not recorded as passing.")
    return not reasons, reasons


def deterministic_lesson_checks() -> CandidateCheckResults:
    """Host checks recorded on the report; they do not call the provider."""
    validator_ok = _schema_rejects_unsupported_rules()
    required = {
        LessonState.DRAFT,
        LessonState.TESTING,
        LessonState.PASSED,
        LessonState.FAILED,
        LessonState.ACTIVE,
        LessonState.REJECTED,
        LessonState.RETIRED,
    }
    lifecycle_ok = required.issubset(set(LessonState))
    return CandidateCheckResults(
        validator_suite_passed=validator_ok,
        lifecycle_suite_passed=lifecycle_ok,
    )


def useful_improvement(
    baseline_scores: dict[str, EpisodeScore],
    candidate_scores: dict[str, EpisodeScore],
    baseline_episodes: Sequence[IsolatedEpisode],
    candidate_episodes: Sequence[IsolatedEpisode],
) -> bool | None:
    """Separate from eligibility: better results or fewer calls on both-correct cases."""
    positive = False
    negative = False
    for case_id, baseline in baseline_scores.items():
        candidate = candidate_scores[case_id]
        if (not baseline.correct) and candidate.correct:
            positive = True
        if baseline.correct and not candidate.correct:
            negative = True
    if negative:
        return False
    if positive:
        return True
    both_correct = [
        case_id
        for case_id, baseline in baseline_scores.items()
        if baseline.correct and candidate_scores[case_id].correct
    ]
    if not both_correct:
        return False
    baseline_by_case = {item.case_id: item for item in baseline_episodes}
    candidate_by_case = {item.case_id: item for item in candidate_episodes}

    def _calls(episode: IsolatedEpisode) -> int:
        if episode.run_result is None:
            return 0
        return episode.run_result.usage.model_attempts + episode.run_result.usage.tool_calls

    baseline_work = sum(_calls(baseline_by_case[case_id]) for case_id in both_correct)
    candidate_work = sum(_calls(candidate_by_case[case_id]) for case_id in both_correct)
    return candidate_work < baseline_work


def episode_record(episode: IsolatedEpisode, score: EpisodeScore) -> CandidateEpisodeRecord:
    result = episode.run_result
    return CandidateEpisodeRecord(
        case_id=episode.case_id,
        arm=episode.arm,
        workspace_id=episode.workspace_id,
        run_id=None if result is None else result.run_id,
        outcome=score.outcome,
        correct=score.correct,
        review_code=score.review_code,
        retrieved_precedent_ids=list(episode.retrieved_precedent_ids),
        validator_rejection_codes=list(episode.validator_rejection_codes),
        new_application_count=len(episode.new_applications),
        duration_ms=episode.duration_ms,
        technical_error=score.technical_error,
        scope_violation=score.scope_violation,
        invalid_proposal=score.invalid_proposal,
        order_index=episode.order_index,
    )


def _episode_from_run(
    connection: sqlite3.Connection,
    case_id: str,
    *,
    arm: Literal["baseline", "candidate"],
    workspace_id: str,
    result: RunResult,
    opening: dict[str, int],
    memory_snapshot: MemorySnapshot,
    order_index: int,
    opening_snap: dict[str, Any] | None = None,
    authoring_id: str | None = None,
) -> IsolatedEpisode:
    invoices = list_invoices(connection, workspace_id)
    applications = list_applications(connection, workspace_id)
    new_apps = [item for item in applications if not item.seeded]
    case = get_case(connection, workspace_id, case_id)
    payment = None
    if case is not None:
        payment = get_payment(connection, workspace_id, case.payment_id)
    events = list_run_events(connection, result.run_id)
    retrieved_ids: list[str] = []
    for event in events:
        if event.event_kind is not RunEventKind.PRECEDENT_RETRIEVED:
            continue
        raw_ids = event.payload.get("precedent_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                if isinstance(item, str) and item and item not in retrieved_ids:
                    retrieved_ids.append(item)
    hints_by_id = {item.precedent_id: item.hint for item in memory_snapshot.hints}
    retrieved_hints = [
        hints_by_id[item_id]
        for item_id in retrieved_ids
        if item_id in hints_by_id and hints_by_id[item_id] is not None
    ]
    used_ids: list[str] = []
    for proposal in list_proposals_for_run(connection, result.run_id):
        for precedent_id in proposal.payload.precedent_ids_used:
            if precedent_id not in used_ids:
                used_ids.append(precedent_id)
    rejections: list[ValidationCode] = []
    for event in events:
        if event.event_kind is RunEventKind.PROPOSAL_REJECTED:
            _append_rejection(rejections, event.payload)
        if event.event_kind is RunEventKind.TOOL_FAILED and event.payload.get("tool") in {
            "validate_resolution",
            "submit_resolution",
        }:
            _append_rejection(rejections, event.payload)
    technical = result.terminal_outcome is AgentOutcome.ERROR or result.error is not None
    closing_snap = financial_snapshot(connection, workspace_id)
    opening_data = (
        opening_snap
        if opening_snap is not None
        else {
            "invoices": opening,
            "payments": {},
            "applications": [],
            "ledger_revision": 0,
        }
    )
    complete_evidence = _complete_evidence_count(connection, new_apps)
    episode = IsolatedEpisode(
        case_id=case_id,
        arm=arm,
        workspace_id=workspace_id,
        run_result=result,
        payment_applied=False if payment is None else payment.applied,
        invoice_balances={item.invoice_id: item.outstanding_cents for item in invoices},
        opening_invoice_balances=opening,
        opening_payment_applied=any(
            item["applied"] for item in opening_data.get("payments", {}).values()
        ),
        opening_application_count=len(opening_data.get("applications", [])),
        opening_ledger_revision=int(opening_data.get("ledger_revision", 0)),
        opening_ledger_fingerprint=snapshot_fingerprint(opening_data),
        closing_ledger_fingerprint=snapshot_fingerprint(closing_snap),
        new_applications=new_apps,
        retrieved_precedent_ids=retrieved_ids,
        retrieved_hints=retrieved_hints,
        used_precedent_ids=used_ids,
        snapshot_hints=list(memory_snapshot.hints),
        validator_rejection_codes=rejections,
        duration_ms=result.usage.elapsed_ms,
        technical_error=technical,
        error_message=None if result.error is None else result.error.message,
        order_index=order_index,
        authoring_id=authoring_id,
        complete_evidence_application_count=complete_evidence,
    )
    return episode.model_copy(update={"disposition": _error_disposition(episode)})


def _append_rejection(codes: list[ValidationCode], payload: dict[str, Any]) -> None:
    raw = payload.get("error_code") or payload.get("code")
    if not isinstance(raw, str):
        report = payload.get("validation_report")
        if isinstance(report, dict) and report.get("valid") is False:
            issues = report.get("issues")
            if isinstance(issues, list) and issues:
                first = issues[0]
                if isinstance(first, dict):
                    raw = first.get("code")
    if not isinstance(raw, str):
        raw = ValidationCode.INVALID_SCHEMA.value
    try:
        code = ValidationCode(raw)
    except ValueError:
        return
    if code not in codes:
        codes.append(code)


def _sorted_allocations(episode: IsolatedEpisode) -> list[tuple[str, int, int]]:
    rows: list[Allocation] = []
    for application in episode.new_applications:
        rows.extend(application.allocations)
    return _sorted_oracle_allocations(rows)


def _sorted_oracle_allocations(allocations: Sequence[Allocation]) -> list[tuple[str, int, int]]:
    return sorted((item.invoice_id, item.cash_cents, item.fee_cents) for item in allocations)


def _scope_violation(oracle: OracleRecord, episode: IsolatedEpisode) -> bool:
    if not oracle.forbidden_precedent_scopes:
        return False
    hints_by_id = {
        item.precedent_id: item.hint for item in episode.snapshot_hints if item.hint is not None
    }
    inspected_ids = set(episode.retrieved_precedent_ids) | set(episode.used_precedent_ids)
    hints = list(episode.retrieved_hints)
    for item_id in inspected_ids:
        hint = hints_by_id.get(item_id)
        if hint is not None and hint not in hints:
            hints.append(hint)
    return any(
        _hint_matches_forbidden(hint, forbidden)
        for hint in hints
        for forbidden in oracle.forbidden_precedent_scopes
    )


def _hint_matches_forbidden(hint: WireFeeLookupHint, forbidden: ForbiddenPrecedentScope) -> bool:
    scope: HintScope = hint.scope
    return (
        hint.template_key == forbidden.template_key.value
        and scope.customer_id == forbidden.customer_id
        and scope.bank_account_id == forbidden.bank_account_id
        and scope.currency == forbidden.currency
        and scope.channel is forbidden.channel
    )


def _schema_rejects_unsupported_rules() -> bool:
    legal = {
        "title": "Follow Harbor's settlement ticket to the bank notice",
        "scope": {
            "customer_id": "CUST-HARBOR",
            "bank_account_id": "CASH-US-01",
            "currency": "USD",
            "channel": "WIRE",
        },
        "lookup": {
            "remittance_reference_field": "settlement_ticket",
            "bank_notice_reference_field": "transfer_reference",
            "search_terms": ["bank notice"],
        },
        "summary": "Use the current remittance ticket, then the fixed company policy.",
    }
    try:
        WireFeeLookupHint.model_validate(legal)
    except ValidationError:
        return False
    for extra in (
        {**legal, "threshold_cents": 3500},
        {**legal, "writeoff": True},
        {**legal, "policy_key": "max_receiving_wire_fee_cents"},
    ):
        try:
            WireFeeLookupHint.model_validate(extra)
        except ValidationError:
            continue
        return False
    return True


def load_split_suite(
    data_dir: Path,
    split: DatasetSplit,
    *,
    seed: int = DEFAULT_SEED,
    limit: int | None = None,
) -> list[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]]:
    """Load packages and evaluator-only oracles for one split."""
    if split is DatasetSplit.HELDOUT and limit is not None:
        raise EvaluationError(
            "INVALID_INPUT",
            "case_limit is not allowed for the held-out split",
        )
    if split is DatasetSplit.CANDIDATE and limit is None:
        return load_dev_suite(data_dir, seed)
    manifest = load_manifest(default_manifest_path(data_dir, seed))
    suite: list[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]] = []
    for entry in manifest.cases:
        if entry.split is not split:
            continue
        package = load_package(data_dir / entry.package_relpath)
        oracle = load_oracle(data_dir / "grading" / f"{entry.case_id}.json")
        suite.append((entry, package, oracle))
    if limit is not None:
        suite = suite[:limit]
    if not suite:
        raise EvaluationError("INVALID_INPUT", f"no cases available for split {split.value}")
    return suite


def split_suite_hash(
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
    *,
    seed: int,
    split: DatasetSplit,
) -> str:
    items = []
    for entry, package, _oracle in suite:
        items.append(
            {
                "authoring_id": entry.authoring_id,
                "case_id": entry.case_id,
                "package": package_fingerprint(package),
                "source_hash": entry.source_hash,
            }
        )
    return hash_canonical(
        {"items": items, "scorer_version": SCORER_VERSION, "seed": seed, "split": split.value}
    )


def oracle_suite_hash(
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
) -> str:
    items = [oracle.model_dump(mode="json") for _entry, _package, oracle in suite]
    return hash_canonical({"oracles": items, "scorer_version": SCORER_VERSION})


def implementation_source_hash(settings: Settings) -> str:
    files: dict[str, str] = {}
    for relative in _BEHAVIOR_FILES:
        path = settings.repo_root / relative
        files[relative] = (
            sha256_utf8(path.read_text(encoding="utf-8")) if path.is_file() else "missing"
        )
    return hash_canonical({"behavior_files": files})


def model_config_hash(settings: Settings) -> str:
    return hash_canonical(
        {
            "budgets": {
                "max_case_seconds": settings.max_case_seconds,
                "max_model_calls": settings.max_model_calls,
                "max_output_tokens": settings.max_output_tokens,
                "max_tool_calls": settings.max_tool_calls,
                "request_timeout_seconds": settings.request_timeout_seconds,
            },
            "model": settings.model,
            "retry_delay_seconds": SHORT_RETRY_DELAY_SECONDS,
        }
    )


def financial_snapshot(connection: sqlite3.Connection, workspace_id: str) -> dict[str, Any]:
    """Invoice balances, payment applied flags, applications, and ledger revision."""
    workspace = get_workspace(connection, workspace_id)
    invoices = list_invoices(connection, workspace_id)
    payments = list_payments(connection, workspace_id)
    applications = list_applications(connection, workspace_id)
    return {
        "applications": [
            {
                "allocations": sorted(
                    (row.invoice_id, row.cash_cents, row.fee_cents) for row in item.allocations
                ),
                "application_id": item.application_id,
                "payment_id": item.payment_id,
                "seeded": item.seeded,
            }
            for item in sorted(applications, key=lambda row: row.application_id)
        ],
        "invoices": {
            item.invoice_id: item.outstanding_cents
            for item in sorted(invoices, key=lambda row: row.invoice_id)
        },
        "ledger_revision": 0 if workspace is None else workspace.ledger_revision,
        "payments": {
            item.payment_id: {"amount_cents": item.amount_cents, "applied": item.applied}
            for item in sorted(payments, key=lambda row: row.payment_id)
        },
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Hash operational ledger facts. Generated application IDs are excluded."""
    return hash_canonical(
        {
            "applications": [
                {
                    "allocations": item["allocations"],
                    "payment_id": item["payment_id"],
                    "seeded": item["seeded"],
                }
                for item in snapshot.get("applications", [])
            ],
            "invoices": snapshot.get("invoices", {}),
            "ledger_revision": snapshot.get("ledger_revision", 0),
            "payments": snapshot.get("payments", {}),
        }
    )


def run_paired_evaluation(
    connection: sqlite3.Connection,
    settings: Settings,
    request: EvaluationRequest,
    *,
    provider_factory: EpisodeProviderFactory | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    seed: int = DEFAULT_SEED,
) -> EvaluationReport:
    """Run a paired baseline/memory experiment. Does not load oracles into the agent."""
    _validate_evaluation_request(settings, request, provider_factory)
    workspace = get_workspace(connection, request.source_workspace_id)
    if workspace is None:
        raise EvaluationError(
            "INVALID_INPUT",
            f"workspace {request.source_workspace_id} was not found",
        )
    suite = load_split_suite(
        settings.data_dir,
        request.split,
        seed=seed,
        limit=request.case_limit,
    )
    from precedent.learning import load_attached_memory_snapshot

    memory_snapshot, _notes = load_attached_memory_snapshot(connection, workspace, settings)
    empty = empty_memory_snapshot()
    experiment_id = f"EXP-{uuid.uuid4().hex}"
    experiment_dir = Path(request.output_dir) / experiment_id
    traces_dir = experiment_dir / "traces"
    experiment_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    frozen = _frozen_manifest(
        settings,
        request,
        workspace,
        suite,
        memory_snapshot,
        seed=seed,
        created_at=started_at,
    )
    artifact_paths = {
        "directory": str(experiment_dir),
        "episodes": str(experiment_dir / "episodes.jsonl"),
        "manifest": str(experiment_dir / "manifest.json"),
        "report_json": str(experiment_dir / "report.json"),
        "report_md": str(experiment_dir / "report.md"),
        "traces": str(traces_dir),
    }
    _write_manifest(
        experiment_dir / "manifest.json",
        experiment_id=experiment_id,
        request=request,
        frozen=frozen,
        memory_snapshot=memory_snapshot,
        oracle_hash=oracle_suite_hash(suite),
        scheduled_episodes=len(suite) * 2,
    )
    report = EvaluationReport(
        experiment_id=experiment_id,
        frozen_manifest=frozen,
        mode=request.mode,
        state=EvaluationState.RUNNING,
        scheduled_count=len(suite) * 2,
        completed_count=0,
        failed_count=0,
        not_run_count=len(suite) * 2,
        timed_out_count=0,
        budget_skipped_count=0,
        per_case_paired_scores=[],
        metrics=EvaluationMetrics(),
        artifact_paths=artifact_paths,
        started_at=started_at,
        finished_at=None,
        split=request.split,
    )
    insert_evaluation_run(
        connection,
        report,
        config=_request_config(request, experiment_id),
        snapshot_ids=[item.precedent_id for item in memory_snapshot.hints],
        split_hash=frozen.dataset_hash,
    )

    baseline_episodes: dict[str, IsolatedEpisode] = {}
    memory_episodes: dict[str, IsolatedEpisode] = {}
    baseline_scores: dict[str, EpisodeScore] = {}
    memory_scores: dict[str, EpisodeScore] = {}
    oracles = {oracle.case_id: oracle for _entry, _package, oracle in suite}
    order_index = 0
    started_mono = monotonic()
    model_attempts = 0
    skip_remaining = False
    skip_reason: str | None = None
    episodes_jsonl = experiment_dir / "episodes.jsonl"
    if episodes_jsonl.exists():
        episodes_jsonl.unlink()

    for case_index, (entry, package, oracle) in enumerate(suite):
        for arm in paired_arm_order(case_index):
            if not skip_remaining:
                elapsed = monotonic() - started_mono
                if elapsed >= request.deadline_seconds:
                    skip_remaining = True
                    skip_reason = (
                        "Experiment deadline reached before this episode started "
                        f"({request.deadline_seconds}s)."
                    )
                elif request.call_cap is not None and model_attempts >= request.call_cap:
                    skip_remaining = True
                    skip_reason = (
                        "Experiment call cap reached before this episode started "
                        f"({request.call_cap} model attempts)."
                    )
            snapshot = empty if arm == "baseline" else memory_snapshot
            if skip_remaining:
                episode = _skipped_episode(
                    package.case_id,
                    arm,
                    order_index=order_index,
                    reason=skip_reason or "Episode was not run.",
                    authoring_id=entry.authoring_id,
                )
                score = _skipped_score(oracle, episode)
            else:
                if request.mode is ExecutionMode.LIVE:
                    provider = None
                elif provider_factory is None:
                    raise EvaluationError(
                        ErrorCode.LIVE_DISABLED.value,
                        "TEST evaluation requires an explicit per-episode provider factory.",
                    )
                else:
                    provider = provider_factory(package.case_id, arm)
                episode = execute_isolated_case(
                    connection,
                    settings,
                    package,
                    arm=arm,
                    mode=request.mode,
                    memory_snapshot=snapshot,
                    provider=provider,
                    order_index=order_index,
                    workspace_prefix=EXPERIMENT_WORKSPACE_PREFIX,
                    authoring_id=entry.authoring_id,
                )
                score = score_episode(oracle, episode)
                if episode.run_result is not None:
                    model_attempts += episode.run_result.usage.model_attempts
                    _write_trace(traces_dir, connection, episode.run_result.run_id)
            if arm == "baseline":
                baseline_episodes[package.case_id] = episode
                baseline_scores[package.case_id] = score
            else:
                memory_episodes[package.case_id] = episode
                memory_scores[package.case_id] = score
            _append_episode_jsonl(episodes_jsonl, episode, score, entry.authoring_id)
            order_index += 1
            report = _refresh_report(
                report,
                suite=suite,
                oracles=oracles,
                baseline_episodes=baseline_episodes,
                memory_episodes=memory_episodes,
                baseline_scores=baseline_scores,
                memory_scores=memory_scores,
                finished=False,
            )
            _write_report_files(experiment_dir, report, suite, baseline_episodes, memory_episodes)
            update_evaluation_run(
                connection,
                report,
                config=_request_config(request, experiment_id),
                snapshot_ids=[item.precedent_id for item in memory_snapshot.hints],
                split_hash=frozen.dataset_hash,
            )

    finished_at = utc_now_iso()
    report = _refresh_report(
        report,
        suite=suite,
        oracles=oracles,
        baseline_episodes=baseline_episodes,
        memory_episodes=memory_episodes,
        baseline_scores=baseline_scores,
        memory_scores=memory_scores,
        finished=True,
        finished_at=finished_at,
    )
    _write_report_files(experiment_dir, report, suite, baseline_episodes, memory_episodes)
    update_evaluation_run(
        connection,
        report,
        config=_request_config(request, experiment_id),
        snapshot_ids=[item.precedent_id for item in memory_snapshot.hints],
        split_hash=frozen.dataset_hash,
    )
    return report


def format_evaluation_report(report: EvaluationReport) -> str:
    mode_label = "LIVE" if report.mode is ExecutionMode.LIVE else "TEST SIMULATION"
    lines = [
        f"Experiment: {report.experiment_id}",
        f"Mode: {report.mode.value} ({mode_label})",
        f"State: {report.state.value}",
        f"Split: {report.split.value if report.split is not None else '(unknown)'}",
        (
            "Episodes: "
            f"scheduled={report.scheduled_count} completed={report.completed_count} "
            f"failed={report.failed_count} timed_out={report.timed_out_count} "
            f"budget_skipped={report.budget_skipped_count} not_run={report.not_run_count}"
        ),
        f"Memory snapshot: {report.frozen_manifest.memory_snapshot_hash}",
        f"Dataset hash: {report.frozen_manifest.dataset_hash}",
        f"Behavior fingerprint: {report.frozen_manifest.behavior_fingerprint}",
    ]
    if report.artifact_paths.get("directory"):
        lines.append(f"Artifacts: {report.artifact_paths['directory']}")
    metrics = report.metrics
    if metrics.baseline is not None:
        lines.append("Baseline arm:")
        lines.extend(_arm_metric_lines(metrics.baseline))
    if metrics.memory is not None:
        lines.append("Memory arm:")
        lines.extend(_arm_metric_lines(metrics.memory))
    pos = ",".join(metrics.positive_transfer_case_ids) or "(none)"
    neg = ",".join(metrics.negative_transfer_case_ids) or "(none)"
    lines.append(f"Positive transfer: {pos}")
    lines.append(f"Negative transfer: {neg}")
    lines.append("Per-case rows:")
    for row in report.per_case_paired_scores:
        lines.append(
            "  "
            f"{row.case_id} baseline="
            f"{_score_label(row.baseline_correct, row.baseline_disposition)} "
            f"memory={_score_label(row.memory_correct, row.memory_disposition)}"
        )
    lines.append("This report does not claim statistical significance or human-time savings.")
    return "\n".join(lines)


def load_evaluation_report(path: Path) -> EvaluationReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "report" in payload:
        payload = payload["report"]
    return EvaluationReport.model_validate(payload)


def _validate_evaluation_request(
    settings: Settings,
    request: EvaluationRequest,
    provider_factory: EpisodeProviderFactory | None,
) -> None:
    if request.mode is ExecutionMode.HUMAN:
        raise EvaluationError("INVALID_INPUT", "HUMAN mode is not an evaluation mode.")
    if request.mode is ExecutionMode.LIVE:
        if isinstance(provider_factory, ScriptedProvider) or provider_factory is not None:
            raise EvaluationError(
                ErrorCode.INTERNAL_ERROR.value,
                "ScriptedProvider is never selected as a live-mode fallback.",
            )
        reason = live_readiness_code(settings)
        if reason is not None:
            code = ErrorCode(reason)
            raise EvaluationError(code.value, _READINESS_MESSAGES.get(code, reason))
        return
    if provider_factory is None:
        raise EvaluationError(
            ErrorCode.LIVE_DISABLED.value,
            "TEST evaluation requires an explicit per-episode provider factory.",
        )


def _frozen_manifest(
    settings: Settings,
    request: EvaluationRequest,
    workspace: WorkspaceRecord,
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
    memory_snapshot: MemorySnapshot,
    *,
    seed: int,
    created_at: str,
) -> FrozenEvaluationManifest:
    budgets = BudgetLimits(
        max_model_calls=settings.max_model_calls,
        max_tool_calls=settings.max_tool_calls,
        max_case_seconds=settings.max_case_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_output_tokens=settings.max_output_tokens,
    )
    return FrozenEvaluationManifest(
        dataset_hash=split_suite_hash(suite, seed=seed, split=request.split),
        memory_snapshot_hash=memory_snapshot.snapshot_hash,
        policy_hash=workspace.policy_hash,
        prompt_hash=investigator_prompt_hash(),
        tool_schema_hash=tool_schema_hash(),
        behavior_fingerprint=behavior_fingerprint(settings, policy_hash=workspace.policy_hash),
        application_source_version=implementation_source_hash(settings),
        budgets=budgets,
        model=settings.model,
        seed=seed,
        split=request.split,
        case_limit=request.case_limit,
        case_ids=[entry.case_id for entry, _package, _oracle in suite],
        source_workspace_id=request.source_workspace_id,
        model_config_hash=model_config_hash(settings),
        input_usd_per_million=(
            None if settings.input_usd_per_million is None else str(settings.input_usd_per_million)
        ),
        output_usd_per_million=(
            None
            if settings.output_usd_per_million is None
            else str(settings.output_usd_per_million)
        ),
        created_at=created_at,
        execution_mode=request.mode,
    )


def _write_manifest(
    path: Path,
    *,
    experiment_id: str,
    request: EvaluationRequest,
    frozen: FrozenEvaluationManifest,
    memory_snapshot: MemorySnapshot,
    oracle_hash: str,
    scheduled_episodes: int,
) -> None:
    payload = {
        "artifact_version": EVALUATION_ARTIFACT_VERSION,
        "created_at": frozen.created_at,
        "experiment_id": experiment_id,
        "frozen": frozen.model_dump(mode="json"),
        "limits": {
            "call_cap": request.call_cap,
            "case_limit": request.case_limit,
            "deadline_seconds": request.deadline_seconds,
        },
        "memory_snapshot": {
            "hint_count": len(memory_snapshot.hints),
            "precedent_ids": [item.precedent_id for item in memory_snapshot.hints],
            "snapshot_hash": memory_snapshot.snapshot_hash,
        },
        "mode": request.mode.value,
        "note": "Answer keys are not stored in this freeze payload.",
        "oracle_hash": oracle_hash,
        "scheduled_episodes": scheduled_episodes,
        "scorer_version": SCORER_VERSION,
        "split": request.split.value,
    }
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _append_episode_jsonl(
    path: Path,
    episode: IsolatedEpisode,
    score: EpisodeScore,
    authoring_id: str,
) -> None:
    arm_label = "memory" if episode.arm == "candidate" else "baseline"
    payload = {
        "arm": arm_label,
        "authoring_id": authoring_id,
        "case_id": episode.case_id,
        "correct": score.correct,
        "disposition": score.disposition.value,
        "duration_ms": episode.duration_ms,
        "opening_ledger_fingerprint": episode.opening_ledger_fingerprint,
        "order_index": episode.order_index,
        "outcome": None if score.outcome is None else score.outcome.value,
        "reasons": score.reasons,
        "retrieved_precedent_ids": episode.retrieved_precedent_ids,
        "run_id": None if episode.run_result is None else episode.run_result.run_id,
        "skip_reason": episode.skip_reason,
        "technical_error": score.technical_error,
        "validator_rejection_codes": [code.value for code in episode.validator_rejection_codes],
        "workspace_id": episode.workspace_id,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _write_trace(traces_dir: Path, connection: sqlite3.Connection, run_id: str) -> None:
    events = list_run_events(connection, run_id)
    payload = {
        "events": [event.model_dump(mode="json") for event in events[:200]],
        "run_id": run_id,
    }
    (traces_dir / f"{run_id}.json").write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _write_report_files(
    experiment_dir: Path,
    report: EvaluationReport,
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
    baseline_episodes: dict[str, IsolatedEpisode],
    memory_episodes: dict[str, IsolatedEpisode],
) -> None:
    payload = {
        "limitations": [
            "No statistical-significance claim from one small synthetic paired run.",
            "Review count is a proxy, not measured human minutes.",
            "Missing usage or token prices are unavailable, not zero.",
        ],
        "mode_label": "LIVE" if report.mode is ExecutionMode.LIVE else "TEST SIMULATION",
        "report": report.model_dump(mode="json"),
    }
    (experiment_dir / "report.json").write_text(canonical_json(payload) + "\n", encoding="utf-8")
    (experiment_dir / "report.md").write_text(
        _render_report_markdown(report, suite, baseline_episodes, memory_episodes),
        encoding="utf-8",
    )


def _refresh_report(
    report: EvaluationReport,
    *,
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
    oracles: dict[str, OracleRecord],
    baseline_episodes: dict[str, IsolatedEpisode],
    memory_episodes: dict[str, IsolatedEpisode],
    baseline_scores: dict[str, EpisodeScore],
    memory_scores: dict[str, EpisodeScore],
    finished: bool,
    finished_at: str | None = None,
) -> EvaluationReport:
    scheduled_cases = [entry.case_id for entry, _package, _oracle in suite]
    paired = []
    for case_id in scheduled_cases:
        baseline_score = baseline_scores.get(case_id)
        memory_score = memory_scores.get(case_id)
        baseline_episode = baseline_episodes.get(case_id)
        memory_episode = memory_episodes.get(case_id)
        paired.append(
            PairedCaseScore(
                case_id=case_id,
                baseline_run_id=_run_id(baseline_episode),
                memory_run_id=_run_id(memory_episode),
                baseline_outcome=None if baseline_score is None else baseline_score.outcome,
                memory_outcome=None if memory_score is None else memory_score.outcome,
                baseline_correct=None if baseline_score is None else baseline_score.correct,
                memory_correct=None if memory_score is None else memory_score.correct,
                baseline_disposition=_row_disposition(baseline_episode, baseline_score),
                memory_disposition=_row_disposition(memory_episode, memory_score),
                baseline_reasons=[] if baseline_score is None else list(baseline_score.reasons),
                memory_reasons=[] if memory_score is None else list(memory_score.reasons),
            )
        )
    all_episodes = [
        baseline_episodes[case_id] for case_id in scheduled_cases if case_id in baseline_episodes
    ]
    all_episodes.extend(
        memory_episodes[case_id] for case_id in scheduled_cases if case_id in memory_episodes
    )
    missing = report.scheduled_count - len(all_episodes)
    completed = sum(1 for item in all_episodes if item.disposition is EpisodeDisposition.COMPLETED)
    failed = sum(1 for item in all_episodes if item.disposition is EpisodeDisposition.FAILED)
    timed_out = sum(1 for item in all_episodes if item.disposition is EpisodeDisposition.TIMED_OUT)
    skipped = sum(
        1 for item in all_episodes if item.disposition is EpisodeDisposition.BUDGET_SKIPPED
    )
    not_run = skipped + missing
    if finished:
        if not_run:
            state = EvaluationState.PARTIAL
        elif failed or timed_out:
            state = EvaluationState.COMPLETE
        else:
            state = EvaluationState.COMPLETE
    else:
        state = EvaluationState.RUNNING if not_run else EvaluationState.COMPLETE
    metrics = _aggregate_metrics(
        scheduled_cases,
        oracles,
        baseline_episodes,
        memory_episodes,
        baseline_scores,
        memory_scores,
    )
    return report.model_copy(
        update={
            "budget_skipped_count": skipped,
            "completed_count": completed,
            "failed_count": failed,
            "finished_at": finished_at if finished else report.finished_at,
            "metrics": metrics,
            "not_run_count": not_run,
            "per_case_paired_scores": paired,
            "state": state,
            "timed_out_count": timed_out,
        }
    )


def _aggregate_metrics(
    scheduled_cases: Sequence[str],
    oracles: dict[str, OracleRecord],
    baseline_episodes: dict[str, IsolatedEpisode],
    memory_episodes: dict[str, IsolatedEpisode],
    baseline_scores: dict[str, EpisodeScore],
    memory_scores: dict[str, EpisodeScore],
) -> EvaluationMetrics:
    baseline = _arm_metrics(scheduled_cases, oracles, baseline_episodes, baseline_scores)
    memory = _arm_metrics(scheduled_cases, oracles, memory_episodes, memory_scores)
    positive: list[str] = []
    negative: list[str] = []
    both_correct: list[str] = []
    for case_id in scheduled_cases:
        base = baseline_scores.get(case_id)
        mem = memory_scores.get(case_id)
        if base is None or mem is None:
            continue
        if base.correct is True and mem.correct is True:
            both_correct.append(case_id)
        if base.correct is False and mem.correct is True:
            positive.append(case_id)
        if base.correct is True and mem.correct is False:
            negative.append(case_id)
    baseline_both = _work_for_cases(both_correct, baseline_episodes)
    memory_both = _work_for_cases(both_correct, memory_episodes)
    return EvaluationMetrics(
        correct_autonomous_resolutions=memory.correct_autonomous_resolutions,
        incorrect_automatic_resolutions=memory.incorrect_automatic_resolutions,
        correct_reviews=memory.correct_reviews,
        unnecessary_reviews=memory.unnecessary_reviews,
        technical_errors=memory.technical_errors,
        positive_transfer_case_ids=positive,
        negative_transfer_case_ids=negative,
        rejected_financial_proposals=memory.rejected_financial_proposals,
        scope_violations=memory.scope_violations,
        evidence_completeness=memory.evidence_completeness,
        work_performed=memory.work_performed,
        estimated_api_cost=memory.estimated_api_cost,
        baseline=baseline,
        memory=memory,
        both_correct_case_ids=both_correct,
        baseline_both_correct_work=baseline_both,
        memory_both_correct_work=memory_both,
    )


def _arm_metrics(
    scheduled_cases: Sequence[str],
    oracles: dict[str, OracleRecord],
    episodes: dict[str, IsolatedEpisode],
    scores: dict[str, EpisodeScore],
) -> EvaluationArmMetrics:
    all_n = len(scheduled_cases)
    resolvable = [
        case_id
        for case_id in scheduled_cases
        if oracles[case_id].expected_outcome is AgentOutcome.RESOLVED
    ]
    reviews = [
        case_id
        for case_id in scheduled_cases
        if oracles[case_id].expected_outcome is AgentOutcome.REVIEW
    ]
    correct_resolved = 0
    incorrect_auto = 0
    correct_review = 0
    unnecessary_review = 0
    technical = 0
    timed_out = 0
    skipped = 0
    rejections = 0
    rejections_by_code: dict[str, int] = {}
    scope = 0
    scope_runs: list[str] = []
    evidence_ok = 0
    evidence_apps = 0
    usages: list[Usage] = []
    for case_id in scheduled_cases:
        oracle = oracles[case_id]
        episode = episodes.get(case_id)
        score = scores.get(case_id)
        if episode is None or score is None or score.correct is None:
            skipped += 1
            continue
        if score.disposition is EpisodeDisposition.BUDGET_SKIPPED:
            skipped += 1
            continue
        if score.disposition is EpisodeDisposition.TIMED_OUT:
            timed_out += 1
            technical += 1
        elif score.technical_error or score.disposition is EpisodeDisposition.FAILED:
            technical += 1
        if (
            oracle.expected_outcome is AgentOutcome.RESOLVED
            and score.outcome is AgentOutcome.RESOLVED
            and score.correct is True
        ):
            correct_resolved += 1
        if score.outcome is AgentOutcome.RESOLVED and score.correct is False:
            incorrect_auto += 1
        if (
            oracle.expected_outcome is AgentOutcome.REVIEW
            and score.outcome is AgentOutcome.REVIEW
            and score.correct is True
        ):
            correct_review += 1
        if (
            oracle.expected_outcome is AgentOutcome.RESOLVED
            and score.outcome is AgentOutcome.REVIEW
        ):
            unnecessary_review += 1
        if episode is not None:
            rejections += len(episode.validator_rejection_codes)
            for code in episode.validator_rejection_codes:
                rejections_by_code[code.value] = rejections_by_code.get(code.value, 0) + 1
            evidence_apps += len(episode.new_applications)
            evidence_ok += episode.complete_evidence_application_count
            if episode.run_result is not None:
                usages.append(episode.run_result.usage)
        if score.scope_violation:
            scope += 1
            run_id = _run_id(episode)
            if run_id is not None:
                scope_runs.append(run_id)
    evidence = None
    if evidence_apps:
        evidence = MetricCount(value=evidence_ok, denominator=evidence_apps)
    work = _combine_usage(usages)
    return EvaluationArmMetrics(
        correct_autonomous_resolutions=MetricCount(value=correct_resolved, denominator=all_n),
        correct_autonomous_resolutions_resolvable=MetricCount(
            value=correct_resolved, denominator=len(resolvable)
        ),
        incorrect_automatic_resolutions=MetricCount(value=incorrect_auto, denominator=all_n),
        correct_reviews=MetricCount(value=correct_review, denominator=len(reviews)),
        unnecessary_reviews=MetricCount(value=unnecessary_review, denominator=len(resolvable)),
        technical_errors=MetricCount(value=technical, denominator=all_n),
        timed_out=MetricCount(value=timed_out, denominator=all_n),
        budget_skipped=MetricCount(value=skipped, denominator=all_n),
        rejected_financial_proposals=rejections,
        rejected_financial_proposals_by_code=rejections_by_code,
        scope_violations=scope,
        scope_violation_run_ids=scope_runs,
        evidence_completeness=evidence,
        work_performed=work,
        estimated_api_cost=work.estimated_cost_usd,
        price_rate_provenance=work.price_rate_provenance,
    )


def _work_for_cases(case_ids: Sequence[str], episodes: dict[str, IsolatedEpisode]) -> Usage | None:
    usages = []
    for case_id in case_ids:
        episode = episodes.get(case_id)
        if episode is None or episode.run_result is None:
            return None
        usages.append(episode.run_result.usage)
    if not usages:
        return None
    return _combine_usage(usages)


def _combine_usage(usages: Sequence[Usage]) -> Usage:
    if not usages:
        return Usage(
            model_attempts=0,
            tool_calls=0,
            input_tokens=None,
            output_tokens=None,
            elapsed_ms=0,
            estimated_cost_usd=None,
            price_rate_provenance=None,
        )
    inputs = [item.input_tokens for item in usages]
    outputs = [item.output_tokens for item in usages]
    costs = [item.estimated_cost_usd for item in usages]
    provenances = {item.price_rate_provenance for item in usages if item.price_rate_provenance}
    return Usage(
        model_attempts=sum(item.model_attempts for item in usages),
        tool_calls=sum(item.tool_calls for item in usages),
        input_tokens=_sum_optional_ints(inputs),
        output_tokens=_sum_optional_ints(outputs),
        elapsed_ms=sum(item.elapsed_ms for item in usages),
        estimated_cost_usd=_sum_optional_decimals(costs),
        price_rate_provenance=next(iter(provenances)) if len(provenances) == 1 else None,
    )


def _sum_optional_ints(values: Sequence[int | None]) -> int | None:
    if not values or any(item is None for item in values):
        return None
    return sum(item for item in values if item is not None)


def _sum_optional_decimals(values: Sequence[Decimal | None]) -> Decimal | None:
    if not values or any(item is None for item in values):
        return None
    total = Decimal("0")
    for item in values:
        assert item is not None
        total += item
    return total


def _skipped_episode(
    case_id: str,
    arm: Literal["baseline", "candidate"],
    *,
    order_index: int,
    reason: str,
    authoring_id: str | None,
) -> IsolatedEpisode:
    return IsolatedEpisode(
        case_id=case_id,
        arm=arm,
        workspace_id=f"WS-SKIP-{order_index:03d}",
        technical_error=False,
        error_message=reason,
        order_index=order_index,
        disposition=EpisodeDisposition.BUDGET_SKIPPED,
        skip_reason=reason,
        authoring_id=authoring_id,
    )


def _skipped_score(oracle: OracleRecord, episode: IsolatedEpisode) -> EpisodeScore:
    reason = episode.skip_reason or "Episode was not run."
    return EpisodeScore(
        case_id=episode.case_id,
        arm=episode.arm,
        correct=None,
        reasons=[reason],
        technical_error=False,
        outcome=None,
        disposition=EpisodeDisposition.BUDGET_SKIPPED,
        expected_outcome=oracle.expected_outcome,
    )


def _error_disposition(episode: IsolatedEpisode) -> EpisodeDisposition:
    if episode.skip_reason:
        return EpisodeDisposition.BUDGET_SKIPPED
    result = episode.run_result
    if result is not None and result.error is not None:
        if result.error.code is ErrorCode.PROVIDER_TIMEOUT:
            return EpisodeDisposition.TIMED_OUT
        if result.error.code is ErrorCode.BUDGET_EXHAUSTED:
            return EpisodeDisposition.FAILED
    if episode.technical_error or (
        result is not None and result.terminal_outcome is AgentOutcome.ERROR
    ):
        return EpisodeDisposition.FAILED
    return EpisodeDisposition.COMPLETED


def _complete_evidence_count(
    connection: sqlite3.Connection, applications: Sequence[ApplicationRecord]
) -> int:
    complete = 0
    for application in applications:
        if application.seeded or application.proposal_id is None:
            continue
        proposal = get_proposal(connection, application.proposal_id)
        if proposal is None:
            continue
        report = proposal.validation_report
        if report.valid and report.evidence_hashes:
            complete += 1
    return complete


def _request_config(request: EvaluationRequest, experiment_id: str) -> dict[str, Any]:
    return {
        "call_cap": request.call_cap,
        "case_limit": request.case_limit,
        "deadline_seconds": request.deadline_seconds,
        "experiment_id": experiment_id,
        "mode": request.mode.value,
        "output_dir": str(request.output_dir),
        "source_workspace_id": request.source_workspace_id,
        "split": request.split.value,
    }


def _run_id(episode: IsolatedEpisode | None) -> str | None:
    if episode is None or episode.run_result is None:
        return None
    return episode.run_result.run_id


def _row_disposition(
    episode: IsolatedEpisode | None, score: EpisodeScore | None
) -> EpisodeDisposition:
    if score is not None:
        return score.disposition
    if episode is not None:
        return episode.disposition
    return EpisodeDisposition.NOT_RUN


def _score_label(correct: bool | None, disposition: EpisodeDisposition | None) -> str:
    if disposition is None or disposition in {
        EpisodeDisposition.BUDGET_SKIPPED,
        EpisodeDisposition.NOT_RUN,
    }:
        return "not-run"
    if correct is None:
        return disposition.value.lower()
    return "correct" if correct else "incorrect"


def _arm_metric_lines(arm: EvaluationArmMetrics) -> list[str]:
    return [
        "  "
        + _metric_line(
            "correct autonomous resolutions",
            arm.correct_autonomous_resolutions,
        ),
        "  "
        + _metric_line(
            "correct autonomous of resolvable",
            arm.correct_autonomous_resolutions_resolvable,
        ),
        "  " + _metric_line("incorrect automatic resolutions", arm.incorrect_automatic_resolutions),
        "  " + _metric_line("correct reviews", arm.correct_reviews),
        "  " + _metric_line("unnecessary reviews", arm.unnecessary_reviews),
        "  " + _metric_line("technical errors", arm.technical_errors),
        "  " + _metric_line("timed out", arm.timed_out),
        "  " + _metric_line("budget skipped", arm.budget_skipped),
        f"  rejected financial proposals: {arm.rejected_financial_proposals}",
        f"  scope violations: {arm.scope_violations}",
        "  " + _optional_metric_line("evidence completeness", arm.evidence_completeness),
        (
            "  work: "
            f"model_attempts={arm.work_performed.model_attempts} "
            f"tool_calls={arm.work_performed.tool_calls} "
            f"elapsed_ms={arm.work_performed.elapsed_ms} "
            f"tokens_in={_fmt_optional(arm.work_performed.input_tokens)} "
            f"tokens_out={_fmt_optional(arm.work_performed.output_tokens)}"
        ),
        f"  estimated API cost: {_fmt_cost(arm.estimated_api_cost, arm.price_rate_provenance)}",
    ]


def _metric_line(label: str, count: MetricCount) -> str:
    if count.denominator == 0:
        return f"{label}: N/A (denominator 0)"
    pct = 100.0 * count.value / count.denominator
    return f"{label}: {count.value}/{count.denominator} ({pct:.0f}%)"


def _optional_metric_line(label: str, count: MetricCount | None) -> str:
    if count is None:
        return f"{label}: undefined (no applications)"
    return _metric_line(label, count)


def _fmt_optional(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _fmt_cost(cost: Decimal | None, provenance: str | None) -> str:
    if cost is None:
        return "unavailable"
    source = provenance or "configured rates"
    return f"{cost} USD ({source})"


def _render_report_markdown(
    report: EvaluationReport,
    suite: Sequence[tuple[FixtureCaseManifest, SourcePackage, OracleRecord]],
    baseline_episodes: dict[str, IsolatedEpisode],
    memory_episodes: dict[str, IsolatedEpisode],
) -> str:
    mode_label = "LIVE" if report.mode is ExecutionMode.LIVE else "TEST SIMULATION"
    lines = [
        f"# Evaluation {report.experiment_id}",
        "",
        f"- Mode: `{report.mode.value}` ({mode_label})",
        f"- State: `{report.state.value}`",
        f"- Split: `{report.split.value if report.split is not None else 'unknown'}`",
        f"- Started: {report.started_at or '(unknown)'}",
        f"- Finished: {report.finished_at or '(running)'}",
        (
            f"- Episodes: scheduled {report.scheduled_count}, completed "
            f"{report.completed_count}, failed {report.failed_count}, timed out "
            f"{report.timed_out_count}, budget-skipped {report.budget_skipped_count}, "
            f"not-run {report.not_run_count}"
        ),
        f"- Dataset hash: `{report.frozen_manifest.dataset_hash}`",
        f"- Memory snapshot hash: `{report.frozen_manifest.memory_snapshot_hash}`",
        f"- Prompt hash: `{report.frozen_manifest.prompt_hash}`",
        f"- Tool schema hash: `{report.frozen_manifest.tool_schema_hash}`",
        f"- Behavior fingerprint: `{report.frozen_manifest.behavior_fingerprint}`",
        f"- Source version: `{report.frozen_manifest.application_source_version}`",
        "",
        "## Metrics",
        "",
    ]
    if report.metrics.baseline is not None:
        lines.append("### Baseline (memory off)")
        lines.append("")
        lines.extend(f"- {text.strip()}" for text in _arm_metric_lines(report.metrics.baseline))
        lines.append("")
    if report.metrics.memory is not None:
        lines.append("### Memory (frozen approved lessons)")
        lines.append("")
        lines.extend(f"- {text.strip()}" for text in _arm_metric_lines(report.metrics.memory))
        lines.append("")
    both = report.metrics.both_correct_case_ids
    lines.append("### Paired comparison")
    lines.append("")
    if both:
        base_work = report.metrics.baseline_both_correct_work
        mem_work = report.metrics.memory_both_correct_work
        lines.append(f"- Both arms correct: {', '.join(both)}")
        if base_work is not None and mem_work is not None:
            lines.append(
                "- Work on both-correct subset: "
                f"baseline attempts {base_work.model_attempts}/tools {base_work.tool_calls}/"
                f"{base_work.elapsed_ms}ms; memory attempts {mem_work.model_attempts}/tools "
                f"{mem_work.tool_calls}/{mem_work.elapsed_ms}ms"
            )
    else:
        lines.append("- Both arms correct: none (N/A for efficiency comparison)")
    pos = report.metrics.positive_transfer_case_ids or ["(none)"]
    neg = report.metrics.negative_transfer_case_ids or ["(none)"]
    lines.append(f"- Positive transfer: {', '.join(pos)}")
    lines.append(f"- Negative transfer: {', '.join(neg)}")
    lines.append("")
    lines.append("## Per-case rows")
    lines.append("")
    lines.append("| Case | Authoring | Baseline | Memory | Opening ledger match |")
    lines.append("|---|---|---|---|---|")
    authoring = {entry.case_id: entry.authoring_id for entry, _package, _oracle in suite}
    for row in report.per_case_paired_scores:
        base_ep = baseline_episodes.get(row.case_id)
        mem_ep = memory_episodes.get(row.case_id)
        match = "n/a"
        if (
            base_ep is not None
            and mem_ep is not None
            and base_ep.opening_ledger_fingerprint
            and mem_ep.opening_ledger_fingerprint
        ):
            match = (
                "yes"
                if base_ep.opening_ledger_fingerprint == mem_ep.opening_ledger_fingerprint
                else "NO"
            )
        lines.append(
            f"| {row.case_id} | {authoring.get(row.case_id, '?')} | "
            f"{_score_label(row.baseline_correct, row.baseline_disposition)} | "
            f"{_score_label(row.memory_correct, row.memory_disposition)} | {match} |"
        )
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append("- Answer keys were loaded only after each episode returned.")
    lines.append("- Missing values are listed as unavailable or not-run, not replaced with zero.")
    lines.append("- No statistical significance is claimed.")
    lines.append("- Invoice face value is not money saved. No labor-cost estimate is included.")
    lines.append("")
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_EVALUATION_DEADLINE_SECONDS",
    "EVALUATION_ARTIFACT_VERSION",
    "EVAL_WORKSPACE_PREFIX",
    "EXPERIMENT_WORKSPACE_PREFIX",
    "EpisodeProviderFactory",
    "EpisodeScore",
    "EvaluationError",
    "IsolatedEpisode",
    "SCORER_VERSION",
    "behavior_fingerprint",
    "candidate_memory_snapshot",
    "deterministic_lesson_checks",
    "dev_suite_hash",
    "episode_record",
    "execute_isolated_case",
    "financial_snapshot",
    "format_evaluation_report",
    "implementation_source_hash",
    "load_dev_suite",
    "load_evaluation_report",
    "load_split_suite",
    "model_config_hash",
    "paired_arm_order",
    "run_paired_evaluation",
    "score_candidate_suite",
    "score_episode",
    "snapshot_fingerprint",
    "split_suite_hash",
    "useful_improvement",
]
