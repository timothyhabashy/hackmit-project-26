from __future__ import annotations

import argparse
import importlib
import json
import sys
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError

from precedent.agent import InvestigationError
from precedent.config import (
    ConfigError,
    Settings,
    live_readiness_code,
    load_settings,
    public_settings_view,
)
from precedent.db import (
    PersistenceError,
    get_correction,
    get_precedent,
    list_run_events,
    open_database,
)
from precedent.evaluation import (
    DEFAULT_EVALUATION_DEADLINE_SECONDS,
    EvaluationError,
    format_evaluation_report,
    load_evaluation_report,
)
from precedent.fixtures import DEFAULT_SEED, FixtureError, build_fixtures, load_manifest
from precedent.learning import (
    DEFAULT_CONTROLLER_ACTOR,
    LearningError,
    format_lesson_draft,
    format_lesson_lifecycle,
    format_lesson_test,
)
from precedent.models import (
    AgentOutcome,
    ApplicationStatus,
    DatasetSplit,
    EvaluationRequest,
    EvaluationState,
    ExecutionMode,
    LessonDraftStatus,
    MemoryMode,
)
from precedent.providers import ProviderError, live_tool_smoke_test
from precedent.services import (
    activate_lesson,
    apply_correction,
    demo_init,
    demo_new,
    export_recorded_run,
    get_case_detail,
    investigate,
    list_cases,
    propose_lesson,
    redraft_lesson,
    retire_lesson,
    run_evaluation,
    save_correction,
    test_lesson,
)

_RUNTIME_PACKAGES = (
    ("precedent-hackmit", "precedent"),
    ("pydantic", "pydantic"),
    ("python-dotenv", "dotenv"),
    ("anthropic", "anthropic"),
    ("streamlit", "streamlit"),
)


class LivePreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return _cmd_preflight(offline=args.offline, live=args.live)
    if args.command == "fixtures":
        if args.fixtures_command == "build":
            return _cmd_fixtures_build(seed=args.seed)
    if args.command == "demo":
        if args.demo_command == "init":
            return _cmd_demo_init()
        if args.demo_command == "new":
            return _cmd_demo_new(dataset=args.dataset, memory_from=args.memory_from)
    if args.command == "run":
        return _cmd_run(
            workspace_id=args.workspace,
            case_id=args.case,
            memory=args.memory,
            live=args.live,
        )
    if args.command == "correction":
        if args.correction_command == "save":
            return _cmd_correction_save(
                workspace_id=args.workspace,
                case_id=args.case,
                file_path=args.file,
            )
        if args.correction_command == "apply":
            return _cmd_correction_apply(
                correction_id=args.id,
                actor=args.actor,
                idempotency_key=args.idempotency_key,
            )
    if args.command == "lesson":
        if args.lesson_command == "propose":
            return _cmd_lesson_propose(correction_id=args.correction, live=args.live)
        if args.lesson_command == "test":
            return _cmd_lesson_test(precedent_id=args.id, live=args.live)
        if args.lesson_command == "activate":
            return _cmd_lesson_activate(precedent_id=args.id, actor=args.actor)
        if args.lesson_command == "retire":
            return _cmd_lesson_retire(precedent_id=args.id, actor=args.actor)
        if args.lesson_command == "redraft":
            return _cmd_lesson_redraft(precedent_id=args.id)
    if args.command == "evaluate":
        return _cmd_evaluate(
            workspace_id=args.workspace,
            split=args.split,
            limit=args.limit,
            live=args.live,
            deadline_seconds=args.deadline_seconds,
            call_cap=args.call_cap,
        )
    if args.command == "report":
        if args.report_command == "show":
            return _cmd_report_show(experiment_id=args.id)
    if args.command == "cases":
        if args.cases_command == "list":
            return _cmd_cases_list(workspace_id=args.workspace)
    if args.command == "case":
        if args.case_command == "show":
            return _cmd_case_show(workspace_id=args.workspace, case_id=args.case)
    if args.command == "replay":
        if args.replay_command == "export":
            return _cmd_replay_export(run_id=args.run)
    print("Unknown command.")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="precedent",
        description="Precedent local demonstration CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight", help="Check local setup or live API readiness.")
    mode = preflight.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--offline",
        action="store_true",
        help="Validate config, imports, and local paths without network calls.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Make one low-token tool-calling request when live mode and credentials are set.",
    )
    fixtures = sub.add_parser(
        "fixtures",
        help="Generate synthetic source packages and grading keys.",
    )
    fixtures_sub = fixtures.add_subparsers(dest="fixtures_command", required=True)
    build = fixtures_sub.add_parser("build", help="Write deterministic fixtures for a seed.")
    build.add_argument("--seed", type=int, required=True, help="Non-negative integer seed.")
    demo = sub.add_parser("demo", help="Create sandbox workspaces from generated fixtures.")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    demo_sub.add_parser("init", help="Import the January teaching packages into a new workspace.")
    demo_new_parser = demo_sub.add_parser(
        "new",
        help="Create a new workspace without deleting stored reports.",
    )
    demo_new_parser.add_argument(
        "--dataset",
        choices=("teaching", "candidate", "heldout"),
        default="teaching",
        help="Which generated split to import. Default teaching.",
    )
    demo_new_parser.add_argument(
        "--memory-from",
        dest="memory_from",
        default=None,
        help="Optional source workspace whose active lessons should be attached.",
    )
    run_parser = sub.add_parser("run", help="Investigate one case through the bounded agent loop.")
    run_parser.add_argument("--workspace", required=True, help="Workspace ID.")
    run_parser.add_argument("--case", required=True, help="Case ID.")
    run_parser.add_argument(
        "--memory",
        required=True,
        choices=("on", "off"),
        help="Use attached active lessons (on) or an empty snapshot (off).",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Required. Makes real provider calls; never falls back to a scripted double.",
    )
    correction = sub.add_parser("correction", help="Save or apply a controller correction.")
    correction_sub = correction.add_subparsers(dest="correction_command", required=True)
    correction_save = correction_sub.add_parser(
        "save",
        help="Save correction text, cited evidence, and an optional human proposal.",
    )
    correction_save.add_argument("--workspace", required=True, help="Workspace ID.")
    correction_save.add_argument("--case", required=True, help="Case ID.")
    correction_save.add_argument(
        "--file",
        required=True,
        help="JSON file containing typed CorrectionInput.",
    )
    correction_apply = correction_sub.add_parser(
        "apply",
        help="Apply a saved verified human proposal without calling the investigator.",
    )
    correction_apply.add_argument("--id", required=True, help="Correction ID.")
    correction_apply.add_argument(
        "--actor",
        default=DEFAULT_CONTROLLER_ACTOR,
        help="Controller actor label. Default demo_controller.",
    )
    correction_apply.add_argument(
        "--idempotency-key",
        default=None,
        help="Optional apply key. Defaults to correction-apply:<id>.",
    )
    lesson = sub.add_parser("lesson", help="Draft, test, or activate a structured lesson.")
    lesson_sub = lesson.add_subparsers(dest="lesson_command", required=True)
    lesson_propose = lesson_sub.add_parser(
        "propose",
        help="Ask the compiler for a draft-only wire_fee_lookup_v1 hint.",
    )
    lesson_propose.add_argument("--correction", required=True, help="Correction ID.")
    lesson_propose.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Required. Makes real provider calls; never falls back to a scripted double.",
    )
    lesson_test = lesson_sub.add_parser(
        "test",
        help="Run the five-case paired candidate suite (ten episodes).",
    )
    lesson_test.add_argument("--id", required=True, help="Precedent ID.")
    lesson_test.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Required. Makes real provider calls; never falls back to a scripted double.",
    )
    lesson_activate = lesson_sub.add_parser(
        "activate",
        help="Activate a live-tested PASSED lesson after explicit approval.",
    )
    lesson_activate.add_argument("--id", required=True, help="Precedent ID.")
    lesson_activate.add_argument(
        "--actor",
        default=DEFAULT_CONTROLLER_ACTOR,
        help="Controller actor label. Default demo_controller.",
    )
    lesson_retire = lesson_sub.add_parser(
        "retire",
        help="Retire an active lesson so it is excluded from future retrieval.",
    )
    lesson_retire.add_argument("--id", required=True, help="Precedent ID.")
    lesson_retire.add_argument(
        "--actor",
        default=DEFAULT_CONTROLLER_ACTOR,
        help="Controller actor label. Default demo_controller.",
    )
    lesson_redraft = lesson_sub.add_parser(
        "redraft",
        help="Create a new DRAFT with the same hint and no inherited test report.",
    )
    lesson_redraft.add_argument("--id", required=True, help="Precedent ID.")
    evaluate = sub.add_parser(
        "evaluate",
        help="Run a paired baseline/memory evaluation on isolated case copies.",
    )
    evaluate.add_argument("--workspace", required=True, help="Source workspace for frozen memory.")
    evaluate.add_argument(
        "--split",
        required=True,
        choices=("teaching", "candidate", "heldout"),
        help="Dataset split to score. Held-out is for Session 18, not development.",
    )
    evaluate.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Development case cap. Forbidden for held-out.",
    )
    evaluate.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Required. Makes real provider calls; never falls back to a scripted double.",
    )
    evaluate.add_argument(
        "--deadline-seconds",
        type=int,
        default=DEFAULT_EVALUATION_DEADLINE_SECONDS,
        help="Experiment elapsed-time cap. Default 5400.",
    )
    evaluate.add_argument(
        "--call-cap",
        type=int,
        default=None,
        help="Optional experiment-level model-attempt cap.",
    )
    report = sub.add_parser("report", help="Show a saved evaluation report without rerunning.")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_show = report_sub.add_parser("show", help="Print a saved evaluation report.")
    report_show.add_argument("--id", required=True, help="Experiment ID.")
    cases = sub.add_parser("cases", help="List sandbox cases in a workspace.")
    cases_sub = cases.add_subparsers(dest="cases_command", required=True)
    cases_list = cases_sub.add_parser("list", help="List cases and payment states.")
    cases_list.add_argument("--workspace", required=True, help="Workspace ID.")
    case_parser = sub.add_parser("case", help="Show one sandbox case.")
    case_sub = case_parser.add_subparsers(dest="case_command", required=True)
    case_show = case_sub.add_parser(
        "show", help="Show payment, invoices, documents, and the latest run."
    )
    case_show.add_argument("--workspace", required=True, help="Workspace ID.")
    case_show.add_argument("--case", required=True, help="Case ID.")
    replay = sub.add_parser("replay", help="Export a persisted run for labeled recorded replay.")
    replay_sub = replay.add_subparsers(dest="replay_command", required=True)
    replay_export = replay_sub.add_parser(
        "export",
        help="Write a recorded-run manifest. Viewing it never reapplies money.",
    )
    replay_export.add_argument("--run", required=True, help="Persisted run ID.")
    return parser


