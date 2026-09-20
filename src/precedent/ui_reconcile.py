"""One derivation of what arrived, what was billed, and what is unexplained.

The Work Queue and Case Detail ask the same question about a payment, so they
have to answer it from the same code. A controller who reads ``$35.00
unexplained`` on a queue row and a different figure on the case would stop
trusting both screens.

This module exists because ``snapshot.invoices`` returns every open invoice
belonging to the payer's customer rather than the invoices this payment could
be settling. For the demo workspace that is fourteen rows against a payment
that settles one. The narrowing rule mirrors ``ledger._payment_remittances``: a
stored remittance belongs to this payment when its ``bank_reference`` equals the
payment's, and the invoices it names are the candidates. Invoices touched by a
stored decision are added afterwards so a posted application is never hidden.

**Two readings of "what was billed", both correct.** They are named separately
instead of being collapsed into one ambiguous field:

``invoice_outstanding_cents``
    What the remittance-named invoices still owe as the ledger stands now. The
    queue asks this, because a queue is a list of work remaining.

``invoice_original_cents``
    What the candidate invoices were billed. Case Detail asks this, because the
    fee equation has to keep reading ``$9,965.00 + $35.00 = $10,000.00`` after
    the invoice has been paid down to zero.

**Nothing here is ever fabricated.** When no remittance names an invoice this
workspace holds, both readings are ``None``. The screens render that as blank
and say so in words. A ``$0.00`` would read as "nothing to see here", which is
the opposite of what is true. Likewise a payment that has already been applied
reports zero unexplained through ``settled``, not through a subtraction that
would happen to land on zero.

This module stays in the UI layer. Moving it into ``services.py`` would be
better layering and is recorded as a follow-up, not done here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import (
    Allocation,
    BankFeeNoticeFacts,
    CaseDetail,
    CaseSummary,
    Document,
    DocumentKind,
    InvoiceRecord,
    PaymentRecord,
    RemittanceFacts,
)

UNKNOWN_BASIS = "No remittance on file"
FOREIGN_BASIS = "Invoice not in this ledger"
CUSTOMER_ORIGIN = "Payer's customer"

_EVIDENCE_KINDS = {DocumentKind.REMITTANCE, DocumentKind.BANK_FEE_NOTICE}


@dataclass(frozen=True)
class Reconciliation:
    """Every ledger fact the queue row, the case header, and the form agree on.

    Each field is read from storage. Nothing is inferred, and no absent value is
    replaced with a zero.
    """

    received_cents: int
    currency: str
    settled: bool
    candidates: tuple[InvoiceRecord, ...] = ()
    origins: dict[str, str] = field(default_factory=dict)
    narrowed: bool = False
    remitted_ids: tuple[str, ...] = ()
    remitted_invoices: tuple[InvoiceRecord, ...] = ()
    remitted_complete: bool = False
    remittances: tuple[Document, ...] = ()
    fee_notices: tuple[Document, ...] = ()
    link_refs: tuple[str, ...] = ()
    explained: bool = False
    source: str | None = None

    @property
    def invoice_original_cents(self) -> int | None:
        """What the candidate invoices were billed, or ``None`` if unestablished."""
        if not self.narrowed:
            return None
        return sum(item.original_cents for item in self.candidates)

    @property
    def invoice_outstanding_cents(self) -> int | None:
        """What the remittance-named invoices still owe, or ``None`` if unknown."""
        if not self.remitted_ids or not self.remitted_complete:
            return None
        return sum(item.outstanding_cents for item in self.remitted_invoices)

    @property
    def unexplained_cents(self) -> int | None:
        """Money still unaccounted for, or ``None`` when that cannot be derived.

        An applied payment is settled by fact, not by arithmetic. Its invoices
        already reflect the cash, so subtracting again would be meaningless.
        """
        if self.settled:
            return 0
        outstanding = self.invoice_outstanding_cents
        if outstanding is None:
            return None
        return outstanding - self.received_cents

    @property
    def is_open_question(self) -> bool:
        return bool(self.unexplained_cents)

    @property
    def difference_cents(self) -> int:
        """Billed minus received against the original invoice amounts."""
        billed = self.invoice_original_cents
        if billed is None:
            return 0
        return billed - self.received_cents

    @property
    def basis(self) -> str:
        """What the payment was matched to, or why it could not be matched."""
        if not self.remitted_ids:
            return UNKNOWN_BASIS
        if not self.remitted_complete:
            return FOREIGN_BASIS
        if len(self.remitted_ids) == 1:
            return self.remitted_ids[0]
        return f"{self.remitted_ids[0]} +{len(self.remitted_ids) - 1}"

    @property
    def key_document_ids(self) -> tuple[str, ...]:
        ids = [item.document_id for item in self.remittances]
        ids.extend(item.document_id for item in self.fee_notices)
        return tuple(ids)


def reconcile(settings: svc.Settings, workspace_id: str, detail: CaseDetail) -> Reconciliation:
    """Derive the reconciliation for one case from its stored evidence."""
    payment = detail.snapshot.payment
    remittances, fee_notices, link_refs = _load_evidence(settings, workspace_id, detail)
    by_id = {item.invoice_id: item for item in detail.snapshot.invoices}

    remitted_ids: list[str] = []
    origins: dict[str, str] = {}
    for document in remittances:
        facts = document.facts
        if not isinstance(facts, RemittanceFacts):
            continue
        for invoice_id in facts.invoice_ids:
            if invoice_id not in remitted_ids:
                remitted_ids.append(invoice_id)
            origins[invoice_id] = "Remittance"

    named = list(remitted_ids)
    for invoice_id in _decision_invoice_ids(detail):
        if invoice_id not in named:
            named.append(invoice_id)
        origins[invoice_id] = "Remittance and decision" if invoice_id in origins else "Decision"

    candidates = [by_id[item] for item in named if item in by_id]
    narrowed = bool(candidates)
    if not narrowed:
        # Nothing establishes which invoices this payment settles, so the page
        # shows the payer's whole customer ledger and has to say that it did.
        candidates = list(detail.snapshot.invoices)
        origins = {item.invoice_id: CUSTOMER_ORIGIN for item in candidates}

    billed = sum(item.original_cents for item in candidates) if narrowed else None
    explained, source = _explanation(detail, payment.amount_cents, billed, fee_notices, link_refs)
    return Reconciliation(
        received_cents=payment.amount_cents,
        currency=payment.currency,
        settled=bool(payment.applied),
        candidates=tuple(candidates),
        origins=origins,
        narrowed=narrowed,
        remitted_ids=tuple(remitted_ids),
        remitted_invoices=tuple(by_id[item] for item in remitted_ids if item in by_id),
        remitted_complete=all(item in by_id for item in remitted_ids),
        remittances=tuple(remittances),
        fee_notices=tuple(fee_notices),
        link_refs=tuple(link_refs),
        explained=explained,
        source=source,
    )


def reconcile_summary(
    settings: svc.Settings, workspace_id: str, case: CaseSummary
) -> Reconciliation:
    """Reconcile a queue row, degrading to "unknown" if the case cannot be read."""
    try:
        detail = svc.get_case_detail(settings, workspace_id, case.case_id)
    except svc.PersistenceError:
        return Reconciliation(
            received_cents=case.amount_cents, currency=case.currency, settled=False
        )
    return reconcile(settings, workspace_id, detail)


def fee_citation(fee_notices: list[Document], link_refs: list[str]) -> str:
    """Name the documents a fee rests on, or say plainly that there are none."""
    if not fee_notices:
        return "with no stored fee notice on file."
    names = ", ".join(item.document_id for item in fee_notices)
    if link_refs:
        return f"evidenced by {names} under settlement ticket {link_refs[0]}."
    return f"evidenced by {names}."


def _load_evidence(
    settings: svc.Settings, workspace_id: str, detail: CaseDetail
) -> tuple[list[Document], list[Document], list[str]]:
    """Return this payment's remittances, the fee notices they link to, and the links.

    Only remittances and bank fee notices are loaded. Matching repeats the
    ledger's own rule so the screen and the validator cannot disagree about
    which documents belong to this payment.
    """
    payment: PaymentRecord = detail.snapshot.payment
    remittances: list[Document] = []
    notices: list[Document] = []
    for summary in detail.documents:
        if summary.kind not in _EVIDENCE_KINDS:
            continue
        try:
            document = svc.get_source_document(settings, workspace_id, summary.document_id)
        except svc.PersistenceError:
            continue
        facts = document.facts
        if isinstance(facts, RemittanceFacts) and facts.bank_reference == payment.bank_reference:
            remittances.append(document)
        elif isinstance(facts, BankFeeNoticeFacts):
            notices.append(document)
    link_refs: list[str] = []
    for document in remittances:
        facts = document.facts
        if not isinstance(facts, RemittanceFacts):
            continue
        for value in (facts.settlement_ticket, facts.transfer_reference):
            if value and value not in link_refs:
                link_refs.append(value)
    linked = [
        document
        for document in notices
        if isinstance(document.facts, BankFeeNoticeFacts)
        and document.facts.transfer_reference in link_refs
    ]
    return remittances, linked, link_refs


def _decision_invoice_ids(detail: CaseDetail) -> list[str]:
    """Invoice ids touched by anything already decided or proposed for this case."""
    found: list[str] = []

    def _add(allocations: list[Allocation]) -> None:
        for item in allocations:
            if item.invoice_id not in found:
                found.append(item.invoice_id)

    if detail.application is not None:
        _add(list(detail.application.allocations))
    for proposal in detail.proposals:
        _add(list(proposal.payload.allocations))
    for correction in detail.corrections:
        if correction.verified_resolved_proposal is not None:
            _add(list(correction.verified_resolved_proposal.allocations))
    return found


def _explanation(
    detail: CaseDetail,
    received_cents: int,
    billed_cents: int | None,
    fee_notices: list[Document],
    link_refs: list[str],
) -> tuple[bool, str | None]:
    """Decide whether the difference has an accepted, sourced explanation.

    Only a posted application counts as accepted. A proposal, however well
    validated, has not moved money and must not turn the gap green.
    """
    if billed_cents is None:
        return False, None
    difference = billed_cents - received_cents
    application = detail.application
    if application is not None:
        fee_total = sum(item.fee_cents for item in application.allocations)
        if difference > 0 and fee_total == difference:
            cited = fee_citation(fee_notices, link_refs)
            return True, (
                f"Posted as application {application.application_id}. "
                f"{theme.money(fee_total)} recorded as a documented bank fee, {cited}"
            )
        if difference == 0:
            return True, f"Posted as application {application.application_id}. Amounts agree."
    if difference == 0:
        return False, None
    return False, _open_question_note(difference, fee_notices, link_refs)


def _open_question_note(difference: int, fee_notices: list[Document], link_refs: list[str]) -> str:
    amount = theme.money(abs(difference))
    if difference < 0:
        return (
            f"{amount} more arrived than was billed. An overpayment is not a supported "
            "resolution shape, so this needs a human decision."
        )
    if fee_notices:
        names = ", ".join(item.document_id for item in fee_notices)
        ticket = link_refs[0] if link_refs else "the remittance reference"
        return (
            f"{names} records a bank fee against settlement ticket {ticket}. That would "
            f"account for the {amount}, but nothing has been applied yet."
        )
    ticket = link_refs[0] if link_refs else "this payment's reference"
    return (
        f"No stored fee notice references {ticket}, so the {amount} has no documentary "
        "support. Documented bank fee, or an amount the customer still owes?"
    )
