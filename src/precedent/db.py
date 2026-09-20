"""SQLite connection, schema initialization, transactions, and typed row helpers.

This is a thin persistence module, not a repository framework. Callers should
prefer a fresh connection per service operation. Ledger apply/replay logic
lives in ``precedent.ledger`` and uses these helpers inside one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from precedent.models import (
    ID_MAX_LENGTH,
    MONEY_MAX_CENTS,
    SCHEMA_VERSION,
    AgentOutcome,
    Allocation,
    ApplicationRecord,
    BudgetLimits,
    CandidateTestReport,
    CandidateTestState,
    CaseRecord,
    CaseState,
    CompanyPolicy,
    CorrectionRecord,
    CustomerRecord,
    Document,
    DocumentKind,
    EvaluationReport,
    ExecutionMode,
    InvoiceRecord,
    InvoiceSourceStatus,
    LessonState,
    MemorySnapshot,
    PaymentChannel,
    PaymentRecord,
    PrecedentRecord,
    ProposalRecord,
    ResolutionProposal,
    RunEvent,
    RunEventKind,
    RunRecord,
    RunState,
    Usage,
    ValidationReport,
    WireFeeLookupHint,
    WorkspaceRecord,
    canonical_json,
    hash_canonical,
    parse_document_facts,
    utc_now_iso,
)

_ID = f"length({{col}}) BETWEEN 1 AND {ID_MAX_LENGTH}"
_CURRENCY = "{col} GLOB '[A-Z][A-Z][A-Z]'"
_MONEY_POS = f"{{col}} > 0 AND {{col}} <= {MONEY_MAX_CENTS}"
_MONEY_NONNEG = f"{{col}} >= 0 AND {{col}} <= {MONEY_MAX_CENTS}"

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS workspaces (
      workspace_id TEXT PRIMARY KEY CHECK ({_ID.format(col="workspace_id")}),
      name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
      company_id TEXT NOT NULL CHECK ({_ID.format(col="company_id")}),
      period_label TEXT NOT NULL CHECK (length(period_label) BETWEEN 1 AND 80),
      period_start TEXT NOT NULL,
      period_end TEXT NOT NULL,
      dataset_hash TEXT NOT NULL CHECK (length(dataset_hash) = 64),
      policy_json TEXT NOT NULL,
      policy_hash TEXT NOT NULL CHECK (length(policy_hash) = 64),
      ledger_revision INTEGER NOT NULL DEFAULT 0 CHECK (ledger_revision >= 0),
      schema_version INTEGER NOT NULL DEFAULT {SCHEMA_VERSION} CHECK (schema_version >= 1),
      created_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS customers (
      workspace_id TEXT NOT NULL,
      customer_id TEXT NOT NULL CHECK ({_ID.format(col="customer_id")}),
      legal_name TEXT NOT NULL CHECK (length(legal_name) BETWEEN 1 AND 200),
      display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
      PRIMARY KEY (workspace_id, customer_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS invoices (
      workspace_id TEXT NOT NULL,
      invoice_id TEXT NOT NULL CHECK ({_ID.format(col="invoice_id")}),
      customer_id TEXT NOT NULL CHECK ({_ID.format(col="customer_id")}),
      currency TEXT NOT NULL CHECK ({_CURRENCY.format(col="currency")}),
      issued_date TEXT NOT NULL,
      due_date TEXT NOT NULL,
      original_cents INTEGER NOT NULL CHECK ({_MONEY_POS.format(col="original_cents")}),
      opening_outstanding_cents INTEGER NOT NULL
        CHECK ({_MONEY_NONNEG.format(col="opening_outstanding_cents")}),
      outstanding_cents INTEGER NOT NULL
        CHECK ({_MONEY_NONNEG.format(col="outstanding_cents")}),
      status TEXT NOT NULL CHECK (status IN ('OPEN', 'VOID')),
      PRIMARY KEY (workspace_id, invoice_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
      FOREIGN KEY (workspace_id, customer_id)
        REFERENCES customers(workspace_id, customer_id),
      CHECK (opening_outstanding_cents <= original_cents),
      CHECK (outstanding_cents <= original_cents)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS payments (
      workspace_id TEXT NOT NULL,
      payment_id TEXT NOT NULL CHECK ({_ID.format(col="payment_id")}),
      bank_transaction_id TEXT NOT NULL CHECK ({_ID.format(col="bank_transaction_id")}),
      bank_account_id TEXT NOT NULL CHECK ({_ID.format(col="bank_account_id")}),
      posted_date TEXT NOT NULL,
      currency TEXT NOT NULL CHECK ({_CURRENCY.format(col="currency")}),
      amount_cents INTEGER NOT NULL CHECK ({_MONEY_POS.format(col="amount_cents")}),
      channel TEXT NOT NULL CHECK (channel IN ('WIRE', 'ACH', 'OTHER')),
      payer_text TEXT NOT NULL,
      bank_reference TEXT NOT NULL CHECK ({_ID.format(col="bank_reference")}),
      applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
      PRIMARY KEY (workspace_id, payment_id),
      UNIQUE (workspace_id, bank_transaction_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
      workspace_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (
        kind IN ('REMITTANCE', 'BANK_FEE_NOTICE', 'DISPUTE_NOTICE', 'OTHER')
      ),
      title TEXT NOT NULL,
      issued_date TEXT NOT NULL,
      body_text TEXT NOT NULL,
      facts_json TEXT NOT NULL,
      sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
      PRIMARY KEY (workspace_id, document_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cases (
      workspace_id TEXT NOT NULL,
      case_id TEXT NOT NULL,
      payment_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK (
        state IN ('OPEN', 'RUNNING', 'RESOLVED', 'NEEDS_REVIEW', 'ERROR')
      ),
      current_run_id TEXT,
      latest_run_id TEXT,
      PRIMARY KEY (workspace_id, case_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
      FOREIGN KEY (workspace_id, payment_id)
        REFERENCES payments(workspace_id, payment_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      case_id TEXT NOT NULL,
      mode TEXT NOT NULL CHECK (mode IN ('LIVE', 'TEST', 'HUMAN')),
      provider TEXT,
      model TEXT,
      prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
      tool_hash TEXT NOT NULL CHECK (length(tool_hash) = 64),
      memory_snapshot_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (
        state IN ('RUNNING', 'COMPLETE', 'FAILED', 'INTERRUPTED')
      ),
      started_at TEXT NOT NULL,
      finished_at TEXT,
      max_model_calls INTEGER NOT NULL CHECK (max_model_calls > 0),
      max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls > 0),
      max_case_seconds INTEGER NOT NULL CHECK (max_case_seconds > 0),
      request_timeout_seconds INTEGER NOT NULL CHECK (request_timeout_seconds > 0),
      max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
      usage_json TEXT,
      terminal_result TEXT,
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
      FOREIGN KEY (workspace_id, case_id) REFERENCES cases(workspace_id, case_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
      run_id TEXT NOT NULL,
      sequence INTEGER NOT NULL CHECK (sequence >= 1),
      event_kind TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (run_id, sequence),
      FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
      proposal_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      case_id TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
      validation_report_json TEXT NOT NULL,
      checked_revision INTEGER NOT NULL CHECK (checked_revision >= 0),
      created_at TEXT NOT NULL,
      FOREIGN KEY (run_id) REFERENCES runs(run_id),
      FOREIGN KEY (workspace_id, case_id) REFERENCES cases(workspace_id, case_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS applications (
      workspace_id TEXT NOT NULL,
      application_id TEXT NOT NULL CHECK ({_ID.format(col="application_id")}),
      payment_id TEXT NOT NULL CHECK ({_ID.format(col="payment_id")}),
      proposal_id TEXT,
      seeded INTEGER NOT NULL CHECK (seeded IN (0, 1)),
      idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 96),
      payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
      actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 80),
      created_at TEXT NOT NULL,
      PRIMARY KEY (workspace_id, application_id),
      UNIQUE (workspace_id, payment_id),
      UNIQUE (workspace_id, idempotency_key),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
      FOREIGN KEY (workspace_id, payment_id)
        REFERENCES payments(workspace_id, payment_id),
      FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS allocations (
      workspace_id TEXT NOT NULL,
      application_id TEXT NOT NULL,
      invoice_id TEXT NOT NULL CHECK ({_ID.format(col="invoice_id")}),
      cash_cents INTEGER NOT NULL CHECK ({_MONEY_POS.format(col="cash_cents")}),
      fee_cents INTEGER NOT NULL CHECK ({_MONEY_NONNEG.format(col="fee_cents")}),
      PRIMARY KEY (workspace_id, application_id, invoice_id),
      FOREIGN KEY (workspace_id, application_id)
        REFERENCES applications(workspace_id, application_id),
      FOREIGN KEY (workspace_id, invoice_id)
        REFERENCES invoices(workspace_id, invoice_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corrections (
      correction_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      case_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      proposal_id TEXT,
      actor TEXT NOT NULL,
      text TEXT NOT NULL,
      cited_documents_json TEXT NOT NULL,
      verified_proposal_json TEXT,
      lesson_eligibility INTEGER NOT NULL CHECK (lesson_eligibility IN (0, 1)),
      eligibility_reason TEXT,
      application_id TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (workspace_id, case_id) REFERENCES cases(workspace_id, case_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS precedents (
      precedent_id TEXT PRIMARY KEY,
      owner_workspace_id TEXT NOT NULL,
      company_id TEXT NOT NULL,
      family_id TEXT NOT NULL,
      version INTEGER NOT NULL CHECK (version >= 1),
      status TEXT NOT NULL,
      hint_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
      correction_id TEXT,
      test_report_id TEXT,
      policy_hash TEXT,
      behavior_fingerprint TEXT,
      compiler_metadata_json TEXT,
      activation_actor TEXT,
      activation_at TEXT,
      retirement_actor TEXT,
      retirement_at TEXT,
      UNIQUE (owner_workspace_id, family_id, version),
      FOREIGN KEY (owner_workspace_id) REFERENCES workspaces(workspace_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_memory (
      workspace_id TEXT NOT NULL,
      precedent_id TEXT NOT NULL,
      PRIMARY KEY (workspace_id, precedent_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
      FOREIGN KEY (precedent_id) REFERENCES precedents(precedent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_tests (
      report_id TEXT PRIMARY KEY,
      candidate_hash TEXT NOT NULL,
      dev_dataset_hash TEXT NOT NULL,
      model_hash TEXT NOT NULL,
      config_hash TEXT NOT NULL,
      outcomes_json TEXT NOT NULL,
      score_summary_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('RUNNING', 'PASSED', 'FAILED')),
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluation_runs (
      experiment_id TEXT PRIMARY KEY,
      status TEXT NOT NULL CHECK (
        status IN ('RUNNING', 'COMPLETE', 'PARTIAL', 'FAILED')
      ),
      config_json TEXT NOT NULL,
      config_hash TEXT NOT NULL,
      snapshot_ids_json TEXT NOT NULL,
      dataset_hash TEXT NOT NULL,
      split_hash TEXT NOT NULL,
      counts_json TEXT NOT NULL,
      metrics_json TEXT NOT NULL,
      artifact_path TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
)


class PersistenceError(Exception):
    """Storage constraint or schema failure. Safe to show; contains no secrets."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Create a SQLite connection with foreign keys and a short busy timeout."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the v1 tables if they do not already exist. Idempotent."""
    with transaction(connection):
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _ensure_column(connection, "precedents", "compiler_metadata_json", "TEXT")


def open_database(path: str | Path) -> sqlite3.Connection:
    """Connect and ensure schema version 1 tables exist."""
    connection = connect(path)
    initialize_schema(connection)
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def insert_workspace(connection: sqlite3.Connection, record: WorkspaceRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO workspaces (
          workspace_id, name, company_id, period_label, period_start, period_end,
          dataset_hash, policy_json, policy_hash, ledger_revision, schema_version,
          created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.workspace_id,
            record.name,
            record.company_id,
            record.period_label,
            record.period_start,
            record.period_end,
            record.dataset_hash,
            canonical_json(record.policy.model_dump(mode="json")),
            record.policy_hash,
            record.ledger_revision,
            record.schema_version,
            record.created_at,
        ),
        action="insert workspace",
    )


def get_workspace(connection: sqlite3.Connection, workspace_id: str) -> WorkspaceRecord | None:
    row = connection.execute(
        "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    return None if row is None else _workspace_from_row(row)


def list_workspaces(connection: sqlite3.Connection) -> list[WorkspaceRecord]:
    rows = connection.execute(
        "SELECT * FROM workspaces ORDER BY created_at, workspace_id"
    ).fetchall()
    return [_workspace_from_row(row) for row in rows]


def insert_customer(connection: sqlite3.Connection, record: CustomerRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO customers (workspace_id, customer_id, legal_name, display_name)
        VALUES (?, ?, ?, ?)
        """,
        (record.workspace_id, record.customer_id, record.legal_name, record.display_name),
        action="insert customer",
    )


