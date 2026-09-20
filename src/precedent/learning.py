"""Controller corrections, tested lessons, and approved-memory retrieval.

Saving a correction, applying a human proposal, and proposing a reusable draft
are separate actions. Drafts stay inactive until an explicit compatible
activation. Ordinary investigation loads ACTIVE compatible lessons attached in
``workspace_memory``. The compiler cannot apply money, activate a lesson, or
amend company policy.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from precedent.config import Settings, live_readiness_code
from precedent.db import (
    PersistenceError,
    get_active_precedent_for_family,
    get_application_by_payment,
    get_candidate_test,
    get_case,
    get_correction,
    get_document,
    get_payment,
    get_precedent,
    get_proposal,
    get_run,
    get_workspace,
    insert_candidate_test,
    insert_correction,
    insert_precedent,
    insert_run,
    insert_workspace_memory,
    list_run_events,
    list_workspace_memory,
    next_precedent_version,
    transaction,
    update_candidate_test,
    update_correction_application,
    update_precedent_lifecycle,
)
from precedent.evaluation import (
    EpisodeProviderFactory,
    IsolatedEpisode,
    behavior_fingerprint,
    candidate_memory_snapshot,
    deterministic_lesson_checks,
    dev_suite_hash,
    episode_record,
    execute_isolated_case,
    load_dev_suite,
    paired_arm_order,
    score_candidate_suite,
    score_episode,
    useful_improvement,
)
from precedent.evidence import read_document
from precedent.ledger import apply_proposal, store_proposal
from precedent.models import (
    ApplicationResult,
    ApplicationStatus,
    BudgetLimits,
    CandidateEpisodeRecord,
    CandidateTestReport,
    CandidateTestState,
    CorrectionInput,
    CorrectionRecord,
    Document,
    DocumentKind,
    ErrorCode,
    EvidenceRecord,
    ExecutionMode,
    HintLookup,
    HintScope,
    LessonDraftResult,
    LessonDraftStatus,
    LessonLifecycleResult,
    LessonState,
    LessonTestResult,
    MemoryEligibilityNote,
    MemoryHintRef,
    MemorySnapshot,
    OpenedEvidence,
    PairedCaseScore,
    PrecedentRecord,
    ProposalRecord,
    RemittanceFacts,
    ResolutionProposal,
    ResolutionType,
    RunContext,
    RunEventKind,
    RunRecord,
    RunState,
    StrictModel,
    ToolCall,
    ToolError,
    ToolResult,
    Usage,
    ValidationReport,
    WireFeeLookupHint,
    WorkspaceRecord,
    hash_canonical,
    sha256_utf8,
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
from precedent.tools import empty_memory_snapshot, freeze_memory_snapshot, record_run_event

PROMPT_PATH = Path(__file__).parent / "prompts" / "lesson_compiler.md"
HUMAN_PROMPT_VERSION = "human-correction-v1"
LESSON_COMPILER_VERSION = "lesson_compiler_v1"
COMPILER_DEADLINE_SECONDS = 60
MAX_COMPILER_REQUESTS = 2
DEFAULT_CONTROLLER_ACTOR = "demo_controller"
OPEN_EVIDENCE_TOOL = "open_evidence"
PROPOSE_TOOL = "propose_precedent"
CANNOT_GENERALIZE_TOOL = "cannot_generalize"
_READINESS_MESSAGES = {
    ErrorCode.LIVE_DISABLED: "Live mode is disabled (PRECEDENT_ENABLE_LIVE is false).",
    ErrorCode.MISSING_CREDENTIALS: "ANTHROPIC_API_KEY is absent.",
    ErrorCode.MODEL_UNAVAILABLE: "PRECEDENT_MODEL is unset.",
}
_FORBIDDEN_INSTRUCTION = (
    (
        re.compile(r"write[\s-]?off", re.IGNORECASE),
        "Write-off instructions conflict with company policy and cannot become a lesson.",
    ),
    (
        re.compile(r"all short payments", re.IGNORECASE),
        "A blanket short-payment rule cannot be represented by wire_fee_lookup_v1.",
    ),
    (
        re.compile(r"\b(every|all|any)\s+customers?\b", re.IGNORECASE),
        "Lesson scope must be narrowed to the verified customer; wildcards are not allowed.",
    ),
    (
        re.compile(
            r"(amend|change|raise|increase).{0,40}(policy|fee limit|fee cap)",
            re.IGNORECASE,
        ),
        "A lesson cannot amend company fee policy or financial authority.",
    ),
    (
        re.compile(r"\beval\s*\(|\bexec\s*\(|\bSELECT\b|\bDROP\b", re.IGNORECASE),
        "Executable or SQL instructions cannot be stored as a lesson.",
    ),
)
_WILDCARD_TERM = re.compile(r"[*?\[\]{}|()\\]|(\.\*)")


class LearningError(Exception):
    """Input or configuration failure before a learning action. Safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProposePrecedentArgs(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    scope: HintScope
    lookup: HintLookup
    summary: str = Field(min_length=1, max_length=500)


class CannotGeneralizeArgs(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)


def lesson_compiler_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def lesson_compiler_hash() -> str:
    return sha256_utf8(lesson_compiler_text())


def compiler_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": PROPOSE_TOOL,
            "description": (
                "Propose a wire_fee_lookup_v1 investigation hint with the exact "
                "verified scope. This only stores candidate data."
            ),
            "input_schema": ProposePrecedentArgs.model_json_schema(),
        },
        {
            "name": CANNOT_GENERALIZE_TOOL,
            "description": (
                "Refuse to draft a lesson when the correction is unsupported, "
                "too broad, or conflicts with company policy."
            ),
            "input_schema": CannotGeneralizeArgs.model_json_schema(),
        },
    ]


def compiler_tool_hash() -> str:
    return hash_canonical(compiler_tool_definitions())


def candidate_payload_hash(
    hint: WireFeeLookupHint,
    *,
    correction_id: str,
    policy_hash: str,
    provenance: Sequence[OpenedEvidence],
    company_id: str,
    owner_workspace_id: str,
) -> str:
    """Hash the typed hint plus immutable correction/policy/provenance binding."""
    return hash_canonical(
        {
            "company_id": company_id,
            "correction_id": correction_id,
            "hint": hint.model_dump(mode="json"),
            "owner_workspace_id": owner_workspace_id,
            "policy_hash": policy_hash,
            "provenance": [
                {"document_id": item.document_id, "sha256": item.sha256}
                for item in sorted(provenance, key=lambda row: row.document_id)
            ],
        }
    )


