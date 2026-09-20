# Three-minute demonstration script

Synthetic data · Sandbox ledger. Speak only what the screen shows. Do not
badge a recorded or scripted path as LIVE.

This script matches the current build: live investigation, lesson propose,
candidate test, and held-out evaluation are disabled until
`ANTHROPIC_API_KEY`, `PRECEDENT_MODEL`, and `PRECEDENT_ENABLE_LIVE=true`
are set. `ScriptedProvider` is not a presentation substitute.

Launch:

```bash
source .venv/bin/activate
streamlit run app.py --server.address 127.0.0.1
```

Open `http://127.0.0.1:8501`. Work Queue is the default page and lives at
`/`; the other three are `/case-detail`, `/lessons`, and `/results`.

**Sidebar (persistent, all four pages).** Title **Precedent**, subtitle
**Teach an investigation once. Check every application.**, then a `HUMAN`
provenance chip beside **Synthetic data · Sandbox ledger**. Below that:
the **Workspace** radio, **Memory attached 0 ACTIVE**, a collapsed **Live
readiness** expander, and a **Demo workspaces** popover.

Expand **Live readiness** only if asked. It lists
`PRECEDENT_ENABLE_LIVE`, `ANTHROPIC_API_KEY`, and `PRECEDENT_MODEL` with a
status mark each, then warns that chargeable live actions are disabled. It
names the variables, never their values. It is the only place in the app
that spells them out; buttons carry a short tooltip instead.

**Top navigation.** Work Queue · Case Detail · Lessons · Results.

**Context strip** (one line under the nav, every page): a provenance chip
reading **LIVE UNAVAILABLE**, then the last persisted run
`RUN-00ef3035d78e45e698a4356306bf548f`, HUMAN, state COMPLETE, model
UNSET.

Select workspace `WS-TEACH-001 - Northstar teaching 2026-01`.

## 0:00 to 0:25 - The question the product exists to answer

Work Queue. Five bordered metrics lead with **Unexplained $65.00**, then
Open 9 / Needs review 0 / Resolved 1 / Technical errors 0.

The table reads left to right: Case, Payer label, **Received**, **Invoice
balance**, **Unexplained**, **Matched to**, State, Latest result. Point at
`CASE-D19F18830478` (T06): **$9,965.00 received, $10,000.00 invoiced,
$35.00 unexplained, matched to `INV-52BE9C0A9941`.**

Say: an amount match is not enough. Thirty-five dollars did not arrive.
That is either a documented bank fee the bank took in transit, or money
this customer still owes. The row cannot tell you which.

Point at `CASE-70F364F7F2A6`: Invoice balance and Unexplained are
**blank**, and Matched to reads **No remittance on file**. Say: no
remittance names an invoice held here, so the difference cannot be
derived. The app leaves it blank rather than printing `$0.00`, because a
zero would read as "nothing to see here".

## 0:25 to 1:00 - The same gap, two different answers

Click the **✓ RESOLVED** state pill to bring the taught case back into
view. `CASE-1CFA9FEE848F` (T03) appears: **$9,965.00 received, $0.00
invoice balance, $0.00 unexplained**, Latest result **Applied.
Application APP-44b27f710fba4a67b8c06faff43b28d1**.

Say: T03 arrived as exactly the same arithmetic as T06. Both were
$9,965.00 against a $10,000.00 invoice with $35.00 unexplained. The queue
cannot separate them, because the arithmetic genuinely is identical.
(A clean `precedent demo init` workspace shows both rows at $35.00 side by
side. In this ledger T03 has already been taught and applied.)

What separates them is the evidence. Click the T03 row, then **Open
case**. The row click and the **Selected case** control stay in sync; the
control is the keyboard and screen reader path.

## 1:00 to 1:35 - T03: the evidence chain closes

Case Detail header: a green **RESOLVED** badge, a **HUMAN** provenance
chip, and the case identifier. Then **Harbor Treasury**, the payment line
(`PAY-201`, account `CASH-US-01`, reference `BR-201`, channel WIRE,
APPLIED), and the reconciliation strip:

**Received USD $9,965.00** · **Explained difference $35.00** · **Invoice
balance $10,000.00**, with the source underneath: posted as application
`APP-44b27f710fba4a67b8c06faff43b28d1`, $35.00 recorded as a documented
bank fee, evidenced by `DOC-F201` under settlement ticket ST-8721.

**Evidence tab.** *How the documents connect* is three numbered steps:

1. Payment `PAY-201` credited $9,965.00, bank reference BR-201.
2. Remittance `DOC-R201` names `INV-1042` and carries reference ST-8721.
3. Fee notice `DOC-F201` explains the difference: gross $10,000.00,
   receiving wire fee $35.00, net $9,965.00.

Say: the link between steps is a reference field on a stored record, not
a guess. Open `DOC-R201` with **Open document** to record a controller
inspection, or **Read full document** for the complete text plus
normalized facts. Both say **synthetic normalized input**; nothing here
extracts text from a scan.

**Decision tab.** The posted resolution is the hero of the tab:

**$9,965.00 received + $35.00 documented bank fee = $10,000.00 invoice
balance**, each term carrying its source (`PAY-201`, `DOC-F201`,
`INV-1042`). Below it the validator verdict, the posted application with
cash and fee stored separately, and **Candidate invoices**: exactly one
row, `INV-1042`, $10,000.00 original and $0.00 current, named by
*Remittance and decision*. Say: this payer's customer has fourteen open
invoices in the workspace. Only the one the evidence names is a
candidate.