def _cmd_preflight(*, offline: bool, live: bool) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("Precedent preflight")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    if offline:
        return _run_offline_preflight(settings)
    if live:
        return _run_live_preflight(settings)
    print("Result: FAIL")
    print("Reason: INVALID_CONFIG (choose --offline or --live)")
    return 2


def _cmd_fixtures_build(*, seed: int) -> int:
    try:
        settings = load_settings()
        manifest = build_fixtures(settings.data_dir, seed=seed)
    except ConfigError as exc:
        print("Precedent fixtures build")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except FixtureError as exc:
        print("Precedent fixtures build")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent fixtures build")
    print(f"Seed: {manifest.seed}")
    print(f"Packages: {len(manifest.cases)}")
    print(f"Manifest: {settings.data_dir / 'manifests' / f'fixtures-seed-{manifest.seed}.json'}")
    print("Authoring  Split      Case ID              Payment")
    for entry in manifest.cases:
        print(
            f"{entry.authoring_id:<10} {entry.split.value:<10} "
            f"{entry.case_id:<20} {entry.payment_id}"
        )
    print("Result: PASS")
    return 0


def _cmd_demo_init() -> int:
    try:
        settings = load_settings()
        result = demo_init(settings)
        manifest = load_manifest(
            settings.data_dir / "manifests" / f"fixtures-seed-{DEFAULT_SEED}.json"
        )
    except ConfigError as exc:
        print("Precedent demo init")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except FixtureError as exc:
        print("Precedent demo init")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent demo init")
    print(f"Workspace: {result.workspace_id}")
    print(f"Name: {result.name}")
    print(f"Cases: {len(result.case_ids)}")
    teaching = {
        entry.authoring_id: entry for entry in manifest.cases if entry.split.value == "teaching"
    }
    for authoring_id in ("T03", "T06", "T10"):
        entry = teaching.get(authoring_id)
        if entry is not None:
            print(f"{authoring_id}: case {entry.case_id} payment {entry.payment_id}")
    print("Result: PASS")
    return 0