def get_customer(
    connection: sqlite3.Connection, workspace_id: str, customer_id: str
) -> CustomerRecord | None:
    row = connection.execute(
        "SELECT * FROM customers WHERE workspace_id = ? AND customer_id = ?",
        (workspace_id, customer_id),
    ).fetchone()
    if row is None:
        return None
    return CustomerRecord(
        workspace_id=row["workspace_id"],
        customer_id=row["customer_id"],
        legal_name=row["legal_name"],
        display_name=row["display_name"],
    )


def insert_invoice(connection: sqlite3.Connection, record: InvoiceRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO invoices (
          workspace_id, invoice_id, customer_id, currency, issued_date, due_date,
          original_cents, opening_outstanding_cents, outstanding_cents, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.workspace_id,
            record.invoice_id,
            record.customer_id,
            record.currency,
            record.issued_date,
            record.due_date,
            record.original_cents,
            record.opening_outstanding_cents,
            record.outstanding_cents,
            record.status.value,
        ),
        action="insert invoice",
    )


def get_invoice(
    connection: sqlite3.Connection, workspace_id: str, invoice_id: str
) -> InvoiceRecord | None:
    row = connection.execute(
        "SELECT * FROM invoices WHERE workspace_id = ? AND invoice_id = ?",
        (workspace_id, invoice_id),
    ).fetchone()
    return None if row is None else _invoice_from_row(row)


