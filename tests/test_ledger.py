from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from precedent.config import detect_repo_root
from precedent.db import (
    get_application,
    get_case,
    get_invoice,
    get_payment,
    get_run,
    insert_case,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_run,
    insert_workspace,
    list_invoices,
    open_database,
)
from precedent.evidence import read_document
from precedent.fixtures import (
    CUST_CEDAR,
    CUST_HARBOR,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    load_manifest,
    load_package,
)
from precedent.ledger import apply_proposal, get_case_snapshot, store_proposal, validate_proposal
from precedent.models import (
    SCHEMA_VERSION,
    ApplicationStatus,
    BankFeeNoticeFacts,
    BudgetLimits,
    CaseRecord,
    CaseState,
    CustomerRecord,
    DocumentKind,
    ExecutionMode,
    InvoiceRecord,
    InvoiceSourceStatus,
    MemorySnapshot,
    OpenedEvidence,
    PaymentChannel,
    PaymentRecord,
    RemittanceFacts,
    ResolutionType,
    RunContext,
    RunRecord,
    RunState,
    SourceDocument,
    ValidationCode,
    WorkspaceRecord,
    hash_model,
)
from precedent.policies import (
    ALLOWED_CASH_ACCOUNT_ID,
    MAX_RECEIVING_WIRE_FEE_CENTS,
    NORTHSTAR_POLICY,
    POLICY_ID,
    SUPPORTED_FEE_TYPE,
    northstar_policy,
)

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
WS = "WS-LEDGER-1"
T03_CASE_ID = "CASE-1CFA9FEE848F"
MANIFEST = load_manifest(detect_repo_root() / "data" / "manifests" / "fixtures-seed-42.json")


def _open(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "precedent.sqlite3")


def _context(
    workspace_id: str = WS,
    *,
    case_id: str = T03_CASE_ID,
    company_id: str = "NORTHSTAR",
    policy_id: str = POLICY_ID,
    revision: int = 0,
    opened: list[OpenedEvidence] | None = None,
    precedents: list[str] | None = None,
) -> RunContext:
    return RunContext(
        workspace_id=workspace_id,
        case_id=case_id,
        run_id="RUN-LEDGER-1",
        company_id=company_id,
        policy_id=policy_id,
        policy_hash=HASH_A,
        dataset_hash=HASH_A,
        initial_ledger_revision=revision,
        execution_mode=ExecutionMode.TEST,
        actor="investigator",
        opened_documents=opened or [],
        retrieved_precedent_version_ids=precedents or [],
        memory_snapshot=MemorySnapshot(snapshot_hash=HASH_A),
        budgets=BudgetLimits(
            max_model_calls=10,
            max_tool_calls=30,
            max_case_seconds=120,
            request_timeout_seconds=30,
            max_output_tokens=1500,
        ),
    )


def _manifest(authoring_id: str):
    return next(item for item in MANIFEST.cases if item.authoring_id == authoring_id)


def _import_authoring(connection: sqlite3.Connection, authoring_id: str, workspace_id: str = WS):
    entry = _manifest(authoring_id)
    package = load_package(detect_repo_root() / "data" / entry.package_relpath)
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name=f"{authoring_id} validation",
            company_id=package.company.company_id,
            period_label=package.company.period_start[:7],
            period_start=package.company.period_start,
            period_end=package.company.period_end,
            dataset_hash=HASH_A,
            policy=package.policy,
            policy_hash=hash_model(package.policy),
            ledger_revision=0,
            schema_version=SCHEMA_VERSION,
            created_at=CREATED,
        ),
    )
    for customer in package.customers:
        insert_customer(
            connection,
            CustomerRecord(
                workspace_id=workspace_id,
                customer_id=customer.customer_id,
                legal_name=customer.legal_name,
                display_name=customer.display_name,
            ),
        )
    for invoice in package.invoices:
        insert_invoice(
            connection,
            InvoiceRecord(
                workspace_id=workspace_id,
                invoice_id=invoice.invoice_id,
                customer_id=invoice.customer_id,
                currency=invoice.currency,
                issued_date=invoice.issued_date,
                due_date=invoice.due_date,
                original_cents=invoice.original_cents,
                opening_outstanding_cents=invoice.opening_outstanding_cents,
                outstanding_cents=invoice.opening_outstanding_cents,
                status=invoice.status,
            ),
        )
    for payment in package.payments:
        insert_payment(
            connection,
            PaymentRecord(
                workspace_id=workspace_id,
                payment_id=payment.payment_id,
                bank_transaction_id=payment.bank_transaction_id,
                bank_account_id=payment.bank_account_id,
                posted_date=payment.posted_date,
                currency=payment.currency,
                amount_cents=payment.amount_cents,
                channel=payment.channel,
                payer_text=payment.payer_text,
                bank_reference=payment.bank_reference,
                applied=False,
            ),
        )
    for document in package.documents:
        insert_document(connection, document.to_persisted(workspace_id))
    insert_case(
        connection,
        CaseRecord(
            workspace_id=workspace_id,
            case_id=package.case_id,
            payment_id=package.payments[0].payment_id,
            state=CaseState.OPEN,
        ),
    )
    return package, entry


