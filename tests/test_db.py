from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from precedent.db import (
    PersistenceError,
    get_application,
    get_case,
    get_customer,
    get_document,
    get_invoice,
    get_payment,
    get_workspace,
    insert_application,
    insert_case,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_workspace,
    open_database,
    transaction,
)
from precedent.models import (
    SCHEMA_VERSION,
    Allocation,
    ApplicationRecord,
    BankFeeNoticeFacts,
    CaseRecord,
    CaseState,
    CompanyPolicy,
    CustomerRecord,
    DocumentKind,
    InvoiceRecord,
    InvoiceSourceStatus,
    PaymentChannel,
    PaymentRecord,
    RemittanceFacts,
    SourceDocument,
    WorkspaceRecord,
    hash_model,
)

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
WORKSPACE_ID = "WS-DEMO"

POLICY = CompanyPolicy(
    policy_id="northstar-usd-v1",
    company_id="NORTHSTAR",
    allowed_cash_account_id="CASH-US-01",
    currency="USD",
    supported_channels=[PaymentChannel.WIRE, PaymentChannel.ACH],
    allow_receiving_wire_fee=True,
    max_receiving_wire_fee_cents=5000,
    max_bundle_invoices=3,
    allow_partial_settlement=False,
    allow_writeoff=False,
    require_remittance=True,
)


def _open(tmp_path: Path) -> sqlite3.Connection:
    return open_database(tmp_path / "precedent.sqlite3")


def _seed_workspace(connection: sqlite3.Connection) -> None:
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=WORKSPACE_ID,
            name="Northstar demo",
            company_id="NORTHSTAR",
            period_label="2026-01",
            period_start="2026-01-01",
            period_end="2026-01-31",
            dataset_hash=HASH_A,
            policy=POLICY,
            policy_hash=hash_model(POLICY),
            ledger_revision=0,
            schema_version=SCHEMA_VERSION,
            created_at=CREATED,
        ),
    )
    insert_customer(
        connection,
        CustomerRecord(
            workspace_id=WORKSPACE_ID,
            customer_id="CUST-HARBOR",
            legal_name="Harbor Labs LLC",
            display_name="Harbor Labs",
        ),
    )


def test_import_db_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sys.modules.pop("precedent.db", None)
    import precedent.db as db

    assert callable(db.open_database)
    assert db.SCHEMA_VERSION == 1


