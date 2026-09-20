# Precedent

Precedent helps an accountant apply incoming customer payments to invoices,
learn a bounded investigation procedure from a correction, and test that
lesson before reuse.

This is a local HackMIT demonstration for the fictional US wholesaler
**Northstar Components**. Inputs are **normalized synthetic records**.
The app does not move real money or write to an external ERP. Header
caption: **Synthetic data · Sandbox ledger**.

## Setup

Python 3.11 or 3.12. This repository used CPython 3.11.14.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
```

Resolved versions: `requirements.lock.txt`. Recreate with
`python -m pip install -r requirements.lock.txt` then
`python -m pip install -e .`.

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
precedent preflight --offline
precedent fixtures build --seed 42
precedent demo init
```

`demo init` imports the January teaching packages into a new sandbox. It
does not delete stored reports or recorded-run files.

## Live API

Chargeable calls require all of:

1. a real `ANTHROPIC_API_KEY`
2. a `PRECEDENT_MODEL` the account can actually call
3. `PRECEDENT_ENABLE_LIVE=true`
4. an explicit live command or UI action

Copy `.env.example` to `.env` for local overrides. Environment variables
win. Defaults keep live mode off. Offline tests never spend just because
a key exists. `ScriptedProvider` is a test double only; CLI `--live`
never selects it.

`precedent preflight --live` makes one low-token tool-calling smoke
request when those four conditions hold. It prints status, not secrets.

## Demo

```bash
streamlit run app.py --server.address 127.0.0.1
```

Timed talk track: `docs/DEMO_SCRIPT.md`. Current demo workspace
`WS-TEACH-001`:

| Role | ID |
|---|---|
| Teaching case T03 | `CASE-1CFA9FEE848F` |
| Payment | `PAY-201` ($9,965.00) |
| Invoice | `INV-1042` ($10,000.00 → $0.00 after apply) |
| Remittance / fee notice | `DOC-R201` / `DOC-F201` |
| Application | `APP-44b27f710fba4a67b8c06faff43b28d1` |
| Correction | `COR-bd383d0aae4342c08c9c4170618b527f` |
| Recorded HUMAN apply | `RUN-00ef3035d78e45e698a4356306bf548f` |
| Same-gap dispute T06 | `CASE-D19F18830478` (still OPEN) |
| Candidate V02 | `CASE-84F1300FE6B4` on `WS-CAND-001` (still OPEN) |

Four pages across the top: **Work Queue**, **Case Detail**, **Lessons**,
and **Results**. Work Queue is the default page and lives at `/`; the
others are `/case-detail`, `/lessons`, and `/results`. Workspace choice,
attached memory, live readiness, and demo workspace admin are global
state and live in the sidebar on every page.

The queue states each payment as cash received against the invoice
balance the remittance advice names, with the unexplained difference as
its own column. When no remittance names an invoice held in the
workspace, that column is blank and says why; it is never filled with a
`$0.00`.

Work Queue **Run agent on selected case**, Case Detail **Propose reusable
lesson**, and the Lessons live test and Results evaluation stay disabled
until the live variables are configured. The sidebar **Live readiness**
panel names them. The Results recorded-run viewer is labeled **Recorded
run - no live model calls** and never reapplies money.

CLI equivalents for the same sandbox:

```bash
precedent cases list --workspace WS-TEACH-001
precedent case show --workspace WS-TEACH-001 --case CASE-1CFA9FEE848F
precedent replay export --run RUN-00ef3035d78e45e698a4356306bf548f
```

Amounts print as integer-cent USD text. `correction save` / `apply` /
`lesson propose --live` / `lesson test --live` / `evaluate --live` are
documented in `docs/PRECEDENT_BUILD_SPEC.md` section J1. Replace printed
IDs from your own commands; the table above is this repository’s demo
ledger.

## Reset

```bash
precedent demo init
```

Creates a new teaching workspace and makes it active. Does not delete
SQLite history, `artifacts/evaluations/`, or `artifacts/recorded-runs/`.
UI **New demo workspace** is the same teaching reset. February follow-up
with currently ACTIVE lessons (none in this checkout):

```bash
precedent demo new --dataset candidate --memory-from WS-TEACH-001
```

After reset, use the new workspace ID the command prints. Saved reports
remain loadable from the Results page.

## Held-out evaluation

Full comparison (20 pairs / 40 episodes, 90-minute deadline):

```bash
precedent evaluate --workspace WS-TEACH-001 --split heldout --live --deadline-seconds 5400
precedent report show --id EXPERIMENT_ID
```

Session 18 attempted that command on 2026-09-20T00:04:16Z. It exited 2
with `LIVE_DISABLED`. No `report.json` was written. The blocked record is
`artifacts/evaluations/EXP-S18-HELDOUT-BLOCKED/` and `docs/RESULTS.md`.
Do not treat a teaching `--limit` run, a HUMAN apply, or a scripted test
as that experiment. Do not tune product logic from held-out outcomes.

## Status

See `docs/BUILD_STATE.md`, `docs/RESULTS.md`, and `docs/sessions/18.md`.

Live investigation, live lesson draft, live candidate suite, and live
held-out comparison are **BLOCKED** until the live variables above are
set. There is no ACTIVE lesson. This prototype is not a complete
production cash-application product.
