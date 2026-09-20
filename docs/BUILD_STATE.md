# Build state
Current session: 19
Status: COMPLETE
Last verified checkpoint: Session 19 presentation-layer overhaul (2026-09-20 02:25:00 UTC)
Next session: none (spec sessions 01-18 are finished; 19 is a presentation-layer pass)

Session 19 changed the Streamlit layer only. No validator, ledger, agent,
service, or evaluation source file was touched, so the behavior fingerprint
that gates lesson activation is unchanged and Session 18's blocked held-out
record still stands exactly as recorded below.

## Environment
Python version: CPython 3.11.14 (`/Users/timothy/.local/bin/python3.11`, venv `.venv`)
Dependency install/lock method: `python -m pip install -e '.[dev]'` then `python -m pip freeze --exclude-editable > requirements.lock.txt`
Runtime provider/model identifier: UNSET (`PRECEDENT_MODEL` absent; no live model call was made)
API preflight: FAIL (never put keys here)

Live command actually run in Session 18:
`precedent evaluate --workspace WS-TEACH-001 --split heldout --live --deadline-seconds 5400`
Result: FAIL exit 2, `LIVE_DISABLED`. `precedent preflight --live` also FAIL
`LIVE_DISABLED`. Missing `PRECEDENT_ENABLE_LIVE`, `ANTHROPIC_API_KEY`, and
`PRECEDENT_MODEL`. No `.env`. `ScriptedProvider` was not used. No
`report.json`. Held-out packages and oracles were not loaded.

## Session checklist
- [x] 01 Repository, dependency setup, and immediate API preflight
- [x] 02 Canonical domain models and persistence
- [x] 03 Concrete synthetic cases and isolated answer keys
- [x] 04 Evidence search and retrieval
- [x] 05 Deterministic policy and proposal validation
- [x] 06 Atomic sandbox application and fresh-workspace reset
- [x] 07 Typed agent tool registry and trace events
- [x] 08 One live provider adapter and explicit offline test double
- [x] 09 Bounded investigation agent
- [x] 10 Human correction to a proposed structured lesson
- [x] 11 Executable lesson checks and explicit activation
- [x] 12 Retrieve learned memory and prove persistence
- [x] 13 Reproducible evaluation runner and honest metrics
- [x] 14 Streamlit shell and work queue
- [x] 15 Case detail, evidence inspection, and human correction
- [x] 16 Learning results and before/after evidence
- [x] 17 End-to-end integration and failure handling
- [x] 18 Frozen held-out comparison, demo package, and final handoff
- [x] 19 Presentation-layer overhaul (not a spec session; see DECISIONS D005)

Session 18 is complete as an honest BLOCKED held-out attempt plus demo
package. It is not a live evaluation success. The product is not ready
as a complete live correction to tested lesson to future-run system.

## J5 capability status
| Capability | Status |
|---|---|
| Offline models, validators, ledger apply, tools, scripted agent | IMPLEMENTED_OFFLINE |
| CLI `cases list` / `case show` / `replay export` | IMPLEMENTED_OFFLINE |
| Recorded-run export and labeled UI replay | IMPLEMENTED_OFFLINE |
| T03 controller apply + correction on `WS-TEACH-001` | IMPLEMENTED_OFFLINE |
| Demo README, `docs/DEMO_SCRIPT.md`, `docs/RESULTS.md` | IMPLEMENTED_OFFLINE |
| Live investigation / lesson draft / candidate test / evaluation | BLOCKED |
| Frozen held-out 20-pair comparison | BLOCKED |

