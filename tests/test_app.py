from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from precedent.config import detect_repo_root, load_settings
from precedent.db import (
    get_precedent,
    insert_candidate_test,
    open_database,
    update_precedent_lifecycle,
)
from precedent.evaluation import load_dev_suite, load_split_suite
from precedent.fixtures import (
    CUST_HARBOR,
    DEFAULT_SEED,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    load_manifest,
)
from precedent.models import (
    ApplicationStatus,
    DatasetSplit,
    EvaluationRequest,
    ExecutionMode,
    LessonState,
    MemoryMode,
    ProviderTurn,
    ToolCall,
    Usage,
)
from precedent.providers import ScriptedProvider
from precedent.services import (
    apply_correction,
    correction_apply_key,
    count_case_runs,
    demo_init,
    export_recorded_run,
    get_case_detail,
    get_lesson,
    investigate,
    list_cases,
    propose_lesson,
    run_evaluation,
    save_correction,
)
from precedent.services import (
    test_lesson as run_lesson_test,
)
from test_evaluation import scripted_dev_provider, scripted_split_provider

APP_PATH = detect_repo_root() / "app.py"
QUEUE_PAGE = "app_pages/work_queue.py"
DETAIL_PAGE = "app_pages/case_detail.py"
LEARNING_PAGE = "app_pages/lessons.py"
RESULTS_PAGE = "app_pages/results.py"
_MANIFEST = load_manifest(detect_repo_root() / "data" / "manifests" / "fixtures-seed-42.json")
T01_CASE_ID = next(item.case_id for item in _MANIFEST.cases if item.authoring_id == "T01")
T03_CASE_ID = next(item.case_id for item in _MANIFEST.cases if item.authoring_id == "T03")


def _settings(tmp_path: Path):
    repo = detect_repo_root()
    return load_settings(
        environ={
            "PRECEDENT_DATA_DIR": str(repo / "data"),
            "PRECEDENT_DB_PATH": str(tmp_path / "var" / "precedent.sqlite3"),
            "PRECEDENT_ENABLE_LIVE": "false",
        },
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )


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


