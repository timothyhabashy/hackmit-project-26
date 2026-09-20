"""Lessons page: owned lessons, their lifecycle, and bound candidate-test reports.

Evaluation reports and recorded runs live on the Results page (``ui_results``).
This module only reads stored records. Nothing here starts a candidate test, an
evaluation, or a model call as a side effect of rendering.

All presentation comes from ``precedent.ui_theme``, including the lifecycle
stepper, which the empty state and the lesson panel both draw.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import streamlit as st

from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import (
    CandidateEpisodeRecord,
    ExecutionMode,
    LessonState,
    PrecedentRecord,
)

# One primary action per status. Reject and retire are never primary: a
# destructive move should cost a deliberate second click.
_PRIMARY_ACTION: dict[LessonState, str] = {
    LessonState.DRAFT: "test",
    LessonState.TESTING: "test",
    LessonState.PASSED: "activate",
    LessonState.FAILED: "redraft",
    LessonState.ACTIVE: "redraft",
    LessonState.REJECTED: "redraft",
    LessonState.RETIRED: "redraft",
}

ACTIVATION_RULE = (
    "Activation requires a compatible LIVE passing candidate-test report bound to this "
    "exact version. A TEST SIMULATION report can never authorize it."
)


def render_learning_page(settings: svc.Settings) -> None:
    """Render the lessons this workspace owns and where each one sits in review."""
    workspace_id = st.session_state.get("workspace_id_choice") or st.session_state.get(
        "remembered_workspace_id"
    )
    theme.section(
        "Lessons",
        subtitle=(
            "A lesson is one reusable investigation hint compiled from one verified human "
            "correction. This page reads stored records only. It does not start a candidate "
            "test, an evaluation, or a model call on load or refresh."
        ),
    )
    _render_lesson_status()
    if not isinstance(workspace_id, str) or not workspace_id:
        st.info("Select a workspace in the sidebar to see the lessons it owns.")
        _render_learning_loop()
        return
    theme.render_inline(
        theme.quiet("Owned by"),
        theme.ident(workspace_id, label="workspace", width_ch=20),
    )
    try:
        lessons = svc.list_lessons(settings, workspace_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        lessons = []
    if not lessons:
        st.info(
            "No owned lessons in this workspace. Propose a draft from Case Detail after a "
            "verified correction. A draft stays inactive until a compatible live test "
            "and explicit activation."
        )
        _render_learning_loop()
        return
    selected = _select_lesson(lessons)
    if selected is not None:
        _render_lesson_panel(settings, workspace_id, selected)


def _render_lesson_status() -> None:
    """Report the outcome of the last lifecycle action, never a page-load claim."""
    error = st.session_state.last_learning_error
    if isinstance(error, dict):
        st.error(f"{error.get('code')}: {error.get('message')}")
    result = st.session_state.last_learning_result
    if not isinstance(result, dict):
        return
    action = result.get("action")
    precedent_id = result.get("precedent_id")
    status = result.get("status")
    reason = result.get("reason")
    if not isinstance(precedent_id, str):
        return
    if action == "test":
        st.success(
            f"Candidate suite finished for `{precedent_id}` as {status}. "
            "This did not activate the lesson."
        )
    elif action == "activate":
        st.success(f"Activated `{precedent_id}` as {status}.")
    elif action == "retire":
        st.info(f"Retired `{precedent_id}`. Past traces remain.")
    elif action == "reject":
        st.info(f"Rejected `{precedent_id}`.")
    elif action == "redraft":
        st.success(f"Created new draft `{precedent_id}` v{result.get('version')}.")
    else:
        return
    if isinstance(reason, str) and reason:
        theme.render_inline(theme.quiet(reason))


def _render_learning_loop() -> None:
    """Explain the loop when there is nothing to show yet.

    Zero lessons is the first thing most viewers see, so the empty state has to
    carry the idea rather than apologize for missing rows.
    """
    with st.container(key="pc-panel-learning-loop"):
        theme.card_title("How this workspace learns")
        theme.body(
            "A lesson records where a person looked to resolve one payment, so the next "
            "similar payment is investigated the same way. It never records what to pay."
        )
        theme.bullets(
            [
                "Investigate a case, then correct the result on Case Detail and cite the "
                "documents that justify it.",
                "Propose a reusable lesson from that correction. A compiler turns it into a "
                "scoped lookup procedure: which reference field on the remittance leads to "
                "which field on the bank notice.",
                "Run the five-case live test. Ten isolated episodes run V01 to V05 twice, "
                "once without the lesson and once with it, in throwaway workspaces.",
                "Activate the version by hand if a compatible LIVE report passed. Only then "
                "can retrieval see it.",
            ],
            ordered=True,
        )
    theme.lifecycle_stepper(None)
    with st.container(key="pc-quiet-learning-limits"):
        theme.card_title("What a lesson can never do", small=True)
        theme.bullets(
            [
                "Add a write-off, change the fee cap, or raise any allocation limit. "
                "Financial authority stays in fixed company policy and the validators.",
                "Replace a validator, widen its own scope, or apply outside the customer, "
                "bank account, currency, and channel it was compiled for.",
                "Turn a short payment into a documented fee. The dev suite includes a case "
                "designed to punish exactly that, and a lesson that does it fails the suite.",
            ],
            fine=True,
        )


def _select_lesson(lessons: list[PrecedentRecord]) -> PrecedentRecord | None:
    by_id = {item.precedent_id: item for item in lessons}
    remembered = st.session_state.get("remembered_precedent_id")
    current = st.session_state.get("lesson_select")
    ids = [item.precedent_id for item in lessons]
    if current not in ids and remembered in ids:
        st.session_state.lesson_select = remembered
    elif current not in ids:
        st.session_state.lesson_select = ids[0]
    selected_id = st.selectbox(
        "Lesson version",
        options=ids,
        format_func=lambda value: (
            f"{by_id[value].precedent_id} · v{by_id[value].version} · {by_id[value].status.value}"
        ),
        key="lesson_select",
    )
    st.session_state.remembered_precedent_id = selected_id
    return by_id.get(selected_id)


def _render_lesson_panel(
    settings: svc.Settings, workspace_id: str, lesson: PrecedentRecord
) -> None:
    blockers = svc.lesson_activation_blockers(settings, lesson.precedent_id)
    with st.container(key=f"pc-panel-lesson-{lesson.precedent_id}"):
        _render_lesson_header(lesson)
        theme.lifecycle_stepper(lesson.status)
        _render_activation_state(lesson, blockers)
        _render_lesson_scope(lesson)
        _render_origin_correction(settings, lesson)
        _render_lesson_controls(settings, workspace_id, lesson, blockers=blockers)
    _render_candidate_tests(settings, lesson)


def _render_lesson_header(lesson: PrecedentRecord) -> None:
    hint = lesson.hint
    theme.card_title(hint.title)
    theme.render_inline(
        theme.ident(lesson.precedent_id, label="lesson", width_ch=24),
        theme.quiet(f"version {lesson.version}"),
        '<span class="pc-quiet">status <strong style="color:'
        f'{theme.INK}">{theme.escape_text(lesson.status.value)}</strong></span>',
    )
    theme.body(
        "This is an investigation hint. Immutable company policy still authorizes the "
        "financial treatment: the lesson cannot add a write-off, change the fee cap, or "
        "replace a validator."
    )


def _render_activation_state(lesson: PrecedentRecord, blockers: list[str]) -> None:
    if lesson.status is LessonState.ACTIVE:
        st.success(
            "ACTIVE. Compatible cases in scope may retrieve this version. Retiring it removes "
            "it from future retrieval; past traces keep citing it."
        )
        return
    theme.render_inline(
        theme.quiet(
            "This version is not ACTIVE, so retrieval ignores it. Retrieval uses only ACTIVE "
            "compatible membership."
        )
    )
    if not blockers:
        st.success(
            "Activation is unblocked: a compatible LIVE passing candidate-test report is bound "
            "to this exact version."
        )
        return
    theme.note_block("Activation is blocked", blockers, footer=ACTIVATION_RULE)


def _render_lesson_scope(lesson: PrecedentRecord) -> None:
    hint = lesson.hint
    theme.card_title("Scope and lookup procedure", small=True)
    theme.render_inline(
        theme.ident(hint.scope.customer_id, label="customer", width_ch=18),
        theme.ident(hint.scope.bank_account_id, label="bank account", width_ch=18),
        theme.chip("currency", hint.scope.currency),
        theme.chip("channel", hint.scope.channel.value),
    )
    theme.body(hint.summary)
    terms = ", ".join(hint.lookup.search_terms or ["(none)"])
    theme.render_inline(
        theme.quiet("Remittance field"),
        f'<span class="pc-ident">'
        f"{theme.escape_text(hint.lookup.remittance_reference_field.value)}</span>",
        theme.quiet("leads to bank notice field"),
        f'<span class="pc-ident">'
        f"{theme.escape_text(hint.lookup.bank_notice_reference_field)}</span>",
        theme.quiet(f"search terms: {terms}"),
    )
    compiler = lesson.compiler_metadata if isinstance(lesson.compiler_metadata, dict) else {}
    st.caption(
        f"Payload `{lesson.payload_hash[:12]}` · policy `{lesson.policy_hash or 'unset'}` · "
        f"fingerprint `{(lesson.behavior_fingerprint or 'unset')[:12]}` · compiler mode "
        f"{compiler.get('mode') or 'unknown'} · prompt "
        f"`{compiler.get('prompt_version') or 'unset'}` · report "
        f"`{lesson.test_report_id or 'none'}`."
    )


def _render_origin_correction(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    if not lesson.correction_id:
        return
    correction = svc.get_correction_record(settings, lesson.correction_id)
    theme.card_title("Origin correction", small=True)
    if correction is None:
        theme.render_inline(theme.quiet(f"Original correction {lesson.correction_id} is missing."))
        return
    with st.container(key=f"pc-quiet-correction-{lesson.precedent_id}"):
        theme.body(correction.text)
        theme.render_inline(
            theme.ident(correction.correction_id, label="correction", width_ch=20),
            theme.ident(correction.case_id, label="case", width_ch=20),
            theme.chip("actor", correction.actor),
        )
        cited = "".join(
            theme.ident(item, width_ch=18) for item in correction.cited_document_ids
        ) or theme.quiet("no cited documents")
        theme.render_inline(theme.quiet("Cited evidence"), cited)


@dataclass(frozen=True)
class _LessonAction:
    name: str
    key: str
    label: str
    icon: str
    disabled: bool
    help_text: str
    run: Callable[[svc.Settings, PrecedentRecord], None]


def _render_lesson_controls(
    settings: svc.Settings,
    workspace_id: str,
    lesson: PrecedentRecord,
    *,
    blockers: list[str],
) -> None:
    """One primary action for the current status; the rest behind a menu."""
    missing = svc.missing_live_variable_names(settings)
    in_progress = bool(st.session_state.mutation_in_progress)
    actions = _lifecycle_actions(
        lesson, blockers=blockers, missing_live=missing, in_progress=in_progress
    )
    primary_name = _PRIMARY_ACTION.get(lesson.status, "test")
    primary = next(item for item in actions if item.name == primary_name)
    others = [item for item in actions if item.name != primary_name]
    theme.card_title("Lifecycle", small=True)
    theme.body(
        "A five-case live test is 10 isolated agent episodes: V01 to V05, each run once as "
        f"baseline and once as candidate. Per-case budget {settings.max_model_calls} model "
        f"calls and {settings.max_case_seconds} seconds. Testing does not mutate "
        f"{workspace_id}."
    )
    clicked: _LessonAction | None = None
    if (
        st.button(
            primary.label,
            key=primary.key,
            disabled=primary.disabled,
            help=primary.help_text,
            icon=primary.icon,
            type="primary",
            width="stretch",
        )
        and not primary.disabled
    ):
        clicked = primary
    with st.popover("Other lifecycle actions", icon=":material/more_horiz:", width="stretch"):
        theme.render_inline(
            theme.quiet(
                "Every action here is recorded with an actor and a reason. None of them move "
                "money or change company policy."
            )
        )
        for action in others:
            if (
                st.button(
                    action.label,
                    key=action.key,
                    disabled=action.disabled,
                    help=action.help_text,
                    icon=action.icon,
                    width="stretch",
                )
                and not action.disabled
            ):
                clicked = action
    if clicked is not None:
        clicked.run(settings, lesson)


def _lifecycle_actions(
    lesson: PrecedentRecord,
    *,
    blockers: list[str],
    missing_live: tuple[str, ...],
    in_progress: bool,
) -> list[_LessonAction]:
    test_disabled, test_help = _live_test_button_state(
        lesson, missing_live=missing_live, in_progress=in_progress
    )
    activate_disabled, activate_help = _activate_button_state(blockers, in_progress=in_progress)
    reject_disabled, reject_help = _reject_button_state(lesson, in_progress=in_progress)
    retire_disabled, retire_help = _retire_button_state(lesson, in_progress=in_progress)
    redraft_disabled, redraft_help = (
        (True, "Another mutation is already in progress.")
        if in_progress
        else (False, "Create a new DRAFT with the same hint. It inherits no report or approval.")
    )
    return [
        _LessonAction(
            name="test",
            key=f"run_five_case_live_test_{lesson.precedent_id}",
            label="Run five-case live test",
            icon=":material/science:",
            disabled=test_disabled,
            help_text=test_help,
            run=_handle_lesson_test,
        ),
        _LessonAction(
            name="activate",
            key=f"activate_tested_lesson_{lesson.precedent_id}",
            label="Activate tested lesson",
            icon=":material/check_circle:",
            disabled=activate_disabled,
            help_text=activate_help,
            run=_handle_lesson_activate,
        ),
        _LessonAction(
            name="redraft",
            key=f"create_new_version_{lesson.precedent_id}",
            label="Create new version",
            icon=":material/note_add:",
            disabled=redraft_disabled,
            help_text=redraft_help,
            run=_handle_lesson_redraft,
        ),
        _LessonAction(
            name="reject",
            key=f"reject_draft_{lesson.precedent_id}",
            label="Reject draft",
            icon=":material/block:",
            disabled=reject_disabled,
            help_text=reject_help,
            run=_handle_lesson_reject,
        ),
        _LessonAction(
            name="retire",
            key=f"retire_active_lesson_{lesson.precedent_id}",
            label="Retire active lesson",
            icon=":material/archive:",
            disabled=retire_disabled,
            help_text=retire_help,
            run=_handle_lesson_retire,
        ),
    ]


def _live_test_button_state(
    lesson: PrecedentRecord, *, missing_live: tuple[str, ...], in_progress: bool
) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if missing_live:
        return True, "Chargeable live test disabled because " + ", ".join(missing_live) + "."
    if lesson.status in {LessonState.ACTIVE, LessonState.REJECTED, LessonState.RETIRED}:
        return True, f"A {lesson.status.value} lesson cannot be tested; create a new version."
    return False, "Ten live isolated episodes. Does not activate this draft."


def _activate_button_state(blockers: list[str], *, in_progress: bool) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if blockers:
        return True, blockers[0]
    return False, "Activate after a compatible passing live report. Actor is demo_controller."


def _reject_button_state(lesson: PrecedentRecord, *, in_progress: bool) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if lesson.status is LessonState.ACTIVE:
        return True, "Retire an ACTIVE lesson; do not reject it."
    if lesson.status in {LessonState.REJECTED, LessonState.RETIRED}:
        return True, f"A {lesson.status.value} lesson cannot be rejected."
    return False, "Reject this version without activating it."


def _retire_button_state(lesson: PrecedentRecord, *, in_progress: bool) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if lesson.status is not LessonState.ACTIVE:
        return True, "Only ACTIVE lessons can be retired."
    return False, "Exclude this version from future retrieval. Past traces remain."


def _render_candidate_tests(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    theme.section(
        "Candidate test report",
        subtitle=(
            "The five-case suite runs each development case twice, once without this lesson "
            "and once with it, in throwaway workspaces. It is the only evidence that can "
            "authorize activation."
        ),
    )
    if lesson.test_report_id is None:
        st.info(
            "No candidate-test report is bound to this version yet. Until a compatible LIVE "
            "report passes, this version cannot activate and cannot be retrieved."
        )
        theme.body(ACTIVATION_RULE)
        return
    view = svc.get_lesson_test(settings, lesson.test_report_id)
    if view is None:
        st.warning(f"Bound report `{lesson.test_report_id}` is missing.")
        return
    report = view.report
    theme.render_inline(
        theme.provenance_badge(report.mode),
        theme.ident(report.report_id, label="report", width_ch=22),
        theme.quiet(f"{report.state.value} · candidate version {report.candidate_version}"),
    )
    if report.mode is ExecutionMode.TEST:
        st.warning(
            "TEST SIMULATION / scripted checks cannot authorize a live-tested lesson. "
            "Activation requires a compatible LIVE passing report."
        )
    theme.body(
        f"Passed these limited checks: {str(view.passed_limited_checks).lower()}. That is not "
        f"a claim of general safety or useful improvement. {_improvement_sentence(view)}"
    )
    st.caption(
        "Deterministic validator/lifecycle checks are host tests, not live agent evaluations. "
        f"Validator {'passed' if report.check_results.validator_suite_passed else 'failed'}; "
        f"lifecycle {'passed' if report.check_results.lifecycle_suite_passed else 'failed'}."
    )
    for reason in view.failed_reasons:
        st.error(reason)
    _render_candidate_cases(settings, view, report)


def _improvement_sentence(view: Any) -> str:
    if view.demonstrated_useful_improvement is True:
        return "Demonstrated useful improvement on this development suite: yes."
    if view.demonstrated_useful_improvement is False:
        return "Demonstrated useful improvement on this development suite: no."
    return "Demonstrated useful improvement: not assessed."


def _render_candidate_cases(settings: svc.Settings, view: Any, report: Any) -> None:
    notes = svc.reviewer_dev_case_notes(settings)
    by_case: dict[str, list[CandidateEpisodeRecord]] = {}
    for episode in view.episodes:
        by_case.setdefault(episode.case_id, []).append(episode)
    score_by_case = {item.case_id: item for item in report.paired_scores}
    for case_id, episodes in by_case.items():
        note = notes.get(case_id)
        authoring = note.authoring_id if note is not None else "dev"
        with st.expander(f"{authoring} `{case_id}`", expanded=authoring == "V02"):
            if note is not None:
                codes = ", ".join(item.value for item in note.allowed_review_codes) or "(none)"
                st.caption(
                    f"Expected disposition for the reviewer: {note.expected_outcome.value}"
                    f"{' / ' + codes if codes != '(none)' else ''}. "
                    f"Expected new applications: {note.expected_new_application_count}. "
                    "These oracle values are not returned to the runtime agent."
                )
            if authoring == "V02":
                theme.body(
                    "Misleading short-payment example: Harbor invoice $1,600.00 versus "
                    "wire $1,565.00, open dispute $35.00, and no fee notice. A fee "
                    "resolution would be incorrect here."
                )
            pair = score_by_case.get(case_id)
            if pair is not None:
                baseline_outcome = _outcome_text(pair.baseline_outcome)
                memory_outcome = _outcome_text(pair.memory_outcome)
                theme.render_inline(
                    theme.quiet(
                        f"Baseline correct={pair.baseline_correct} ({baseline_outcome})"
                        f" · candidate correct={pair.memory_correct} ({memory_outcome})"
                    )
                )
            for episode in sorted(episodes, key=lambda item: item.order_index):
                _render_episode_row(settings, episode)
            candidate = next((item for item in episodes if item.arm == "candidate"), None)
            if candidate is not None:
                _render_episode_sources(settings, candidate)


def _outcome_text(outcome: Any) -> str:
    return "none" if outcome is None else str(getattr(outcome, "value", outcome))


def _render_episode_row(settings: svc.Settings, episode: CandidateEpisodeRecord) -> None:
    outcome = "(none)" if episode.outcome is None else episode.outcome.value
    review = "(none)" if episode.review_code is None else episode.review_code.value
    rejections = ", ".join(item.value for item in episode.validator_rejection_codes) or "(none)"
    retrieved = ", ".join(episode.retrieved_precedent_ids) or "(none)"
    st.markdown(
        f'<div class="pc-body" style="margin-top:0.35rem">'
        f"<strong>{theme.escape_text(episode.arm)}</strong> outcome "
        f"{theme.escape_text(outcome)} · correct={theme.escape_text(episode.correct)} · "
        f"review {theme.escape_text(review)} · new applications "
        f"{theme.escape_text(episode.new_application_count)} · workspace "
        f"{theme.ident(episode.workspace_id, width_ch=18)}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Validator rejections: {rejections}. Retrieved: {retrieved}. "
        f"Scope violation={str(episode.scope_violation).lower()} · "
        f"invalid proposal={str(episode.invalid_proposal).lower()}."
    )
    if episode.run_id:
        with st.expander(f"Trace `{episode.run_id}`", expanded=False):
            theme.trace_timeline(svc.list_trace_events(settings, episode.run_id))


def _render_episode_sources(settings: svc.Settings, episode: CandidateEpisodeRecord) -> None:
    """Show the sources the candidate arm actually read, not a summary of them."""
    try:
        detail = svc.get_case_detail(settings, episode.workspace_id, episode.case_id)
    except svc.PersistenceError as exc:
        theme.render_inline(theme.quiet(f"Episode sources unavailable: {exc}"))
        return
    payment = detail.snapshot.payment
    theme.render_inline(
        theme.quiet("Payment"),
        theme.ident(payment.payment_id, width_ch=18),
        theme.money_html(payment.amount_cents, tone="strong"),
        theme.quiet(payment.currency),
        theme.quiet(f"payer label {payment.payer_text}"),
    )
    for invoice in detail.snapshot.invoices:
        theme.render_inline(
            theme.quiet("Invoice"),
            theme.ident(invoice.invoice_id, width_ch=18),
            theme.quiet("original"),
            theme.money_html(invoice.original_cents),
            theme.quiet("current"),
            theme.money_html(invoice.outstanding_cents, tone="strong"),
        )
    for document in detail.documents:
        st.caption(f"`{document.document_id}` {document.kind.value} · {document.title}")


def _handle_lesson_test(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    reason = svc.live_readiness_code(settings)
    if reason is not None:
        missing = svc.missing_live_variable_names(settings)
        st.session_state.last_learning_error = {
            "code": reason,
            "message": "Live lesson test is not configured. Missing: " + ", ".join(missing) + ".",
        }
        return
    st.session_state.mutation_in_progress = True
    try:
        with st.spinner("Running ten isolated candidate-test episodes..."):
            result = svc.test_lesson(settings, lesson.precedent_id, mode=ExecutionMode.LIVE)
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "test",
            "precedent_id": result.precedent_id,
            "status": result.report.state.value,
            "reason": (
                "Passed these limited checks."
                if result.passed_limited_checks
                else "; ".join(result.failed_reasons) or "Suite finished."
            ),
        }
        st.session_state.remembered_precedent_id = result.precedent_id
    except (svc.LearningError, svc.PersistenceError, svc.ProviderError) as exc:
        _store_learning_error(exc)
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _handle_lesson_activate(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.activate_lesson(
            settings, lesson.precedent_id, actor=svc.DEFAULT_CONTROLLER_ACTOR
        )
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "activate",
            "precedent_id": result.precedent_id,
            "status": result.status.value,
            "reason": result.reason,
        }
        st.session_state.remembered_precedent_id = result.precedent_id
    except (svc.LearningError, svc.PersistenceError) as exc:
        _store_learning_error(exc)
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _handle_lesson_reject(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.reject_lesson(
            settings, lesson.precedent_id, actor=svc.DEFAULT_CONTROLLER_ACTOR
        )
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "reject",
            "precedent_id": result.precedent_id,
            "status": result.status.value,
            "reason": result.reason,
        }
    except (svc.LearningError, svc.PersistenceError) as exc:
        _store_learning_error(exc)
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _handle_lesson_retire(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.retire_lesson(
            settings, lesson.precedent_id, actor=svc.DEFAULT_CONTROLLER_ACTOR
        )
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "retire",
            "precedent_id": result.precedent_id,
            "status": result.status.value,
            "reason": result.reason,
        }
    except (svc.LearningError, svc.PersistenceError) as exc:
        _store_learning_error(exc)
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _handle_lesson_redraft(settings: svc.Settings, lesson: PrecedentRecord) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.redraft_lesson(settings, lesson.precedent_id)
        st.session_state.last_learning_error = None
        st.session_state.last_learning_result = {
            "action": "redraft",
            "precedent_id": result.precedent_id,
            "status": result.status.value,
            "version": result.version,
            "reason": result.reason,
        }
        st.session_state.remembered_precedent_id = result.precedent_id
        st.session_state.lesson_select = result.precedent_id
    except (svc.LearningError, svc.PersistenceError) as exc:
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
