from __future__ import annotations

import os
from pathlib import Path

import pytest

from precedent.cli import main
from precedent.config import load_settings
from precedent.db import get_application, get_invoice, get_payment, get_workspace, open_database
from precedent.fixtures import (
    CUST_CEDAR,
    CUST_HARBOR,
    DEFAULT_SEED,
    FORBIDDEN_RUNTIME_TOKENS,
    T03_BANK_REF,
    T03_FEE_ID,
    T03_INVOICE_ID,
    T03_PAYMENT_ID,
    T03_REMIT_ID,
    T03_TICKET,
    FixtureError,
    build_fixtures,
    load_manifest,
    load_oracle,
    load_package,
    package_fingerprint,
    runtime_source_text,
    scenario_table,
)
from precedent.models import (
    AgentOutcome,
    BankFeeNoticeFacts,
    DatasetSplit,
    DisputeNoticeFacts,
    DisputeStatus,
    DocumentKind,
    RemittanceFacts,
)
from precedent.services import demo_init, demo_new

CONFIG_ENV = (
    "ANTHROPIC_API_KEY",
    "PRECEDENT_MODEL",
    "PRECEDENT_DB_PATH",
    "PRECEDENT_DATA_DIR",
    "PRECEDENT_MAX_MODEL_CALLS",
    "PRECEDENT_MAX_TOOL_CALLS",
    "PRECEDENT_MAX_CASE_SECONDS",
    "PRECEDENT_REQUEST_TIMEOUT_SECONDS",
    "PRECEDENT_MAX_OUTPUT_TOKENS",
    "PRECEDENT_INPUT_USD_PER_MILLION",
    "PRECEDENT_OUTPUT_USD_PER_MILLION",
    "PRECEDENT_ENABLE_LIVE",
)

LITERAL = {
    "T01": {
        "openings": [120_000],
        "payment": 120_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(120_000, 0)],
    },
    "T02": {
        "openings": [65_000, 35_000],
        "payment": 100_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(65_000, 0), (35_000, 0)],
    },
    "T03": {
        "openings": [1_000_000],
        "payment": 996_500,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(996_500, 3500)],
    },
    "T04": {
        "openings": [74_235],
        "payment": 74_235,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(74_235, 0)],
    },
    "T05": {
        "openings": [15_000, 27_000, 18_000],
        "payment": 60_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(15_000, 0), (27_000, 0), (18_000, 0)],
    },
    "T06": {"openings": [1_000_000], "payment": 996_500, "outcome": AgentOutcome.REVIEW},
    "T07": {"openings": [80_000], "payment": 78_000, "outcome": AgentOutcome.REVIEW},
    "T08": {"openings": [45_000, 45_000], "payment": 45_000, "outcome": AgentOutcome.REVIEW},
    "T09": {"openings": [120_000], "payment": 121_000, "outcome": AgentOutcome.REVIEW},
    "T10": {
        "openings": [0],
        "originals": [50_000],
        "payment": 50_000,
        "outcome": AgentOutcome.REVIEW,
    },
    "V01": {
        "openings": [240_000],
        "payment": 238_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(238_000, 2000)],
    },
    "V02": {"openings": [160_000], "payment": 156_500, "outcome": AgentOutcome.REVIEW},
    "V03": {
        "openings": [220_000],
        "payment": 218_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(218_000, 2000)],
    },
    "V04": {"openings": [80_000], "payment": 78_000, "outcome": AgentOutcome.REVIEW},
    "V05": {
        "openings": [0],
        "originals": [97_000],
        "payment": 97_000,
        "outcome": AgentOutcome.REVIEW,
    },
    "H01": {
        "openings": [37_337],
        "payment": 37_337,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(37_337, 0)],
    },
    "H02": {
        "openings": [13_500, 24_600],
        "payment": 38_100,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(13_500, 0), (24_600, 0)],
    },
    "H03": {
        "openings": [8_700, 1_300, 22_000],
        "payment": 32_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(8_700, 0), (1_300, 0), (22_000, 0)],
    },
    "H04": {
        "openings": [184_000],
        "payment": 182_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(182_000, 2000)],
    },
    "H05": {
        "openings": [315_999],
        "payment": 312_499,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(312_499, 3500)],
    },
    "H06": {
        "openings": [98_000],
        "payment": 97_999,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(97_999, 1)],
    },
    "H07": {
        "openings": [600_000],
        "payment": 595_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(595_000, 5000)],
    },
    "H08": {
        "openings": [400_000],
        "payment": 399_100,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(399_100, 900)],
    },
    "H09": {
        "openings": [220_000],
        "payment": 218_000,
        "outcome": AgentOutcome.RESOLVED,
        "allocs": [(218_000, 2000)],
    },
    "H10": {"openings": [1_000_000], "payment": 996_500, "outcome": AgentOutcome.REVIEW},
    "H11": {"openings": [75_000], "payment": 71_500, "outcome": AgentOutcome.REVIEW},
    "H12": {"openings": [280_000], "payment": 276_500, "outcome": AgentOutcome.REVIEW},
    "H13": {"openings": [190_000], "payment": 186_500, "outcome": AgentOutcome.REVIEW},
    "H14": {"openings": [145_000], "payment": 145_000, "outcome": AgentOutcome.REVIEW},
    "H15": {"openings": [390_000], "payment": 386_500, "outcome": AgentOutcome.REVIEW},
    "H16": {"openings": [510_000], "payment": 504_900, "outcome": AgentOutcome.REVIEW},
    "H17": {"openings": [100_000], "payment": 100_100, "outcome": AgentOutcome.REVIEW},
    "H18": {"openings": [45_000, 55_000], "payment": 96_500, "outcome": AgentOutcome.REVIEW},
    "H19": {
        "openings": [0],
        "originals": [88_400],
        "payment": 88_400,
        "outcome": AgentOutcome.REVIEW,
    },
    "H20": {
        "openings": [50_000],
        "originals": [100_000],
        "payment": 100_000,
        "outcome": AgentOutcome.REVIEW,
    },
}


