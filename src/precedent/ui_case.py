"""Case Detail: the reconciliation header, then Evidence, Decision, and Teach.

The page answers one question: an amount arrived that does not equal the amount
billed, so is the difference a documented bank fee or money the customer still
owes? The header states the arithmetic. The Evidence tab shows the documents
that could answer it. The Decision tab shows what was proposed, what the
validator said, and what was posted. The Teach tab is where a controller
records the investigation.

Candidate invoices, the documented amounts, and the open gap all come from
``precedent.ui_reconcile``, which the Work Queue also uses. Presentation comes
from ``precedent.ui_theme``. Nothing visual is defined locally in this module.
"""

from __future__ import annotations

import streamlit as st
from pydantic import ValidationError

from precedent import ui_reconcile as recon_lib
from precedent import ui_services as svc
from precedent import ui_theme as theme
from precedent.models import (
    Allocation,
    ApplicationStatus,
    BankFeeNoticeFacts,
    CaseDetail,
    CaseState,
    CorrectionRecord,
    Document,
    DocumentKind,
    DocumentSummary,
    ExecutionMode,
    InvoiceRecord,
    PaymentRecord,
    PrecedentRecord,
    RemittanceFacts,
    ResolutionProposal,
    ResolutionType,
    parse_decimal_cents,
)
from precedent.ui_reconcile import Reconciliation

HARBOR_EXAMPLE_TEXT = (
    "For Harbor's wire remittances, look up the settlement_ticket in the bank notice "
    "transfer_reference. This ticket links the current invoice to the current bank fee "
    "notice. Use the documented gross, net, and fee amounts and the existing company fee "
    "policy; never infer a fee merely from a short payment."
)
HARBOR_EXAMPLE_EXPLANATION = (
    "The settlement ticket links the remittance and bank advice; "
    "the documented amounts agree exactly."
)
HARBOR_EXAMPLE_CASH = "9965.00"
HARBOR_EXAMPLE_FEE = "35.00"

_EVIDENCE_EXCERPT_LINES = 10