def format_lesson_draft(
    correction: CorrectionRecord,
    result: LessonDraftResult,
    precedent: PrecedentRecord | None = None,
) -> str:
    """CLI-readable original correction, scope, evidence, and proposed lookup."""
    lines = [
        f"Status: {result.status.value}",
        f"Correction: {correction.correction_id}",
        f"Original correction: {correction.text}",
        f"Evidence: {', '.join(correction.cited_document_ids)}",
        f"Lesson eligible: {str(correction.lesson_eligibility).lower()}",
    ]
    if correction.eligibility_reason:
        lines.append(f"Eligibility reason: {correction.eligibility_reason}")
    if precedent is not None:
        hint = precedent.hint
        lines.extend(
            [
                f"Precedent: {precedent.precedent_id}",
                f"Version: {precedent.version}",
                f"Lesson status: {precedent.status.value}",
                f"Title: {hint.title}",
                (
                    "Scope: "
                    f"customer={hint.scope.customer_id} "
                    f"account={hint.scope.bank_account_id} "
                    f"currency={hint.scope.currency} "
                    f"channel={hint.scope.channel.value}"
                ),
                (
                    "Lookup: remittance."
                    f"{hint.lookup.remittance_reference_field.value} -> notice."
                    f"{hint.lookup.bank_notice_reference_field}"
                ),
                f"Search terms: {', '.join(hint.lookup.search_terms) or '(none)'}",
                f"Summary: {hint.summary}",
                f"Payload hash: {precedent.payload_hash}",
            ]
        )
        metadata = precedent.compiler_metadata
        if metadata:
            lines.append(
                "Compiler: "
                f"provider={metadata.get('provider')} "
                f"model={metadata.get('model')} "
                f"prompt={metadata.get('prompt_version')}"
            )
    lines.append(f"Reason: {result.reason}")
    return "\n".join(lines)


def open_evidence_record(
    connection: sqlite3.Connection,
    settings: Settings,
    workspace_id: str,
    case_id: str,
    document_id: str,
    *,
    actor: str,
) -> EvidenceRecord:
    """Record controller evidence access on a HUMAN run for this case."""
    context = _human_context(connection, settings, workspace_id, case_id, actor=actor)
    _ensure_human_run(connection, context)
    opened = _open_document(connection, context, document_id)
    return EvidenceRecord(
        workspace_id=workspace_id,
        case_id=case_id,
        run_id=context.run_id,
        document_id=opened.document_id,
        sha256=opened.sha256,
        actor=actor,
        opened_at=utc_now_iso(),
    )


def save_correction_record(
    connection: sqlite3.Connection,
    settings: Settings,
    workspace_id: str,
    case_id: str,
    correction_input: CorrectionInput | Mapping[str, Any],
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> CorrectionRecord:
    """Persist a controller correction without mutating invoice or payment balances."""
    payload = _parse_correction_input(correction_input)
    context = _human_context(connection, settings, workspace_id, case_id, actor=actor)
    _ensure_human_run(connection, context)
    opened: list[OpenedEvidence] = []
    for document_id in payload.evidence_document_ids:
        opened.append(_open_document(connection, context, document_id))

    conflict = _forbidden_instruction_reason(payload.text)
    stored_proposal: ProposalRecord | None = None
    verified: ResolutionProposal | None = None
    report: ValidationReport | None = None
    if payload.corrected_proposal is not None:
        payment = _case_payment(connection, workspace_id, case_id)
        if payment.applied:
            original = _original_fee_proposal(connection, workspace_id, case_id)
            if original is not None:
                stored_proposal = original
                verified = original.payload
        else:
            stored_proposal = store_proposal(connection, context, payload.corrected_proposal)
            report = stored_proposal.validation_report
            is_fee = stored_proposal.payload.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE
            if report.valid and is_fee:
                verified = stored_proposal.payload
    if verified is None:
        original = _original_fee_proposal(connection, workspace_id, case_id)
        if original is not None:
            stored_proposal = stored_proposal or original
            verified = original.payload

    eligible, reason = _eligibility(
        connection,
        workspace_id,
        payload,
        verified=verified,
        report=report,
        conflict=conflict,
        opened=opened,
    )
    created_at = utc_now_iso()
    record = CorrectionRecord(
        correction_id=_new_id("COR"),
        workspace_id=workspace_id,
        case_id=case_id,
        run_id=context.run_id,
        proposal_id=None if stored_proposal is None else stored_proposal.proposal_id,
        actor=actor,
        text=payload.text,
        cited_document_ids=list(payload.evidence_document_ids),
        verified_resolved_proposal=verified,
        lesson_eligibility=eligible,
        eligibility_reason=reason,
        application_id=None,
        created_at=created_at,
    )
    insert_correction(connection, record)
    record_run_event(
        connection,
        context.run_id,
        RunEventKind.RUN_FINISHED,
        {
            "action": "correction_saved",
            "correction_id": record.correction_id,
            "lesson_eligibility": eligible,
        },
        created_at=created_at,
    )
    connection.execute(
        """
        UPDATE runs SET state = ?, finished_at = ? WHERE run_id = ? AND state = ?
        """,
        (RunState.COMPLETE.value, created_at, context.run_id, RunState.RUNNING.value),
    )
    return record


def apply_correction_record(
    connection: sqlite3.Connection,
    settings: Settings,
    correction_id: str,
    *,
    idempotency_key: str,
    actor: str,
) -> ApplicationResult:
    """Revalidate a saved human proposal in a fresh HUMAN context and apply it."""
    correction = get_correction(connection, correction_id)
    if correction is None:
        raise LearningError("INVALID_INPUT", f"correction {correction_id} was not found")
    if correction.verified_resolved_proposal is None:
        raise LearningError(
            "INVALID_INPUT",
            "correction has no verified proposal to apply",
        )
    context = _human_context(
        connection,
        settings,
        correction.workspace_id,
        correction.case_id,
        actor=actor,
        reuse_running=False,
    )
    _ensure_human_run(connection, context)
    for document_id in correction.cited_document_ids:
        _open_document(connection, context, document_id)
    stored = store_proposal(connection, context, correction.verified_resolved_proposal)
    result = apply_proposal(
        connection,
        context,
        stored.proposal_id,
        idempotency_key=idempotency_key,
        actor=actor,
    )
    if result.status in {ApplicationStatus.APPLIED, ApplicationStatus.REPLAYED}:
        if result.application_id:
            update_correction_application(connection, correction_id, result.application_id)
    elif get_run(connection, context.run_id) is not None:
        finished = utc_now_iso()
        connection.execute(
            """
            UPDATE runs SET state = ?, finished_at = ? WHERE run_id = ? AND state = ?
            """,
            (RunState.COMPLETE.value, finished, context.run_id, RunState.RUNNING.value),
        )
        record_run_event(
            connection,
            context.run_id,
            RunEventKind.RUN_FINISHED,
            {"action": "correction_apply_rejected", "correction_id": correction_id},
            created_at=finished,
        )
    return result


def propose_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    correction_id: str,
    *,
    mode: ExecutionMode,
    provider: Provider | None = None,
) -> LessonDraftResult:
    """Ask the compiler for a draft-only wire_fee_lookup_v1 hint."""
    if mode is ExecutionMode.HUMAN:
        raise LearningError("INVALID_INPUT", "HUMAN mode is not a lesson compiler mode.")
    correction = get_correction(connection, correction_id)
    if correction is None:
        raise LearningError("INVALID_INPUT", f"correction {correction_id} was not found")
    workspace = get_workspace(connection, correction.workspace_id)
    if workspace is None:
        raise LearningError("INVALID_INPUT", f"workspace {correction.workspace_id} was not found")
    if not correction.lesson_eligibility or correction.verified_resolved_proposal is None:
        reason = correction.eligibility_reason or (
            "Correction is not eligible for a reusable fee-lookup lesson."
        )
        return LessonDraftResult(
            status=LessonDraftStatus.INELIGIBLE,
            correction_id=correction.correction_id,
            precedent_id=None,
            version=None,
            reason=reason,
            compiler_run_metadata={"provider_called": False},
        )
    selected = _resolve_compiler_provider(settings, mode, provider)
    verified_scope = _verified_scope(
        connection,
        correction.workspace_id,
        correction.case_id,
        correction.verified_resolved_proposal,
    )
    if verified_scope is None:
        return LessonDraftResult(
            status=LessonDraftStatus.INELIGIBLE,
            correction_id=correction.correction_id,
            reason="Verified source scope is missing; a lesson cannot be drafted.",
            compiler_run_metadata={"provider_called": False},
        )
    started = time.monotonic()
    compiler_run_id = _new_id("RUN")
    _insert_compiler_run(connection, settings, correction, compiler_run_id, mode)
    metadata: dict[str, Any] = {
        "provider": "anthropic" if mode is ExecutionMode.LIVE else "scripted",
        "model": settings.model,
        "prompt_version": LESSON_COMPILER_VERSION,
        "prompt_hash": lesson_compiler_hash(),
        "mode": mode.value,
        "run_id": compiler_run_id,
        "requests": 0,
    }
    try:
        hint, reason, requests = _compile_hint(
            connection,
            settings,
            selected,
            correction,
            compiler_run_id=compiler_run_id,
            verified_scope=verified_scope,
            remaining_deadline=lambda: COMPILER_DEADLINE_SECONDS - (time.monotonic() - started),
        )
        metadata["requests"] = requests
        metadata["elapsed_ms"] = max(0, int(round((time.monotonic() - started) * 1000)))
        if hint is None:
            ineligible = reason.startswith("cannot_generalize") or (
                "cannot be represented" in reason.lower()
            )
            _finish_compiler_run(connection, compiler_run_id, RunState.FAILED)
            return LessonDraftResult(
                status=LessonDraftStatus.INELIGIBLE if ineligible else LessonDraftStatus.ERROR,
                correction_id=correction.correction_id,
                reason=reason,
                compiler_run_metadata=metadata,
            )
        provenance = [
            OpenedEvidence(document_id=item.document_id, sha256=item.sha256)
            for item in _cited_documents(connection, correction)
        ]
        payload_hash = candidate_payload_hash(
            hint,
            correction_id=correction.correction_id,
            policy_hash=workspace.policy_hash,
            provenance=provenance,
            company_id=workspace.company_id,
            owner_workspace_id=workspace.workspace_id,
        )
        family_id = _family_id(hint.scope)
        version = next_precedent_version(connection, workspace.workspace_id, family_id)
        record = PrecedentRecord(
            precedent_id=_new_id("PRC"),
            owner_workspace_id=workspace.workspace_id,
            company_id=workspace.company_id,
            family_id=family_id,
            version=version,
            status=LessonState.DRAFT,
            hint=hint,
            payload_hash=payload_hash,
            correction_id=correction.correction_id,
            test_report_id=None,
            policy_hash=workspace.policy_hash,
            behavior_fingerprint=None,
            compiler_metadata=metadata,
        )
        insert_precedent(connection, record)
        _finish_compiler_run(connection, compiler_run_id, RunState.COMPLETE)
        return LessonDraftResult(
            status=LessonDraftStatus.CREATED,
            correction_id=correction.correction_id,
            precedent_id=record.precedent_id,
            version=record.version,
            reason="Draft lesson stored; it is not active and was not attached to other cases.",
            compiler_run_metadata=metadata,
        )
    except ProviderError as exc:
        metadata["elapsed_ms"] = max(0, int(round((time.monotonic() - started) * 1000)))
        _finish_compiler_run(connection, compiler_run_id, RunState.FAILED)
        return LessonDraftResult(
            status=LessonDraftStatus.ERROR,
            correction_id=correction.correction_id,
            reason=f"{exc.code.value}: {exc.message}",
            compiler_run_metadata=metadata,
        )


