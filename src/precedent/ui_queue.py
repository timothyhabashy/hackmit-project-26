"""Work Queue page: triage payments whose amount does not match what was billed.

Every row asks one question: money arrived, a different amount was billed, and
nothing yet explains the difference. The arithmetic comes from
``precedent.ui_reconcile``, which Case Detail also uses, so the two screens
cannot report different figures for the same payment.

Selection is driven two ways on purpose. Clicking a table row is the primary
gesture, but a dataframe row click has no keyboard or screen-reader equivalent
and cannot be driven by ``streamlit.testing.v1.AppTest``, so the ``case_select``
control remains the accessible and testable path. ``_sync_selection`` keeps the
two pointing at the same case in both directions.
"""

from __future__ import annotations

import streamlit as st

from precedent import ui_reconcile as recon_lib
from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import CaseState, CaseSummary, ExecutionMode, MemoryMode, RunEventKind
from precedent.ui import PAGE_DETAIL_PATH, SEED_COMMAND
from precedent.ui_reconcile import Reconciliation

_TABLE_KEY = "case_table"
_TOKEN_KEY = "queue_table_token"
_FILTER_KEY = "queue_state_filter"
_FILTER_SEEN_KEY = "queue_state_filter_seen"

# States that still owe someone a decision. RESOLVED is excluded so the queue
# opens on work rather than on history.
_NEEDS_WORK = (CaseState.OPEN, CaseState.NEEDS_REVIEW, CaseState.RUNNING, CaseState.ERROR)