def _cmd_demo_new(*, dataset: str, memory_from: str | None) -> int:
    try:
        settings = load_settings()
        result = demo_new(
            settings,
            dataset=DatasetSplit(dataset),
            memory_from=memory_from,
        )
    except ConfigError as exc:
        print("Precedent demo new")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except FixtureError as exc:
        print("Precedent demo new")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent demo new")
    print(f"Workspace: {result.workspace_id}")
    print(f"Dataset: {result.split.value}")
    print(f"Cases: {len(result.case_ids)}")
    print(f"Attached lessons: {len(result.attached_precedent_ids)}")
    print("Result: PASS")
    return 0


def _cmd_run(*, workspace_id: str, case_id: str, memory: str, live: bool) -> int:
    if not live:
        print("Precedent run")
        print("Result: FAIL")
        print("Reason: INVALID_INPUT (--live is required; scripted doubles are tests only)")
        return 2
    try:
        settings = load_settings()
        result = investigate(
            settings,
            workspace_id,
            case_id,
            mode=ExecutionMode.LIVE,
            memory_mode=MemoryMode(memory),
        )
    except ConfigError as exc:
        print("Precedent run")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except InvestigationError as exc:
        print("Precedent run")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    except ProviderError as exc:
        print("Precedent run")
        print("Result: FAIL")
        print(f"Reason: {exc.code.value} ({exc.message})")
        return 1
    print("Precedent run")
    print(f"Workspace: {result.workspace_id}")
    print(f"Case: {result.case_id}")
    print(f"Run: {result.run_id}")
    print(f"Mode: {result.execution_mode.value}")
    print(f"Outcome: {result.terminal_outcome.value}")
    print(f"Summary: {result.summary}")
    if result.application_id:
        print(f"Application: {result.application_id}")
    if result.proposal_id:
        print(f"Proposal: {result.proposal_id}")
    if result.review is not None:
        print(f"Review: {result.review.reason_code.value} ({result.review.message})")
    if result.error is not None:
        print(f"Error: {result.error.code.value} ({result.error.message})")
    print(
        "Usage: "
        f"model_attempts={result.usage.model_attempts} "
        f"tool_calls={result.usage.tool_calls} "
        f"elapsed_ms={result.usage.elapsed_ms}"
    )
    if result.usage.input_tokens is None and result.usage.output_tokens is None:
        print("Tokens: unavailable")
    else:
        print(f"Tokens: input={result.usage.input_tokens} output={result.usage.output_tokens}")
    if result.usage.estimated_cost_usd is None:
        print("Estimated cost: unavailable")
    else:
        print(f"Estimated cost USD: {result.usage.estimated_cost_usd}")
    _print_trace(settings, result.run_id)
    if result.terminal_outcome is AgentOutcome.ERROR:
        print("Result: FAIL")
        return 1
    print("Result: PASS")
    return 0


