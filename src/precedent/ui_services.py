"""Single seam between the Streamlit pages and the service layer.

Every callable, error type, and tuning constant the UI needs is re-exported
here. Page modules must reach the service layer through this module object and
never bind the names directly::

    from precedent import ui_services as svc

    settings = svc.load_settings()
    detail = svc.get_case_detail(settings, workspace_id, case_id)

Importing the module rather than the names keeps one stable patch surface, so
``monkeypatch.setattr("precedent.ui_services.investigate", ...)`` reaches every
page. ``from precedent.ui_services import investigate`` would bind at import
time and silently bypass the patch.

Pydantic models are not re-exported. They are inert data and are imported
directly from ``precedent.models`` by whichever page needs them.
"""

from __future__ import annotations

from precedent.agent import InvestigationError
from precedent.config import (
    ConfigError,
    Settings,
    live_readiness_code,
    load_settings,
    missing_live_variable_names,
)
from precedent.db import PersistenceError
from precedent.evaluation import DEFAULT_EVALUATION_DEADLINE_SECONDS, EvaluationError
from precedent.fixtures import (
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    FixtureError,
)
from precedent.learning import DEFAULT_CONTROLLER_ACTOR, LearningError
from precedent.providers import ProviderError
from precedent.services import (
    SavedEvaluationSummary,
    activate_lesson,
    apply_correction,
    attached_memory_ids,
    correction_apply_key,
    count_case_runs,
    demo_new,
    evaluation_artifact_dir,
    get_case_detail,
    get_correction_record,
    get_customer_label,
    get_lesson_test,
    get_source_document,
    investigate,
    latest_workspace_run,
    lesson_activation_blockers,
    list_cases,
    list_lessons,
    list_lessons_for_correction,
    list_recorded_runs,
    list_saved_evaluations,
    list_trace_events,
    list_workspace_summaries,
    load_evaluation,
    load_evaluation_raw,
    open_evidence,
    propose_lesson,
    queue_counts,
    read_active_workspace_id,
    redraft_lesson,
    reject_lesson,
    retire_lesson,
    reviewer_dev_case_notes,
    run_evaluation,
    save_correction,
    test_lesson,
)

__all__ = [
    "DEFAULT_CONTROLLER_ACTOR",
    "DEFAULT_EVALUATION_DEADLINE_SECONDS",
    "T03_FEE_ID",
    "T03_INVOICE_ID",
    "T03_PAYMENT_ID",
    "T03_REMIT_ID",
    "ConfigError",
    "EvaluationError",
    "FixtureError",
    "InvestigationError",
    "LearningError",
    "PersistenceError",
    "ProviderError",
    "SavedEvaluationSummary",
    "Settings",
    "activate_lesson",
    "apply_correction",
    "attached_memory_ids",
    "correction_apply_key",
    "count_case_runs",
    "demo_new",
    "evaluation_artifact_dir",
    "get_case_detail",
    "get_correction_record",
    "get_customer_label",
    "get_lesson_test",
    "get_source_document",
    "investigate",
    "latest_workspace_run",
    "lesson_activation_blockers",
    "list_cases",
    "list_lessons",
    "list_lessons_for_correction",
    "list_recorded_runs",
    "list_saved_evaluations",
    "list_trace_events",
    "list_workspace_summaries",
    "live_readiness_code",
    "load_evaluation",
    "load_evaluation_raw",
    "load_settings",
    "missing_live_variable_names",
    "open_evidence",
    "propose_lesson",
    "queue_counts",
    "read_active_workspace_id",
    "redraft_lesson",
    "reject_lesson",
    "retire_lesson",
    "reviewer_dev_case_notes",
    "run_evaluation",
    "save_correction",
    "test_lesson",
]