def test_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    precedent_id: str,
    *,
    mode: ExecutionMode,
    provider_factory: EpisodeProviderFactory | None = None,
) -> LessonTestResult:
    """Run five isolated paired cases. Never mutates the owner workspace ledger."""
    if mode is ExecutionMode.HUMAN:
        raise LearningError("INVALID_INPUT", "HUMAN mode is not a lesson test mode.")
    if mode is ExecutionMode.LIVE:
        _require_live_provider(settings, provider_factory)
    elif provider_factory is None:
        raise LearningError(
            ErrorCode.LIVE_DISABLED.value,
            "TEST lesson checks require an explicit per-episode provider factory.",
        )
    factory = provider_factory
    record = get_precedent(connection, precedent_id)
    if record is None:
        raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
    if record.status in {LessonState.ACTIVE, LessonState.REJECTED, LessonState.RETIRED}:
        raise LearningError(
            "INVALID_INPUT",
            f"a {record.status.value} lesson cannot be tested; redraft it first.",
        )
    owner = get_workspace(connection, record.owner_workspace_id)
    if owner is None:
        raise LearningError(
            "INVALID_INPUT",
            f"owner workspace {record.owner_workspace_id} was not found",
        )
    suite = load_dev_suite(settings.data_dir)
    fingerprint = behavior_fingerprint(settings, policy_hash=owner.policy_hash)
    suite_hash = dev_suite_hash(settings.data_dir)
    checks = deterministic_lesson_checks()
    report_id = _new_id("RPT")
    started_at = utc_now_iso()
    report = CandidateTestReport(
        report_id=report_id,
        candidate_version=record.version,
        candidate_payload_hash=record.payload_hash,
        behavior_fingerprint=fingerprint,
        policy_hash=owner.policy_hash,
        dev_suite_hash=suite_hash,
        mode=mode,
        state=CandidateTestState.RUNNING,
        episode_run_ids=[],
        paired_scores=[],
        check_results=checks,
        started_at=started_at,
        finished_at=None,
    )
    testing = record.model_copy(
        update={
            "status": LessonState.TESTING,
            "test_report_id": report_id,
            "behavior_fingerprint": fingerprint,
            "policy_hash": owner.policy_hash,
        }
    )
    update_precedent_lifecycle(connection, testing)
    insert_candidate_test(
        connection,
        report,
        outcomes={"report": report.model_dump(mode="json"), "episodes": []},
        score_summary={"passed_limited_checks": False, "failed_reasons": []},
    )

    baseline_episodes: dict[str, IsolatedEpisode] = {}
    candidate_episodes: dict[str, IsolatedEpisode] = {}
    episode_rows: list[CandidateEpisodeRecord] = []
    order_index = 0
    authoring_by_case = {entry.case_id: entry.authoring_id for entry, _package, _oracle in suite}
    empty = empty_memory_snapshot()
    candidate_snapshot = candidate_memory_snapshot(record, owner)

    for case_index, (_entry, package, oracle) in enumerate(suite):
        for arm in paired_arm_order(case_index):
            snapshot = empty if arm == "baseline" else candidate_snapshot
            if mode is ExecutionMode.LIVE:
                provider = None
            else:
                provider = factory(package.case_id, arm)
            episode = execute_isolated_case(
                connection,
                settings,
                package,
                arm=arm,
                mode=mode,
                memory_snapshot=snapshot,
                provider=provider,
                order_index=order_index,
            )
            score = score_episode(oracle, episode)
            row = episode_record(episode, score)
            episode_rows.append(row)
            if arm == "baseline":
                baseline_episodes[package.case_id] = episode
            else:
                candidate_episodes[package.case_id] = episode
            order_index += 1
            run_ids = [item.run_id for item in episode_rows if item.run_id]
            running = report.model_copy(update={"episode_run_ids": run_ids})
            update_candidate_test(
                connection,
                running,
                outcomes={
                    "report": running.model_dump(mode="json"),
                    "episodes": [item.model_dump(mode="json") for item in episode_rows],
                },
                score_summary={"passed_limited_checks": False, "failed_reasons": []},
            )
            report = running

    baseline_scores = {
        case_id: score_episode(oracle, baseline_episodes[case_id])
        for _entry, _package, oracle in suite
        for case_id in [oracle.case_id]
    }
    candidate_scores = {
        case_id: score_episode(oracle, candidate_episodes[case_id])
        for _entry, _package, oracle in suite
        for case_id in [oracle.case_id]
    }
    finished = all(
        not item.technical_error and item.run_result is not None
        for item in [*baseline_episodes.values(), *candidate_episodes.values()]
    )
    passed, reasons = score_candidate_suite(
        authoring_by_case=authoring_by_case,
        baseline_scores=baseline_scores,
        candidate_scores=candidate_scores,
        check_results=checks,
        finished_without_error=finished,
    )
    improvement = useful_improvement(
        baseline_scores,
        candidate_scores,
        list(baseline_episodes.values()),
        list(candidate_episodes.values()),
    )
    paired = [
        PairedCaseScore(
            case_id=entry.case_id,
            baseline_run_id=(
                None
                if baseline_episodes[entry.case_id].run_result is None
                else baseline_episodes[entry.case_id].run_result.run_id
            ),
            memory_run_id=(
                None
                if candidate_episodes[entry.case_id].run_result is None
                else candidate_episodes[entry.case_id].run_result.run_id
            ),
            baseline_outcome=baseline_scores[entry.case_id].outcome,
            memory_outcome=candidate_scores[entry.case_id].outcome,
            baseline_correct=baseline_scores[entry.case_id].correct,
            memory_correct=candidate_scores[entry.case_id].correct,
        )
        for entry, _package, _oracle in suite
    ]
    finished_at = utc_now_iso()
    final_state = CandidateTestState.PASSED if passed else CandidateTestState.FAILED
    final_report = report.model_copy(
        update={
            "state": final_state,
            "paired_scores": paired,
            "episode_run_ids": [item.run_id for item in episode_rows if item.run_id],
            "finished_at": finished_at,
        }
    )
    lesson_status = LessonState.PASSED if passed else LessonState.FAILED
    finished_lesson = testing.model_copy(
        update={"status": lesson_status, "test_report_id": final_report.report_id}
    )
    update_precedent_lifecycle(connection, finished_lesson)
    result = LessonTestResult(
        precedent_id=precedent_id,
        report=final_report,
        episodes=episode_rows,
        failed_reasons=reasons,
        passed_limited_checks=passed,
        demonstrated_useful_improvement=improvement,
    )
    update_candidate_test(
        connection,
        final_report,
        outcomes={
            "report": final_report.model_dump(mode="json"),
            "episodes": [item.model_dump(mode="json") for item in episode_rows],
            "failed_reasons": reasons,
        },
        score_summary={
            "passed_limited_checks": passed,
            "demonstrated_useful_improvement": improvement,
            "failed_reasons": reasons,
        },
    )
    return result