def _cmd_correction_save(*, workspace_id: str, case_id: str, file_path: str) -> int:
    path = Path(file_path)
    if not path.is_file():
        print("Precedent correction save")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT (file not found: {path})")
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print("Precedent correction save")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT (invalid JSON: {exc.msg})")
        return 2
    try:
        settings = load_settings()
        record = save_correction(settings, workspace_id, case_id, payload)
    except ConfigError as exc:
        print("Precedent correction save")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent correction save")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    print("Precedent correction save")
    print(f"Workspace: {record.workspace_id}")
    print(f"Case: {record.case_id}")
    print(f"Correction: {record.correction_id}")
    print(f"Run: {record.run_id}")
    print(f"Actor: {record.actor}")
    print(f"Lesson eligible: {str(record.lesson_eligibility).lower()}")
    if record.eligibility_reason:
        print(f"Eligibility reason: {record.eligibility_reason}")
    if record.proposal_id:
        print(f"Proposal: {record.proposal_id}")
    print(f"Evidence: {', '.join(record.cited_document_ids)}")
    print("Result: PASS")
    return 0


def _cmd_correction_apply(*, correction_id: str, actor: str, idempotency_key: str | None) -> int:
    key = idempotency_key or f"correction-apply:{correction_id}"
    try:
        settings = load_settings()
        result = apply_correction(settings, correction_id, idempotency_key=key, actor=actor)
    except ConfigError as exc:
        print("Precedent correction apply")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent correction apply")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    print("Precedent correction apply")
    print(f"Correction: {correction_id}")
    print(f"Actor: {actor}")
    print(f"Status: {result.status.value}")
    if result.application_id:
        print(f"Application: {result.application_id}")
    if result.proposal_id:
        print(f"Proposal: {result.proposal_id}")
    if result.issues:
        codes = ", ".join(issue.code.value for issue in result.issues)
        print(f"Issues: {codes}")
    print(f"Ledger revision: {result.ledger_revision}")
    if result.status is ApplicationStatus.REJECTED:
        print("Result: REJECTED")
        return 0
    print("Result: PASS")
    return 0


def _cmd_lesson_propose(*, correction_id: str, live: bool) -> int:
    if not live:
        print("Precedent lesson propose")
        print("Result: FAIL")
        print("Reason: INVALID_INPUT (--live is required; scripted doubles are tests only)")
        return 2
    try:
        settings = load_settings()
        result = propose_lesson(settings, correction_id, mode=ExecutionMode.LIVE)
    except ConfigError as exc:
        print("Precedent lesson propose")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent lesson propose")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    except ProviderError as exc:
        print("Precedent lesson propose")
        print("Result: FAIL")
        print(f"Reason: {exc.code.value} ({exc.message})")
        return 1
    print("Precedent lesson propose")
    print(f"Correction: {result.correction_id}")
    print(f"Status: {result.status.value}")
    if result.precedent_id:
        print(f"Precedent: {result.precedent_id}")
    if result.version is not None:
        print(f"Version: {result.version}")
    metadata = result.compiler_run_metadata
    if metadata:
        print(
            "Compiler: "
            f"provider={metadata.get('provider')} "
            f"model={metadata.get('model')} "
            f"prompt={metadata.get('prompt_version')}"
        )
    connection = open_database(settings.db_path)
    try:
        correction = get_correction(connection, result.correction_id)
        precedent = (
            None if result.precedent_id is None else get_precedent(connection, result.precedent_id)
        )
        if correction is not None:
            print(format_lesson_draft(correction, result, precedent))
    finally:
        connection.close()
    if result.status is LessonDraftStatus.ERROR:
        print("Result: FAIL")
        return 1
    print("Result: PASS")
    return 0


def _cmd_lesson_test(*, precedent_id: str, live: bool) -> int:
    if not live:
        print("Precedent lesson test")
        print("Result: FAIL")
        print("Reason: INVALID_INPUT (--live is required; scripted doubles are tests only)")
        return 2
    try:
        settings = load_settings()
        result = test_lesson(settings, precedent_id, mode=ExecutionMode.LIVE)
    except ConfigError as exc:
        print("Precedent lesson test")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent lesson test")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    except ProviderError as exc:
        print("Precedent lesson test")
        print("Result: FAIL")
        print(f"Reason: {exc.code.value} ({exc.message})")
        return 1
    print("Precedent lesson test")
    print(format_lesson_test(result))
    if result.report.state.value == "FAILED":
        print("Result: FAILED")
        return 1
    print("Result: PASS")
    return 0


