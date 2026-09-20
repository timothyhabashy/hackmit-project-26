# Precedent - Session 18 results

Synthetic data · Sandbox ledger. These findings are from the frozen
Session 17 product plus the Session 18 held-out attempt. They are not
production accounting results and do not move real money.

## Submission paragraph

Precedent investigates incoming customer payments, links supporting records,
and applies validated settlements to a sandbox receivables ledger. A
controller can teach it a recurring lookup procedure; Precedent turns that
correction into a narrowly scoped lesson, tests it on five positive and
negative development cases, and requires approval before reuse. We compare
the same agent with and without persisted lessons on twenty separate
synthetic cases and expose actual outcomes, rejected proposals, source
evidence, and model/tool usage. The frozen 20-pair held-out comparison did
not run: `ANTHROPIC_API_KEY`, `PRECEDENT_MODEL`, and
`PRECEDENT_ENABLE_LIVE=true` were not configured, `ScriptedProvider` was not
used as a substitute, and no accuracy or efficiency effect is claimed. The
prototype supports exact single payments, exact bundles, and documented
single-invoice bank fees; it uses normalized synthetic records and does not
move real money.

## Held-out comparison

**Status: BLOCKED.** Not COMPLETE. Not PARTIAL. No scored rows.

| Field | Value |
|---|---|
| Command | `precedent evaluate --workspace WS-TEACH-001 --split heldout --live --deadline-seconds 5400` |
| Attempted at | 2026-09-20T00:04:16Z |
| Exit | 2 |
| Reason | `LIVE_DISABLED` (`PRECEDENT_ENABLE_LIVE` is false) |
| Also missing | `ANTHROPIC_API_KEY`, `PRECEDENT_MODEL` |
| Scheduled | 20 pairs / 40 episodes |
| Completed / failed / not-run | 0 / 0 / 40 |
| `ScriptedProvider` | not used |
| Oracles loaded | no |
| `report.json` | not written |
| Local artifact | `artifacts/evaluations/EXP-S18-HELDOUT-BLOCKED/` (`status.json`, this summary) |

`precedent report show --id EXP-S18-HELDOUT-BLOCKED` returned
`INVALID_INPUT (report EXP-S18-HELDOUT-BLOCKED was not found)`. The runner
refuses live evaluation before it writes `report.json`. Missing metrics are
unavailable, not zero.

The L3 wording options are unused. None of them match a measured paired
run. Do not substitute “from X to Y”, “both resolved X of nine”, or “no
demonstrated gain from memory” as if the experiment finished.

Operator manifest seed 42 lists 20 held-out authoring IDs (H01-H20). Those
IDs were not opened for scoring in this session. Product logic was not
tuned from held-out outcomes.

## What is demonstrated without live API

Teaching workspace `WS-TEACH-001` already contains a controller-applied
Harbor fee settlement:

- Case `CASE-1CFA9FEE848F` (T03), payment `PAY-201` $9,965.00 USD, state
  `RESOLVED`, `applied=true`
- Invoice `INV-1042` outstanding $0.00 / original $10,000.00
- Application `APP-44b27f710fba4a67b8c06faff43b28d1` by `demo_controller`:
  cash $9,965.00 + fee $35.00
- Remittance `DOC-R201`, fee notice `DOC-F201` (ticket `ST-8721`)
- Correction `COR-bd383d0aae4342c08c9c4170618b527f`
- Recorded HUMAN run `RUN-00ef3035d78e45e698a4356306bf548f`, captured
  2026-09-19T23:51:15Z, labeled **Recorded run - no live model calls**

The same-gap disputed teaching case remains unpaid: T06
`CASE-D19F18830478`, payment `PAY-7B8F50C8D785` $9,965.00, invoice
`INV-52BE9C0A9941` still $10,000.00 outstanding, `applied=false`, no
application. Candidate V02 `CASE-84F1300FE6B4` on `WS-CAND-001`, payment
`PAY-9E7573431B8D` $1,565.00 versus invoice `INV-A6E37EFFAE11` $1,600.00,
is still `OPEN` and unapplied. Applying T03 did not invent a fee on those
cases.

Offline tests cover the three supported resolution shapes (exact single,
exact bundle, documented single-invoice bank fee) and reject duplicate,
stale, wrong-reference, wrong-customer, currency, and over-limit fee
proposals. That is deterministic validator evidence, not a live agent
result.

## What is not demonstrated

- No live tool-calling investigation
- No live lesson compile from the T03 correction
- No ten-episode live candidate test and no ACTIVE lesson
- No live teaching `--limit 2` evaluation
- No live held-out 20-pair comparison
- No measured improvement, regression, token, or API-cost figure
- No human-time saving