def activate_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    """Activate a live-tested PASSED lesson in its owner workspace."""
    record = get_precedent(connection, precedent_id)
    if record is None:
        raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
    blockers = activation_blockers(connection, settings, record)
    if blockers:
        raise LearningError("INVALID_INPUT", blockers[0])
    now = utc_now_iso()
    previous_id: str | None = None
    try:
        with transaction(connection, immediate=True):
            current = get_precedent(connection, precedent_id)
            if current is None:
                raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
            still_blocked = activation_blockers(connection, settings, current)
            if still_blocked:
                raise LearningError("INVALID_INPUT", still_blocked[0])
            previous = get_active_precedent_for_family(
                connection, current.owner_workspace_id, current.family_id
            )
            if previous is not None and previous.precedent_id != current.precedent_id:
                previous_id = previous.precedent_id
                update_precedent_lifecycle(
                    connection,
                    previous.model_copy(
                        update={
                            "status": LessonState.RETIRED,
                            "retirement_actor": actor,
                            "retirement_at": now,
                        }
                    ),
                )
            update_precedent_lifecycle(
                connection,
                current.model_copy(
                    update={
                        "status": LessonState.ACTIVE,
                        "activation_actor": actor,
                        "activation_at": now,
                    }
                ),
            )
            attached = list_workspace_memory(connection, current.owner_workspace_id)
            if current.precedent_id not in attached:
                insert_workspace_memory(
                    connection, current.owner_workspace_id, current.precedent_id
                )
    except PersistenceError as exc:
        raise LearningError("INVALID_INPUT", str(exc)) from exc
    return LessonLifecycleResult(
        precedent_id=precedent_id,
        version=record.version,
        status=LessonState.ACTIVE,
        reason="Activated after a compatible passing live report and explicit approval.",
        previous_precedent_id=previous_id,
        test_report_id=record.test_report_id,
    )


def retire_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    del settings
    record = get_precedent(connection, precedent_id)
    if record is None:
        raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
    if record.status is not LessonState.ACTIVE:
        raise LearningError(
            "INVALID_INPUT",
            f"only ACTIVE lessons can be retired; status is {record.status.value}.",
        )
    now = utc_now_iso()
    update_precedent_lifecycle(
        connection,
        record.model_copy(
            update={
                "status": LessonState.RETIRED,
                "retirement_actor": actor,
                "retirement_at": now,
            }
        ),
    )
    return LessonLifecycleResult(
        precedent_id=precedent_id,
        version=record.version,
        status=LessonState.RETIRED,
        reason="Retired. Past traces remain; the version is excluded from future retrieval.",
        test_report_id=record.test_report_id,
    )


def reject_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    del settings, actor
    record = get_precedent(connection, precedent_id)
    if record is None:
        raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
    if record.status is LessonState.ACTIVE:
        raise LearningError("INVALID_INPUT", "retire an ACTIVE lesson; do not reject it.")
    if record.status in {LessonState.REJECTED, LessonState.RETIRED}:
        raise LearningError(
            "INVALID_INPUT",
            f"a {record.status.value} lesson cannot be rejected.",
        )
    update_precedent_lifecycle(
        connection,
        record.model_copy(update={"status": LessonState.REJECTED}),
    )
    return LessonLifecycleResult(
        precedent_id=precedent_id,
        version=record.version,
        status=LessonState.REJECTED,
        reason="Rejected. The version is not active and cannot be retrieved.",
        test_report_id=record.test_report_id,
    )


