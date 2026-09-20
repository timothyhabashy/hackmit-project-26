from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from precedent.config import detect_repo_root
from precedent.db import (
    get_payment,
    insert_customer,
    insert_document,
    insert_invoice,
    insert_payment,
    insert_workspace,
    list_documents,
    list_invoices,
    open_database,
)
from precedent.evidence import (
    read_document,
    search_documents,
    search_invoices,
)
from precedent.fixtures import (
    CUST_CEDAR,
    CUST_HARBOR,
    FORBIDDEN_RUNTIME_TOKENS,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    T03_TICKET,
    load_package,
)
from precedent.models import (
    SCHEMA_VERSION,
    BudgetLimits,
    CompanyPolicy,
    CustomerRecord,
    DocumentKind,
    ErrorCode,
    ExecutionMode,
    InvoiceRecord,
    InvoiceSourceStatus,
    MemorySnapshot,
    PaymentChannel,
    PaymentRecord,
    RunContext,
    SourceDocument,
    ValidationCode,
    WorkspaceRecord,
    hash_model,
)
from precedent.models import Document as PersistedDocument

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"
WS_A = "WS-EVIDENCE-A"
WS_B = "WS-EVIDENCE-B"
T03_CASE_ID = "CASE-1CFA9FEE848F"
CEDAR_T03_INVOICE_ID = "INV-5BD2CDB765F9"
ORACLE_TOKENS = FORBIDDEN_RUNTIME_TOKENS + ("expected_allocations", "oracle")

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


def _context(workspace_id: str = WS_A, *, company_id: str = "NORTHSTAR") -> RunContext:
    return RunContext(
        workspace_id=workspace_id,
        case_id=T03_CASE_ID,
        run_id="RUN-EVIDENCE-1",
        company_id=company_id,
        policy_id="northstar-usd-v1",
        policy_hash=HASH_A,
        dataset_hash=HASH_A,
        initial_ledger_revision=0,
        execution_mode=ExecutionMode.TEST,
        actor="investigator",
        opened_documents=[],
        retrieved_precedent_version_ids=[],
        memory_snapshot=MemorySnapshot(snapshot_hash=HASH_A),
        budgets=BudgetLimits(
            max_model_calls=10,
            max_tool_calls=30,
            max_case_seconds=120,
            request_timeout_seconds=30,
            max_output_tokens=1500,
        ),
    )


def _seed_workspace(
    connection: sqlite3.Connection,
    workspace_id: str = WS_A,
    *,
    company_id: str = "NORTHSTAR",
) -> None:
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name="Evidence tests",
            company_id=company_id,
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
            workspace_id=workspace_id,
            customer_id=CUST_HARBOR,
            legal_name="Harbor Labs LLC",
            display_name="Harbor Labs",
        ),
    )
    insert_customer(
        connection,
        CustomerRecord(
            workspace_id=workspace_id,
            customer_id=CUST_CEDAR,
            legal_name="Cedar Design LLC",
            display_name="Cedar Design",
        ),
    )


def _invoice(
    workspace_id: str,
    invoice_id: str,
    customer_id: str,
    *,
    outstanding_cents: int,
    issued_date: str = "2026-01-02",
) -> InvoiceRecord:
    return InvoiceRecord(
        workspace_id=workspace_id,
        invoice_id=invoice_id,
        customer_id=customer_id,
        currency="USD",
        issued_date=issued_date,
        due_date="2026-01-20",
        original_cents=outstanding_cents,
        opening_outstanding_cents=outstanding_cents,
        outstanding_cents=outstanding_cents,
        status=InvoiceSourceStatus.OPEN,
    )