`var/precedent.sqlite3` has 0 precedent rows, 0 `workspace_memory` rows,
and 0 evaluation runs.

## Configuration that would have been frozen

These hashes describe the current offline freeze. They are not an
experiment manifest, because the experiment did not start.

| Item | Value |
|---|---|
| Fixture seed / hash | 42 / `54a27241c64a57f0a37699fe68de828d4faf7b23009f3248fe437cf74615245a` |
| Teaching dataset hash | `dc7982d1d4e799268d82d566bfba65920133dcd06febe657eaf32d17449cfe21` |
| Policy | `northstar-usd-v1` / `9fdc438a24631deb410acd28633528d15f04e6591ab33fdf3e408c7cfb8269b1` |
| Investigator prompt hash | `add5c9692ad598423a5d93894827237b7d7000eef9afb045173ea78c2fe6bd03` |
| Tool schema hash | `5c8c6c048249d09cb8e6df985ca47796202f059482edb45deb1fa6040782b452` |
| Behavior fingerprint | `5639a25ad48bc455711cc0e097bd7fd886d6f739ff093f8ac39aa048d970464d` |
| Application source version | `65a0f5dd8d4c687f0516291f2ecbd2c0bfde9666fca52c6463fe252967dcaac2` |
| Empty memory snapshot | `fc33b54607a2f3e65cd1595c97c424de3064738d353eadaa6d32c9842f725cf4` |
| Model | UNSET |
| Token prices | unknown |
| Budgets | 10 model calls, 30 tool calls, 120s/case, 30s request, 1500 output tokens |
| Memory intervention | none attached (0 ACTIVE lessons) |

## Limitations

- Live Anthropic calls require a real `ANTHROPIC_API_KEY`, a provider-verified
  `PRECEDENT_MODEL`, and `PRECEDENT_ENABLE_LIVE=true`, plus an explicit live
  command. Defaults keep chargeable calls off.
- A lesson that has not passed a live candidate suite must stay inactive.
- Review count is a proxy for workload, not measured minutes.
- Invoice face value is not money saved.
- One synthetic paired run would not support a statistical-significance or
  production-safety claim even if it had completed.
- Runtime sources are normalized synthetic JSON, not OCR of real bank files.
- Supported shapes only: exact single, exact bundle (≤3 invoices), documented
  receiving-bank fee on one invoice within policy. No partials, write-offs,
  multi-currency settlement, or real ERP posting.
- `case show` lists every invoice in the workspace snapshot, not only the
  case’s matched candidates.
- The project is under git and published at
  https://github.com/timothyhabashy/hackmit-project-26. Source version is
  still a behavior-file hash rather than a commit SHA.
- `artifacts/` is gitignored. Durable claims live in this file and
  `docs/BUILD_STATE.md`.

## L5 checklist (honest)

- [x] Install and launch commands work in the documented environment (offline).
- [ ] One real tool-calling run - BLOCKED. Sandbox application of T03 is captured as HUMAN, not LIVE.
- [x] Three supported resolution shapes have deterministic tests.
- [x] Duplicate/stale/wrong-reference/wrong-customer/currency/fee-limit cases cannot apply incorrectly (offline tests).
- [ ] Live controller-to-lesson draft - BLOCKED. Correction exists and was applied.
- [ ] Ten real candidate-test episodes - BLOCKED.
- [ ] Compatible approved lesson persists - BLOCKED (no ACTIVE memory).
- [ ] New-case live behavior with memory - BLOCKED. T06/V02 remain unapplied in the sandbox.
- [x] Final report states the twenty pairs did not run (BLOCKED, 40 not-run, no zero-fill).
- [x] No fabricated metrics. Errors and negative transfer are not hidden because they were not measured.
- [x] UI rerenders do not spend tokens or apply money twice (Session 17 evidence).
- [x] Recorded runs and TEST simulations are labeled apart from LIVE.
- [x] README states synthetic inputs, setup, demo, reset, and live requirements.
- [x] This file records actual findings and limitations.
- [x] `docs/DEMO_SCRIPT.md` matches current controls, including disabled live buttons.
- [x] `docs/BUILD_STATE.md` lists capability status and remaining blockers.
- [x] No secrets or invented held-out effect.

## After credentials exist

Do not retune product logic from held-out outcomes. Preserve this BLOCKED
record. A later live run is a new experiment with its own ID. Inspect all
ten candidate episodes before activating a lesson. Then:

```bash
precedent preflight --live
precedent evaluate --workspace WS-TEACH-001 --split heldout --live --deadline-seconds 5400
precedent report show --id EXPERIMENT_ID
```

Replace `EXPERIMENT_ID` with the ID printed by evaluate. If the run stops
early, keep the PARTIAL artifact; do not relabel a subset as the complete
experiment.
