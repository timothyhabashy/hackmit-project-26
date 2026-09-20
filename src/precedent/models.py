"""Canonical domain models, enums, money parsing, and hash helpers.

Provider SDK types stay out of this module. Settings remains defined in
``precedent.config`` and is re-exported here so later sessions share one shape.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from precedent.config import Settings

SCHEMA_VERSION = 1
ID_MAX_LENGTH = 80
MONEY_MAX_CENTS = 1_000_000_000_000
DOCUMENT_BODY_MAX_CHARS = 20_000
SUPPORTED_CURRENCY = "USD"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CENTS_STRING_PATTERN = re.compile(r"^(0|[1-9]\d*)$")
DECIMAL_MONEY_PATTERN = re.compile(r"^(0|[1-9]\d*)(\.\d{1,2})?$")
_CANONICAL_SEPARATORS = (",", ":")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvoiceSourceStatus(StrEnum):
    OPEN = "OPEN"
    VOID = "VOID"


class PaymentChannel(StrEnum):
    WIRE = "WIRE"
    ACH = "ACH"
    OTHER = "OTHER"


class DocumentKind(StrEnum):
    REMITTANCE = "REMITTANCE"
    BANK_FEE_NOTICE = "BANK_FEE_NOTICE"
    DISPUTE_NOTICE = "DISPUTE_NOTICE"
    OTHER = "OTHER"


class DisputeStatus(StrEnum):
    OPEN = "OPEN"
    WITHDRAWN = "WITHDRAWN"


class ResolutionType(StrEnum):
    EXACT_SINGLE = "EXACT_SINGLE"
    EXACT_BUNDLE = "EXACT_BUNDLE"
    SINGLE_WITH_BANK_FEE = "SINGLE_WITH_BANK_FEE"


class AgentOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    REVIEW = "REVIEW"
    ERROR = "ERROR"


class CaseState(StrEnum):
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    RESOLVED = "RESOLVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ERROR = "ERROR"


class PaymentState(StrEnum):
    UNAPPLIED = "UNAPPLIED"
    APPLIED = "APPLIED"


class LessonState(StrEnum):
    DRAFT = "DRAFT"
    TESTING = "TESTING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class ExecutionMode(StrEnum):
    LIVE = "LIVE"
    TEST = "TEST"
    HUMAN = "HUMAN"


class RunState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class ApplicationStatus(StrEnum):
    APPLIED = "APPLIED"
    REPLAYED = "REPLAYED"
    REJECTED = "REJECTED"


class LessonDraftStatus(StrEnum):
    CREATED = "CREATED"
    INELIGIBLE = "INELIGIBLE"
    ERROR = "ERROR"


class CandidateTestState(StrEnum):
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class EvaluationState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class EpisodeDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    BUDGET_SKIPPED = "BUDGET_SKIPPED"
    NOT_RUN = "NOT_RUN"


class DatasetSplit(StrEnum):
    TEACHING = "teaching"
    CANDIDATE = "candidate"
    HELDOUT = "heldout"


class MemoryMode(StrEnum):
    ON = "on"
    OFF = "off"


class RemittanceReferenceField(StrEnum):
    SETTLEMENT_TICKET = "settlement_ticket"
    TRANSFER_REFERENCE = "transfer_reference"


class HintTemplateKey(StrEnum):
    WIRE_FEE_LOOKUP_V1 = "wire_fee_lookup_v1"


class ValidationCode(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    NOT_FOUND = "NOT_FOUND"
    WRONG_WORKSPACE = "WRONG_WORKSPACE"
    PAYMENT_ALREADY_APPLIED = "PAYMENT_ALREADY_APPLIED"
    INVOICE_ALREADY_PAID = "INVOICE_ALREADY_PAID"
    VOID_INVOICE = "VOID_INVOICE"
    CUSTOMER_MISMATCH = "CUSTOMER_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    UNSUPPORTED_CHANNEL = "UNSUPPORTED_CHANNEL"
    OUT_OF_PERIOD = "OUT_OF_PERIOD"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    MISSING_REMITTANCE = "MISSING_REMITTANCE"
    MISSING_FEE_NOTICE = "MISSING_FEE_NOTICE"
    EVIDENCE_NOT_OPENED = "EVIDENCE_NOT_OPENED"
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    OPEN_DISPUTE = "OPEN_DISPUTE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    FEE_OVER_LIMIT = "FEE_OVER_LIMIT"
    UNSUPPORTED_FEE_TYPE = "UNSUPPORTED_FEE_TYPE"
    UNSUPPORTED_PARTIAL = "UNSUPPORTED_PARTIAL"
    UNSUPPORTED_OVERPAYMENT = "UNSUPPORTED_OVERPAYMENT"
    UNSUPPORTED_BUNDLE_FEE = "UNSUPPORTED_BUNDLE_FEE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    INVALID_PRECEDENT_REFERENCE = "INVALID_PRECEDENT_REFERENCE"
    STALE_LEDGER = "STALE_LEDGER"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class ErrorCode(StrEnum):
    MISSING_CREDENTIALS = "MISSING_CREDENTIALS"
    LIVE_DISABLED = "LIVE_DISABLED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MALFORMED_PROVIDER_RESPONSE = "MALFORMED_PROVIDER_RESPONSE"
    INVALID_TOOL_ARGUMENTS = "INVALID_TOOL_ARGUMENTS"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_TERMINAL_ACTION = "NO_TERMINAL_ACTION"
    INTERRUPTED = "INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CUSTOMER_NOT_ESTABLISHED = "CUSTOMER_NOT_ESTABLISHED"


class RunEventKind(StrEnum):
    RUN_STARTED = "run_started"
    MODEL_RESPONSE = "model_response"
    TOOL_CALLED = "tool_called"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    PROPOSAL_VALIDATED = "proposal_validated"
    PROPOSAL_REJECTED = "proposal_rejected"
    PRECEDENT_RETRIEVED = "precedent_retrieved"
    APPLICATION_COMMITTED = "application_committed"
    REVIEW_REQUESTED = "review_requested"
    RUN_FAILED = "run_failed"
    RUN_FINISHED = "run_finished"


def _require_identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("ID must be a string")
    if not value or value.strip() == "":
        raise ValueError("ID must be a nonempty string")
    if len(value) > ID_MAX_LENGTH:
        raise ValueError(f"ID must be at most {ID_MAX_LENGTH} characters")
    return value


def _require_currency(value: str) -> str:
    if not isinstance(value, str) or not CURRENCY_PATTERN.fullmatch(value):
        raise ValueError("currency must be exactly three uppercase ASCII letters")
    return value


def _require_iso_date(value: str) -> str:
    if not isinstance(value, str) or not ISO_DATE_PATTERN.fullmatch(value):
        raise ValueError("date must be ISO YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a real calendar day in ISO YYYY-MM-DD") from exc
    return value


def _require_utc_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a UTC ISO 8601 string")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp must be UTC ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


def _require_sha256(value: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError("hash must be 64 lowercase hex characters")
    return value


def _unique_ids(values: list[str], *, label: str) -> list[str]:
    seen: set[str] = set()
    for item in values:
        _require_identifier(item)
        if item in seen:
            raise ValueError(f"{label} must be unique")
        seen.add(item)
    return values


Identifier = Annotated[str, Field(min_length=1, max_length=ID_MAX_LENGTH)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
UtcTimestamp = Annotated[str, Field(min_length=20)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveCents = Annotated[StrictInt, Field(gt=0, le=MONEY_MAX_CENTS)]
NonNegativeCents = Annotated[StrictInt, Field(ge=0, le=MONEY_MAX_CENTS)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=96)]
ActorLabel = Annotated[str, Field(min_length=1, max_length=80)]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=_CANONICAL_SEPARATORS, ensure_ascii=False)


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_canonical(value: Any) -> str:
    return sha256_utf8(canonical_json(value))


def hash_model(model: BaseModel) -> str:
    return hash_canonical(model.model_dump(mode="json"))


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_cents(raw: str) -> int:
    """Parse CSV money: base-10 whole-number strings only."""
    if not isinstance(raw, str):
        raise ValueError("cents must be a base-10 whole-number string")
    text = raw.strip()
    if not CENTS_STRING_PATTERN.fullmatch(text):
        raise ValueError(f"cents must be a base-10 whole-number string, got {raw!r}")
    value = int(text)
    if value > MONEY_MAX_CENTS:
        raise ValueError(f"cents exceed the allowed maximum of {MONEY_MAX_CENTS}")
    return value


def parse_decimal_cents(raw: str) -> int:
    """Parse operator-entered decimal money text into integer cents.

    Accepts an optional leading ``$`` and thousands commas. Rejects floats,
    booleans, scientific notation, and more than two decimal places.
    """
    if type(raw) is not str:
        raise ValueError("money must be entered as decimal text, not a float or boolean")
    text = raw.strip()
    if text.startswith("$"):
        text = text[1:].strip()
    text = text.replace(",", "")
    if not DECIMAL_MONEY_PATTERN.fullmatch(text):
        raise ValueError(f"money must be decimal text with at most two places, got {raw!r}")
    cents = int((Decimal(text) * 100).to_integral_exact())
    if cents > MONEY_MAX_CENTS:
        raise ValueError(f"cents exceed the allowed maximum of {MONEY_MAX_CENTS}")
    return cents


def document_content_hash(
    *,
    kind: DocumentKind | str,
    title: str,
    issued_date: str,
    body_text: str,
    facts: Any,
) -> str:
    kind_value = kind.value if isinstance(kind, DocumentKind) else kind
    facts_value = facts.model_dump(mode="json") if isinstance(facts, BaseModel) else facts
    return hash_canonical(
        {
            "body_text": body_text,
            "facts": facts_value,
            "issued_date": issued_date,
            "kind": kind_value,
            "title": title,
        }
    )


class Company(StrictModel):
    company_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    cash_account_id: Identifier
    period_start: IsoDate
    period_end: IsoDate

    @field_validator("company_id", "cash_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("period_start", "period_end")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @model_validator(mode="after")
    def _period_order(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        return self


class Customer(StrictModel):
    customer_id: Identifier
    legal_name: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("customer_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class Invoice(StrictModel):
    invoice_id: Identifier
    customer_id: Identifier
    currency: CurrencyCode
    issued_date: IsoDate
    due_date: IsoDate
    original_cents: PositiveCents
    opening_outstanding_cents: NonNegativeCents
    status: InvoiceSourceStatus

    @field_validator("invoice_id", "customer_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("issued_date", "due_date")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @model_validator(mode="after")
    def _opening_within_original(self) -> Self:
        if self.opening_outstanding_cents > self.original_cents:
            raise ValueError("opening_outstanding_cents cannot exceed original_cents")
        return self


class Payment(StrictModel):
    payment_id: Identifier
    bank_transaction_id: Identifier
    bank_account_id: Identifier
    posted_date: IsoDate
    currency: CurrencyCode
    amount_cents: PositiveCents
    channel: PaymentChannel
    payer_text: str = Field(min_length=1, max_length=500)
    bank_reference: str = Field(min_length=1, max_length=80)

    @field_validator("payment_id", "bank_transaction_id", "bank_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("posted_date")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @field_validator("bank_reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        return _require_identifier(value)


class RemittanceFacts(StrictModel):
    customer_id: Identifier
    bank_reference: Identifier
    invoice_ids: list[Identifier] = Field(min_length=1, max_length=3)
    gross_settlement_cents: PositiveCents
    currency: CurrencyCode
    receiving_account_id: Identifier
    transfer_reference: Identifier | None = None
    settlement_ticket: Identifier | None = None

    @field_validator("customer_id", "bank_reference", "receiving_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("transfer_reference", "settlement_ticket")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("invoice_ids")
    @classmethod
    def _unique_invoices(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="invoice_ids")


class BankFeeNoticeFacts(StrictModel):
    customer_id: Identifier
    receiving_account_id: Identifier
    currency: CurrencyCode
    transfer_reference: Identifier
    fee_cents: PositiveCents
    fee_type: str = Field(min_length=1, max_length=80)
    gross_cents: PositiveCents
    net_cents: PositiveCents

    @field_validator("customer_id", "receiving_account_id", "transfer_reference")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)


class DisputeNoticeFacts(StrictModel):
    customer_id: Identifier
    invoice_id: Identifier
    currency: CurrencyCode
    disputed_cents: PositiveCents
    status: DisputeStatus

    @field_validator("customer_id", "invoice_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)


class OtherFacts(StrictModel):
    pass


DocumentFacts = RemittanceFacts | BankFeeNoticeFacts | DisputeNoticeFacts | OtherFacts

_FACTS_BY_KIND: dict[DocumentKind, type[StrictModel]] = {
    DocumentKind.REMITTANCE: RemittanceFacts,
    DocumentKind.BANK_FEE_NOTICE: BankFeeNoticeFacts,
    DocumentKind.DISPUTE_NOTICE: DisputeNoticeFacts,
    DocumentKind.OTHER: OtherFacts,
}


def parse_document_facts(kind: DocumentKind, data: Any) -> DocumentFacts:
    return _FACTS_BY_KIND[kind].model_validate({} if data is None else data)


class SourceDocument(StrictModel):
    document_id: Identifier
    kind: DocumentKind
    title: str = Field(min_length=1, max_length=200)
    issued_date: IsoDate
    body_text: str = Field(min_length=0, max_length=DOCUMENT_BODY_MAX_CHARS)
    facts: DocumentFacts

    @field_validator("document_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("issued_date")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @model_validator(mode="before")
    @classmethod
    def _coerce_facts(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        kind_raw = data.get("kind")
        facts = data.get("facts", {})
        try:
            kind = DocumentKind(kind_raw)
        except ValueError:
            return data
        parsed = dict(data)
        parsed["facts"] = parse_document_facts(kind, facts)
        return parsed

    @model_validator(mode="after")
    def _facts_match_kind(self) -> Self:
        expected = _FACTS_BY_KIND[self.kind]
        if not isinstance(self.facts, expected):
            raise ValueError(f"facts must match document kind {self.kind}")
        return self

    def content_hash(self) -> str:
        return document_content_hash(
            kind=self.kind,
            title=self.title,
            issued_date=self.issued_date,
            body_text=self.body_text,
            facts=self.facts,
        )

    def to_persisted(self, workspace_id: str, *, sha256: str | None = None) -> Document:
        return Document(
            workspace_id=workspace_id,
            sha256=_require_sha256(sha256 or self.content_hash()),
            **self.model_dump(),
        )


class Document(SourceDocument):
    workspace_id: Identifier
    sha256: Sha256Hex

    @field_validator("workspace_id")
    @classmethod
    def _workspace(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class CompanyPolicy(StrictModel):
    policy_id: Identifier
    company_id: Identifier
    allowed_cash_account_id: Identifier
    currency: CurrencyCode
    supported_channels: list[PaymentChannel] = Field(min_length=1, max_length=8)
    allow_receiving_wire_fee: StrictBool
    max_receiving_wire_fee_cents: NonNegativeCents
    max_bundle_invoices: StrictInt = Field(ge=1, le=3)
    allow_partial_settlement: StrictBool
    allow_writeoff: StrictBool
    require_remittance: StrictBool

    @field_validator("policy_id", "company_id", "allowed_cash_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("supported_channels")
    @classmethod
    def _unique_channels(cls, value: list[PaymentChannel]) -> list[PaymentChannel]:
        if len(value) != len(set(value)):
            raise ValueError("supported_channels must be unique")
        return value


class Allocation(StrictModel):
    invoice_id: Identifier
    cash_cents: PositiveCents
    fee_cents: NonNegativeCents

    @field_validator("invoice_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @model_validator(mode="after")
    def _bounded_sum(self) -> Self:
        total = self.cash_cents + self.fee_cents
        if total > MONEY_MAX_CENTS:
            raise ValueError("allocation cash_cents + fee_cents exceeds the money bound")
        return self


def _validate_allocations(allocations: list[Allocation]) -> list[Allocation]:
    if not 1 <= len(allocations) <= 3:
        raise ValueError("allocations must contain 1 to 3 items")
    _unique_ids([item.invoice_id for item in allocations], label="allocation invoice IDs")
    return allocations


class ResolutionProposal(StrictModel):
    payment_id: Identifier
    customer_id: Identifier
    resolution_type: ResolutionType
    allocations: list[Allocation] = Field(min_length=1, max_length=3)
    evidence_document_ids: list[Identifier] = Field(min_length=1, max_length=10)
    explanation: str = Field(min_length=1, max_length=1000)
    precedent_ids_used: list[Identifier] = Field(default_factory=list, max_length=3)

    @field_validator("payment_id", "customer_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("allocations")
    @classmethod
    def _allocs(cls, value: list[Allocation]) -> list[Allocation]:
        return _validate_allocations(value)

    @field_validator("evidence_document_ids")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="evidence_document_ids")

    @field_validator("precedent_ids_used")
    @classmethod
    def _precedents(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="precedent_ids_used")


class ValidationIssue(StrictModel):
    code: ValidationCode
    message: str = Field(min_length=1, max_length=1000)
    record_ids: list[Identifier] = Field(default_factory=list, max_length=20)

    @field_validator("record_ids")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="record_ids")


class ValidationReport(StrictModel):
    valid: StrictBool
    issues: list[ValidationIssue] = Field(default_factory=list, max_length=50)
    normalized_proposal: ResolutionProposal | None = None
    checked_ledger_revision: StrictInt = Field(ge=0)
    policy_id: Identifier
    evidence_hashes: list[Sha256Hex] = Field(default_factory=list, max_length=10)

    @field_validator("policy_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("evidence_hashes")
    @classmethod
    def _hashes(cls, value: list[str]) -> list[str]:
        return [_require_sha256(item) for item in _unique_ids(value, label="evidence_hashes")]

    @model_validator(mode="after")
    def _normalized_only_when_valid(self) -> Self:
        if self.valid:
            if self.normalized_proposal is None:
                raise ValueError("valid reports require a normalized_proposal")
            if self.issues:
                raise ValueError("valid reports cannot include validation issues")
        else:
            if self.normalized_proposal is not None:
                raise ValueError("invalid reports cannot include a normalized_proposal")
            if not self.issues:
                raise ValueError("invalid reports require at least one issue")
        return self


class ReviewRequest(StrictModel):
    reason_code: ValidationCode
    message: str = Field(min_length=1, max_length=1000)
    needed_information: str = Field(min_length=1, max_length=1000)
    evidence_document_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    candidate_invoice_ids: list[Identifier] = Field(default_factory=list, max_length=10)

    @field_validator("evidence_document_ids")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="evidence_document_ids")

    @field_validator("candidate_invoice_ids")
    @classmethod
    def _invoices(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="candidate_invoice_ids")


class CorrectionInput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_document_ids: list[Identifier] = Field(min_length=1, max_length=10)
    corrected_proposal: ResolutionProposal | None = None
    review_reason_code: ValidationCode | None = None

    @field_validator("evidence_document_ids")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="evidence_document_ids")


class HintScope(StrictModel):
    customer_id: Identifier
    bank_account_id: Identifier
    currency: CurrencyCode
    channel: PaymentChannel

    @field_validator("customer_id", "bank_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)


class HintLookup(StrictModel):
    remittance_reference_field: RemittanceReferenceField
    bank_notice_reference_field: Literal["transfer_reference"] = "transfer_reference"
    search_terms: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("search_terms")
    @classmethod
    def _terms(cls, value: list[str]) -> list[str]:
        for term in value:
            if not 1 <= len(term) <= 50:
                raise ValueError("each search term must be 1 to 50 characters")
        return value


class WireFeeLookupHint(StrictModel):
    template_key: Literal["wire_fee_lookup_v1"] = "wire_fee_lookup_v1"
    title: str = Field(min_length=1, max_length=100)
    scope: HintScope
    lookup: HintLookup
    summary: str = Field(min_length=1, max_length=500)


class SeedApplication(StrictModel):
    application_id: Identifier
    payment_id: Identifier
    allocations: list[Allocation] = Field(min_length=1, max_length=3)
    seeded: Literal[True] = True

    @field_validator("application_id", "payment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("allocations")
    @classmethod
    def _allocs(cls, value: list[Allocation]) -> list[Allocation]:
        return _validate_allocations(value)


class InitialLedger(StrictModel):
    applications: list[SeedApplication] = Field(default_factory=list, max_length=250)


class InvoiceRecord(Invoice):
    workspace_id: Identifier
    outstanding_cents: NonNegativeCents

    @field_validator("workspace_id")
    @classmethod
    def _workspace(cls, value: str) -> str:
        return _require_identifier(value)

    @model_validator(mode="after")
    def _outstanding_within_original(self) -> Self:
        if self.outstanding_cents > self.original_cents:
            raise ValueError("outstanding_cents cannot exceed original_cents")
        return self

    @property
    def display_status(self) -> str:
        if self.status is InvoiceSourceStatus.VOID:
            return InvoiceSourceStatus.VOID.value
        if self.outstanding_cents == 0:
            return "PAID"
        return InvoiceSourceStatus.OPEN.value


class PaymentRecord(Payment):
    workspace_id: Identifier
    applied: StrictBool = False

    @field_validator("workspace_id")
    @classmethod
    def _workspace(cls, value: str) -> str:
        return _require_identifier(value)

    @property
    def state(self) -> PaymentState:
        return PaymentState.APPLIED if self.applied else PaymentState.UNAPPLIED


class CustomerRecord(Customer):
    workspace_id: Identifier

    @field_validator("workspace_id")
    @classmethod
    def _workspace(cls, value: str) -> str:
        return _require_identifier(value)


class WorkspaceRecord(StrictModel):
    workspace_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    company_id: Identifier
    period_label: str = Field(min_length=1, max_length=80)
    period_start: IsoDate
    period_end: IsoDate
    dataset_hash: Sha256Hex
    policy: CompanyPolicy
    policy_hash: Sha256Hex
    ledger_revision: StrictInt = Field(default=0, ge=0)
    schema_version: StrictInt = Field(default=SCHEMA_VERSION, ge=1)
    created_at: UtcTimestamp

    @field_validator("workspace_id", "company_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("period_start", "period_end")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @field_validator("dataset_hash", "policy_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class CaseRecord(StrictModel):
    workspace_id: Identifier
    case_id: Identifier
    payment_id: Identifier
    state: CaseState
    current_run_id: Identifier | None = None
    latest_run_id: Identifier | None = None

    @field_validator("workspace_id", "case_id", "payment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("current_run_id", "latest_run_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class ApplicationRecord(StrictModel):
    application_id: Identifier
    workspace_id: Identifier
    payment_id: Identifier
    proposal_id: Identifier | None = None
    seeded: StrictBool
    idempotency_key: IdempotencyKey
    payload_hash: Sha256Hex
    actor: ActorLabel
    allocations: list[Allocation] = Field(min_length=1, max_length=3)
    created_at: UtcTimestamp

    @field_validator("application_id", "workspace_id", "payment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("proposal_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("payload_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("allocations")
    @classmethod
    def _allocs(cls, value: list[Allocation]) -> list[Allocation]:
        return _validate_allocations(value)

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)

    @model_validator(mode="after")
    def _seed_has_no_proposal(self) -> Self:
        if self.seeded and self.proposal_id is not None:
            raise ValueError("seeded applications cannot reference a proposal")
        if not self.seeded and self.proposal_id is None:
            raise ValueError("runtime applications require a proposal_id")
        return self


class ProposalRecord(StrictModel):
    proposal_id: Identifier
    run_id: Identifier
    workspace_id: Identifier
    case_id: Identifier
    payload: ResolutionProposal
    payload_hash: Sha256Hex
    validation_report: ValidationReport
    checked_revision: StrictInt = Field(ge=0)
    created_at: UtcTimestamp

    @field_validator("proposal_id", "run_id", "workspace_id", "case_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("payload_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class CorrectionRecord(StrictModel):
    correction_id: Identifier
    workspace_id: Identifier
    case_id: Identifier
    run_id: Identifier
    proposal_id: Identifier | None = None
    actor: ActorLabel
    text: str = Field(min_length=1, max_length=2000)
    cited_document_ids: list[Identifier] = Field(min_length=1, max_length=10)
    verified_resolved_proposal: ResolutionProposal | None = None
    lesson_eligibility: StrictBool
    eligibility_reason: str | None = Field(default=None, max_length=1000)
    application_id: Identifier | None = None
    created_at: UtcTimestamp

    @field_validator("correction_id", "workspace_id", "case_id", "run_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("proposal_id", "application_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("cited_document_ids")
    @classmethod
    def _docs(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="cited_document_ids")

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class PrecedentRecord(StrictModel):
    precedent_id: Identifier
    owner_workspace_id: Identifier
    company_id: Identifier
    family_id: Identifier
    version: StrictInt = Field(ge=1)
    status: LessonState
    hint: WireFeeLookupHint
    payload_hash: Sha256Hex
    correction_id: Identifier | None = None
    test_report_id: Identifier | None = None
    policy_hash: Sha256Hex | None = None
    behavior_fingerprint: Sha256Hex | None = None
    compiler_metadata: dict[str, Any] = Field(default_factory=dict)
    activation_actor: ActorLabel | None = None
    activation_at: UtcTimestamp | None = None
    retirement_actor: ActorLabel | None = None
    retirement_at: UtcTimestamp | None = None

    @field_validator("precedent_id", "owner_workspace_id", "company_id", "family_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("correction_id", "test_report_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("payload_hash", "policy_hash", "behavior_fingerprint")
    @classmethod
    def _hashes(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("activation_at", "retirement_at")
    @classmethod
    def _timestamps(cls, value: str | None) -> str | None:
        return None if value is None else _require_utc_timestamp(value)


class WorkspaceMemoryMembership(StrictModel):
    workspace_id: Identifier
    precedent_id: Identifier

    @field_validator("workspace_id", "precedent_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class BudgetLimits(StrictModel):
    max_model_calls: StrictInt = Field(gt=0)
    max_tool_calls: StrictInt = Field(gt=0)
    max_case_seconds: StrictInt = Field(gt=0)
    request_timeout_seconds: StrictInt = Field(gt=0)
    max_output_tokens: StrictInt = Field(gt=0)


class OpenedEvidence(StrictModel):
    document_id: Identifier
    sha256: Sha256Hex

    @field_validator("document_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class EvidenceRecord(StrictModel):
    workspace_id: Identifier
    case_id: Identifier
    run_id: Identifier
    document_id: Identifier
    sha256: Sha256Hex
    actor: ActorLabel
    opened_at: UtcTimestamp

    @field_validator("workspace_id", "case_id", "run_id", "document_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("opened_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class MemoryHintRef(StrictModel):
    precedent_id: Identifier
    version: StrictInt = Field(ge=1)
    payload_hash: Sha256Hex
    hint: WireFeeLookupHint | None = None

    @field_validator("precedent_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("payload_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class MemoryEligibilityNote(StrictModel):
    precedent_id: Identifier
    version: StrictInt | None = None
    included: StrictBool
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("precedent_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


class MemorySnapshot(StrictModel):
    hints: list[MemoryHintRef] = Field(default_factory=list, max_length=50)
    source_workspace_id: Identifier | None = None
    company_id: Identifier | None = None
    policy_id: Identifier | None = None
    policy_hash: Sha256Hex | None = None
    snapshot_hash: Sha256Hex

    @field_validator("source_workspace_id", "company_id", "policy_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("policy_hash", "snapshot_hash")
    @classmethod
    def _hashes(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)


class RunContext(StrictModel):
    workspace_id: Identifier
    case_id: Identifier
    run_id: Identifier
    company_id: Identifier
    policy_id: Identifier
    policy_hash: Sha256Hex
    dataset_hash: Sha256Hex
    initial_ledger_revision: StrictInt = Field(ge=0)
    execution_mode: ExecutionMode
    actor: ActorLabel
    opened_documents: list[OpenedEvidence] = Field(default_factory=list, max_length=250)
    retrieved_precedent_version_ids: list[Identifier] = Field(default_factory=list, max_length=32)
    memory_snapshot: MemorySnapshot
    budgets: BudgetLimits
    memory_mode: MemoryMode | None = None
    memory_eligibility: list[MemoryEligibilityNote] = Field(default_factory=list, max_length=50)
    retrieve_eligibility: list[MemoryEligibilityNote] = Field(default_factory=list, max_length=50)

    @field_validator(
        "workspace_id",
        "case_id",
        "run_id",
        "company_id",
        "policy_id",
    )
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("policy_hash", "dataset_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("retrieved_precedent_version_ids")
    @classmethod
    def _precedents(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="retrieved_precedent_version_ids")


class CaseSummary(StrictModel):
    case_id: Identifier
    payment_id: Identifier
    payer_text: str = Field(min_length=1, max_length=500)
    amount_cents: PositiveCents
    currency: CurrencyCode
    posted_date: IsoDate
    case_state: CaseState
    latest_run_id: Identifier | None = None
    latest_summary: str | None = Field(default=None, max_length=2000)

    @field_validator("case_id", "payment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("latest_run_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("posted_date")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)


class CaseSnapshot(StrictModel):
    summary: CaseSummary
    payment: PaymentRecord
    invoices: list[InvoiceRecord] = Field(default_factory=list, max_length=50)
    policy: CompanyPolicy
    ledger_revision: StrictInt = Field(ge=0)
    dataset_hash: Sha256Hex

    @field_validator("dataset_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class DocumentSummary(StrictModel):
    document_id: Identifier
    kind: DocumentKind
    title: str = Field(min_length=1, max_length=200)
    issued_date: IsoDate
    sha256: Sha256Hex

    @field_validator("document_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("issued_date")
    @classmethod
    def _dates(cls, value: str) -> str:
        return _require_iso_date(value)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class RunSummary(StrictModel):
    run_id: Identifier
    state: RunState
    mode: ExecutionMode
    terminal_outcome: AgentOutcome | None = None
    summary: str | None = Field(default=None, max_length=2000)

    @field_validator("run_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class CaseDetail(StrictModel):
    snapshot: CaseSnapshot
    proposals: list[ProposalRecord] = Field(default_factory=list, max_length=50)
    application: ApplicationRecord | None = None
    corrections: list[CorrectionRecord] = Field(default_factory=list, max_length=50)
    latest_run: RunSummary | None = None
    documents: list[DocumentSummary] = Field(default_factory=list, max_length=250)
    trace_references: list[str] = Field(default_factory=list, max_length=20)


class InvoiceOutstanding(StrictModel):
    invoice_id: Identifier
    outstanding_cents: NonNegativeCents

    @field_validator("invoice_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class ResultingBalances(StrictModel):
    payment_id: Identifier
    payment_applied: StrictBool
    invoices: list[InvoiceOutstanding] = Field(default_factory=list, max_length=50)

    @field_validator("payment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class ApplicationResult(StrictModel):
    status: ApplicationStatus
    application_id: Identifier | None = None
    proposal_id: Identifier | None = None
    issues: list[ValidationIssue] = Field(default_factory=list, max_length=50)
    resulting_balances: ResultingBalances | None = None
    ledger_revision: StrictInt = Field(ge=0)

    @field_validator("application_id", "proposal_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class Usage(StrictModel):
    model_attempts: StrictInt = Field(ge=0)
    tool_calls: StrictInt = Field(ge=0)
    input_tokens: StrictInt | None = None
    output_tokens: StrictInt | None = None
    elapsed_ms: StrictInt = Field(ge=0)
    estimated_cost_usd: Decimal | None = None
    price_rate_provenance: str | None = Field(default=None, max_length=80)


class ToolCall(StrictModel):
    call_id: Identifier
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]

    @field_validator("call_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class ProviderTurn(StrictModel):
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=30)
    public_text: str | None = Field(default=None, max_length=8000)
    stop_reason: str | None = Field(default=None, max_length=80)
    provider_assistant_message: Any = None
    usage: Usage
    provider_request_id: str | None = Field(default=None, max_length=200)


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=1000)


class RunResult(StrictModel):
    run_id: Identifier
    workspace_id: Identifier
    case_id: Identifier
    execution_mode: ExecutionMode
    terminal_outcome: AgentOutcome
    application_id: Identifier | None = None
    proposal_id: Identifier | None = None
    review: ReviewRequest | None = None
    error: ErrorDetail | None = None
    summary: str = Field(min_length=1, max_length=2000)
    usage: Usage
    trace_reference: str | None = Field(default=None, max_length=200)

    @field_validator("run_id", "workspace_id", "case_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("application_id", "proposal_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class ToolError(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=1000)


class ToolResult(StrictModel):
    ok: StrictBool
    data: Any = None
    error: ToolError | None = None
    source_ids: list[Identifier] = Field(default_factory=list, max_length=20)

    @field_validator("source_ids")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="source_ids")


class LessonDraftResult(StrictModel):
    status: LessonDraftStatus
    correction_id: Identifier
    precedent_id: Identifier | None = None
    version: StrictInt | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    compiler_run_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("correction_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("precedent_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class PairedCaseScore(StrictModel):
    case_id: Identifier
    baseline_run_id: Identifier | None = None
    memory_run_id: Identifier | None = None
    baseline_outcome: AgentOutcome | None = None
    memory_outcome: AgentOutcome | None = None
    baseline_correct: StrictBool | None = None
    memory_correct: StrictBool | None = None
    baseline_disposition: EpisodeDisposition | None = None
    memory_disposition: EpisodeDisposition | None = None
    baseline_reasons: list[str] = Field(default_factory=list, max_length=20)
    memory_reasons: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("case_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("baseline_run_id", "memory_run_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class CandidateCheckResults(StrictModel):
    validator_suite_passed: StrictBool
    lifecycle_suite_passed: StrictBool


class CandidateTestReport(StrictModel):
    report_id: Identifier
    candidate_version: StrictInt = Field(ge=1)
    candidate_payload_hash: Sha256Hex
    behavior_fingerprint: Sha256Hex
    policy_hash: Sha256Hex
    dev_suite_hash: Sha256Hex
    mode: ExecutionMode
    state: CandidateTestState
    episode_run_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    paired_scores: list[PairedCaseScore] = Field(default_factory=list, max_length=5)
    check_results: CandidateCheckResults
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None

    @field_validator("report_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator(
        "candidate_payload_hash",
        "behavior_fingerprint",
        "policy_hash",
        "dev_suite_hash",
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("episode_run_ids")
    @classmethod
    def _runs(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="episode_run_ids")

    @field_validator("started_at", "finished_at")
    @classmethod
    def _timestamps(cls, value: str | None) -> str | None:
        return None if value is None else _require_utc_timestamp(value)


class CandidateEpisodeRecord(StrictModel):
    case_id: Identifier
    arm: Literal["baseline", "candidate"]
    workspace_id: Identifier
    run_id: Identifier | None = None
    outcome: AgentOutcome | None = None
    correct: StrictBool | None = None
    review_code: ValidationCode | None = None
    retrieved_precedent_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    validator_rejection_codes: list[ValidationCode] = Field(default_factory=list, max_length=20)
    new_application_count: StrictInt = Field(ge=0)
    duration_ms: StrictInt = Field(ge=0)
    technical_error: StrictBool = False
    scope_violation: StrictBool = False
    invalid_proposal: StrictBool = False
    order_index: StrictInt = Field(ge=0)

    @field_validator("case_id", "workspace_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("run_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class LessonTestResult(StrictModel):
    precedent_id: Identifier
    report: CandidateTestReport
    episodes: list[CandidateEpisodeRecord] = Field(default_factory=list, max_length=10)
    failed_reasons: list[str] = Field(default_factory=list, max_length=20)
    passed_limited_checks: StrictBool
    demonstrated_useful_improvement: StrictBool | None = None

    @field_validator("precedent_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class LessonLifecycleResult(StrictModel):
    precedent_id: Identifier
    version: StrictInt = Field(ge=1)
    status: LessonState
    reason: str = Field(min_length=1, max_length=1000)
    previous_precedent_id: Identifier | None = None
    test_report_id: Identifier | None = None

    @field_validator("precedent_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("previous_precedent_id", "test_report_id")
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)


class EvaluationRequest(StrictModel):
    source_workspace_id: Identifier
    split: DatasetSplit
    case_limit: StrictInt | None = Field(default=None, gt=0)
    mode: ExecutionMode
    deadline_seconds: StrictInt = Field(gt=0)
    call_cap: StrictInt | None = Field(default=None, gt=0)
    output_dir: Path

    @field_validator("source_workspace_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @model_validator(mode="after")
    def _development_limit_and_mode(self) -> Self:
        if self.split is DatasetSplit.HELDOUT and self.case_limit is not None:
            raise ValueError("case_limit is not allowed for the held-out split")
        if self.mode not in (ExecutionMode.LIVE, ExecutionMode.TEST):
            raise ValueError("evaluation mode must be LIVE or TEST")
        return self


class MetricCount(StrictModel):
    value: StrictInt = Field(ge=0)
    denominator: StrictInt = Field(ge=0)


class EvaluationArmMetrics(StrictModel):
    correct_autonomous_resolutions: MetricCount
    correct_autonomous_resolutions_resolvable: MetricCount
    incorrect_automatic_resolutions: MetricCount
    correct_reviews: MetricCount
    unnecessary_reviews: MetricCount
    technical_errors: MetricCount
    timed_out: MetricCount
    budget_skipped: MetricCount
    rejected_financial_proposals: StrictInt = Field(ge=0)
    rejected_financial_proposals_by_code: dict[str, StrictInt] = Field(default_factory=dict)
    scope_violations: StrictInt = Field(ge=0)
    scope_violation_run_ids: list[Identifier] = Field(default_factory=list)
    evidence_completeness: MetricCount | None = None
    work_performed: Usage
    estimated_api_cost: Decimal | None = None
    price_rate_provenance: str | None = Field(default=None, max_length=120)

    @field_validator("scope_violation_run_ids")
    @classmethod
    def _run_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="scope_violation_run_ids")


class EvaluationMetrics(StrictModel):
    correct_autonomous_resolutions: MetricCount | None = None
    incorrect_automatic_resolutions: MetricCount | None = None
    correct_reviews: MetricCount | None = None
    unnecessary_reviews: MetricCount | None = None
    technical_errors: MetricCount | None = None
    positive_transfer_case_ids: list[Identifier] = Field(default_factory=list)
    negative_transfer_case_ids: list[Identifier] = Field(default_factory=list)
    rejected_financial_proposals: StrictInt | None = Field(default=None, ge=0)
    scope_violations: StrictInt | None = Field(default=None, ge=0)
    evidence_completeness: MetricCount | None = None
    work_performed: Usage | None = None
    estimated_api_cost: Decimal | None = None
    baseline: EvaluationArmMetrics | None = None
    memory: EvaluationArmMetrics | None = None
    both_correct_case_ids: list[Identifier] = Field(default_factory=list)
    baseline_both_correct_work: Usage | None = None
    memory_both_correct_work: Usage | None = None

    @field_validator(
        "positive_transfer_case_ids",
        "negative_transfer_case_ids",
        "both_correct_case_ids",
    )
    @classmethod
    def _case_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="evaluation metric case ids")


class FrozenEvaluationManifest(StrictModel):
    dataset_hash: Sha256Hex
    memory_snapshot_hash: Sha256Hex
    policy_hash: Sha256Hex
    prompt_hash: Sha256Hex
    tool_schema_hash: Sha256Hex
    behavior_fingerprint: Sha256Hex
    application_source_version: str = Field(min_length=1, max_length=80)
    budgets: BudgetLimits
    model: str | None = Field(default=None, max_length=200)
    seed: StrictInt | None = None
    split: DatasetSplit | None = None
    case_limit: StrictInt | None = Field(default=None, gt=0)
    case_ids: list[Identifier] = Field(default_factory=list)
    source_workspace_id: Identifier | None = None
    model_config_hash: Sha256Hex | None = None
    input_usd_per_million: str | None = Field(default=None, max_length=40)
    output_usd_per_million: str | None = Field(default=None, max_length=40)
    created_at: UtcTimestamp | None = None
    execution_mode: ExecutionMode | None = None

    @field_validator(
        "dataset_hash",
        "memory_snapshot_hash",
        "policy_hash",
        "prompt_hash",
        "tool_schema_hash",
        "behavior_fingerprint",
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("model_config_hash")
    @classmethod
    def _optional_hash(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("source_workspace_id")
    @classmethod
    def _optional_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value)

    @field_validator("case_ids")
    @classmethod
    def _case_ids(cls, value: list[str]) -> list[str]:
        return _unique_ids(value, label="case_ids")

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str | None) -> str | None:
        return None if value is None else _require_utc_timestamp(value)


class EvaluationReport(StrictModel):
    experiment_id: Identifier
    frozen_manifest: FrozenEvaluationManifest
    mode: ExecutionMode
    state: EvaluationState
    scheduled_count: StrictInt = Field(ge=0)
    completed_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    not_run_count: StrictInt = Field(ge=0)
    timed_out_count: StrictInt = Field(default=0, ge=0)
    budget_skipped_count: StrictInt = Field(default=0, ge=0)
    per_case_paired_scores: list[PairedCaseScore] = Field(default_factory=list)
    metrics: EvaluationMetrics
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    split: DatasetSplit | None = None

    @field_validator("experiment_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _timestamps(cls, value: str | None) -> str | None:
        return None if value is None else _require_utc_timestamp(value)


class RunEvent(StrictModel):
    run_id: Identifier
    sequence: StrictInt = Field(ge=1)
    event_kind: RunEventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcTimestamp

    @field_validator("run_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("created_at")
    @classmethod
    def _timestamp(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class RecordedRunManifest(StrictModel):
    run_id: Identifier
    original_started_at: UtcTimestamp
    mode: ExecutionMode
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    source_hash: Sha256Hex
    policy_hash: Sha256Hex
    prompt_hash: Sha256Hex
    tool_hash: Sha256Hex
    memory_hash: Sha256Hex
    public_trace: list[RunEvent] = Field(default_factory=list)
    input_snapshot: CaseSnapshot
    final_financial_snapshot: CaseSnapshot
    capture_timestamp: UtcTimestamp

    @field_validator("run_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator(
        "source_hash",
        "policy_hash",
        "prompt_hash",
        "tool_hash",
        "memory_hash",
    )
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("original_started_at", "capture_timestamp")
    @classmethod
    def _timestamps(cls, value: str) -> str:
        return _require_utc_timestamp(value)


class ForbiddenPrecedentScope(StrictModel):
    template_key: HintTemplateKey
    customer_id: Identifier
    bank_account_id: Identifier
    currency: CurrencyCode
    channel: PaymentChannel

    @field_validator("customer_id", "bank_account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return _require_currency(value)


class OracleRecord(StrictModel):
    case_id: Identifier
    expected_outcome: AgentOutcome
    expected_allocations: list[Allocation] = Field(default_factory=list, max_length=3)
    expected_ending_balances: dict[str, NonNegativeCents] = Field(default_factory=dict)
    expected_new_application_count: StrictInt = Field(ge=0)
    expected_payment_applied: StrictBool
    allowed_review_codes: list[ValidationCode] = Field(default_factory=list)
    forbidden_precedent_scopes: list[ForbiddenPrecedentScope] = Field(default_factory=list)
    must_preserve_other_records: StrictBool = True

    @field_validator("case_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)


class WorkspaceSummary(StrictModel):
    workspace_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    company_id: Identifier
    period_label: str = Field(min_length=1, max_length=80)
    dataset_hash: Sha256Hex
    policy_id: Identifier
    ledger_revision: StrictInt = Field(ge=0)
    schema_version: StrictInt = Field(ge=1)

    @field_validator("workspace_id", "company_id", "policy_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("dataset_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _require_sha256(value)


class RunRecord(StrictModel):
    run_id: Identifier
    workspace_id: Identifier
    case_id: Identifier
    mode: ExecutionMode
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    prompt_hash: Sha256Hex
    tool_hash: Sha256Hex
    memory_snapshot: MemorySnapshot
    state: RunState
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    budgets: BudgetLimits
    usage: Usage | None = None
    terminal_result: AgentOutcome | None = None

    @field_validator("run_id", "workspace_id", "case_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _require_identifier(value)

    @field_validator("prompt_hash", "tool_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _timestamps(cls, value: str | None) -> str | None:
        return None if value is None else _require_utc_timestamp(value)


__all__ = [
    "SCHEMA_VERSION",
    "ID_MAX_LENGTH",
    "MONEY_MAX_CENTS",
    "DOCUMENT_BODY_MAX_CHARS",
    "SUPPORTED_CURRENCY",
    "Settings",
    "AgentOutcome",
    "Allocation",
    "ApplicationRecord",
    "ApplicationResult",
    "ApplicationStatus",
    "BankFeeNoticeFacts",
    "BudgetLimits",
    "CandidateCheckResults",
    "CandidateEpisodeRecord",
    "CandidateTestReport",
    "CandidateTestState",
    "CaseDetail",
    "CaseRecord",
    "CaseSnapshot",
    "CaseState",
    "CaseSummary",
    "Company",
    "CompanyPolicy",
    "CorrectionInput",
    "CorrectionRecord",
    "Customer",
    "CustomerRecord",
    "DatasetSplit",
    "DisputeNoticeFacts",
    "DisputeStatus",
    "Document",
    "DocumentKind",
    "DocumentSummary",
    "ErrorCode",
    "EpisodeDisposition",
    "ErrorDetail",
    "EvidenceRecord",
    "EvaluationArmMetrics",
    "EvaluationMetrics",
    "EvaluationReport",
    "EvaluationRequest",
    "EvaluationState",
    "ExecutionMode",
    "ForbiddenPrecedentScope",
    "FrozenEvaluationManifest",
    "HintLookup",
    "HintScope",
    "HintTemplateKey",
    "InitialLedger",
    "Invoice",
    "InvoiceOutstanding",
    "InvoiceRecord",
    "InvoiceSourceStatus",
    "LessonDraftResult",
    "LessonDraftStatus",
    "LessonLifecycleResult",
    "LessonState",
    "LessonTestResult",
    "MemoryEligibilityNote",
    "MemoryHintRef",
    "MemoryMode",
    "MemorySnapshot",
    "MetricCount",
    "OpenedEvidence",
    "OracleRecord",
    "OtherFacts",
    "PairedCaseScore",
    "Payment",
    "PaymentChannel",
    "PaymentRecord",
    "PaymentState",
    "PrecedentRecord",
    "ProposalRecord",
    "ProviderTurn",
    "RecordedRunManifest",
    "RemittanceFacts",
    "RemittanceReferenceField",
    "ResolutionProposal",
    "ResolutionType",
    "ResultingBalances",
    "ReviewRequest",
    "RunContext",
    "RunEvent",
    "RunEventKind",
    "RunRecord",
    "RunResult",
    "RunState",
    "RunSummary",
    "SeedApplication",
    "SourceDocument",
    "ToolCall",
    "ToolError",
    "ToolResult",
    "Usage",
    "ValidationCode",
    "ValidationIssue",
    "ValidationReport",
    "WireFeeLookupHint",
    "WorkspaceMemoryMembership",
    "WorkspaceRecord",
    "WorkspaceSummary",
    "canonical_json",
    "document_content_hash",
    "hash_canonical",
    "hash_model",
    "parse_cents",
    "parse_decimal_cents",
    "parse_document_facts",
    "sha256_utf8",
    "utc_now_iso",
]
