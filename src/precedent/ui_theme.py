"""Design tokens, the single injected CSS layer, and every shared primitive.

Every page module renders through the primitives here, so the app has one
visual language and one place to fix it. Three rules matter for anyone
extending this file.

**Four text tiers, and only four.** Copy belongs to exactly one of them:

===========================  ====================================================
``section`` / ``card_title`` a heading that introduces a block
``money_html`` / ``kpi``     the primary value the block exists to show
``body`` / ``bullets``       supporting text a reader is expected to read
``quiet`` / ``st.caption``   fine print: identifiers, hashes, disclaimers

Anything that is not genuinely fine print must not be rendered as fine print.
Dropping everything into the smallest gray tier is what made the first version
of this app unreadable.

**Fragments versus blocks.** Primitives that produce an inline piece of a
sentence return an HTML ``str`` (``money_html``, ``ident``, ``state_badge``,
``provenance_badge``, ``quiet``, ``chip``). Render one or more of them with
``render_inline``. Primitives that own vertical space render themselves and
return ``None`` (``section``, ``kpi``, ``gap_strip``, ``doc_card``,
``equation_block``, ``chain_block``, ``verdict_block``, ``lifecycle_stepper``,
``budget_strip``, ``count_strip``, ``readiness_panel``, ``trace_timeline``).

**Blocks go through st.markdown, not st.html.** ``st.html`` output is invisible
to ``streamlit.testing.v1.AppTest``, which the offline suite depends on. Only
the stylesheet uses ``st.html``. Everything carrying text uses
``st.markdown(..., unsafe_allow_html=True)`` so the text stays readable by
``at.markdown``. Text is escaped by ``escape_text``, which also neutralizes
``$`` so Streamlit's LaTeX pass cannot swallow a money amount. Assert on the
digits (``"9,965.00"``), not on a literal ``$``.

CSS is scoped to classes this module owns (``.pc-*``) and to Streamlit's stable
``.st-key-*`` and ``data-testid`` hooks. It never targets ``st-emotion-cache-*``.

Colors are checked against the background they actually land on. Every pairing
in this file meets WCAG AA: 4.5:1 for body text, 3:1 for large text and
non-text boundaries.
"""

from __future__ import annotations

import html as _html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import streamlit as st

from precedent import ui_services as svc
from precedent.models import CaseState, DocumentKind, ExecutionMode, LessonState, RunEventKind

INK = "#0F172A"
MUTED = "#475569"
# Slate 500 (#64748B) reaches only 4.34:1 on the #F1F5F9 recessed surface this
# tier lands on inside gap cells, document headers, and the context strip. This
# value clears 5.0:1 there and 5.5:1 on white while staying visibly lighter
# than MUTED, so the tier separation survives the fix.
FAINT = "#5B6A80"
BORDER = "#E2E8F0"
BORDER_STRONG = "#CBD5E1"
SURFACE = "#FFFFFF"
CANVAS = "#F8FAFC"
SUBTLE = "#F1F5F9"
NAVY = "#1E3A8A"
NAVY_SOFT = "#DBEAFE"
AMBER = "#A16207"
AMBER_SOFT = "#FEF3C7"
AMBER_TEXT = "#92400E"
GREEN = "#166534"
GREEN_SOFT = "#DCFCE7"
RED = "#991B1B"
RED_SOFT = "#FEE2E2"
INDIGO = "#3730A3"
INDIGO_SOFT = "#E0E7FF"
MONO_STACK = '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace'

# Case state: filled badges. NEEDS_REVIEW is the only solid fill in the set, so
# "the system wants a human" is the most salient thing on any screen. Amber is
# reserved for exactly that meaning and appears nowhere else.
_STATE_STYLE: dict[CaseState, tuple[str, str, str, str]] = {
    CaseState.OPEN: ("\u25cb", NAVY_SOFT, NAVY, "#BFDBFE"),
    CaseState.RUNNING: ("\u25d0", INDIGO_SOFT, INDIGO, "#C7D2FE"),
    CaseState.RESOLVED: ("\u2713", GREEN_SOFT, GREEN, "#BBF7D0"),
    CaseState.NEEDS_REVIEW: ("\u25b2", AMBER, "#FFFFFF", AMBER),
    CaseState.ERROR: ("\u2715", RED_SOFT, RED, "#FECACA"),
}

# Run provenance: outlined uppercase mono chips, never filled. A dashed outline
# means "this did not come from a live model call".
_PROVENANCE_STYLE: dict[str, tuple[str, bool]] = {
    "LIVE": (GREEN, False),
    "HUMAN": (NAVY, False),
    "RECORDED RUN": (INDIGO, True),
    "TEST SIMULATION": (MUTED, True),
    "LIVE UNAVAILABLE": (FAINT, True),
    "UNKNOWN": (FAINT, True),
}

_EVENT_STYLE: dict[RunEventKind, tuple[str, str]] = {
    RunEventKind.RUN_STARTED: ("\u25b7", MUTED),
    RunEventKind.MODEL_RESPONSE: ("\u25cc", MUTED),
    RunEventKind.TOOL_CALLED: ("\u00bb", MUTED),
    RunEventKind.TOOL_SUCCEEDED: ("\u2713", GREEN),
    RunEventKind.TOOL_FAILED: ("\u2715", RED),
    RunEventKind.PRECEDENT_RETRIEVED: ("\u2605", NAVY),
    RunEventKind.PROPOSAL_VALIDATED: ("\u25a3", GREEN),
    RunEventKind.PROPOSAL_REJECTED: ("\u25a8", RED),
    RunEventKind.APPLICATION_COMMITTED: ("\u25cf", GREEN),
    RunEventKind.REVIEW_REQUESTED: ("\u25b2", AMBER),
    RunEventKind.RUN_FAILED: ("\u2715", RED),
    RunEventKind.RUN_FINISHED: ("\u25a0", MUTED),
}

