"""Streamlit shell: navigation, persistent sidebar, and session bootstrapping.

Page bodies live in ``ui_queue``, ``ui_case``, ``ui_learning``, and
``ui_results``. The thin scripts under ``app_pages/`` are what ``st.navigation``
registers, which is also what makes ``AppTest.switch_page()`` work. Service
access goes through ``precedent.ui_services``; presentation goes through
``precedent.ui_theme``.
"""

from __future__ import annotations

import streamlit as st

from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import DatasetSplit, ExecutionMode

PAGE_QUEUE_PATH = "app_pages/work_queue.py"
PAGE_DETAIL_PATH = "app_pages/case_detail.py"
PAGE_LEARNING_PATH = "app_pages/lessons.py"
PAGE_RESULTS_PATH = "app_pages/results.py"
PAGE_QUEUE = "Work Queue"
PAGE_DETAIL = "Case Detail"
PAGE_LEARNING = "Lessons"
PAGE_RESULTS = "Results"
PAGE_TITLES = {
    PAGE_QUEUE_PATH: PAGE_QUEUE,
    PAGE_DETAIL_PATH: PAGE_DETAIL,
    PAGE_LEARNING_PATH: PAGE_LEARNING,
    PAGE_RESULTS_PATH: PAGE_RESULTS,
}
SEED_COMMAND = "`precedent demo init`"
_SETTINGS_KEY = "active_settings"
_PAGE_KEY = "active_page"


def render_app() -> None:
    """Entry point for app.py. Renders chrome, then delegates to the active page."""
    st.set_page_config(page_title="Precedent", layout="wide")
    try:
        settings = svc.load_settings()
    except svc.ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.caption("Fix local settings, then reload. Secret values are never displayed.")
        return
    st.session_state[_SETTINGS_KEY] = settings
    _ensure_state(settings)
    pages = [
        # No url_path on the default page. Streamlit ignores it and serves the
        # default at "/", so declaring one advertises a route that answers with
        # "Page not found" before falling back to this same page.
        st.Page(PAGE_QUEUE_PATH, title=PAGE_QUEUE, default=True),
        st.Page(PAGE_DETAIL_PATH, title=PAGE_DETAIL, url_path="case-detail"),
        st.Page(PAGE_LEARNING_PATH, title=PAGE_LEARNING, url_path="lessons"),
        st.Page(PAGE_RESULTS_PATH, title=PAGE_RESULTS, url_path="results"),
    ]
    active = st.navigation(pages, position="top")
    st.session_state[_PAGE_KEY] = active.title
    theme.inject_theme()
    _render_sidebar(settings)
    _render_context_strip(settings)
    active.run()


def page_settings() -> svc.Settings:
    """Settings resolved by the shell for this script run.

    Page scripts call this instead of loading settings again, so a run cannot
    see two different configurations.
    """
    settings = st.session_state.get(_SETTINGS_KEY)
    if settings is None:
        settings = svc.load_settings()
        st.session_state[_SETTINGS_KEY] = settings
    return settings


def active_page_title() -> str:
    title = st.session_state.get(_PAGE_KEY)
    return title if isinstance(title, str) else PAGE_QUEUE


