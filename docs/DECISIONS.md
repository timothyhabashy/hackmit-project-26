# Decisions

Implementation adjustments that differ from a naive reading of the spec, or
choices the spec left to the builder. Routine file-level details that match
the spec are not listed.

## D001 - CPython 3.11.14 instead of the default `python3`

- Date: 2026-09-19
- Status: accepted
- Affects: C1
- Decision: create `.venv` with `/Users/timothy/.local/bin/python3.11` (3.11.14).
- Reason: the machine default `python3` is 3.13.7. The spec prefers 3.11 or 3.12.
- Consequence: README and BUILD_STATE document `python3.11 -m venv .venv`.

## D002 - Config loading without pydantic-settings

- Date: 2026-09-19
- Status: accepted
- Affects: C1, C4
- Decision: parse settings with Pydantic v2 models plus `python-dotenv`.
  Precedence is environment over `.env` over defaults. Paths resolve from the
  repository root (`pyproject.toml`).
- Reason: C1 names pydantic, python-dotenv, and the Anthropic SDK. It does
  not name pydantic-settings.
- Consequence: `precedent.config.load_settings` is the Session 01 settings seam.

## D003 - Exact dependency lock via pip freeze

- Date: 2026-09-19
- Status: accepted
- Affects: C5, J1
- Decision: keep `pyproject.toml` ranges/unpinned direct deps; record the
  resolved environment with `python -m pip freeze --exclude-editable >
  requirements.lock.txt`. Recreate with `pip install -r requirements.lock.txt`
  then `pip install -e .`.
- Reason: the spec forbids copying guessed “latest” versions into the spec
  and requires a documented reproducible lock method.
- Consequence: lock versions are those actually installed on 2026-09-19
  (notably `anthropic==1.7.0`, `pydantic==2.13.5`, `streamlit==1.64.0`).

## D004 - `store_proposal` persists a proposal before `apply_proposal`

- Date: 2026-09-19
- Status: accepted
- Affects: E5, F2, Session 06-07
- Decision: add `store_proposal(repo, context, proposal) -> ProposalRecord` in
  `precedent.ledger`. Schema-valid proposals are stored with their current
  validation report. Schema-invalid input is rejected and not stored.
- Reason: E5 `apply_proposal` takes `proposal_id`. F2 `validate_resolution`
  persists an immutable proposal before `submit_resolution`. Session 06 needs
  that persist seam so apply can load a payload hash and revalidate inside the
  write transaction.
- Consequence: Session 07 `validate_resolution` should call `store_proposal`
  rather than invent a second proposal table. `apply_proposal` still
  revalidates and does not trust the stored report.

## D005 - The L4 custom-CSS scope cut is deliberately reversed

- Date: 2026-09-19
- Status: accepted
- Affects: L4, I1, I4, Session 19
- Decision: build a real design layer. `.streamlit/config.toml` carries the
  full Streamlit 1.64 token set, `src/precedent/ui_theme.py` owns one injected
  stylesheet plus every shared primitive, and navigation splits the former
  single **Learning Results** page into **Lessons** and **Results**.
- Reason: spec section L4 lists custom CSS and charts first among the cuts to
  take *if running behind*, and `docs/BUILD_STATE.md` recorded them as
  deliberately unbuilt. Sessions 01-18 finished, so that condition no longer
  held. Demo quality is an explicit judging criterion for this track, and the
  product's central claim was invisible: the queue rendered the real
  documented fee (T03) and the deceptive dispute (T06) as two identical
  `$9,965.00` rows, with the unexplained difference nowhere on screen. The
  measured symptom was 84 `st.caption` calls against 1 `st.title` and 10
  `st.subheader`, which collapsed roughly two-thirds of all text into the
  smallest gray tier.
- Consequence: `ui.py` is a shell over `app_pages/` scripts; presentation lives
  in `ui_theme`, the service seam in `ui_services`, and shared arithmetic in
  `ui_reconcile`. The other L4 cuts (extra exporters, version-editing
  conveniences, automatic UI progress streaming, additional exception
  families, general upload support) stand. No validator, ledger, or agent
  behavior changed, so the behavior fingerprint that gates lesson activation
  is intact. Spec sections I1, I4, and L4 carry a pointer to this entry.

## D006 - The correction form is partly, not wholly, an `st.form`

- Date: 2026-09-19
- Status: accepted
- Affects: I3, Session 19
- Decision: keep the allocation fields (resolution type, invoice, cash, fee)
  reactive and outside `st.form`. Wrap only the two long free-text fields,
  correction text and proposal explanation, and move save validation to the
  submit handler in `_save_blocker`.
- Reason: the UI plan asked for both `st.form` *and* the live arithmetic
  preview. Those are mutually exclusive. A form exists to suppress the reruns
  that a preview needs, so wrapping the allocation fields would leave the
  preview stale until submit, which is worse than having no preview. This was
  a defect in the plan, not in the implementation.
- Consequence: typing a long explanation no longer reruns the page, which is
  where suppression actually helps. The preview still updates as the numbers
  change and still shows the same three-term arithmetic as the case header.
  Because a form hides typed values until submission, the save button cannot
  be value-disabled; blockers are reported after submit instead, so a user is
  never locked out of their own input.

## D007 - The queue keeps `case_select` and `open_case` beside row-click

- Date: 2026-09-19
- Status: accepted
- Affects: I2, Session 19
- Decision: `st.dataframe(on_select="rerun", selection_mode="single-row")` is
  the primary selection gesture. The `case_select` selectbox and the
  `open_case` button stay, and `_sync_selection` keeps them pointing at the
  same case in both directions.
- Reason: two independent blockers. `streamlit.testing.v1.AppTest` exposes
  `st.dataframe` as a plain element with no click support, so a
  row-selection-only queue would be undriveable by the entire offline suite,
  which is the only evidence this build has. Separately, a dataframe row click
  has no keyboard or screen-reader equivalent, so removing the selectbox would
  remove the only accessible path to choosing a case.
- Consequence: three controls can address one selection. `_sync_selection`
  resolves them with one rule: a row click wins when the table's selection
  changed since the last script run, otherwise the remembered case wins and is
  written back into the table so the highlight follows a keyboard choice. A
  remembered case is never dropped because a state filter hides it. Tests
  drive selection by writing `st.session_state["case_table"]`, which is the
  same input the browser sends.
