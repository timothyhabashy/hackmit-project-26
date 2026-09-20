"""Results page: saved paired evaluations and recorded runs.

Lessons and their lifecycle live on the Lessons page (``ui_learning``). This
page reads stored artifacts. Loading a report never reruns an experiment, and
viewing a recorded run never invokes ``apply_proposal``.

Every number shown here comes from a stored ``report.json``. Nothing is
zero-filled: a metric with a zero denominator reads ``N/A``, and a metric whose
two arms were scored over different denominators is named as not comparable
rather than charted.
"""

from __future__ import annotations

from typing import Any

import streamlit as st
from pydantic import ValidationError

from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import (
    DatasetSplit,
    EvaluationArmMetrics,
    EvaluationRequest,
    ExecutionMode,
    MetricCount,
)

# Metrics that carry an explicit denominator, so baseline and memory can be put
# side by side without inventing a rate.
_CHART_METRICS: tuple[tuple[str, str], ...] = (
    ("Correct autonomous", "correct_autonomous_resolutions"),
    ("Correct of resolvable", "correct_autonomous_resolutions_resolvable"),
    ("Incorrect automatic", "incorrect_automatic_resolutions"),
    ("Correct reviews", "correct_reviews"),
    ("Unnecessary reviews", "unnecessary_reviews"),
    ("Technical errors", "technical_errors"),
)

_BASELINE_COLOR = theme.FAINT
_MEMORY_COLOR = theme.NAVY

RECORDED_RUN_LABEL = "Recorded run - no live model calls"


def render_results_page(settings: svc.Settings) -> None:
    """Render saved evaluation reports and recorded runs for this checkout."""
    workspace_id = st.session_state.get("workspace_id_choice") or st.session_state.get(
        "remembered_workspace_id"
    )
    theme.section(
        "Results",
        subtitle=(
            "Paired evaluations and recorded runs. Load report reads "
            "artifacts/evaluations/<id>/report.json and never starts a new experiment. "
            "Prefer the CLI for long final evaluations."
        ),
    )
    _render_evaluation_status()
    if not isinstance(workspace_id, str) or not workspace_id:
        workspace_id = None
    _render_saved_evaluation_section(settings, workspace_id=workspace_id)
    _render_recorded_runs(settings)


def _render_evaluation_status() -> None:
    error = st.session_state.last_learning_error
    if isinstance(error, dict):
        st.error(f"{error.get('code')}: {error.get('message')}")
    result = st.session_state.last_learning_result
    if not isinstance(result, dict) or result.get("action") != "evaluate":
        return
    experiment_id = result.get("experiment_id")
    if isinstance(experiment_id, str):
        st.success(
            f"Saved evaluation `{experiment_id}` as {result.get('status')}. "
            "Load the report below; this page did not invent metrics."
        )


def _render_saved_evaluation_section(settings: svc.Settings, *, workspace_id: str | None) -> None:
    _render_teaching_evaluation_control(settings, workspace_id)
    summaries = svc.list_saved_evaluations(settings)
    if not summaries:
        st.info("No saved evaluation reports are present yet.")
        _render_evaluation_explainer()
        return
    by_id = {item.experiment_id: item for item in summaries}
    ids = [item.experiment_id for item in summaries]
    loaded = st.session_state.get("loaded_experiment_id")
    if loaded in ids and st.session_state.get("evaluation_select") not in ids:
        st.session_state.evaluation_select = loaded
    if st.session_state.get("evaluation_select") not in ids:
        st.session_state.evaluation_select = ids[0]
    selected_id = st.selectbox(
        "Saved evaluation",
        options=ids,
        format_func=lambda value: _evaluation_option_label(by_id[value]),
        key="evaluation_select",
    )
    load_clicked = st.button("Load report", key="load_evaluation_report")
    if load_clicked:
        st.session_state.loaded_experiment_id = selected_id
        st.session_state.last_learning_error = None
        st.rerun()
        return
    current = st.session_state.get("loaded_experiment_id")
    if current not in ids:
        theme.render_inline(
            theme.quiet("Select a saved experiment and Load report. Loading does not rerun it.")
        )
        _render_evaluation_explainer()
        return
    _render_loaded_evaluation(settings, current)