def test_queue_run_does_not_repeat_on_navigation(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    demo_init(settings, seed=DEFAULT_SEED)
    calls: list[tuple[str, str, ExecutionMode, MemoryMode]] = []

    def _live_ready(_settings) -> None:
        return None

    def _missing(_settings) -> tuple[str, ...]:
        return ()

    def _investigate(
        bound_settings,
        workspace_id: str,
        case_id: str,
        *,
        mode: ExecutionMode,
        memory_mode: MemoryMode,
        **kwargs: Any,
    ):
        calls.append((workspace_id, case_id, mode, memory_mode))
        return investigate(
            bound_settings,
            workspace_id,
            case_id,
            mode=ExecutionMode.TEST,
            memory_mode=memory_mode,
            provider=_review_script(),
        )

    def _click_row(app_test: AppTest, case_id: str) -> None:
        """Select a case the way the queue table does, by clicking its row.

        AppTest exposes st.dataframe as a plain element with no click support, so
        the row selection the browser would send is written to session state
        directly. That is the same input the queue reads back.
        """
        rows = list(app_test.dataframe[0].value["Case"])
        app_test.session_state["case_table"] = {"selection": {"rows": [rows.index(case_id)]}}
        app_test.run()

    monkeypatch.setattr("precedent.ui_services.load_settings", lambda: settings)
    monkeypatch.setattr("precedent.ui_services.live_readiness_code", _live_ready)
    monkeypatch.setattr("precedent.ui_services.missing_live_variable_names", _missing)
    monkeypatch.setattr("precedent.ui_services.investigate", _investigate)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
    assert not at.exception
    assert calls == []
    at.radio("workspace_id_choice").set_value("WS-TEACH-001")
    at.run()
    # The queue states the payment against the invoice balance before any run.
    queue = at.dataframe[0].value.set_index("Case")
    assert queue.loc[T03_CASE_ID, "Received"] == 9965.00
    assert queue.loc[T03_CASE_ID, "Invoice balance"] == 10000.00
    assert queue.loc[T03_CASE_ID, "Unexplained"] == 35.00
    _click_row(at, T01_CASE_ID)
    assert at.selectbox("case_select").value == T01_CASE_ID
    assert count_case_runs(settings, "WS-TEACH-001", T01_CASE_ID) == 0
    at.button("open_case").click()
    at.run()
    assert not at.exception
    assert at.session_state["active_page"] == "Case Detail"
    markdown = " ".join(item.value for item in at.markdown)
    assert T01_CASE_ID in markdown
    at.switch_page(QUEUE_PAGE)
    at.run()
    assert at.session_state["active_page"] == "Work Queue"
    assert at.selectbox("case_select").value == T01_CASE_ID
    at.button("run_selected_case").click()
    at.run()
    assert not at.exception
    assert calls == [("WS-TEACH-001", T01_CASE_ID, ExecutionMode.LIVE, MemoryMode.ON)]
    assert count_case_runs(settings, "WS-TEACH-001", T01_CASE_ID) == 1
    after = {item.case_id: item for item in list_cases(settings, "WS-TEACH-001")}
    assert after[T01_CASE_ID].latest_run_id is not None
    at.switch_page(DETAIL_PAGE)
    at.run()
    at.switch_page(QUEUE_PAGE)
    at.run()
    assert not at.exception
    assert len(calls) == 1
    assert count_case_runs(settings, "WS-TEACH-001", T01_CASE_ID) == 1
    rerendered = {item.case_id: item for item in list_cases(settings, "WS-TEACH-001")}
    assert rerendered[T01_CASE_ID].latest_run_id == after[T01_CASE_ID].latest_run_id


def test_failed_live_setup_keeps_the_queue(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    demo_init(settings, seed=DEFAULT_SEED)
    called = {"n": 0}

    def _investigate(*_args: Any, **_kwargs: Any):
        called["n"] += 1
        raise AssertionError("live investigate must not run when credentials are missing")

    monkeypatch.setattr("precedent.ui_services.load_settings", lambda: settings)
    monkeypatch.setattr("precedent.ui_services.investigate", _investigate)
    at = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
    assert not at.exception
    assert at.session_state["active_page"] == "Work Queue"
    warning_text = " ".join(item.value for item in at.warning)
    assert "PRECEDENT_ENABLE_LIVE" in warning_text
    assert "ANTHROPIC_API_KEY" in warning_text
    assert "PRECEDENT_MODEL" in warning_text
    # The keyboard-reachable selection path stays usable with no credentials.
    at.selectbox("case_select").select(T01_CASE_ID)
    at.run()
    assert list(at.dataframe[0].value["Case"])
    button = at.button("run_selected_case")
    assert button.disabled
    assert "PRECEDENT_ENABLE_LIVE" in (button.help or "")
    # The block is stated in the page itself, not only in a hover tooltip, and
    # nothing on screen implies a model call was made.
    page = " ".join(item.value for item in at.markdown)
    assert "LIVE UNAVAILABLE" in page
    assert ">LIVE<" not in page
    # Reconciliation is local work, so the queue still answers its question.
    assert at.dataframe[0].value.set_index("Case").loc[T03_CASE_ID, "Unexplained"] == 35.00
    assert called["n"] == 0
    assert count_case_runs(settings, "WS-TEACH-001", T01_CASE_ID) == 0


def test_case_detail_saves_and_applies_t03_correction_once(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    demo_init(settings, seed=DEFAULT_SEED)
    monkeypatch.setattr("precedent.ui_services.load_settings", lambda: settings)

    at = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    assert not at.exception
    at.radio("workspace_id_choice").set_value("WS-TEACH-001")
    at.run()
    at.selectbox("case_select").select(T03_CASE_ID)
    at.run()
    at.button("open_case").click()
    at.run()
    assert not at.exception
    assert at.session_state["active_page"] == "Case Detail"
    page = " ".join(item.value for item in at.markdown)
    captions = " ".join(item.value for item in at.caption)
    assert T03_CASE_ID in page
    assert "PAY-201" in captions
    invoice_bits = page + captions + " ".join(item.value for item in at.caption)
    frames = []
    for frame in at.dataframe:
        frames.append(str(getattr(frame, "value", frame)))
    assert T03_INVOICE_ID in invoice_bits or T03_INVOICE_ID in " ".join(frames)
    assert "9,965.00" in page
    # The click above already navigated, which the assertion on active_page proves.
    # AppTest tracks the page it will run separately from st.switch_page, so pin it
    # before driving Case Detail widgets across further reruns.
    at.switch_page(DETAIL_PAGE)
    at.run()
    at.selectbox(f"evidence_document_{T03_CASE_ID}").select(T03_REMIT_ID)
    at.run()
    at.button(f"open_evidence_{T03_CASE_ID}").click()
    at.run()
    assert not at.exception
    at.selectbox(f"evidence_document_{T03_CASE_ID}").select(T03_FEE_ID)
    at.run()
    at.button(f"open_evidence_{T03_CASE_ID}").click()
    at.run()
    assert not at.exception
    opened = " ".join(item.value for item in at.caption)
    assert T03_FEE_ID in opened
    at.button(f"use_example_correction_{T03_CASE_ID}").click()
    at.run()
    assert not at.exception
    at.button(f"save_correction_{T03_CASE_ID}").click()
    at.run()
    assert not at.exception
    errors = " ".join(item.value for item in at.error)
    assert "INVALID_INPUT" not in errors
    detail = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert detail.corrections
    correction = detail.corrections[-1]
    assert correction.lesson_eligibility is True
    assert T03_REMIT_ID in correction.cited_document_ids
    assert T03_FEE_ID in correction.cited_document_ids
    apply_key = f"apply_correction_{correction.correction_id}"
    at.button(apply_key).click()
    at.run()
    assert not at.exception
    applied = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert applied.application is not None
    application_id = applied.application.application_id
    assert applied.snapshot.payment.applied is True
    invoice = next(item for item in applied.snapshot.invoices if item.invoice_id == T03_INVOICE_ID)
    assert invoice.outstanding_cents == 0
    success = " ".join(item.value for item in at.success)
    assert application_id in success
    page_after = " ".join(item.value for item in at.markdown) + " ".join(
        item.value for item in at.caption
    )
    assert "9,965.00 received" in page_after or "9,965 received" in page_after
    apply_button = at.button(apply_key)
    assert apply_button.disabled
    propose = at.button(f"propose_lesson_{correction.correction_id}")
    assert propose.disabled
    propose_help = propose.help or ""
    captions_after = " ".join(item.value for item in at.caption)
    assert "PRECEDENT_ENABLE_LIVE" in propose_help or "PRECEDENT_ENABLE_LIVE" in captions_after
    at.switch_page(QUEUE_PAGE)
    at.run()
    at.switch_page(DETAIL_PAGE)
    at.run()
    assert not at.exception
    reopened = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert reopened.application is not None
    assert reopened.application.application_id == application_id
    assert reopened.snapshot.payment.applied is True
    still = next(item for item in reopened.snapshot.invoices if item.invoice_id == T03_INVOICE_ID)
    assert still.outstanding_cents == 0
    assert any(item.correction_id == correction.correction_id for item in reopened.corrections)
    assert at.button(apply_key).disabled
    at.run()
    replayed = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert replayed.application is not None
    assert replayed.application.application_id == application_id
    replay_invoice = next(
        item for item in replayed.snapshot.invoices if item.invoice_id == T03_INVOICE_ID
    )
    assert replay_invoice.outstanding_cents == 0
    replay_result = apply_correction(
        settings,
        correction.correction_id,
        idempotency_key=correction_apply_key(correction.correction_id),
        actor="demo_controller",
    )
    assert replay_result.status is ApplicationStatus.REPLAYED
    assert replay_result.application_id == application_id


V02_CASE_ID = next(item.case_id for item in _MANIFEST.cases if item.authoring_id == "V02")
TEACHING_TEXT = (
    "For Harbor's wire remittances, look up the settlement_ticket in the bank notice "
    "transfer_reference. This ticket links the current invoice to the current bank fee "
    "notice. Use the documented gross, net, and fee amounts and the existing company fee "
    "policy; never infer a fee merely from a short payment."
)


def _compiler_script() -> ScriptedProvider:
    return ScriptedProvider(
        [
            _turn(
                [
                    _call(
                        "c1",
                        "propose_precedent",
                        {
                            "title": "Follow Harbor's settlement ticket to the bank notice",
                            "scope": {
                                "customer_id": CUST_HARBOR,
                                "bank_account_id": "CASH-US-01",
                                "currency": "USD",
                                "channel": "WIRE",
                            },
                            "lookup": {
                                "remittance_reference_field": "settlement_ticket",
                                "bank_notice_reference_field": "transfer_reference",
                                "search_terms": ["bank notice", "receiving fee"],
                            },
                            "summary": (
                                "Use the current remittance ticket to locate the current "
                                "bank notice, then check the fixed company policy."
                            ),
                        },
                    )
                ]
            )
        ]
    )


def _dev_factory(settings):
    suite = load_dev_suite(settings.data_dir)
    by_case = {entry.case_id: (entry, package, oracle) for entry, package, oracle in suite}

    def factory(case_id: str, arm: str):
        _entry, package, oracle = by_case[case_id]
        return scripted_dev_provider(package, oracle, arm)

    return factory


def _bind_live_report(db_path: Path, result) -> str:
    connection = open_database(db_path)
    live_id = f"RPT-{uuid.uuid4().hex}"
    live = result.report.model_copy(update={"report_id": live_id, "mode": ExecutionMode.LIVE})
    insert_candidate_test(
        connection,
        live,
        outcomes={
            "report": live.model_dump(mode="json"),
            "episodes": [item.model_dump(mode="json") for item in result.episodes],
        },
        score_summary={
            "passed_limited_checks": True,
            "failed_reasons": [],
            "demonstrated_useful_improvement": result.demonstrated_useful_improvement,
        },
    )
    record = get_precedent(connection, result.precedent_id)
    assert record is not None
    update_precedent_lifecycle(
        connection,
        record.model_copy(update={"test_report_id": live_id, "status": LessonState.PASSED}),
    )
    connection.close()
    return live_id


def test_learning_results_activates_passing_version_and_matches_report_json(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    demo = demo_init(settings, seed=DEFAULT_SEED)
    workspace_id = demo.workspace_id
    saved = save_correction(
        settings,
        workspace_id,
        T03_CASE_ID,
        {
            "text": TEACHING_TEXT,
            "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
            "corrected_proposal": {
                "payment_id": T03_PAYMENT_ID,
                "customer_id": CUST_HARBOR,
                "resolution_type": "SINGLE_WITH_BANK_FEE",
                "allocations": [
                    {"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}
                ],
                "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
                "explanation": (
                    "The settlement ticket links the remittance and bank advice; "
                    "the documented amounts agree exactly."
                ),
                "precedent_ids_used": [],
            },
            "review_reason_code": None,
        },
    )
    drafted = propose_lesson(
        settings,
        saved.correction_id,
        mode=ExecutionMode.TEST,
        provider=_compiler_script(),
    )
    assert drafted.precedent_id is not None
    tested = run_lesson_test(
        settings,
        drafted.precedent_id,
        mode=ExecutionMode.TEST,
        provider_factory=_dev_factory(settings),
    )
    assert tested.passed_limited_checks is True
    assert tested.report.mode is ExecutionMode.TEST
    live_report_id = _bind_live_report(settings.db_path, tested)
    suite = load_split_suite(settings.data_dir, DatasetSplit.TEACHING, limit=2)
    packages = {entry.case_id: (package, oracle) for entry, package, oracle in suite}

    def eval_factory(case_id: str, arm: str):
        package, oracle = packages[case_id]
        return scripted_split_provider(package, oracle, arm)

    eval_report = run_evaluation(
        settings,
        EvaluationRequest(
            source_workspace_id=workspace_id,
            split=DatasetSplit.TEACHING,
            case_limit=2,
            mode=ExecutionMode.TEST,
            deadline_seconds=120,
            output_dir=tmp_path / "artifacts" / "evaluations",
        ),
        provider_factory=eval_factory,
    )
    raw_path = Path(eval_report.artifact_paths["report_json"])
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    inner = raw_payload["report"]
    baseline_metric = inner["metrics"]["baseline"]["correct_autonomous_resolutions"]
    memory_metric = inner["metrics"]["memory"]["correct_autonomous_resolutions"]
    expected_baseline = f"{baseline_metric['value']}/{baseline_metric['denominator']}"
    expected_memory = f"{memory_metric['value']}/{memory_metric['denominator']}"

    counts = {"test": 0, "evaluate": 0, "activate": 0}

    def _blocked_test(*_args: Any, **_kwargs: Any):
        counts["test"] += 1
        raise AssertionError("candidate testing must not run on load, refresh, or this click")

    def _blocked_eval(*_args: Any, **_kwargs: Any):
        counts["evaluate"] += 1
        raise AssertionError("evaluation must not run on load, refresh, or Load report")

    def _counted_activate(*args: Any, **kwargs: Any):
        counts["activate"] += 1
        from precedent.services import activate_lesson as real_activate

        return real_activate(*args, **kwargs)

    monkeypatch.setattr("precedent.ui_services.load_settings", lambda: settings)
    monkeypatch.setattr("precedent.ui_services.test_lesson", _blocked_test)
    monkeypatch.setattr("precedent.ui_services.run_evaluation", _blocked_eval)
    monkeypatch.setattr("precedent.ui_services.activate_lesson", _counted_activate)

    at = AppTest.from_file(str(APP_PATH), default_timeout=90).run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 0}
    at.switch_page(LEARNING_PAGE)
    at.run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 0}
    page = " ".join(item.value for item in at.markdown)
    captions = " ".join(item.value for item in at.caption)
    combined = page + " " + captions
    assert drafted.precedent_id in combined
    assert "PASSED" in combined
    before = get_lesson(settings, drafted.precedent_id)
    assert before is not None
    assert before.status is LessonState.PASSED
    assert before.test_report_id == live_report_id
    expander_labels = [item.label for item in at.expander]
    assert any(V02_CASE_ID in label and "V02" in label for label in expander_labels)
    assert "OPEN_DISPUTE" in combined
    assert "1,565.00" in combined
    assert "DOC-18991B17C9B2" in combined or "DISPUTE_NOTICE" in combined
    live_test = at.button(f"run_five_case_live_test_{drafted.precedent_id}")
    assert live_test.disabled
    assert "PRECEDENT_ENABLE_LIVE" in (live_test.help or "") or "PRECEDENT_ENABLE_LIVE" in captions
    activate = at.button(f"activate_tested_lesson_{drafted.precedent_id}")
    assert not activate.disabled
    activate.click()
    at.run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 1}
    activated = get_lesson(settings, drafted.precedent_id)
    assert activated is not None
    assert activated.status is LessonState.ACTIVE
    assert activated.activation_actor == "demo_controller"
    success = " ".join(item.value for item in at.success)
    assert drafted.precedent_id in success
    # Evaluations and recorded runs live on Results; lessons stay on the Lessons page.
    at.switch_page(RESULTS_PAGE)
    at.run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 1}
    teaching = at.button("run_teaching_evaluation")
    assert teaching.disabled
    at.selectbox("evaluation_select").select(eval_report.experiment_id)
    at.run()
    at.button("load_evaluation_report").click()
    at.run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 1}
    frames = [str(getattr(frame, "value", frame)) for frame in at.dataframe]
    table_text = " ".join(frames)
    assert expected_baseline in table_text
    assert expected_memory in table_text
    # The chart is drawn beside the table from the same stored arms, never instead of it.
    charts = at.get("vega_lite_chart")
    assert charts
    chart_spec = charts[0].proto.spec
    assert '"Baseline"' in chart_spec
    assert '"Memory"' in chart_spec
    assert eval_report.experiment_id in " ".join(item.value for item in at.markdown)
    json_bodies = [item.value for item in at.json]
    assert json_bodies
    loaded_raw = json.loads(json_bodies[-1])
    assert loaded_raw["report"]["metrics"]["baseline"]["correct_autonomous_resolutions"] == (
        baseline_metric
    )
    assert loaded_raw["report"]["experiment_id"] == eval_report.experiment_id
    at.run()
    assert not at.exception
    assert counts == {"test": 0, "evaluate": 0, "activate": 1}
    still = get_lesson(settings, drafted.precedent_id)
    assert still is not None
    assert still.status is LessonState.ACTIVE
    assert still.activation_actor == "demo_controller"