@pytest.fixture
def fixture_data(tmp_path: Path):
    data_dir = tmp_path / "data"
    manifest = build_fixtures(data_dir, seed=DEFAULT_SEED)
    return tmp_path, data_dir, manifest


def _by_authoring(manifest):
    return {entry.authoring_id: entry for entry in manifest.cases}


def _package_for(data_dir: Path, entry):
    return load_package(data_dir / entry.package_relpath)


def _oracle_for(data_dir: Path, entry):
    return load_oracle(data_dir / "grading" / f"{entry.case_id}.json")


def _scenario_invoices(package, entry):
    by_id = {invoice.invoice_id: invoice for invoice in package.invoices}
    return [by_id[invoice_id] for invoice_id in entry.invoice_ids]


def _payment_remittance(package):
    payment = package.payments[0]
    hits = []
    for document in package.documents:
        if document.kind is DocumentKind.REMITTANCE:
            assert isinstance(document.facts, RemittanceFacts)
            if document.facts.bank_reference == payment.bank_reference:
                hits.append(document)
    return hits


def _fee_notices(package):
    return [
        document for document in package.documents if document.kind is DocumentKind.BANK_FEE_NOTICE
    ]


def test_scenario_table_has_thirty_five_authoring_ids() -> None:
    rows = scenario_table()
    assert len(rows) == 35
    assert [row.authoring_id for row in rows] == (
        [f"T{index:02d}" for index in range(1, 11)]
        + [f"V{index:02d}" for index in range(1, 6)]
        + [f"H{index:02d}" for index in range(1, 21)]
    )