def redraft_lesson_record(
    connection: sqlite3.Connection,
    settings: Settings,
    precedent_id: str,
) -> LessonLifecycleResult:
    """Create a new DRAFT with the same hint/provenance and no inherited report."""
    source = get_precedent(connection, precedent_id)
    if source is None:
        raise LearningError("INVALID_INPUT", f"precedent {precedent_id} was not found")
    workspace = get_workspace(connection, source.owner_workspace_id)
    if workspace is None:
        raise LearningError(
            "INVALID_INPUT",
            f"owner workspace {source.owner_workspace_id} was not found",
        )
    provenance: list[OpenedEvidence] = []
    if source.correction_id:
        correction = get_correction(connection, source.correction_id)
        if correction is not None:
            provenance = [
                OpenedEvidence(document_id=item.document_id, sha256=item.sha256)
                for item in _cited_documents(connection, correction)
            ]
            payload_hash = candidate_payload_hash(
                source.hint,
                correction_id=correction.correction_id,
                policy_hash=workspace.policy_hash,
                provenance=provenance,
                company_id=workspace.company_id,
                owner_workspace_id=workspace.workspace_id,
            )
        else:
            payload_hash = source.payload_hash
    else:
        payload_hash = source.payload_hash
    version = next_precedent_version(connection, source.owner_workspace_id, source.family_id)
    fingerprint = behavior_fingerprint(settings, policy_hash=workspace.policy_hash)
    draft = PrecedentRecord(
        precedent_id=_new_id("PRC"),
        owner_workspace_id=source.owner_workspace_id,
        company_id=source.company_id,
        family_id=source.family_id,
        version=version,
        status=LessonState.DRAFT,
        hint=source.hint,
        payload_hash=payload_hash,
        correction_id=source.correction_id,
        test_report_id=None,
        policy_hash=workspace.policy_hash,
        behavior_fingerprint=fingerprint,
        compiler_metadata={
            **source.compiler_metadata,
            "redraft_of": source.precedent_id,
        },
    )
    insert_precedent(connection, draft)
    return LessonLifecycleResult(
        precedent_id=draft.precedent_id,
        version=draft.version,
        status=LessonState.DRAFT,
        reason=(
            "Created a new draft with the same typed hint and provenance. "
            "It inherited no test report or approval."
        ),
        previous_precedent_id=source.precedent_id,
        test_report_id=None,
    )


def activation_blockers(
    connection: sqlite3.Connection,
    settings: Settings,
    record: PrecedentRecord,
) -> list[str]:
    """Return reasons the current version cannot become ACTIVE."""
    reasons: list[str] = []
    if record.status is not LessonState.PASSED:
        reasons.append(
            f"status is {record.status.value}, not PASSED; "
            "untested or failed lessons cannot activate."
        )
        return reasons
    if record.test_report_id is None:
        reasons.append("no test report is bound to this version.")
        return reasons
    stored = get_candidate_test(connection, record.test_report_id)
    if stored is None:
        reasons.append("the bound test report is missing.")
        return reasons
    report, _outcomes, _summary = stored
    if report.state is not CandidateTestState.PASSED:
        reasons.append("the bound test report is not PASSED.")
    if report.mode is not ExecutionMode.LIVE:
        reasons.append("offline scripted checks cannot authorize a live-tested lesson.")
    if report.candidate_payload_hash != record.payload_hash:
        reasons.append("payload hash does not match the bound report.")
    if report.candidate_version != record.version:
        reasons.append("the bound report belongs to a different lesson version.")
    owner = get_workspace(connection, record.owner_workspace_id)
    if owner is None:
        reasons.append("the owner workspace is missing.")
        return reasons
    current_fp = behavior_fingerprint(settings, policy_hash=owner.policy_hash)
    if report.behavior_fingerprint != current_fp:
        reasons.append("behavior fingerprint changed; redraft and retest.")
    if record.policy_hash != report.policy_hash or owner.policy_hash != report.policy_hash:
        reasons.append("policy hash does not match the bound report.")
    current_dev = dev_suite_hash(settings.data_dir)
    if report.dev_suite_hash != current_dev:
        reasons.append("dev-suite hash changed; retest.")
    if not report.check_results.validator_suite_passed:
        reasons.append("validator checks did not pass for this implementation version.")
    if not report.check_results.lifecycle_suite_passed:
        reasons.append("lifecycle checks did not pass for this implementation version.")
    return reasons


def load_attached_memory_snapshot(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    settings: Settings,
) -> tuple[MemorySnapshot, list[MemoryEligibilityNote]]:
    """Freeze ACTIVE compatible lessons attached in this workspace's memberships."""
    notes: list[MemoryEligibilityNote] = []
    hints: list[MemoryHintRef] = []
    for precedent_id in list_workspace_memory(connection, workspace.workspace_id):
        record = get_precedent(connection, precedent_id)
        if record is None:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=precedent_id,
                    included=False,
                    reason="workspace membership points to a missing lesson",
                )
            )
            continue
        reason = _runtime_exclusion_reason(record, workspace, settings)
        if reason is not None:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=record.precedent_id,
                    version=record.version,
                    included=False,
                    reason=reason,
                )
            )
            continue
        notes.append(
            MemoryEligibilityNote(
                precedent_id=record.precedent_id,
                version=record.version,
                included=True,
                reason="active compatible lesson attached in this workspace",
            )
        )
        hints.append(
            MemoryHintRef(
                precedent_id=record.precedent_id,
                version=record.version,
                payload_hash=record.payload_hash,
                hint=record.hint,
            )
        )
    snapshot = freeze_memory_snapshot(
        hints,
        source_workspace_id=workspace.workspace_id,
        company_id=workspace.company_id,
        policy_id=workspace.policy.policy_id,
        policy_hash=workspace.policy_hash,
    )
    return snapshot, notes


def _runtime_exclusion_reason(
    record: PrecedentRecord,
    workspace: WorkspaceRecord,
    settings: Settings,
) -> str | None:
    if record.status is not LessonState.ACTIVE:
        return f"status is {record.status.value}, not ACTIVE"
    if record.company_id != workspace.company_id:
        return "lesson company does not match this workspace"
    if record.policy_hash != workspace.policy_hash:
        return "lesson policy hash is incompatible with this workspace"
    if record.behavior_fingerprint is None:
        return "lesson is missing a behavior fingerprint and cannot be retrieved"
    current = behavior_fingerprint(settings, policy_hash=workspace.policy_hash)
    if record.behavior_fingerprint != current:
        return "behavior fingerprint changed; redraft and retest"
    return None