_EVENT_LABEL: dict[RunEventKind, str] = {
    RunEventKind.RUN_STARTED: "Run started",
    RunEventKind.MODEL_RESPONSE: "Model response",
    RunEventKind.TOOL_CALLED: "Tool called",
    RunEventKind.TOOL_SUCCEEDED: "Tool succeeded",
    RunEventKind.TOOL_FAILED: "Tool failed",
    RunEventKind.PRECEDENT_RETRIEVED: "Precedent retrieved",
    RunEventKind.PROPOSAL_VALIDATED: "Proposal validated",
    RunEventKind.PROPOSAL_REJECTED: "Proposal rejected",
    RunEventKind.APPLICATION_COMMITTED: "Application committed",
    RunEventKind.REVIEW_REQUESTED: "Review requested",
    RunEventKind.RUN_FAILED: "Run failed",
    RunEventKind.RUN_FINISHED: "Run finished",
}

_DOCUMENT_LABEL: dict[DocumentKind, str] = {
    DocumentKind.REMITTANCE: "Remittance advice",
    DocumentKind.BANK_FEE_NOTICE: "Bank fee notice",
    DocumentKind.DISPUTE_NOTICE: "Dispute notice",
    DocumentKind.OTHER: "Source document",
}

# The lesson lifecycle is a path with three exits. The path is what the stepper
# draws; the exits are drawn as a terminal note under it.
_LIFECYCLE_PATH: tuple[LessonState, ...] = (
    LessonState.DRAFT,
    LessonState.TESTING,
    LessonState.PASSED,
    LessonState.ACTIVE,
)

_STEP_NOTE: dict[LessonState, str] = {
    LessonState.DRAFT: "Compiled from one verified correction. Never retrieved.",
    LessonState.TESTING: "Ten isolated episodes: five cases, with and without this lesson.",
    LessonState.PASSED: "Passed the limited checks. Still not retrieved.",
    LessonState.ACTIVE: "Retrievable inside its scope. Reversible by retiring it.",
}

_TERMINAL_NOTE: dict[LessonState, str] = {
    LessonState.FAILED: "The candidate suite did not pass. Create a new version and retest.",
    LessonState.REJECTED: "Closed without activation. It was never retrievable.",
    LessonState.RETIRED: "Withdrawn from retrieval. Past traces still cite it.",
}

# How far along the path each status got. A terminal status keeps the furthest
# step it reached marked as done, so the stepper reads as history, not as a
# position the lesson is still sitting in.
_REACHED_INDEX: dict[LessonState, int] = {
    LessonState.DRAFT: 0,
    LessonState.TESTING: 1,
    LessonState.PASSED: 2,
    LessonState.ACTIVE: 3,
    LessonState.FAILED: 1,
    LessonState.REJECTED: 0,
    LessonState.RETIRED: 3,
}

_PAYLOAD_DETAIL_KEYS = ("tool", "status", "error_code", "terminal_outcome", "reason_code")


def escape_text(value: Any) -> str:
    """Escape for both HTML and Streamlit's markdown pass.

    ``$`` and ``*`` become entities so an amount cannot open a LaTeX span and a
    title cannot open emphasis. Underscores are left alone: CommonMark does not
    treat intraword ``_`` as emphasis, so ``PRECEDENT_ENABLE_LIVE`` survives
    intact and stays greppable in tests.

    Every module that renders HTML must use this one function. Three copies of
    it existed across the page modules and were already drifting.
    """
    escaped = _html.escape(str(value), quote=True)
    return escaped.replace("$", "&#36;").replace("*", "&#42;").replace("`", "&#96;")