def test_build_writes_expected_counts_and_disjoint_splits(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    assert len(manifest.cases) == 35
    teaching = [entry for entry in manifest.cases if entry.split is DatasetSplit.TEACHING]
    candidate = [entry for entry in manifest.cases if entry.split is DatasetSplit.CANDIDATE]
    heldout = [entry for entry in manifest.cases if entry.split is DatasetSplit.HELDOUT]
    assert len(teaching) == 10
    assert len(candidate) == 5
    assert len(heldout) == 20
    teaching_ids = {entry.case_id for entry in teaching}
    candidate_ids = {entry.case_id for entry in candidate}
    heldout_ids = {entry.case_id for entry in heldout}
    assert teaching_ids.isdisjoint(heldout_ids)
    assert candidate_ids.isdisjoint(heldout_ids)
    assert teaching_ids.isdisjoint(candidate_ids)
    assert (data_dir / "manifests" / "fixtures-seed-42.json").is_file()


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    first = build_fixtures(tmp_path / "a", seed=42)
    second = build_fixtures(tmp_path / "b", seed=42)
    assert first.model_dump() == second.model_dump()
    for entry in first.cases:
        left = load_package(tmp_path / "a" / entry.package_relpath)
        right = load_package(tmp_path / "b" / entry.package_relpath)
        assert package_fingerprint(left) == package_fingerprint(right)
        assert left.invoices[0].opening_outstanding_cents == (
            right.invoices[0].opening_outstanding_cents
        )


def test_packages_and_oracles_validate(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    for entry in manifest.cases:
        package = _package_for(data_dir, entry)
        oracle = _oracle_for(data_dir, entry)
        assert package.case_id == entry.case_id
        assert oracle.case_id == entry.case_id
        invoice_ids = {invoice.invoice_id: invoice.invoice_id for invoice in package.invoices}
        document_ids = {document.document_id for document in package.documents}
        for invoice_id in entry.invoice_ids:
            assert invoice_id in invoice_ids
        for document_id in entry.document_ids:
            assert document_id in document_ids
        for document in package.documents:
            if isinstance(document.facts, RemittanceFacts):
                for invoice_id in document.facts.invoice_ids:
                    assert invoice_id in invoice_ids
            if isinstance(document.facts, DisputeNoticeFacts):
                assert document.facts.invoice_id in invoice_ids


@pytest.mark.parametrize("authoring_id", sorted(LITERAL))
def test_literal_amounts_and_oracle_allocations(fixture_data, authoring_id: str) -> None:
    spec = LITERAL[authoring_id]
    _tmp_path, data_dir, manifest = fixture_data
    entry = _by_authoring(manifest)[authoring_id]
    package = _package_for(data_dir, entry)
    oracle = _oracle_for(data_dir, entry)
    invoices = _scenario_invoices(package, entry)
    payment = package.payments[0]
    assert payment.amount_cents == spec["payment"]
    assert [invoice.opening_outstanding_cents for invoice in invoices] == spec["openings"]
    if "originals" in spec:
        assert [invoice.original_cents for invoice in invoices] == spec["originals"]
    assert oracle.expected_outcome is spec["outcome"]
    if spec["outcome"] is AgentOutcome.RESOLVED:
        got = [
            (item.invoice_id, item.cash_cents, item.fee_cents)
            for item in oracle.expected_allocations
        ]
        expected = [
            (entry.invoice_ids[index], cash, fee)
            for index, (cash, fee) in enumerate(spec["allocs"])
        ]
        assert sorted(got) == sorted(expected)
        assert oracle.expected_new_application_count == 1
        assert oracle.expected_payment_applied is True
        assert oracle.allowed_review_codes == []
        for invoice_id in entry.invoice_ids:
            assert oracle.expected_ending_balances[invoice_id] == 0
    else:
        assert oracle.expected_allocations == []
        assert oracle.expected_new_application_count == 0
        assert oracle.allowed_review_codes


def test_positive_cases_satisfy_equations(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    for authoring_id, spec in LITERAL.items():
        if spec["outcome"] is not AgentOutcome.RESOLVED:
            continue
        entry = _by_authoring(manifest)[authoring_id]
        package = _package_for(data_dir, entry)
        remittances = _payment_remittance(package)
        assert len(remittances) == 1
        remittance = remittances[0].facts
        assert isinstance(remittance, RemittanceFacts)
        invoices = _scenario_invoices(package, entry)
        named = [invoice for invoice in invoices if invoice.invoice_id in remittance.invoice_ids]
        opening_sum = sum(invoice.opening_outstanding_cents for invoice in named)
        payment = package.payments[0]
        allocs = spec["allocs"]
        if len(allocs) == 1 and allocs[0][1] > 0:
            notices = [
                document
                for document in _fee_notices(package)
                if isinstance(document.facts, BankFeeNoticeFacts)
                and document.facts.transfer_reference == remittance.settlement_ticket
            ]
            assert len(notices) == 1
            notice = notices[0].facts
            assert isinstance(notice, BankFeeNoticeFacts)
            assert notice.gross_cents == named[0].opening_outstanding_cents
            assert notice.net_cents == payment.amount_cents
            assert notice.fee_cents == notice.gross_cents - notice.net_cents
            assert notice.fee_cents == allocs[0][1]
            assert remittance.gross_settlement_cents == opening_sum
            assert allocs[0][0] + allocs[0][1] == named[0].opening_outstanding_cents
            assert allocs[0][0] == payment.amount_cents
        else:
            assert payment.amount_cents == opening_sum
            assert remittance.gross_settlement_cents == opening_sum
            assert all(fee == 0 for _cash, fee in allocs)


def test_negative_cases_preserve_intended_contradictions(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    by_id = _by_authoring(manifest)

    t06 = _package_for(data_dir, by_id["T06"])
    disputes = [
        document for document in t06.documents if document.kind is DocumentKind.DISPUTE_NOTICE
    ]
    assert len(disputes) == 1
    assert isinstance(disputes[0].facts, DisputeNoticeFacts)
    assert disputes[0].facts.status is DisputeStatus.OPEN
    assert disputes[0].facts.disputed_cents == 3500
    remittance = _payment_remittance(t06)[0].facts
    assert isinstance(remittance, RemittanceFacts)
    matching_notices = [
        document
        for document in _fee_notices(t06)
        if document.facts.transfer_reference == remittance.settlement_ticket
    ]
    assert matching_notices == []
    assert len(_fee_notices(t06)) >= 1

    t07 = _package_for(data_dir, by_id["T07"])
    remittance = _payment_remittance(t07)[0].facts
    assert isinstance(remittance, RemittanceFacts)
    assert remittance.settlement_ticket
    assert [
        document
        for document in _fee_notices(t07)
        if document.facts.transfer_reference == remittance.settlement_ticket
    ] == []

    t08 = _package_for(data_dir, by_id["T08"])
    assert _payment_remittance(t08) == []
    assert len(_scenario_invoices(t08, by_id["T08"])) == 2

    t09 = _package_for(data_dir, by_id["T09"])
    named = _scenario_invoices(t09, by_id["T09"])[0]
    assert t09.payments[0].amount_cents > named.opening_outstanding_cents

    t10 = _package_for(data_dir, by_id["T10"])
    assert t10.initial_ledger.applications
    assert _scenario_invoices(t10, by_id["T10"])[0].opening_outstanding_cents == 0

    h12 = _package_for(data_dir, by_id["H12"])
    remittance = _payment_remittance(h12)[0].facts
    assert isinstance(remittance, RemittanceFacts)
    notices = _fee_notices(h12)
    assert notices
    assert all(item.facts.transfer_reference != remittance.settlement_ticket for item in notices)
    assert notices[0].facts.fee_cents == 3500

    h13 = _package_for(data_dir, by_id["H13"])
    remittance = _payment_remittance(h13)[0].facts
    invoice = _scenario_invoices(h13, by_id["H13"])[0]
    assert isinstance(remittance, RemittanceFacts)
    assert remittance.customer_id != invoice.customer_id
    assert invoice.invoice_id in remittance.invoice_ids

    h14 = _package_for(data_dir, by_id["H14"])
    invoice = _scenario_invoices(h14, by_id["H14"])[0]
    assert invoice.currency == "EUR"
    assert h14.payments[0].currency == "USD"

    h15 = _package_for(data_dir, by_id["H15"])
    remittance = _payment_remittance(h15)[0].facts
    notice = [
        document
        for document in _fee_notices(h15)
        if document.facts.transfer_reference == remittance.settlement_ticket
    ][0]
    assert notice.facts.receiving_account_id != h15.payments[0].bank_account_id

    h16 = _package_for(data_dir, by_id["H16"])
    remittance = _payment_remittance(h16)[0].facts
    notice = [
        document
        for document in _fee_notices(h16)
        if document.facts.transfer_reference == remittance.settlement_ticket
    ][0]
    assert notice.facts.fee_cents == 5100
    assert notice.facts.fee_cents > 5000

    h18 = _package_for(data_dir, by_id["H18"])
    remittance = _payment_remittance(h18)[0].facts
    assert isinstance(remittance, RemittanceFacts)
    assert len(remittance.invoice_ids) == 2
    assert _fee_notices(h18)

    h20 = _package_for(data_dir, by_id["H20"])
    invoice = _scenario_invoices(h20, by_id["H20"])[0]
    assert invoice.original_cents == 100_000
    assert invoice.opening_outstanding_cents == 50_000
    assert h20.payments[0].amount_cents == 100_000


def test_t03_uses_specified_source_records(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    entry = _by_authoring(manifest)["T03"]
    package = _package_for(data_dir, entry)
    assert entry.payment_id == T03_PAYMENT_ID
    assert T03_INVOICE_ID in entry.invoice_ids
    payment = package.payments[0]
    assert payment.payment_id == T03_PAYMENT_ID
    assert payment.bank_reference == T03_BANK_REF
    assert payment.amount_cents == 996_500
    remittance = next(
        document for document in package.documents if document.document_id == T03_REMIT_ID
    )
    notice = next(document for document in package.documents if document.document_id == T03_FEE_ID)
    assert remittance.body_text == (
        "Harbor Labs paid invoice INV-1042, gross USD 10,000.00. Bank reference BR-201. "
        "Settlement ticket ST-8721. Receiving account CASH-US-01."
    )
    assert notice.body_text == (
        "Transfer ST-8721 for Harbor Labs to CASH-US-01: gross USD 10,000.00, "
        "receiving wire fee USD 35.00, net credit USD 9,965.00."
    )
    assert remittance.facts.settlement_ticket == T03_TICKET
    assert notice.facts.transfer_reference == T03_TICKET
    assert notice.facts.fee_cents == 3500


def test_runtime_sources_omit_answer_keys_and_split_labels(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    for entry in manifest.cases:
        package_dir = data_dir / entry.package_relpath
        assert package_dir.name == entry.case_id
        lowered = package_dir.name.lower()
        for token in ("trap", "expected_review", "fee_case"):
            assert token not in lowered
        assert package_dir.name.startswith("CASE-")
        text = runtime_source_text(package_dir).lower()
        for token in FORBIDDEN_RUNTIME_TOKENS:
            assert token not in text
        payload = (package_dir / "documents.jsonl").read_text(encoding="utf-8")
        assert "sha256" not in payload
        assert "expected_allocations" not in payload


def test_held_out_distribution_and_cedar_scope(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    held = [entry for entry in manifest.cases if entry.split is DatasetSplit.HELDOUT]
    resolved = 0
    review = 0
    for entry in held:
        oracle = _oracle_for(data_dir, entry)
        if oracle.expected_outcome is AgentOutcome.RESOLVED:
            resolved += 1
        else:
            review += 1
    assert resolved == 9
    assert review == 11
    for authoring_id in ("V03", "H09"):
        entry = _by_authoring(manifest)[authoring_id]
        oracle = _oracle_for(data_dir, entry)
        package = _package_for(data_dir, entry)
        assert oracle.forbidden_precedent_scopes
        assert oracle.forbidden_precedent_scopes[0].customer_id == CUST_HARBOR
        invoice = _scenario_invoices(package, entry)[0]
        assert invoice.customer_id == CUST_CEDAR


def test_t04_payer_text_differs_from_legal_name(fixture_data) -> None:
    _tmp_path, data_dir, manifest = fixture_data
    package = _package_for(data_dir, _by_authoring(manifest)["T04"])
    assert package.payments[0].payer_text != "Harbor Labs LLC"
    remittance = _payment_remittance(package)[0].facts
    assert remittance.customer_id == CUST_HARBOR


def test_demo_init_imports_teaching_workspace_and_keeps_reports(fixture_data) -> None:
    tmp_path, data_dir, manifest = fixture_data
    settings = load_settings(
        environ={
            "PRECEDENT_DATA_DIR": str(data_dir),
            "PRECEDENT_DB_PATH": str(tmp_path / "var" / "precedent.sqlite3"),
        },
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )
    connection = open_database(settings.db_path)
    connection.execute(
        """
        INSERT INTO evaluation_runs (
          experiment_id, status, config_json, config_hash, snapshot_ids_json,
          dataset_hash, split_hash, counts_json, metrics_json, artifact_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "EXP-KEEP",
            "COMPLETE",
            "{}",
            "a" * 64,
            "[]",
            "b" * 64,
            "c" * 64,
            "{}",
            "{}",
            "artifacts/evaluations/keep.json",
            "2026-01-20T00:00:00Z",
        ),
    )
    connection.close()

    result = demo_init(settings, seed=DEFAULT_SEED)
    assert result.workspace_id == "WS-TEACH-001"
    assert len(result.case_ids) == 10
    connection = open_database(settings.db_path)
    kept = connection.execute(
        "SELECT experiment_id FROM evaluation_runs WHERE experiment_id = 'EXP-KEEP'"
    ).fetchone()
    assert kept is not None
    t03 = _by_authoring(manifest)["T03"]
    payment = get_payment(connection, result.workspace_id, t03.payment_id)
    invoice = get_invoice(connection, result.workspace_id, T03_INVOICE_ID)
    assert payment is not None
    assert payment.amount_cents == 996_500
    assert payment.applied is False
    assert invoice is not None
    assert invoice.outstanding_cents == 1_000_000
    t10 = _by_authoring(manifest)["T10"]
    seeded_payment = get_payment(connection, result.workspace_id, t10.payment_id)
    assert seeded_payment is not None
    assert seeded_payment.applied is True
    package = _package_for(data_dir, t10)
    application = get_application(
        connection, result.workspace_id, package.initial_ledger.applications[0].application_id
    )
    assert application is not None
    assert application.seeded is True
    assert application.actor == "seed_import"
    second = demo_init(settings, seed=DEFAULT_SEED)
    assert second.workspace_id == "WS-TEACH-002"
    assert get_workspace(connection, "WS-TEACH-001") is not None
    connection.close()


def test_demo_new_candidate_attaches_no_memory_by_default(fixture_data) -> None:
    tmp_path, data_dir, _manifest = fixture_data
    settings = load_settings(
        environ={
            "PRECEDENT_DATA_DIR": str(data_dir),
            "PRECEDENT_DB_PATH": str(tmp_path / "var" / "precedent.sqlite3"),
        },
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )
    teaching = demo_init(settings, seed=DEFAULT_SEED)
    candidate = demo_new(settings, dataset=DatasetSplit.CANDIDATE, seed=DEFAULT_SEED)
    assert candidate.workspace_id == "WS-CAND-001"
    assert len(candidate.case_ids) == 5
    assert candidate.attached_precedent_ids == ()
    attached = demo_new(
        settings,
        dataset=DatasetSplit.CANDIDATE,
        memory_from=teaching.workspace_id,
        seed=DEFAULT_SEED,
    )
    assert attached.attached_precedent_ids == ()
    with pytest.raises(FixtureError, match="does not exist"):
        demo_new(
            settings,
            dataset=DatasetSplit.CANDIDATE,
            memory_from="WS-MISSING",
            seed=DEFAULT_SEED,
        )


def test_negative_seed_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FixtureError):
        build_fixtures(tmp_path / "data", seed=-1)


def test_cli_fixtures_build_and_demo_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRECEDENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRECEDENT_DB_PATH", str(tmp_path / "var" / "precedent.sqlite3"))

    def _load():
        return load_settings(
            environ=os.environ,
            dotenv_path=tmp_path / "missing.env",
            repo_root=tmp_path,
        )

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    code = main(["fixtures", "build", "--seed", "42"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Packages: 35" in out
    assert "T03" in out
    assert "Result: PASS" in out
    code = main(["demo", "init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "WS-TEACH-001" in out
    assert "T03:" in out
    assert "PAY-201" in out
    loaded = load_manifest(tmp_path / "data" / "manifests" / "fixtures-seed-42.json")
    assert len(loaded.cases) == 35