def format_lesson_test(result: LessonTestResult) -> str:
    """CLI-readable ten-episode report. Does not claim general safety."""
    report = result.report
    lines = [
        f"Precedent: {result.precedent_id}",
        f"Report: {report.report_id}",
        f"Mode: {report.mode.value}",
        f"State: {report.state.value}",
        f"Payload hash: {report.candidate_payload_hash}",
        f"Behavior fingerprint: {report.behavior_fingerprint}",
        f"Policy hash: {report.policy_hash}",
        f"Dev suite hash: {report.dev_suite_hash}",
        f"Passed limited checks: {str(result.passed_limited_checks).lower()}",
        (
            "Demonstrated useful improvement: "
            + (
                "n/a"
                if result.demonstrated_useful_improvement is None
                else str(result.demonstrated_useful_improvement).lower()
            )
        ),
        "Episodes:",
    ]
    for episode in result.episodes:
        outcome = "(none)" if episode.outcome is None else episode.outcome.value
        correct = "(n/a)" if episode.correct is None else str(episode.correct).lower()
        run_id = episode.run_id or "(no run)"
        retrieved = ",".join(episode.retrieved_precedent_ids) or "(none)"
        lines.append(
            f"  {episode.order_index + 1:>2} {episode.case_id} {episode.arm:<9} "
            f"ws={episode.workspace_id} run={run_id} outcome={outcome} "
            f"correct={correct} retrieved={retrieved}"
        )
        if episode.validator_rejection_codes:
            codes = ",".join(code.value for code in episode.validator_rejection_codes)
            lines.append(f"     validator rejections: {codes}")
    if result.failed_reasons:
        lines.append("Failed reasons:")
        for reason in result.failed_reasons:
            lines.append(f"  - {reason}")
    lines.append(
        "These five cases do not prove general safety. "
        "Activation requires this passing live report and explicit approval."
    )
    return "\n".join(lines)


def format_lesson_lifecycle(result: LessonLifecycleResult) -> str:
    lines = [
        f"Precedent: {result.precedent_id}",
        f"Version: {result.version}",
        f"Status: {result.status.value}",
        f"Reason: {result.reason}",
    ]
    if result.previous_precedent_id:
        lines.append(f"Previous: {result.previous_precedent_id}")
    if result.test_report_id:
        lines.append(f"Report: {result.test_report_id}")
    return "\n".join(lines)


def _require_live_provider(
    settings: Settings, provider_factory: EpisodeProviderFactory | None
) -> None:
    if provider_factory is not None:
        raise LearningError(
            ErrorCode.INTERNAL_ERROR.value,
            "ScriptedProvider is never selected as a live-mode fallback.",
        )
    reason = live_readiness_code(settings)
    if reason is not None:
        code = ErrorCode(reason)
        raise LearningError(code.value, _READINESS_MESSAGES.get(code, reason))


def _parse_correction_input(value: CorrectionInput | Mapping[str, Any]) -> CorrectionInput:
    try:
        if isinstance(value, CorrectionInput):
            return value
        if hasattr(value, "model_dump"):
            return CorrectionInput.model_validate(value.model_dump(mode="json"))
        return CorrectionInput.model_validate(value)
    except ValidationError as exc:
        raise LearningError("INVALID_INPUT", f"invalid correction input: {exc}") from exc


def _human_context(
    connection: sqlite3.Connection,
    settings: Settings,
    workspace_id: str,
    case_id: str,
    *,
    actor: str,
    reuse_running: bool = True,
) -> RunContext:
    workspace = get_workspace(connection, workspace_id)
    if workspace is None:
        raise LearningError("INVALID_INPUT", f"workspace {workspace_id} was not found")
    case = get_case(connection, workspace_id, case_id)
    if case is None:
        raise LearningError("INVALID_INPUT", f"case {case_id} was not found")
    run_id = None
    opened: list[OpenedEvidence] = []
    if reuse_running:
        run_id, opened = _running_human_run(connection, workspace_id, case_id)
    return RunContext(
        workspace_id=workspace_id,
        case_id=case_id,
        run_id=run_id or _new_id("RUN"),
        company_id=workspace.company_id,
        policy_id=workspace.policy.policy_id,
        policy_hash=workspace.policy_hash,
        dataset_hash=workspace.dataset_hash,
        initial_ledger_revision=workspace.ledger_revision,
        execution_mode=ExecutionMode.HUMAN,
        actor=actor,
        opened_documents=opened,
        retrieved_precedent_version_ids=[],
        memory_snapshot=empty_memory_snapshot(),
        budgets=_budgets(settings),
    )


def _running_human_run(
    connection: sqlite3.Connection, workspace_id: str, case_id: str
) -> tuple[str | None, list[OpenedEvidence]]:
    row = connection.execute(
        """
        SELECT run_id FROM runs
        WHERE workspace_id = ? AND case_id = ? AND mode = ? AND state = ?
        ORDER BY started_at DESC
        """,
        (workspace_id, case_id, ExecutionMode.HUMAN.value, RunState.RUNNING.value),
    ).fetchone()
    if row is None:
        return None, []
    opened: list[OpenedEvidence] = []
    seen: set[str] = set()
    for event in list_run_events(connection, row["run_id"]):
        if event.event_kind is not RunEventKind.TOOL_SUCCEEDED:
            continue
        payload = event.payload
        if payload.get("tool") != OPEN_EVIDENCE_TOOL:
            continue
        document_id = payload.get("document_id")
        sha256 = payload.get("sha256")
        if isinstance(document_id, str) and isinstance(sha256, str) and document_id not in seen:
            opened.append(OpenedEvidence(document_id=document_id, sha256=sha256))
            seen.add(document_id)
    return row["run_id"], opened


def _ensure_human_run(connection: sqlite3.Connection, context: RunContext) -> None:
    if get_run(connection, context.run_id) is not None:
        return
    case = get_case(connection, context.workspace_id, context.case_id)
    started_at = utc_now_iso()
    insert_run(
        connection,
        RunRecord(
            run_id=context.run_id,
            workspace_id=context.workspace_id,
            case_id=context.case_id,
            mode=ExecutionMode.HUMAN,
            provider=None,
            model=None,
            prompt_hash=sha256_utf8(HUMAN_PROMPT_VERSION),
            tool_hash=hash_canonical({"name": OPEN_EVIDENCE_TOOL}),
            memory_snapshot=context.memory_snapshot,
            state=RunState.RUNNING,
            started_at=started_at,
            finished_at=None,
            budgets=context.budgets,
            usage=None,
            terminal_result=None,
        ),
    )
    record_run_event(
        connection,
        context.run_id,
        RunEventKind.RUN_STARTED,
        {
            "case_id": context.case_id,
            "execution_mode": ExecutionMode.HUMAN.value,
            "original_run_id": None if case is None else case.latest_run_id,
            "actor": context.actor,
        },
        created_at=started_at,
    )


def _open_document(
    connection: sqlite3.Connection, context: RunContext, document_id: str
) -> OpenedEvidence:
    result = read_document(connection, context, {"document_id": document_id})
    if not result.ok or not isinstance(result.data, dict):
        code = result.error.code if result.error else "NOT_FOUND"
        message = result.error.message if result.error else f"document {document_id} was not found"
        raise LearningError(code, message)
    opened = OpenedEvidence(document_id=result.data["document_id"], sha256=result.data["sha256"])
    record_run_event(
        connection,
        context.run_id,
        RunEventKind.TOOL_CALLED,
        {"tool": OPEN_EVIDENCE_TOOL, "document_id": opened.document_id},
    )
    record_run_event(
        connection,
        context.run_id,
        RunEventKind.TOOL_SUCCEEDED,
        {
            "tool": OPEN_EVIDENCE_TOOL,
            "document_id": opened.document_id,
            "sha256": opened.sha256,
        },
    )
    return opened