def list_invoices(connection: sqlite3.Connection, workspace_id: str) -> list[InvoiceRecord]:
    rows = connection.execute(
        "SELECT * FROM invoices WHERE workspace_id = ? ORDER BY invoice_id",
        (workspace_id,),
    ).fetchall()
    return [_invoice_from_row(row) for row in rows]


def insert_payment(connection: sqlite3.Connection, record: PaymentRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO payments (
          workspace_id, payment_id, bank_transaction_id, bank_account_id, posted_date,
          currency, amount_cents, channel, payer_text, bank_reference, applied
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.workspace_id,
            record.payment_id,
            record.bank_transaction_id,
            record.bank_account_id,
            record.posted_date,
            record.currency,
            record.amount_cents,
            record.channel.value,
            record.payer_text,
            record.bank_reference,
            int(record.applied),
        ),
        action="insert payment",
    )


def get_payment(
    connection: sqlite3.Connection, workspace_id: str, payment_id: str
) -> PaymentRecord | None:
    row = connection.execute(
        "SELECT * FROM payments WHERE workspace_id = ? AND payment_id = ?",
        (workspace_id, payment_id),
    ).fetchone()
    if row is None:
        return None
    return _payment_from_row(row)


def insert_document(connection: sqlite3.Connection, record: Document) -> None:
    _execute(
        connection,
        """
        INSERT INTO documents (
          workspace_id, document_id, kind, title, issued_date, body_text, facts_json,
          sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.workspace_id,
            record.document_id,
            record.kind.value,
            record.title,
            record.issued_date,
            record.body_text,
            canonical_json(record.facts.model_dump(mode="json")),
            record.sha256,
        ),
        action="insert document",
    )


def get_document(
    connection: sqlite3.Connection, workspace_id: str, document_id: str
) -> Document | None:
    row = connection.execute(
        "SELECT * FROM documents WHERE workspace_id = ? AND document_id = ?",
        (workspace_id, document_id),
    ).fetchone()
    return None if row is None else _document_from_row(row)


def list_documents(connection: sqlite3.Connection, workspace_id: str) -> list[Document]:
    rows = connection.execute(
        "SELECT * FROM documents WHERE workspace_id = ? ORDER BY document_id",
        (workspace_id,),
    ).fetchall()
    return [_document_from_row(row) for row in rows]


def insert_case(connection: sqlite3.Connection, record: CaseRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO cases (
          workspace_id, case_id, payment_id, state, current_run_id, latest_run_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            record.workspace_id,
            record.case_id,
            record.payment_id,
            record.state.value,
            record.current_run_id,
            record.latest_run_id,
        ),
        action="insert case",
    )


def get_case(connection: sqlite3.Connection, workspace_id: str, case_id: str) -> CaseRecord | None:
    row = connection.execute(
        "SELECT * FROM cases WHERE workspace_id = ? AND case_id = ?",
        (workspace_id, case_id),
    ).fetchone()
    return None if row is None else _case_from_row(row)


def list_cases_with_payments(
    connection: sqlite3.Connection, workspace_id: str
) -> list[tuple[CaseRecord, PaymentRecord]]:
    rows = connection.execute(
        """
        SELECT
          c.workspace_id AS case_workspace_id,
          c.case_id,
          c.payment_id AS case_payment_id,
          c.state,
          c.current_run_id,
          c.latest_run_id,
          p.workspace_id AS payment_workspace_id,
          p.payment_id,
          p.bank_transaction_id,
          p.bank_account_id,
          p.posted_date,
          p.currency,
          p.amount_cents,
          p.channel,
          p.payer_text,
          p.bank_reference,
          p.applied
        FROM cases AS c
        INNER JOIN payments AS p
          ON p.workspace_id = c.workspace_id AND p.payment_id = c.payment_id
        WHERE c.workspace_id = ?
        ORDER BY p.posted_date, c.case_id
        """,
        (workspace_id,),
    ).fetchall()
    listed: list[tuple[CaseRecord, PaymentRecord]] = []
    for row in rows:
        listed.append(
            (
                CaseRecord(
                    workspace_id=row["case_workspace_id"],
                    case_id=row["case_id"],
                    payment_id=row["case_payment_id"],
                    state=CaseState(row["state"]),
                    current_run_id=row["current_run_id"],
                    latest_run_id=row["latest_run_id"],
                ),
                PaymentRecord(
                    workspace_id=row["payment_workspace_id"],
                    payment_id=row["payment_id"],
                    bank_transaction_id=row["bank_transaction_id"],
                    bank_account_id=row["bank_account_id"],
                    posted_date=row["posted_date"],
                    currency=row["currency"],
                    amount_cents=row["amount_cents"],
                    channel=PaymentChannel(row["channel"]),
                    payer_text=row["payer_text"],
                    bank_reference=row["bank_reference"],
                    applied=bool(row["applied"]),
                ),
            )
        )
    return listed


def insert_application(connection: sqlite3.Connection, record: ApplicationRecord) -> None:
    def _write() -> None:
        connection.execute(
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
        for allocation in record.allocations:
            connection.execute(
                """
                INSERT INTO allocations (
                  workspace_id, application_id, invoice_id, cash_cents, fee_cents
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.workspace_id,
                    record.application_id,
                    allocation.invoice_id,
                    allocation.cash_cents,
                    allocation.fee_cents,
                ),
            )

    try:
        if connection.in_transaction:
            _write()
        else:
            with transaction(connection, immediate=True):
                _write()
    except sqlite3.IntegrityError as exc:
        raise PersistenceError(f"insert application violated a storage constraint: {exc}") from exc