def _ensure_state(settings: svc.Settings) -> None:
    workspace_choice = st.session_state.get("workspace_id_choice")
    if isinstance(workspace_choice, str) and workspace_choice:
        st.session_state.remembered_workspace_id = workspace_choice
    case_choice = st.session_state.get("case_select")
    if isinstance(case_choice, str) and case_choice:
        st.session_state.remembered_case_id = case_choice
    if "remembered_workspace_id" not in st.session_state:
        st.session_state.remembered_workspace_id = svc.read_active_workspace_id(settings)
    if "remembered_case_id" not in st.session_state:
        st.session_state.remembered_case_id = None
    if "workspace_id_choice" not in st.session_state:
        st.session_state.workspace_id_choice = st.session_state.remembered_workspace_id
    if "case_select" not in st.session_state:
        st.session_state.case_select = st.session_state.remembered_case_id
    if "run_in_progress" not in st.session_state:
        st.session_state.run_in_progress = False
    if "last_action_error" not in st.session_state:
        st.session_state.last_action_error = None
    if "last_run_id" not in st.session_state:
        st.session_state.last_run_id = None
    if "mutation_in_progress" not in st.session_state:
        st.session_state.mutation_in_progress = False
    if "last_detail_error" not in st.session_state:
        st.session_state.last_detail_error = None
    if "last_apply_result" not in st.session_state:
        st.session_state.last_apply_result = None
    if "last_learning_error" not in st.session_state:
        st.session_state.last_learning_error = None
    if "last_learning_result" not in st.session_state:
        st.session_state.last_learning_result = None
    if "loaded_experiment_id" not in st.session_state:
        st.session_state.loaded_experiment_id = None
    if "remembered_precedent_id" not in st.session_state:
        st.session_state.remembered_precedent_id = None
    if "viewing_recorded_run_id" not in st.session_state:
        st.session_state.viewing_recorded_run_id = None
    if "last_admin_message" not in st.session_state:
        st.session_state.last_admin_message = None


def _render_sidebar(settings: svc.Settings) -> None:
    """Workspace, memory, live readiness, and demo admin: global context only."""
    with st.sidebar:
        with st.container(key="pc-sidebar-brand"):
            theme.section(
                "Precedent",
                subtitle="Teach an investigation once. Check every application.",
            )
            theme.render_inline(
                theme.provenance_badge("HUMAN"),
                theme.quiet("Synthetic data \u00b7 Sandbox ledger"),
            )
        workspace_id = _render_workspace_picker(settings)
        _render_memory_summary(settings, workspace_id)
        with st.expander("Live readiness", expanded=False):
            theme.readiness_panel(settings)
        _render_demo_admin(settings, workspace_id)


def _render_workspace_picker(settings: svc.Settings) -> str | None:
    """Workspace choice is global state, so it is picked once in the sidebar."""
    try:
        workspaces = svc.list_workspace_summaries(settings)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return None
    if not workspaces:
        st.info(f"No sandbox workspace is loaded. Run the documented seed command {SEED_COMMAND}.")
        return None
    workspace_ids = [item.workspace_id for item in workspaces]
    labels = {item.workspace_id: f"{item.workspace_id} - {item.name}" for item in workspaces}
    current = st.session_state.get("workspace_id_choice")
    remembered = st.session_state.get("remembered_workspace_id")
    if current not in workspace_ids and remembered in workspace_ids:
        st.session_state.workspace_id_choice = remembered
        current = remembered
    if current not in workspace_ids:
        st.session_state.workspace_id_choice = workspace_ids[0]
    workspace_id = st.radio(
        "Workspace",
        options=workspace_ids,
        format_func=lambda value: labels.get(value, value),
        key="workspace_id_choice",
    )
    st.session_state.remembered_workspace_id = workspace_id
    return workspace_id


def _render_memory_summary(settings: svc.Settings, workspace_id: str | None) -> None:
    if not workspace_id:
        return
    try:
        memory_ids = svc.attached_memory_ids(settings, workspace_id)
    except svc.PersistenceError:
        return
    with st.container(key="pc-sidebar-memory"):
        theme.kpi(
            "Memory attached",
            f"{len(memory_ids)} ACTIVE",
            icon=":material/school:",
            help=(
                "ACTIVE lessons attached to this workspace. Ordinary investigation uses them "
                "only when Memory enabled is on."
            ),
        )