def render_case_page(settings: svc.Settings) -> None:
    """Render Case Detail for the case remembered from the Work Queue."""
    workspace_id = st.session_state.get("workspace_id_choice") or st.session_state.get(
        "remembered_workspace_id"
    )
    remembered = st.session_state.get("remembered_case_id")
    if isinstance(remembered, str) and remembered:
        case_id = remembered
    else:
        case_id = st.session_state.get("case_select")
    if not isinstance(workspace_id, str) or not workspace_id:
        st.info("Select a workspace on the Work Queue.")
        return
    if not isinstance(case_id, str) or not case_id:
        st.info("Select a case on the Work Queue, then Open case.")
        return
    try:
        detail = svc.get_case_detail(settings, workspace_id, case_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    recon = recon_lib.reconcile(settings, workspace_id, detail)
    _render_case_header(detail, recon)
    _render_detail_error(case_id)
    _render_apply_status(case_id)
    _render_lesson_status(case_id)
    evidence_tab, decision_tab, teach_tab = st.tabs(["Evidence", "Decision", "Teach"])
    with evidence_tab:
        _render_evidence(settings, workspace_id, case_id, detail, recon)
    with decision_tab:
        _render_decision(settings, workspace_id, detail, recon)
    with teach_tab:
        _render_saved_corrections(settings, detail)
        _render_correction_form(settings, workspace_id, case_id, detail, recon)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------


def _render_case_header(detail: CaseDetail, recon: Reconciliation) -> None:
    summary = detail.snapshot.summary
    payment = detail.snapshot.payment
    theme.render_inline(
        theme.state_badge(summary.case_state),
        theme.provenance_badge(_case_provenance(detail)),
        theme.ident(summary.case_id, label="case", width_ch=22),
    )
    theme.section(
        payment.payer_text,
        subtitle=(
            f"Case {summary.case_id} received {theme.money(payment.amount_cents)} "
            f"{payment.currency} on {payment.posted_date}."
        ),
    )
    st.caption(
        f"Payment `{payment.payment_id}` · account `{payment.bank_account_id}` · "
        f"reference `{payment.bank_reference}` · channel {payment.channel.value} · "
        f"{payment.state.value}"
    )
    billed = recon.invoice_original_cents
    if billed is None:
        st.warning(
            f"No remittance matches bank reference {payment.bank_reference}, so no invoice "
            "balance can be stated for this payment. There is nothing to reconcile against "
            "until the customer is established from evidence."
        )
    else:
        theme.gap_strip(
            recon.received_cents,
            billed,
            explained=recon.explained,
            currency=recon.currency,
            source=recon.source,
        )
    _render_latest_run(detail)


def _case_provenance(detail: CaseDetail) -> ExecutionMode | str:
    """Label who produced the current state of this case, never guessing LIVE."""
    if detail.latest_run is not None:
        return detail.latest_run.mode
    if detail.application is not None or detail.corrections:
        return ExecutionMode.HUMAN
    return "NO RUN YET"


def _render_latest_run(detail: CaseDetail) -> None:
    run = detail.latest_run
    if run is None:
        return
    theme.render_inline(
        theme.quiet("Latest run"),
        theme.ident(run.run_id, width_ch=18),
        theme.provenance_badge(run.mode),
        theme.quiet(run.state.value),
    )
    if run.summary:
        theme.body(run.summary)


def _render_detail_error(case_id: str) -> None:
    error = st.session_state.last_detail_error
    if isinstance(error, dict) and error.get("case_id") == case_id:
        st.error(f"{error.get('code')}: {error.get('message')}")


def _render_apply_status(case_id: str) -> None:
    result = st.session_state.last_apply_result
    if not isinstance(result, dict) or result.get("case_id") != case_id:
        return
    status = result.get("status")
    application_id = result.get("application_id")
    if status == ApplicationStatus.APPLIED.value:
        st.success(f"Applied corrected resolution. Application `{application_id}`.")
    elif status == ApplicationStatus.REPLAYED.value:
        st.info(
            f"Already applied. No additional funds were posted. Application `{application_id}`."
        )
    elif status == ApplicationStatus.REJECTED.value:
        issues = result.get("issues") or []
        st.error("Corrected resolution was rejected. " + " ".join(str(item) for item in issues))


def _render_lesson_status(case_id: str) -> None:
    result = st.session_state.get("last_lesson_result")
    if not isinstance(result, dict) or result.get("case_id") != case_id:
        return
    precedent_id = result.get("precedent_id")
    status = result.get("status")
    if isinstance(precedent_id, str) and precedent_id:
        st.success(f"Draft lesson `{precedent_id}` stored as {status}. It is not active.")
        return
    reason = result.get("reason")
    if isinstance(reason, str) and reason:
        st.warning(reason)


# --------------------------------------------------------------------------
# Evidence tab
# --------------------------------------------------------------------------


@st.dialog("Source document", width="large")
def _document_dialog(document: Document) -> None:
    """Full stored text and normalized facts for one document."""
    theme.doc_card(document, footer=f"Stored content hash {document.sha256[:16]}.")
    st.caption(
        "Synthetic normalized input. Facts were authored with the fixture; this screen "
        "does not extract text from a scan."
    )
    st.json(document.facts.model_dump(mode="json"))


def _render_evidence(
    settings: svc.Settings,
    workspace_id: str,
    case_id: str,
    detail: CaseDetail,
    recon: Reconciliation,
) -> None:
    theme.section(
        "How the documents connect",
        subtitle=(
            "Resolving the difference takes more than one document. Each step below is "
            "a stored record, and the link between them is a reference field, not a guess."
        ),
    )
    _render_evidence_chain(detail.snapshot.payment, recon)
    theme.section(
        "Source documents",
        subtitle=(
            "Normalized synthetic source. Facts were authored with the fixture; "
            "this screen does not extract text from a scan."
        ),
    )
    documents = _ordered_documents(detail, recon)
    if not documents:
        st.info("This workspace has no source documents.")
        return
    by_id = {item.document_id: item for item in documents}
    selected_id = st.selectbox(
        "Source document",
        options=[item.document_id for item in documents],
        format_func=lambda value: _document_option_label(by_id[value], recon),
        key=f"evidence_document_{case_id}",
    )
    with st.container(key="pc-evidence-actions"):
        open_col, read_col, _spacer = st.columns([2.4, 3.2, 4.4])
        with open_col:
            open_clicked = st.button(
                "Open document",
                key=f"open_evidence_{case_id}",
                icon=":material/fact_check:",
                width="content",
                help=(
                    "Record that a controller inspected this document. The stored record "
                    "carries the document's content hash."
                ),
            )
        with read_col:
            read_clicked = st.button(
                "Read full document",
                key=f"read_document_{case_id}",
                icon=":material/article:",
                width="content",
                help="Open the complete stored text and its normalized facts. Records nothing.",
            )
    if open_clicked:
        _handle_open_evidence(settings, workspace_id, case_id, selected_id)
        return
    try:
        document = svc.get_source_document(settings, workspace_id, selected_id)
    except svc.PersistenceError as exc:
        st.error(str(exc))
        return
    theme.doc_card(
        document,
        excerpt_lines=_EVIDENCE_EXCERPT_LINES,
        footer="Synthetic normalized input.",
    )
    with st.expander("Normalized facts", expanded=False):
        st.json(document.facts.model_dump(mode="json"))
    if read_clicked:
        _document_dialog(document)


def _render_evidence_chain(payment: PaymentRecord, recon: Reconciliation) -> None:
    """Render the payment, remittance, ticket, and fee notice as ordered steps."""
    steps: list[tuple[str, str, bool]] = [
        (
            f"Payment {payment.payment_id} credited {theme.money(payment.amount_cents)}",
            f"Bank reference {payment.bank_reference} on account {payment.bank_account_id}, "
            f"channel {payment.channel.value}.",
            False,
        )
    ]
    if recon.remittances:
        for document in recon.remittances:
            facts = document.facts
            if not isinstance(facts, RemittanceFacts):
                continue
            invoices = ", ".join(facts.invoice_ids)
            ticket = facts.settlement_ticket or facts.transfer_reference
            tail = f" It carries reference {ticket}." if ticket else ""
            steps.append(
                (
                    f"Remittance {document.document_id} names {invoices}",
                    f"The customer states {theme.money(facts.gross_settlement_cents)} "
                    f"{facts.currency} settled for {facts.customer_id}, matching bank "
                    f"reference {facts.bank_reference}.{tail}",
                    False,
                )
            )
    else:
        steps.append(
            (
                "No remittance matches this payment",
                f"No stored remittance carries bank reference {payment.bank_reference}, so "
                "the customer and the invoices are not established by evidence.",
                True,
            )
        )
    if recon.fee_notices:
        for document in recon.fee_notices:
            facts = document.facts
            if not isinstance(facts, BankFeeNoticeFacts):
                continue
            steps.append(
                (
                    f"Fee notice {document.document_id} explains the difference",
                    f"Transfer {facts.transfer_reference}: gross "
                    f"{theme.money(facts.gross_cents)}, {facts.fee_type.lower().replace('_', ' ')} "
                    f"{theme.money(facts.fee_cents)}, net {theme.money(facts.net_cents)} "
                    f"{facts.currency}.",
                    False,
                )
            )
    elif recon.link_refs:
        steps.append(
            (
                f"Nothing references {recon.link_refs[0]}",
                "No stored bank fee notice carries that transfer reference, so a fee is "
                "not documented for this payment. A short payment alone does not imply one.",
                True,
            )
        )
    theme.chain_block(steps)


def _ordered_documents(detail: CaseDetail, recon: Reconciliation) -> list[DocumentSummary]:
    """Documents this case depends on come first, in the order the chain reads them.

    The remittance is what a reviewer opens first, then the notice it points at,
    so key documents keep chain order rather than sorting alphabetically.
    """
    chain = {value: index for index, value in enumerate(recon.key_document_ids)}
    cited: set[str] = set()
    for proposal in detail.proposals:
        cited.update(proposal.payload.evidence_document_ids)
    for correction in detail.corrections:
        cited.update(correction.cited_document_ids)
    preferred = {DocumentKind.REMITTANCE, DocumentKind.BANK_FEE_NOTICE}

    def sort_key(item: DocumentSummary) -> tuple[int, int, int, str]:
        return (
            0 if item.document_id in chain else 1,
            chain.get(item.document_id, 0),
            (0 if item.document_id in cited else 1) + (0 if item.kind in preferred else 1),
            item.document_id,
        )

    return sorted(detail.documents, key=sort_key)


def _document_option_label(item: DocumentSummary, recon: Reconciliation) -> str:
    mark = "* " if item.document_id in recon.key_document_ids else ""
    return f"{mark}{item.document_id} · {item.kind.value} · {item.title}"


def _handle_open_evidence(
    settings: svc.Settings, workspace_id: str, case_id: str, document_id: str
) -> None:
    st.session_state.mutation_in_progress = True
    try:
        opened = svc.open_evidence(
            settings, workspace_id, case_id, document_id, actor=svc.DEFAULT_CONTROLLER_ACTOR
        )
        st.session_state.last_detail_error = None
        st.session_state.last_opened_evidence = {
            "case_id": case_id,
            "document_id": opened.document_id,
            "sha256": opened.sha256,
        }
    except (svc.LearningError, svc.PersistenceError) as exc:
        code = getattr(exc, "code", "INTERNAL_ERROR")
        message = getattr(exc, "message", str(exc))
        st.session_state.last_detail_error = {
            "case_id": case_id,
            "code": code,
            "message": message,
        }
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


# --------------------------------------------------------------------------
# Decision tab
# --------------------------------------------------------------------------


def _render_decision(
    settings: svc.Settings,
    workspace_id: str,
    detail: CaseDetail,
    recon: Reconciliation,
) -> None:
    _render_resolution_hero(detail, recon)
    _render_validator_verdict(detail)
    _render_stored_application(detail)
    _render_latest_proposal(detail)
    _render_candidate_invoices(settings, workspace_id, detail, recon)
    review = _review_reason(settings, detail)
    if review:
        st.warning(review)
    _render_decision_trace(settings, detail)


def _render_resolution_hero(detail: CaseDetail, recon: Reconciliation) -> None:
    """The arithmetic, stated once, as the first thing in the tab."""
    decided = _decided_allocations(detail)
    payment = detail.snapshot.payment
    if decided is not None:
        allocations, stage, source_id = decided
        cash_total = sum(item.cash_cents for item in allocations)
        fee_total = sum(item.fee_cents for item in allocations)
        if fee_total > 0 and cash_total == payment.amount_cents and len(allocations) == 1:
            invoice_id = allocations[0].invoice_id
            theme.equation_block(
                title=f"{stage} resolution",
                terms=[
                    ("", cash_total, "received", payment.payment_id, "strong"),
                    ("+", fee_total, "documented bank fee", _fee_source(recon), "accent"),
                    ("=", cash_total + fee_total, "invoice balance", invoice_id, "strong"),
                ],
                footer=_equation_sentence(
                    cash_total, fee_total, payment.currency, recon, source_id, stage
                ),
            )
            return
        theme.equation_block(
            title=f"{stage} resolution",
            terms=[
                ("", cash_total, "cash applied", payment.payment_id, "strong"),
                ("+", fee_total, "fee recorded", _fee_source(recon), "default"),
                ("=", cash_total + fee_total, "settled against invoices", source_id, "strong"),
            ],
            footer=(
                f"{theme.money(cash_total)} cash and {theme.money(fee_total)} fee settle "
                f"{len(allocations)} invoice(s) in {payment.currency}."
            ),
        )
        return
    billed = recon.invoice_original_cents
    if billed is None:
        st.info(
            "This payment is not yet matched to an invoice, so there is no arithmetic to "
            "check. Establish the customer from evidence first."
        )
        return
    difference = recon.difference_cents
    if difference == 0:
        theme.equation_block(
            title="Open, amounts agree",
            terms=[
                ("", recon.received_cents, "received", payment.payment_id, "strong"),
                ("=", billed, "invoice balance", _candidate_label(recon), "strong"),
            ],
            footer="The amounts match. Nothing has been applied yet.",
        )
        return
    # More arrived than was billed, so the unknown is subtracted, not added.
    # Rendering "received + ? = invoice" for an overpayment would be arithmetic
    # a reviewer could not follow.
    operator, label = ("-", "overpaid") if difference < 0 else ("+", "unexplained")
    theme.equation_block(
        title="Open question",
        terms=[
            ("", recon.received_cents, "received", payment.payment_id, "strong"),
            (operator, None, label, "no accepted source", "accent"),
            ("=", billed, "invoice balance", _candidate_label(recon), "strong"),
        ],
        footer=recon.source or "",
        open_question=True,
    )


def _decided_allocations(detail: CaseDetail) -> tuple[list[Allocation], str, str] | None:
    """The most authoritative decision on this case: posted, then proposed, then saved."""
    if detail.application is not None:
        return list(detail.application.allocations), "Posted", detail.application.application_id
    if detail.proposals:
        proposal = detail.proposals[-1]
        return list(proposal.payload.allocations), "Proposed", proposal.proposal_id
    for correction in reversed(detail.corrections):
        verified = correction.verified_resolved_proposal
        if verified is not None:
            return list(verified.allocations), "Saved correction", correction.correction_id
    return None


def _fee_source(recon: Reconciliation) -> str:
    if recon.fee_notices:
        return recon.fee_notices[0].document_id
    return "no fee notice"


def _candidate_label(recon: Reconciliation) -> str:
    if not recon.candidates:
        return "no candidate"
    if len(recon.candidates) == 1:
        return recon.candidates[0].invoice_id
    return f"{len(recon.candidates)} invoices"


def _equation_sentence(
    cash_cents: int,
    fee_cents: int,
    currency: str,
    recon: Reconciliation,
    source_id: str,
    stage: str,
) -> str:
    """Plain prose for the equation, so an auditor can read it without the layout."""
    sentence = (
        f"{theme.money(cash_cents)} received + {theme.money(fee_cents)} documented bank fee "
        f"= {theme.money(cash_cents + fee_cents)} invoice balance ({currency})."
    )
    cited = recon_lib.fee_citation(list(recon.fee_notices), list(recon.link_refs))
    return f"{sentence} Fee {cited} {stage} under {source_id}."


def _render_validator_verdict(detail: CaseDetail) -> None:
    if not detail.proposals:
        return
    report = detail.proposals[-1].validation_report
    if report.valid:
        theme.verdict_block(
            "Validator accepted this proposal against the checked ledger revision.",
            issues=[],
            ok=True,
        )
        return
    theme.verdict_block(
        "Validator rejected this proposal. No money moved.",
        issues=[(issue.code.value, issue.message) for issue in report.issues],
        ok=False,
    )


def _render_stored_application(detail: CaseDetail) -> None:
    application = detail.application
    if application is None:
        return
    payment = detail.snapshot.payment
    theme.section(
        "Posted application",
        subtitle="Cash and fee are stored separately, so the fee never reads as revenue.",
    )
    theme.render_inline(
        theme.ident(application.application_id, label="application", width_ch=22),
        theme.quiet(f"actor {application.actor}"),
        theme.quiet(
            f"payment {payment.payment_id} is {'applied' if payment.applied else 'unapplied'}"
        ),
    )
    _render_allocations(application.allocations, evidence_ids=[])


def _render_latest_proposal(detail: CaseDetail) -> None:
    if not detail.proposals:
        if detail.application is None:
            theme.render_inline(
                theme.quiet("No stored proposal yet. A controller correction can still be saved.")
            )
        return
    proposal = detail.proposals[-1]
    theme.section(
        "Latest stored proposal",
        subtitle="What was proposed, and the documents it cites for each material decision.",
    )
    theme.render_inline(
        theme.ident(proposal.proposal_id, label="proposal", width_ch=22),
        theme.quiet(proposal.payload.resolution_type.value),
    )
    if _same_allocations(detail, proposal.payload.allocations):
        theme.body("Posted exactly as proposed, so the allocations are the table above.")
        st.caption(
            "Material decision cites "
            + ", ".join(f"`{item}`" for item in proposal.payload.evidence_document_ids)
            + "."
        )
        return
    _render_allocations(
        proposal.payload.allocations,
        evidence_ids=list(proposal.payload.evidence_document_ids),
    )


def _same_allocations(detail: CaseDetail, allocations: list[Allocation]) -> bool:
    """True when the posted application matches these allocations line for line."""
    if detail.application is None:
        return False
    return _allocation_key(list(detail.application.allocations)) == _allocation_key(allocations)


def _allocation_key(allocations: list[Allocation]) -> tuple[tuple[str, int, int], ...]:
    return tuple(sorted((item.invoice_id, item.cash_cents, item.fee_cents) for item in allocations))


def _render_allocations(allocations: list[Allocation], *, evidence_ids: list[str]) -> None:
    rows = [
        {
            "Invoice": item.invoice_id,
            "Cash": theme.money(item.cash_cents),
            "Fee": theme.money(item.fee_cents),
            "Cash + fee": theme.money(item.cash_cents + item.fee_cents),
        }
        for item in allocations
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    if evidence_ids:
        st.caption(
            "Material decision cites " + ", ".join(f"`{item}`" for item in evidence_ids) + "."
        )


def _render_candidate_invoices(
    settings: svc.Settings,
    workspace_id: str,
    detail: CaseDetail,
    recon: Reconciliation,
) -> None:
    """Only the invoices this payment could be settling, not the customer's whole ledger."""
    if recon.narrowed:
        total = len(detail.snapshot.invoices)
        subtitle = (
            f"Named by the evidence and by any stored decision. The payer's customer has "
            f"{total} open invoice(s) in this workspace; the rest are not candidates for "
            "this payment."
        )
    else:
        subtitle = (
            "No remittance establishes which invoices this payment settles, so every open "
            "invoice for the payer's customer is listed. Narrow this with evidence before "
            "proposing a resolution."
        )
    theme.section("Candidate invoices", subtitle=subtitle)
    if not recon.candidates:
        st.info("No candidate invoices are linked to this payment.")
        return
    labels = _customer_labels(settings, workspace_id, list(recon.candidates))
    rows = [
        {
            "Invoice": item.invoice_id,
            "Customer": f"{labels.get(item.customer_id, item.customer_id)} ({item.customer_id})",
            "Original": f"{theme.money(item.original_cents)} {item.currency}",
            "Current": f"{theme.money(item.outstanding_cents)} {item.currency}",
            "Status": item.display_status,
            "Named by": recon.origins.get(item.invoice_id, recon_lib.CUSTOMER_ORIGIN),
        }
        for item in recon.candidates
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    if detail.snapshot.summary.case_state is CaseState.RESOLVED and detail.application is not None:
        st.success(f"Sandbox application `{detail.application.application_id}` is stored.")


def _customer_labels(
    settings: svc.Settings, workspace_id: str, invoices: list[InvoiceRecord]
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for invoice in invoices:
        if invoice.customer_id in labels:
            continue
        labels[invoice.customer_id] = svc.get_customer_label(
            settings, workspace_id, invoice.customer_id
        )
    return labels


def _review_reason(settings: svc.Settings, detail: CaseDetail) -> str | None:
    run_id = None if detail.latest_run is None else detail.latest_run.run_id
    if run_id is None:
        return None
    for event in reversed(svc.list_trace_events(settings, run_id)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_kind.value != "review_requested":
            continue
        reason = payload.get("reason_code") or "REVIEW"
        message = payload.get("message") or "Review requested."
        return f"Review reason ({reason}): {message}"
    return None


def _render_decision_trace(settings: svc.Settings, detail: CaseDetail) -> None:
    with st.expander("Tool and lesson timeline", expanded=False):
        run_ids: list[str] = []
        if detail.latest_run is not None:
            run_ids.append(detail.latest_run.run_id)
        for correction in detail.corrections:
            if correction.run_id not in run_ids:
                run_ids.append(correction.run_id)
        if not run_ids:
            theme.render_inline(theme.quiet("No persisted run is attached to this case yet."))
            return
        for run_id in run_ids:
            st.caption(f"Run `{run_id}`")
            theme.trace_timeline(svc.list_trace_events(settings, run_id))


# --------------------------------------------------------------------------
# Teach tab: saved corrections
# --------------------------------------------------------------------------


def _render_saved_corrections(settings: svc.Settings, detail: CaseDetail) -> None:
    theme.section(
        "Saved corrections",
        subtitle="A stored controller explanation. Saving it posted no money by itself.",
    )
    if not detail.corrections:
        theme.render_inline(theme.quiet("No controller correction is stored for this case yet."))
        return
    payment_applied = detail.snapshot.payment.applied
    in_progress = bool(st.session_state.mutation_in_progress)
    missing = svc.missing_live_variable_names(settings)
    for correction in detail.corrections:
        _render_one_correction(
            settings,
            correction,
            payment_applied=payment_applied,
            in_progress=in_progress,
            missing_live=missing,
        )


def _render_one_correction(
    settings: svc.Settings,
    correction: CorrectionRecord,
    *,
    payment_applied: bool,
    in_progress: bool,
    missing_live: tuple[str, ...],
) -> None:
    with st.container(border=True, key=f"pc-panel-correction-{correction.correction_id}"):
        theme.render_inline(
            theme.provenance_badge(ExecutionMode.HUMAN),
            theme.ident(correction.correction_id, label="correction", width_ch=22),
            theme.quiet(f"actor {correction.actor}"),
        )
        theme.body(correction.text)
        st.caption(
            "Cited evidence: "
            + ", ".join(f"`{item}`" for item in correction.cited_document_ids)
            + f" · lesson eligible: {str(correction.lesson_eligibility).lower()}"
        )
        if correction.eligibility_reason:
            theme.render_inline(theme.quiet(correction.eligibility_reason))
        if correction.verified_resolved_proposal is not None:
            proposal = correction.verified_resolved_proposal
            theme.card_title("Verified corrected proposal", small=True)
            _render_allocations(
                proposal.allocations, evidence_ids=list(proposal.evidence_document_ids)
            )
            equation = _fee_equation_from_allocations(
                proposal.allocations,
                sum(item.cash_cents for item in proposal.allocations),
                "USD",
            )
            if equation:
                theme.body(equation)
        apply_disabled, apply_help = _apply_button_state(
            correction, payment_applied=payment_applied, in_progress=in_progress
        )
        propose_disabled, propose_help = _propose_button_state(
            correction, missing_live=missing_live, in_progress=in_progress
        )
        apply_col, propose_col = st.columns(2)
        with apply_col:
            apply_clicked = st.button(
                "Apply corrected resolution",
                key=f"apply_correction_{correction.correction_id}",
                disabled=apply_disabled,
                help=apply_help,
                icon=":material/account_balance:",
            )
        with propose_col:
            propose_clicked = st.button(
                "Propose reusable lesson",
                key=f"propose_lesson_{correction.correction_id}",
                disabled=propose_disabled,
                help=propose_help,
                icon=":material/school:",
            )
        for lesson in svc.list_lessons_for_correction(settings, correction.correction_id):
            _render_draft_lesson(lesson)
    if apply_clicked and not apply_disabled:
        _handle_apply_correction(settings, correction)
        return
    if propose_clicked and not propose_disabled:
        _handle_propose_lesson(settings, correction)


def _apply_button_state(
    correction: CorrectionRecord, *, payment_applied: bool, in_progress: bool
) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if correction.verified_resolved_proposal is None:
        return True, "This correction has no verified proposal to apply."
    if payment_applied:
        return True, "Resolved payment. Applying again cannot post additional funds."
    return False, "Validate and apply the stored human proposal. This does not activate a lesson."


def _propose_button_state(
    correction: CorrectionRecord, *, missing_live: tuple[str, ...], in_progress: bool
) -> tuple[bool, str]:
    if in_progress:
        return True, "Another mutation is already in progress."
    if missing_live:
        return True, "Chargeable lesson draft disabled because " + ", ".join(missing_live) + "."
    if not correction.lesson_eligibility or correction.verified_resolved_proposal is None:
        reason = correction.eligibility_reason or "No supported lesson for this correction."
        return True, reason
    return False, "Ask the live compiler for a DRAFT lookup hint. This does not activate a lesson."


def _render_draft_lesson(lesson: PrecedentRecord) -> None:
    hint = lesson.hint
    st.info(
        f"Draft `{lesson.precedent_id}` v{lesson.version} is {lesson.status.value}. "
        "This is an investigation hint, not a financial approval."
    )
    theme.card_title(hint.title, small=True)
    theme.body(hint.summary)
    st.caption(
        f"Scope `{hint.scope.customer_id}` / `{hint.scope.bank_account_id}` / "
        f"{hint.scope.currency} / {hint.scope.channel.value}"
    )


def _handle_apply_correction(settings: svc.Settings, correction: CorrectionRecord) -> None:
    st.session_state.mutation_in_progress = True
    try:
        result = svc.apply_correction(
            settings,
            correction.correction_id,
            idempotency_key=svc.correction_apply_key(correction.correction_id),
            actor=svc.DEFAULT_CONTROLLER_ACTOR,
        )
        st.session_state.last_detail_error = None
        st.session_state.last_apply_result = {
            "case_id": correction.case_id,
            "correction_id": correction.correction_id,
            "status": result.status.value,
            "application_id": result.application_id,
            "issues": [issue.message for issue in result.issues],
        }
    except (svc.LearningError, svc.PersistenceError) as exc:
        code = getattr(exc, "code", "INTERNAL_ERROR")
        message = getattr(exc, "message", str(exc))
        st.session_state.last_detail_error = {
            "case_id": correction.case_id,
            "code": code,
            "message": message,
        }
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _handle_propose_lesson(settings: svc.Settings, correction: CorrectionRecord) -> None:
    reason = svc.live_readiness_code(settings)
    if reason is not None:
        missing = svc.missing_live_variable_names(settings)
        st.session_state.last_detail_error = {
            "case_id": correction.case_id,
            "code": reason,
            "message": "Live lesson draft is not configured. Missing: " + ", ".join(missing) + ".",
        }
        return
    st.session_state.mutation_in_progress = True
    try:
        result = svc.propose_lesson(settings, correction.correction_id, mode=ExecutionMode.LIVE)
        st.session_state.last_detail_error = None
        st.session_state.last_lesson_result = {
            "case_id": correction.case_id,
            "correction_id": result.correction_id,
            "status": result.status.value,
            "precedent_id": result.precedent_id,
            "reason": result.reason,
        }
        if result.precedent_id is None:
            st.session_state.last_detail_error = {
                "case_id": correction.case_id,
                "code": result.status.value,
                "message": result.reason,
            }
    except (svc.LearningError, svc.PersistenceError, svc.ProviderError) as exc:
        code = getattr(exc, "code", "INTERNAL_ERROR")
        if hasattr(code, "value"):
            code = code.value
        message = getattr(exc, "message", str(exc))
        st.session_state.last_detail_error = {
            "case_id": correction.case_id,
            "code": code,
            "message": message,
        }
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


# --------------------------------------------------------------------------
# Teach tab: the correction form
# --------------------------------------------------------------------------


def _render_correction_form(
    settings: svc.Settings,
    workspace_id: str,
    case_id: str,
    detail: CaseDetail,
    recon: Reconciliation,
) -> None:
    """Reconcile the numbers reactively, then commit the narrative through a form.

    The allocation fields stay outside ``st.form`` on purpose. They drive the
    arithmetic preview, and a preview that cannot update until submit would be
    worse than no preview. The two long free-text fields are inside the form,
    which is where suppressing reruns actually helps. See DECISIONS D006.
    """
    payment = detail.snapshot.payment
    resolved = payment.applied
    theme.section(
        "Controller correction",
        subtitle=(
            "This payment is already applied. You can save an explanatory correction "
            "without posting money again."
            if resolved
            else "Save, apply, and propose are separate actions. Saving does not apply "
            "funds. Applying does not activate a lesson."
        ),
    )
    _apply_example_prefill(case_id)
    example_ok = _example_available(detail, recon)
    example_help = (
        "Prefills the Harbor teaching explanation and fee allocation. "
        "This is not a model-generated discovery and does not submit."
        if example_ok
        else "Example correction is only for the Harbor fee teaching case."
    )
    if st.button(
        "Use example correction",
        key=f"use_example_correction_{case_id}",
        disabled=not example_ok,
        help=example_help,
        icon=":material/auto_fix_high:",
    ):
        st.session_state[f"prefill_example_{case_id}"] = True
        st.rerun()
        return
    _render_inspection_note(case_id)
    cited = st.multiselect(
        "Source documents",
        options=[item.document_id for item in _ordered_documents(detail, recon)],
        key=f"correction_docs_{case_id}",
        help="Select 1-10 source IDs the controller inspected.",
    )
    include_key = f"include_resolution_{case_id}"
    if include_key not in st.session_state:
        st.session_state[include_key] = not resolved
    include_resolution = st.checkbox("Include a corrected resolution", key=include_key)
    proposal: ResolutionProposal | None = None
    proposal_error: str | None = None
    if include_resolution:
        proposal, proposal_error = _render_resolution_fields(case_id, recon, payment, cited)
        _render_arithmetic_preview(payment, recon, proposal, proposal_error, resolved=resolved)
    in_progress = bool(st.session_state.mutation_in_progress)
    with st.form(key=f"correction_form_{case_id}", border=True):
        theme.section(
            "Controller explanation",
            subtitle=(
                "Describe the investigation path a reviewer would repeat. Typing here does "
                "not rerun the page."
            ),
        )
        text = st.text_area(
            "Correction text",
            max_chars=2000,
            key=f"correction_text_{case_id}",
            help="1-2,000 characters. Describe the investigation path, not a write-off policy.",
        )
        if include_resolution:
            st.text_area(
                "Proposal explanation",
                max_chars=1000,
                key=f"proposal_explanation_{case_id}",
            )
        submitted = st.form_submit_button(
            "Save correction",
            type="primary",
            key=f"save_correction_{case_id}",
            icon=":material/save:",
            disabled=in_progress,
            help="Save the correction without applying funds or drafting a lesson.",
        )
    if not submitted or in_progress:
        return
    blocker = _save_blocker(
        text=text,
        cited=cited,
        include_resolution=include_resolution,
        proposal=proposal,
        proposal_error=proposal_error,
    )
    if blocker:
        st.error(blocker)
        return
    _handle_save_correction(
        settings,
        workspace_id,
        case_id,
        text=text,
        cited=cited,
        proposal=proposal if include_resolution else None,
    )


def _render_inspection_note(case_id: str) -> None:
    opened = st.session_state.get("last_opened_evidence")
    if isinstance(opened, dict) and opened.get("case_id") == case_id:
        st.caption(
            f"Recorded controller inspection of `{opened.get('document_id')}` "
            f"(hash `{str(opened.get('sha256') or '')[:12]}`)."
        )


def _apply_example_prefill(case_id: str) -> None:
    if not st.session_state.pop(f"prefill_example_{case_id}", False):
        return
    st.session_state[f"correction_text_{case_id}"] = HARBOR_EXAMPLE_TEXT
    st.session_state[f"correction_docs_{case_id}"] = [svc.T03_REMIT_ID, svc.T03_FEE_ID]
    st.session_state[f"include_resolution_{case_id}"] = True
    st.session_state[f"resolution_type_{case_id}"] = ResolutionType.SINGLE_WITH_BANK_FEE.value
    st.session_state[f"proposal_explanation_{case_id}"] = HARBOR_EXAMPLE_EXPLANATION
    st.session_state[f"alloc_invoice_0_{case_id}"] = svc.T03_INVOICE_ID
    st.session_state[f"alloc_cash_0_{case_id}"] = HARBOR_EXAMPLE_CASH
    st.session_state[f"alloc_fee_0_{case_id}"] = HARBOR_EXAMPLE_FEE


def _example_available(detail: CaseDetail, recon: Reconciliation) -> bool:
    ids = {item.document_id for item in detail.documents}
    invoice_ids = {item.invoice_id for item in recon.candidates}
    return (
        detail.snapshot.payment.payment_id == svc.T03_PAYMENT_ID
        and svc.T03_REMIT_ID in ids
        and svc.T03_FEE_ID in ids
        and svc.T03_INVOICE_ID in invoice_ids
    )


def _render_resolution_fields(
    case_id: str,
    recon: Reconciliation,
    payment: PaymentRecord,
    cited: list[str],
) -> tuple[ResolutionProposal | None, str | None]:
    invoice_ids = [item.invoice_id for item in recon.candidates]
    if not invoice_ids:
        return None, "No candidate invoices are available for a manual resolution."
    resolution_type = st.selectbox(
        "Resolution type",
        options=[item.value for item in ResolutionType],
        key=f"resolution_type_{case_id}",
    )
    row_count = 3 if resolution_type == ResolutionType.EXACT_BUNDLE.value else 1
    by_id = {item.invoice_id: item for item in recon.candidates}
    built: list[Allocation] = []
    parse_error = None
    empty_hint = False
    for index in range(row_count):
        cols = st.columns(3)
        with cols[0]:
            invoice_id = st.selectbox(
                f"Invoice {index + 1}",
                options=invoice_ids,
                key=f"alloc_invoice_{index}_{case_id}",
            )
        with cols[1]:
            cash_raw = st.text_input(
                f"Cash {index + 1} (decimal USD)",
                key=f"alloc_cash_{index}_{case_id}",
                placeholder="9965.00",
            )
        with cols[2]:
            fee_placeholder = (
                "35.00" if resolution_type == ResolutionType.SINGLE_WITH_BANK_FEE.value else "0.00"
            )
            fee_raw = st.text_input(
                f"Fee {index + 1} (decimal USD)",
                key=f"alloc_fee_{index}_{case_id}",
                placeholder=fee_placeholder,
            )
        if index > 0 and not (cash_raw or "").strip():
            continue
        cash_cents, cash_error = _parse_money_field(cash_raw, empty_ok=False)
        fee_default_zero = resolution_type != ResolutionType.SINGLE_WITH_BANK_FEE.value
        fee_cents, fee_error = _parse_money_field(
            fee_raw, empty_ok=fee_default_zero, default_zero=fee_default_zero
        )
        if cash_error or fee_error:
            parse_error = cash_error or fee_error
            continue
        if cash_cents is None or fee_cents is None:
            empty_hint = True
            continue
        try:
            built.append(
                Allocation(invoice_id=invoice_id, cash_cents=cash_cents, fee_cents=fee_cents)
            )
        except ValidationError as exc:
            parse_error = _validation_message(exc)
    if parse_error:
        st.error(parse_error)
        return None, parse_error
    if empty_hint or not built:
        theme.render_inline(theme.quiet("Enter cash and fee as decimal text such as 9965.00."))
        return None, None
    customer_ids = {by_id[item.invoice_id].customer_id for item in built}
    if len(customer_ids) != 1:
        message = "A corrected resolution must name invoices for one customer."
        st.error(message)
        return None, message
    if not cited:
        return None, "Select source documents before previewing a resolution."
    explanation = st.session_state.get(f"proposal_explanation_{case_id}")
    try:
        proposal = ResolutionProposal(
            payment_id=payment.payment_id,
            customer_id=next(iter(customer_ids)),
            resolution_type=ResolutionType(resolution_type),
            allocations=built,
            evidence_document_ids=list(cited),
            explanation=(explanation or "").strip() or "Controller-corrected resolution.",
            precedent_ids_used=[],
        )
    except ValidationError as exc:
        message = _validation_message(exc)
        st.error(message)
        return None, message
    return proposal, None


def _parse_money_field(
    raw: str, *, empty_ok: bool, default_zero: bool = False
) -> tuple[int | None, str | None]:
    text = (raw or "").strip()
    if not text:
        if empty_ok and default_zero:
            return 0, None
        return None, None
    try:
        return parse_decimal_cents(text), None
    except ValueError as exc:
        return None, str(exc)


def _render_arithmetic_preview(
    payment: PaymentRecord,
    recon: Reconciliation,
    proposal: ResolutionProposal | None,
    proposal_error: str | None,
    *,
    resolved: bool,
) -> None:
    """Show the same three-term arithmetic as the header, for the draft numbers."""
    if proposal is None:
        if proposal_error:
            theme.render_inline(
                theme.quiet("Fix the allocation fields to preview arithmetic and validation.")
            )
        return
    cash_total = sum(item.cash_cents for item in proposal.allocations)
    fee_total = sum(item.fee_cents for item in proposal.allocations)
    if len(proposal.allocations) == 1:
        settles = proposal.allocations[0].invoice_id
    else:
        settles = _candidate_label(recon)
    if resolved:
        title = "Draft only, this payment is already applied"
        footer = (
            f"Payment received {theme.money(payment.amount_cents)} {payment.currency} and is "
            "already applied. Saving another correction records an explanation and posts "
            "no further funds."
        )
    else:
        title = "Preview, not yet saved"
        footer = (
            f"Payment received {theme.money(payment.amount_cents)} {payment.currency}. "
            "Nothing is saved or posted until you save the correction."
        )
    theme.equation_block(
        title=title,
        terms=[
            ("", cash_total, "cash", payment.payment_id, "strong"),
            ("+", fee_total, "fee", _fee_source(recon), "default"),
            ("=", cash_total + fee_total, "settles", settles, "strong"),
        ],
        footer=footer,
    )
    by_id = {item.invoice_id: item for item in recon.candidates}
    for item in proposal.allocations:
        invoice = by_id.get(item.invoice_id)
        if invoice is None:
            continue
        st.caption(
            f"`{item.invoice_id}` current {theme.money(invoice.outstanding_cents)} · "
            f"cash + fee {theme.money(item.cash_cents + item.fee_cents)}"
        )
    if cash_total != payment.amount_cents:
        st.warning("Cash allocations must equal the payment amount before apply can succeed.")


def _save_blocker(
    *,
    text: str | None,
    cited: list[str] | None,
    include_resolution: bool,
    proposal: ResolutionProposal | None,
    proposal_error: str | None,
) -> str | None:
    """Why this submission cannot be saved, or None when it can.

    Checked after submit rather than by disabling the button: inside a form the
    typed values are not visible until submission, so a value-driven disable
    would lock the user out of their own input.
    """
    if not (text or "").strip():
        return "Correction text is required."
    if not cited:
        return "Select at least one source document."
    if include_resolution and proposal is None:
        return proposal_error or "Corrected resolution is incomplete or invalid."
    return None


def _handle_save_correction(
    settings: svc.Settings,
    workspace_id: str,
    case_id: str,
    *,
    text: str,
    cited: list[str],
    proposal: ResolutionProposal | None,
) -> None:
    st.session_state.mutation_in_progress = True
    try:
        payload: dict[str, object] = {
            "text": text.strip(),
            "evidence_document_ids": list(cited),
            "corrected_proposal": (None if proposal is None else proposal.model_dump(mode="json")),
            "review_reason_code": None,
        }
        record = svc.save_correction(
            settings, workspace_id, case_id, payload, actor=svc.DEFAULT_CONTROLLER_ACTOR
        )
        st.session_state.last_detail_error = None
        st.session_state.last_saved_correction_id = record.correction_id
    except (svc.LearningError, svc.PersistenceError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            code = "INVALID_INPUT"
            message = _validation_message(exc)
        else:
            code = getattr(exc, "code", "INTERNAL_ERROR")
            message = getattr(exc, "message", str(exc))
        st.session_state.last_detail_error = {
            "case_id": case_id,
            "code": code,
            "message": message,
        }
    finally:
        st.session_state.mutation_in_progress = False
    st.rerun()


def _fee_equation_from_allocations(
    allocations: list[Allocation], payment_cents: int, currency: str
) -> str | None:
    if len(allocations) != 1:
        return None
    item = allocations[0]
    if item.fee_cents <= 0:
        return None
    if item.cash_cents != payment_cents:
        return None
    invoice_cents = item.cash_cents + item.fee_cents
    return (
        f"{theme.money(item.cash_cents)} received + "
        f"{theme.money(item.fee_cents)} documented bank fee = "
        f"{theme.money(invoice_cents)} invoice balance ({currency})"
    )


def _validation_message(exc: ValidationError | Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            item = errors[0]
            loc = ".".join(str(part) for part in item.get("loc", ()))
            msg = item.get("msg") or "invalid input"
            return f"{loc}: {msg}" if loc else str(msg)
    return str(exc)