def _render_evaluation_explainer() -> None:
    """Say what a paired evaluation measures when there is no report to show."""
    with st.container(key="pc-panel-evaluation-explainer"):
        theme.card_title("What a paired evaluation measures")
        theme.bullets(
            [
                "Every case in the split is investigated twice from one frozen dataset "
                "snapshot: once with memory off (baseline) and once with the ACTIVE lessons "
                "attached (memory). Both arms read identical source data, so retrieval is "
                "the only difference between them.",
                "Positive transfer is a case the memory arm got right and the baseline arm "
                "got wrong. Negative transfer is the reverse, and it matters more: it is "
                "memory making the system worse.",
                "Counts are reported as value out of the denominator stored in the report. "
                "Cases that timed out, were skipped on budget, or never ran are listed as "
                "incomplete rather than counted as failures.",
            ]
        )
        theme.render_inline(
            theme.quiet(
                "Populate this page with the teaching evaluation above, or run a longer "
                "experiment from the CLI: precedent evaluate --workspace WS --split teaching."
            )
        )


def _render_teaching_evaluation_control(settings: svc.Settings, workspace_id: str | None) -> None:
    missing = svc.missing_live_variable_names(settings)
    in_progress = bool(st.session_state.mutation_in_progress)
    theme.body(
        "Deliberate small run: teaching --limit 2 is 2 cases and 4 live agent episodes. "
        f"Experiment deadline {svc.DEFAULT_EVALUATION_DEADLINE_SECONDS}s. Per-case budget "
        f"{settings.max_model_calls} model calls and {settings.max_case_seconds}s. The full "
        "held-out comparison is CLI-only."
    )
    if missing:
        help_text = "Chargeable evaluation disabled because " + ", ".join(missing) + "."
    elif not workspace_id:
        help_text = "Select a workspace before starting a teaching evaluation."
    elif in_progress:
        help_text = "Another mutation is already in progress."
    else:
        help_text = (
            "Starts four live isolated episodes and writes report.json. "
            "Not a page-load side effect."
        )
    disabled = bool(missing) or not workspace_id or in_progress
    clicked = st.button(
        "Run teaching evaluation (2 cases / 4 episodes)",
        key="run_teaching_evaluation",
        disabled=disabled,
        help=help_text,
        icon=":material/science:",
    )
    if clicked and not disabled and workspace_id:
        _handle_teaching_evaluation(settings, workspace_id)


def _evaluation_option_label(item: svc.SavedEvaluationSummary) -> str:
    mode = _evaluation_mode_label(item.mode) if item.mode is not None else "unknown"
    state = item.state.value if item.state is not None else "unknown"
    when = item.started_at or "unknown time"
    return f"{item.experiment_id} · {mode} · {state} · {when}"


def _evaluation_mode_label(mode: ExecutionMode | None) -> str:
    if mode is None:
        return "unknown"
    return theme.execution_mode_label(mode)


