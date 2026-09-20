"""Deterministic proposal validation and atomic sandbox application.

``validate_proposal`` inspects current workspace state and source facts. It
does not invent a decision or consult an LLM. ``apply_proposal`` revalidates
inside one ``BEGIN IMMEDIATE`` transaction, applies money at most once per
idempotency key, and rolls back every write on failure.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import ValidationError

from precedent.db import (
    PersistenceError,
    get_application_by_idempotency_key,
    get_application_by_payment,
    get_case,
    get_customer,
    get_document,
    get_invoice,
    get_payment,
    get_proposal,
    get_run,
    get_workspace,
    insert_proposal,
    insert_run_event,
    list_documents,
    list_invoices,
    next_run_event_sequence,
    transaction,
)
from precedent.models import (
    AgentOutcome,
    ApplicationRecord,
    ApplicationResult,
    ApplicationStatus,
    BankFeeNoticeFacts,
    CaseSnapshot,
    CaseState,
    CaseSummary,
    CompanyPolicy,
    DisputeNoticeFacts,
    DisputeStatus,
    Document,
    DocumentKind,
    InvoiceOutstanding,
    InvoiceRecord,
    InvoiceSourceStatus,
    PaymentChannel,
    PaymentRecord,
    ProposalRecord,
    RemittanceFacts,
    ResolutionProposal,
    ResolutionType,
    ResultingBalances,
    RunContext,
    RunEvent,
    RunEventKind,
    RunState,
    ValidationCode,
    ValidationIssue,
    ValidationReport,
    WorkspaceRecord,
    hash_model,
    utc_now_iso,
)
from precedent.policies import SUPPORTED_FEE_TYPE

Repo = sqlite3.Connection

_CODE_RANK = {code: index for index, code in enumerate(ValidationCode)}


def validate_proposal(
    repo: Repo,
    context: RunContext,
    proposal: ResolutionProposal | dict[str, Any] | object,
) -> ValidationReport:
    """Return a validation report without mutating financial state.

    Invalid reports never include a commit-ready normalized proposal. A
    self-reported ``evidence_verified`` flag is extra input and cannot prove
    evidence.
    """
    parsed, parse_issues = _parse_proposal(proposal)
    workspace = get_workspace(repo, context.workspace_id)
    issues = list(parse_issues)
    evidence_hashes: list[str] = []

    if workspace is None:
        issues.append(
            _issue(
                ValidationCode.WRONG_WORKSPACE,
                f"Workspace {context.workspace_id} was not found.",
                context.workspace_id,
            )
        )
        return _report(
            issues=issues,
            normalized=None,
            context=context,
            workspace=None,
            evidence_hashes=[],
        )

    if context.company_id != workspace.company_id:
        issues.append(
            _issue(
                ValidationCode.WRONG_WORKSPACE,
                "The run context is bound to a different company than this workspace.",
                context.workspace_id,
                context.company_id,
                workspace.company_id,
            )
        )
    if context.policy_id != workspace.policy.policy_id:
        issues.append(
            _issue(
                ValidationCode.WRONG_WORKSPACE,
                "The run context policy does not match the workspace company policy.",
                context.policy_id,
                workspace.policy.policy_id,
            )
        )
    if context.initial_ledger_revision != workspace.ledger_revision:
        issues.append(
            _issue(
                ValidationCode.STALE_LEDGER,
                "The proposal was checked against a stale ledger revision; revalidate "
                "against the current workspace state.",
                context.workspace_id,
            )
        )

    if parsed is None:
        return _report(
            issues=issues,
            normalized=None,
            context=context,
            workspace=workspace,
            evidence_hashes=[],
        )

    issues.extend(_collect_business_issues(repo, context, workspace, parsed))
    evidence_hashes = _evidence_hashes(repo, context.workspace_id, parsed.evidence_document_ids)
    unique_issues = _unique_sorted(issues)
    normalized = parsed if not unique_issues else None
    return _report(
        issues=unique_issues,
        normalized=normalized,
        context=context,
        workspace=workspace,
        evidence_hashes=evidence_hashes,
    )


def store_proposal(
    repo: Repo,
    context: RunContext,
    proposal: ResolutionProposal | dict[str, Any] | object,
) -> ProposalRecord:
    """Persist an immutable proposal and its current validation report.

    Schema-invalid input cannot be stored. Business-invalid but schema-valid
    proposals are stored so ``apply_proposal`` can revalidate them later.
    This function does not mutate invoice or payment balances.
    """
    parsed, _parse_issues = _parse_proposal(proposal)
    if parsed is None:
        raise PersistenceError("cannot store a proposal that fails schema validation")
    case = get_case(repo, context.workspace_id, context.case_id)
    if case is None:
        raise PersistenceError(
            f"case {context.case_id} was not found in workspace {context.workspace_id}"
        )
    run = get_run(repo, context.run_id)
    if run is None:
        raise PersistenceError(f"run {context.run_id} was not found")
    if run.workspace_id != context.workspace_id or run.case_id != context.case_id:
        raise PersistenceError("run context does not match the stored run")
    report = validate_proposal(repo, context, parsed)
    record = ProposalRecord(
        proposal_id=_new_id("PRP"),
        run_id=context.run_id,
        workspace_id=context.workspace_id,
        case_id=context.case_id,
        payload=parsed,
        payload_hash=hash_model(parsed),
        validation_report=report,
        checked_revision=report.checked_ledger_revision,
        created_at=utc_now_iso(),
    )

    def _write() -> None:
        insert_proposal(repo, record)

    if repo.in_transaction:
        _write()
    else:
        with transaction(repo, immediate=True):
            _write()
    return record


def apply_proposal(
    repo: Repo,
    context: RunContext,
    proposal_id: str,
    *,
    idempotency_key: str,
    actor: str,
) -> ApplicationResult:
    """Revalidate and apply a stored proposal inside one immediate transaction."""
    if repo.in_transaction:
        return _apply_proposal_body(
            repo, context, proposal_id, idempotency_key=idempotency_key, actor=actor
        )
    with transaction(repo, immediate=True):
        return _apply_proposal_body(
            repo, context, proposal_id, idempotency_key=idempotency_key, actor=actor
        )


def get_case_snapshot(repo: Repo, workspace_id: str, case_id: str) -> CaseSnapshot:
    """Return the current sandbox case, payment, policy, and related invoices."""
    workspace = get_workspace(repo, workspace_id)
    case = get_case(repo, workspace_id, case_id)
    if workspace is None:
        raise PersistenceError(f"workspace {workspace_id} was not found")
    if case is None:
        raise PersistenceError(f"case {case_id} was not found in workspace {workspace_id}")
    payment = get_payment(repo, workspace_id, case.payment_id)
    if payment is None:
        raise PersistenceError(
            f"payment {case.payment_id} was not found in workspace {workspace_id}"
        )
    return CaseSnapshot(
        summary=CaseSummary(
            case_id=case.case_id,
            payment_id=payment.payment_id,
            payer_text=payment.payer_text,
            amount_cents=payment.amount_cents,
            currency=payment.currency,
            posted_date=payment.posted_date,
            case_state=case.state,
            latest_run_id=case.latest_run_id,
            latest_summary=None,
        ),
        payment=payment,
        invoices=_snapshot_invoices(repo, workspace_id, payment),
        policy=workspace.policy,
        ledger_revision=workspace.ledger_revision,
        dataset_hash=workspace.dataset_hash,
    )


def _apply_proposal_body(
    repo: Repo,
    context: RunContext,
    proposal_id: str,
    *,
    idempotency_key: str,
    actor: str,
) -> ApplicationResult:
    workspace = get_workspace(repo, context.workspace_id)
    key_issue = _idempotency_input_issue(idempotency_key, actor)
    if key_issue is not None:
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=None,
            invoice_ids=(),
            issues=[key_issue],
        )

    stored = get_proposal(repo, proposal_id)
    if stored is None:
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=None,
            invoice_ids=(),
            issues=[
                _issue(
                    ValidationCode.NOT_FOUND,
                    f"Proposal {proposal_id} was not found.",
                    proposal_id,
                )
            ],
        )
    if stored.workspace_id != context.workspace_id:
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=stored.payload.payment_id,
            invoice_ids=[item.invoice_id for item in stored.payload.allocations],
            issues=[
                _issue(
                    ValidationCode.WRONG_WORKSPACE,
                    "The proposal does not belong to this workspace.",
                    proposal_id,
                    context.workspace_id,
                    stored.workspace_id,
                )
            ],
        )
    if stored.case_id != context.case_id or stored.run_id != context.run_id:
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=stored.payload.payment_id,
            invoice_ids=[item.invoice_id for item in stored.payload.allocations],
            issues=[
                _issue(
                    ValidationCode.NOT_FOUND,
                    "The proposal does not belong to this run and case.",
                    proposal_id,
                )
            ],
        )

    payload = stored.payload
    payload_hash = hash_model(payload)
    invoice_ids = [item.invoice_id for item in payload.allocations]
    existing_key = get_application_by_idempotency_key(repo, context.workspace_id, idempotency_key)
    if existing_key is not None:
        if existing_key.payload_hash == payload_hash:
            return _replayed(repo, context, existing_key, workspace=workspace)
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=payload.payment_id,
            invoice_ids=invoice_ids,
            issues=[
                _issue(
                    ValidationCode.IDEMPOTENCY_CONFLICT,
                    "The idempotency key was already used with a different proposal payload.",
                    payload.payment_id,
                    existing_key.application_id,
                )
            ],
        )

    existing_payment = get_application_by_payment(repo, context.workspace_id, payload.payment_id)
    payment = get_payment(repo, context.workspace_id, payload.payment_id)
    if existing_payment is not None or (payment is not None and payment.applied):
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=payload.payment_id,
            invoice_ids=invoice_ids,
            issues=[
                _issue(
                    ValidationCode.PAYMENT_ALREADY_APPLIED,
                    f"Payment {payload.payment_id} has already been applied.",
                    payload.payment_id,
                )
            ],
        )

    report = validate_proposal(repo, context, payload)
    if not report.valid or report.normalized_proposal is None:
        return _rejected(
            repo,
            context=context,
            workspace=workspace,
            proposal_id=proposal_id,
            payment_id=payload.payment_id,
            invoice_ids=invoice_ids,
            issues=report.issues,
        )

    normalized = report.normalized_proposal
    application_id = _new_id("APP")
    created_at = utc_now_iso()
    _insert_application_header(
        repo,
        ApplicationRecord(
            application_id=application_id,
            workspace_id=context.workspace_id,
            payment_id=normalized.payment_id,
            proposal_id=proposal_id,
            seeded=False,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            actor=actor,
            allocations=list(normalized.allocations),
            created_at=created_at,
        ),
    )
    for index, allocation in enumerate(normalized.allocations):
        repo.execute(
            """
            INSERT INTO allocations (
              workspace_id, application_id, invoice_id, cash_cents, fee_cents
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                context.workspace_id,
                application_id,
                allocation.invoice_id,
                allocation.cash_cents,
                allocation.fee_cents,
            ),
        )
        invoice = get_invoice(repo, context.workspace_id, allocation.invoice_id)
        if invoice is None:
            raise PersistenceError(f"invoice {allocation.invoice_id} disappeared during apply")
        new_outstanding = invoice.outstanding_cents - allocation.cash_cents - allocation.fee_cents
        if new_outstanding < 0:
            raise PersistenceError(
                f"invoice {allocation.invoice_id} would have a negative outstanding balance"
            )
        repo.execute(
            """
            UPDATE invoices
            SET outstanding_cents = ?
            WHERE workspace_id = ? AND invoice_id = ?
            """,
            (new_outstanding, context.workspace_id, allocation.invoice_id),
        )
        _after_allocation_write(index)

    updated_payment = repo.execute(
        """
        UPDATE payments SET applied = 1
        WHERE workspace_id = ? AND payment_id = ? AND applied = 0
        """,
        (context.workspace_id, normalized.payment_id),
    )
    if updated_payment.rowcount != 1:
        raise PersistenceError(f"payment {normalized.payment_id} could not be marked applied")
    repo.execute(
        """
        UPDATE workspaces SET ledger_revision = ledger_revision + 1
        WHERE workspace_id = ?
        """,
        (context.workspace_id,),
    )
    repo.execute(
        """
        UPDATE cases
        SET state = ?, current_run_id = NULL, latest_run_id = ?
        WHERE workspace_id = ? AND case_id = ?
        """,
        (CaseState.RESOLVED.value, context.run_id, context.workspace_id, context.case_id),
    )
    repo.execute(
        """
        UPDATE runs
        SET state = ?, finished_at = ?, terminal_result = ?
        WHERE run_id = ?
        """,
        (RunState.COMPLETE.value, created_at, AgentOutcome.RESOLVED.value, context.run_id),
    )
    insert_run_event(
        repo,
        RunEvent(
            run_id=context.run_id,
            sequence=next_run_event_sequence(repo, context.run_id),
            event_kind=RunEventKind.APPLICATION_COMMITTED,
            payload={
                "application_id": application_id,
                "proposal_id": proposal_id,
                "payment_id": normalized.payment_id,
            },
            created_at=created_at,
        ),
    )
    committed = get_workspace(repo, context.workspace_id)
    revision = 0 if committed is None else committed.ledger_revision
    return ApplicationResult(
        status=ApplicationStatus.APPLIED,
        application_id=application_id,
        proposal_id=proposal_id,
        issues=[],
        resulting_balances=_resulting_balances(
            repo,
            context.workspace_id,
            normalized.payment_id,
            [item.invoice_id for item in normalized.allocations],
        ),
        ledger_revision=revision,
    )