def _render_demo_admin(settings: svc.Settings, workspace_id: str | None) -> None:
    """Workspace creation sits behind a popover so it is not clicked mid-demo."""
    message = st.session_state.get("last_admin_message")
    if isinstance(message, str) and message:
        st.success(message)
    in_progress = bool(st.session_state.mutation_in_progress)
    with st.popover("Demo workspaces", icon=":material/database:", width="stretch"):
        theme.body(
            "Creating a workspace seeds another synthetic teaching ledger and makes it "
            "active. It deletes no experiment, lesson, report file, or recorded-run manifest."
        )
        st.caption("CLI equivalent: `precedent demo new`.")
        new_clicked = st.button(
            "New demo workspace",
            key="new_demo_workspace",
            disabled=in_progress,
            help="Seed a new teaching workspace with no learned memory.",
            width="stretch",
        )
        follow_disabled = in_progress or not workspace_id
        follow_help = (
            "Import February candidate packages and attach currently ACTIVE compatible "
            f"lessons from `{workspace_id}`."
            if workspace_id
            else "Select a source workspace first."
        )
        follow_clicked = st.button(
            "Candidate follow-up workspace",
            key="candidate_followup_workspace",
            disabled=follow_disabled,
            help=follow_help,
            width="stretch",
        )
    if new_clicked and not in_progress:
        _handle_demo_new(settings, dataset=DatasetSplit.TEACHING, memory_from=None)
        return
    if follow_clicked and not follow_disabled and workspace_id:
        _handle_demo_new(settings, dataset=DatasetSplit.CANDIDATE, memory_from=workspace_id)


def _handle_demo_new(
    settings: svc.Settings, *, dataset: DatasetSplit, memory_from: str | None
) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.demo_new(settings, dataset=dataset, memory_from=memory_from)
        attached = list(result.attached_precedent_ids)
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "demo",
            "workspace_id": result.workspace_id,
            "attached": attached,
        }
        st.session_state.last_admin_message = (
            f"Created workspace `{result.workspace_id}`. Attached {len(attached)} lesson(s)."
        )
        st.session_state.remembered_workspace_id = result.workspace_id
        st.session_state.workspace_id_choice = result.workspace_id
        st.session_state.remembered_case_id = None
        st.session_state.case_select = None
    except (svc.FixtureError, svc.PersistenceError) as exc:
        code = getattr(exc, "code", "INTERNAL_ERROR")
        if hasattr(code, "value"):
            code = code.value
        message = getattr(exc, "message", str(exc))
        st.session_state.last_admin_message = None
        st.session_state.last_learning_error = {"code": code, "message": message}
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _render_context_strip(settings: svc.Settings) -> None:
    """One row of run provenance, shown above every page."""
    last = _latest_run(settings)
    label = _provenance_label(settings, last)
    fragments = [theme.provenance_badge(label)]
    if last is None:
        fragments.append(theme.quiet("No persisted run in this workspace yet."))
    else:
        model = last.model or "UNSET"
        fragments.append(theme.quiet("Last run"))
        fragments.append(theme.ident(last.run_id, width_ch=18))
        fragments.append(
            theme.quiet(
                f"{theme.execution_mode_label(last.mode)} \u00b7 state {last.state.value} "
                f"\u00b7 model {model} \u00b7 started {last.started_at}"
            )
        )
    with st.container(key="pc-quiet-context"):
        theme.render_inline(*fragments)


def _latest_run(settings: svc.Settings) -> object | None:
    workspace_id = st.session_state.get("workspace_id_choice") or st.session_state.get(
        "remembered_workspace_id"
    )
    if not isinstance(workspace_id, str) or not workspace_id:
        return None
    try:
        return svc.latest_workspace_run(settings, workspace_id)
    except svc.PersistenceError:
        return None


def _provenance_label(settings: svc.Settings, last: object) -> str:
    recorded_id = st.session_state.get("recorded_run_select") or st.session_state.get(
        "viewing_recorded_run_id"
    )
    # The recorded-run viewer moved to Results, so the badge follows that page.
    on_recorded_page = active_page_title() == PAGE_RESULTS
    if on_recorded_page and (
        (isinstance(recorded_id, str) and recorded_id) or bool(svc.list_recorded_runs(settings))
    ):
        return "RECORDED RUN"
    if last is not None and getattr(last, "mode", None) is ExecutionMode.TEST:
        return "TEST SIMULATION"
    if svc.missing_live_variable_names(settings):
        return "LIVE UNAVAILABLE"
    return "LIVE"