def _cmd_lesson_activate(*, precedent_id: str, actor: str) -> int:
    try:
        settings = load_settings()
        result = activate_lesson(settings, precedent_id, actor=actor)
    except ConfigError as exc:
        print("Precedent lesson activate")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent lesson activate")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    print("Precedent lesson activate")
    print(format_lesson_lifecycle(result))
    print("Result: PASS")
    return 0


def _cmd_lesson_retire(*, precedent_id: str, actor: str) -> int:
    try:
        settings = load_settings()
        result = retire_lesson(settings, precedent_id, actor=actor)
    except ConfigError as exc:
        print("Precedent lesson retire")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent lesson retire")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    print("Precedent lesson retire")
    print(format_lesson_lifecycle(result))
    print("Result: PASS")
    return 0


def _cmd_lesson_redraft(*, precedent_id: str) -> int:
    try:
        settings = load_settings()
        result = redraft_lesson(settings, precedent_id)
    except ConfigError as exc:
        print("Precedent lesson redraft")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except LearningError as exc:
        print("Precedent lesson redraft")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    print("Precedent lesson redraft")
    print(format_lesson_lifecycle(result))
    print("Result: PASS")
    return 0


def _cmd_evaluate(
    *,
    workspace_id: str,
    split: str,
    limit: int | None,
    live: bool,
    deadline_seconds: int,
    call_cap: int | None,
) -> int:
    if not live:
        print("Precedent evaluate")
        print("Result: FAIL")
        print("Reason: INVALID_INPUT (--live is required; scripted doubles are tests only)")
        return 2
    try:
        settings = load_settings()
        request = EvaluationRequest(
            source_workspace_id=workspace_id,
            split=DatasetSplit(split),
            case_limit=limit,
            mode=ExecutionMode.LIVE,
            deadline_seconds=deadline_seconds,
            call_cap=call_cap,
            output_dir=settings.repo_root / "artifacts" / "evaluations",
        )
        if limit is None:
            scheduled_label = "all cases in split"
            scheduled_episodes = "2 per case"
        else:
            scheduled_label = f"{limit} cases"
            scheduled_episodes = str(limit * 2)
        print("Precedent evaluate")
        print(f"Workspace: {workspace_id}")
        print(f"Split: {split}")
        print(f"Scheduled: {scheduled_label} / {scheduled_episodes} episodes")
        print(f"Deadline seconds: {deadline_seconds}")
        print(f"Call cap: {call_cap if call_cap is not None else '(none)'}")
        print("Proceeding because --live was provided.")
        report = run_evaluation(settings, request)
    except ConfigError as exc:
        print("Precedent evaluate")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except ValidationError as exc:
        print("Precedent evaluate")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc.errors()[0]['msg'] if exc.errors() else exc})")
        return 2
    except EvaluationError as exc:
        print("Precedent evaluate")
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 2 if exc.code in _RUN_INPUT_CODES else 1
    except ProviderError as exc:
        print("Precedent evaluate")
        print("Result: FAIL")
        print(f"Reason: {exc.code.value} ({exc.message})")
        return 1
    print(format_evaluation_report(report))
    if report.state is EvaluationState.PARTIAL:
        print("Result: PARTIAL")
        return 1
    if report.state is EvaluationState.FAILED:
        print("Result: FAIL")
        return 1
    print("Result: PASS")
    return 0


def _cmd_cases_list(*, workspace_id: str) -> int:
    try:
        settings = load_settings()
        cases = list_cases(settings, workspace_id)
    except ConfigError as exc:
        print("Precedent cases list")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except PersistenceError as exc:
        print("Precedent cases list")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent cases list")
    print(f"Workspace: {workspace_id}")
    print(f"Cases: {len(cases)}")
    for item in cases:
        print(
            f"{item.case_id}  {item.payment_id}  {item.case_state.value}  "
            f"{_usd(item.amount_cents)} {item.currency}  {item.payer_text}"
        )
    print("Result: PASS")
    return 0