def render_queue_page(settings: svc.Settings) -> None:
    """Render the Work Queue. Workspace choice comes from the sidebar."""
    workspace_id = st.session_state.get("workspace_id_choice") or st.session_state.get(
        "remembered_workspace_id"
    )
    if not isinstance(workspace_id, str) or not workspace_id:
        st.info(f"No sandbox workspace is loaded. Run the documented seed command {SEED_COMMAND}.")
        return
    try:
        cases = svc.list_cases(settings, workspace_id)
        counts = svc.queue_counts(settings, workspace_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    theme.section(
        "Work queue",
        subtitle=(
            "Each row is a payment that arrived without an agreed explanation. The "
            "unexplained column is the amount nobody has accounted for yet. Two rows can "
            "carry the same difference for completely different reasons, which is what "
            "opening a case settles."
        ),
    )
    reconciliations = {
        item.case_id: recon_lib.reconcile_summary(settings, workspace_id, item) for item in cases
    }
    _render_kpis(counts, reconciliations)
    if not cases:
        st.info(f"This workspace has no cases. Run the documented seed command {SEED_COMMAND}.")
        return

    visible = _apply_filter(cases)
    selected = _sync_selection(cases, visible)
    _render_table(visible, reconciliations, selected)
    selected = _render_selection_controls(cases, visible, selected)
    st.session_state.remembered_case_id = selected.case_id

    _render_selected_case(settings, workspace_id, selected, reconciliations[selected.case_id])
    _render_run_panel(settings, workspace_id, selected, runs_in_workspace=counts.runs)


def _render_kpis(counts: object, reconciliations: dict[str, Reconciliation]) -> None:
    """Five bordered metrics, led by the money nobody has explained yet."""
    open_gaps = [
        abs(item.unexplained_cents)
        for item in reconciliations.values()
        if item.unexplained_cents is not None
    ]
    unknown = sum(1 for item in reconciliations.values() if item.unexplained_cents is None)
    unexplained_help = (
        "Total money in question across the queue: every difference between matched "
        "invoice balance and cash received, for payments not yet applied."
    )
    if unknown:
        unexplained_help += (
            f" {unknown} case(s) have no remittance naming an invoice held here, so their "
            "difference cannot be derived and is not counted."
        )
    # Keyed so the theme can let five cards wrap onto a second row rather than
    # squeeze each one until "Technical errors" reads "Te..." on a 1024px screen.
    with st.container(key="pc-kpis-queue"):
        columns = st.columns(5)
        with columns[0]:
            theme.kpi(
                "Unexplained",
                theme.money(sum(open_gaps)),
                icon=":material/priority_high:",
                help=unexplained_help,
            )
        with columns[1]:
            theme.kpi("Open", counts.open, icon=":material/inbox:")
        with columns[2]:
            theme.kpi(
                "Needs review",
                counts.needs_review,
                icon=":material/flag:",
                help="The investigator stopped and asked a person to decide.",
            )
        with columns[3]:
            theme.kpi("Resolved", counts.resolved, icon=":material/task_alt:")
        with columns[4]:
            theme.kpi(
                "Technical errors",
                counts.errors,
                icon=":material/error:",
                help="A run failed for a technical reason. This is not a financial outcome.",
            )
    if counts.running:
        theme.render_inline(
            theme.quiet(
                f"{counts.running} case(s) are marked RUNNING. A row left in that state has "
                "a persisted run row and no terminal outcome."
            )
        )


def _apply_filter(cases: list[CaseSummary]) -> list[CaseSummary]:
    """Filter by case state, defaulting to the states that still need work."""
    present = [state for state in CaseState if any(item.case_state is state for item in cases)]
    if not present:
        return cases
    _seed_filter(present)
    chosen = st.pills(
        "Show states",
        options=present,
        selection_mode="multi",
        format_func=theme.state_text,
        key=_FILTER_KEY,
        help="Clearing every pill shows all states.",
    )
    picked = set(chosen or present)
    visible = [item for item in cases if item.case_state in picked]
    hidden = len(cases) - len(visible)
    if hidden:
        theme.render_inline(theme.quiet(f"{hidden} case(s) hidden by the state filter."))
    return visible


def _seed_filter(present: list[CaseState]) -> None:
    """Own the pill selection in session state so the widget needs no default.

    Passing ``default=`` alongside a session-state value makes Streamlit warn, so
    the selection is seeded here instead. Two rules keep work from vanishing: a
    state the controller has never been offered is added when it needs work, so a
    run that ends in NEEDS_REVIEW cannot hide the case it just changed; and a
    state no longer present is dropped so the stored value stays a valid option.
    An empty selection is left empty, because clearing every pill means show all.
    """
    seen = st.session_state.get(_FILTER_SEEN_KEY)
    stored = st.session_state.get(_FILTER_KEY)
    if isinstance(stored, list) and isinstance(seen, list):
        fresh = [item for item in present if item not in seen and item in _NEEDS_WORK]
        selection = [*(item for item in stored if item in present), *fresh]
    else:
        selection = [item for item in present if item in _NEEDS_WORK]
    st.session_state[_FILTER_KEY] = selection
    st.session_state[_FILTER_SEEN_KEY] = list(present)


def _sync_selection(cases: list[CaseSummary], visible: list[CaseSummary]) -> CaseSummary:
    """Reconcile the table row selection with the ``case_select`` control.

    A row click wins when the table's selection changed since the last script
    run. Otherwise the remembered case wins and is pushed back into the table so
    the highlight follows a keyboard-driven choice. The remembered case is never
    dropped because a filter hides it: losing a selection silently would send the
    Case Detail page to a case the controller did not choose.
    """
    by_id = {item.case_id: item for item in cases}
    pool = visible or cases
    table_rows = _stored_table_rows()
    previous = st.session_state.get(_TOKEN_KEY)
    st.session_state[_TOKEN_KEY] = table_rows

    remembered = st.session_state.get("case_select")
    if remembered not in by_id:
        remembered = st.session_state.get("remembered_case_id")
    if remembered not in by_id:
        remembered = pool[0].case_id

    clicked_new_row = bool(table_rows) and previous is not None and table_rows != previous
    if clicked_new_row and table_rows[0] < len(visible):
        remembered = visible[table_rows[0]].case_id
    else:
        _highlight_row(visible, remembered)
    st.session_state.case_select = remembered
    return by_id[remembered]


def _stored_table_rows() -> tuple[int, ...]:
    state = st.session_state.get(_TABLE_KEY)
    selection = getattr(state, "selection", None)
    if selection is None and isinstance(state, dict):
        selection = state.get("selection")
    rows = getattr(selection, "rows", None)
    if rows is None and isinstance(selection, dict):
        rows = selection.get("rows")
    if not rows:
        return ()
    return tuple(int(value) for value in rows)


def _highlight_row(visible: list[CaseSummary], case_id: str) -> None:
    """Point the table at ``case_id`` before the widget is instantiated."""
    index = next((pos for pos, item in enumerate(visible) if item.case_id == case_id), None)
    target = () if index is None else (index,)
    if _stored_table_rows() == target:
        return
    st.session_state[_TABLE_KEY] = {"selection": {"rows": list(target)}}
    st.session_state[_TOKEN_KEY] = target


def _render_table(
    visible: list[CaseSummary],
    reconciliations: dict[str, Reconciliation],
    selected: CaseSummary,
) -> None:
    """The queue table. Received, invoiced, and the difference read left to right."""
    if not visible:
        st.info("No case matches the selected states. Add a state pill to see more.")
        return
    rows = []
    for item in visible:
        recon = reconciliations[item.case_id]
        rows.append(
            {
                "Case": item.case_id,
                "Payer label": item.payer_text,
                "Received": _dollars(recon.received_cents),
                "Invoice balance": _dollars(recon.invoice_outstanding_cents),
                "Unexplained": _dollars(recon.unexplained_cents),
                "Matched to": recon.basis,
                "State": theme.state_text(item.case_state),
                "Latest result": item.latest_summary or "-",
            }
        )
    index = next(
        (pos for pos, item in enumerate(visible) if item.case_id == selected.case_id), None
    )
    st.dataframe(
        rows,
        key=_TABLE_KEY,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        selection_default=None if index is None else {"selection": {"rows": [index]}},
        placeholder="unknown",
        column_config={
            "Case": st.column_config.TextColumn("Case", width="small"),
            # Narrow on purpose. At 1024px the table scrolls horizontally, and
            # the unexplained difference must stay on screen without scrolling.
            "Payer label": st.column_config.TextColumn("Payer label", width="small"),
            "Received": st.column_config.NumberColumn(
                "Received", format="$%,.2f", help="Cash that actually landed in the bank account."
            ),
            "Invoice balance": st.column_config.NumberColumn(
                "Invoice balance",
                format="$%,.2f",
                help=(
                    "What the invoices named on the remittance advice still owe, as the "
                    "ledger stands now. Blank when no remittance names an invoice held here."
                ),
            ),
            "Unexplained": st.column_config.NumberColumn(
                "Unexplained",
                format="$%,.2f",
                help=(
                    "Invoice balance minus cash received, for payments not yet applied. "
                    "Positive means money is still owed, negative means more arrived than "
                    "was billed. A documented bank fee and a customer who still owes the "
                    "money look identical here, which is the question an investigation "
                    "answers. Blank means the difference cannot be derived."
                ),
            ),
            "Matched to": st.column_config.TextColumn("Matched to", width="small"),
            "State": st.column_config.TextColumn("State", width="small"),
            "Latest result": st.column_config.TextColumn("Latest result", width="medium"),
        },
    )


def _render_selection_controls(
    cases: list[CaseSummary], visible: list[CaseSummary], selected: CaseSummary
) -> CaseSummary:
    """Keyboard-reachable selection plus the jump into Case Detail."""
    by_id = {item.case_id: item for item in cases}
    options = [item.case_id for item in visible]
    if selected.case_id not in options:
        options = [selected.case_id, *options]
    select_col, open_col = st.columns([3, 1], vertical_alignment="bottom")
    with select_col:
        chosen_id = st.selectbox(
            "Selected case",
            options=options,
            format_func=lambda value: _case_option_label(by_id[value]),
            key="case_select",
            help=(
                "Clicking a row in the table above does the same thing. This control is the "
                "keyboard and screen reader path, and the two stay in sync."
            ),
        )
    with open_col:
        if st.button(
            "Open case",
            key="open_case",
            type="primary",
            icon=":material/open_in_new:",
            width="stretch",
        ):
            st.switch_page(PAGE_DETAIL_PATH)
    return by_id.get(chosen_id, selected)


def _render_selected_case(
    settings: svc.Settings,
    workspace_id: str,
    selected: CaseSummary,
    recon: Reconciliation,
) -> None:
    """The reconciliation for the selected case, stated as money rather than prose."""
    theme.render_inline(
        theme.state_badge(selected.case_state),
        theme.ident(selected.case_id, label="case", width_ch=20),
        theme.quiet(f"{selected.payer_text} \u00b7 posted {selected.posted_date}"),
    )
    outstanding = recon.invoice_outstanding_cents
    if recon.settled:
        balance = "unknown" if outstanding is None else theme.money(outstanding)
        st.success(
            f"{theme.money(recon.received_cents)} has already been applied to invoices. "
            f"Nothing about this payment is unexplained. Matched invoice balance is now "
            f"{balance}."
        )
    elif outstanding is None:
        st.warning(
            f"{recon.basis}. Without a remittance naming an invoice held in this workspace "
            "there is no balance to compare against, so the difference is unknown rather "
            "than zero."
        )
    else:
        theme.gap_strip(
            recon.received_cents,
            outstanding,
            currency=selected.currency,
            source=f"Matched to {recon.basis} from the remittance advice on file."
            if not recon.is_open_question
            else None,
        )
    error = st.session_state.last_action_error
    if isinstance(error, dict) and error.get("case_id") == selected.case_id:
        st.error(f"{error.get('code')}: {error.get('message')}")
    if selected.case_state is CaseState.RESOLVED:
        st.info("This case is already resolved. Open the result rather than running again.")
    elif selected.case_state is CaseState.ERROR:
        st.warning("The last investigation failed. Retry is a new explicit action, not a review.")
    if selected.latest_summary:
        theme.body(selected.latest_summary)


@st.fragment
def _render_run_panel(
    settings: svc.Settings,
    workspace_id: str,
    selected: CaseSummary,
    *,
    runs_in_workspace: int,
) -> None:
    """Start an investigation and show what the last one actually did.

    This is a fragment so that toggling memory or expanding the trace does not
    re-query every case in the queue. Starting a run still reruns the whole app,
    because a finished run changes the case state shown in the table above.
    """
    try:
        case_runs = svc.count_case_runs(settings, workspace_id, selected.case_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    theme.section(
        "Investigate",
        subtitle=(
            f"One bounded run: at most {settings.max_model_calls} model calls and "
            f"{settings.max_case_seconds} seconds. This case has {case_runs} persisted run(s), "
            f"the workspace {runs_in_workspace}."
        ),
    )
    memory_on = st.checkbox(
        "Memory enabled",
        value=True,
        key="memory_enabled",
        help="Freeze the ACTIVE lessons attached to this workspace and let the run read them.",
    )
    _render_run_action(settings, workspace_id, selected, memory_on=memory_on)
    _render_last_run(settings, selected)


def _render_run_action(
    settings: svc.Settings,
    workspace_id: str,
    selected: CaseSummary,
    *,
    memory_on: bool,
) -> None:
    missing = svc.missing_live_variable_names(settings)
    in_progress = bool(st.session_state.run_in_progress)
    resolved = selected.case_state is CaseState.RESOLVED
    running = selected.case_state is CaseState.RUNNING or in_progress
    live_blocked = bool(missing)
    if live_blocked:
        run_help = "Chargeable live run disabled because " + ", ".join(missing) + "."
    elif resolved:
        run_help = "Resolved cases open the stored result instead of running again."
    elif running:
        run_help = "A run is already in progress for this case."
    else:
        run_help = "Spend one bounded live investigation on the selected case."
    run_clicked = st.button(
        "Run agent on selected case",
        key="run_selected_case",
        disabled=live_blocked or resolved or running,
        help=run_help,
        icon=":material/play_arrow:",
        type="secondary",
    )
    if live_blocked:
        # State the block in the page, not only in a tooltip. The sidebar
        # readiness panel names the variables; this says what it means here.
        theme.render_inline(
            theme.provenance_badge("LIVE UNAVAILABLE"),
            theme.quiet(
                "No model call can be made from this checkout. Nothing shown below is a "
                "live result."
            ),
        )
    if run_clicked:
        _run_investigation(settings, workspace_id, selected, memory_on=memory_on)


def _run_investigation(
    settings: svc.Settings,
    workspace_id: str,
    selected: CaseSummary,
    *,
    memory_on: bool,
) -> None:
    """Execute one investigation inside a status container.

    ``svc.investigate`` is synchronous and returns only once the run reaches a
    terminal outcome, so there is no partial event stream to render while it
    works. The status therefore reports exactly two honest things: that a call is
    in flight, and what came back. No intermediate step is invented to make the
    wait look busy.
    """
    case_id = selected.case_id
    reason = svc.live_readiness_code(settings)
    if reason is not None:
        missing = svc.missing_live_variable_names(settings)
        st.session_state.last_action_error = {
            "case_id": case_id,
            "code": reason,
            "message": "Live investigation is not configured. Missing: " + ", ".join(missing) + ".",
        }
        return
    st.session_state.run_in_progress = True
    st.session_state.last_action_error = None
    try:
        with st.status(f"Investigating {case_id}", expanded=True) as status:
            st.write(
                f"Live model call in flight. Budget: {settings.max_model_calls} model calls "
                f"and {settings.max_case_seconds} seconds for this case."
            )
            result = svc.investigate(
                settings,
                workspace_id,
                case_id,
                mode=ExecutionMode.LIVE,
                memory_mode=MemoryMode.ON if memory_on else MemoryMode.OFF,
            )
            st.session_state.last_run_id = result.run_id
            if result.error is not None:
                st.session_state.last_action_error = {
                    "case_id": case_id,
                    "code": result.error.code.value,
                    "message": result.error.message,
                }
                status.update(label=f"Run {result.run_id} ended with an error", state="error")
            else:
                status.update(
                    label=f"Run {result.run_id}: {result.terminal_outcome.value}",
                    state="complete",
                )
                st.write(result.summary)
    except svc.InvestigationError as exc:
        st.session_state.last_action_error = {
            "case_id": case_id,
            "code": exc.code,
            "message": exc.message,
        }
    except svc.PersistenceError as exc:
        st.session_state.last_action_error = {
            "case_id": case_id,
            "code": "INTERNAL_ERROR",
            "message": str(exc),
        }
    finally:
        st.session_state.run_in_progress = False
    st.rerun()


def _render_last_run(settings: svc.Settings, selected: CaseSummary) -> None:
    """Budget consumption and the event timeline for this case's latest run."""
    run_id = selected.latest_run_id
    if not run_id:
        theme.render_inline(theme.quiet("No persisted model run is attached to this case yet."))
        return
    try:
        events = svc.list_trace_events(settings, run_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    mode = _run_mode(events)
    theme.render_inline(
        theme.provenance_badge(mode) if mode is not None else theme.provenance_badge("UNKNOWN"),
        theme.ident(run_id, label="run", width_ch=22),
    )
    theme.budget_strip(_budget_readings(settings, events))
    theme.trace_timeline(events, empty_message=f"Run {run_id} has no stored events.")


def _budget_readings(settings: svc.Settings, events: list) -> list[theme.BudgetReading]:
    """Budget consumption counted from the stored trace.

    Counting events is the only per-run usage the service seam exposes, and it
    is exact for calls: one ``model_response`` event is one model call. Elapsed
    time is the model time those events report, which is a floor on wall clock,
    so it is labelled as model time rather than presented as the deadline clock.
    """
    model_calls = sum(1 for item in events if item.event_kind is RunEventKind.MODEL_RESPONSE)
    tool_calls = sum(1 for item in events if item.event_kind is RunEventKind.TOOL_CALLED)
    elapsed_ms = 0
    for item in events:
        usage = item.payload.get("usage") if isinstance(item.payload, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("elapsed_ms"), int):
            elapsed_ms += usage["elapsed_ms"]
    return [
        theme.BudgetReading("Model calls", model_calls, settings.max_model_calls),
        theme.BudgetReading("Tool calls", tool_calls, settings.max_tool_calls),
        theme.BudgetReading(
            "Model time", elapsed_ms / 1000, settings.max_case_seconds, unit="s", decimals=1
        ),
    ]


def _run_mode(events: list) -> ExecutionMode | None:
    """Read run provenance from the trace rather than from current settings."""
    for item in events:
        if item.event_kind is not RunEventKind.RUN_STARTED:
            continue
        raw = item.payload.get("execution_mode") if isinstance(item.payload, dict) else None
        try:
            return ExecutionMode(raw)
        except ValueError:
            return None
    return None


def _case_option_label(item: CaseSummary) -> str:
    amount = theme.money(item.amount_cents)
    return f"{item.case_id} \u00b7 {item.payer_text} \u00b7 {amount} \u00b7 {item.case_state.value}"


def _dollars(cents: int | None) -> float | None:
    """Convert integer cents to dollars for NumberColumn formatting only.

    Money stays in integer cents everywhere else in this module, including every
    value handed to the service layer. This conversion exists solely so the table
    sorts numerically and right-aligns.
    """
    return None if cents is None else cents / 100