def _import_t03(connection: sqlite3.Connection, workspace_id: str = WS_A) -> None:
    package = load_package(detect_repo_root() / "data" / "source" / T03_CASE_ID)
    insert_workspace(
        connection,
        WorkspaceRecord(
            workspace_id=workspace_id,
            name="T03 lookup",
            company_id=package.company.company_id,
            period_label="2026-01",
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


def _opened_ids(context: RunContext) -> list[str]:
    return [item.document_id for item in context.opened_documents]


def _serialized(result) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)


def _assert_no_oracle_fields(result) -> None:
    blob = _serialized(result).lower()
    for token in ORACLE_TOKENS:
        assert token not in blob


def test_import_evidence_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sys.modules.pop("precedent.evidence", None)
    import precedent.evidence as evidence

    source = Path(evidence.__file__).read_text(encoding="utf-8")
    assert "data/grading" not in source
    assert "load_oracle" not in source
    assert callable(evidence.search_invoices)


def test_t03_lookup_payment_advice_leads_to_bank_notice(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    payment = get_payment(connection, WS_A, T03_PAYMENT_ID)
    assert payment is not None
    assert payment.payment_id == T03_PAYMENT_ID
    assert payment.amount_cents == 996_500

    invoices = search_invoices(connection, context, {"customer_id": CUST_HARBOR})
    assert invoices.ok is True
    harbor_ids = [hit["invoice_id"] for hit in invoices.data["hits"]]
    assert T03_INVOICE_ID in harbor_ids
    assert CEDAR_T03_INVOICE_ID not in harbor_ids
    _assert_no_oracle_fields(invoices)

    remittances = search_documents(
        connection,
        context,
        {"reference": payment.bank_reference, "kind": DocumentKind.REMITTANCE.value},
    )
    assert remittances.ok is True
    assert remittances.data["total"] == 1
    assert remittances.data["hits"][0]["document_id"] == T03_REMIT_ID
    assert remittances.data["hits"][0]["kind"] == DocumentKind.REMITTANCE.value
    assert "facts" not in remittances.data["hits"][0]
    assert _opened_ids(context) == []

    opened = read_document(connection, context, {"document_id": T03_REMIT_ID})
    assert opened.ok is True
    assert opened.data["document_id"] == T03_REMIT_ID
    assert opened.data["facts"]["settlement_ticket"] == T03_TICKET
    assert opened.data["facts"]["invoice_ids"] == [T03_INVOICE_ID]
    assert opened.data["sha256"]
    assert T03_REMIT_ID in _opened_ids(context)
    _assert_no_oracle_fields(opened)

    notices = search_documents(
        connection,
        context,
        {
            "reference": opened.data["facts"]["settlement_ticket"],
            "kind": DocumentKind.BANK_FEE_NOTICE.value,
            "customer_id": CUST_HARBOR,
        },
    )
    assert notices.ok is True
    assert [hit["document_id"] for hit in notices.data["hits"]] == [T03_FEE_ID]
    assert T03_FEE_ID not in _opened_ids(context)

    notice = read_document(connection, context, {"document_id": T03_FEE_ID})
    assert notice.ok is True
    assert notice.data["facts"]["transfer_reference"] == T03_TICKET
    assert notice.data["facts"]["fee_cents"] == 3500
    assert notice.data["facts"]["net_cents"] == 996_500
    assert _opened_ids(context) == [T03_REMIT_ID, T03_FEE_ID]
    _assert_no_oracle_fields(notice)
    connection.close()


def test_t03_runtime_lookup_does_not_read_grading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    opened_paths: list[str] = []
    real_open = open

    def tracking_open(file, *args, **kwargs):
        path = str(file)
        opened_paths.append(path)
        if "grading" in Path(path).parts:
            raise AssertionError(f"runtime opened grading path: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    search_documents(connection, context, {"reference": T03_TICKET})
    read_document(connection, context, {"document_id": T03_REMIT_ID})
    read_document(connection, context, {"document_id": T03_FEE_ID})
    search_invoices(connection, context, {"invoice_id": T03_INVOICE_ID})
    assert not any("grading" in Path(path).parts for path in opened_paths)
    connection.close()


def test_unrelated_customer_similar_amount_is_not_a_match(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    insert_invoice(connection, _invoice(WS_A, "INV-H-500", CUST_HARBOR, outstanding_cents=50_000))
    insert_invoice(connection, _invoice(WS_A, "INV-C-500", CUST_CEDAR, outstanding_cents=50_000))
    context = _context()

    harbor = search_invoices(connection, context, {"customer_id": CUST_HARBOR})
    cedar = search_invoices(connection, context, {"customer_id": CUST_CEDAR})
    amount_query = search_invoices(connection, context, {"query": "50000"})
    assert [hit["invoice_id"] for hit in harbor.data["hits"]] == ["INV-H-500"]
    assert [hit["invoice_id"] for hit in cedar.data["hits"]] == ["INV-C-500"]
    assert amount_query.ok is True
    assert amount_query.data["hits"] == []
    assert amount_query.data["total"] == 0
    connection.close()


def test_missing_reference_is_empty_success(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    result = search_documents(connection, context, {"reference": "TX-MISSING"})
    assert result.ok is True
    assert result.error is None
    assert result.data["hits"] == []
    assert result.data["total"] == 0
    assert result.data["truncated"] is False
    assert result.source_ids == []
    assert _opened_ids(context) == []
    connection.close()


def test_cedar_scope_cannot_use_harbor_ticket(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    result = search_documents(
        connection,
        context,
        {"reference": T03_TICKET, "customer_id": CUST_CEDAR},
    )
    assert result.ok is True
    assert result.data["hits"] == []
    connection.close()


def test_search_hits_do_not_include_facts_or_oracle_fields(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    result = search_documents(connection, context, {"query": "Harbor payment advice"})
    assert result.ok is True
    hit = result.data["hits"][0]
    assert set(hit) == {
        "document_id",
        "kind",
        "title",
        "issued_date",
        "snippet",
        "sha256",
    }
    invoice_result = search_invoices(connection, context, {"invoice_id": T03_INVOICE_ID})
    invoice_hit = invoice_result.data["hits"][0]
    assert invoice_hit["outstanding_cents"] == 1_000_000
    assert "original_cents" not in invoice_hit
    _assert_no_oracle_fields(result)
    _assert_no_oracle_fields(invoice_result)
    connection.close()


def test_results_are_stable_and_truncated(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    for index in range(12):
        insert_invoice(
            connection,
            _invoice(
                WS_A,
                f"INV-{index:02d}",
                CUST_HARBOR,
                outstanding_cents=10_000 + index,
            ),
        )
    context = _context()
    result = search_invoices(connection, context, {"customer_id": CUST_HARBOR, "limit": 10})
    ids = [hit["invoice_id"] for hit in result.data["hits"]]
    assert ids == [f"INV-{index:02d}" for index in range(10)]
    assert result.data["total"] == 12
    assert result.data["truncated"] is True
    limited = search_invoices(connection, context, {"customer_id": CUST_HARBOR, "limit": 3})
    assert [hit["invoice_id"] for hit in limited.data["hits"]] == ["INV-00", "INV-01", "INV-02"]
    connection.close()


def test_near_identical_reference_does_not_fuzzy_match(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    nearby = search_documents(connection, context, {"reference": "ST-8722"})
    stripped_digits = search_documents(connection, context, {"reference": "ST8721"})
    exact = search_documents(connection, context, {"reference": "  ST-8721  "})
    assert nearby.data["hits"] == []
    assert stripped_digits.data["hits"] == []
    assert [hit["document_id"] for hit in exact.data["hits"]] == [T03_FEE_ID, T03_REMIT_ID]
    connection.close()


def test_query_matches_currency_and_date_tokens(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    insert_invoice(
        connection,
        _invoice(WS_A, "INV-DATE", CUST_HARBOR, outstanding_cents=12_000, issued_date="2026-01-15"),
    )
    context = _context()
    by_date = search_invoices(connection, context, {"query": "2026-01-15"})
    by_currency = search_invoices(connection, context, {"customer_id": CUST_HARBOR, "query": "USD"})
    assert [hit["invoice_id"] for hit in by_date.data["hits"]] == ["INV-DATE"]
    assert [hit["invoice_id"] for hit in by_currency.data["hits"]] == ["INV-DATE"]
    connection.close()


def test_query_is_case_insensitive_token_search(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    result = search_documents(connection, context, {"query": "bank notice"})
    assert T03_FEE_ID in [hit["document_id"] for hit in result.data["hits"]]
    lowered = search_documents(connection, context, {"query": "st-8721"})
    assert {hit["document_id"] for hit in lowered.data["hits"]} >= {T03_REMIT_ID, T03_FEE_ID}
    connection.close()


def test_cross_workspace_ids_are_not_readable(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection, WS_A)
    _seed_workspace(connection, WS_B)
    insert_document(
        connection,
        SourceDocument(
            document_id="DOC-SECRET",
            kind=DocumentKind.OTHER,
            title="Other workspace note",
            issued_date="2026-01-20",
            body_text="Secret to workspace B.",
            facts={},
        ).to_persisted(WS_B),
    )
    context_a = _context(WS_A)
    missing = read_document(connection, context_a, {"document_id": "DOC-SECRET"})
    assert missing.ok is False
    assert missing.error is not None
    assert missing.error.code == ValidationCode.NOT_FOUND.value
    search = search_documents(connection, context_a, {"query": "Secret to workspace B"})
    assert search.data["hits"] == []
    wrong_company = search_invoices(
        connection, _context(company_id="OTHERCO"), {"customer_id": CUST_HARBOR}
    )
    assert wrong_company.ok is False
    assert wrong_company.error is not None
    assert wrong_company.error.code == ValidationCode.WRONG_WORKSPACE.value
    connection.close()


def test_path_like_document_id_is_rejected(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    result = read_document(
        connection,
        context,
        {"document_id": "data/grading/CASE-1CFA9FEE848F.json"},
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == ErrorCode.INVALID_TOOL_ARGUMENTS.value
    assert _opened_ids(context) == []
    connection.close()


def test_invalid_extra_arguments_fail(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _seed_workspace(connection)
    context = _context()
    extra = search_invoices(
        connection,
        context,
        {"customer_id": CUST_HARBOR, "amount_cents": 50_000},
    )
    empty = search_documents(connection, context, {})
    assert extra.ok is False
    assert extra.error is not None
    assert extra.error.code == ErrorCode.INVALID_TOOL_ARGUMENTS.value
    assert empty.ok is False
    assert empty.error is not None
    assert empty.error.code == ErrorCode.INVALID_TOOL_ARGUMENTS.value
    connection.close()


def test_reread_does_not_duplicate_opened_evidence(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    context = _context()
    first = read_document(connection, context, {"document_id": T03_REMIT_ID})
    second = read_document(connection, context, {"document_id": T03_REMIT_ID})
    assert first.ok is True
    assert second.ok is True
    assert _opened_ids(context) == [T03_REMIT_ID]
    connection.close()


def test_list_helpers_are_workspace_ordered(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    _import_t03(connection)
    invoices = list_invoices(connection, WS_A)
    documents = list_documents(connection, WS_A)
    assert [item.invoice_id for item in invoices] == sorted(item.invoice_id for item in invoices)
    assert [item.document_id for item in documents] == sorted(
        item.document_id for item in documents
    )
    assert isinstance(documents[0], PersistedDocument)
    connection.close()
