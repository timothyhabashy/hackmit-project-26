"""Use-case helpers shared by the CLI and later UI.

Session 03 implements demo workspace creation from generated fixtures. It
does not delete stored evaluation reports or learned records. Session 06
routes sandbox apply and case snapshots through a fresh database connection.
Session 09 routes investigation through ``investigate`` rather than provider
internals. Session 10 routes controller evidence, corrections, and draft
lesson proposals through ``open_evidence``, ``save_correction``,
``apply_correction``, and ``propose_lesson``. Session 11 routes lesson
testing and lifecycle through ``test_lesson``, ``activate_lesson``,
``retire_lesson``, ``reject_lesson``, and ``redraft_lesson``. Session 12
loads attached ACTIVE compatible lessons during ordinary ``memory_mode=on``
investigation. Session 13 routes paired evaluation through ``run_evaluation``.
Session 14 exposes workspace/case listing and case detail for the Streamlit queue.
Session 15 reads source documents and routes Case Detail mutations through
``open_evidence``, ``save_correction``, ``apply_correction``, and ``propose_lesson``.
Session 16 loads lessons, candidate-test reports, and saved evaluation artifacts
for Learning Results. It does not start tests or evaluations on import.
Session 17 exports persisted runs as labeled recorded-run manifests and lists
cases for demo reset/replay. Export never reapplies money.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from precedent.agent import investigate_case
from precedent.config import Settings, load_settings
from precedent.db import (
    PersistenceError,
    count_runs,
    get_application,
    get_application_by_payment,
    get_candidate_test,
    get_correction,
    get_customer,
    get_document,
    get_evaluation_run,
    get_precedent,
    get_run,
    get_workspace,
    insert_application,
    insert_case,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_workspace,
    latest_run_for_workspace,
    list_cases_with_payments,
    list_corrections,
    list_documents,
    list_evaluation_run_rows,
    list_precedents_for_correction,
    list_precedents_for_workspace,
    list_proposals_for_case,
    list_run_events,
    list_workspace_memory,
    list_workspaces,
    open_database,
    transaction,
)
from precedent.fixtures import (
    DEFAULT_SEED,
    FixtureError,
    SourcePackage,
    default_manifest_path,
    load_manifest,
    package_fingerprint,
    packages_for_split,
)
from precedent.learning import (
    DEFAULT_CONTROLLER_ACTOR,
    activation_blockers,
    apply_correction_record,
    load_attached_memory_snapshot,
    open_evidence_record,
    propose_lesson_record,
    save_correction_record,
)
from precedent.ledger import apply_proposal, get_case_snapshot
from precedent.models import (
    AgentOutcome,
    ApplicationRecord,
    ApplicationResult,
    BankFeeNoticeFacts,
    CandidateEpisodeRecord,
    CandidateTestReport,
    CaseDetail,
    CaseRecord,
    CaseSnapshot,
    CaseState,
    CaseSummary,
    Company,
    CompanyPolicy,
    CorrectionInput,
    CorrectionRecord,
    Customer,
    CustomerRecord,
    DatasetSplit,
    Document,
    DocumentSummary,
    EvaluationReport,
    EvaluationRequest,
    EvaluationState,
    EvidenceRecord,
    ExecutionMode,
    Invoice,
    InvoiceRecord,
    LessonDraftResult,
    LessonLifecycleResult,
    LessonTestResult,
    MemoryMode,
    MemorySnapshot,
    Payment,
    PaymentRecord,
    PrecedentRecord,
    RecordedRunManifest,
    RemittanceFacts,
    RunContext,
    RunEvent,
    RunEventKind,
    RunRecord,
    RunResult,
    RunSummary,
    SeedApplication,
    SourceDocument,
    ValidationCode,
    WorkspaceRecord,
    WorkspaceSummary,
    hash_canonical,
    hash_model,
    utc_now_iso,
)
from precedent.providers import Provider

_SPLIT_PREFIX = {
    DatasetSplit.TEACHING: "TEACH",
    DatasetSplit.CANDIDATE: "CAND",
    DatasetSplit.HELDOUT: "HOLD",
}


@dataclass(frozen=True)
class DemoWorkspaceResult:
    workspace_id: str
    name: str
    split: DatasetSplit
    case_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    attached_precedent_ids: tuple[str, ...]
    dataset_hash: str


@dataclass(frozen=True)
class QueueCounts:
    open: int
    resolved: int
    needs_review: int
    errors: int
    running: int
    runs: int


@dataclass(frozen=True)
class LessonTestView:
    report: CandidateTestReport
    episodes: tuple[CandidateEpisodeRecord, ...]
    failed_reasons: tuple[str, ...]
    passed_limited_checks: bool
    demonstrated_useful_improvement: bool | None


@dataclass(frozen=True)
class SavedEvaluationSummary:
    experiment_id: str
    report_path: Path
    mode: ExecutionMode | None
    state: EvaluationState | None
    started_at: str | None
    finished_at: str | None
    split: DatasetSplit | None
    scheduled_count: int | None
    completed_count: int | None
    not_run_count: int | None
    timed_out_count: int | None
    budget_skipped_count: int | None


@dataclass(frozen=True)
class ReviewerCaseNote:
    authoring_id: str
    case_id: str
    expected_outcome: AgentOutcome
    allowed_review_codes: tuple[ValidationCode, ...]
    expected_new_application_count: int


@dataclass(frozen=True)
class MergedSourceBundle:
    company: Company
    policy: CompanyPolicy
    customers: tuple[Customer, ...]
    invoices: tuple[Invoice, ...]
    payments: tuple[Payment, ...]
    documents: tuple[SourceDocument, ...]
    applications: tuple[SeedApplication, ...]
    cases: tuple[tuple[str, str], ...]
    dataset_hash: str


def demo_init(settings: Settings | None = None, *, seed: int = DEFAULT_SEED) -> DemoWorkspaceResult:
    return create_demo_workspace(settings or load_settings(), DatasetSplit.TEACHING, seed=seed)


def demo_new(
    settings: Settings | None = None,
    *,
    dataset: DatasetSplit = DatasetSplit.TEACHING,
    memory_from: str | None = None,
    seed: int = DEFAULT_SEED,
) -> DemoWorkspaceResult:
    return create_demo_workspace(
        settings or load_settings(),
        dataset,
        memory_from=memory_from,
        seed=seed,
    )


def create_demo_workspace(
    settings: Settings,
    split: DatasetSplit,
    *,
    memory_from: str | None = None,
    seed: int = DEFAULT_SEED,
) -> DemoWorkspaceResult:
    _require_fixtures(settings.data_dir, seed)
    packages = packages_for_split(settings.data_dir, split, seed=seed)
    bundle = merge_packages(packages)
    connection = open_database(settings.db_path)
    try:
        workspace_id = allocate_workspace_id(connection, _SPLIT_PREFIX[split])
        period_label = PERIOD_LABELS[split]
        name = f"Northstar {split.value} {period_label}"
        created_at = utc_now_iso()
        _import_bundle(connection, workspace_id, name, bundle, created_at)
        attached: tuple[str, ...] = ()
        if memory_from:
            attached = tuple(_attach_active_memory(connection, memory_from, workspace_id, settings))
        _write_active_workspace(settings.var_dir, workspace_id)
    finally:
        connection.close()
    return DemoWorkspaceResult(
        workspace_id=workspace_id,
        name=name,
        split=split,
        case_ids=tuple(case_id for case_id, _payment_id in bundle.cases),
        payment_ids=tuple(payment_id for _case_id, payment_id in bundle.cases),
        attached_precedent_ids=attached,
        dataset_hash=bundle.dataset_hash,
    )


def snapshot_case(settings: Settings, workspace_id: str, case_id: str) -> CaseSnapshot:
    """Load a sandbox case snapshot using a fresh connection."""
    connection = open_database(settings.db_path)
    try:
        return get_case_snapshot(connection, workspace_id, case_id)
    finally:
        connection.close()


def initialize_demo(settings: Settings, *, new_workspace: bool) -> WorkspaceSummary:
    """Create a teaching workspace (``demo init``) or another workspace (``demo new``)."""
    result = demo_new(settings) if new_workspace else demo_init(settings)
    return get_workspace_summary(settings, result.workspace_id)


def read_active_workspace_id(settings: Settings) -> str | None:
    path = settings.var_dir / "active_workspace.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    workspace_id = payload.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None
    return workspace_id.strip()


def list_workspace_summaries(settings: Settings) -> list[WorkspaceSummary]:
    connection = open_database(settings.db_path)
    try:
        return [_workspace_summary(item) for item in list_workspaces(connection)]
    finally:
        connection.close()


def get_workspace_summary(settings: Settings, workspace_id: str) -> WorkspaceSummary:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        return _workspace_summary(workspace)
    finally:
        connection.close()


def list_cases(settings: Settings, workspace_id: str) -> list[CaseSummary]:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        summaries: list[CaseSummary] = []
        for case, payment in list_cases_with_payments(connection, workspace_id):
            summaries.append(_case_summary(connection, case, payment))
        return summaries
    finally:
        connection.close()


def queue_counts(settings: Settings, workspace_id: str) -> QueueCounts:
    cases = list_cases(settings, workspace_id)
    connection = open_database(settings.db_path)
    try:
        runs = count_runs(connection, workspace_id)
    finally:
        connection.close()
    return QueueCounts(
        open=sum(1 for item in cases if item.case_state is CaseState.OPEN),
        resolved=sum(1 for item in cases if item.case_state is CaseState.RESOLVED),
        needs_review=sum(1 for item in cases if item.case_state is CaseState.NEEDS_REVIEW),
        errors=sum(1 for item in cases if item.case_state is CaseState.ERROR),
        running=sum(1 for item in cases if item.case_state is CaseState.RUNNING),
        runs=runs,
    )


def count_case_runs(settings: Settings, workspace_id: str, case_id: str | None = None) -> int:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        return count_runs(connection, workspace_id, case_id)
    finally:
        connection.close()


def get_case_detail(settings: Settings, workspace_id: str, case_id: str) -> CaseDetail:
    connection = open_database(settings.db_path)
    try:
        snapshot = get_case_snapshot(connection, workspace_id, case_id)
        latest_run = None
        latest_summary = None
        if snapshot.summary.latest_run_id:
            run = get_run(connection, snapshot.summary.latest_run_id)
            if run is not None:
                latest_summary = _summary_from_run(connection, run)
                latest_run = RunSummary(
                    run_id=run.run_id,
                    state=run.state,
                    mode=run.mode,
                    terminal_outcome=run.terminal_result,
                    summary=latest_summary,
                )
        snapshot = snapshot.model_copy(
            update={
                "summary": snapshot.summary.model_copy(update={"latest_summary": latest_summary})
            }
        )
        application = get_application_by_payment(
            connection, workspace_id, snapshot.payment.payment_id
        )
        if application is not None and application.seeded:
            application = None
        stored_docs = list_documents(connection, workspace_id)
        related_ids = _payment_related_document_ids(stored_docs, snapshot.payment)
        documents = [
            DocumentSummary(
                document_id=item.document_id,
                kind=item.kind,
                title=item.title,
                issued_date=item.issued_date,
                sha256=item.sha256,
            )
            for item in sorted(
                stored_docs,
                key=lambda item: (
                    0 if item.document_id in related_ids else 1,
                    item.document_id,
                ),
            )
        ]
        trace_references: list[str] = []
        if latest_run is not None:
            trace_references.append(f"run:{latest_run.run_id}")
        return CaseDetail(
            snapshot=snapshot,
            proposals=list_proposals_for_case(connection, workspace_id, case_id),
            application=application,
            corrections=list_corrections(connection, workspace_id, case_id),
            latest_run=latest_run,
            documents=documents,
            trace_references=trace_references,
        )
    finally:
        connection.close()


def get_source_document(settings: Settings, workspace_id: str, document_id: str) -> Document:
    """Return a workspace source document. Does not record evidence access."""
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        document = get_document(connection, workspace_id, document_id)
        if document is None:
            raise PersistenceError(
                f"document {document_id} was not found in workspace {workspace_id}"
            )
        return document
    finally:
        connection.close()


def get_customer_label(settings: Settings, workspace_id: str, customer_id: str) -> str:
    connection = open_database(settings.db_path)
    try:
        record = get_customer(connection, workspace_id, customer_id)
        if record is None:
            return customer_id
        return record.display_name
    finally:
        connection.close()


def list_lessons_for_correction(settings: Settings, correction_id: str) -> list[PrecedentRecord]:
    connection = open_database(settings.db_path)
    try:
        return list_precedents_for_correction(connection, correction_id)
    finally:
        connection.close()


def list_lessons(settings: Settings, workspace_id: str) -> list[PrecedentRecord]:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        return list_precedents_for_workspace(connection, workspace_id)
    finally:
        connection.close()


def get_lesson(settings: Settings, precedent_id: str) -> PrecedentRecord | None:
    connection = open_database(settings.db_path)
    try:
        return get_precedent(connection, precedent_id)
    finally:
        connection.close()


def get_correction_record(settings: Settings, correction_id: str) -> CorrectionRecord | None:
    connection = open_database(settings.db_path)
    try:
        return get_correction(connection, correction_id)
    finally:
        connection.close()


def get_lesson_test(settings: Settings, report_id: str) -> LessonTestView | None:
    connection = open_database(settings.db_path)
    try:
        stored = get_candidate_test(connection, report_id)
    finally:
        connection.close()
    if stored is None:
        return None
    report, outcomes, summary = stored
    raw_episodes = outcomes.get("episodes") if isinstance(outcomes, dict) else None
    episodes: list[CandidateEpisodeRecord] = []
    if isinstance(raw_episodes, list):
        for item in raw_episodes:
            if isinstance(item, dict):
                episodes.append(CandidateEpisodeRecord.model_validate(item))
    reasons_raw = []
    if isinstance(outcomes, dict) and isinstance(outcomes.get("failed_reasons"), list):
        reasons_raw = [str(item) for item in outcomes["failed_reasons"] if isinstance(item, str)]
    elif isinstance(summary, dict) and isinstance(summary.get("failed_reasons"), list):
        reasons_raw = [str(item) for item in summary["failed_reasons"] if isinstance(item, str)]
    passed = False
    improvement = None
    if isinstance(summary, dict):
        passed = bool(summary.get("passed_limited_checks"))
        raw_improvement = summary.get("demonstrated_useful_improvement")
        if isinstance(raw_improvement, bool):
            improvement = raw_improvement
    return LessonTestView(
        report=report,
        episodes=tuple(episodes),
        failed_reasons=tuple(reasons_raw),
        passed_limited_checks=passed,
        demonstrated_useful_improvement=improvement,
    )


def lesson_activation_blockers(settings: Settings, precedent_id: str) -> list[str]:
    connection = open_database(settings.db_path)
    try:
        record = get_precedent(connection, precedent_id)
        if record is None:
            return [f"precedent {precedent_id} was not found"]
        return activation_blockers(connection, settings, record)
    finally:
        connection.close()


def reviewer_dev_case_notes(settings: Settings) -> dict[str, ReviewerCaseNote]:
    """Evaluator-only expected dispositions for the human reviewer, never the agent."""
    from precedent.evaluation import load_dev_suite

    notes: dict[str, ReviewerCaseNote] = {}
    for entry, _package, oracle in load_dev_suite(settings.data_dir):
        notes[entry.case_id] = ReviewerCaseNote(
            authoring_id=entry.authoring_id,
            case_id=entry.case_id,
            expected_outcome=oracle.expected_outcome,
            allowed_review_codes=tuple(oracle.allowed_review_codes),
            expected_new_application_count=oracle.expected_new_application_count,
        )
    return notes


def evaluation_artifact_dir(settings: Settings) -> Path:
    return settings.repo_root / "artifacts" / "evaluations"


def recorded_run_dir(settings: Settings) -> Path:
    return settings.repo_root / "artifacts" / "recorded-runs"


def list_saved_evaluations(settings: Settings) -> list[SavedEvaluationSummary]:
    found: dict[str, SavedEvaluationSummary] = {}
    root = evaluation_artifact_dir(settings)
    if root.is_dir():
        for report_path in sorted(root.glob("*/report.json")):
            summary = _summary_from_report_path(report_path)
            if summary is not None:
                found[summary.experiment_id] = summary
    connection = open_database(settings.db_path)
    try:
        for row in list_evaluation_run_rows(connection):
            experiment_id = str(row["experiment_id"])
            if experiment_id in found:
                continue
            artifact = Path(row["artifact_path"]) if row["artifact_path"] else None
            if artifact is None:
                continue
            report_path = artifact / "report.json" if artifact.name != "report.json" else artifact
            if not report_path.is_file():
                continue
            summary = _summary_from_report_path(report_path)
            if summary is not None:
                found[summary.experiment_id] = summary
    finally:
        connection.close()
    return sorted(
        found.values(),
        key=lambda item: item.started_at or item.experiment_id,
        reverse=True,
    )


def load_evaluation(settings: Settings, experiment_id: str) -> EvaluationReport:
    """Read a saved report.json. Never starts a new experiment."""
    from precedent.evaluation import load_evaluation_report

    path = evaluation_artifact_dir(settings) / experiment_id / "report.json"
    if path.is_file():
        return load_evaluation_report(path)
    connection = open_database(settings.db_path)
    try:
        stored = get_evaluation_run(connection, experiment_id)
    finally:
        connection.close()
    if stored is None:
        raise PersistenceError(f"evaluation report {experiment_id} was not found")
    report, _counts, _metrics = stored
    if report is None:
        raise PersistenceError(f"evaluation report {experiment_id} has no report.json")
    return report


def load_evaluation_raw(settings: Settings, experiment_id: str) -> dict[str, object]:
    """Return the saved report.json object used as the aggregate source."""
    path = evaluation_artifact_dir(settings) / experiment_id / "report.json"
    if not path.is_file():
        report = load_evaluation(settings, experiment_id)
        artifact = report.artifact_paths.get("report_json")
        if artifact:
            path = Path(artifact)
    if not path.is_file():
        raise PersistenceError(f"evaluation report {experiment_id} has no report.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PersistenceError(f"evaluation report {experiment_id} is not a JSON object")
    return payload


def list_recorded_runs(settings: Settings) -> list[RecordedRunManifest]:
    root = recorded_run_dir(settings)
    if not root.is_dir():
        return []
    records: list[RecordedRunManifest] = []
    for path in sorted(root.glob("*.json")):
        loaded = _load_recorded_run_file(path)
        if loaded is not None:
            records.append(loaded)
    return records


def load_recorded_run(settings: Settings, run_id: str) -> RecordedRunManifest:
    path = recorded_run_dir(settings) / f"{run_id}.json"
    loaded = _load_recorded_run_file(path)
    if loaded is None:
        raise PersistenceError(f"recorded run {run_id} was not found")
    return loaded


def export_recorded_run(settings: Settings, run_id: str) -> Path:
    """Write a labeled recorded-run manifest. Never invokes apply_proposal."""
    connection = open_database(settings.db_path)
    try:
        run = get_run(connection, run_id)
        if run is None:
            raise PersistenceError(f"run {run_id} was not found")
        workspace = get_workspace(connection, run.workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {run.workspace_id} was not found")
        events = list_run_events(connection, run_id)
        final = get_case_snapshot(connection, run.workspace_id, run.case_id)
        application = _committed_application(connection, run.workspace_id, events)
        opening = _opening_snapshot(final, application)
        public_trace = [
            event.model_copy(update={"payload": _redact_recorded_payload(event.payload)})
            for event in events
        ]
        manifest = RecordedRunManifest(
            run_id=run.run_id,
            original_started_at=run.started_at,
            mode=run.mode,
            provider=run.provider,
            model=run.model,
            source_hash=workspace.dataset_hash,
            policy_hash=workspace.policy_hash,
            prompt_hash=run.prompt_hash,
            tool_hash=run.tool_hash,
            memory_hash=run.memory_snapshot.snapshot_hash,
            public_trace=public_trace,
            input_snapshot=opening,
            final_financial_snapshot=final,
            capture_timestamp=utc_now_iso(),
        )
    finally:
        connection.close()
    path = recorded_run_dir(settings) / f"{run.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _load_recorded_run_file(path: Path) -> RecordedRunManifest | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RecordedRunManifest.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _committed_application(
    connection: sqlite3.Connection, workspace_id: str, events: list[RunEvent]
) -> ApplicationRecord | None:
    for event in events:
        if event.event_kind is not RunEventKind.APPLICATION_COMMITTED:
            continue
        application_id = event.payload.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            continue
        found = get_application(connection, workspace_id, application_id)
        if found is not None and not found.seeded:
            return found
    return None


def _opening_snapshot(final: CaseSnapshot, application: ApplicationRecord | None) -> CaseSnapshot:
    if application is None:
        return final
    restored = {
        item.invoice_id: item.cash_cents + item.fee_cents for item in application.allocations
    }
    invoices = []
    for invoice in final.invoices:
        extra = restored.get(invoice.invoice_id, 0)
        if extra:
            invoices.append(
                invoice.model_copy(update={"outstanding_cents": invoice.outstanding_cents + extra})
            )
        else:
            invoices.append(invoice)
    return final.model_copy(
        update={
            "summary": final.summary.model_copy(update={"case_state": CaseState.OPEN}),
            "payment": final.payment.model_copy(update={"applied": False}),
            "invoices": invoices,
            "ledger_revision": max(0, final.ledger_revision - 1),
        }
    )


_RECORDED_SECRET_KEYS = frozenset(
    {
        "api_key",
        "anthropic_api_key",
        "authorization",
        "password",
        "secret",
        "token",
        "access_token",
        "private_key",
    }
)


def _redact_recorded_payload(payload: dict[str, object]) -> dict[str, object]:
    redacted = _redact_recorded_value(payload)
    return redacted if isinstance(redacted, dict) else {}


def _redact_recorded_value(value: object) -> object:
    if isinstance(value, dict):
        safe: dict[str, object] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _RECORDED_SECRET_KEYS or lowered.endswith("_api_key"):
                safe[str(key)] = "[redacted]"
            else:
                safe[str(key)] = _redact_recorded_value(item)
        return safe
    if isinstance(value, list):
        return [_redact_recorded_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith("sk-ant-") or "ANTHROPIC_API_KEY" in value:
            return "[redacted]"
        return value
    return value


def _summary_from_report_path(report_path: Path) -> SavedEvaluationSummary | None:
    from precedent.evaluation import load_evaluation_report

    try:
        report = load_evaluation_report(report_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return SavedEvaluationSummary(
        experiment_id=report.experiment_id,
        report_path=report_path,
        mode=report.mode,
        state=report.state,
        started_at=report.started_at,
        finished_at=report.finished_at,
        split=report.split,
        scheduled_count=report.scheduled_count,
        completed_count=report.completed_count,
        not_run_count=report.not_run_count,
        timed_out_count=report.timed_out_count,
        budget_skipped_count=report.budget_skipped_count,
    )


def correction_apply_key(correction_id: str) -> str:
    return f"correction-apply:{correction_id}"


def list_trace_events(settings: Settings, run_id: str) -> list[RunEvent]:
    connection = open_database(settings.db_path)
    try:
        return list_run_events(connection, run_id)
    finally:
        connection.close()


def attached_memory_ids(settings: Settings, workspace_id: str) -> tuple[str, ...]:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        return tuple(list_workspace_memory(connection, workspace_id))
    finally:
        connection.close()


def latest_workspace_run(settings: Settings, workspace_id: str) -> RunRecord | None:
    connection = open_database(settings.db_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        if workspace is None:
            raise PersistenceError(f"workspace {workspace_id} was not found")
        return latest_run_for_workspace(connection, workspace_id)
    finally:
        connection.close()


def _workspace_summary(workspace: WorkspaceRecord) -> WorkspaceSummary:
    return WorkspaceSummary(
        workspace_id=workspace.workspace_id,
        name=workspace.name,
        company_id=workspace.company_id,
        period_label=workspace.period_label,
        dataset_hash=workspace.dataset_hash,
        policy_id=workspace.policy.policy_id,
        ledger_revision=workspace.ledger_revision,
        schema_version=workspace.schema_version,
    )


def _payment_related_document_ids(documents: list[Document], payment: PaymentRecord) -> set[str]:
    related: set[str] = set()
    link_refs: set[str] = set()
    for document in documents:
        facts = document.facts
        if not isinstance(facts, RemittanceFacts):
            continue
        if facts.bank_reference != payment.bank_reference:
            continue
        related.add(document.document_id)
        if facts.settlement_ticket:
            link_refs.add(facts.settlement_ticket)
        if facts.transfer_reference:
            link_refs.add(facts.transfer_reference)
    for document in documents:
        facts = document.facts
        if isinstance(facts, BankFeeNoticeFacts) and facts.transfer_reference in link_refs:
            related.add(document.document_id)
    return related


def _case_summary(
    connection: sqlite3.Connection, case: CaseRecord, payment: PaymentRecord
) -> CaseSummary:
    latest_summary = None
    if case.latest_run_id:
        run = get_run(connection, case.latest_run_id)
        if run is not None:
            latest_summary = _summary_from_run(connection, run)
    return CaseSummary(
        case_id=case.case_id,
        payment_id=payment.payment_id,
        payer_text=payment.payer_text,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        posted_date=payment.posted_date,
        case_state=case.state,
        latest_run_id=case.latest_run_id,
        latest_summary=latest_summary,
    )


def _summary_from_run(connection: sqlite3.Connection, run: RunRecord) -> str:
    events = list_run_events(connection, run.run_id)
    for event in reversed(events):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_kind is RunEventKind.RUN_FAILED:
            code = payload.get("error_code") or "ERROR"
            message = payload.get("message") or "Investigation failed."
            return f"ERROR {code}: {message}"[:2000]
        if event.event_kind is RunEventKind.REVIEW_REQUESTED:
            reason = payload.get("reason_code") or "REVIEW"
            return f"Requested review ({reason})."[:2000]
        if event.event_kind is RunEventKind.APPLICATION_COMMITTED:
            application_id = payload.get("application_id")
            if isinstance(application_id, str) and application_id:
                return f"Applied. Application {application_id}."[:2000]
            return "Applied a supported resolution."
    if run.terminal_result is not None:
        return f"Investigation finished as {run.terminal_result.value}."
    return f"Run {run.state.value} ({run.mode.value})."


def investigate(
    settings: Settings,
    workspace_id: str,
    case_id: str,
    *,
    mode: ExecutionMode,
    memory_mode: MemoryMode,
    provider: Provider | None = None,
    run_id: str | None = None,
    memory_snapshot: MemorySnapshot | None = None,
) -> RunResult:
    """Run one bounded investigation. UI and CLI must call this, not the provider.

    ``memory_mode=on`` freezes ACTIVE compatible lessons attached in this
    workspace. ``memory_snapshot`` is an evaluator-only override and is not a
    public UI parameter.
    """
    connection = open_database(settings.db_path)
    try:
        return investigate_case(
            connection,
            settings,
            workspace_id,
            case_id,
            mode=mode,
            memory_mode=memory_mode,
            provider=provider,
            run_id=run_id,
            memory_snapshot=memory_snapshot,
        )
    finally:
        connection.close()


def open_evidence(
    settings: Settings,
    workspace_id: str,
    case_id: str,
    document_id: str,
    *,
    actor: str,
) -> EvidenceRecord:
    """Record controller evidence access for a case. Does not call the investigator."""
    connection = open_database(settings.db_path)
    try:
        return open_evidence_record(
            connection, settings, workspace_id, case_id, document_id, actor=actor
        )
    finally:
        connection.close()


def save_correction(
    settings: Settings,
    workspace_id: str,
    case_id: str,
    correction_input: CorrectionInput | dict[str, object],
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> CorrectionRecord:
    """Save a controller correction without applying money or activating a lesson."""
    connection = open_database(settings.db_path)
    try:
        return save_correction_record(
            connection,
            settings,
            workspace_id,
            case_id,
            correction_input,
            actor=actor,
        )
    finally:
        connection.close()


def apply_correction(
    settings: Settings,
    correction_id: str,
    *,
    idempotency_key: str,
    actor: str,
) -> ApplicationResult:
    """Apply a saved verified correction through the normal financial functions."""
    connection = open_database(settings.db_path)
    try:
        return apply_correction_record(
            connection,
            settings,
            correction_id,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    finally:
        connection.close()


def propose_lesson(
    settings: Settings,
    correction_id: str,
    *,
    mode: ExecutionMode,
    provider: Provider | None = None,
) -> LessonDraftResult:
    """Propose a draft-only structured lesson. Does not activate or attach memory."""
    connection = open_database(settings.db_path)
    try:
        return propose_lesson_record(
            connection,
            settings,
            correction_id,
            mode=mode,
            provider=provider,
        )
    finally:
        connection.close()


def test_lesson(
    settings: Settings,
    precedent_id: str,
    *,
    mode: ExecutionMode,
    provider_factory=None,
) -> LessonTestResult:
    """Run the five-case paired candidate suite. Does not activate the draft."""
    from precedent.learning import test_lesson_record

    connection = open_database(settings.db_path)
    try:
        return test_lesson_record(
            connection,
            settings,
            precedent_id,
            mode=mode,
            provider_factory=provider_factory,
        )
    finally:
        connection.close()


def activate_lesson(
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    from precedent.learning import activate_lesson_record

    connection = open_database(settings.db_path)
    try:
        return activate_lesson_record(connection, settings, precedent_id, actor=actor)
    finally:
        connection.close()


def retire_lesson(
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    from precedent.learning import retire_lesson_record

    connection = open_database(settings.db_path)
    try:
        return retire_lesson_record(connection, settings, precedent_id, actor=actor)
    finally:
        connection.close()


def reject_lesson(
    settings: Settings,
    precedent_id: str,
    *,
    actor: str = DEFAULT_CONTROLLER_ACTOR,
) -> LessonLifecycleResult:
    from precedent.learning import reject_lesson_record

    connection = open_database(settings.db_path)
    try:
        return reject_lesson_record(connection, settings, precedent_id, actor=actor)
    finally:
        connection.close()


def redraft_lesson(settings: Settings, precedent_id: str) -> LessonLifecycleResult:
    from precedent.learning import redraft_lesson_record

    connection = open_database(settings.db_path)
    try:
        return redraft_lesson_record(connection, settings, precedent_id)
    finally:
        connection.close()


def run_evaluation(
    settings: Settings,
    request: EvaluationRequest,
    *,
    provider_factory=None,
    monotonic=None,
    seed: int = DEFAULT_SEED,
):
    """Run a paired baseline/memory experiment on isolated case workspaces."""
    from precedent.evaluation import run_paired_evaluation

    connection = open_database(settings.db_path)
    try:
        kwargs = {"provider_factory": provider_factory, "seed": seed}
        if monotonic is not None:
            kwargs["monotonic"] = monotonic
        return run_paired_evaluation(connection, settings, request, **kwargs)
    finally:
        connection.close()


def apply_stored_proposal(
    settings: Settings,
    context: RunContext,
    proposal_id: str,
    *,
    idempotency_key: str,
    actor: str,
) -> ApplicationResult:
    """Apply a stored proposal through ``apply_proposal`` on a fresh connection."""
    connection = open_database(settings.db_path)
    try:
        return apply_proposal(
            connection,
            context,
            proposal_id,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    finally:
        connection.close()


PERIOD_LABELS = {
    DatasetSplit.TEACHING: "2026-01",
    DatasetSplit.CANDIDATE: "2026-02",
    DatasetSplit.HELDOUT: "2026-03",
}


def merge_packages(packages: list[SourcePackage]) -> MergedSourceBundle:
    if not packages:
        raise FixtureError("cannot import an empty package list")
    company = packages[0].company
    policy = packages[0].policy
    customers: dict[str, Customer] = {}
    invoices: dict[str, Invoice] = {}
    payments: dict[str, Payment] = {}
    payments_by_bank: dict[str, Payment] = {}
    source_documents: list[SourceDocument] = []
    applications: list[SeedApplication] = []
    cases: list[tuple[str, str]] = []
    seen_app_ids: set[str] = set()

    for package in packages:
        if package.company != company:
            raise FixtureError("cannot combine packages with conflicting company metadata")
        if package.policy != policy:
            raise FixtureError("cannot combine packages with conflicting policy metadata")
        if (
            package.company.period_start != company.period_start
            or package.company.period_end != company.period_end
        ):
            raise FixtureError("cannot combine packages from different processing periods")
        for customer in package.customers:
            existing = customers.get(customer.customer_id)
            if existing is None:
                customers[customer.customer_id] = customer
            elif existing != customer:
                raise FixtureError(f"conflicting customer definition for {customer.customer_id}")
        for invoice in package.invoices:
            if invoice.invoice_id in invoices and invoices[invoice.invoice_id] != invoice:
                raise FixtureError(f"conflicting invoice {invoice.invoice_id}")
            invoices[invoice.invoice_id] = invoice
        for payment in package.payments:
            prior = payments_by_bank.get(payment.bank_transaction_id)
            if prior is not None:
                if _payment_source_tuple(prior) != _payment_source_tuple(payment):
                    raise FixtureError(
                        f"conflicting bank transaction {payment.bank_transaction_id}"
                    )
                continue
            if payment.payment_id in payments:
                raise FixtureError(f"duplicate payment {payment.payment_id}")
            payments[payment.payment_id] = payment
            payments_by_bank[payment.bank_transaction_id] = payment
        for document in package.documents:
            if any(item.document_id == document.document_id for item in source_documents):
                raise FixtureError(f"duplicate document {document.document_id}")
            source_documents.append(document)
        for application in package.initial_ledger.applications:
            if application.application_id in seen_app_ids:
                raise FixtureError(f"duplicate seed application {application.application_id}")
            seen_app_ids.add(application.application_id)
            applications.append(application)
        if len(package.payments) != 1:
            raise FixtureError(f"package {package.case_id} must contain exactly one target payment")
        cases.append((package.case_id, package.payments[0].payment_id))

    if len(source_documents) > 250:
        raise FixtureError("combined workspace exceeds 250 source documents")
    dataset_hash = hash_canonical(
        {
            "company": company.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "customers": [item.model_dump(mode="json") for item in customers.values()],
            "invoices": [
                item.model_dump(mode="json")
                for item in sorted(invoices.values(), key=lambda row: row.invoice_id)
            ],
            "payments": [
                item.model_dump(mode="json")
                for item in sorted(payments.values(), key=lambda row: row.payment_id)
            ],
            "documents": [
                item.model_dump(mode="json")
                for item in sorted(source_documents, key=lambda row: row.document_id)
            ],
            "applications": [
                item.model_dump(mode="json")
                for item in sorted(applications, key=lambda row: row.application_id)
            ],
            "cases": cases,
            "package_fingerprints": [package_fingerprint(item) for item in packages],
        }
    )
    return MergedSourceBundle(
        company=company,
        policy=policy,
        customers=tuple(customers.values()),
        invoices=tuple(invoices.values()),
        payments=tuple(payments.values()),
        documents=tuple(source_documents),
        applications=tuple(applications),
        cases=tuple(cases),
        dataset_hash=dataset_hash,
    )


def allocate_workspace_id(connection: sqlite3.Connection, prefix: str) -> str:
    for index in range(1, 10_000):
        candidate = f"WS-{prefix}-{index:03d}"
        if get_workspace(connection, candidate) is None:
            return candidate
    raise FixtureError("unable to allocate a workspace id")


def import_source_bundle(
    connection: sqlite3.Connection,
    workspace_id: str,
    name: str,
    bundle: MergedSourceBundle,
    created_at: str | None = None,
) -> None:
    """Import a merged source bundle into an existing allocated workspace id."""
    _import_bundle(connection, workspace_id, name, bundle, created_at or utc_now_iso())


def _import_bundle(
    connection: sqlite3.Connection,
    workspace_id: str,
    name: str,
    bundle: MergedSourceBundle,
    created_at: str,
) -> None:
    period_label = f"{bundle.company.period_start[:7]}"
    workspace = WorkspaceRecord(
        workspace_id=workspace_id,
        name=name,
        company_id=bundle.company.company_id,
        period_label=period_label,
        period_start=bundle.company.period_start,
        period_end=bundle.company.period_end,
        dataset_hash=bundle.dataset_hash,
        policy=bundle.policy,
        policy_hash=hash_model(bundle.policy),
        ledger_revision=0,
        created_at=created_at,
    )
    applied_payments = {item.payment_id for item in bundle.applications}
    try:
        with transaction(connection, immediate=True):
            insert_workspace(connection, workspace)
            for customer in bundle.customers:
                insert_customer(
                    connection,
                    CustomerRecord(
                        workspace_id=workspace_id,
                        customer_id=customer.customer_id,
                        legal_name=customer.legal_name,
                        display_name=customer.display_name,
                    ),
                )
            for invoice in bundle.invoices:
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
            for payment in bundle.payments:
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
                        applied=payment.payment_id in applied_payments,
                    ),
                )
            for document in bundle.documents:
                insert_document(connection, document.to_persisted(workspace_id))
            for application in bundle.applications:
                insert_application(
                    connection,
                    ApplicationRecord(
                        application_id=application.application_id,
                        workspace_id=workspace_id,
                        payment_id=application.payment_id,
                        proposal_id=None,
                        seeded=True,
                        idempotency_key=f"seed:{application.application_id}",
                        payload_hash=_seed_payload_hash(application),
                        actor="seed_import",
                        allocations=list(application.allocations),
                        created_at=_seed_timestamp(bundle, application.payment_id, created_at),
                    ),
                )
            for case_id, payment_id in bundle.cases:
                insert_case(
                    connection,
                    CaseRecord(
                        workspace_id=workspace_id,
                        case_id=case_id,
                        payment_id=payment_id,
                        state=CaseState.OPEN,
                    ),
                )
    except PersistenceError as exc:
        raise FixtureError(str(exc)) from exc


def _attach_active_memory(
    connection: sqlite3.Connection,
    source_workspace_id: str,
    dest_workspace_id: str,
    settings: Settings,
) -> list[str]:
    source = get_workspace(connection, source_workspace_id)
    dest = get_workspace(connection, dest_workspace_id)
    if source is None:
        raise FixtureError(f"memory source workspace {source_workspace_id} does not exist")
    if dest is None:
        raise FixtureError(f"destination workspace {dest_workspace_id} does not exist")
    if source.company_id != dest.company_id:
        raise FixtureError("memory source is a different company")
    if source.policy_hash != dest.policy_hash:
        raise FixtureError("memory source policy is incompatible")
    snapshot, _notes = load_attached_memory_snapshot(connection, source, settings)
    attached: list[str] = []
    for item in snapshot.hints:
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    INSERT INTO workspace_memory (workspace_id, precedent_id)
                    VALUES (?, ?)
                    """,
                    (dest_workspace_id, item.precedent_id),
                )
        except sqlite3.IntegrityError:
            continue
        attached.append(item.precedent_id)
    return attached