def test_initialize_and_reopen_preserves_workspace(tmp_path: Path) -> None:
    path = tmp_path / "var" / "precedent.sqlite3"
    connection = open_database(path)
    _seed_workspace(connection)
    flags = connection.execute("PRAGMA foreign_keys").fetchone()
    assert flags[0] == 1
    connection.close()

    reopened = open_database(path)
    workspace = get_workspace(reopened, WORKSPACE_ID)
    assert workspace is not None
    assert workspace.schema_version == 1
    assert workspace.policy.policy_id == "northstar-usd-v1"
    assert workspace.ledger_revision == 0
    tables = {
        row[0]
        for row in reopened.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for required in (
        "workspaces",
        "customers",
        "invoices",
        "payments",
        "documents",
        "cases",
        "runs",
        "run_events",
        "proposals",
        "applications",
        "allocations",
        "corrections",
        "precedents",
        "workspace_memory",
        "candidate_tests",
        "evaluation_runs",
    ):
        assert required in tables
    reopened.close()


def test_invoice_payment_evidence_case_round_trip(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)

    invoice = InvoiceRecord(
        workspace_id=WORKSPACE_ID,
        invoice_id="INV-1042",
        customer_id="CUST-HARBOR",
        currency="USD",
        issued_date="2026-01-02",
        due_date="2026-01-20",
        original_cents=1_000_000,
        opening_outstanding_cents=1_000_000,
        outstanding_cents=1_000_000,
        status=InvoiceSourceStatus.OPEN,
    )
    payment = PaymentRecord(
        workspace_id=WORKSPACE_ID,
        payment_id="PAY-201",
        bank_transaction_id="BANK-TX-201",
        bank_account_id="CASH-US-01",
        posted_date="2026-01-20",
        currency="USD",
        amount_cents=996_500,
        channel=PaymentChannel.WIRE,
        payer_text="Harbor Treasury",
        bank_reference="BR-201",
        applied=False,
    )
    remittance = SourceDocument(
        document_id="DOC-R201",
        kind=DocumentKind.REMITTANCE,
        title="Harbor payment advice",
        issued_date="2026-01-20",
        body_text="Harbor Labs paid invoice INV-1042, gross USD 10,000.00.",
        facts=RemittanceFacts(
            customer_id="CUST-HARBOR",
            bank_reference="BR-201",
            invoice_ids=["INV-1042"],
            gross_settlement_cents=1_000_000,
            currency="USD",
            receiving_account_id="CASH-US-01",
            transfer_reference=None,
            settlement_ticket="ST-8721",
        ),
    ).to_persisted(WORKSPACE_ID)
    notice = SourceDocument(
        document_id="DOC-F201",
        kind=DocumentKind.BANK_FEE_NOTICE,
        title="Transfer advice ST-8721",
        issued_date="2026-01-20",
        body_text="Transfer ST-8721 fee USD 35.00.",
        facts=BankFeeNoticeFacts(
            customer_id="CUST-HARBOR",
            receiving_account_id="CASH-US-01",
            currency="USD",
            transfer_reference="ST-8721",
            fee_cents=3500,
            fee_type="RECEIVING_WIRE_FEE",
            gross_cents=1_000_000,
            net_cents=996_500,
        ),
    ).to_persisted(WORKSPACE_ID)
    case = CaseRecord(
        workspace_id=WORKSPACE_ID,
        case_id="CASE-201",
        payment_id="PAY-201",
        state=CaseState.OPEN,
    )

    insert_invoice(connection, invoice)
    insert_payment(connection, payment)
    insert_document(connection, remittance)
    insert_document(connection, notice)
    insert_case(connection, case)
    connection.close()

    reopened = open_database(tmp_path / "precedent.sqlite3")
    loaded_invoice = get_invoice(reopened, WORKSPACE_ID, "INV-1042")
    loaded_payment = get_payment(reopened, WORKSPACE_ID, "PAY-201")
    loaded_remit = get_document(reopened, WORKSPACE_ID, "DOC-R201")
    loaded_notice = get_document(reopened, WORKSPACE_ID, "DOC-F201")
    loaded_case = get_case(reopened, WORKSPACE_ID, "CASE-201")
    assert loaded_invoice is not None
    assert loaded_invoice.original_cents == 1_000_000
    assert loaded_invoice.outstanding_cents == 1_000_000
    assert loaded_invoice.invoice_id == "INV-1042"
    assert loaded_payment is not None
    assert loaded_payment.amount_cents == 996_500
    assert loaded_payment.payment_id == "PAY-201"
    assert loaded_payment.applied is False
    assert loaded_remit is not None
    assert loaded_remit.sha256 == remittance.sha256
    assert loaded_remit.facts.settlement_ticket == "ST-8721"
    assert loaded_notice is not None
    assert loaded_notice.facts.fee_cents == 3500
    assert loaded_case is not None
    assert loaded_case.payment_id == "PAY-201"
    assert loaded_case.state is CaseState.OPEN
    reopened.close()


def test_unsupported_currency_round_trips_for_policy_review(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    insert_customer(
        connection,
        CustomerRecord(
            workspace_id=WORKSPACE_ID,
            customer_id="CUST-CEDAR",
            legal_name="Cedar Design LLC",
            display_name="Cedar Design",
        ),
    )
    insert_invoice(
        connection,
        InvoiceRecord(
            workspace_id=WORKSPACE_ID,
            invoice_id="INV-EUR-1",
            customer_id="CUST-CEDAR",
            currency="EUR",
            issued_date="2026-01-02",
            due_date="2026-01-20",
            original_cents=145_000,
            opening_outstanding_cents=145_000,
            outstanding_cents=145_000,
            status=InvoiceSourceStatus.OPEN,
        ),
    )
    loaded = get_invoice(connection, WORKSPACE_ID, "INV-EUR-1")
    assert loaded is not None
    assert loaded.currency == "EUR"
    assert loaded.original_cents == 145_000
    connection.close()


def _seeded_application(**overrides: object) -> ApplicationRecord:
    payload: dict[str, object] = {
        "application_id": "APP-SEED-1",
        "workspace_id": WORKSPACE_ID,
        "payment_id": "PAY-201",
        "proposal_id": None,
        "seeded": True,
        "idempotency_key": "seed:APP-SEED-1",
        "payload_hash": HASH_A,
        "actor": "seed_import",
        "allocations": [Allocation(invoice_id="INV-1042", cash_cents=1_000_000, fee_cents=0)],
        "created_at": CREATED,
    }
    payload.update(overrides)
    return ApplicationRecord.model_validate(payload)


def _seed_invoice_and_payment(connection: sqlite3.Connection) -> None:
    insert_invoice(
        connection,
        InvoiceRecord(
            workspace_id=WORKSPACE_ID,
            invoice_id="INV-1042",
            customer_id="CUST-HARBOR",
            currency="USD",
            issued_date="2026-01-02",
            due_date="2026-01-20",
            original_cents=1_000_000,
            opening_outstanding_cents=0,
            outstanding_cents=0,
            status=InvoiceSourceStatus.OPEN,
        ),
    )
    insert_payment(
        connection,
        PaymentRecord(
            workspace_id=WORKSPACE_ID,
            payment_id="PAY-201",
            bank_transaction_id="BANK-TX-201",
            bank_account_id="CASH-US-01",
            posted_date="2026-01-20",
            currency="USD",
            amount_cents=1_000_000,
            channel=PaymentChannel.WIRE,
            payer_text="Harbor Treasury",
            bank_reference="BR-201",
            applied=True,
        ),
    )


def test_duplicate_application_identifiers_fail_at_storage(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    _seed_invoice_and_payment(connection)
    insert_application(connection, _seeded_application())
    loaded = get_application(connection, WORKSPACE_ID, "APP-SEED-1")
    assert loaded is not None
    assert loaded.allocations[0].cash_cents == 1_000_000

    insert_payment(
        connection,
        PaymentRecord(
            workspace_id=WORKSPACE_ID,
            payment_id="PAY-202",
            bank_transaction_id="BANK-TX-202",
            bank_account_id="CASH-US-01",
            posted_date="2026-01-21",
            currency="USD",
            amount_cents=50_000,
            channel=PaymentChannel.ACH,
            payer_text="Harbor Treasury",
            bank_reference="BR-202",
            applied=True,
        ),
    )
    with pytest.raises(PersistenceError):
        insert_application(
            connection,
            _seeded_application(
                payment_id="PAY-202",
                idempotency_key="seed:APP-SEED-1-b",
                allocations=[Allocation(invoice_id="INV-1042", cash_cents=50_000, fee_cents=0)],
            ),
        )
    with pytest.raises(PersistenceError):
        insert_application(
            connection,
            _seeded_application(
                application_id="APP-SEED-2",
                payment_id="PAY-201",
                idempotency_key="seed:APP-SEED-2",
            ),
        )
    with pytest.raises(PersistenceError):
        insert_application(
            connection,
            _seeded_application(
                application_id="APP-SEED-3",
                payment_id="PAY-202",
                idempotency_key="seed:APP-SEED-1",
                allocations=[Allocation(invoice_id="INV-1042", cash_cents=50_000, fee_cents=0)],
            ),
        )
    connection.close()


def test_duplicate_bank_transaction_id_fails(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    insert_payment(
        connection,
        PaymentRecord(
            workspace_id=WORKSPACE_ID,
            payment_id="PAY-201",
            bank_transaction_id="BANK-TX-201",
            bank_account_id="CASH-US-01",
            posted_date="2026-01-20",
            currency="USD",
            amount_cents=100,
            channel=PaymentChannel.WIRE,
            payer_text="Harbor",
            bank_reference="BR-201",
        ),
    )
    with pytest.raises(PersistenceError):
        insert_payment(
            connection,
            PaymentRecord(
                workspace_id=WORKSPACE_ID,
                payment_id="PAY-202",
                bank_transaction_id="BANK-TX-201",
                bank_account_id="CASH-US-01",
                posted_date="2026-01-21",
                currency="USD",
                amount_cents=200,
                channel=PaymentChannel.WIRE,
                payer_text="Harbor",
                bank_reference="BR-202",
            ),
        )
    connection.close()


def test_negative_outstanding_fails_storage_check(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO invoices (
              workspace_id, invoice_id, customer_id, currency, issued_date, due_date,
              original_cents, opening_outstanding_cents, outstanding_cents, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                WORKSPACE_ID,
                "INV-BAD",
                "CUST-HARBOR",
                "USD",
                "2026-01-02",
                "2026-01-20",
                100,
                100,
                -1,
                "OPEN",
            ),
        )
    connection.close()


def test_transaction_rollback_restores_state(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    with pytest.raises(PersistenceError):
        with transaction(connection, immediate=True):
            insert_customer(
                connection,
                CustomerRecord(
                    workspace_id=WORKSPACE_ID,
                    customer_id="CUST-CEDAR",
                    legal_name="Cedar Design LLC",
                    display_name="Cedar Design",
                ),
            )
            insert_customer(
                connection,
                CustomerRecord(
                    workspace_id=WORKSPACE_ID,
                    customer_id="CUST-CEDAR",
                    legal_name="Cedar Duplicate",
                    display_name="Cedar Duplicate",
                ),
            )
    assert get_workspace(connection, WORKSPACE_ID) is not None
    assert get_customer(connection, WORKSPACE_ID, "CUST-CEDAR") is None
    connection.close()