def _stylesheet() -> str:
    return f"""
<style>
:root {{
  --pc-ink: {INK};
  --pc-muted: {MUTED};
  --pc-faint: {FAINT};
  --pc-border: {BORDER};
  --pc-border-strong: {BORDER_STRONG};
  --pc-surface: {SURFACE};
  --pc-subtle: {SUBTLE};
  --pc-navy: {NAVY};
  --pc-navy-soft: {NAVY_SOFT};
  --pc-amber: {AMBER};
  --pc-amber-soft: {AMBER_SOFT};
  --pc-amber-text: {AMBER_TEXT};
  --pc-green: {GREEN};
  --pc-red: {RED};
  --pc-mono: {MONO_STACK};
}}

:focus-visible {{
  outline: 2px solid var(--pc-navy) !important;
  outline-offset: 2px !important;
  border-radius: 2px;
}}

/* A disabled action here is load-bearing: it is how the app says a chargeable
   live call cannot be made. Streamlit's default disabled styling fades the
   label below readable contrast, so it is restored to the muted tier and
   given a border. Shape and cursor, not color alone, mark it unavailable. */
[data-testid="stButton"] button:disabled,
[data-testid="stButton"] button[disabled],
[data-testid="stFormSubmitButton"] button:disabled {{
  opacity: 1 !important;
  color: var(--pc-muted) !important;
  background-color: var(--pc-subtle) !important;
  border: 1px dashed var(--pc-border-strong) !important;
  cursor: not-allowed !important;
}}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
    scroll-behavior: auto !important;
  }}
}}

/* Reusable container hooks. Any page may open
   st.container(border=True, key="pc-panel-NAME") to get the standard card,
   or key="pc-quiet-NAME" for a recessed block.
   Never put an angle bracket in this stylesheet: a literal < anywhere in the
   style body makes the sanitizer drop the whole element silently. */
[class*="st-key-pc-panel"] {{
  background: var(--pc-surface);
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  padding: 0.875rem 1rem;
}}
[class*="st-key-pc-quiet"] {{
  background: var(--pc-subtle);
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  padding: 0.75rem 0.875rem;
}}

/* A row of metric cards in st.container(key="pc-kpis-NAME") wraps instead of
   compressing. st.columns holds its ratio at any width, which at 1024px
   squeezed five cards to 101px each and truncated both the labels and the
   headline amount. Wrapping to a second row keeps every card readable. */
[class*="st-key-pc-kpis"] [data-testid="stHorizontalBlock"] {{
  flex-wrap: wrap;
  row-gap: 0.5rem;
}}
[class*="st-key-pc-kpis"] [data-testid="stColumn"] {{
  flex: 1 1 10.5rem;
  min-width: 10.5rem;
}}

/* Money. Tabular figures so columns of amounts line up. */
.pc-money {{
  font-family: var(--pc-mono);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  letter-spacing: -0.01em;
  white-space: nowrap;
}}
.pc-money-negative {{ color: var(--pc-red); }}
.pc-money-accent {{ color: var(--pc-amber-text); font-weight: 600; }}
.pc-money-strong {{ font-weight: 600; color: var(--pc-ink); }}

/* Identifier chip. Truncation is visual only: the full value stays in the DOM
   so it can be copied, read by a screen reader, and asserted on in tests. */
.pc-ident {{
  display: inline-block;
  max-width: 14ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-family: var(--pc-mono);
  font-size: 0.8125rem;
  color: var(--pc-muted);
  background: var(--pc-subtle);
  border: 1px solid var(--pc-border);
  border-radius: 3px;
  padding: 0 0.3rem;
  cursor: help;
}}
.pc-ident-label {{
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--pc-faint);
  margin-right: 0.25rem;
}}

.pc-state {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  border-radius: 3px;
  padding: 0.1rem 0.45rem;
  white-space: nowrap;
}}
.pc-state-icon {{ font-size: 0.7rem; line-height: 1; }}

.pc-prov {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-family: var(--pc-mono);
  font-size: 0.6875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  background: transparent;
  border-radius: 3px;
  padding: 0.1rem 0.4rem;
  white-space: nowrap;
}}
.pc-prov-dot {{ font-size: 0.55rem; line-height: 1; }}

/* Text tiers. Tier 1 heading, tier 3 supporting text, tier 4 fine print.
   Tier 2 is the primary value and is carried by .pc-money and st.metric. */
.pc-section {{ margin: 1.1rem 0 0.55rem; }}
.pc-section-title {{
  font-size: 1.0625rem;
  font-weight: 600;
  color: var(--pc-ink);
  line-height: 1.3;
}}
.pc-section-sub {{
  font-size: 0.875rem;
  color: var(--pc-muted);
  margin-top: 0.15rem;
  max-width: 78ch;
}}
.pc-card-title {{
  font-size: 1.0625rem;
  font-weight: 600;
  color: var(--pc-ink);
  line-height: 1.3;
}}
.pc-card-title-sm {{ font-size: 0.9375rem; }}
.pc-body {{
  font-size: 0.9375rem;
  color: var(--pc-ink);
  line-height: 1.55;
  max-width: 78ch;
}}
.pc-list {{
  margin: 0.45rem 0 0 1.1rem;
  padding: 0;
  font-size: 0.9375rem;
  color: var(--pc-ink);
  line-height: 1.65;
  max-width: 82ch;
}}
.pc-list-fine {{ font-size: 0.8125rem; color: var(--pc-muted); line-height: 1.6; }}
.pc-inline {{ display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.4rem; }}
.pc-quiet {{ font-size: 0.8125rem; color: var(--pc-muted); }}

/* Recessed note. Used for activation blockers and anything else that states a
   constraint without being an error. */
.pc-note {{
  border: 1px solid var(--pc-border);
  border-left: 3px solid var(--pc-muted);
  border-radius: 4px;
  background: var(--pc-subtle);
  padding: 0.55rem 0.8rem;
  margin-top: 0.4rem;
}}
.pc-note-title {{
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: var(--pc-ink);
}}
.pc-note-foot {{ font-size: 0.75rem; color: var(--pc-muted); margin-top: 0.35rem; }}

/* Reconciliation strip. The unexplained difference is the product thesis, so
   it is the largest type on the row and the only amber element. */
.pc-gap {{
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  gap: 0;
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  overflow: hidden;
  background: var(--pc-surface);
}}
.pc-gap-cell {{
  flex: 1 1 8rem;
  padding: 0.6rem 0.9rem;
  border-right: 1px solid var(--pc-border);
}}
.pc-gap-cell:last-child {{ border-right: none; }}
.pc-gap-label {{
  font-size: 0.6875rem;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--pc-faint);
  margin-bottom: 0.1rem;
}}
.pc-gap-value {{ font-size: 1.0625rem; }}
.pc-gap-hero {{
  background: var(--pc-amber-soft);
  border-left: 3px solid var(--pc-amber);
  border-right: 1px solid var(--pc-amber);
  flex: 1.25 1 10rem;
}}
.pc-gap-hero .pc-gap-label {{ color: var(--pc-amber-text); font-weight: 600; }}
.pc-gap-hero .pc-gap-value {{ font-size: 1.375rem; color: var(--pc-amber-text); }}
.pc-gap-settled {{ background: var(--pc-subtle); }}
.pc-gap-settled .pc-gap-value {{ color: var(--pc-green); }}
.pc-gap-note {{
  flex: 1 1 100%;
  border-top: 1px solid var(--pc-border);
  border-right: none;
  padding: 0.4rem 0.9rem;
  font-size: 0.8125rem;
  color: var(--pc-muted);
  background: var(--pc-subtle);
}}

/* Bordered cell strip, shared by budget readings and evaluation denominators.
   Both are "a row of small labelled counts", so they are one component. */
.pc-strip {{
  display: flex;
  flex-wrap: wrap;
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  overflow: hidden;
  background: var(--pc-surface);
  margin-top: 0.4rem;
}}
.pc-strip-cell {{
  flex: 1 1 6rem;
  padding: 0.5rem 0.85rem;
  border-right: 1px solid var(--pc-border);
}}
.pc-strip-cell:last-child {{ border-right: none; }}
.pc-strip-label {{
  font-size: 0.6875rem;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--pc-faint);
}}
.pc-strip-value {{ font-size: 1.0625rem; color: var(--pc-ink); }}
.pc-strip-of {{ font-size: 0.8125rem; color: var(--pc-muted); }}
.pc-strip-meter {{
  height: 3px;
  background: var(--pc-border);
  border-radius: 2px;
  margin-top: 0.35rem;
}}
.pc-strip-fill {{ height: 3px; border-radius: 2px; background: var(--pc-navy); }}
.pc-strip-fill-high {{ background: var(--pc-amber); }}

/* Lesson lifecycle stepper. Position is marked by fill, glyph, and label
   color together, so it never depends on color alone. */
.pc-steps {{
  display: flex;
  flex-wrap: wrap;
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  overflow: hidden;
  background: var(--pc-surface);
}}
.pc-step {{
  flex: 1 1 9rem;
  padding: 0.55rem 0.8rem;
  border-right: 1px solid var(--pc-border);
}}
.pc-step:last-child {{ border-right: none; }}
.pc-step-name {{
  font-size: 0.6875rem;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  font-weight: 600;
  color: var(--pc-faint);
}}
.pc-step-note {{ font-size: 0.8125rem; color: var(--pc-muted); margin-top: 0.15rem; }}
.pc-step-todo {{ background: var(--pc-subtle); }}
.pc-step-done .pc-step-name {{ color: var(--pc-green); }}
.pc-step-now {{ background: var(--pc-navy-soft); }}
.pc-step-now .pc-step-name {{ color: var(--pc-navy); }}
.pc-step-exit {{
  flex: 1 1 100%;
  border-top: 1px solid var(--pc-border);
  padding: 0.4rem 0.8rem;
  background: var(--pc-subtle);
  font-size: 0.8125rem;
  color: var(--pc-ink);
}}

/* Money arithmetic as the hero of a block. */
.pc-eq {{
  border: 1px solid var(--pc-border);
  border-radius: 6px;
  background: var(--pc-surface);
  overflow: hidden;
  margin: 0.35rem 0 0.6rem;
}}
.pc-eq-head {{
  padding: 0.4rem 0.9rem;
  border-bottom: 1px solid var(--pc-border);
  background: var(--pc-subtle);
  font-size: 0.6875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--pc-faint);
}}
.pc-eq-row {{ display: flex; align-items: stretch; flex-wrap: wrap; }}
.pc-eq-term {{ flex: 1 1 8rem; padding: 0.7rem 0.9rem; }}
.pc-eq-op {{
  display: flex;
  align-items: center;
  padding: 0 0.4rem;
  font-family: var(--pc-mono);
  font-size: 1.25rem;
  color: var(--pc-faint);
}}
.pc-eq-value {{ font-size: 1.375rem; line-height: 1.25; }}
.pc-eq-label {{ font-size: 0.8125rem; color: var(--pc-muted); margin-top: 0.1rem; }}
.pc-eq-src {{
  font-family: var(--pc-mono);
  font-size: 0.6875rem;
  color: var(--pc-faint);
  margin-top: 0.15rem;
  overflow-wrap: anywhere;
}}
.pc-eq-foot {{
  padding: 0.45rem 0.9rem;
  border-top: 1px solid var(--pc-border);
  background: var(--pc-subtle);
  font-size: 0.8125rem;
  color: var(--pc-muted);
}}
.pc-eq-open {{ border-color: var(--pc-amber); }}
.pc-eq-open .pc-eq-head {{ background: var(--pc-amber-soft); color: var(--pc-amber-text); }}
.pc-eq-open .pc-eq-foot {{ background: var(--pc-amber-soft); color: var(--pc-amber-text); }}

/* Ordered evidence steps. A missing link is amber, because that is exactly
   the moment the system should ask a human instead of inferring. */
.pc-chain {{ display: grid; gap: 0.35rem; }}
.pc-chain-step {{
  display: grid;
  grid-template-columns: 1.3rem 1fr;
  gap: 0.6rem;
  align-items: start;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--pc-border);
  border-left: 3px solid var(--pc-navy);
  border-radius: 4px;
  background: var(--pc-surface);
}}
.pc-chain-num {{
  font-family: var(--pc-mono);
  font-size: 0.75rem;
  color: var(--pc-faint);
  text-align: right;
  line-height: 1.5;
}}
.pc-chain-title {{ font-size: 0.875rem; font-weight: 600; color: var(--pc-ink); }}
.pc-chain-body {{ font-size: 0.8125rem; color: var(--pc-muted); margin-top: 0.1rem; }}
.pc-chain-miss {{ border-left-color: var(--pc-amber); background: var(--pc-amber-soft); }}
.pc-chain-miss .pc-chain-num {{ color: var(--pc-amber-text); }}
.pc-chain-miss .pc-chain-title {{ color: var(--pc-amber-text); }}
.pc-chain-miss .pc-chain-body {{ color: var(--pc-amber-text); }}

/* Validator result, with issue codes legible rather than stacked as warnings. */
.pc-verdict {{
  border: 1px solid var(--pc-border);
  border-left: 3px solid var(--pc-green);
  border-radius: 4px;
  background: var(--pc-surface);
  padding: 0.55rem 0.85rem;
  margin: 0.35rem 0 0.6rem;
}}
.pc-verdict-bad {{ border-left-color: var(--pc-red); }}
.pc-verdict-title {{ font-size: 0.875rem; font-weight: 600; color: var(--pc-ink); }}
.pc-verdict-issue {{
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  font-size: 0.8125rem;
  color: var(--pc-muted);
  margin-top: 0.3rem;
}}
.pc-verdict-code {{
  font-family: var(--pc-mono);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--pc-red);
  border: 1px solid var(--pc-red);
  border-radius: 2px;
  padding: 0 0.3rem;
  white-space: nowrap;
}}

/* Evidence rendered to look like a document rather than a console dump. */
.pc-doc {{
  border: 1px solid var(--pc-border-strong);
  border-radius: 4px;
  background: var(--pc-surface);
  box-shadow: 0 1px 0 var(--pc-border);
  overflow: hidden;
}}
.pc-doc-head {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0.6rem 0.9rem;
  border-bottom: 1px solid var(--pc-border);
  background: var(--pc-subtle);
}}
.pc-doc-kind {{
  font-family: var(--pc-mono);
  font-size: 0.625rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--pc-muted);
  border: 1px solid var(--pc-border-strong);
  border-radius: 2px;
  padding: 0.05rem 0.35rem;
}}
.pc-doc-title {{ font-weight: 600; color: var(--pc-ink); }}
.pc-doc-meta {{ margin-left: auto; font-size: 0.75rem; color: var(--pc-faint); }}
.pc-doc-body {{
  margin: 0;
  padding: 0.9rem 1.1rem;
  font-family: var(--pc-mono);
  font-size: 0.8125rem;
  line-height: 1.55;
  color: var(--pc-ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}}
.pc-doc-foot {{
  padding: 0.4rem 0.9rem;
  border-top: 1px dashed var(--pc-border-strong);
  font-size: 0.75rem;
  color: var(--pc-muted);
}}

/* Run trace. precedent_retrieved is emphasized in navy, not amber: it is the
   system proving memory changed its behavior, not a request for a human. */
.pc-timeline {{ display: grid; gap: 0.15rem; }}
.pc-tl-row {{
  display: grid;
  grid-template-columns: 2.25rem 1.25rem minmax(9rem, auto) 1fr;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.25rem 0.5rem;
  border-left: 3px solid transparent;
  border-radius: 3px;
}}
.pc-tl-seq {{
  font-family: var(--pc-mono);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
  color: var(--pc-faint);
  text-align: right;
}}
.pc-tl-icon {{ text-align: center; line-height: 1.4; }}
.pc-tl-kind {{ font-size: 0.875rem; color: var(--pc-ink); }}
.pc-tl-detail {{
  font-family: var(--pc-mono);
  font-size: 0.75rem;
  color: var(--pc-muted);
  overflow-wrap: anywhere;
}}
.pc-tl-key {{
  background: var(--pc-subtle);
  border-left-color: var(--pc-navy);
}}
.pc-tl-key .pc-tl-kind {{ font-weight: 600; color: var(--pc-navy); }}
.pc-tl-attention {{
  background: var(--pc-amber-soft);
  border-left-color: var(--pc-amber);
}}
.pc-tl-attention .pc-tl-kind {{ font-weight: 600; color: var(--pc-amber-text); }}
.pc-tl-bad {{ border-left-color: var(--pc-red); }}

.pc-ready {{ display: grid; gap: 0.2rem; }}
.pc-ready-row {{
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  font-size: 0.8125rem;
}}
.pc-ready-mark {{ width: 0.9rem; flex: 0 0 0.9rem; font-size: 0.75rem; }}
.pc-ready-name {{
  font-family: var(--pc-mono);
  font-size: 0.75rem;
  color: var(--pc-ink);
}}
.pc-ready-value {{ margin-left: auto; font-size: 0.75rem; color: var(--pc-muted); }}
.pc-ready-ok .pc-ready-mark {{ color: var(--pc-green); }}
.pc-ready-bad .pc-ready-mark {{ color: var(--pc-amber); }}

/* Case-id tag used for transfer lists. Color is paired with a written label
   in the row beside it, never used on its own to carry meaning. */
.pc-tag {{
  display: inline-block;
  font-family: var(--pc-mono);
  font-size: 0.8125rem;
  border-radius: 3px;
  padding: 0 0.35rem;
  margin-right: 0.3rem;
}}
.pc-tag-label {{
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
}}
</style>
""".strip()