## Verified commands
| Command | Result | Date/time | Notes |
|---|---|---|---|
| `.venv/bin/python -m pytest -q` | PASS 252 tests in 8.06s | 2026-09-20 02:33 UTC | Recorded-run label hyphen fix in `cli.py` + `tests/test_services.py` |
| `.venv/bin/python -m ruff check .` | PASS | 2026-09-20 02:33 UTC | Same fix |
| `.venv/bin/python -m ruff format --check .` | PASS 109 files | 2026-09-20 02:33 UTC | Same fix |
| `.venv/bin/python -m pytest -q` | PASS 252 tests in 7.60s | 2026-09-20 02:20 UTC | Session 19 re-run, same count |
| `.venv/bin/python -m ruff check .` | PASS | 2026-09-20 02:20 UTC | Session 19 |
| `.venv/bin/python -m ruff format --check .` | PASS 109 files | 2026-09-20 02:20 UTC | Session 19 |
| Browser Work Queue / Case Detail / Lessons / Results at 1440px and 1024px | PASS. Nav is four pages; T03 and T06 both read $9,965.00 / $10,000.00 / $35.00 on a fresh `demo init`; T06 evidence chain ends in an amber gap step; T03 resolved reads `Explained difference $35.00`; unmatched case renders blank, not `$0.00`; recorded-run label reads `Recorded run - no live model calls` | 2026-09-20 02:25 UTC | Two throwaway servers, ports 8510/8511; user's 8501 left running |
| `.venv/bin/python -m pytest -q` | PASS 252 tests in 6.26s | 2026-09-20 00:05 UTC | Session 18 freeze suite |
| `.venv/bin/python -m ruff check .` | PASS | 2026-09-20 00:05 UTC | Session 18 |
| `.venv/bin/python -m ruff format --check .` | PASS 54 files | 2026-09-20 00:05 UTC | Session 18 |
| `.venv/bin/precedent preflight --offline` | PASS. Fixtures READY | 2026-09-20 00:04 UTC | CPython 3.11.14 |
| `.venv/bin/precedent preflight --live` | FAIL exit 2 `LIVE_DISABLED` | 2026-09-20 00:04 UTC | Names enable flag |
| `.venv/bin/precedent evaluate --workspace WS-TEACH-001 --split heldout --live --deadline-seconds 5400` | FAIL exit 2 `LIVE_DISABLED` | 2026-09-20T00:04:16Z | Scheduled all split cases / 2 episodes; no artifacts from runner |
| `.venv/bin/precedent report show --id EXP-S18-HELDOUT-BLOCKED` | FAIL exit 2 not found | 2026-09-20 00:04 UTC | No `report.json` by design |
| `.venv/bin/precedent cases list --workspace WS-TEACH-001` | PASS. 10 cases; T03 `RESOLVED` `PAY-201`; T06 `OPEN` `PAY-7B8F50C8D785` | 2026-09-20 00:04 UTC | Open 9 / Resolved 1 |
| `.venv/bin/precedent case show` T03 | PASS. `INV-1042` $0 / $10,000; `DOC-R201`; `DOC-F201`; `APP-44b27f710fba4a67b8c06faff43b28d1`; `COR-bd383d0aae4342c08c9c4170618b527f`; HUMAN `RUN-00ef3035d78e45e698a4356306bf548f` | 2026-09-20 00:04 UTC | Teaching apply |
| `.venv/bin/precedent case show` T06 | PASS. OPEN $9,965 unapplied; `INV-52BE9C0A9941` $10,000 | 2026-09-20 00:04 UTC | No invented fee |
| `.venv/bin/precedent case show` V02 on `WS-CAND-001` | PASS. OPEN $1,565 `PAY-9E7573431B8D`; `INV-A6E37EFFAE11` $1,600 | 2026-09-20 00:04 UTC | Candidate copy |
| Browser Work Queue / T03 Case Detail / Learning Results `http://127.0.0.1:8501` | PASS. LIVE unavailable; Open 9 / Resolved 1; T03 fee equation and disabled Propose; RECORDED RUN `INV-1042` 10,000.00 to 0.00; no saved evaluations; T06 OPEN in queue | 2026-09-20 00:08 UTC | Session 18 nav (three pages), superseded |

## Decisions and deviations
- See `docs/DECISIONS.md` (D001-D007).
- D005 reverses the L4 custom-CSS and chart cuts. D006 records that the
  correction form is partly, not wholly, an `st.form`. D007 records why the
  queue keeps `case_select` and `open_case` beside dataframe row selection.
- Session 18 did not change `_BEHAVIOR_FILES` or product logic. No G4a
  redraft. Held-out outcomes were not used for tuning (none existed).
- Session 19 did not change `_BEHAVIOR_FILES` or product logic either. It
  touched only `app.py`, `app_pages/`, `.streamlit/config.toml`, `static/`,
  `src/precedent/ui*.py`, `tests/test_app.py`, and documentation.
- A later one-character follow-up changed the recorded-run label in
  `src/precedent/cli.py` line 926 and its `tests/test_services.py` line 531
  assertion from an em-dash to a hyphen, so the CLI matches
  `ui_results.RECORDED_RUN_LABEL`. Text only; no CLI behavior, output
  structure, or exit code changed, and `cli.py` is not a `_BEHAVIOR_FILES`
  member, so the fingerprint is unchanged. See `docs/sessions/19.md`.
- Blocked held-out record is `status.json` + `report.md` only. A scored
  `report.json` with zero-filled metrics was not written.
- Remaining L4 cuts are unbuilt: extra exporters, version-editing
  conveniences, automatic UI progress streaming, additional exception
  families, general upload support.