def _case_payment(connection: sqlite3.Connection, workspace_id: str, case_id: str):
    case = get_case(connection, workspace_id, case_id)
    if case is None:
        raise LearningError("INVALID_INPUT", f"case {case_id} was not found")
    payment = get_payment(connection, workspace_id, case.payment_id)
    if payment is None:
        raise LearningError("INVALID_INPUT", f"payment {case.payment_id} was not found")
    return payment


def _original_fee_proposal(
    connection: sqlite3.Connection, workspace_id: str, case_id: str
) -> ProposalRecord | None:
    case = get_case(connection, workspace_id, case_id)
    if case is None:
        return None
    application = get_application_by_payment(connection, workspace_id, case.payment_id)
    if application is None or application.proposal_id is None:
        return None
    stored = get_proposal(connection, application.proposal_id)
    if stored is None:
        return None
    if stored.payload.resolution_type is not ResolutionType.SINGLE_WITH_BANK_FEE:
        return None
    if not stored.validation_report.valid:
        return None
    return stored


def _eligibility(
    connection: sqlite3.Connection,
    workspace_id: str,
    payload: CorrectionInput,
    *,
    verified: ResolutionProposal | None,
    report: ValidationReport | None,
    conflict: str | None,
    opened: Sequence[OpenedEvidence],
) -> tuple[bool, str | None]:
    if conflict is not None:
        return False, conflict
    if verified is None:
        if report is not None and not report.valid:
            codes = ", ".join(issue.code.value for issue in report.issues)
            return False, f"Corrected proposal is not a validated fee resolution ({codes})."
        return False, (
            "Reusable lessons require a validated SINGLE_WITH_BANK_FEE resolution "
            "and linked remittance and bank fee notice sources."
        )
    if verified.resolution_type is not ResolutionType.SINGLE_WITH_BANK_FEE:
        return False, "Reusable lessons require a validated SINGLE_WITH_BANK_FEE resolution."
    documents = [get_document(connection, workspace_id, item.document_id) for item in opened]
    kinds = {document.kind for document in documents if document is not None}
    if DocumentKind.REMITTANCE not in kinds or DocumentKind.BANK_FEE_NOTICE not in kinds:
        return False, ("Reusable lessons require linked remittance and bank fee notice sources.")
    cited = set(payload.evidence_document_ids)
    missing = [doc_id for doc_id in verified.evidence_document_ids if doc_id not in cited]
    if missing:
        return False, "Cited sources must include the documents that authorize the fee resolution."
    return True, "Validated fee resolution with linked remittance and bank fee notice."


def _forbidden_instruction_reason(text: str) -> str | None:
    for pattern, reason in _FORBIDDEN_INSTRUCTION:
        if pattern.search(text):
            return reason
    return None


def _cited_documents(
    connection: sqlite3.Connection, correction: CorrectionRecord
) -> list[Document]:
    documents: list[Document] = []
    for document_id in correction.cited_document_ids:
        document = get_document(connection, correction.workspace_id, document_id)
        if document is not None:
            documents.append(document)
    return documents


def _verified_scope(
    connection: sqlite3.Connection,
    workspace_id: str,
    case_id: str,
    proposal: ResolutionProposal,
) -> HintScope | None:
    payment = _case_payment(connection, workspace_id, case_id)
    return HintScope(
        customer_id=proposal.customer_id,
        bank_account_id=payment.bank_account_id,
        currency=payment.currency,
        channel=payment.channel,
    )


def _family_id(scope: HintScope) -> str:
    digest = hash_canonical(scope.model_dump(mode="json"))[:16]
    return f"FAM-{digest}"


def _budgets(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        max_model_calls=settings.max_model_calls,
        max_tool_calls=settings.max_tool_calls,
        max_case_seconds=settings.max_case_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_output_tokens=settings.max_output_tokens,
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _resolve_compiler_provider(
    settings: Settings, mode: ExecutionMode, provider: Provider | None
) -> Provider:
    if mode is ExecutionMode.LIVE:
        if isinstance(provider, ScriptedProvider):
            raise LearningError(
                ErrorCode.INTERNAL_ERROR.value,
                "ScriptedProvider is never selected as a live-mode fallback.",
            )
        reason = live_readiness_code(settings)
        if reason is not None:
            code = ErrorCode(reason)
            raise LearningError(code.value, _READINESS_MESSAGES.get(code, reason))
        try:
            return select_provider(settings, scripted=None)
        except ProviderError as exc:
            raise LearningError(exc.code.value, exc.message) from exc
    scripted = provider if isinstance(provider, ScriptedProvider) else None
    if scripted is None:
        raise LearningError(
            ErrorCode.LIVE_DISABLED.value,
            "TEST lesson drafting requires an explicit ScriptedProvider.",
        )
    try:
        return select_provider(settings, scripted=scripted)
    except ProviderError as exc:
        raise LearningError(exc.code.value, exc.message) from exc


def _insert_compiler_run(
    connection: sqlite3.Connection,
    settings: Settings,
    correction: CorrectionRecord,
    run_id: str,
    mode: ExecutionMode,
) -> None:
    started_at = utc_now_iso()
    insert_run(
        connection,
        RunRecord(
            run_id=run_id,
            workspace_id=correction.workspace_id,
            case_id=correction.case_id,
            mode=mode,
            provider="anthropic" if mode is ExecutionMode.LIVE else "scripted",
            model=settings.model,
            prompt_hash=lesson_compiler_hash(),
            tool_hash=compiler_tool_hash(),
            memory_snapshot=empty_memory_snapshot(),
            state=RunState.RUNNING,
            started_at=started_at,
            finished_at=None,
            budgets=BudgetLimits(
                max_model_calls=MAX_COMPILER_REQUESTS,
                max_tool_calls=MAX_COMPILER_REQUESTS,
                max_case_seconds=COMPILER_DEADLINE_SECONDS,
                request_timeout_seconds=min(
                    settings.request_timeout_seconds, COMPILER_DEADLINE_SECONDS
                ),
                max_output_tokens=settings.max_output_tokens,
            ),
            usage=Usage(
                model_attempts=0,
                tool_calls=0,
                input_tokens=None,
                output_tokens=None,
                elapsed_ms=0,
            ),
            terminal_result=None,
        ),
    )
    record_run_event(
        connection,
        run_id,
        RunEventKind.RUN_STARTED,
        {
            "action": "lesson_compile",
            "correction_id": correction.correction_id,
            "execution_mode": mode.value,
        },
        created_at=started_at,
    )


def _finish_compiler_run(connection: sqlite3.Connection, run_id: str, state: RunState) -> None:
    connection.execute(
        """
        UPDATE runs SET state = ?, finished_at = ? WHERE run_id = ?
        """,
        (state.value, utc_now_iso(), run_id),
    )


def _compile_hint(
    connection: sqlite3.Connection,
    settings: Settings,
    provider: Provider,
    correction: CorrectionRecord,
    *,
    compiler_run_id: str,
    verified_scope: HintScope,
    remaining_deadline,
) -> tuple[WireFeeLookupHint | None, str, int]:
    documents = _cited_documents(connection, correction)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _compiler_user_message(correction, documents, verified_scope)}
    ]
    definitions = compiler_tool_definitions()
    system_prompt = lesson_compiler_text()
    requests = 0
    last_reason = "Compiler did not produce a valid wire_fee_lookup_v1 draft."
    while requests < MAX_COMPILER_REQUESTS:
        remaining = float(remaining_deadline())
        if remaining < MIN_REQUEST_SECONDS:
            return None, "Compiler deadline was exhausted before a valid draft.", requests
        turn = provider.generate(
            system_prompt,
            messages,
            definitions,
            settings,
            remaining_seconds=min(remaining, float(settings.request_timeout_seconds)),
            tool_choice={"type": "any"},
        )
        requests += 1
        record_run_event(
            connection,
            compiler_run_id,
            RunEventKind.MODEL_RESPONSE,
            {
                "action": "lesson_compile",
                "stop_reason": turn.stop_reason,
                "tool_names": [call.name for call in turn.tool_calls],
            },
        )
        assistant = turn.provider_assistant_message
        if not isinstance(assistant, dict):
            assistant = {"role": "assistant", "content": turn.public_text or ""}
        messages.append(assistant)
        if not turn.tool_calls:
            last_reason = "Compiler returned no tool call; a schema-shaped output is required."
            if requests >= MAX_COMPILER_REQUESTS:
                break
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Call propose_precedent with the exact verified scope or "
                        "cannot_generalize with a reason. Do not return free-form JSON."
                    ),
                }
            )
            continue
        call = turn.tool_calls[0]
        hint, reason, tool_result = _handle_compiler_tool(
            call, verified_scope=verified_scope, documents=documents
        )
        results = [(call.call_id, tool_result)]
        for extra in turn.tool_calls[1:]:
            skipped = ToolResult(
                ok=False,
                data=None,
                error=ToolError(code="TERMINAL_REACHED", message="Only one compiler tool is used."),
                source_ids=[],
            )
            results.append((extra.call_id, skipped))
        messages.append(tool_result_user_message(results))
        if hint is not None:
            return hint, reason, requests
        last_reason = reason
        if call.name == CANNOT_GENERALIZE_TOOL:
            return None, reason, requests
        if requests >= MAX_COMPILER_REQUESTS:
            break
    return None, last_reason, requests