def test_recorded_run_viewer_is_not_live(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    demo_init(settings, seed=DEFAULT_SEED)
    result = investigate(
        settings,
        "WS-TEACH-001",
        T01_CASE_ID,
        mode=ExecutionMode.TEST,
        memory_mode=MemoryMode.OFF,
        provider=_review_script(),
        run_id="RUN-UI-T01",
    )
    export_recorded_run(settings, result.run_id)
    before = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert before.snapshot.payment.applied is False
    t01_before = get_case_detail(settings, "WS-TEACH-001", T01_CASE_ID)
    t01_state = t01_before.snapshot.summary.case_state
    monkeypatch.setattr("precedent.ui_services.load_settings", lambda: settings)
    at = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
    assert not at.exception
    at.switch_page(RESULTS_PAGE)
    at.run()
    assert not at.exception
    warnings = " ".join(item.value for item in at.warning)
    captions = " ".join(item.value for item in at.caption)
    markdown = " ".join(item.value for item in at.markdown)
    combined = warnings + " " + captions + " " + markdown
    assert "Recorded run - no live model calls" in combined
    assert "never invokes apply_proposal" in combined
    assert result.run_id in combined
    assert "TEST SIMULATION" in combined
    assert "PRECEDENT_ENABLE_LIVE" in combined
    at.switch_page(QUEUE_PAGE)
    at.run()
    at.switch_page(RESULTS_PAGE)
    at.run()
    assert not at.exception
    after = get_case_detail(settings, "WS-TEACH-001", T03_CASE_ID)
    assert after.snapshot.payment.applied is False
    t01_after = get_case_detail(settings, "WS-TEACH-001", T01_CASE_ID)
    assert t01_after.snapshot.summary.case_state is t01_state
    assert t01_after.snapshot.payment.applied is False