def inject_theme() -> None:
    """Inject the stylesheet once per script run.

    Call this after ``st.navigation``. Anything written to the main container
    before the navigated page runs is discarded, and wrapping the stylesheet in
    ``st.container(key=...)`` loses it the same way. A bare ``st.html`` with
    style-only content goes to Streamlit's event container, so it costs no
    vertical space.
    """
    st.html(_stylesheet())


# --------------------------------------------------------------------------
# Inline fragments
# --------------------------------------------------------------------------


def money(cents: int) -> str:
    """Format integer cents as plain accounting text, for example ``$9,965.00``.

    Plain text, so it is safe for dataframe cells, widget labels, and any
    assertion that needs a literal ``$``. Use ``money_html`` inside primitives.
    """
    amount = abs(int(cents))
    sign = "-" if int(cents) < 0 else ""
    return f"{sign}${amount // 100:,}.{amount % 100:02d}"


def money_html(cents: int, *, tone: str = "default", signed: bool = False) -> str:
    """Return a tabular-figure mono money fragment.

    ``tone`` is one of ``"default"``, ``"strong"``, or ``"accent"``. ``accent``
    is amber and is reserved for an unexplained difference. ``signed=True``
    prefixes a positive amount with ``+``.
    """
    value = int(cents)
    classes = ["pc-money"]
    if value < 0:
        classes.append("pc-money-negative")
    if tone == "strong":
        classes.append("pc-money-strong")
    elif tone == "accent":
        classes.append("pc-money-accent")
    body_text = money(value)
    if signed and value > 0:
        body_text = f"+{body_text}"
    return f'<span class="{" ".join(classes)}">{escape_text(body_text)}</span>'