def get_application(
    connection: sqlite3.Connection, workspace_id: str, application_id: str
) -> ApplicationRecord | None:
    row = connection.execute(
        """
        SELECT * FROM applications
        WHERE workspace_id = ? AND application_id = ?
        """,
        (workspace_id, application_id),
    ).fetchone()
    return None if row is None else _application_from_row(connection, row)


def get_application_by_payment(
    connection: sqlite3.Connection, workspace_id: str, payment_id: str
) -> ApplicationRecord | None:
    row = connection.execute(
        """
        SELECT * FROM applications
        WHERE workspace_id = ? AND payment_id = ?
        """,
        (workspace_id, payment_id),
    ).fetchone()
    return None if row is None else _application_from_row(connection, row)


def get_application_by_idempotency_key(
    connection: sqlite3.Connection, workspace_id: str, idempotency_key: str
) -> ApplicationRecord | None:
    row = connection.execute(
        """
        SELECT * FROM applications
        WHERE workspace_id = ? AND idempotency_key = ?
        """,
        (workspace_id, idempotency_key),
    ).fetchone()
    return None if row is None else _application_from_row(connection, row)


def list_applications(connection: sqlite3.Connection, workspace_id: str) -> list[ApplicationRecord]:
    rows = connection.execute(
        """
        SELECT * FROM applications
        WHERE workspace_id = ?
        ORDER BY created_at, application_id
        """,
        (workspace_id,),
    ).fetchall()
    return [_application_from_row(connection, row) for row in rows]


def list_payments(connection: sqlite3.Connection, workspace_id: str) -> list[PaymentRecord]:
    rows = connection.execute(
        """
        SELECT * FROM payments
        WHERE workspace_id = ?
        ORDER BY payment_id
        """,
        (workspace_id,),
    ).fetchall()
    return [_payment_from_row(row) for row in rows]


def list_proposals_for_run(connection: sqlite3.Connection, run_id: str) -> list[ProposalRecord]:
    rows = connection.execute(
        """
        SELECT * FROM proposals
        WHERE run_id = ?
        ORDER BY created_at, proposal_id
        """,
        (run_id,),
    ).fetchall()
    return [_proposal_from_row(row) for row in rows]


def list_proposals_for_case(
    connection: sqlite3.Connection, workspace_id: str, case_id: str
) -> list[ProposalRecord]:
    rows = connection.execute(
        """
        SELECT * FROM proposals
        WHERE workspace_id = ? AND case_id = ?
        ORDER BY created_at, proposal_id
        """,
        (workspace_id, case_id),
    ).fetchall()
    return [_proposal_from_row(row) for row in rows]