def _insert_application_header(repo: Repo, record: ApplicationRecord) -> None:
    try:
        repo.execute(
            """
            INSERT INTO applications (
              workspace_id, application_id, payment_id, proposal_id, seeded,
              idempotency_key, payload_hash, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.workspace_id,
                record.application_id,
                record.payment_id,
                record.proposal_id,
                int(record.seeded),
                record.idempotency_key,
                record.payload_hash,
                record.actor,
                record.created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise PersistenceError(f"insert application violated a storage constraint: {exc}") from exc


def _after_allocation_write(index: int) -> None:
    """Test seam invoked after each allocation row and invoice decrement."""
    del index


def _idempotency_input_issue(idempotency_key: str, actor: str) -> ValidationIssue | None:
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 96:
        return _issue(
            ValidationCode.INVALID_SCHEMA,
            "idempotency_key must be a string of 1 to 96 characters.",
        )
    if not isinstance(actor, str) or not 1 <= len(actor) <= 80:
        return _issue(
            ValidationCode.INVALID_SCHEMA,
            "actor must be a string of 1 to 80 characters.",
        )
    return None


def _replayed(
    repo: Repo,
    context: RunContext,
    existing: ApplicationRecord,
    *,
    workspace: WorkspaceRecord | None,
) -> ApplicationResult:
    revision = (
        workspace.ledger_revision if workspace is not None else context.initial_ledger_revision
    )
    return ApplicationResult(
        status=ApplicationStatus.REPLAYED,
        application_id=existing.application_id,
        proposal_id=existing.proposal_id,
        issues=[],
        resulting_balances=_resulting_balances(
            repo,
            context.workspace_id,
            existing.payment_id,
            [item.invoice_id for item in existing.allocations],
        ),
        ledger_revision=revision,
    )


def _rejected(
    repo: Repo,
    *,
    context: RunContext,
    workspace: WorkspaceRecord | None,
    proposal_id: str,
    payment_id: str | None,
    invoice_ids: Sequence[str],
    issues: Sequence[ValidationIssue],
) -> ApplicationResult:
    revision = (
        workspace.ledger_revision if workspace is not None else context.initial_ledger_revision
    )
    balances = None
    if payment_id is not None:
        balances = _resulting_balances(repo, context.workspace_id, payment_id, invoice_ids)
    return ApplicationResult(
        status=ApplicationStatus.REJECTED,
        application_id=None,
        proposal_id=proposal_id,
        issues=_unique_sorted(issues),
        resulting_balances=balances,
        ledger_revision=revision,
    )


def _resulting_balances(
    repo: Repo,
    workspace_id: str,
    payment_id: str,
    invoice_ids: Sequence[str],
) -> ResultingBalances | None:
    payment = get_payment(repo, workspace_id, payment_id)
    if payment is None:
        return None
    invoices: list[InvoiceOutstanding] = []
    for invoice_id in invoice_ids:
        invoice = get_invoice(repo, workspace_id, invoice_id)
        if invoice is None:
            continue
        invoices.append(
            InvoiceOutstanding(
                invoice_id=invoice.invoice_id,
                outstanding_cents=invoice.outstanding_cents,
            )
        )
    return ResultingBalances(
        payment_id=payment.payment_id,
        payment_applied=payment.applied,
        invoices=invoices,
    )


def _snapshot_invoices(
    repo: Repo, workspace_id: str, payment: PaymentRecord
) -> list[InvoiceRecord]:
    documents = list_documents(repo, workspace_id)
    customer_ids = {
        document.facts.customer_id
        for document in _payment_remittances(documents, payment)
        if isinstance(document.facts, RemittanceFacts)
    }
    allocated_ids: set[str] = set()
    existing = get_application_by_payment(repo, workspace_id, payment.payment_id)
    if existing is not None:
        allocated_ids = {item.invoice_id for item in existing.allocations}
    selected = [
        invoice
        for invoice in list_invoices(repo, workspace_id)
        if invoice.customer_id in customer_ids or invoice.invoice_id in allocated_ids
    ]
    return selected


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _parse_proposal(
    proposal: ResolutionProposal | dict[str, Any] | object,
) -> tuple[ResolutionProposal | None, list[ValidationIssue]]:
    if isinstance(proposal, ResolutionProposal):
        return proposal, []
    if isinstance(proposal, dict):
        duplicate_ids = _duplicate_allocation_ids(proposal.get("allocations"))
        if duplicate_ids:
            return None, [
                _issue(
                    ValidationCode.DUPLICATE_INVOICE,
                    "A proposal cannot allocate the same invoice more than once.",
                    *duplicate_ids,
                )
            ]
        try:
            return ResolutionProposal.model_validate(proposal), []
        except ValidationError:
            extra = sorted(
                str(key) for key in proposal if key not in ResolutionProposal.model_fields
            )
            if extra:
                message = (
                    "The resolution proposal contains unsupported fields: "
                    + ", ".join(extra)
                    + ". Extra flags such as evidence_verified are not proof."
                )
            else:
                message = (
                    "The resolution proposal does not match the required schema. "
                    "Amounts must be positive integer cents."
                )
            return None, [_issue(ValidationCode.INVALID_SCHEMA, message)]
    return None, [
        _issue(
            ValidationCode.INVALID_SCHEMA,
            "The resolution proposal does not match the required schema.",
        )
    ]


def _collect_business_issues(
    repo: Repo,
    context: RunContext,
    workspace: WorkspaceRecord,
    proposal: ResolutionProposal,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    policy = workspace.policy
    payment = get_payment(repo, workspace.workspace_id, proposal.payment_id)
    invoices_by_id: dict[str, InvoiceRecord] = {}
    missing_invoices: list[str] = []
    for allocation in proposal.allocations:
        invoice = get_invoice(repo, workspace.workspace_id, allocation.invoice_id)
        if invoice is None:
            missing_invoices.append(allocation.invoice_id)
        else:
            invoices_by_id[allocation.invoice_id] = invoice

    customer = get_customer(repo, workspace.workspace_id, proposal.customer_id)
    if customer is None:
        issues.append(
            _issue(
                ValidationCode.NOT_FOUND,
                f"Customer {proposal.customer_id} was not found in this workspace.",
                proposal.customer_id,
            )
        )

    if payment is None:
        issues.append(
            _issue(
                ValidationCode.NOT_FOUND,
                f"Payment {proposal.payment_id} was not found in this workspace.",
                proposal.payment_id,
            )
        )
    else:
        issues.extend(_payment_issues(payment, workspace, policy, proposal))

    for invoice_id in missing_invoices:
        issues.append(
            _issue(
                ValidationCode.NOT_FOUND,
                f"Invoice {invoice_id} was not found in this workspace.",
                invoice_id,
            )
        )

    selected_invoices = [
        invoices_by_id[item.invoice_id]
        for item in proposal.allocations
        if item.invoice_id in invoices_by_id
    ]
    for invoice in selected_invoices:
        issues.extend(_invoice_issues(invoice, proposal.customer_id, policy, payment))

    documents = list_documents(repo, workspace.workspace_id)
    cited_docs, evidence_issues = _evidence_issues(repo, context, workspace, proposal, documents)
    issues.extend(evidence_issues)
    issues.extend(_precedent_issues(context, proposal))

    if payment is None:
        return issues

    remittances = _payment_remittances(documents, payment)
    if _remittances_conflict(remittances):
        issues.append(
            _issue(
                ValidationCode.CONFLICTING_EVIDENCE,
                "Payment-bound remittances disagree about customer, invoices, or gross amount.",
                *[document.document_id for document in remittances],
            )
        )

    matching_remittance = _cited_matching_remittance(cited_docs, payment)
    if matching_remittance is None:
        if policy.require_remittance:
            if remittances:
                issues.append(
                    _issue(
                        ValidationCode.MISSING_REMITTANCE,
                        "A remittance bound to this payment exists but was not included as "
                        "opened evidence for the proposal.",
                        payment.payment_id,
                        *[document.document_id for document in remittances],
                    )
                )
            else:
                issues.append(
                    _issue(
                        ValidationCode.MISSING_REMITTANCE,
                        "A matching remittance is required before this payment can be applied.",
                        payment.payment_id,
                    )
                )
        if _ambiguous_without_remittance(repo, workspace.workspace_id, payment, remittances):
            issues.append(
                _issue(
                    ValidationCode.AMBIGUOUS_MATCH,
                    "More than one open invoice matches this payment amount and no remittance "
                    "identifies the intended invoice.",
                    payment.payment_id,
                )
            )
    else:
        issues.extend(
            _remittance_relationship_issues(
                matching_remittance, proposal, payment, policy, selected_invoices
            )
        )

    issues.extend(_open_dispute_issues(documents, selected_invoices))
    issues.extend(_resolution_structure_issues(proposal, policy))
    issues.extend(_amount_issues(proposal, payment, selected_invoices, matching_remittance, policy))

    if proposal.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE:
        issues.extend(
            _fee_evidence_issues(
                proposal=proposal,
                payment=payment,
                policy=policy,
                remittance=matching_remittance,
                invoices=selected_invoices,
                documents=documents,
                cited_docs=cited_docs,
            )
        )
    return issues


def _payment_issues(
    payment: PaymentRecord,
    workspace: WorkspaceRecord,
    policy: CompanyPolicy,
    proposal: ResolutionProposal,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if payment.applied:
        issues.append(
            _issue(
                ValidationCode.PAYMENT_ALREADY_APPLIED,
                f"Payment {payment.payment_id} has already been applied.",
                payment.payment_id,
            )
        )
    if payment.currency != policy.currency:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_CURRENCY,
                f"Payment currency {payment.currency} is not supported by company policy.",
                payment.payment_id,
            )
        )
    if payment.bank_account_id != policy.allowed_cash_account_id:
        issues.append(
            _issue(
                ValidationCode.ACCOUNT_MISMATCH,
                "The payment was received on an account that is not the allowed cash account.",
                payment.payment_id,
                payment.bank_account_id,
                policy.allowed_cash_account_id,
            )
        )
    if payment.channel not in policy.supported_channels:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_CHANNEL,
                f"Payment channel {payment.channel.value} is not supported by company policy.",
                payment.payment_id,
            )
        )
    elif (
        proposal.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE
        and payment.channel is not PaymentChannel.WIRE
    ):
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_CHANNEL,
                "A documented receiving-wire fee requires channel WIRE.",
                payment.payment_id,
            )
        )
    if payment.posted_date < workspace.period_start or payment.posted_date > workspace.period_end:
        issues.append(
            _issue(
                ValidationCode.OUT_OF_PERIOD,
                "The payment posted date is outside the workspace processing period.",
                payment.payment_id,
            )
        )
    return issues


def _invoice_issues(
    invoice: InvoiceRecord,
    customer_id: str,
    policy: CompanyPolicy,
    payment: PaymentRecord | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if invoice.status is InvoiceSourceStatus.VOID:
        issues.append(
            _issue(
                ValidationCode.VOID_INVOICE,
                f"Invoice {invoice.invoice_id} is void and cannot be settled.",
                invoice.invoice_id,
            )
        )
    elif invoice.outstanding_cents == 0:
        issues.append(
            _issue(
                ValidationCode.INVOICE_ALREADY_PAID,
                f"Invoice {invoice.invoice_id} has no remaining outstanding balance.",
                invoice.invoice_id,
            )
        )
    if invoice.customer_id != customer_id:
        issues.append(
            _issue(
                ValidationCode.CUSTOMER_MISMATCH,
                (
                    f"Invoice {invoice.invoice_id} belongs to {invoice.customer_id}, "
                    f"not {customer_id}."
                ),
                invoice.invoice_id,
                invoice.customer_id,
                customer_id,
            )
        )
    if invoice.currency != policy.currency:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_CURRENCY,
                f"Invoice currency {invoice.currency} is not supported by company policy.",
                invoice.invoice_id,
            )
        )
    if payment is not None and invoice.currency != payment.currency:
        issues.append(
            _issue(
                ValidationCode.CURRENCY_MISMATCH,
                "Invoice and payment currencies do not match; no conversion is performed.",
                invoice.invoice_id,
                payment.payment_id,
            )
        )
    return issues


def _evidence_issues(
    repo: Repo,
    context: RunContext,
    workspace: WorkspaceRecord,
    proposal: ResolutionProposal,
    documents: Sequence[Document],
) -> tuple[list[Document], list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    opened = {item.document_id: item.sha256 for item in context.opened_documents}
    cited: list[Document] = []
    by_id = {document.document_id: document for document in documents}
    for document_id in proposal.evidence_document_ids:
        document = by_id.get(document_id) or get_document(repo, workspace.workspace_id, document_id)
        if document is None:
            issues.append(
                _issue(
                    ValidationCode.NOT_FOUND,
                    f"Evidence document {document_id} was not found in this workspace.",
                    document_id,
                )
            )
            continue
        cited.append(document)
        if document_id not in opened:
            issues.append(
                _issue(
                    ValidationCode.EVIDENCE_NOT_OPENED,
                    f"Document {document_id} must be opened in this run before it can authorize "
                    "a proposal.",
                    document_id,
                )
            )
        elif opened[document_id] != document.sha256:
            issues.append(
                _issue(
                    ValidationCode.EVIDENCE_NOT_OPENED,
                    f"Opened evidence for {document_id} does not match the stored document hash.",
                    document_id,
                )
            )
        if document.issued_date > workspace.period_end:
            issues.append(
                _issue(
                    ValidationCode.FUTURE_EVIDENCE,
                    f"Document {document_id} is dated after the workspace period end.",
                    document_id,
                )
            )
    return cited, issues


def _precedent_issues(context: RunContext, proposal: ResolutionProposal) -> list[ValidationIssue]:
    allowed = set(context.retrieved_precedent_version_ids)
    unknown = [item for item in proposal.precedent_ids_used if item not in allowed]
    if not unknown:
        return []
    return [
        _issue(
            ValidationCode.INVALID_PRECEDENT_REFERENCE,
            "Precedent IDs must be a subset of versions retrieved in this run.",
            *unknown,
        )
    ]


def _payment_remittances(documents: Sequence[Document], payment: PaymentRecord) -> list[Document]:
    return [
        document
        for document in documents
        if document.kind is DocumentKind.REMITTANCE
        and isinstance(document.facts, RemittanceFacts)
        and document.facts.bank_reference == payment.bank_reference
    ]


def _remittances_conflict(remittances: Sequence[Document]) -> bool:
    comparable = [
        document.facts for document in remittances if isinstance(document.facts, RemittanceFacts)
    ]
    if len(comparable) < 2:
        return False
    first = comparable[0]
    first_invoices = set(first.invoice_ids)
    for facts in comparable[1:]:
        if (
            facts.customer_id != first.customer_id
            or set(facts.invoice_ids) != first_invoices
            or facts.gross_settlement_cents != first.gross_settlement_cents
        ):
            return True
    return False


def _cited_matching_remittance(
    cited_docs: Sequence[Document], payment: PaymentRecord
) -> Document | None:
    matches = _payment_remittances(cited_docs, payment)
    return matches[0] if matches else None


def _ambiguous_without_remittance(
    repo: Repo,
    workspace_id: str,
    payment: PaymentRecord,
    remittances: Sequence[Document],
) -> bool:
    if remittances:
        return False
    matches = [
        invoice
        for invoice in list_invoices(repo, workspace_id)
        if invoice.status is InvoiceSourceStatus.OPEN
        and invoice.outstanding_cents == payment.amount_cents
        and invoice.currency == payment.currency
    ]
    return len(matches) >= 2


def _remittance_relationship_issues(
    remittance: Document,
    proposal: ResolutionProposal,
    payment: PaymentRecord,
    policy: CompanyPolicy,
    invoices: Sequence[InvoiceRecord],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    facts = remittance.facts
    if not isinstance(facts, RemittanceFacts):
        return issues
    selected_ids = [invoice.invoice_id for invoice in invoices]
    if facts.customer_id != proposal.customer_id:
        issues.append(
            _issue(
                ValidationCode.CUSTOMER_MISMATCH,
                "The remittance customer does not match the proposed customer.",
                remittance.document_id,
                facts.customer_id,
                proposal.customer_id,
            )
        )
    if facts.currency != payment.currency:
        issues.append(
            _issue(
                ValidationCode.CURRENCY_MISMATCH,
                "The remittance currency does not match the payment currency.",
                remittance.document_id,
                payment.payment_id,
            )
        )
    if facts.currency != policy.currency:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_CURRENCY,
                f"Remittance currency {facts.currency} is not supported by company policy.",
                remittance.document_id,
            )
        )
    if facts.receiving_account_id != payment.bank_account_id:
        issues.append(
            _issue(
                ValidationCode.ACCOUNT_MISMATCH,
                "The remittance receiving account does not match the payment account.",
                remittance.document_id,
                facts.receiving_account_id,
                payment.bank_account_id,
            )
        )
    if facts.bank_reference != payment.bank_reference:
        issues.append(
            _issue(
                ValidationCode.REFERENCE_MISMATCH,
                "The remittance bank reference does not match the payment.",
                remittance.document_id,
                payment.payment_id,
            )
        )
    if selected_ids and set(facts.invoice_ids) != set(selected_ids):
        issues.append(
            _issue(
                ValidationCode.REFERENCE_MISMATCH,
                "Selected invoices must be exactly the invoice set named by the remittance.",
                remittance.document_id,
                *sorted(set(facts.invoice_ids) | set(selected_ids)),
            )
        )
    return issues


def _open_dispute_issues(
    documents: Sequence[Document], invoices: Sequence[InvoiceRecord]
) -> list[ValidationIssue]:
    selected = {invoice.invoice_id for invoice in invoices}
    issues: list[ValidationIssue] = []
    for document in documents:
        if document.kind is not DocumentKind.DISPUTE_NOTICE:
            continue
        facts = document.facts
        if not isinstance(facts, DisputeNoticeFacts):
            continue
        if facts.invoice_id not in selected:
            continue
        if facts.status is DisputeStatus.OPEN:
            issues.append(
                _issue(
                    ValidationCode.OPEN_DISPUTE,
                    f"Invoice {facts.invoice_id} has an open dispute and cannot be settled "
                    "automatically.",
                    document.document_id,
                    facts.invoice_id,
                )
            )
    return issues


def _resolution_structure_issues(
    proposal: ResolutionProposal,
    policy: CompanyPolicy,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    allocation_count = len(proposal.allocations)
    fee_amounts = [item.fee_cents for item in proposal.allocations]
    positive_fees = [amount for amount in fee_amounts if amount > 0]
    if proposal.resolution_type is ResolutionType.EXACT_SINGLE and allocation_count != 1:
        issues.append(
            _issue(
                ValidationCode.INVALID_SCHEMA,
                "An exact single-invoice settlement must include exactly one allocation.",
                proposal.payment_id,
            )
        )
    if proposal.resolution_type is ResolutionType.EXACT_BUNDLE and not (
        2 <= allocation_count <= policy.max_bundle_invoices
    ):
        issues.append(
            _issue(
                ValidationCode.INVALID_SCHEMA,
                "An exact bundle must include two or three invoices named by the remittance.",
                proposal.payment_id,
            )
        )
    if proposal.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE:
        if allocation_count != 1 or len(positive_fees) != 1:
            issues.append(
                _issue(
                    ValidationCode.UNSUPPORTED_BUNDLE_FEE
                    if allocation_count > 1
                    else ValidationCode.AMOUNT_MISMATCH,
                    "A documented bank-fee settlement requires exactly one invoice and one "
                    "positive fee.",
                    proposal.payment_id,
                )
            )
        if not policy.allow_receiving_wire_fee:
            issues.append(
                _issue(
                    ValidationCode.UNSUPPORTED_FEE_TYPE,
                    "Company policy does not allow a receiving-wire fee deduction.",
                    proposal.payment_id,
                )
            )
        if positive_fees and positive_fees[0] > policy.max_receiving_wire_fee_cents:
            issues.append(
                _issue(
                    ValidationCode.FEE_OVER_LIMIT,
                    "The proposed receiving-wire fee exceeds the company policy limit of "
                    f"{policy.max_receiving_wire_fee_cents} cents.",
                    proposal.payment_id,
                )
            )
    else:
        if positive_fees:
            code = (
                ValidationCode.UNSUPPORTED_BUNDLE_FEE
                if allocation_count > 1
                else ValidationCode.UNSUPPORTED_FEE_TYPE
            )
            issues.append(
                _issue(
                    code,
                    "Exact settlements cannot include a fee; documented bank fees require "
                    "resolution type SINGLE_WITH_BANK_FEE.",
                    proposal.payment_id,
                    *[item.invoice_id for item in proposal.allocations],
                )
            )
    return issues


def _amount_issues(
    proposal: ResolutionProposal,
    payment: PaymentRecord,
    invoices: Sequence[InvoiceRecord],
    remittance: Document | None,
    policy: CompanyPolicy,
) -> list[ValidationIssue]:
    if len(invoices) != len(proposal.allocations):
        return []
    issues: list[ValidationIssue] = []
    outstanding_by_id = {invoice.invoice_id: invoice.outstanding_cents for invoice in invoices}
    named_outstanding = sum(outstanding_by_id.values())
    cash_sum = sum(item.cash_cents for item in proposal.allocations)
    fee_sum = sum(item.fee_cents for item in proposal.allocations)
    remaining_after = []
    for item in proposal.allocations:
        covered = item.cash_cents + item.fee_cents
        outstanding = outstanding_by_id[item.invoice_id]
        remaining_after.append(outstanding - covered)
    equations_hold = cash_sum == payment.amount_cents and all(
        item.cash_cents + item.fee_cents == outstanding_by_id[item.invoice_id]
        for item in proposal.allocations
    )
    remittance_facts = remittance.facts if remittance is not None else None
    if isinstance(remittance_facts, RemittanceFacts):
        if cash_sum + fee_sum != remittance_facts.gross_settlement_cents:
            equations_hold = False
        if remittance_facts.gross_settlement_cents != named_outstanding:
            equations_hold = False

    if payment.amount_cents > named_outstanding:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_OVERPAYMENT,
                "The payment exceeds the current outstanding balances of the named invoices.",
                payment.payment_id,
                *[invoice.invoice_id for invoice in invoices],
            )
        )
    fee_case = (
        proposal.resolution_type is ResolutionType.SINGLE_WITH_BANK_FEE
        and len(proposal.allocations) == 1
        and proposal.allocations[0].fee_cents > 0
    )
    leftover = any(remaining > 0 for remaining in remaining_after)
    if leftover and not policy.allow_partial_settlement and not fee_case:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_PARTIAL,
                "Partial settlement is not allowed; remaining invoice balances must stay "
                "unapplied and go to review.",
                payment.payment_id,
                *[invoice.invoice_id for invoice in invoices],
            )
        )
    if fee_case and leftover:
        issues.append(
            _issue(
                ValidationCode.AMOUNT_MISMATCH,
                "Cash plus documented fee must equal the current invoice outstanding balance.",
                payment.payment_id,
                proposal.allocations[0].invoice_id,
            )
        )
    if not equations_hold:
        issues.append(
            _issue(
                ValidationCode.AMOUNT_MISMATCH,
                "Allocation cash and fee amounts must match the payment, current invoice "
                "outstanding balances, and remittance gross with no rounding.",
                payment.payment_id,
                *[invoice.invoice_id for invoice in invoices],
            )
        )
    return issues


def _fee_evidence_issues(
    *,
    proposal: ResolutionProposal,
    payment: PaymentRecord,
    policy: CompanyPolicy,
    remittance: Document | None,
    invoices: Sequence[InvoiceRecord],
    documents: Sequence[Document],
    cited_docs: Sequence[Document],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    link_refs = _link_references(remittance)
    if remittance is not None and not link_refs:
        issues.append(
            _issue(
                ValidationCode.MISSING_FEE_NOTICE,
                "The remittance does not include a transfer reference or settlement ticket "
                "that can identify a bank fee notice.",
                remittance.document_id,
            )
        )
    if len(link_refs) >= 2:
        notices_by_ref = {
            reference: _matching_fee_notices(
                documents,
                customer_id=proposal.customer_id,
                account_id=payment.bank_account_id,
                currency=payment.currency,
                references=[reference],
            )
            for reference in link_refs
        }
        populated = [
            notices_by_ref[reference] for reference in link_refs if notices_by_ref[reference]
        ]
        if len(populated) >= 2 and _fee_notices_conflict(populated[0][0], populated[1][0]):
            issues.append(
                _issue(
                    ValidationCode.CONFLICTING_EVIDENCE,
                    "The remittance transfer reference and settlement ticket point to "
                    "contradictory bank fee notices.",
                    remittance.document_id if remittance is not None else payment.payment_id,
                    populated[0][0].document_id,
                    populated[1][0].document_id,
                )
            )

    cited_notices = [
        document
        for document in cited_docs
        if document.kind is DocumentKind.BANK_FEE_NOTICE
        and isinstance(document.facts, BankFeeNoticeFacts)
    ]
    for notice in cited_notices:
        facts = notice.facts
        assert isinstance(facts, BankFeeNoticeFacts)
        if facts.customer_id != proposal.customer_id:
            issues.append(
                _issue(
                    ValidationCode.CUSTOMER_MISMATCH,
                    "The bank fee notice customer does not match the proposed customer.",
                    notice.document_id,
                    facts.customer_id,
                    proposal.customer_id,
                )
            )
        if facts.receiving_account_id != payment.bank_account_id:
            issues.append(
                _issue(
                    ValidationCode.ACCOUNT_MISMATCH,
                    "The bank fee notice account does not match the payment account.",
                    notice.document_id,
                    facts.receiving_account_id,
                    payment.bank_account_id,
                )
            )
        if facts.currency != payment.currency:
            issues.append(
                _issue(
                    ValidationCode.CURRENCY_MISMATCH,
                    "The bank fee notice currency does not match the payment.",
                    notice.document_id,
                    payment.payment_id,
                )
            )
        if link_refs and facts.transfer_reference not in link_refs:
            issues.append(
                _issue(
                    ValidationCode.REFERENCE_MISMATCH,
                    "The bank fee notice transfer reference does not match the remittance "
                    "ticket or transfer reference for this payment.",
                    notice.document_id,
                    *link_refs,
                )
            )
        if facts.fee_type != SUPPORTED_FEE_TYPE:
            issues.append(
                _issue(
                    ValidationCode.UNSUPPORTED_FEE_TYPE,
                    _unsupported_fee_type_message(facts.fee_type),
                    notice.document_id,
                )
            )

    matches = _matching_fee_notices(
        documents,
        customer_id=proposal.customer_id,
        account_id=payment.bank_account_id,
        currency=payment.currency,
        references=link_refs,
    )
    cited_match_ids = {document.document_id for document in cited_notices}
    usable = [document for document in matches if document.document_id in cited_match_ids]
    if not matches:
        issues.append(
            _issue(
                ValidationCode.MISSING_FEE_NOTICE,
                _missing_fee_message(link_refs),
                payment.payment_id,
                *link_refs,
            )
        )
        return issues
    if not usable:
        issues.append(
            _issue(
                ValidationCode.MISSING_FEE_NOTICE,
                "A matching bank fee notice exists but was not included as opened evidence.",
                payment.payment_id,
                *[document.document_id for document in matches],
            )
        )
        return issues
    if len(usable) > 1 and any(_fee_notices_conflict(usable[0], other) for other in usable[1:]):
        issues.append(
            _issue(
                ValidationCode.CONFLICTING_EVIDENCE,
                "Multiple matching bank fee notices disagree about this transfer.",
                *[document.document_id for document in usable],
            )
        )
        return issues

    notice = usable[0]
    facts = notice.facts
    assert isinstance(facts, BankFeeNoticeFacts)
    if facts.fee_type != SUPPORTED_FEE_TYPE:
        issues.append(
            _issue(
                ValidationCode.UNSUPPORTED_FEE_TYPE,
                _unsupported_fee_type_message(facts.fee_type),
                notice.document_id,
            )
        )
    if facts.fee_cents > policy.max_receiving_wire_fee_cents:
        issues.append(
            _issue(
                ValidationCode.FEE_OVER_LIMIT,
                "The bank fee notice exceeds the company policy limit of "
                f"{policy.max_receiving_wire_fee_cents} cents.",
                notice.document_id,
            )
        )
    invoice = invoices[0] if len(invoices) == 1 else None
    allocation = proposal.allocations[0] if proposal.allocations else None
    if invoice is not None and allocation is not None:
        expected_fee = invoice.outstanding_cents - payment.amount_cents
        if (
            facts.gross_cents != invoice.outstanding_cents
            or facts.net_cents != payment.amount_cents
            or facts.fee_cents != expected_fee
            or facts.net_cents + facts.fee_cents != facts.gross_cents
            or allocation.fee_cents != facts.fee_cents
            or allocation.cash_cents != facts.net_cents
        ):
            issues.append(
                _issue(
                    ValidationCode.AMOUNT_MISMATCH,
                    "The bank fee notice gross, net, and fee must agree with the current "
                    "invoice balance and the incoming payment.",
                    notice.document_id,
                    invoice.invoice_id,
                    payment.payment_id,
                )
            )
    return issues


def _link_references(remittance: Document | None) -> list[str]:
    if remittance is None or not isinstance(remittance.facts, RemittanceFacts):
        return []
    values: list[str] = []
    for value in (remittance.facts.transfer_reference, remittance.facts.settlement_ticket):
        if value and value not in values:
            values.append(value)
    return values


def _matching_fee_notices(
    documents: Sequence[Document],
    *,
    customer_id: str,
    account_id: str,
    currency: str,
    references: Sequence[str],
) -> list[Document]:
    if not references:
        return []
    allowed = set(references)
    matches: list[Document] = []
    for document in documents:
        if document.kind is not DocumentKind.BANK_FEE_NOTICE:
            continue
        facts = document.facts
        if not isinstance(facts, BankFeeNoticeFacts):
            continue
        if facts.transfer_reference not in allowed:
            continue
        if facts.customer_id != customer_id:
            continue
        if facts.receiving_account_id != account_id:
            continue
        if facts.currency != currency:
            continue
        matches.append(document)
    return matches


def _fee_notices_conflict(left: Document, right: Document) -> bool:
    first = left.facts
    second = right.facts
    if not isinstance(first, BankFeeNoticeFacts) or not isinstance(second, BankFeeNoticeFacts):
        return True
    return (
        first.customer_id != second.customer_id
        or first.receiving_account_id != second.receiving_account_id
        or first.currency != second.currency
        or first.transfer_reference != second.transfer_reference
        or first.fee_cents != second.fee_cents
        or first.gross_cents != second.gross_cents
        or first.net_cents != second.net_cents
        or first.fee_type != second.fee_type
    )


def _unsupported_fee_type_message(fee_type: str) -> str:
    return f"Fee type {fee_type} is not supported; only {SUPPORTED_FEE_TYPE} is allowed."


def _missing_fee_message(link_refs: Sequence[str]) -> str:
    if link_refs:
        joined = ", ".join(link_refs)
        return f"Obtain the bank fee notice matching transfer or settlement ticket {joined}."
    return "Obtain the bank fee notice that documents this receiving-wire fee."


def _duplicate_allocation_ids(allocations: object) -> list[str]:
    if not isinstance(allocations, list):
        return []
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in allocations:
        if not isinstance(item, dict):
            continue
        invoice_id = item.get("invoice_id")
        if not isinstance(invoice_id, str):
            continue
        if invoice_id in seen and invoice_id not in duplicates:
            duplicates.append(invoice_id)
        seen.add(invoice_id)
    return duplicates


def _evidence_hashes(repo: Repo, workspace_id: str, document_ids: Iterable[str]) -> list[str]:
    hashes: list[str] = []
    seen: set[str] = set()
    for document_id in document_ids:
        document = get_document(repo, workspace_id, document_id)
        if document is None or document.sha256 in seen:
            continue
        seen.add(document.sha256)
        hashes.append(document.sha256)
    return hashes


def _issue(code: ValidationCode, message: str, *record_ids: str) -> ValidationIssue:
    unique_ids = list(dict.fromkeys(record_id for record_id in record_ids if record_id))
    return ValidationIssue(code=code, message=message, record_ids=unique_ids)


def _unique_sorted(issues: Sequence[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[object, ...]] = set()
    unique: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.code, tuple(issue.record_ids), issue.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return sorted(
        unique,
        key=lambda item: (_CODE_RANK[item.code], item.record_ids, item.message),
    )


def _report(
    *,
    issues: Sequence[ValidationIssue],
    normalized: ResolutionProposal | None,
    context: RunContext,
    workspace: WorkspaceRecord | None,
    evidence_hashes: Sequence[str],
) -> ValidationReport:
    unique_issues = _unique_sorted(issues)
    policy_id = workspace.policy.policy_id if workspace is not None else context.policy_id
    revision = (
        workspace.ledger_revision if workspace is not None else context.initial_ledger_revision
    )
    valid = not unique_issues and normalized is not None
    return ValidationReport(
        valid=valid,
        issues=[] if valid else unique_issues,
        normalized_proposal=normalized if valid else None,
        checked_ledger_revision=revision,
        policy_id=policy_id,
        evidence_hashes=list(evidence_hashes),
    )


__all__ = [
    "Repo",
    "apply_proposal",
    "get_case_snapshot",
    "store_proposal",
    "validate_proposal",
]
