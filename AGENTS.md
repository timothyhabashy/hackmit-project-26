# Precedent agent instructions

Work from `docs/PRECEDENT_BUILD_SPEC.md`, `AGENTS.md`, and `docs/BUILD_STATE.md`.
A session is a bounded checkpoint. It ends when its specified outcome is
verified. Do not execute all sessions in one chat. Do not start a new
architecture at each session. If a chat becomes unreliable, open a new chat
for the **same session number** and resume recorded remaining work.

## Authority

1. The human builder's explicit current instruction takes precedence.
2. The specification defines intended behavior and interfaces.
3. `docs/DECISIONS.md` records approved or necessary adjustments, with reasons
   and affected sections.
4. `docs/BUILD_STATE.md` records actual completion and evidence. It cannot
   silently redefine requirements.
5. Actual code and test output determine what exists, regardless of previous
   chat claims.

If a contract is wrong, fix the smallest affected area and update the
specification or decision record, callers, and tests together. Do not leave
two conflicting definitions. A routine implementation detail does not require
asking the human. Ask before removing the learning demonstration, broadening
financial actions, adding paid infrastructure, or changing the runtime
provider.

## Handoff

Update `docs/BUILD_STATE.md` and write `docs/sessions/NN.md` with: goal;
completed changes; actual files changed; exact commands/checks and outcomes;
what was not checked; unresolved issues; deviations; next-session
prerequisites; and a copy-paste prompt for the next chat. Keep the session
log factual and normally under 150 lines.

## Project rules

- Read the named session and build state first. Work only on that session.
- Financial authority comes from fixed company policy and validators, never
  learned memory.
- Use integer cents; reject booleans and floats as money input.
- Keep runtime source data separate from evaluation labels.
- Do not expose shell, arbitrary SQL, web browsing, or filesystem access to
  the runtime agent.
- No success claim without evidence. Distinguish offline tests, recorded
  runs, and live model runs.
- Do not commit secrets or logs containing secrets. Do not delete unrelated
  user work.
- No new service, framework, provider, or broad refactor without a concrete
  need.
- End with an updated build state and a precise handoff.
