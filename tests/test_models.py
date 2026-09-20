from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from precedent.config import load_settings
from precedent.models import (
    MONEY_MAX_CENTS,
    SCHEMA_VERSION,
    Allocation,
    ApplicationRecord,
    BankFeeNoticeFacts,
    BudgetLimits,
    CaseState,
    CompanyPolicy,
    CorrectionInput,
    Customer,
    DatasetSplit,
    DocumentKind,
    EvaluationRequest,
    ExecutionMode,
    HintLookup,
    HintScope,
    Invoice,
    InvoiceRecord,
    InvoiceSourceStatus,
    MemorySnapshot,
    Payment,
    PaymentChannel,
    RemittanceFacts,
    ResolutionProposal,
    ResolutionType,
    RunContext,
    Settings,
    SourceDocument,
    ValidationCode,
    ValidationIssue,
    ValidationReport,
    WireFeeLookupHint,
    canonical_json,
    document_content_hash,
    hash_canonical,
    parse_cents,
    parse_decimal_cents,
)

HASH_A = "a" * 64
CREATED = "2026-01-20T00:00:00Z"

NORTHSTAR_POLICY = CompanyPolicy(
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


def _invoice(**overrides: object) -> Invoice:
    payload: dict[str, object] = {
        "invoice_id": "INV-1042",
        "customer_id": "CUST-HARBOR",
        "currency": "USD",
        "issued_date": "2026-01-02",
        "due_date": "2026-01-20",
        "original_cents": 1_000_000,
        "opening_outstanding_cents": 1_000_000,
        "status": InvoiceSourceStatus.OPEN,
    }
    payload.update(overrides)
    return Invoice.model_validate(payload)


def _payment(**overrides: object) -> Payment:
    payload: dict[str, object] = {
        "payment_id": "PAY-201",
        "bank_transaction_id": "BANK-TX-201",
        "bank_account_id": "CASH-US-01",
        "posted_date": "2026-01-20",
        "currency": "USD",
        "amount_cents": 996_500,
        "channel": PaymentChannel.WIRE,
        "payer_text": "Harbor Treasury",
        "bank_reference": "BR-201",
    }
    payload.update(overrides)
    return Payment.model_validate(payload)


def _allocation(**overrides: object) -> Allocation:
    payload: dict[str, object] = {
        "invoice_id": "INV-1042",
        "cash_cents": 996_500,
        "fee_cents": 3500,
    }
    payload.update(overrides)
    return Allocation.model_validate(payload)


def _proposal(**overrides: object) -> ResolutionProposal:
    payload: dict[str, object] = {
        "payment_id": "PAY-201",
        "customer_id": "CUST-HARBOR",
        "resolution_type": ResolutionType.SINGLE_WITH_BANK_FEE,
        "allocations": [_allocation()],
        "evidence_document_ids": ["DOC-R201", "DOC-F201"],
        "explanation": (
            "The remittance identifies this invoice; its ticket matches the bank fee notice."
        ),
        "precedent_ids_used": [],
    }
    payload.update(overrides)
    return ResolutionProposal.model_validate(payload)


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


def test_settings_is_config_settings() -> None:
    from precedent import config

    assert Settings is config.Settings


def test_settings_repr_excludes_secret(tmp_path: Path) -> None:
    settings = load_settings(
        environ={"ANTHROPIC_API_KEY": "secret-test-key"},
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )
    assert "secret-test-key" not in repr(settings)


def test_modules_import_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PRECEDENT_ENABLE_LIVE", raising=False)
    sys.modules.pop("precedent.models", None)
    sys.modules.pop("precedent.db", None)
    import precedent.db as db
    import precedent.models as models

    assert models.SCHEMA_VERSION == 1
    assert db.SCHEMA_VERSION == 1


@pytest.mark.parametrize("amount", [True, False, 1.5, "12.50", "996500", 1e3])
def test_payment_rejects_non_strict_integer_money(amount: object) -> None:
    with pytest.raises(ValidationError):
        _payment(amount_cents=amount)


def test_payment_rejects_negative_and_zero_and_overflow() -> None:
    with pytest.raises(ValidationError):
        _payment(amount_cents=-1)
    with pytest.raises(ValidationError):
        _payment(amount_cents=0)
    with pytest.raises(ValidationError):
        _payment(amount_cents=MONEY_MAX_CENTS + 1)


def test_invoice_rejects_opening_above_original_and_zero_original() -> None:
    with pytest.raises(ValidationError):
        _invoice(original_cents=100, opening_outstanding_cents=101)
    with pytest.raises(ValidationError):
        _invoice(original_cents=0, opening_outstanding_cents=0)


def test_invoice_accepts_zero_opening_balance() -> None:
    invoice = _invoice(opening_outstanding_cents=0)
    assert invoice.opening_outstanding_cents == 0


def test_invoice_record_paid_display_status() -> None:
    record = InvoiceRecord(
        workspace_id="WS-DEMO",
        outstanding_cents=0,
        **_invoice(opening_outstanding_cents=0).model_dump(),
    )
    assert record.display_status == "PAID"


@pytest.mark.parametrize("currency", ["usd", "US", "USDT", "US$", "€UR", "USD1"])
def test_malformed_currency_is_rejected(currency: str) -> None:
    with pytest.raises(ValidationError):
        _invoice(currency=currency)
    with pytest.raises(ValidationError):
        _payment(currency=currency)


def test_unsupported_but_well_formed_currency_is_importable() -> None:
    invoice = _invoice(currency="EUR")
    payment = _payment(currency="EUR")
    assert invoice.currency == "EUR"
    assert payment.currency == "EUR"


def test_invalid_enum_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _invoice(status="PAID")
    with pytest.raises(ValidationError):
        _payment(channel="CHECK")
    with pytest.raises(ValidationError):
        _proposal(resolution_type="WRITE_OFF")


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        Payment.model_validate({**_payment().model_dump(), "customer_id": "CUST-HARBOR"})
    with pytest.raises(ValidationError):
        Invoice.model_validate({**_invoice().model_dump(), "foo": 1})


def test_ids_must_be_nonempty_and_bounded() -> None:
    with pytest.raises(ValidationError):
        _invoice(invoice_id="")
    with pytest.raises(ValidationError):
        _invoice(invoice_id="I" * 81)
    with pytest.raises(ValidationError):
        Customer.model_validate(
            {"customer_id": "   ", "legal_name": "Harbor Labs LLC", "display_name": "Harbor"}
        )


def test_allocation_rejects_negative_fee_and_non_positive_cash() -> None:
    with pytest.raises(ValidationError):
        _allocation(fee_cents=-1)
    with pytest.raises(ValidationError):
        _allocation(cash_cents=0)
    with pytest.raises(ValidationError):
        _allocation(cash_cents=True)


def test_proposal_rejects_duplicate_invoice_ids_and_host_fields() -> None:
    with pytest.raises(ValidationError):
        _proposal(
            allocations=[
                _allocation(invoice_id="INV-1", cash_cents=1, fee_cents=0),
                _allocation(invoice_id="INV-1", cash_cents=2, fee_cents=0),
            ]
        )
    with pytest.raises(ValidationError):
        ResolutionProposal.model_validate({**_proposal().model_dump(), "proposal_id": "PR-1"})


def test_proposal_accepts_teaching_fee_example() -> None:
    proposal = _proposal()
    assert proposal.allocations[0].cash_cents == 996_500
    assert proposal.allocations[0].fee_cents == 3500


def test_invalid_validation_report_cannot_carry_a_normalized_proposal() -> None:
    issue = ValidationIssue(code=ValidationCode.AMOUNT_MISMATCH, message="One cent short.")
    with pytest.raises(ValidationError):
        ValidationReport(
            valid=False,
            issues=[issue],
            normalized_proposal=_proposal(),
            checked_ledger_revision=0,
            policy_id="northstar-usd-v1",
            evidence_hashes=[],
        )
    with pytest.raises(ValidationError):
        ValidationReport(
            valid=True,
            issues=[],
            normalized_proposal=None,
            checked_ledger_revision=0,
            policy_id="northstar-usd-v1",
            evidence_hashes=[],
        )


def test_source_document_facts_must_match_kind() -> None:
    remittance = SourceDocument(
        document_id="DOC-R201",
        kind=DocumentKind.REMITTANCE,
        title="Harbor payment advice",
        issued_date="2026-01-20",
        body_text="Harbor Labs paid invoice INV-1042.",
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
    )
    digest = remittance.content_hash()
    assert digest == document_content_hash(
        kind=remittance.kind,
        title=remittance.title,
        issued_date=remittance.issued_date,
        body_text=remittance.body_text,
        facts=remittance.facts,
    )
    persisted = remittance.to_persisted("WS-DEMO")
    assert persisted.sha256 == digest
    with pytest.raises(ValidationError):
        SourceDocument.model_validate(
            {
                "document_id": "DOC-X",
                "kind": "OTHER",
                "title": "noise",
                "issued_date": "2026-01-20",
                "body_text": "ignore this",
                "facts": {"customer_id": "CUST-HARBOR"},
            }
        )


def test_bank_fee_notice_allows_unsupported_fee_type_string() -> None:
    facts = BankFeeNoticeFacts(
        customer_id="CUST-HARBOR",
        receiving_account_id="CASH-US-01",
        currency="USD",
        transfer_reference="ST-8721",
        fee_cents=3500,
        fee_type="SOME_OTHER_FEE",
        gross_cents=1_000_000,
        net_cents=996_500,
    )
    assert facts.fee_type == "SOME_OTHER_FEE"


def test_hint_template_bounds() -> None:
    hint = WireFeeLookupHint(
        title="Follow Harbor's settlement ticket to the bank notice",
        scope=HintScope(
            customer_id="CUST-HARBOR",
            bank_account_id="CASH-US-01",
            currency="USD",
            channel=PaymentChannel.WIRE,
        ),
        lookup=HintLookup(
            remittance_reference_field="settlement_ticket",
            search_terms=["bank notice", "receiving fee"],
        ),
        summary="Use the current remittance ticket to locate the current bank notice.",
    )
    assert hint.lookup.bank_notice_reference_field == "transfer_reference"
    with pytest.raises(ValidationError):
        HintLookup.model_validate(
            {
                "remittance_reference_field": "settlement_ticket",
                "bank_notice_reference_field": "settlement_ticket",
                "search_terms": [],
            }
        )


def test_correction_input_bounds() -> None:
    payload = CorrectionInput(
        text="Look up the settlement ticket in the bank notice.",
        evidence_document_ids=["DOC-R201", "DOC-F201"],
        corrected_proposal=_proposal(),
        review_reason_code=None,
    )
    assert payload.corrected_proposal is not None
    with pytest.raises(ValidationError):
        CorrectionInput(text="x", evidence_document_ids=[])


def test_run_context_has_no_oracle_and_forbids_extra_fields() -> None:
    context = RunContext(
        workspace_id="WS-DEMO",
        case_id="CASE-201",
        run_id="RUN-1",
        company_id="NORTHSTAR",
        policy_id="northstar-usd-v1",
        policy_hash=HASH_A,
        dataset_hash=HASH_A,
        initial_ledger_revision=0,
        execution_mode=ExecutionMode.LIVE,
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
    assert not hasattr(context, "oracle")
    with pytest.raises(ValidationError):
        RunContext.model_validate({**context.model_dump(), "oracle": {"expected": "RESOLVED"}})


def test_seeded_application_cannot_reference_a_proposal() -> None:
    with pytest.raises(ValidationError):
        ApplicationRecord(
            application_id="APP-1",
            workspace_id="WS-DEMO",
            payment_id="PAY-201",
            proposal_id="PR-1",
            seeded=True,
            idempotency_key="seed:APP-1",
            payload_hash=HASH_A,
            actor="seed_import",
            allocations=[_allocation(cash_cents=1_000_000, fee_cents=0)],
            created_at=CREATED,
        )


def test_parse_cents_accepts_whole_number_strings_only() -> None:
    assert parse_cents("0") == 0
    assert parse_cents("996500") == 996_500
    assert parse_cents(" 12 ") == 12
    for raw in ["12.50", "1.5", "true", "-3", "1e3", "+12", "00", ""]:
        with pytest.raises(ValueError):
            parse_cents(raw)
    with pytest.raises(ValueError):
        parse_cents(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_cents(12)  # type: ignore[arg-type]


def test_parse_decimal_cents_uses_text_not_floats() -> None:
    assert parse_decimal_cents("9965.00") == 996_500
    assert parse_decimal_cents("$9,965.00") == 996_500
    assert parse_decimal_cents("35") == 3500
    assert parse_decimal_cents("0.00") == 0
    assert parse_decimal_cents(" 35.5 ") == 3550
    for raw in ["", "1e2", "12.345", "-1", "+12", "true", "00.1"]:
        with pytest.raises(ValueError):
            parse_decimal_cents(raw)
    with pytest.raises(ValueError):
        parse_decimal_cents(9965.00)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_decimal_cents(True)  # type: ignore[arg-type]


def test_canonical_hash_is_stable_and_order_independent_for_objects() -> None:
    left = hash_canonical({"b": 2, "a": 1})
    right = hash_canonical({"a": 1, "b": 2})
    assert left == right
    assert left == hash_canonical({"a": 1, "b": 2})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_validation_code_enum_contains_required_codes() -> None:
    required = {
        "INVALID_SCHEMA",
        "NOT_FOUND",
        "WRONG_WORKSPACE",
        "PAYMENT_ALREADY_APPLIED",
        "INVOICE_ALREADY_PAID",
        "VOID_INVOICE",
        "CUSTOMER_MISMATCH",
        "CURRENCY_MISMATCH",
        "UNSUPPORTED_CURRENCY",
        "ACCOUNT_MISMATCH",
        "UNSUPPORTED_CHANNEL",
        "OUT_OF_PERIOD",
        "FUTURE_EVIDENCE",
        "MISSING_REMITTANCE",
        "MISSING_FEE_NOTICE",
        "EVIDENCE_NOT_OPENED",
        "REFERENCE_MISMATCH",
        "CONFLICTING_EVIDENCE",
        "OPEN_DISPUTE",
        "AMOUNT_MISMATCH",
        "FEE_OVER_LIMIT",
        "UNSUPPORTED_FEE_TYPE",
        "UNSUPPORTED_PARTIAL",
        "UNSUPPORTED_OVERPAYMENT",
        "UNSUPPORTED_BUNDLE_FEE",
        "AMBIGUOUS_MATCH",
        "DUPLICATE_INVOICE",
        "INVALID_PRECEDENT_REFERENCE",
        "STALE_LEDGER",
        "IDEMPOTENCY_CONFLICT",
    }
    assert required <= {item.value for item in ValidationCode}


def test_evaluation_request_rejects_heldout_limit_and_human_mode() -> None:
    with pytest.raises(ValidationError):
        EvaluationRequest(
            source_workspace_id="WS-DEMO",
            split=DatasetSplit.HELDOUT,
            case_limit=2,
            mode=ExecutionMode.LIVE,
            deadline_seconds=5400,
            output_dir=Path("artifacts/evaluations"),
        )
    with pytest.raises(ValidationError):
        EvaluationRequest(
            source_workspace_id="WS-DEMO",
            split=DatasetSplit.TEACHING,
            mode=ExecutionMode.HUMAN,
            deadline_seconds=120,
            output_dir=Path("artifacts/evaluations"),
        )


def test_policy_matches_fixed_company_document() -> None:
    assert NORTHSTAR_POLICY.policy_id == "northstar-usd-v1"
    assert NORTHSTAR_POLICY.currency == "USD"
    assert NORTHSTAR_POLICY.max_receiving_wire_fee_cents == 5000
    assert CaseState.NEEDS_REVIEW.value == "NEEDS_REVIEW"