**Teach tab** carries correction
`COR-bd383d0aae4342c08c9c4170618b527f`, the Harbor settlement-ticket
lookup. **Apply corrected resolution** is disabled because the payment is
already applied. **Propose reusable lesson** is disabled and its tooltip
names the missing live variables.

## 1:35 to 2:10 - T06: the same arithmetic, an open chain

Back to Work Queue, click `CASE-D19F18830478`, **Open case**.

Header: an **OPEN** badge, a **NO RUN YET** provenance chip, and the same
strip shape, now with **Unexplained $35.00** in amber. The note reads: no
stored fee notice references ST-276395BFD4E3, so the $35.00 has no
documentary support.

**Evidence tab.** The chain has three steps again, but the third is amber:

1. Payment `PAY-7B8F50C8D785` credited $9,965.00.
2. Remittance `DOC-7C3F261406F8` names `INV-52BE9C0A9941`, reference
   ST-276395BFD4E3.
3. **Nothing references ST-276395BFD4E3.** No stored bank fee notice
   carries that transfer reference, so a fee is not documented for this
   payment. A short payment alone does not imply one.

This is the beat. Identical money, identical gap, opposite evidence. The
system refuses to invent the fee, and **Use example correction** is
disabled because the Harbor fee example belongs to T03 only.

Optional five-second contrast: switch the workspace to `WS-CAND-001` and
open `CASE-84F1300FE6B4` (V02). Payment `PAY-9E7573431B8D` $1,565.00 is
still unapplied; invoice `INV-A6E37EFFAE11` remains $1,600.00.

## 2:10 to 2:35 - Lessons: what a lesson is, and what it can never do

Navigate to **Lessons**. Expected copy: no owned lessons in this
workspace; propose a draft from Case Detail after a verified correction;
a draft stays inactive until a compatible live test and explicit
activation.

*How this workspace learns* states the loop in four steps, and the
lifecycle stepper shows **DRAFT · TESTING · PASSED · ACTIVE** with
nothing reached. *What a lesson can never do* is the important panel: a
lesson cannot add a write-off, change the fee cap, raise an allocation
limit, replace a validator, widen its own scope, or turn a short payment
into a documented fee. Financial authority stays in fixed company policy
`northstar-usd-v1` and the validators.

Say: there is no scoped lesson, no five-case development table, and no
**Activate tested lesson** target in this checkout. The same validator
would score both arms of a paired test.

## 2:35 to 3:00 - Results: the frozen record, labeled

Navigate to **Results**.

**Saved evaluations** says no saved reports are present yet. **Run
teaching evaluation (2 cases / 4 episodes)** is disabled and its tooltip
names the live variables. *What a paired evaluation measures* explains
baseline versus memory over the denominators stored in `report.json`.

**Recorded runs** carries a persistent warning: **Recorded run - no live
model calls**. Select `RUN-00ef3035d78e45e698a4356306bf548f` (HUMAN,
captured 2026-09-19T21:54:44Z, exported 2026-09-19T23:51:15Z, model
UNSET). Point at `INV-1042 outstanding $10,000.00 to $0.00`. Say this is
a captured sandbox apply. The line underneath states that viewing it
never invokes `apply_proposal`.

Close on the actual Session 18 result, not an L3 template:

"On the frozen held-out set we did not obtain paired scores. Live
evaluate exited 2 with `LIVE_DISABLED` before any of the 40 episodes
ran. We did not fill zeros, and we did not substitute a scripted agent.
Records are synthetic normalized inputs."

Point at `docs/RESULTS.md` if a judge wants the command transcript.

## If live credentials are configured later

Do not use this script's BLOCKED lines. Confirm **Live readiness** shows
live mode on, key present, and a real model ID. Then:

1. Prefer a clean teaching workspace (`precedent demo init`) so T03 is
   OPEN. **Run agent on selected case** is the real investigation. The
   status container reports one thing while the call is in flight and one
   thing when it returns; no intermediate step is invented.
2. After the run, the budget strip shows model calls and tool calls
   against their limits, and the event timeline gives
   **Precedent retrieved** its own emphasis.
3. If it reviews, read its reason; if it resolves, show the search path.
4. **Use example correction** prefills only; it does not submit. **Save
   correction**, then **Apply corrected resolution** if still unpaid.
   Saving on an already-applied payment records an explanation and posts
   no further funds.
5. **Propose reusable lesson**, then Lessons: run the five-case live
   test, then **Activate tested lesson** only if that LIVE report passed.
   Activation blockers are listed inline on the lesson card.
6. **Candidate follow-up workspace** from the sidebar, then run a fresh
   in-scope case with **Memory enabled**.
7. Show T06 or V02 still unpaid if that is what happened.
8. After `precedent evaluate --workspace WS-TEACH-001 --split heldout
   --live --deadline-seconds 5400`, use Results **Load report** and read
   the stored denominators. The baseline-versus-memory chart draws only
   metrics whose two arms share a non-zero denominator, and names the
   rest as not charted. Choose the L3 sentence that matches those
   figures. Preserve the earlier BLOCKED artifact; a new run gets a new
   ID.

Capture a backup from that completed live run (screen recording of Work
Queue, T03, Lessons, T06, and the loaded `report.json`). A simulation
with a LIVE badge is out of scope.

## Controls this script must not click in the short path

- Sidebar **Demo workspaces**: **New demo workspace** and **Candidate
  follow-up workspace** both create extra ledgers
- **Run agent on selected case**, **Propose reusable lesson**, **Run
  teaching evaluation** while they are disabled
- Anything that would present TEST SIMULATION as LIVE