def insert_run(connection: sqlite3.Connection, record: RunRecord) -> None:
    usage_json = (
        None if record.usage is None else canonical_json(record.usage.model_dump(mode="json"))
    )
    terminal = None if record.terminal_result is None else record.terminal_result.value
    _execute(
        connection,
        """
        INSERT INTO runs (
          run_id, workspace_id, case_id, mode, provider, model, prompt_hash, tool_hash,
          memory_snapshot_json, state, started_at, finished_at, max_model_calls,
          max_tool_calls, max_case_seconds, request_timeout_seconds, max_output_tokens,
          usage_json, terminal_result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.run_id,
            record.workspace_id,
            record.case_id,
            record.mode.value,
            record.provider,
            record.model,
            record.prompt_hash,
            record.tool_hash,
            canonical_json(record.memory_snapshot.model_dump(mode="json")),
            record.state.value,
            record.started_at,
            record.finished_at,
            record.budgets.max_model_calls,
            record.budgets.max_tool_calls,
            record.budgets.max_case_seconds,
            record.budgets.request_timeout_seconds,
            record.budgets.max_output_tokens,
            usage_json,
            terminal,
        ),
        action="insert run",
    )


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    usage = None
    if row["usage_json"] is not None:
        usage = Usage.model_validate(json.loads(row["usage_json"]))
    terminal = row["terminal_result"]
    return RunRecord(
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        case_id=row["case_id"],
        mode=ExecutionMode(row["mode"]),
        provider=row["provider"],
        model=row["model"],
        prompt_hash=row["prompt_hash"],
        tool_hash=row["tool_hash"],
        memory_snapshot=MemorySnapshot.model_validate(json.loads(row["memory_snapshot_json"])),
        state=RunState(row["state"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        budgets=BudgetLimits(
            max_model_calls=row["max_model_calls"],
            max_tool_calls=row["max_tool_calls"],
            max_case_seconds=row["max_case_seconds"],
            request_timeout_seconds=row["request_timeout_seconds"],
            max_output_tokens=row["max_output_tokens"],
        ),
        usage=usage,
        terminal_result=None if terminal is None else AgentOutcome(terminal),
    )


def count_runs(
    connection: sqlite3.Connection,
    workspace_id: str,
    case_id: str | None = None,
) -> int:
    if case_id is None:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE workspace_id = ? AND case_id = ?",
            (workspace_id, case_id),
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def latest_run_for_workspace(connection: sqlite3.Connection, workspace_id: str) -> RunRecord | None:
    row = connection.execute(
        """
        SELECT run_id FROM runs
        WHERE workspace_id = ?
        ORDER BY started_at DESC, run_id DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None
    return get_run(connection, row["run_id"])


def insert_proposal(connection: sqlite3.Connection, record: ProposalRecord) -> None:
    _execute(
        connection,
        """
        INSERT INTO proposals (
          proposal_id, run_id, workspace_id, case_id, payload_json, payload_hash,
          validation_report_json, checked_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.proposal_id,
            record.run_id,
            record.workspace_id,
            record.case_id,
            canonical_json(record.payload.model_dump(mode="json")),
            record.payload_hash,
            canonical_json(record.validation_report.model_dump(mode="json")),
            record.checked_revision,
            record.created_at,
        ),
        action="insert proposal",
    )


def get_proposal(connection: sqlite3.Connection, proposal_id: str) -> ProposalRecord | None:
    row = connection.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    return _proposal_from_row(row)


def insert_run_event(connection: sqlite3.Connection, event: RunEvent) -> None:
    _execute(
        connection,
        """
        INSERT INTO run_events (run_id, sequence, event_kind, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event.run_id,
            event.sequence,
            event.event_kind.value,
            canonical_json(event.payload),
            event.created_at,
        ),
        action="insert run event",
    )


def next_run_event_sequence(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM run_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["max_sequence"]) + 1


def list_run_events(connection: sqlite3.Connection, run_id: str) -> list[RunEvent]:
    rows = connection.execute(
        """
        SELECT run_id, sequence, event_kind, payload_json, created_at
        FROM run_events
        WHERE run_id = ?
        ORDER BY sequence
        """,
        (run_id,),
    ).fetchall()
    return [
        RunEvent(
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_kind=RunEventKind(row["event_kind"]),
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def insert_correction(connection: sqlite3.Connection, record: CorrectionRecord) -> None:
    verified = (
        None
        if record.verified_resolved_proposal is None
        else canonical_json(record.verified_resolved_proposal.model_dump(mode="json"))
    )
    _execute(
        connection,
        """
        INSERT INTO corrections (
          correction_id, workspace_id, case_id, run_id, proposal_id, actor, text,
          cited_documents_json, verified_proposal_json, lesson_eligibility,
          eligibility_reason, application_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.correction_id,
            record.workspace_id,
            record.case_id,
            record.run_id,
            record.proposal_id,
            record.actor,
            record.text,
            canonical_json(record.cited_document_ids),
            verified,
            int(record.lesson_eligibility),
            record.eligibility_reason,
            record.application_id,
            record.created_at,
        ),
        action="insert correction",
    )


def get_correction(connection: sqlite3.Connection, correction_id: str) -> CorrectionRecord | None:
    row = connection.execute(
        "SELECT * FROM corrections WHERE correction_id = ?",
        (correction_id,),
    ).fetchone()
    return None if row is None else _correction_from_row(row)


def list_corrections(
    connection: sqlite3.Connection, workspace_id: str, case_id: str
) -> list[CorrectionRecord]:
    rows = connection.execute(
        """
        SELECT * FROM corrections
        WHERE workspace_id = ? AND case_id = ?
        ORDER BY created_at, correction_id
        """,
        (workspace_id, case_id),
    ).fetchall()
    return [_correction_from_row(row) for row in rows]


def update_correction_application(
    connection: sqlite3.Connection, correction_id: str, application_id: str
) -> None:
    _execute(
        connection,
        "UPDATE corrections SET application_id = ? WHERE correction_id = ?",
        (application_id, correction_id),
        action="update correction application",
    )


def insert_precedent(connection: sqlite3.Connection, record: PrecedentRecord) -> None:
    metadata = record.compiler_metadata or {}
    metadata_json = None if not metadata else canonical_json(metadata)
    _execute(
        connection,
        """
        INSERT INTO precedents (
          precedent_id, owner_workspace_id, company_id, family_id, version, status,
          hint_json, payload_hash, correction_id, test_report_id, policy_hash,
          behavior_fingerprint, compiler_metadata_json, activation_actor,
          activation_at, retirement_actor, retirement_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.precedent_id,
            record.owner_workspace_id,
            record.company_id,
            record.family_id,
            record.version,
            record.status.value,
            canonical_json(record.hint.model_dump(mode="json")),
            record.payload_hash,
            record.correction_id,
            record.test_report_id,
            record.policy_hash,
            record.behavior_fingerprint,
            metadata_json,
            record.activation_actor,
            record.activation_at,
            record.retirement_actor,
            record.retirement_at,
        ),
        action="insert precedent",
    )


def get_precedent(connection: sqlite3.Connection, precedent_id: str) -> PrecedentRecord | None:
    row = connection.execute(
        "SELECT * FROM precedents WHERE precedent_id = ?",
        (precedent_id,),
    ).fetchone()
    return None if row is None else _precedent_from_row(row)


def next_precedent_version(
    connection: sqlite3.Connection, owner_workspace_id: str, family_id: str
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(version), 0) AS max_version
        FROM precedents
        WHERE owner_workspace_id = ? AND family_id = ?
        """,
        (owner_workspace_id, family_id),
    ).fetchone()
    return int(row["max_version"]) + 1


def list_precedents_for_correction(
    connection: sqlite3.Connection, correction_id: str
) -> list[PrecedentRecord]:
    rows = connection.execute(
        """
        SELECT * FROM precedents
        WHERE correction_id = ?
        ORDER BY version, precedent_id
        """,
        (correction_id,),
    ).fetchall()
    return [_precedent_from_row(row) for row in rows]


def list_precedents_for_workspace(
    connection: sqlite3.Connection, owner_workspace_id: str
) -> list[PrecedentRecord]:
    rows = connection.execute(
        """
        SELECT * FROM precedents
        WHERE owner_workspace_id = ?
        ORDER BY family_id, version, precedent_id
        """,
        (owner_workspace_id,),
    ).fetchall()
    return [_precedent_from_row(row) for row in rows]


def list_precedents_for_family(
    connection: sqlite3.Connection, owner_workspace_id: str, family_id: str
) -> list[PrecedentRecord]:
    rows = connection.execute(
        """
        SELECT * FROM precedents
        WHERE owner_workspace_id = ? AND family_id = ?
        ORDER BY version
        """,
        (owner_workspace_id, family_id),
    ).fetchall()
    return [_precedent_from_row(row) for row in rows]


def get_active_precedent_for_family(
    connection: sqlite3.Connection, owner_workspace_id: str, family_id: str
) -> PrecedentRecord | None:
    row = connection.execute(
        """
        SELECT * FROM precedents
        WHERE owner_workspace_id = ? AND family_id = ? AND status = ?
        ORDER BY version DESC
        """,
        (owner_workspace_id, family_id, LessonState.ACTIVE.value),
    ).fetchone()
    return None if row is None else _precedent_from_row(row)


def update_precedent_lifecycle(connection: sqlite3.Connection, record: PrecedentRecord) -> None:
    _execute(
        connection,
        """
        UPDATE precedents SET
          status = ?,
          test_report_id = ?,
          policy_hash = ?,
          behavior_fingerprint = ?,
          activation_actor = ?,
          activation_at = ?,
          retirement_actor = ?,
          retirement_at = ?
        WHERE precedent_id = ?
        """,
        (
            record.status.value,
            record.test_report_id,
            record.policy_hash,
            record.behavior_fingerprint,
            record.activation_actor,
            record.activation_at,
            record.retirement_actor,
            record.retirement_at,
            record.precedent_id,
        ),
        action="update precedent lifecycle",
    )


def insert_workspace_memory(
    connection: sqlite3.Connection, workspace_id: str, precedent_id: str
) -> None:
    _execute(
        connection,
        """
        INSERT INTO workspace_memory (workspace_id, precedent_id)
        VALUES (?, ?)
        """,
        (workspace_id, precedent_id),
        action="insert workspace memory",
    )


def list_workspace_memory(connection: sqlite3.Connection, workspace_id: str) -> list[str]:
    rows = connection.execute(
        """
        SELECT precedent_id FROM workspace_memory
        WHERE workspace_id = ?
        ORDER BY precedent_id
        """,
        (workspace_id,),
    ).fetchall()
    return [str(row["precedent_id"]) for row in rows]


def insert_candidate_test(
    connection: sqlite3.Connection,
    report: CandidateTestReport,
    *,
    outcomes: dict[str, Any],
    score_summary: dict[str, Any],
) -> None:
    model_hash = report.behavior_fingerprint
    _execute(
        connection,
        """
        INSERT INTO candidate_tests (
          report_id, candidate_hash, dev_dataset_hash, model_hash, config_hash,
          outcomes_json, score_summary_json, state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report.report_id,
            report.candidate_payload_hash,
            report.dev_suite_hash,
            model_hash,
            report.behavior_fingerprint,
            canonical_json(outcomes),
            canonical_json(score_summary),
            report.state.value,
            report.started_at,
        ),
        action="insert candidate test",
    )


def update_candidate_test(
    connection: sqlite3.Connection,
    report: CandidateTestReport,
    *,
    outcomes: dict[str, Any],
    score_summary: dict[str, Any],
) -> None:
    _execute(
        connection,
        """
        UPDATE candidate_tests SET
          candidate_hash = ?,
          dev_dataset_hash = ?,
          model_hash = ?,
          config_hash = ?,
          outcomes_json = ?,
          score_summary_json = ?,
          state = ?
        WHERE report_id = ?
        """,
        (
            report.candidate_payload_hash,
            report.dev_suite_hash,
            report.behavior_fingerprint,
            report.behavior_fingerprint,
            canonical_json(outcomes),
            canonical_json(score_summary),
            report.state.value,
            report.report_id,
        ),
        action="update candidate test",
    )


def get_candidate_test(
    connection: sqlite3.Connection, report_id: str
) -> tuple[CandidateTestReport, dict[str, Any], dict[str, Any]] | None:
    row = connection.execute(
        "SELECT * FROM candidate_tests WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        return None
    outcomes = json.loads(row["outcomes_json"])
    summary = json.loads(row["score_summary_json"])
    report_payload = outcomes.get("report") if isinstance(outcomes, dict) else None
    if isinstance(report_payload, dict):
        report = CandidateTestReport.model_validate(report_payload)
    else:
        report = CandidateTestReport(
            report_id=row["report_id"],
            candidate_version=1,
            candidate_payload_hash=row["candidate_hash"],
            behavior_fingerprint=row["config_hash"],
            policy_hash=row["config_hash"],
            dev_suite_hash=row["dev_dataset_hash"],
            mode=ExecutionMode.TEST,
            state=CandidateTestState(row["state"]),
            episode_run_ids=[],
            paired_scores=[],
            check_results={"validator_suite_passed": False, "lifecycle_suite_passed": False},
            started_at=row["created_at"],
            finished_at=None,
        )
    return (
        report,
        outcomes if isinstance(outcomes, dict) else {},
        (summary if isinstance(summary, dict) else {}),
    )


def insert_evaluation_run(
    connection: sqlite3.Connection,
    report: EvaluationReport,
    *,
    config: dict[str, Any],
    snapshot_ids: list[str],
    split_hash: str,
) -> None:
    counts = {
        "budget_skipped": report.budget_skipped_count,
        "completed": report.completed_count,
        "failed": report.failed_count,
        "not_run": report.not_run_count,
        "scheduled": report.scheduled_count,
        "timed_out": report.timed_out_count,
    }
    created_at = report.started_at or utc_now_iso()
    _execute(
        connection,
        """
        INSERT INTO evaluation_runs (
          experiment_id, status, config_json, config_hash, snapshot_ids_json,
          dataset_hash, split_hash, counts_json, metrics_json, artifact_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report.experiment_id,
            report.state.value,
            canonical_json(config),
            hash_canonical(config),
            canonical_json(snapshot_ids),
            report.frozen_manifest.dataset_hash,
            split_hash,
            canonical_json(counts),
            canonical_json(report.metrics.model_dump(mode="json")),
            report.artifact_paths.get("directory", ""),
            created_at,
        ),
        action="insert evaluation run",
    )


def update_evaluation_run(
    connection: sqlite3.Connection,
    report: EvaluationReport,
    *,
    config: dict[str, Any],
    snapshot_ids: list[str],
    split_hash: str,
) -> None:
    counts = {
        "budget_skipped": report.budget_skipped_count,
        "completed": report.completed_count,
        "failed": report.failed_count,
        "not_run": report.not_run_count,
        "scheduled": report.scheduled_count,
        "timed_out": report.timed_out_count,
    }
    _execute(
        connection,
        """
        UPDATE evaluation_runs SET
          status = ?,
          config_json = ?,
          config_hash = ?,
          snapshot_ids_json = ?,
          dataset_hash = ?,
          split_hash = ?,
          counts_json = ?,
          metrics_json = ?,
          artifact_path = ?
        WHERE experiment_id = ?
        """,
        (
            report.state.value,
            canonical_json(config),
            hash_canonical(config),
            canonical_json(snapshot_ids),
            report.frozen_manifest.dataset_hash,
            split_hash,
            canonical_json(counts),
            canonical_json(report.metrics.model_dump(mode="json")),
            report.artifact_paths.get("directory", ""),
            report.experiment_id,
        ),
        action="update evaluation run",
    )


def list_evaluation_run_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT experiment_id, status, artifact_path, created_at
        FROM evaluation_runs
        ORDER BY created_at DESC, experiment_id
        """
    ).fetchall()


def get_evaluation_run(
    connection: sqlite3.Connection, experiment_id: str
) -> tuple[EvaluationReport | None, dict[str, Any], dict[str, Any]] | None:
    row = connection.execute(
        "SELECT * FROM evaluation_runs WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    counts = json.loads(row["counts_json"])
    metrics = json.loads(row["metrics_json"])
    artifact_dir = Path(row["artifact_path"]) if row["artifact_path"] else None
    report = None
    if artifact_dir is not None:
        report_path = artifact_dir / "report.json"
        if report_path.is_file():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            raw = payload.get("report") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                report = EvaluationReport.model_validate(raw)
    counts_payload = counts if isinstance(counts, dict) else {}
    metrics_payload = metrics if isinstance(metrics, dict) else {}
    return report, counts_payload, metrics_payload


def _workspace_from_row(row: sqlite3.Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=row["workspace_id"],
        name=row["name"],
        company_id=row["company_id"],
        period_label=row["period_label"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        dataset_hash=row["dataset_hash"],
        policy=CompanyPolicy.model_validate(json.loads(row["policy_json"])),
        policy_hash=row["policy_hash"],
        ledger_revision=row["ledger_revision"],
        schema_version=row["schema_version"],
        created_at=row["created_at"],
    )


def _case_from_row(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        workspace_id=row["workspace_id"],
        case_id=row["case_id"],
        payment_id=row["payment_id"],
        state=CaseState(row["state"]),
        current_run_id=row["current_run_id"],
        latest_run_id=row["latest_run_id"],
    )


def _payment_from_row(row: sqlite3.Row) -> PaymentRecord:
    return PaymentRecord(
        workspace_id=row["workspace_id"],
        payment_id=row["payment_id"],
        bank_transaction_id=row["bank_transaction_id"],
        bank_account_id=row["bank_account_id"],
        posted_date=row["posted_date"],
        currency=row["currency"],
        amount_cents=row["amount_cents"],
        channel=PaymentChannel(row["channel"]),
        payer_text=row["payer_text"],
        bank_reference=row["bank_reference"],
        applied=bool(row["applied"]),
    )


def _proposal_from_row(row: sqlite3.Row) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        case_id=row["case_id"],
        payload=ResolutionProposal.model_validate(json.loads(row["payload_json"])),
        payload_hash=row["payload_hash"],
        validation_report=ValidationReport.model_validate(
            json.loads(row["validation_report_json"])
        ),
        checked_revision=row["checked_revision"],
        created_at=row["created_at"],
    )


def _correction_from_row(row: sqlite3.Row) -> CorrectionRecord:
    verified_raw = row["verified_proposal_json"]
    verified = None
    if verified_raw is not None:
        verified = ResolutionProposal.model_validate(json.loads(verified_raw))
    return CorrectionRecord(
        correction_id=row["correction_id"],
        workspace_id=row["workspace_id"],
        case_id=row["case_id"],
        run_id=row["run_id"],
        proposal_id=row["proposal_id"],
        actor=row["actor"],
        text=row["text"],
        cited_document_ids=json.loads(row["cited_documents_json"]),
        verified_resolved_proposal=verified,
        lesson_eligibility=bool(row["lesson_eligibility"]),
        eligibility_reason=row["eligibility_reason"],
        application_id=row["application_id"],
        created_at=row["created_at"],
    )


def _precedent_from_row(row: sqlite3.Row) -> PrecedentRecord:
    metadata_raw = None
    try:
        metadata_raw = row["compiler_metadata_json"]
    except (IndexError, KeyError):
        metadata_raw = None
    metadata = {} if metadata_raw in (None, "") else json.loads(metadata_raw)
    return PrecedentRecord(
        precedent_id=row["precedent_id"],
        owner_workspace_id=row["owner_workspace_id"],
        company_id=row["company_id"],
        family_id=row["family_id"],
        version=row["version"],
        status=LessonState(row["status"]),
        hint=WireFeeLookupHint.model_validate(json.loads(row["hint_json"])),
        payload_hash=row["payload_hash"],
        correction_id=row["correction_id"],
        test_report_id=row["test_report_id"],
        policy_hash=row["policy_hash"],
        behavior_fingerprint=row["behavior_fingerprint"],
        compiler_metadata=metadata,
        activation_actor=row["activation_actor"],
        activation_at=row["activation_at"],
        retirement_actor=row["retirement_actor"],
        retirement_at=row["retirement_at"],
    )


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _application_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> ApplicationRecord:
    allocation_rows = connection.execute(
        """
        SELECT invoice_id, cash_cents, fee_cents FROM allocations
        WHERE workspace_id = ? AND application_id = ?
        ORDER BY invoice_id
        """,
        (row["workspace_id"], row["application_id"]),
    ).fetchall()
    return ApplicationRecord(
        application_id=row["application_id"],
        workspace_id=row["workspace_id"],
        payment_id=row["payment_id"],
        proposal_id=row["proposal_id"],
        seeded=bool(row["seeded"]),
        idempotency_key=row["idempotency_key"],
        payload_hash=row["payload_hash"],
        actor=row["actor"],
        allocations=[
            Allocation(
                invoice_id=item["invoice_id"],
                cash_cents=item["cash_cents"],
                fee_cents=item["fee_cents"],
            )
            for item in allocation_rows
        ],
        created_at=row["created_at"],
    )


def _invoice_from_row(row: sqlite3.Row) -> InvoiceRecord:
    return InvoiceRecord(
        workspace_id=row["workspace_id"],
        invoice_id=row["invoice_id"],
        customer_id=row["customer_id"],
        currency=row["currency"],
        issued_date=row["issued_date"],
        due_date=row["due_date"],
        original_cents=row["original_cents"],
        opening_outstanding_cents=row["opening_outstanding_cents"],
        outstanding_cents=row["outstanding_cents"],
        status=InvoiceSourceStatus(row["status"]),
    )


def _document_from_row(row: sqlite3.Row) -> Document:
    kind = DocumentKind(row["kind"])
    return Document(
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        kind=kind,
        title=row["title"],
        issued_date=row["issued_date"],
        body_text=row["body_text"],
        facts=parse_document_facts(kind, json.loads(row["facts_json"])),
        sha256=row["sha256"],
    )


def _execute(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...], *, action: str
) -> None:
    try:
        connection.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise PersistenceError(f"{action} violated a storage constraint: {exc}") from exc


__all__ = [
    "SCHEMA_VERSION",
    "PersistenceError",
    "connect",
    "get_active_precedent_for_family",
    "get_application",
    "get_application_by_idempotency_key",
    "get_application_by_payment",
    "get_candidate_test",
    "get_case",
    "get_correction",
    "get_customer",
    "get_document",
    "get_evaluation_run",
    "list_evaluation_run_rows",
    "get_invoice",
    "get_payment",
    "get_precedent",
    "get_proposal",
    "get_run",
    "get_workspace",
    "initialize_schema",
    "latest_run_for_workspace",
    "list_cases_with_payments",
    "insert_application",
    "insert_candidate_test",
    "insert_case",
    "insert_correction",
    "insert_customer",
    "insert_document",
    "insert_evaluation_run",
    "insert_invoice",
    "insert_payment",
    "insert_precedent",
    "insert_proposal",
    "insert_run",
    "insert_run_event",
    "insert_workspace",
    "insert_workspace_memory",
    "count_runs",
    "list_applications",
    "list_corrections",
    "list_documents",
    "list_invoices",
    "list_payments",
    "list_precedents_for_correction",
    "list_precedents_for_family",
    "list_precedents_for_workspace",
    "list_proposals_for_case",
    "list_proposals_for_run",
    "list_run_events",
    "list_workspace_memory",
    "list_workspaces",
    "next_precedent_version",
    "next_run_event_sequence",
    "open_database",
    "transaction",
    "update_candidate_test",
    "update_correction_application",
    "update_evaluation_run",
    "update_precedent_lifecycle",
]
