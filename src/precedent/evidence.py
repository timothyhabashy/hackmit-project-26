"""Workspace-scoped invoice and document lookup.

Runtime search and document reads use the host-bound workspace in SQLite.
They never accept a filesystem path, database path, or grading location from
the caller. Search hits are candidates; only ``read_document`` marks evidence
as opened for the current run.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from precedent.db import get_document, get_workspace, list_documents, list_invoices
from precedent.models import (
    DOCUMENT_BODY_MAX_CHARS,
    Document,
    DocumentKind,
    ErrorCode,
    Identifier,
    InvoiceRecord,
    IsoDate,
    OpenedEvidence,
    RunContext,
    Sha256Hex,
    StrictInt,
    StrictModel,
    ToolError,
    ToolResult,
    ValidationCode,
)

SNIPPET_MAX_CHARS = 160
SEARCH_LIMIT_MAX = 10


class SearchInvoicesArgs(StrictModel):
    customer_id: Identifier | None = None
    invoice_id: Identifier | None = None
    query: str | None = Field(default=None, min_length=1, max_length=200)
    limit: StrictInt = Field(default=SEARCH_LIMIT_MAX, ge=1, le=SEARCH_LIMIT_MAX)

    @field_validator("customer_id", "invoice_id", "query", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _require_a_filter(self) -> SearchInvoicesArgs:
        if self.customer_id is None and self.invoice_id is None and self.query is None:
            raise ValueError("at least one of customer_id, invoice_id, or query is required")
        return self


class SearchDocumentsArgs(StrictModel):
    kind: DocumentKind | None = None
    customer_id: Identifier | None = None
    reference: Identifier | None = None
    query: str | None = Field(default=None, min_length=1, max_length=200)
    limit: StrictInt = Field(default=SEARCH_LIMIT_MAX, ge=1, le=SEARCH_LIMIT_MAX)

    @field_validator("customer_id", "reference", "query", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _require_a_filter(self) -> SearchDocumentsArgs:
        if (
            self.kind is None
            and self.customer_id is None
            and self.reference is None
            and self.query is None
        ):
            raise ValueError("at least one of kind, customer_id, reference, or query is required")
        return self


class ReadDocumentArgs(StrictModel):
    document_id: Identifier

    @field_validator("document_id", mode="before")
    @classmethod
    def _strip_id(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class InvoiceSearchHit(StrictModel):
    invoice_id: Identifier
    customer_id: Identifier
    currency: str
    outstanding_cents: StrictInt
    status: str
    issued_date: IsoDate


class InvoiceSearchPage(StrictModel):
    hits: list[InvoiceSearchHit]
    total: StrictInt = Field(ge=0)
    truncated: bool


class DocumentSearchHit(StrictModel):
    document_id: Identifier
    kind: DocumentKind
    title: str
    issued_date: IsoDate
    snippet: str = Field(max_length=SNIPPET_MAX_CHARS)
    sha256: Sha256Hex


class DocumentSearchPage(StrictModel):
    hits: list[DocumentSearchHit]
    total: StrictInt = Field(ge=0)
    truncated: bool


class OpenedDocumentView(StrictModel):
    document_id: Identifier
    kind: DocumentKind
    title: str
    issued_date: IsoDate
    body_text: str = Field(max_length=DOCUMENT_BODY_MAX_CHARS)
    facts: dict[str, Any]
    sha256: Sha256Hex


def search_invoices(
    connection: sqlite3.Connection,
    context: RunContext,
    payload: SearchInvoicesArgs | dict[str, Any],
) -> ToolResult:
    args = _parse_args(SearchInvoicesArgs, payload, "search_invoices")
    if isinstance(args, ToolResult):
        return args
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    path_error = _path_like_error(args.customer_id) or _path_like_error(args.invoice_id)
    if path_error is not None:
        return path_error

    matches = [
        invoice
        for invoice in list_invoices(connection, context.workspace_id)
        if _invoice_matches(invoice, args)
    ]
    limit = args.limit
    page = InvoiceSearchPage(
        hits=[_invoice_hit(invoice) for invoice in matches[:limit]],
        total=len(matches),
        truncated=len(matches) > limit,
    )
    return _ok(page, [hit.invoice_id for hit in page.hits])


def search_documents(
    connection: sqlite3.Connection,
    context: RunContext,
    payload: SearchDocumentsArgs | dict[str, Any],
) -> ToolResult:
    args = _parse_args(SearchDocumentsArgs, payload, "search_documents")
    if isinstance(args, ToolResult):
        return args
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    path_error = _path_like_error(args.customer_id) or _path_like_error(args.reference)
    if path_error is not None:
        return path_error

    needle = args.reference or args.query
    matches = [
        document
        for document in list_documents(connection, context.workspace_id)
        if _document_matches(document, args)
    ]
    limit = args.limit
    page = DocumentSearchPage(
        hits=[_document_hit(document, needle) for document in matches[:limit]],
        total=len(matches),
        truncated=len(matches) > limit,
    )
    return _ok(page, [hit.document_id for hit in page.hits])


def read_document(
    connection: sqlite3.Connection,
    context: RunContext,
    payload: ReadDocumentArgs | dict[str, Any],
) -> ToolResult:
    args = _parse_args(ReadDocumentArgs, payload, "read_document")
    if isinstance(args, ToolResult):
        return args
    scoped = _require_workspace(connection, context)
    if isinstance(scoped, ToolResult):
        return scoped
    path_error = _path_like_error(args.document_id)
    if path_error is not None:
        return path_error

    document = get_document(connection, context.workspace_id, args.document_id)
    if document is None:
        return _error(
            ValidationCode.NOT_FOUND.value,
            f"document {args.document_id} was not found in this workspace",
        )
    _mark_opened(context, document)
    view = OpenedDocumentView(
        document_id=document.document_id,
        kind=document.kind,
        title=document.title,
        issued_date=document.issued_date,
        body_text=document.body_text,
        facts=document.facts.model_dump(mode="json"),
        sha256=document.sha256,
    )
    return _ok(view, [document.document_id])


def _parse_args(
    model: type[StrictModel], payload: StrictModel | dict[str, Any], label: str
) -> Any | ToolResult:
    try:
        if isinstance(payload, model):
            return payload
        if not isinstance(payload, dict):
            raise ValueError(f"{label} arguments must be an object")
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


def _require_workspace(connection: sqlite3.Connection, context: RunContext) -> ToolResult | None:
    workspace = get_workspace(connection, context.workspace_id)
    if workspace is None or workspace.company_id != context.company_id:
        return _error(
            ValidationCode.WRONG_WORKSPACE.value,
            "the bound workspace is not available to this run",
        )
    return None


def _invoice_matches(invoice: InvoiceRecord, args: SearchInvoicesArgs) -> bool:
    if args.customer_id is not None and invoice.customer_id != args.customer_id:
        return False
    if args.invoice_id is not None and invoice.invoice_id != args.invoice_id:
        return False
    if args.query is not None and not _tokens_match(_invoice_haystack(invoice), args.query):
        return False
    return True


def _document_matches(document: Document, args: SearchDocumentsArgs) -> bool:
    if args.kind is not None and document.kind is not args.kind:
        return False
    customer_id = getattr(document.facts, "customer_id", None)
    if args.customer_id is not None and customer_id != args.customer_id:
        return False
    if args.reference is not None and args.reference not in _reference_values(document):
        return False
    if args.query is not None and not _tokens_match(_document_haystack(document), args.query):
        return False
    return True


def _invoice_haystack(invoice: InvoiceRecord) -> str:
    return " ".join(
        (
            invoice.invoice_id,
            invoice.customer_id,
            invoice.currency,
            invoice.issued_date,
            invoice.due_date,
            invoice.status.value,
            invoice.display_status,
        )
    )


def _document_haystack(document: Document) -> str:
    return " ".join(
        (
            document.document_id,
            document.kind.value,
            document.title,
            document.issued_date,
            document.body_text,
        )
    )


def _reference_values(document: Document) -> set[str]:
    values = {document.document_id}
    facts = document.facts
    for attr in ("bank_reference", "transfer_reference", "settlement_ticket", "invoice_id"):
        value = getattr(facts, attr, None)
        if isinstance(value, str) and value:
            values.add(value)
    invoice_ids = getattr(facts, "invoice_ids", None)
    if isinstance(invoice_ids, list):
        values.update(str(item) for item in invoice_ids)
    return values


def _invoice_hit(invoice: InvoiceRecord) -> InvoiceSearchHit:
    return InvoiceSearchHit(
        invoice_id=invoice.invoice_id,
        customer_id=invoice.customer_id,
        currency=invoice.currency,
        outstanding_cents=invoice.outstanding_cents,
        status=invoice.display_status,
        issued_date=invoice.issued_date,
    )


def _document_hit(document: Document, needle: str | None) -> DocumentSearchHit:
    return DocumentSearchHit(
        document_id=document.document_id,
        kind=document.kind,
        title=document.title,
        issued_date=document.issued_date,
        snippet=_snippet(document.body_text, needle),
        sha256=document.sha256,
    )


def _snippet(body: str, needle: str | None) -> str:
    text = " ".join(body.split())
    if not text:
        return ""
    haystack = text.lower()
    if needle:
        index = haystack.find(needle.lower())
        if index >= 0:
            start = max(0, index - 40)
            end = min(len(text), index + len(needle) + 80)
            snippet = text[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            return snippet[:SNIPPET_MAX_CHARS]
    return text[:SNIPPET_MAX_CHARS]


def _tokens_match(haystack: str, query: str) -> bool:
    blob = haystack.lower()
    return all(token in blob for token in query.lower().split())


def _mark_opened(context: RunContext, document: Document) -> None:
    if any(item.document_id == document.document_id for item in context.opened_documents):
        return
    context.opened_documents.append(
        OpenedEvidence(document_id=document.document_id, sha256=document.sha256)
    )


def _path_like_error(value: str | None) -> ToolResult | None:
    if value is None:
        return None
    normalized = value.replace("\\", "/").lower()
    if "/" in value or "\\" in value or ".." in value:
        return _error(
            ErrorCode.INVALID_TOOL_ARGUMENTS.value,
            "evidence identifiers cannot be filesystem paths",
        )
    parts = [part for part in normalized.split("/") if part]
    if "grading" in parts:
        return _error(
            ErrorCode.INVALID_TOOL_ARGUMENTS.value,
            "grading files are not readable through runtime evidence tools",
        )
    return None


def _blank_to_none(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _ok(model: StrictModel, source_ids: list[str]) -> ToolResult:
    return ToolResult(
        ok=True,
        data=model.model_dump(mode="json"),
        error=None,
        source_ids=source_ids,
    )


def _error(code: str, message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        data=None,
        error=ToolError(code=code, message=message),
        source_ids=[],
    )


__all__ = [
    "DocumentSearchHit",
    "DocumentSearchPage",
    "InvoiceSearchHit",
    "InvoiceSearchPage",
    "OpenedDocumentView",
    "ReadDocumentArgs",
    "SNIPPET_MAX_CHARS",
    "SearchDocumentsArgs",
    "SearchInvoicesArgs",
    "read_document",
    "search_documents",
    "search_invoices",
]