def ident(value: str, *, label: str | None = None, width_ch: int = 14) -> str:
    """Return a truncating monospace chip for a long identifier.

    Truncation is CSS only, so the full value is still selectable and carried
    in the ``title`` tooltip. ``label`` prefixes a small uppercase caption.
    """
    raw = "" if value is None else str(value)
    prefix = f'<span class="pc-ident-label">{escape_text(label)}</span>' if label else ""
    style = f' style="max-width:{int(width_ch)}ch"' if width_ch != 14 else ""
    return (
        f'{prefix}<span class="pc-ident" title="{escape_text(raw)}"{style}>'
        f"{escape_text(raw)}</span>"
    )


def chip(label: str, value: str) -> str:
    """Return a labelled inline fact, for scope fields that are not identifiers."""
    return (
        f'<span class="pc-ident-label">{escape_text(label)}</span>'
        f'<span class="pc-ident" title="{escape_text(value)}">{escape_text(value)}</span>'
    )


def quiet(text: str) -> str:
    """Return a fine-print fragment. Tier 4: identifiers, counts, disclaimers."""
    return f'<span class="pc-quiet">{escape_text(text)}</span>'


def state_badge(state: CaseState) -> str:
    """Return a filled, icon-bearing badge fragment for a case state."""
    icon, background, color, border = _STATE_STYLE.get(state, ("\u25cb", SUBTLE, MUTED, BORDER))
    label = getattr(state, "value", str(state))
    return (
        f'<span class="pc-state" style="background:{background};color:{color};'
        f'border:1px solid {border}">'
        f'<span class="pc-state-icon" aria-hidden="true">{icon}</span>'
        f"{escape_text(label)}</span>"
    )


def state_text(state: CaseState) -> str:
    """Return the plain-text twin of ``state_badge``, glyph included.

    ``st.column_config`` has no HTML cell renderer, so a table needs a text form
    of the badge. It reads from the same glyph table, which is what keeps the
    two from drifting, and it carries the icon so color is never the only
    signal in either form.
    """
    icon = _STATE_STYLE.get(state, ("\u25cb", "", "", ""))[0]
    return f"{icon} {getattr(state, 'value', state)}"