def _render_loaded_evaluation(settings: svc.Settings, experiment_id: str) -> None:
    try:
        report = svc.load_evaluation(settings, experiment_id)
        raw = svc.load_evaluation_raw(settings, experiment_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    split = report.split.value if report.split is not None else "(unknown)"
    theme.card_title(report.experiment_id)
    theme.render_inline(
        theme.provenance_badge(report.mode),
        theme.quiet(f"{report.state.value} · split {split}"),
        theme.quiet(
            f"{report.started_at or '(unknown)'} to {report.finished_at or '(unfinished)'}"
        ),
    )
    theme.count_strip(
        [
            ("Scheduled", report.scheduled_count),
            ("Completed", report.completed_count),
            ("Failed", report.failed_count),
            ("Timed out", report.timed_out_count),
            ("Budget skipped", report.budget_skipped_count),
            ("Not run", report.not_run_count),
        ]
    )
    if report.not_run_count or report.budget_skipped_count or report.timed_out_count:
        st.warning(
            "Incomplete cases are listed, not filled with zeros. Headline metrics use "
            "the denominators stored in this report."
        )
    st.caption(
        f"Source `{report.frozen_manifest.source_workspace_id or '(none)'}` · "
        f"dataset `{report.frozen_manifest.dataset_hash[:12]}` · "
        f"memory `{report.frozen_manifest.memory_snapshot_hash[:12]}`"
    )
    metrics = report.metrics
    if metrics.baseline is not None and metrics.memory is not None:
        chart_column, table_column = st.columns([1, 1])
        with chart_column:
            _render_arm_chart(metrics.baseline, metrics.memory)
        with table_column:
            theme.card_title("Stored counts", small=True)
            st.dataframe(
                _arm_metric_rows(metrics.baseline, metrics.memory),
                hide_index=True,
                width="stretch",
            )
    else:
        st.info(
            "This report does not store both a baseline arm and a memory arm, so there is "
            "no paired comparison to show. The raw report below is the whole record."
        )
    _render_transfer(metrics.positive_transfer_case_ids, metrics.negative_transfer_case_ids)
    theme.body(
        "No statistical-significance or money-saved claim. Review count is not measured "
        "human minutes. Invoice face value is not money saved."
    )
    rows = [
        {
            "Case": item.case_id,
            "Baseline": _score_cell(
                item.baseline_correct, item.baseline_outcome, item.baseline_disposition
            ),
            "Memory": _score_cell(
                item.memory_correct, item.memory_outcome, item.memory_disposition
            ),
        }
        for item in report.per_case_paired_scores
    ]
    if rows:
        theme.card_title("Per-case paired scores", small=True)
        st.dataframe(rows, hide_index=True, width="stretch")
    with st.expander("Raw report.json", expanded=False):
        st.json(raw)
        artifact = svc.evaluation_artifact_dir(settings) / experiment_id / "report.json"
        st.caption(f"File `{artifact}`.")


def _render_arm_chart(baseline: EvaluationArmMetrics, memory: EvaluationArmMetrics) -> None:
    """Chart only what the two arms measured over the same denominator.

    A metric whose arms were scored over different denominators, or over none at
    all, is named as not charted instead of being drawn at a misleading height.
    """
    rows: list[dict[str, Any]] = []
    not_charted: list[str] = []
    for label, field in _CHART_METRICS:
        base: MetricCount = getattr(baseline, field)
        mem: MetricCount = getattr(memory, field)
        if base.denominator == 0 or mem.denominator == 0:
            not_charted.append(f"{label} (no scored cases)")
            continue
        if base.denominator != mem.denominator:
            not_charted.append(
                f"{label} (baseline out of {base.denominator}, memory out of {mem.denominator})"
            )
            continue
        rows.append(
            {
                "Metric": f"{label} of {base.denominator}",
                "Baseline": base.value,
                "Memory": mem.value,
            }
        )
    theme.card_title("Baseline versus memory", small=True)
    if rows:
        st.bar_chart(
            rows,
            x="Metric",
            y=["Baseline", "Memory"],
            color=[_BASELINE_COLOR, _MEMORY_COLOR],
            horizontal=True,
            stack=False,
            height=max(200, 52 * len(rows) + 60),
        )
        theme.body(
            "Bars are case counts, not rates. Each label carries the denominator stored in "
            "report.json. Baseline ran with memory off; memory ran with ACTIVE lessons "
            "attached."
        )
    else:
        st.info(
            "No metric in this report has the same non-zero denominator in both arms, so "
            "there is nothing to compare as a chart. The stored counts are beside this."
        )
    if not_charted:
        theme.body("Not charted: " + "; ".join(not_charted) + ".")


def _render_transfer(positive: list[str], negative: list[str]) -> None:
    theme.card_title("Transfer", small=True)
    _render_transfer_row(
        "Positive transfer",
        positive,
        color=theme.GREEN,
        soft=theme.GREEN_SOFT,
        note="memory correct where baseline was wrong",
        empty="No case where memory fixed a baseline mistake in this experiment.",
    )
    _render_transfer_row(
        "Negative transfer",
        negative,
        color=theme.RED,
        soft=theme.RED_SOFT,
        note="baseline correct where memory was wrong",
        empty="No case where memory broke a baseline success in this experiment.",
    )


def _render_transfer_row(
    title: str,
    case_ids: list[str],
    *,
    color: str,
    soft: str,
    note: str,
    empty: str,
) -> None:
    label = (
        f'<span class="pc-tag-label" style="color:{color}">{theme.escape_text(title)}</span>'
        f"{theme.quiet(note)}"
    )
    if not case_ids:
        theme.render_inline(label, theme.quiet(empty))
        return
    chips = "".join(
        f'<span class="pc-tag" style="background:{soft};color:{color};'
        f'border:1px solid {color}">{theme.escape_text(case_id)}</span>'
        for case_id in case_ids
    )
    theme.render_inline(label, theme.quiet(f"{len(case_ids)} case(s)"), chips)


def _arm_metric_rows(
    baseline: EvaluationArmMetrics, memory: EvaluationArmMetrics
) -> list[dict[str, str]]:
    return [
        _metric_row(
            "Correct autonomous resolutions",
            baseline.correct_autonomous_resolutions,
            memory.correct_autonomous_resolutions,
        ),
        _metric_row(
            "Correct autonomous of resolvable",
            baseline.correct_autonomous_resolutions_resolvable,
            memory.correct_autonomous_resolutions_resolvable,
        ),
        _metric_row(
            "Incorrect automatic resolutions",
            baseline.incorrect_automatic_resolutions,
            memory.incorrect_automatic_resolutions,
        ),
        _metric_row("Correct reviews", baseline.correct_reviews, memory.correct_reviews),
        _metric_row(
            "Unnecessary reviews",
            baseline.unnecessary_reviews,
            memory.unnecessary_reviews,
        ),
        _metric_row(
            "Technical errors",
            baseline.technical_errors,
            memory.technical_errors,
        ),
        {
            "Metric": "Rejected financial proposals",
            "Baseline": str(baseline.rejected_financial_proposals),
            "Memory": str(memory.rejected_financial_proposals),
        },
        {
            "Metric": "Scope violations",
            "Baseline": str(baseline.scope_violations),
            "Memory": str(memory.scope_violations),
        },
        {
            "Metric": "Model attempts",
            "Baseline": str(baseline.work_performed.model_attempts),
            "Memory": str(memory.work_performed.model_attempts),
        },
        {
            "Metric": "Elapsed ms",
            "Baseline": str(baseline.work_performed.elapsed_ms),
            "Memory": str(memory.work_performed.elapsed_ms),
        },
    ]


def _metric_row(label: str, baseline: MetricCount, memory: MetricCount) -> dict[str, str]:
    return {
        "Metric": label,
        "Baseline": _format_metric_count(baseline),
        "Memory": _format_metric_count(memory),
    }


def _format_metric_count(count: MetricCount) -> str:
    if count.denominator == 0:
        return "N/A"
    return f"{count.value}/{count.denominator}"


def _score_cell(correct: bool | None, outcome: Any, disposition: Any) -> str:
    bits = []
    if correct is True:
        bits.append("correct")
    elif correct is False:
        bits.append("incorrect")
    else:
        bits.append("n/a")
    if outcome is not None:
        bits.append(getattr(outcome, "value", str(outcome)))
    if disposition is not None:
        bits.append(getattr(disposition, "value", str(disposition)))
    return " · ".join(bits)


def _render_recorded_runs(settings: svc.Settings) -> None:
    records = svc.list_recorded_runs(settings)
    theme.section(
        "Recorded runs",
        subtitle=(
            "This viewer reads a saved manifest only. It never invokes apply_proposal "
            "and cannot satisfy a live evaluation claim."
        ),
    )
    if not records:
        st.session_state.viewing_recorded_run_id = None
        st.info(
            "No captured recorded runs. Offline scripted simulations are labeled "
            "TEST SIMULATION and cannot satisfy live acceptance. Export a persisted "
            "run with `precedent replay export --run RUN_ID` after a real investigation."
        )
        return
    st.warning(RECORDED_RUN_LABEL)
    by_id = {item.run_id: item for item in records}
    ids = [item.run_id for item in records]
    selected_id = st.selectbox(
        "Recorded run",
        options=ids,
        format_func=lambda value: _recorded_run_option_label(by_id[value]),
        key="recorded_run_select",
    )
    st.session_state.viewing_recorded_run_id = selected_id
    item = by_id[selected_id]
    with st.container(key="pc-panel-recorded-run"):
        theme.render_inline(
            theme.provenance_badge("RECORDED RUN"),
            theme.provenance_badge(item.mode),
            theme.ident(item.run_id, label="run", width_ch=22),
            theme.quiet(
                f"captured {item.original_started_at} · model {item.model or 'UNSET'} · "
                f"provider {item.provider or 'UNSET'}"
            ),
        )
        st.caption(
            f"source `{item.source_hash[:12]}` · policy `{item.policy_hash[:12]}` · "
            f"prompt `{item.prompt_hash[:12]}` · tools `{item.tool_hash[:12]}` · "
            f"memory `{item.memory_hash[:12]}` · exported {item.capture_timestamp}"
        )
        _render_recorded_ledger(item)
    with st.expander("Recorded public trace", expanded=False):
        theme.trace_timeline(
            item.public_trace,
            empty_message="This captured run has no stored events.",
        )
    theme.body("Viewing a recorded run never invokes apply_proposal.")


def _render_recorded_ledger(item: Any) -> None:
    """Show the opening and closing ledger state the manifest captured."""
    opening = item.input_snapshot
    closing = item.final_financial_snapshot
    theme.render_inline(
        theme.quiet("Opening"),
        theme.ident(opening.summary.case_id, width_ch=20),
        theme.quiet(opening.summary.case_state.value),
        theme.money_html(opening.payment.amount_cents, tone="strong"),
        theme.quiet(opening.payment.currency),
        theme.quiet(
            f"applied={str(opening.payment.applied).lower()} · revision {opening.ledger_revision}"
        ),
    )
    theme.render_inline(
        theme.quiet("Final"),
        theme.ident(closing.summary.case_id, width_ch=20),
        theme.quiet(closing.summary.case_state.value),
        theme.quiet(
            f"applied={str(closing.payment.applied).lower()} · revision {closing.ledger_revision}"
        ),
    )
    for invoice in closing.invoices:
        opened = next(
            (row for row in opening.invoices if row.invoice_id == invoice.invoice_id),
            None,
        )
        before = (
            theme.quiet("n/a") if opened is None else theme.money_html(opened.outstanding_cents)
        )
        theme.render_inline(
            theme.quiet("Invoice"),
            theme.ident(invoice.invoice_id, width_ch=18),
            theme.quiet("outstanding"),
            before,
            theme.quiet("to"),
            theme.money_html(invoice.outstanding_cents, tone="strong"),
        )


def _recorded_run_option_label(item: object) -> str:
    run_id = getattr(item, "run_id", "unknown")
    mode = getattr(item, "mode", None)
    started = getattr(item, "original_started_at", "")
    mode_label = theme.execution_mode_label(mode) if isinstance(mode, ExecutionMode) else "UNKNOWN"
    return f"{run_id} · {mode_label} · {started}"


def _handle_teaching_evaluation(settings: svc.Settings, workspace_id: str) -> None:
    reason = svc.live_readiness_code(settings)
    if reason is not None:
        missing = svc.missing_live_variable_names(settings)
        st.session_state.last_learning_error = {
            "code": reason,
            "message": "Live evaluation is not configured. Missing: " + ", ".join(missing) + ".",
        }
        return
    st.session_state.mutation_in_progress = True
    try:
        request = EvaluationRequest(
            source_workspace_id=workspace_id,
            split=DatasetSplit.TEACHING,
            case_limit=2,
            mode=ExecutionMode.LIVE,
            deadline_seconds=svc.DEFAULT_EVALUATION_DEADLINE_SECONDS,
            output_dir=svc.evaluation_artifact_dir(settings),
        )
        with st.spinner("Running four isolated teaching evaluation episodes..."):
            report = svc.run_evaluation(settings, request)
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "evaluate",
            "experiment_id": report.experiment_id,
            "status": report.state.value,
        }
        st.session_state.loaded_experiment_id = report.experiment_id
    except (svc.EvaluationError, svc.PersistenceError, ValidationError) as exc:
        _store_learning_error(exc)
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _store_learning_error(exc: Exception) -> None:
    code = getattr(exc, "code", "INTERNAL_ERROR")
    if hasattr(code, "value"):
        code = code.value
    message = getattr(exc, "message", str(exc))
    st.session_state.last_learning_error = {"code": code, "message": message}