## Named fixture IDs (seed 42)
Operator mapping is `data/manifests/fixtures-seed-42.json`. Authoring IDs are not runtime file names.

| Authoring | Split | Case ID | Payment |
|---|---|---|---|
| T01 | teaching | `CASE-45E5749872CE` | `PAY-877D88D817A6` |
| T02 | teaching | `CASE-D3F251969B6A` | `PAY-D97ECA44A2C0` |
| T03 | teaching | `CASE-1CFA9FEE848F` | `PAY-201` |
| T05 | teaching | `CASE-FCD5F8079F33` | `PAY-C23953E3A1C8` |
| T06 | teaching | `CASE-D19F18830478` | `PAY-7B8F50C8D785` |
| T08 | teaching | `CASE-70F364F7F2A6` | `PAY-6F42C3BB628C` |
| T10 | teaching | `CASE-77B15B1E6163` | `PAY-756919601CAF` |
| V01 | candidate | `CASE-9A654576715E` | `PAY-1A0A7CFEC31C` |
| V02 | candidate | `CASE-84F1300FE6B4` | `PAY-9E7573431B8D` |
| V03 | candidate | `CASE-1B003942CE4D` | `PAY-C026A7F29432` |
| V04 | candidate | `CASE-7B8939951586` | `PAY-FA2A57D41E63` |
| V05 | candidate | `CASE-F0931BF35CB3` | `PAY-4409E608BACD` |
| H01 | heldout | `CASE-43DFFE59E311` | `PAY-391D7D3F47FD` |

T03 also keeps the Section N source IDs: invoice `INV-1042`, remittance `DOC-R201`, fee notice `DOC-F201`, ticket `ST-8721`. Demo teaching workspace: `WS-TEACH-001`. Saved T03 teaching correction: `COR-bd383d0aae4342c08c9c4170618b527f` (applied; application `APP-44b27f710fba4a67b8c06faff43b28d1`; actor `demo_controller`; cash `996500` + fee `3500`). Recorded HUMAN apply: `RUN-00ef3035d78e45e698a4356306bf548f`. Demo candidate follow-up: `WS-CAND-001` (`--memory-from WS-TEACH-001`, 0 attached). Default candidate workspace: `WS-CAND-002` (0 attached). T01 is the exact-single teaching case; T08 is the ambiguous-match teaching case. V03 is the Cedar control whose oracle forbids Harbor scope. Teaching `--limit 2` uses T01 then T02.

## Current limitations and blockers
- Live Anthropic calls are unavailable: `ANTHROPIC_API_KEY` absent; no `.env`;
  `PRECEDENT_MODEL` unset; `PRECEDENT_ENABLE_LIVE` false. Work Queue Run,
  Case Detail Propose, the Lessons five-case live test, and the Results
  teaching evaluation stay disabled. The sidebar Live readiness panel names
  those three variables; individual buttons carry a short tooltip.
- Held-out comparison is BLOCKED at live readiness. Artifact
  `EXP-S18-HELDOUT-BLOCKED` is not a scored experiment.
- No precedent row / ACTIVE memory in `var/precedent.sqlite3`.
- Candidate DBs may still contain orphan `RUNNING` inspect rows
  (`RUN-INSPECT-V02-FOLLOW`). Queue `running` stays 0.
- `case show` lists every invoice in the workspace snapshot (pre-existing).
  The UI no longer does: `src/precedent/ui_reconcile.py` narrows to the
  invoices the remittance and any stored decision name. The CLI still shows
  the unnarrowed list, so CLI and UI differ on that one view.
- `src/precedent/services.py` line 18 still says "for Learning Results" in a
  docstring. It was outside Session 19's permitted edit surface.
- Git is initialized and the project is published at
  https://github.com/timothyhabashy/hackmit-project-26. Application source
  version is still a hash of investigator behavior files rather than a commit
  SHA; that hash is what gates lesson activation.
- Do not tune product logic from a future held-out run. Preserve this
  BLOCKED record; a later live run is a new experiment ID.

## Next exact action
If presenting: follow `docs/DEMO_SCRIPT.md` and `docs/RESULTS.md`. The demo
ledger has T03 already applied, so click the RESOLVED state pill to show it
beside T06; a fresh `precedent demo init` shows both at $35.00 unexplained.
If live credentials are added, run `precedent preflight --live`, then the
held-out evaluate command above, then `precedent report show --id` with the
printed experiment ID. Do not substitute `ScriptedProvider`. Do not relabel a
partial or teaching `--limit` run as the complete experiment.