def execution_mode_label(mode: ExecutionMode | str | None) -> str:
    """Return the canonical provenance wording for a run mode.

    ``TEST`` reads as ``TEST SIMULATION`` everywhere. This labeling is required
    honesty, not decoration, so there is exactly one place that spells it.
    """
    if mode is ExecutionMode.LIVE:
        return "LIVE"
    if mode is ExecutionMode.TEST:
        return "TEST SIMULATION"
    if mode is ExecutionMode.HUMAN:
        return "HUMAN"
    if isinstance(mode, str) and mode:
        return mode.upper()
    return "UNKNOWN"


def provenance_badge(mode: ExecutionMode | str | None) -> str:
    """Return an outlined uppercase mono provenance chip.

    Accepts an ``ExecutionMode`` or a literal label such as ``"RECORDED RUN"``
    or ``"LIVE UNAVAILABLE"``. A dashed outline means the result did not come
    from a live model call.
    """
    label = execution_mode_label(mode)
    color, dashed = _PROVENANCE_STYLE.get(label, (FAINT, True))
    stroke = "dashed" if dashed else "solid"
    dot = "\u25cb" if dashed else "\u25cf"
    return (
        f'<span class="pc-prov" style="color:{color};border:1px {stroke} {color}">'
        f'<span class="pc-prov-dot" aria-hidden="true">{dot}</span>'
        f"{escape_text(label)}</span>"
    )


# --------------------------------------------------------------------------
# Text tiers
# --------------------------------------------------------------------------


def render_inline(*fragments: str) -> None:
    """Render inline fragments as one markdown element on a single row."""
    body_html = "".join(item for item in fragments if item)
    st.markdown(f'<div class="pc-inline">{body_html}</div>', unsafe_allow_html=True)


def section(title: str, *, subtitle: str | None = None) -> None:
    """Render a section heading, with optional supporting text below it.

    Tier 1. Use this instead of ``st.caption`` for anything that introduces a
    block. The subtitle is supporting text, not fine print.
    """
    sub = f'<div class="pc-section-sub">{escape_text(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f'<div class="pc-section"><div class="pc-section-title">{escape_text(title)}</div>'
        f"{sub}</div>",
        unsafe_allow_html=True,
    )


def card_title(text: str, *, small: bool = False) -> None:
    """Render a heading inside a card or panel. Tier 1."""
    extra = " pc-card-title-sm" if small else ""
    st.markdown(
        f'<div class="pc-card-title{extra}">{escape_text(text)}</div>', unsafe_allow_html=True
    )


def body(text: str) -> None:
    """Render supporting prose a reader is expected to read. Tier 3."""
    st.markdown(f'<div class="pc-body">{escape_text(text)}</div>', unsafe_allow_html=True)


def bullets(items: Sequence[str], *, ordered: bool = False, fine: bool = False) -> None:
    """Render a list at the supporting-text tier, or at the fine-print tier."""
    tag = "ol" if ordered else "ul"
    extra = " pc-list-fine" if fine else ""
    rows = "".join(f"<li>{escape_text(item)}</li>" for item in items)
    st.markdown(f'<{tag} class="pc-list{extra}">{rows}</{tag}>', unsafe_allow_html=True)


def note_block(title: str, items: Sequence[str], *, footer: str | None = None) -> None:
    """Render a recessed constraint note: a title, reasons, and an optional rule."""
    rows = "".join(f"<li>{escape_text(item)}</li>" for item in items)
    foot = f'<div class="pc-note-foot">{escape_text(footer)}</div>' if footer else ""
    st.markdown(
        '<div class="pc-note"><div class="pc-note-title">'
        f'<span aria-hidden="true">\u25b3</span> {escape_text(title)}</div>'
        f'<ul class="pc-list pc-list-fine">{rows}</ul>{foot}</div>',
        unsafe_allow_html=True,
    )


def kpi(label: str, value: Any, *, icon: str | None = None, help: str | None = None) -> None:  # noqa: A002
    """Render a bordered metric card. Tier 2: the primary value.

    ``icon`` takes Streamlit's ``":material/name:"`` form.
    """
    st.metric(label, value, icon=icon, help=help, border=True)


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def gap_strip(
    payment_cents: int,
    invoice_cents: int,
    *,
    explained: bool = False,
    currency: str = "USD",
    source: str | None = None,
) -> None:
    """Render the reconciliation strip: received, difference, invoice.

    The difference between what arrived and what was billed is the question the
    product exists to answer, so it is the emphasized cell. ``explained=True``
    means a documented reason has been accepted, which moves the cell out of
    amber. ``source`` adds a provenance note under the strip.
    """
    received = int(payment_cents)
    billed = int(invoice_cents)
    difference = billed - received
    if difference == 0:
        label = "Fully matched"
        hero_class = "pc-gap-cell pc-gap-settled"
        hero_value = money_html(0, tone="strong")
    elif explained:
        label = "Explained difference" if difference > 0 else "Explained overpayment"
        hero_class = "pc-gap-cell pc-gap-settled"
        hero_value = money_html(abs(difference), tone="strong")
    elif difference > 0:
        label = "Unexplained"
        hero_class = "pc-gap-cell pc-gap-hero"
        hero_value = money_html(difference, tone="accent")
    else:
        label = "Overpaid"
        hero_class = "pc-gap-cell pc-gap-hero"
        hero_value = money_html(abs(difference), tone="accent")
    note = ""
    if source:
        note = f'<div class="pc-gap-note">{escape_text(source)}</div>'
    elif difference != 0 and not explained:
        note = (
            '<div class="pc-gap-note">Documented bank fee, or an amount the customer '
            "still owes? Resolving this needs evidence, not an assumption.</div>"
        )
    st.markdown(
        '<div class="pc-gap">'
        f'<div class="pc-gap-cell"><div class="pc-gap-label">Received '
        f"{escape_text(currency)}</div>"
        f'<div class="pc-gap-value">{money_html(received, tone="strong")}</div></div>'
        f'<div class="{hero_class}"><div class="pc-gap-label">{escape_text(label)}</div>'
        f'<div class="pc-gap-value">{hero_value}</div></div>'
        f'<div class="pc-gap-cell"><div class="pc-gap-label">Invoice balance</div>'
        f'<div class="pc-gap-value">{money_html(billed, tone="strong")}</div></div>'
        f"{note}</div>",
        unsafe_allow_html=True,
    )