def _cmd_case_show(*, workspace_id: str, case_id: str) -> int:
    try:
        settings = load_settings()
        detail = get_case_detail(settings, workspace_id, case_id)
    except ConfigError as exc:
        print("Precedent case show")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except PersistenceError as exc:
        print("Precedent case show")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    snapshot = detail.snapshot
    payment = snapshot.payment
    print("Precedent case show")
    print(f"Workspace: {workspace_id}")
    print(f"Case: {snapshot.summary.case_id}")
    print(f"State: {snapshot.summary.case_state.value}")
    print(
        f"Payment: {payment.payment_id}  {_usd(payment.amount_cents)} {payment.currency}  "
        f"applied={str(payment.applied).lower()}  {payment.payer_text}"
    )
    print("Invoices:")
    for invoice in snapshot.invoices:
        print(
            f"  {invoice.invoice_id}  outstanding {_usd(invoice.outstanding_cents)} / "
            f"original {_usd(invoice.original_cents)} {invoice.currency}"
        )
    print("Documents:")
    if not detail.documents:
        print("  none")
    for document in detail.documents:
        print(f"  {document.document_id}  {document.kind.value}  {document.title}")
    if detail.application is None:
        print("Application: none")
    else:
        print(f"Application: {detail.application.application_id}")
        for allocation in detail.application.allocations:
            print(
                f"  {allocation.invoice_id}  cash {_usd(allocation.cash_cents)}  "
                f"fee {_usd(allocation.fee_cents)}"
            )
    if not detail.corrections:
        print("Corrections: none")
    else:
        print("Corrections:")
        for correction in detail.corrections:
            print(f"  {correction.correction_id}  actor {correction.actor}")
    if detail.latest_run is None:
        print("Latest run: none")
    else:
        outcome = (
            "none"
            if detail.latest_run.terminal_outcome is None
            else detail.latest_run.terminal_outcome.value
        )
        print(
            f"Latest run: {detail.latest_run.run_id}  {detail.latest_run.mode.value}  "
            f"{detail.latest_run.state.value}  {outcome}"
        )
    print("Result: PASS")
    return 0


def _cmd_replay_export(*, run_id: str) -> int:
    try:
        settings = load_settings()
        path = export_recorded_run(settings, run_id)
    except ConfigError as exc:
        print("Precedent replay export")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    except PersistenceError as exc:
        print("Precedent replay export")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent replay export")
    print(f"Run: {run_id}")
    print(f"Manifest: {path}")
    print("Label: Recorded run - no live model calls")
    print("Viewing this file never invokes apply_proposal.")
    print("Result: PASS")
    return 0


def _usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    amount = abs(int(cents))
    return f"{sign}${amount // 100:,}.{amount % 100:02d}"


def _cmd_report_show(*, experiment_id: str) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("Precedent report show")
        print("Result: FAIL")
        print(f"Reason: INVALID_CONFIG ({exc})")
        return 2
    report_path = settings.repo_root / "artifacts" / "evaluations" / experiment_id / "report.json"
    if not report_path.is_file():
        print("Precedent report show")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT (report {experiment_id} was not found)")
        return 2
    try:
        report = load_evaluation_report(report_path)
    except Exception as exc:
        print("Precedent report show")
        print("Result: FAIL")
        print(f"Reason: INVALID_INPUT ({exc})")
        return 2
    print("Precedent report show")
    print(format_evaluation_report(report))
    print("Result: PASS")
    return 0


def _print_trace(settings: Settings, run_id: str) -> None:
    connection = open_database(settings.db_path)
    try:
        events = list_run_events(connection, run_id)
    finally:
        connection.close()
    print(f"Trace events: {len(events)}")
    for event in events:
        extra = _trace_label(event.payload)
        line = f"  {event.sequence:>3} {event.event_kind.value}"
        if extra:
            line = f"{line} {extra}"
        print(line)