def _handle_compiler_tool(
    call: ToolCall,
    *,
    verified_scope: HintScope,
    documents: Sequence[Document],
) -> tuple[WireFeeLookupHint | None, str, ToolResult]:
    if call.name == CANNOT_GENERALIZE_TOOL:
        try:
            args = CannotGeneralizeArgs.model_validate(call.arguments)
        except ValidationError as exc:
            return (
                None,
                f"invalid cannot_generalize arguments: {exc}",
                _tool_error("INVALID_TOOL_ARGUMENTS", "cannot_generalize arguments were invalid"),
            )
        reason = f"cannot_generalize: {args.reason}"
        return None, reason, _tool_ok({"status": "cannot_generalize", "reason": args.reason})
    if call.name != PROPOSE_TOOL:
        return (
            None,
            f"unknown compiler tool {call.name!r}",
            _tool_error("UNKNOWN_TOOL", f"unknown compiler tool {call.name!r}"),
        )
    try:
        args = ProposePrecedentArgs.model_validate(call.arguments)
    except ValidationError as exc:
        return (
            None,
            f"invalid propose_precedent arguments: {exc}",
            _tool_error("INVALID_TOOL_ARGUMENTS", "propose_precedent arguments were invalid"),
        )
    problem = _hint_problem(args, verified_scope=verified_scope, documents=documents)
    if problem is not None:
        return None, problem, _tool_error("INVALID_PRECEDENT_REFERENCE", problem)
    hint = WireFeeLookupHint(
        title=args.title,
        scope=verified_scope,
        lookup=args.lookup,
        summary=args.summary,
    )
    return hint, "created", _tool_ok(hint.model_dump(mode="json"))


def _hint_problem(
    args: ProposePrecedentArgs,
    *,
    verified_scope: HintScope,
    documents: Sequence[Document],
) -> str | None:
    if args.scope.model_dump(mode="json") != verified_scope.model_dump(mode="json"):
        return (
            "Scope must be the exact verified customer, account, currency, and channel; "
            "widening is not allowed."
        )
    forbidden = _forbidden_instruction_reason(f"{args.title}\n{args.summary}")
    if forbidden is not None:
        return forbidden
    for term in args.lookup.search_terms:
        if _WILDCARD_TERM.search(term):
            return "Search terms must be literal text without wildcards or regular expressions."
    remittances = [item for item in documents if item.kind is DocumentKind.REMITTANCE]
    if not remittances:
        return "A cited remittance is required to bind the lookup field."
    field_name = args.lookup.remittance_reference_field.value
    populated = False
    for remittance in remittances:
        if not isinstance(remittance.facts, RemittanceFacts):
            continue
        value = getattr(remittance.facts, field_name)
        if isinstance(value, str) and value.strip():
            populated = True
            break
    if not populated:
        return (
            f"Cited remittance does not populate {field_name}; choose a populated "
            "settlement_ticket or transfer_reference."
        )
    if args.lookup.bank_notice_reference_field != "transfer_reference":
        return "Bank notice reference field must be transfer_reference."
    return None


def _compiler_user_message(
    correction: CorrectionRecord,
    documents: Sequence[Document],
    verified_scope: HintScope,
) -> str:
    proposal = correction.verified_resolved_proposal
    proposal_json = "{}" if proposal is None else proposal.model_dump_json()
    source_blocks = []
    for document in documents:
        source_blocks.append(
            "\n".join(
                [
                    f"Document {document.document_id} ({document.kind.value})",
                    f"Hash: {document.sha256}",
                    f"Facts: {document.facts.model_dump_json()}",
                    f"Body: {document.body_text[:1500]}",
                ]
            )
        )
    return (
        "Propose a reusable wire_fee_lookup_v1 hint from this correction, or call "
        "cannot_generalize.\n"
        f"Verified scope (required): customer_id={verified_scope.customer_id}, "
        f"bank_account_id={verified_scope.bank_account_id}, "
        f"currency={verified_scope.currency}, channel={verified_scope.channel.value}.\n"
        f"Correction ID: {correction.correction_id}\n"
        f"Correction text: {correction.text}\n"
        f"Cited document IDs: {', '.join(correction.cited_document_ids)}\n"
        f"Verified proposal JSON: {proposal_json}\n"
        "Sources:\n" + "\n\n".join(source_blocks)
    )


def _tool_ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data, error=None, source_ids=[])


def _tool_error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        data=None,
        error=ToolError(code=code, message=message),
        source_ids=[],
    )


__all__ = [
    "COMPILER_DEADLINE_SECONDS",
    "DEFAULT_CONTROLLER_ACTOR",
    "LESSON_COMPILER_VERSION",
    "LearningError",
    "MAX_COMPILER_REQUESTS",
    "activate_lesson_record",
    "activation_blockers",
    "apply_correction_record",
    "candidate_payload_hash",
    "compiler_tool_definitions",
    "format_lesson_draft",
    "format_lesson_lifecycle",
    "format_lesson_test",
    "lesson_compiler_hash",
    "lesson_compiler_text",
    "load_attached_memory_snapshot",
    "open_evidence_record",
    "propose_lesson_record",
    "redraft_lesson_record",
    "reject_lesson_record",
    "retire_lesson_record",
    "save_correction_record",
    "test_lesson_record",
]