def _doc(package, kind: DocumentKind):
    return next(item for item in package.documents if item.kind is kind)


def _open_named(context: RunContext, package, *document_ids: str) -> RunContext:
    by_id = {
        item.document_id: item.to_persisted(context.workspace_id) for item in package.documents
    }
    opened = list(context.opened_documents)
    for document_id in document_ids:
        document = by_id[document_id]
        opened.append(OpenedEvidence(document_id=document.document_id, sha256=document.sha256))
    return context.model_copy(update={"opened_documents": opened})


def _exact_payload(package, *, resolution_type: ResolutionType) -> dict[str, object]:
    remittance = _doc(package, DocumentKind.REMITTANCE)
    invoices = {item.invoice_id: item for item in package.invoices}
    payment = package.payments[0]
    facts = remittance.facts
    assert isinstance(facts, RemittanceFacts)
    return {
        "payment_id": payment.payment_id,
        "customer_id": facts.customer_id,
        "resolution_type": resolution_type.value,
        "allocations": [
            {
                "invoice_id": invoice_id,
                "cash_cents": invoices[invoice_id].opening_outstanding_cents,
                "fee_cents": 0,
            }
            for invoice_id in facts.invoice_ids
        ],
        "evidence_document_ids": [remittance.document_id],
        "explanation": "Remittance names the selected invoices for full cash settlement.",
        "precedent_ids_used": [],
    }