def _trace_label(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    tool = payload.get("tool")
    if isinstance(tool, str) and tool:
        parts.append(tool)
    status = payload.get("status")
    if isinstance(status, str) and status:
        parts.append(status)
    code = payload.get("error_code") or payload.get("terminal_outcome")
    if isinstance(code, str) and code:
        parts.append(code)
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        parts.append(reason)
    return " ".join(parts)


_RUN_INPUT_CODES = frozenset(
    {
        "INVALID_INPUT",
        "INVALID_CONFIG",
        "LIVE_DISABLED",
        "MISSING_CREDENTIALS",
        "MODEL_UNAVAILABLE",
        "NOT_FOUND",
    }
)


def _run_offline_preflight(settings: Settings) -> int:
    print("Precedent offline preflight")
    print(f"Python: {sys.version.split()[0]}")
    print("Package versions:")
    import_ok = True
    for dist_name, module_name in _RUNTIME_PACKAGES:
        version = _package_version(dist_name)
        try:
            importlib.import_module(module_name)
            status = "importable"
        except Exception as exc:
            import_ok = False
            status = f"IMPORT_FAILED ({type(exc).__name__})"
        print(f"  {dist_name}: {version} ({status})")

    print("Config: OK")
    for key, value in public_settings_view(settings).items():
        print(f"  {key}: {value}")

    runtime_ok, runtime_detail = _ensure_writable_runtime_dir(settings.var_dir)
    print(f"Runtime directory: {runtime_detail}")

    source_status = _source_status(settings.source_dir)
    print(f"Source fixtures: {source_status}")

    if not import_ok:
        print("Result: FAIL")
        print("Reason: IMPORT_FAILED")
        return 1
    if not runtime_ok:
        print("Result: FAIL")
        print("Reason: RUNTIME_DIR_NOT_WRITABLE")
        return 1
    print("Result: PASS")
    return 0


def _run_live_preflight(settings: Settings) -> int:
    print("Precedent live preflight")
    print(f"enable_live: {settings.enable_live}")
    print(f"api_key: {settings.api_key_status}")
    print(f"configured_model: {settings.model_status}")
    reason = live_readiness_code(settings)
    if reason == "LIVE_DISABLED":
        print("Result: FAIL")
        print("Reason: LIVE_DISABLED (PRECEDENT_ENABLE_LIVE is false)")
        return 2
    if reason == "MISSING_CREDENTIALS":
        print("Result: FAIL")
        print("Reason: MISSING_CREDENTIALS (ANTHROPIC_API_KEY is absent)")
        return 2
    if reason == "MODEL_UNAVAILABLE":
        print("Result: FAIL")
        print("Reason: MODEL_UNAVAILABLE (PRECEDENT_MODEL is unset)")
        return 2
    try:
        actual_model = _perform_live_model_request(settings)
    except LivePreflightError as exc:
        print("Result: FAIL")
        print(f"Reason: {exc.code} ({exc.message})")
        return 1
    print(f"provider_model: {actual_model}")
    print("smoke_test: one-tool preflight_ping")
    print("Result: PASS")
    print("runtime API readiness: verified")
    return 0


def _perform_live_model_request(settings: Settings) -> str:
    try:
        result = live_tool_smoke_test(settings)
    except ProviderError as exc:
        raise LivePreflightError(exc.code.value, exc.message) from exc
    return result.model


def _package_version(dist_name: str) -> str:
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def _ensure_writable_runtime_dir(var_dir: Path) -> tuple[bool, str]:
    try:
        var_dir.mkdir(parents=True, exist_ok=True)
        probe = var_dir / ".preflight_write_check"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"not writable ({var_dir}: {exc.strerror})"
    return True, f"writable ({var_dir})"


def _source_status(source_dir: Path) -> str:
    if not source_dir.is_dir():
        return f"NOT_READY ({source_dir} is missing; fixtures are not generated yet)"
    has_files = any(path.is_file() for path in source_dir.rglob("*"))
    if not has_files:
        return f"NOT_READY ({source_dir} is empty; fixtures are not generated yet)"
    return f"READY ({source_dir})"


if __name__ == "__main__":
    raise SystemExit(main())