def equation_block(
    *,
    title: str,
    terms: Sequence[tuple[str, int | None, str, str, str]],
    footer: str,
    open_question: bool = False,
) -> None:
    """Render money arithmetic as the hero of a block. Tier 2.

    Each term is ``(operator, cents, label, source, tone)``. ``cents=None``
    renders a question mark, which is how an unexplained difference is stated:
    the amount is known, the reason is not. ``tone`` is passed to
    ``money_html``, so ``accent`` stays reserved for the unexplained or fee
    term.
    """
    cells = []
    for operator, cents, label, source, tone in terms:
        if operator:
            cells.append(f'<div class="pc-eq-op" aria-hidden="true">{escape_text(operator)}</div>')
        if cents is None:
            value = '<span class="pc-money pc-money-accent">?</span>'
        else:
            value = money_html(cents, tone=tone)
        source_html = f'<div class="pc-eq-src">{escape_text(source)}</div>' if source else ""
        cells.append(
            '<div class="pc-eq-term">'
            f'<div class="pc-eq-value">{value}</div>'
            f'<div class="pc-eq-label">{escape_text(label)}</div>'
            f"{source_html}</div>"
        )
    block_class = "pc-eq pc-eq-open" if open_question else "pc-eq"
    foot = f'<div class="pc-eq-foot">{escape_text(footer)}</div>' if footer else ""
    st.markdown(
        f'<div class="{block_class}"><div class="pc-eq-head">{escape_text(title)}</div>'
        f'<div class="pc-eq-row">{"".join(cells)}</div>{foot}</div>',
        unsafe_allow_html=True,
    )


def chain_block(steps: Sequence[tuple[str, str, bool]]) -> None:
    """Render ordered evidence steps. Each step is ``(title, body, is_gap)``.

    A gap step is amber, because a missing link is exactly the moment the system
    should be asking for a human rather than inferring.
    """
    rows = []
    for index, (title, text, is_gap) in enumerate(steps, start=1):
        row_class = "pc-chain-step pc-chain-miss" if is_gap else "pc-chain-step"
        rows.append(
            f'<div class="{row_class}"><div class="pc-chain-num">{index}</div><div>'
            f'<div class="pc-chain-title">{escape_text(title)}</div>'
            f'<div class="pc-chain-body">{escape_text(text)}</div></div></div>'
        )
    st.markdown(f'<div class="pc-chain">{"".join(rows)}</div>', unsafe_allow_html=True)


def verdict_block(headline: str, *, issues: Sequence[tuple[str, str]], ok: bool) -> None:
    """Render a validator result with issue codes kept legible."""
    rows = []
    for code, message in issues:
        rows.append(
            '<div class="pc-verdict-issue">'
            f'<span class="pc-verdict-code">{escape_text(code)}</span>'
            f"<span>{escape_text(message)}</span></div>"
        )
    block_class = "pc-verdict" if ok else "pc-verdict pc-verdict-bad"
    mark = "\u2713" if ok else "\u2715"
    st.markdown(
        f'<div class="{block_class}"><div class="pc-verdict-title">'
        f'<span aria-hidden="true">{mark}</span> {escape_text(headline)}</div>'
        f"{''.join(rows)}</div>",
        unsafe_allow_html=True,
    )


@dataclass(frozen=True)
class BudgetReading:
    """One bounded-budget reading: how much of a hard limit a run consumed."""

    label: str
    used: float
    limit: int
    unit: str = ""
    decimals: int = 0


def budget_strip(readings: Sequence[BudgetReading]) -> None:
    """Render budget consumption as labelled cells with proportional bars.

    A bar at or past ninety percent of its limit turns amber, because a run
    about to hit a ceiling is a thing a person may need to know about.
    """
    cells = []
    for item in readings:
        share = 0.0 if item.limit <= 0 else min(1.0, float(item.used) / float(item.limit))
        fill = "pc-strip-fill pc-strip-fill-high" if share >= 0.9 else "pc-strip-fill"
        if item.decimals:
            shown = f"{item.used:.{item.decimals}f}{item.unit}"
        else:
            shown = f"{item.used:g}{item.unit}"
        cells.append(
            '<div class="pc-strip-cell">'
            f'<div class="pc-strip-label">{escape_text(item.label)}</div>'
            f'<div class="pc-strip-value"><span class="pc-money">{escape_text(shown)}</span>'
            f'<span class="pc-strip-of"> of {escape_text(f"{item.limit}{item.unit}")}</span></div>'
            f'<div class="pc-strip-meter"><div class="{fill}" '
            f'style="width:{share * 100:.0f}%"></div></div></div>'
        )
    st.markdown(f'<div class="pc-strip">{"".join(cells)}</div>', unsafe_allow_html=True)


def count_strip(cells: Sequence[tuple[str, Any]]) -> None:
    """Render labelled counts as one bordered row. Sibling of ``gap_strip``."""
    body_html = "".join(
        '<div class="pc-strip-cell">'
        f'<div class="pc-strip-label">{escape_text(label)}</div>'
        f'<div class="pc-strip-value pc-money">{escape_text(value)}</div></div>'
        for label, value in cells
    )
    st.markdown(f'<div class="pc-strip">{body_html}</div>', unsafe_allow_html=True)


def lifecycle_stepper(status: LessonState | None) -> None:
    """Draw DRAFT, TESTING, PASSED, ACTIVE with the current position marked.

    ``status=None`` draws the path with nothing reached, which is what an empty
    Lessons page needs. A terminal status (FAILED, REJECTED, RETIRED) draws the
    path as history and names the exit underneath.
    """
    reached = -1 if status is None else _REACHED_INDEX.get(status, 0)
    terminal = status in _TERMINAL_NOTE
    cells: list[str] = []
    for index, step in enumerate(_LIFECYCLE_PATH):
        done = index <= reached if terminal else index < reached
        if status is not None and step is status:
            mark, step_class = "\u25cf", "pc-step pc-step-now"
        elif done:
            mark, step_class = "\u2713", "pc-step pc-step-done"
        else:
            mark, step_class = "\u25cb", "pc-step pc-step-todo"
        cells.append(
            f'<div class="{step_class}"><div class="pc-step-name">'
            f'<span aria-hidden="true">{mark}</span> {escape_text(step.value)}</div>'
            f'<div class="pc-step-note">{escape_text(_STEP_NOTE[step])}</div></div>'
        )
    if terminal and status is not None:
        cells.append(
            f'<div class="pc-step-exit"><strong>Exited as {escape_text(status.value)}.</strong> '
            f"{escape_text(_TERMINAL_NOTE[status])}</div>"
        )
    st.markdown(f'<div class="pc-steps">{"".join(cells)}</div>', unsafe_allow_html=True)