def _snapshot(connection: sqlite3.Connection, workspace_id: str = WS) -> dict[str, object]:
    invoices = tuple(
        (item.invoice_id, item.outstanding_cents, item.status.value)
        for item in list_invoices(connection, workspace_id)
    )
    payments = tuple(
        connection.execute(
            """
            SELECT payment_id, applied, amount_cents
            FROM payments WHERE workspace_id = ? ORDER BY payment_id
            """,
            (workspace_id,),
        ).fetchall()
    )
    applications = connection.execute(
        "SELECT COUNT(*) FROM applications WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]
    allocations = connection.execute(
        "SELECT COUNT(*) FROM allocations WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]
    workspace = connection.execute(
        "SELECT ledger_revision FROM workspaces WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return {
        "revision": None if workspace is None else workspace[0],
        "invoices": invoices,
        "payments": payments,
        "applications": applications,
        "allocations": allocations,
        "total_changes": connection.total_changes,
    }


def _codes(report) -> list[ValidationCode]:
    return [issue.code for issue in report.issues]


def _validate_unchanged(connection, context, payload, *, workspace_id: str = WS):
    before = _snapshot(connection, workspace_id)
    report = validate_proposal(connection, context, payload)
    after = _snapshot(connection, workspace_id)
    assert after == before
    return report


def test_import_ledger_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import precedent.ledger as ledger

    assert callable(ledger.validate_proposal)
    assert callable(ledger.apply_proposal)
    assert callable(ledger.get_case_snapshot)
    assert callable(ledger.store_proposal)
    assert "apply_proposal" in ledger.__all__


def test_northstar_policy_is_fixed_company_policy() -> None:
    policy = northstar_policy()
    assert policy == NORTHSTAR_POLICY
    assert policy.policy_id == "northstar-usd-v1"
    assert policy.company_id == "NORTHSTAR"
    assert policy.allowed_cash_account_id == ALLOWED_CASH_ACCOUNT_ID
    assert policy.currency == "USD"
    assert policy.max_receiving_wire_fee_cents == MAX_RECEIVING_WIRE_FEE_CENTS == 5000
    assert policy.allow_receiving_wire_fee is True
    assert policy.allow_partial_settlement is False
    assert policy.allow_writeoff is False
    assert policy.require_remittance is True
    assert SUPPORTED_FEE_TYPE == "RECEIVING_WIRE_FEE"


def test_t01_exact_single_validates_without_mutation(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T01")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    context = _open_named(_context(case_id=entry.case_id), package, remittance.document_id)
    payload = _exact_payload(package, resolution_type=ResolutionType.EXACT_SINGLE)
    report = _validate_unchanged(connection, context, payload)
    assert report.valid is True
    assert report.issues == []
    assert report.policy_id == POLICY_ID
    assert report.checked_ledger_revision == 0
    assert report.normalized_proposal is not None
    assert report.normalized_proposal.allocations[0].cash_cents == 120_000
    assert report.normalized_proposal.allocations[0].fee_cents == 0
    connection.close()


def test_t02_and_t05_exact_bundles_validate(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    for authoring_id, expected_cash in (
        ("T02", (65_000, 35_000)),
        ("T05", (15_000, 27_000, 18_000)),
    ):
        package, entry = _import_authoring(connection, authoring_id, workspace_id=authoring_id)
        remittance = _doc(package, DocumentKind.REMITTANCE)
        context = _open_named(
            _context(workspace_id=authoring_id, case_id=entry.case_id),
            package,
            remittance.document_id,
        )
        payload = _exact_payload(package, resolution_type=ResolutionType.EXACT_BUNDLE)
        report = _validate_unchanged(connection, context, payload, workspace_id=authoring_id)
        assert report.valid is True, _codes(report)
        cash = tuple(item.cash_cents for item in report.normalized_proposal.allocations)
        assert cash == expected_cash
        assert all(item.fee_cents == 0 for item in report.normalized_proposal.allocations)
    connection.close()


def test_t03_documented_fee_validates_after_opening_evidence(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, _entry = _import_authoring(connection, "T03")
    context = _context(case_id=T03_CASE_ID)
    assert T03_PAYMENT_ID == "PAY-201"
    assert T03_INVOICE_ID == "INV-1042"
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    assert payment is not None
    assert payment.amount_cents == 996_500

    opened_remit = read_document(connection, context, {"document_id": T03_REMIT_ID})
    opened_fee = read_document(connection, context, {"document_id": T03_FEE_ID})
    assert opened_remit.ok is True
    assert opened_fee.ok is True
    assert opened_fee.data["facts"]["fee_cents"] == 3500
    assert opened_fee.data["facts"]["fee_type"] == SUPPORTED_FEE_TYPE

    payload = {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": (
            "The remittance identifies INV-1042; ticket ST-8721 matches the bank fee notice."
        ),
        "precedent_ids_used": [],
    }
    report = _validate_unchanged(connection, context, payload)
    assert report.valid is True, _codes(report)
    assert report.normalized_proposal is not None
    assert report.normalized_proposal.allocations[0].cash_cents == 996_500
    assert report.normalized_proposal.allocations[0].fee_cents == 3500
    assert T03_REMIT_ID in payload["evidence_document_ids"]
    assert package is not None
    connection.close()


def test_t03_search_hit_without_open_cannot_validate(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_authoring(connection, "T03")
    context = _context()
    payload = {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": "Cited documents were only search hits, not opened evidence.",
        "precedent_ids_used": [],
    }
    report = _validate_unchanged(connection, context, payload)
    assert report.valid is False
    assert report.normalized_proposal is None
    assert ValidationCode.EVIDENCE_NOT_OPENED in _codes(report)
    connection.close()


def test_t06_disputed_same_gap_cannot_clear_as_fee(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T06")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    unrelated = _doc(package, DocumentKind.BANK_FEE_NOTICE)
    invoice_id = remittance.facts.invoice_ids[0]
    context = _open_named(
        _context(case_id=entry.case_id),
        package,
        remittance.document_id,
        unrelated.document_id,
    )
    payload = {
        "payment_id": package.payments[0].payment_id,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
        "allocations": [{"invoice_id": invoice_id, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [remittance.document_id, unrelated.document_id],
        "explanation": "Treat the 3500 gap as a bank fee using an unrelated notice.",
        "precedent_ids_used": [],
    }
    report = _validate_unchanged(connection, context, payload)
    assert report.valid is False
    assert report.normalized_proposal is None
    assert ValidationCode.OPEN_DISPUTE in _codes(report)
    connection.close()


def test_wrong_customer_and_currency_fail(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T03")
    cedar = next(item for item in package.invoices if item.customer_id == CUST_CEDAR)
    context = _open_named(_context(case_id=entry.case_id), package, T03_REMIT_ID, T03_FEE_ID)
    customer_report = _validate_unchanged(
        connection,
        context,
        {
            "payment_id": T03_PAYMENT_ID,
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
            "allocations": [
                {"invoice_id": cedar.invoice_id, "cash_cents": 996_500, "fee_cents": 3500}
            ],
            "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
            "explanation": "Allocate Harbor's payment to a Cedar invoice.",
            "precedent_ids_used": [],
        },
    )
    assert customer_report.valid is False
    assert ValidationCode.CUSTOMER_MISMATCH in _codes(customer_report)

    _seed_currency_mismatch(connection, "WS-EUR")
    eur_context = _open_named(
        _context(workspace_id="WS-EUR", case_id="CASE-EUR"),
        _currency_package(),
        "DOC-R-EUR",
    )
    currency_report = _validate_unchanged(
        connection,
        eur_context,
        {
            "payment_id": "PAY-EUR",
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.EXACT_SINGLE.value,
            "allocations": [{"invoice_id": "INV-EUR", "cash_cents": 145_000, "fee_cents": 0}],
            "evidence_document_ids": ["DOC-R-EUR"],
            "explanation": "EUR invoice cannot be converted.",
            "precedent_ids_used": [],
        },
        workspace_id="WS-EUR",
    )
    assert currency_report.valid is False
    assert ValidationCode.UNSUPPORTED_CURRENCY in _codes(currency_report)
    assert ValidationCode.CURRENCY_MISMATCH in _codes(currency_report)
    connection.close()


def test_over_allocation_missing_fee_negative_and_duplicate(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T09")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    invoice_id = remittance.facts.invoice_ids[0]
    context = _open_named(_context(case_id=entry.case_id), package, remittance.document_id)
    over = _validate_unchanged(
        connection,
        context,
        {
            "payment_id": package.payments[0].payment_id,
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.EXACT_SINGLE.value,
            "allocations": [{"invoice_id": invoice_id, "cash_cents": 121_000, "fee_cents": 0}],
            "evidence_document_ids": [remittance.document_id],
            "explanation": "Apply an overpayment as if it cleared the invoice.",
            "precedent_ids_used": [],
        },
    )
    assert over.valid is False
    assert ValidationCode.UNSUPPORTED_OVERPAYMENT in _codes(over)

    t03_package, t03_entry = _import_authoring(connection, "T03", workspace_id="WS-T03")
    t03_context = _open_named(
        _context(workspace_id="WS-T03", case_id=t03_entry.case_id),
        t03_package,
        T03_REMIT_ID,
    )
    missing_fee = _validate_unchanged(
        connection,
        t03_context,
        {
            "payment_id": T03_PAYMENT_ID,
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
            "allocations": [
                {"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}
            ],
            "evidence_document_ids": [T03_REMIT_ID],
            "explanation": "Fee case without the bank notice.",
            "precedent_ids_used": [],
        },
        workspace_id="WS-T03",
    )
    assert missing_fee.valid is False
    assert ValidationCode.MISSING_FEE_NOTICE in _codes(missing_fee)

    negative = _validate_unchanged(
        connection,
        context,
        {
            "payment_id": package.payments[0].payment_id,
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.EXACT_SINGLE.value,
            "allocations": [{"invoice_id": invoice_id, "cash_cents": -1, "fee_cents": 0}],
            "evidence_document_ids": [remittance.document_id],
            "explanation": "Negative cash is not a valid amount.",
            "precedent_ids_used": [],
        },
    )
    assert negative.valid is False
    assert _codes(negative) == [ValidationCode.INVALID_SCHEMA]
    assert negative.normalized_proposal is None

    duplicate = _validate_unchanged(
        connection,
        context,
        {
            "payment_id": package.payments[0].payment_id,
            "customer_id": CUST_HARBOR,
            "resolution_type": ResolutionType.EXACT_BUNDLE.value,
            "allocations": [
                {"invoice_id": invoice_id, "cash_cents": 60_000, "fee_cents": 0},
                {"invoice_id": invoice_id, "cash_cents": 61_000, "fee_cents": 0},
            ],
            "evidence_document_ids": [remittance.document_id],
            "explanation": "Duplicate invoice use.",
            "precedent_ids_used": [],
        },
    )
    assert duplicate.valid is False
    assert ValidationCode.DUPLICATE_INVOICE in _codes(duplicate)
    connection.close()


def test_unsupported_bundle_fee_and_self_reported_evidence_flag(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T02")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    context = _open_named(_context(case_id=entry.case_id), package, remittance.document_id)
    payload = _exact_payload(package, resolution_type=ResolutionType.EXACT_BUNDLE)
    payload["allocations"][0]["fee_cents"] = 3500
    payload["allocations"][0]["cash_cents"] = payload["allocations"][0]["cash_cents"] - 3500
    payload["explanation"] = "Bundle with an invented receiving fee."
    report = _validate_unchanged(connection, context, payload)
    assert report.valid is False
    assert ValidationCode.UNSUPPORTED_BUNDLE_FEE in _codes(report)

    flagged = _exact_payload(package, resolution_type=ResolutionType.EXACT_BUNDLE)
    flagged["evidence_verified"] = True
    flagged_report = _validate_unchanged(connection, context, flagged)
    assert flagged_report.valid is False
    assert _codes(flagged_report) == [ValidationCode.INVALID_SCHEMA]
    assert "evidence_verified" in flagged_report.issues[0].message
    connection.close()


def test_fee_limit_one_cent_and_already_applied(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    one = _seed_fee_workspace(
        connection,
        "WS-FEE-1",
        invoice_cents=98_000,
        payment_cents=97_999,
        fee_cents=1,
    )
    report_one = _validate_unchanged(
        connection, one["context"], one["payload"], workspace_id="WS-FEE-1"
    )
    assert report_one.valid is True, _codes(report_one)
    assert report_one.normalized_proposal.allocations[0].fee_cents == 1

    limit = _seed_fee_workspace(
        connection,
        "WS-FEE-5000",
        invoice_cents=600_000,
        payment_cents=595_000,
        fee_cents=5000,
    )
    report_limit = _validate_unchanged(
        connection, limit["context"], limit["payload"], workspace_id="WS-FEE-5000"
    )
    assert report_limit.valid is True, _codes(report_limit)

    over = _seed_fee_workspace(
        connection,
        "WS-FEE-5001",
        invoice_cents=105_001,
        payment_cents=100_000,
        fee_cents=5_001,
    )
    report_over = _validate_unchanged(
        connection, over["context"], over["payload"], workspace_id="WS-FEE-5001"
    )
    assert report_over.valid is False
    assert ValidationCode.FEE_OVER_LIMIT in _codes(report_over)

    applied = _seed_fee_workspace(
        connection,
        "WS-APPLIED",
        invoice_cents=50_000,
        payment_cents=50_000,
        fee_cents=0,
        exact=True,
    )
    connection.execute(
        "UPDATE payments SET applied = 1 WHERE workspace_id = ? AND payment_id = ?",
        ("WS-APPLIED", "PAY-FEE"),
    )
    report_applied = _validate_unchanged(
        connection, applied["context"], applied["payload"], workspace_id="WS-APPLIED"
    )
    assert report_applied.valid is False
    assert ValidationCode.PAYMENT_ALREADY_APPLIED in _codes(report_applied)
    connection.close()


def test_stale_ledger_void_invoice_and_unretrieved_precedent(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T01")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    payload = _exact_payload(package, resolution_type=ResolutionType.EXACT_SINGLE)
    connection.execute(
        "UPDATE workspaces SET ledger_revision = 2 WHERE workspace_id = ?",
        (WS,),
    )
    stale_context = _open_named(
        _context(case_id=entry.case_id, revision=0), package, remittance.document_id
    )
    stale = _validate_unchanged(connection, stale_context, payload)
    assert stale.valid is False
    assert ValidationCode.STALE_LEDGER in _codes(stale)
    assert stale.checked_ledger_revision == 2

    connection.execute(
        "UPDATE workspaces SET ledger_revision = 0 WHERE workspace_id = ?",
        (WS,),
    )
    invoice_id = payload["allocations"][0]["invoice_id"]
    connection.execute(
        "UPDATE invoices SET status = 'VOID' WHERE workspace_id = ? AND invoice_id = ?",
        (WS, invoice_id),
    )
    void_context = _open_named(
        _context(case_id=entry.case_id, revision=0), package, remittance.document_id
    )
    void = _validate_unchanged(connection, void_context, payload)
    assert void.valid is False
    assert ValidationCode.VOID_INVOICE in _codes(void)

    connection.execute(
        "UPDATE invoices SET status = 'OPEN' WHERE workspace_id = ? AND invoice_id = ?",
        (WS, invoice_id),
    )
    payload["precedent_ids_used"] = ["PRE-001"]
    precedent_context = _open_named(
        _context(case_id=entry.case_id), package, remittance.document_id
    )
    precedent = _validate_unchanged(connection, precedent_context, payload)
    assert precedent.valid is False
    assert ValidationCode.INVALID_PRECEDENT_REFERENCE in _codes(precedent)
    connection.close()


def test_t03_apply_changes_expected_balances_once(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    package, _entry = _import_authoring(connection, "T03")
    cedar = next(item for item in package.invoices if item.customer_id == CUST_CEDAR)
    context = _open_t03(connection)
    stored = _store(connection, context, _t03_payload())
    before = _money_snapshot(connection)
    result = apply_proposal(
        connection, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    after = _money_snapshot(connection)
    invoice = get_invoice(connection, WS, T03_INVOICE_ID)
    payment = get_payment(connection, WS, T03_PAYMENT_ID)
    cedar_after = get_invoice(connection, WS, cedar.invoice_id)
    case = get_case(connection, WS, T03_CASE_ID)
    application = get_application(connection, WS, result.application_id)
    snapshot = get_case_snapshot(connection, WS, T03_CASE_ID)
    run = get_run(connection, context.run_id)
    assert result.status is ApplicationStatus.APPLIED
    assert result.issues == []
    assert result.ledger_revision == 1
    assert result.resulting_balances is not None
    assert result.resulting_balances.payment_applied is True
    assert result.resulting_balances.invoices[0].outstanding_cents == 0
    assert invoice is not None
    assert invoice.outstanding_cents == 0
    assert invoice.opening_outstanding_cents == 1_000_000
    assert payment is not None
    assert payment.applied is True
    assert cedar_after is not None
    assert cedar_after.outstanding_cents == cedar.opening_outstanding_cents
    assert case is not None
    assert case.state is CaseState.RESOLVED
    assert case.latest_run_id == context.run_id
    assert application is not None
    assert application.seeded is False
    assert application.allocations[0].cash_cents == 996_500
    assert application.allocations[0].fee_cents == 3500
    assert after["applications"] == before["applications"] + 1
    assert after["allocations"] == before["allocations"] + 1
    assert after["revision"] == 1
    assert snapshot.ledger_revision == 1
    assert snapshot.payment.applied is True
    assert snapshot.summary.case_state is CaseState.RESOLVED
    assert any(
        item.invoice_id == T03_INVOICE_ID and item.outstanding_cents == 0
        for item in snapshot.invoices
    )
    assert run is not None
    assert run.terminal_result is not None
    connection.close()


def test_t03_replay_is_noop_even_when_revision_is_stale(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_authoring(connection, "T03")
    context = _open_t03(connection)
    stored = _store(connection, context, _t03_payload())
    first = apply_proposal(
        connection, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    before = _money_snapshot(connection)
    stale_context = context.model_copy(update={"initial_ledger_revision": 0})
    replayed = apply_proposal(
        connection,
        stale_context,
        stored.proposal_id,
        idempotency_key="APPLY-T03",
        actor="investigator",
    )
    after = _money_snapshot(connection)
    assert first.status is ApplicationStatus.APPLIED
    assert replayed.status is ApplicationStatus.REPLAYED
    assert replayed.application_id == first.application_id
    assert replayed.ledger_revision == 1
    assert after == before
    assert get_invoice(connection, WS, T03_INVOICE_ID).outstanding_cents == 0
    connection.close()


def test_t03_conflicting_retry_and_second_key_fail_safely(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_authoring(connection, "T03")
    context = _open_t03(connection)
    payload = _t03_payload()
    stored = _store(connection, context, payload)
    first = apply_proposal(
        connection, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    before = _money_snapshot(connection)
    current_context = context.model_copy(update={"initial_ledger_revision": 1})
    other_payload = dict(payload)
    other_payload["explanation"] = "A different immutable payload for the same operation key."
    other = store_proposal(connection, current_context, other_payload)
    conflict = apply_proposal(
        connection,
        current_context,
        other.proposal_id,
        idempotency_key="APPLY-T03",
        actor="investigator",
    )
    second_key = apply_proposal(
        connection,
        current_context,
        stored.proposal_id,
        idempotency_key="APPLY-T03-OTHER",
        actor="investigator",
    )
    after = _money_snapshot(connection)
    assert first.status is ApplicationStatus.APPLIED
    assert conflict.status is ApplicationStatus.REJECTED
    assert ValidationCode.IDEMPOTENCY_CONFLICT in _result_codes(conflict)
    assert second_key.status is ApplicationStatus.REJECTED
    assert ValidationCode.PAYMENT_ALREADY_APPLIED in _result_codes(second_key)
    assert after == before
    connection.close()


def test_stale_unapplied_proposal_rejects_without_mutation(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_authoring(connection, "T03")
    context = _open_t03(connection)
    stored = _store(connection, context, _t03_payload())
    connection.execute("UPDATE workspaces SET ledger_revision = 4 WHERE workspace_id = ?", (WS,))
    before = _money_snapshot(connection)
    result = apply_proposal(
        connection, context, stored.proposal_id, idempotency_key="APPLY-T03", actor="investigator"
    )
    after = _money_snapshot(connection)
    assert result.status is ApplicationStatus.REJECTED
    assert ValidationCode.STALE_LEDGER in _result_codes(result)
    assert result.application_id is None
    assert after == before
    assert get_payment(connection, WS, T03_PAYMENT_ID).applied is False
    assert get_invoice(connection, WS, T03_INVOICE_ID).outstanding_cents == 1_000_000
    assert get_case(connection, WS, T03_CASE_ID).state is CaseState.OPEN
    connection.close()


def test_t02_bundle_apply_and_injected_failure_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path)
    package, entry = _import_authoring(connection, "T02")
    remittance = _doc(package, DocumentKind.REMITTANCE)
    payload = _exact_payload(package, resolution_type=ResolutionType.EXACT_BUNDLE)
    context = _open_named(_context(case_id=entry.case_id), package, remittance.document_id)
    stored = _store(connection, context, payload)
    expected_cash = (65_000, 35_000)

    def explode(index: int) -> None:
        if index == 0:
            raise RuntimeError("injected allocation failure")

    monkeypatch.setattr("precedent.ledger._after_allocation_write", explode)
    before = _money_snapshot(connection)
    with pytest.raises(RuntimeError, match="injected allocation failure"):
        apply_proposal(
            connection,
            context,
            stored.proposal_id,
            idempotency_key="APPLY-T02",
            actor="investigator",
        )
    after_fail = _money_snapshot(connection)
    assert after_fail == before
    invoices = {item.invoice_id: item for item in list_invoices(connection, WS)}
    for invoice_id, cash in zip(payload["allocations"], expected_cash, strict=True):
        assert invoices[invoice_id["invoice_id"]].outstanding_cents == cash
    assert get_payment(connection, WS, package.payments[0].payment_id).applied is False
    assert get_case(connection, WS, entry.case_id).state is CaseState.OPEN

    monkeypatch.setattr("precedent.ledger._after_allocation_write", lambda index: None)
    applied = apply_proposal(
        connection, context, stored.proposal_id, idempotency_key="APPLY-T02", actor="investigator"
    )
    assert applied.status is ApplicationStatus.APPLIED
    assert applied.ledger_revision == 1
    invoices = {item.invoice_id: item for item in list_invoices(connection, WS)}
    for item in payload["allocations"]:
        assert invoices[item["invoice_id"]].outstanding_cents == 0
    assert get_payment(connection, WS, package.payments[0].payment_id).applied is True
    connection.close()


def test_unknown_proposal_is_rejected(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_authoring(connection, "T03")
    context = _open_t03(connection)
    _insert_run(connection, context)
    result = apply_proposal(
        connection, context, "PRP-MISSING", idempotency_key="APPLY-NONE", actor="investigator"
    )
    assert result.status is ApplicationStatus.REJECTED
    assert ValidationCode.NOT_FOUND in _result_codes(result)
    assert get_payment(connection, WS, T03_PAYMENT_ID).applied is False
    connection.close()


def _open_t03(connection: sqlite3.Connection, workspace_id: str = WS) -> RunContext:
    context = _context(workspace_id=workspace_id, case_id=T03_CASE_ID)
    opened_remit = read_document(connection, context, {"document_id": T03_REMIT_ID})
    opened_fee = read_document(connection, context, {"document_id": T03_FEE_ID})
    assert opened_remit.ok is True
    assert opened_fee.ok is True
    return context


def _t03_payload() -> dict[str, object]:
    return {
        "payment_id": T03_PAYMENT_ID,
        "customer_id": CUST_HARBOR,
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE.value,
        "allocations": [{"invoice_id": T03_INVOICE_ID, "cash_cents": 996_500, "fee_cents": 3500}],
        "evidence_document_ids": [T03_REMIT_ID, T03_FEE_ID],
        "explanation": (
            "The remittance identifies INV-1042; ticket ST-8721 matches the bank fee notice."
        ),
        "precedent_ids_used": [],
    }


def _insert_run(connection: sqlite3.Connection, context: RunContext) -> None:
    insert_run(
        connection,
        RunRecord(
            run_id=context.run_id,
            workspace_id=context.workspace_id,
            case_id=context.case_id,
            mode=context.execution_mode,
            provider=None,
            model=None,
            prompt_hash=HASH_A,
            tool_hash=HASH_A,
            memory_snapshot=context.memory_snapshot,
            state=RunState.RUNNING,
            started_at=CREATED,
            finished_at=None,
            budgets=context.budgets,
            usage=None,
            terminal_result=None,
        ),
    )


def _store(connection: sqlite3.Connection, context: RunContext, payload: dict[str, object]):
    if get_run(connection, context.run_id) is None:
        _insert_run(connection, context)
    return store_proposal(connection, context, payload)


def _money_snapshot(connection: sqlite3.Connection, workspace_id: str = WS) -> dict[str, object]:
    snapshot = _snapshot(connection, workspace_id)
    snapshot.pop("total_changes")
    return snapshot


def _result_codes(result) -> list[ValidationCode]:
    return [issue.code for issue in result.issues]


def _currency_package():
    class _Bundle:
        documents = [
            SourceDocument(
                document_id="DOC-R-EUR",
                kind=DocumentKind.REMITTANCE,
                title="EUR remittance",
                issued_date="2026-01-20",
                body_text="EUR remittance for INV-EUR.",
                facts=RemittanceFacts(
                    customer_id=CUST_HARBOR,
                    bank_reference="BR-EUR",
                    invoice_ids=["INV-EUR"],
                    gross_settlement_cents=145_000,
                    currency="EUR",
                    receiving_account_id=ALLOWED_CASH_ACCOUNT_ID,
                    transfer_reference=None,
                    settlement_ticket=None,
                ),
            )
        ]

    return _Bundle()


def _seed_currency_mismatch(connection: sqlite3.Connection, workspace_id: str) -> None:
    policy = NORTHSTAR_POLICY
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name="EUR rejection",
            company_id=policy.company_id,
            period_label="2026-01",
            period_start="2026-01-01",
            period_end="2026-01-31",
            dataset_hash=HASH_A,
            policy=policy,
            policy_hash=hash_model(policy),
            ledger_revision=0,
            schema_version=SCHEMA_VERSION,
            created_at=CREATED,
        ),
    )
    insert_customer(
        connection,
        CustomerRecord(
            workspace_id=workspace_id,
            customer_id=CUST_HARBOR,
            legal_name="Harbor Labs LLC",
            display_name="Harbor Labs",
        ),
    )
    insert_invoice(
        connection,
        InvoiceRecord(
            workspace_id=workspace_id,
            invoice_id="INV-EUR",
            customer_id=CUST_HARBOR,
            currency="EUR",
            issued_date="2026-01-02",
            due_date="2026-01-20",
            original_cents=145_000,
            opening_outstanding_cents=145_000,
            outstanding_cents=145_000,
            status=InvoiceSourceStatus.OPEN,
        ),
    )
    insert_payment(
        connection,
        PaymentRecord(
            workspace_id=workspace_id,
            payment_id="PAY-EUR",
            bank_transaction_id="BTX-EUR",
            bank_account_id=ALLOWED_CASH_ACCOUNT_ID,
            posted_date="2026-01-20",
            currency="USD",
            amount_cents=145_000,
            channel=PaymentChannel.ACH,
            payer_text="Harbor Treasury",
            bank_reference="BR-EUR",
            applied=False,
        ),
    )
    insert_document(connection, _currency_package().documents[0].to_persisted(workspace_id))


def _seed_fee_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    invoice_cents: int,
    payment_cents: int,
    fee_cents: int,
    exact: bool = False,
) -> dict[str, object]:
    policy = NORTHSTAR_POLICY
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name=workspace_id,
            company_id=policy.company_id,
            period_label="2026-01",
            period_start="2026-01-01",
            period_end="2026-01-31",
            dataset_hash=HASH_A,
            policy=policy,
            policy_hash=hash_model(policy),
            ledger_revision=0,
            schema_version=SCHEMA_VERSION,
            created_at=CREATED,
        ),
    )
    insert_customer(
        connection,
        CustomerRecord(
            workspace_id=workspace_id,
            customer_id=CUST_HARBOR,
            legal_name="Harbor Labs LLC",
            display_name="Harbor Labs",
        ),
    )
    insert_invoice(
        connection,
        InvoiceRecord(
            workspace_id=workspace_id,
            invoice_id="INV-FEE",
            customer_id=CUST_HARBOR,
            currency="USD",
            issued_date="2026-01-02",
            due_date="2026-01-20",
            original_cents=invoice_cents,
            opening_outstanding_cents=invoice_cents,
            outstanding_cents=invoice_cents,
            status=InvoiceSourceStatus.OPEN,
        ),
    )
    insert_payment(
        connection,
        PaymentRecord(
            workspace_id=workspace_id,
            payment_id="PAY-FEE",
            bank_transaction_id=f"BTX-{workspace_id[-6:]}",
            bank_account_id=ALLOWED_CASH_ACCOUNT_ID,
            posted_date="2026-01-20",
            currency="USD",
            amount_cents=payment_cents,
            channel=PaymentChannel.ACH if exact else PaymentChannel.WIRE,
            payer_text="Harbor Treasury",
            bank_reference=f"BR-{workspace_id[-6:]}",
            applied=False,
        ),
    )
    remittance = SourceDocument(
        document_id="DOC-R-FEE",
        kind=DocumentKind.REMITTANCE,
        title="Fee remittance",
        issued_date="2026-01-20",
        body_text="Harbor remittance for INV-FEE.",
        facts=RemittanceFacts(
            customer_id=CUST_HARBOR,
            bank_reference=f"BR-{workspace_id[-6:]}",
            invoice_ids=["INV-FEE"],
            gross_settlement_cents=invoice_cents,
            currency="USD",
            receiving_account_id=ALLOWED_CASH_ACCOUNT_ID,
            transfer_reference=None,
            settlement_ticket=None if exact else "ST-FEE",
        ),
    )
    documents = [remittance]
    evidence_ids = [remittance.document_id]
    if not exact:
        notice = SourceDocument(
            document_id="DOC-F-FEE",
            kind=DocumentKind.BANK_FEE_NOTICE,
            title="Fee notice",
            issued_date="2026-01-20",
            body_text="Bank receiving wire fee notice.",
            facts=BankFeeNoticeFacts(
                customer_id=CUST_HARBOR,
                receiving_account_id=ALLOWED_CASH_ACCOUNT_ID,
                currency="USD",
                transfer_reference="ST-FEE",
                fee_cents=max(fee_cents, 1),
                fee_type=SUPPORTED_FEE_TYPE,
                gross_cents=invoice_cents,
                net_cents=payment_cents,
            ),
        )
        documents.append(notice)
        evidence_ids.append(notice.document_id)
    for document in documents:
        insert_document(connection, document.to_persisted(workspace_id))
    opened = [
        OpenedEvidence(document_id=item.document_id, sha256=item.to_persisted(workspace_id).sha256)
        for item in documents
    ]
    payload = {
        "payment_id": "PAY-FEE",
        "customer_id": CUST_HARBOR,
        "resolution_type": (
            ResolutionType.EXACT_SINGLE.value
            if exact
            else ResolutionType.SINGLE_WITH_BANK_FEE.value
        ),
        "allocations": [
            {
                "invoice_id": "INV-FEE",
                "cash_cents": payment_cents,
                "fee_cents": 0 if exact else fee_cents,
            }
        ],
        "evidence_document_ids": evidence_ids,
        "explanation": "Synthetic fee-bound settlement for limit checks.",
        "precedent_ids_used": [],
    }
    context = _context(workspace_id=workspace_id, case_id="CASE-FEE", opened=opened)
    return {"context": context, "payload": payload}