def _require_fixtures(data_dir: Path, seed: int) -> None:
    manifest_path = default_manifest_path(data_dir, seed)
    if not manifest_path.is_file():
        raise FixtureError("fixtures are not generated; run `precedent fixtures build --seed 42`")
    load_manifest(manifest_path)


def _write_active_workspace(var_dir: Path, workspace_id: str) -> None:
    var_dir.mkdir(parents=True, exist_ok=True)
    path = var_dir / "active_workspace.json"
    path.write_text(json.dumps({"workspace_id": workspace_id}, indent=2) + "\n", encoding="utf-8")


def _payment_source_tuple(payment: Payment) -> tuple[object, ...]:
    return (
        payment.bank_transaction_id,
        payment.bank_account_id,
        payment.posted_date,
        payment.currency,
        payment.amount_cents,
        payment.channel,
        payment.payer_text,
        payment.bank_reference,
    )


def _seed_payload_hash(application: SeedApplication) -> str:
    payload = [
        {
            "invoice_id": item.invoice_id,
            "cash_cents": item.cash_cents,
            "fee_cents": item.fee_cents,
        }
        for item in sorted(application.allocations, key=lambda row: row.invoice_id)
    ]
    return hash_canonical(payload)


def _seed_timestamp(bundle: MergedSourceBundle, payment_id: str, fallback: str) -> str:
    for payment in bundle.payments:
        if payment.payment_id == payment_id:
            return f"{payment.posted_date}T12:00:00Z"
    return fallback
