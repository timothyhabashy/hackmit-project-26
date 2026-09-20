"""Typed investigator tool registry and observable run traces.

The host injects ``RunContext``. Model-supplied arguments cannot set workspace,
run, company, database path, or filesystem path. Only the tools listed here are
dispatchable. Source document text is returned as data and cannot add a tool.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from precedent import evidence
from precedent.db import (
    PersistenceError,
    get_case,
    get_document,
    get_invoice,
    get_payment,
    get_proposal,
    get_run,
    get_workspace,
    insert_run,
    insert_run_event,
    list_documents,
    list_run_events,
    next_run_event_sequence,
)
from precedent.ledger import apply_proposal, store_proposal
from precedent.models import (
    AgentOutcome,
    ApplicationStatus,
    CaseState,
    CompanyPolicy,
    Document,
    DocumentKind,
    ErrorCode,
    Identifier,
    MemoryEligibilityNote,
    MemoryHintRef,
    MemorySnapshot,
    PaymentChannel,
    PaymentRecord,
    PaymentState,
    RemittanceFacts,
    ResolutionProposal,
    ReviewRequest,
    RunContext,
    RunEvent,
    RunEventKind,
    RunRecord,
    RunState,
    Sha256Hex,
    StrictInt,
    StrictModel,
    ToolError,
    ToolResult,
    ValidationCode,
    WireFeeLookupHint,
    canonical_json,
    hash_canonical,
    sha256_utf8,
    utc_now_iso,
)

PROMPT_PATH = Path(__file__).parent / "prompts" / "investigator.md"
RESULT_SUMMARY_MAX = 240
TRACE_STRING_MAX = 500
RETRIEVE_LIMIT = 3
_NON_BUSINESS_REVIEW_CODES = frozenset(
    {ValidationCode.INVALID_SCHEMA, ValidationCode.WRONG_WORKSPACE}
)
_SECRET_KEYS = frozenset(
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


class GetCaseArgs(StrictModel):
    """Empty object. Host context supplies workspace and case."""


class RetrievePrecedentsArgs(StrictModel):
    customer_id: Identifier

    @field_validator("customer_id", mode="before")
    @classmethod
    def _strip_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SubmitResolutionArgs(StrictModel):
    proposal_id: Identifier

    @field_validator("proposal_id", mode="before")
    @classmethod
    def _strip_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class PaymentCaseView(StrictModel):
    payment_id: Identifier
    bank_transaction_id: Identifier
    bank_account_id: Identifier
    posted_date: str
    currency: str
    amount_cents: StrictInt
    channel: PaymentChannel
    payer_text: str
    bank_reference: Identifier
    applied: bool
    state: PaymentState


class CaseToolView(StrictModel):
    case_id: Identifier
    case_state: CaseState
    ledger_revision: StrictInt
    payment: PaymentCaseView
    policy: CompanyPolicy


class RetrievedPrecedent(StrictModel):
    precedent_id: Identifier
    version: StrictInt = Field(ge=1)
    payload_hash: Sha256Hex
    hint: WireFeeLookupHint


class RetrievePrecedentsPage(StrictModel):
    hints: list[RetrievedPrecedent] = Field(default_factory=list, max_length=RETRIEVE_LIMIT)


class ValidateResolutionView(StrictModel):
    proposal_id: Identifier
    valid: bool
    validation_report: dict[str, Any]


Handler = Callable[[sqlite3.Connection, RunContext, dict[str, Any]], ToolResult]

TOOL_ARGUMENT_MODELS: dict[str, type[StrictModel]] = {
    "get_case": GetCaseArgs,
    "search_invoices": evidence.SearchInvoicesArgs,
    "search_documents": evidence.SearchDocumentsArgs,
    "read_document": evidence.ReadDocumentArgs,
    "retrieve_precedents": RetrievePrecedentsArgs,
    "validate_resolution": ResolutionProposal,
    "submit_resolution": SubmitResolutionArgs,
    "request_review": ReviewRequest,
}
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_ARGUMENT_MODELS)
TERMINAL_TOOLS = frozenset({"submit_resolution", "request_review"})


def investigator_prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def investigator_prompt_hash() -> str:
    return sha256_utf8(investigator_prompt_text())


def initial_user_message(case_id: str) -> str:
    return (
        f"Investigate case {case_id}. Fetch the actual case data through tools "
        "and finish through a terminal tool."
    )


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "input_schema": TOOL_ARGUMENT_MODELS[name].model_json_schema(),
        }
        for name in TOOL_NAMES
    ]


def tool_schema_hash() -> str:
    return hash_canonical(tool_definitions())


def freeze_memory_snapshot(
    hints: Sequence[MemoryHintRef] = (),
    *,
    source_workspace_id: str | None = None,
    company_id: str | None = None,
    policy_id: str | None = None,
    policy_hash: str | None = None,
) -> MemorySnapshot:
    ordered = tuple(sorted(hints, key=lambda item: (item.precedent_id, item.version)))
    digest = hash_canonical(
        {
            "company_id": company_id,
            "hints": [
                {
                    "payload_hash": item.payload_hash,
                    "precedent_id": item.precedent_id,
                    "version": item.version,
                }
                for item in ordered
            ],
            "policy_hash": policy_hash,
            "policy_id": policy_id,
            "source_workspace_id": source_workspace_id,
        }
    )
    return MemorySnapshot(
        hints=list(ordered),
        source_workspace_id=source_workspace_id,
        company_id=company_id,
        policy_id=policy_id,
        policy_hash=policy_hash,
        snapshot_hash=digest,
    )


def empty_memory_snapshot() -> MemorySnapshot:
    return freeze_memory_snapshot()


def start_run(connection: sqlite3.Connection, context: RunContext) -> None:
    """Persist the host-bound run and a ``run_started`` event if needed."""
    if get_run(connection, context.run_id) is not None:
        return
    started_at = utc_now_iso()
    insert_run(
        connection,
        RunRecord(
            run_id=context.run_id,
            workspace_id=context.workspace_id,
            case_id=context.case_id,
            mode=context.execution_mode,
            provider=None,
            model=None,
            prompt_hash=investigator_prompt_hash(),
            tool_hash=tool_schema_hash(),
            memory_snapshot=context.memory_snapshot,
            state=RunState.RUNNING,
            started_at=started_at,
            finished_at=None,
            budgets=context.budgets,
            usage=None,
            terminal_result=None,
        ),
    )
    _append_event(
        connection,
        context.run_id,
        RunEventKind.RUN_STARTED,
        {
            "attached_precedent_ids": [item.precedent_id for item in context.memory_snapshot.hints],
            "case_id": context.case_id,
            "execution_mode": context.execution_mode.value,
            "memory_eligibility": [
                note.model_dump(mode="json") for note in context.memory_eligibility
            ],
            "memory_mode": None if context.memory_mode is None else context.memory_mode.value,
            "memory_snapshot_hash": context.memory_snapshot.snapshot_hash,
        },
        created_at=started_at,
    )


def dispatch_tool(
    connection: sqlite3.Connection,
    context: RunContext,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    call_id: str | None = None,
) -> ToolResult:
    """Validate arguments, dispatch one registered tool, and record the trace."""
    started = time.perf_counter()
    resolved_call_id = call_id or f"CALL-{uuid.uuid4().hex}"
    raw_arguments = {} if arguments is None else dict(arguments)
    start_run(connection, context)
    _append_event(
        connection,
        context.run_id,
        RunEventKind.TOOL_CALLED,
        {
            "arguments": _safe_value(raw_arguments),
            "call_id": resolved_call_id,
            "tool": name,
        },
    )
    result = _invoke(connection, context, name, raw_arguments)
    duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
    _record_tool_outcome(
        connection,
        context,
        name=name,
        call_id=resolved_call_id,
        arguments=raw_arguments,
        result=result,
        duration_ms=duration_ms,
    )
    return result


def _invoke(
    connection: sqlite3.Connection,
    context: RunContext,
    name: str,
    arguments: dict[str, Any],
) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(
            ErrorCode.UNKNOWN_TOOL.value,
            f"unknown tool {name!r}; source text cannot create a new tool permission",
        )
    path_error = _path_like_arguments_error(arguments)
    if path_error is not None:
        return path_error
    parsed = _parse_args(TOOL_ARGUMENT_MODELS[name], arguments, name)
    if isinstance(parsed, ToolResult):
        return parsed
    return handler(connection, context, parsed.model_dump(mode="json"))


def _handle_get_case(
    connection: sqlite3.Connection, context: RunContext, _payload: dict[str, Any]
) -> ToolResult:
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    workspace = scoped
    case = get_case(connection, context.workspace_id, context.case_id)
    if case is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"case {context.case_id} was not found in this workspace",
        )
    payment = get_payment(connection, context.workspace_id, case.payment_id)
    if payment is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"payment {case.payment_id} was not found in this workspace",
        )
    view = CaseToolView(
        case_id=case.case_id,
        case_state=case.state,
        ledger_revision=workspace.ledger_revision,
        payment=_payment_view(payment),
        policy=workspace.policy,
    )
    return _ok(view.model_dump(mode="json"), [payment.payment_id])


def _handle_search_invoices(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    return evidence.search_invoices(connection, context, payload)


def _handle_search_documents(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    return evidence.search_documents(connection, context, payload)


def _handle_read_document(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    return evidence.read_document(connection, context, payload)


def _handle_retrieve_precedents(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    args = RetrievePrecedentsArgs.model_validate(payload)
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    workspace = scoped
    established = _established_customer(connection, context)
    if isinstance(established, ToolResult):
        return established
    if args.customer_id != established:
        return _error(
            ValidationCode.CUSTOMER_MISMATCH.value,
            "retrieve_precedents customer_id must match the opened payment-bound remittance",
        )
    payment = _current_payment(connection, context)
    if isinstance(payment, ToolResult):
        return payment
    snapshot = context.memory_snapshot
    retrieved, notes = _select_retrieved_precedents(
        snapshot, context.company_id, workspace.policy_hash, args.customer_id, payment
    )
    context.retrieve_eligibility = notes
    for item in retrieved:
        if item.precedent_id not in context.retrieved_precedent_version_ids:
            context.retrieved_precedent_version_ids.append(item.precedent_id)
    page = RetrievePrecedentsPage(hints=retrieved)
    return _ok(page.model_dump(mode="json"), [item.precedent_id for item in retrieved])


def _handle_validate_resolution(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    try:
        record = store_proposal(connection, context, payload)
    except PersistenceError as exc:
        return _error(ErrorCode.INVALID_TOOL_ARGUMENTS.value, str(exc)[:1000])
    view = ValidateResolutionView(
        proposal_id=record.proposal_id,
        valid=record.validation_report.valid,
        validation_report=record.validation_report.model_dump(mode="json"),
    )
    return _ok(view.model_dump(mode="json"), list(record.payload.evidence_document_ids))


def _handle_submit_resolution(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    args = SubmitResolutionArgs.model_validate(payload)
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    stored = get_proposal(connection, args.proposal_id)
    if stored is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"proposal {args.proposal_id} was not found",
        )
    if stored.workspace_id != context.workspace_id:
        return _error(
            ValidationCode.WRONG_WORKSPACE.value,
            "the proposal does not belong to this workspace",
        )
    if stored.run_id != context.run_id or stored.case_id != context.case_id:
        return _error(
            ValidationCode.NOT_FOUND.value,
            "the proposal does not belong to this run and case",
        )
    result = apply_proposal(
        connection,
        context,
        args.proposal_id,
        idempotency_key=_submit_idempotency_key(args.proposal_id),
        actor=context.actor,
    )
    source_ids = list(stored.payload.evidence_document_ids)
    if result.status is ApplicationStatus.REJECTED:
        code = result.issues[0].code.value if result.issues else ValidationCode.NOT_FOUND.value
        message = (
            result.issues[0].message
            if result.issues
            else "the stored proposal could not be applied"
        )
        return ToolResult(
            ok=False,
            data=result.model_dump(mode="json"),
            error=ToolError(code=code, message=message[:1000]),
            source_ids=source_ids,
        )
    return ToolResult(
        ok=True,
        data=result.model_dump(mode="json"),
        error=None,
        source_ids=source_ids,
    )


def _handle_request_review(
    connection: sqlite3.Connection, context: RunContext, payload: dict[str, Any]
) -> ToolResult:
    request = ReviewRequest.model_validate(payload)
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    case = get_case(connection, context.workspace_id, context.case_id)
    if case is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"case {context.case_id} was not found in this workspace",
        )
    if case.state is CaseState.RESOLVED:
        return _error(
            ValidationCode.PAYMENT_ALREADY_APPLIED.value,
            "a resolved case cannot be sent to review through this tool",
        )
    if request.reason_code in _NON_BUSINESS_REVIEW_CODES:
        return _error(
            ErrorCode.INVALID_TOOL_ARGUMENTS.value,
            f"{request.reason_code.value} cannot satisfy a genuine business review",
        )
    for document_id in request.evidence_document_ids:
        document = get_document(connection, context.workspace_id, document_id)
        if document is None:
            return _error(
                ValidationCode.NOT_FOUND.value,
                f"review evidence {document_id} was not found in this workspace",
            )
    for invoice_id in request.candidate_invoice_ids:
        invoice = get_invoice(connection, context.workspace_id, invoice_id)
        if invoice is None:
            return _error(
                ValidationCode.NOT_FOUND.value,
                f"review candidate invoice {invoice_id} was not found in this workspace",
            )
    finished_at = utc_now_iso()
    connection.execute(
        """
        UPDATE cases
        SET state = ?, current_run_id = NULL, latest_run_id = ?
        WHERE workspace_id = ? AND case_id = ?
        """,
        (
            CaseState.NEEDS_REVIEW.value,
            context.run_id,
            context.workspace_id,
            context.case_id,
        ),
    )
    connection.execute(
        """
        UPDATE runs
        SET state = ?, finished_at = ?, terminal_result = ?
        WHERE run_id = ?
        """,
        (RunState.COMPLETE.value, finished_at, AgentOutcome.REVIEW.value, context.run_id),
    )
    return _ok(request.model_dump(mode="json"), list(request.evidence_document_ids))


_HANDLERS: dict[str, Handler] = {
    "get_case": _handle_get_case,
    "search_invoices": _handle_search_invoices,
    "search_documents": _handle_search_documents,
    "read_document": _handle_read_document,
    "retrieve_precedents": _handle_retrieve_precedents,
    "validate_resolution": _handle_validate_resolution,
    "submit_resolution": _handle_submit_resolution,
    "request_review": _handle_request_review,
}


def _record_tool_outcome(
    connection: sqlite3.Connection,
    context: RunContext,
    *,
    name: str,
    call_id: str,
    arguments: dict[str, Any],
    result: ToolResult,
    duration_ms: int,
) -> None:
    payload = {
        "arguments": _safe_value(arguments),
        "call_id": call_id,
        "duration_ms": duration_ms,
        "source_ids": list(result.source_ids),
        "status": "ok" if result.ok else "error",
        "summary": _result_summary(name, result),
        "tool": name,
    }
    if result.error is not None:
        payload["error_code"] = result.error.code
        payload["error_message"] = result.error.message[:RESULT_SUMMARY_MAX]
    kind = RunEventKind.TOOL_SUCCEEDED if result.ok else RunEventKind.TOOL_FAILED
    _append_event(connection, context.run_id, kind, payload)
    if name == "validate_resolution" and result.ok and isinstance(result.data, dict):
        report_kind = (
            RunEventKind.PROPOSAL_VALIDATED
            if result.data.get("valid")
            else RunEventKind.PROPOSAL_REJECTED
        )
        _append_event(
            connection,
            context.run_id,
            report_kind,
            {
                "call_id": call_id,
                "proposal_id": result.data.get("proposal_id"),
                "valid": result.data.get("valid"),
            },
        )
    if name == "retrieve_precedents" and result.ok:
        _append_event(
            connection,
            context.run_id,
            RunEventKind.PRECEDENT_RETRIEVED,
            {
                "call_id": call_id,
                "considered_precedent_ids": [
                    item.precedent_id for item in context.memory_snapshot.hints
                ],
                "excluded": [
                    note.model_dump(mode="json")
                    for note in context.retrieve_eligibility
                    if not note.included
                ],
                "precedent_ids": list(result.source_ids),
            },
        )
    if name == "request_review" and result.ok:
        _append_event(
            connection,
            context.run_id,
            RunEventKind.REVIEW_REQUESTED,
            {
                "call_id": call_id,
                "reason_code": (
                    result.data.get("reason_code") if isinstance(result.data, dict) else None
                ),
                "source_ids": list(result.source_ids),
            },
        )
        _append_finished(connection, context.run_id, AgentOutcome.REVIEW)
    if (
        name == "submit_resolution"
        and result.ok
        and isinstance(result.data, dict)
        and result.data.get("status") in {ApplicationStatus.APPLIED.value, "APPLIED"}
    ):
        _append_finished(connection, context.run_id, AgentOutcome.RESOLVED)


def record_run_event(
    connection: sqlite3.Connection,
    run_id: str,
    kind: RunEventKind,
    payload: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> None:
    """Append one redacted trace event. Used by the Session 09 loop."""
    _append_event(connection, run_id, kind, payload, created_at=created_at)


def record_run_finished(connection: sqlite3.Connection, run_id: str, outcome: AgentOutcome) -> None:
    """Record ``run_finished`` once for a terminal outcome."""
    _append_finished(connection, run_id, outcome)


def _append_finished(connection: sqlite3.Connection, run_id: str, outcome: AgentOutcome) -> None:
    events = list_run_events(connection, run_id)
    if any(event.event_kind is RunEventKind.RUN_FINISHED for event in events):
        return
    _append_event(
        connection,
        run_id,
        RunEventKind.RUN_FINISHED,
        {"terminal_outcome": outcome.value},
    )


def _append_event(
    connection: sqlite3.Connection,
    run_id: str,
    kind: RunEventKind,
    payload: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> None:
    insert_run_event(
        connection,
        RunEvent(
            run_id=run_id,
            sequence=next_run_event_sequence(connection, run_id),
            event_kind=kind,
            payload=dict(_safe_value(payload)),
            created_at=created_at or utc_now_iso(),
        ),
    )


def _require_workspace(connection: sqlite3.Connection, context: RunContext):
    workspace = get_workspace(connection, context.workspace_id)
    if workspace is None or workspace.company_id != context.company_id:
        return _error(
            ValidationCode.WRONG_WORKSPACE.value,
            "the bound workspace is not available to this run",
        )
    return workspace


def _current_payment(connection: sqlite3.Connection, context: RunContext):
    case = get_case(connection, context.workspace_id, context.case_id)
    if case is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"case {context.case_id} was not found in this workspace",
        )
    payment = get_payment(connection, context.workspace_id, case.payment_id)
    if payment is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"payment {case.payment_id} was not found in this workspace",
        )
    return payment


def _established_customer(connection: sqlite3.Connection, context: RunContext):
    payment = _current_payment(connection, context)
    if isinstance(payment, ToolResult):
        return payment
    remittances = _payment_remittances(list_documents(connection, context.workspace_id), payment)
    if _remittances_conflict(remittances, payment):
        return _error(
            ValidationCode.CONFLICTING_EVIDENCE.value,
            "payment-bound remittances disagree about customer, account, or currency",
        )
    opened = {item.document_id for item in context.opened_documents}
    opened_remittances = [document for document in remittances if document.document_id in opened]
    if not opened_remittances:
        return _error(
            ErrorCode.CUSTOMER_NOT_ESTABLISHED.value,
            "open a payment-bound remittance before retrieving precedents",
        )
    customers = {
        document.facts.customer_id
        for document in opened_remittances
        if isinstance(document.facts, RemittanceFacts)
    }
    if len(customers) != 1:
        return _error(
            ValidationCode.CONFLICTING_EVIDENCE.value,
            "opened remittances do not establish a single customer",
        )
    return next(iter(customers))


def _payment_remittances(documents: Sequence[Document], payment: PaymentRecord) -> list[Document]:
    return [
        document
        for document in documents
        if document.kind is DocumentKind.REMITTANCE
        and isinstance(document.facts, RemittanceFacts)
        and document.facts.bank_reference == payment.bank_reference
    ]


def _remittances_conflict(remittances: Sequence[Document], payment: PaymentRecord) -> bool:
    comparable = [
        document.facts for document in remittances if isinstance(document.facts, RemittanceFacts)
    ]
    if not comparable:
        return False
    first = comparable[0]
    for facts in comparable:
        if (
            facts.customer_id != first.customer_id
            or facts.receiving_account_id != first.receiving_account_id
            or facts.currency != first.currency
            or facts.receiving_account_id != payment.bank_account_id
            or facts.currency != payment.currency
        ):
            return True
    return False


def _hint_in_scope(hint: WireFeeLookupHint, customer_id: str, payment: PaymentRecord) -> bool:
    return _scope_mismatch_reason(hint, customer_id, payment) is None


def _scope_mismatch_reason(
    hint: WireFeeLookupHint, customer_id: str, payment: PaymentRecord
) -> str | None:
    scope = hint.scope
    if scope.customer_id != customer_id:
        return "customer_id does not match lesson scope"
    if scope.bank_account_id != payment.bank_account_id:
        return "bank_account_id does not match lesson scope"
    if scope.currency != payment.currency:
        return "currency does not match lesson scope"
    if scope.channel != payment.channel:
        return "channel does not match lesson scope"
    return None


def _select_retrieved_precedents(
    snapshot: MemorySnapshot,
    company_id: str,
    policy_hash: str,
    customer_id: str,
    payment: PaymentRecord,
) -> tuple[list[RetrievedPrecedent], list[MemoryEligibilityNote]]:
    notes: list[MemoryEligibilityNote] = []
    if snapshot.company_id is not None and snapshot.company_id != company_id:
        reason = "frozen snapshot company does not match this workspace"
        for item in snapshot.hints:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=item.precedent_id,
                    version=item.version,
                    included=False,
                    reason=reason,
                )
            )
        return [], notes
    if snapshot.policy_hash is not None and snapshot.policy_hash != policy_hash:
        reason = "frozen snapshot policy is incompatible with this workspace"
        for item in snapshot.hints:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=item.precedent_id,
                    version=item.version,
                    included=False,
                    reason=reason,
                )
            )
        return [], notes
    eligible: list[MemoryHintRef] = []
    for item in snapshot.hints:
        if item.hint is None:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=item.precedent_id,
                    version=item.version,
                    included=False,
                    reason="hint payload is unavailable",
                )
            )
            continue
        reason = _scope_mismatch_reason(item.hint, customer_id, payment)
        if reason is not None:
            notes.append(
                MemoryEligibilityNote(
                    precedent_id=item.precedent_id,
                    version=item.version,
                    included=False,
                    reason=reason,
                )
            )
            continue
        eligible.append(item)
    eligible = sorted(eligible, key=lambda item: (item.precedent_id, item.version))
    overflow = eligible[RETRIEVE_LIMIT:]
    kept = eligible[:RETRIEVE_LIMIT]
    for item in overflow:
        notes.append(
            MemoryEligibilityNote(
                precedent_id=item.precedent_id,
                version=item.version,
                included=False,
                reason="retrieve limit is 3",
            )
        )
    retrieved: list[RetrievedPrecedent] = []
    for item in kept:
        if item.hint is None:
            continue
        notes.append(
            MemoryEligibilityNote(
                precedent_id=item.precedent_id,
                version=item.version,
                included=True,
                reason="in-scope investigation hint; current-case evidence is still required",
            )
        )
        retrieved.append(
            RetrievedPrecedent(
                precedent_id=item.precedent_id,
                version=item.version,
                payload_hash=item.payload_hash,
                hint=item.hint,
            )
        )
    return retrieved, notes


def _payment_view(payment: PaymentRecord) -> PaymentCaseView:
    return PaymentCaseView(
        payment_id=payment.payment_id,
        bank_transaction_id=payment.bank_transaction_id,
        bank_account_id=payment.bank_account_id,
        posted_date=payment.posted_date,
        currency=payment.currency,
        amount_cents=payment.amount_cents,
        channel=payment.channel,
        payer_text=payment.payer_text,
        bank_reference=payment.bank_reference,
        applied=payment.applied,
        state=payment.state,
    )


def _parse_args(
    model: type[StrictModel], payload: dict[str, Any], label: str
) -> StrictModel | ToolResult:
    try:
        return model.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        message = f"invalid {label} arguments"
        if isinstance(exc, ValidationError) and exc.errors():
            detail = str(exc.errors()[0].get("msg", "")).strip()
            if detail:
                message = f"{message}: {detail}"
        elif str(exc):
            message = f"{message}: {exc}"
        return _error(ErrorCode.INVALID_TOOL_ARGUMENTS.value, message[:1000])


_ID_KEYS = frozenset(
    {
        "proposal_id",
        "document_id",
        "customer_id",
        "invoice_id",
        "payment_id",
        "workspace_id",
        "case_id",
        "run_id",
        "reference",
        "path",
        "db_path",
    }
)


def _path_like_arguments_error(payload: Mapping[str, Any]) -> ToolResult | None:
    for value in _walk_identifier_strings(payload):
        error = _path_like_error(value)
        if error is not None:
            return error
    return None


def _walk_identifier_strings(value: Any, key: str | None = None) -> list[str]:
    if isinstance(value, str):
        if key is not None and (key in _ID_KEYS or key.endswith("_id") or key.endswith("_ids")):
            return [value]
        return []
    if isinstance(value, Mapping):
        found: list[str] = []
        for nested_key, item in value.items():
            found.extend(_walk_identifier_strings(item, str(nested_key)))
        return found
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_walk_identifier_strings(item, key))
        return found
    return []


def _path_like_error(value: str) -> ToolResult | None:
    normalized = value.replace("\\", "/").lower()
    if "/" in value or "\\" in value or ".." in value:
        return _error(
            ErrorCode.INVALID_TOOL_ARGUMENTS.value,
            "tool identifiers cannot be filesystem or workspace paths",
        )
    parts = [part for part in normalized.split("/") if part]
    if "grading" in parts:
        return _error(
            ErrorCode.INVALID_TOOL_ARGUMENTS.value,
            "grading files are not readable through runtime tools",
        )
    return None


def _submit_idempotency_key(proposal_id: str) -> str:
    return f"submit:{proposal_id}"[:96]


def _result_summary(name: str, result: ToolResult) -> str:
    if not result.ok and result.error is not None:
        return f"{name} failed ({result.error.code})"[:RESULT_SUMMARY_MAX]
    if name == "read_document" and isinstance(result.data, dict):
        return f"opened {result.data.get('document_id', '')}"[:RESULT_SUMMARY_MAX]
    if name == "validate_resolution" and isinstance(result.data, dict):
        status = "valid" if result.data.get("valid") else "rejected"
        return f"proposal {result.data.get('proposal_id', '')} {status}"[:RESULT_SUMMARY_MAX]
    if name == "submit_resolution" and isinstance(result.data, dict):
        return f"application {result.data.get('status', '')}"[:RESULT_SUMMARY_MAX]
    if result.source_ids:
        return f"{name} sources {', '.join(result.source_ids[:5])}"[:RESULT_SUMMARY_MAX]
    return f"{name} ok"[:RESULT_SUMMARY_MAX]


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                safe[str(key)] = "[redacted]"
            else:
                safe[str(key)] = _safe_value(item)
        return safe
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith("sk-ant-") or "ANTHROPIC_API_KEY" in value:
            return "[redacted]"
        if len(value) > TRACE_STRING_MAX:
            return value[:TRACE_STRING_MAX] + "..."
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return canonical_json(value)[:TRACE_STRING_MAX]


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SECRET_KEYS or lowered.endswith("_api_key")


def _ok(data: Any, source_ids: list[str]) -> ToolResult:
    return ToolResult(ok=True, data=data, error=None, source_ids=source_ids)


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        data=None,
        error=ToolError(code=code, message=message),
        source_ids=[],
    )


__all__ = [
    "CaseToolView",
    "GetCaseArgs",
    "PROMPT_PATH",
    "PaymentCaseView",
    "RetrievePrecedentsArgs",
    "RetrievePrecedentsPage",
    "RetrievedPrecedent",
    "SubmitResolutionArgs",
    "TERMINAL_TOOLS",
    "TOOL_ARGUMENT_MODELS",
    "TOOL_NAMES",
    "ValidateResolutionView",
    "dispatch_tool",
    "empty_memory_snapshot",
    "freeze_memory_snapshot",
    "initial_user_message",
    "investigator_prompt_hash",
    "investigator_prompt_text",
    "record_run_event",
    "record_run_finished",
    "start_run",
    "tool_definitions",
    "tool_schema_hash",
]