def doc_card(document: Any, *, excerpt_lines: int | None = None, footer: str | None = None) -> None:
    """Render a source document so it reads as a document.

    Accepts a ``Document`` (with ``body_text``) or a ``DocumentSummary``. When
    only a summary is available the card shows its header and omits the body.
    ``excerpt_lines`` truncates a long body and says so.
    """
    kind = getattr(document, "kind", None)
    kind_label = _DOCUMENT_LABEL.get(kind, getattr(kind, "value", "Source document"))
    title = getattr(document, "title", "Source document")
    document_id = getattr(document, "document_id", "")
    issued = getattr(document, "issued_date", None)
    sha = getattr(document, "sha256", None)
    body_text = getattr(document, "body_text", None)

    meta_bits = []
    if issued:
        meta_bits.append(f"Issued {escape_text(issued)}")
    if sha:
        meta_bits.append(f"sha {escape_text(str(sha)[:12])}")
    meta = f'<div class="pc-doc-meta">{" &middot; ".join(meta_bits)}</div>' if meta_bits else ""

    if isinstance(body_text, str) and body_text:
        lines = body_text.splitlines()
        truncated = excerpt_lines is not None and len(lines) > excerpt_lines
        shown = lines[:excerpt_lines] if truncated else lines
        rendered = f'<pre class="pc-doc-body">{escape_text(chr(10).join(shown))}</pre>'
        if truncated:
            hidden = len(lines) - len(shown)
            rendered += (
                f'<div class="pc-doc-foot">{hidden} more line(s) in the stored document.</div>'
            )
    else:
        rendered = (
            '<div class="pc-doc-foot">Body not loaded. Open the document to read the '
            "stored text.</div>"
        )
    foot = f'<div class="pc-doc-foot">{escape_text(footer)}</div>' if footer else ""
    st.markdown(
        '<div class="pc-doc"><div class="pc-doc-head">'
        f'<span class="pc-doc-kind">{escape_text(kind_label)}</span>'
        f'<span class="pc-doc-title">{escape_text(title)}</span>'
        f"{ident(document_id, width_ch=18)}{meta}</div>"
        f"{rendered}{foot}</div>",
        unsafe_allow_html=True,
    )


def readiness_panel(settings: svc.Settings) -> None:
    """Render the one live-readiness surface for the whole app.

    Individual buttons keep a short ``help=`` tooltip. This panel is the only
    place that spells the variable names out, which removes the most repeated
    text in the app without hiding why a chargeable action is unavailable.
    """
    missing = svc.missing_live_variable_names(settings)
    live_flag = "true" if settings.enable_live else "false"
    rows = [
        ("PRECEDENT_ENABLE_LIVE", live_flag, settings.enable_live),
        ("ANTHROPIC_API_KEY", settings.api_key_status, bool(settings.anthropic_api_key)),
        ("PRECEDENT_MODEL", settings.model_status, bool(settings.model)),
    ]
    body_rows = []
    for name, value, ok in rows:
        mark = "\u2713" if ok else "\u25b2"
        state_class = "pc-ready-ok" if ok else "pc-ready-bad"
        body_rows.append(
            f'<div class="pc-ready-row {state_class}">'
            f'<span class="pc-ready-mark" aria-hidden="true">{mark}</span>'
            f'<span class="pc-ready-name">{escape_text(name)}</span>'
            f'<span class="pc-ready-value">{escape_text(value)}</span></div>'
        )
    st.markdown(f'<div class="pc-ready">{"".join(body_rows)}</div>', unsafe_allow_html=True)
    if missing:
        st.warning(
            "Chargeable live actions are disabled. Missing or disabled: " + ", ".join(missing) + "."
        )
    else:
        st.success("Live mode, API key, and model identifier are configured.")
    st.caption("Cursor coding-model identity is not the application investigator model.")


def trace_timeline(
    events: Sequence[Any] | Iterable[Any], *, empty_message: str | None = None
) -> None:
    """Render run events as a readable timeline.

    Each ``RunEventKind`` gets its own icon and wording. ``precedent_retrieved``
    is emphasized because it is the evidence that stored memory changed what the
    system did. ``review_requested`` is the only amber row.
    """
    rows = list(events)
    if not rows:
        st.markdown(
            quiet(empty_message or "No stored events for this run."), unsafe_allow_html=True
        )
        return
    html_rows = []
    for event in rows:
        kind = getattr(event, "event_kind", None)
        icon, color = _EVENT_STYLE.get(kind, ("\u25cc", MUTED))
        label = _EVENT_LABEL.get(kind, getattr(kind, "value", str(kind)))
        row_class = "pc-tl-row"
        if kind is RunEventKind.PRECEDENT_RETRIEVED:
            row_class += " pc-tl-key"
        elif kind is RunEventKind.REVIEW_REQUESTED:
            row_class += " pc-tl-attention"
        elif kind in {
            RunEventKind.TOOL_FAILED,
            RunEventKind.PROPOSAL_REJECTED,
            RunEventKind.RUN_FAILED,
        }:
            row_class += " pc-tl-bad"
        detail = _payload_detail(getattr(event, "payload", None))
        html_rows.append(
            f'<div class="{row_class}">'
            f'<span class="pc-tl-seq">{escape_text(getattr(event, "sequence", ""))}</span>'
            f'<span class="pc-tl-icon" style="color:{color}" aria-hidden="true">{icon}</span>'
            f'<span class="pc-tl-kind">{escape_text(label)}</span>'
            f'<span class="pc-tl-detail">{escape_text(detail)}</span></div>'
        )
    st.markdown(f'<div class="pc-timeline">{"".join(html_rows)}</div>', unsafe_allow_html=True)


def _payload_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts = []
    for key in _PAYLOAD_DETAIL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    ids = payload.get("precedent_ids") or payload.get("retrieved_precedent_ids")
    if isinstance(ids, list) and ids:
        parts.append(", ".join(str(item) for item in ids))
    return " \u00b7 ".join(parts)
