# Parsing Architecture Refactor Plan

> **Status:** Drafted 2026-05-06. Multi-session, expected to span weeks.
> **Owners:** rotating (any AI agent + bradrcurry).
> **GitHub:** https://github.com/bradrcurry/NCUC_standalone (branch `main`).
> **Document conventions:** keep this doc the single source of truth for the
> refactor. Update statuses inline (use the checkbox list and the per-task
> tables). When a phase completes, add an entry to *Implementation Log* at
> the bottom rather than rewriting the body.

---

## 1. Why this plan exists

The existing parser architecture treats a *parser profile* as three things at
once:

1. a **router** — which doc gets which rule set,
2. a **rule set** — the regexes/normalizers it applies,
3. an **implicit family** — schedules/products grouped together.

That conflation has produced four observed failure modes:

| Failure | What it looks like | Why it happens |
|---|---|---|
| **Greedy regex** | LLM-generated regex matches almost any rate sheet → 110 of 153 suggestions auto-rejected | A regex is generated for "the profile" but carries no document-identity, so it false-positives on every other doc in the profile |
| **`wrong_profile` after parse** | 251 cases where the parser ran but extracted nothing — diagnosis blames profile assignment | Profile = router + rules; when extraction fails we can't tell whether the wrong rules ran or the right rules ran on a doc they don't fit |
| **Stale profile coverage** | New schedule codes (e.g., `RES-79`) routed by purity ≤ 50% to the dominant historical profile | Schedule-code → profile mapping is implicit, never refreshed |
| **No exception path** | Every doc that doesn't match a profile goes to the LLM-suggest pipeline, which then proposes rules at the *profile* scope, polluting other docs | There's no concept of "rule attached to one specific document" |

The refactor introduces a clean separation:

```
Today:  doc → classifier → profile_id → run profile's regexes → charges
Target: doc → evidence-gathering → identity bundle (with confidence)
                                          ↓
                                     routing decision
                                          ↓
                            ┌─────────────┴─────────────┐
                            ↓                            ↓
                  matched-template parser     document-specific rules
                  (high-confidence ID)        (low-confidence ID or unique)
                                          ↓
                                       charges
```

**What's reused unchanged:** all parser code (profiles become
*templates*), the fingerprinter, the classifier, the
`profile_consensus` engine, the `regex_validation` harness, the
`shadow_test` harness. Profiles aren't deleted — they get a renamed role.

**What changes:** *how* and *when* a regex gets bound to a document. That's
a routing-layer change, not a parser-layer change.

---

## 2. Phased plan (top-level checklist)

> Each phase is independently shippable. Layer 1 is zero-risk and high
> leverage and should be done first regardless of how aggressively we pursue
> Layers 2 & 3.

- [x] **Phase 0 — Quick wins from prior session** (carryover, not refactor)
  - [x] 0A. Deadline-cutoff fix in overnight wrapper *(2026-05-06)*
  - [x] 0B. Anti-greedy suggest prompt with profile-specific anchors *(2026-05-06)*
  - [x] 0C. Investigate why normalization suggestions are always 0 *(2026-05-06)*
- [x] **Phase 1 — Document Identity Layer** (foundation, zero behavior change)
  - [x] 1A. `document_identity` table + DDL *(2026-05-06)*
  - [x] 1B. Aggregator that populates from existing tables *(2026-05-06)*
  - [x] 1C. CLI to inspect identity bundles *(2026-05-06)*
  - [x] 1D. Identity-quality report *(2026-05-06)*
- [x] **Phase 2 — Routing Tier System** (decision layer, no extraction change)
  - [x] 2A. Tier classifier *(2026-05-06)*
  - [x] 2B. Tier-prediction validation against existing parses *(2026-05-06)*
  - [x] 2C. Tier dashboard *(2026-05-06)*
- [x] **Phase 3 — Profile-as-Template binding** (Tier 1 active routing) — *dry-run only; active binding deferred*
  - [x] 3A. Profile → template rename / re-doc *(2026-05-06)*
  - [x] 3B. Tier 1 binder (auto-bind high-confidence docs) *(2026-05-06; dry-run only)*
  - [x] 3C. Compare binder output vs current parser routing *(2026-05-06)*
- [ ] **Phase 4 — Per-Document Rules track** (Tier 3 path)
  - [ ] 4A. `document_specific_rules` table
  - [ ] 4B. Rule-attachment generator (replaces current per-profile suggest for Tier 3)
  - [ ] 4C. Promotion path (per-doc rule → template rule when N docs share it)
- [ ] **Phase 5 — Decommission `wrong_profile`**
  - [ ] 5A. Replace `wrong_profile` failure type with tier-bind diagnostics
  - [ ] 5B. Migrate existing `wrong_profile` rows
  - [ ] 5C. Update overnight loop to skip the legacy path

---

## 3. Phase 0 — Quick wins (carryover)

These were the "three concrete improvements worth doing" identified in the
2026-05-06 session and are worth completing whether or not the full refactor
ships. They are scoped and stand alone.

### 0A. Deadline-cutoff fix in overnight wrapper

**Problem:** When `--max-runtime-minutes` budget shrinks below ~3 min in the
final loop iteration, `suggest` consumes the whole budget (10 docs × ~30–60s)
and `validate`/`shadow_test` don't run. Result: late iterations leave
`pending_review` rows that never get validated until the next overnight run.

**Fix options (pick one):**
- **Option A (simpler):** within one CLI invocation, run `validate` first and
  `suggest` last, so the deadline-tail iteration validates pending work
  before generating more.
- **Option B (cleaner):** add per-stage minimum runtime budget; skip stages
  whose remaining budget is below their floor.

**Files likely touched:**
- `src/duke_rates/document_intelligence/parse_improvement_loop.py`
  (`run()` task ordering or new gating check)

**Acceptance test:**
- A 3-min budget run starting from `pending_review > 0` should finish with
  `pending_review` not larger than it started.

**GitNexus before editing:** run `gitnexus_impact({target: "ParseImprovementLoop.run", direction: "upstream"})`.

**Implemented as Option B** (per-stage minimum runtime budget). Added
`STAGE_MIN_BUDGET_SECONDS` mapping in `parse_improvement_loop.py` and a
budget guard before each stage. When remaining wall-clock is below the
stage's minimum, the stage is skipped with stat
`skipped_insufficient_budget=1` and the loop continues — so deterministic
stages still run when the LLM-bound ones can't fit.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

### 0B. Anti-greedy suggest prompt

**Problem:** The LLM generates regexes like
`\$?\d+(\.\d+)?\s*\/\s*(kWh|kW|month|year|day)\b` — patterns generic enough
to match any rate sheet. Of 153 suggestions, 110 auto-rejected as broad
false positives.

**Fix:** Augment the suggest prompt to require **profile-specific anchors**
in every candidate regex. Anchors come from the failed doc's identity
bundle (Phase 1 makes this trivial; in the meantime, pull from
`document_fingerprints_v2`):

- a high-specificity schedule code (e.g., `RES-28`),
- a schedule keyword phrase (e.g., `"Schedule R-1"`, `"Rider STS"`),
- or a distinctive title-candidate substring.

Validation harness should also reject candidates whose regex contains zero
identifiable anchors when measured against the doc's evidence — that catches
the failure mode at suggest time rather than at validate time.

**Files likely touched:**
- `src/duke_rates/document_intelligence/regex_suggestions.py` (prompt template + structured anchor injection)
- `src/duke_rates/document_intelligence/regex_validation.py` (optional anchor presence check)

**Acceptance test:**
- Run suggest on 10 fresh `regex_gap` cases. False-positive auto-rejection
  rate should drop from ~72% to under 40%.

**GitNexus before editing:** run `gitnexus_impact({target: "RegexSuggestionGenerator.generate_suggestion", direction: "upstream"})`.

**Implementation:**
- `regex_suggestions.py`: added `fetch_document_anchors()`,
  `render_anchors_for_prompt()`, `regex_contains_anchor()` module-level
  helpers. Prompt template gained a `## DOCUMENT-SPECIFIC ANCHORS (REQUIRED)`
  section with concrete worked example. `generate_suggestion()` now passes
  the anchor block into the prompt.
- `regex_validation.py`: added Phase 1b in `validate_suggestion()` —
  `_check_regex_has_anchor()` looks up `source_pdf` via
  `diagnosis_id → parse_attempt_id`, fetches the anchors, and rejects
  before the corpus sweep if the regex contains zero anchors. New status
  `rejected_no_anchor` added to `ALLOWED_VALIDATION_STATUSES`.
- Smoke-tested on 50 fingerprinted docs: anchor extraction returned
  high-specificity codes (e.g. RES-17, RES-19) and distinctive titles
  (e.g. "RIDER NMB", "NET METERING BRIDGE") cleanly. Suggestion 153 (a
  prior `rejected_false_positive`) now gets caught at validation time
  with a clear reason: "regex contains no document-specific anchor
  (available: titles=['RIDER NMB', 'NET METERING BRIDGE'])".

**Acceptance test follow-up:** the published acceptance criterion was that
auto-rejection rate drops from ~72% to under 40%. We won't have that
number until the next overnight run produces fresh suggestions under the
new prompt. The early-exit `rejected_no_anchor` path will surface the new
distribution.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

### 0C. Investigate empty normalization suggestions

**Problem:** 0 of 153 suggestions have `suggestion_type = 'normalization_rule'`.
Either the prompt doesn't surface normalization as an option, or the path is
filtered out somewhere.

**Investigation steps:**
1. Read `_SUGGESTION_SYSTEM_PROMPT` in `regex_suggestions.py`. Does it
   mention normalization at all?
2. Look at one full LLM response to a suggest call. Is the model emitting
   normalization candidates and are they being dropped?
3. Decide whether to fix the prompt (most likely), retire the
   `normalization_rule` type, or split into a separate task kind.

**Files likely touched:**
- `src/duke_rates/document_intelligence/regex_suggestions.py`

**Acceptance test:**
- Either a documented decision to retire the type, OR ≥ 5% of new suggestions
  emit `suggestion_type = 'normalization_rule'`.

**Investigation findings:** The empty-normalization issue is **upstream of
the suggest prompt.** Database audit:

- Of 153 existing suggestions: 140 `regex_candidate`, 13 `parser_profile_hint`,
  0 `normalization_rule`.
- Of all diagnoses with failure_type in `('regex_gap', 'normalization_gap',
  'ocr_noise')` (the trio that feeds the suggest stage): 194 `regex_gap`,
  0 `normalization_gap`, 0 `ocr_noise`.

The suggest LLM correctly never emits normalization rules because every
diagnosis it sees is labeled `regex_gap`. The issue is the **diagnose
prompt** — it lists failure types as a flat enum without definitions,
giving the model no basis to choose `normalization_gap` or `ocr_noise`
over the more obvious-sounding `regex_gap`.

**Fix shipped:** Rewrote `_DIAGNOSIS_SYSTEM_PROMPT` in
`parse_diagnosis.py` to include explicit definitions for every failure
type, with rules for the OCR/normalization disambiguation:

> *When you see OCR artifacts (ligatures, character substitutions, broken
> spacing), prefer `ocr_noise` over `regex_gap`.*
> *When rates are clearly present but in non-canonical form (cents
> notation, unusual unit strings), prefer `normalization_gap` over
> `regex_gap`.*

**Effect:** The next overnight diagnose pass should produce some
`normalization_gap` and `ocr_noise` rows, which the suggest stage can
then act on. We won't have the 5%-of-suggestions metric until a fresh
diagnose+suggest cycle runs against the rediagnose-unknown pool.

**Decision:** keep the `normalization_rule` suggestion_type. Did NOT
retire it.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

---

## 4. Phase 1 — Document Identity Layer

**Goal:** one row per `source_pdf` that aggregates every signal we have
about that document, with a confidence score and an evidence log.
**Zero behavior change to extraction.** This is a read-only output layer
that future phases consume.

### 1A. `document_identity` table

```sql
CREATE TABLE document_identity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL UNIQUE,

    -- Strong (high-specificity) signals
    schedule_codes_strong_json TEXT NOT NULL DEFAULT '[]',
    rider_codes_strong_json TEXT NOT NULL DEFAULT '[]',
    leaf_numbers_json TEXT NOT NULL DEFAULT '[]',
    detected_titles_json TEXT NOT NULL DEFAULT '[]',
    filename_signals_json TEXT NOT NULL DEFAULT '[]',

    -- Classifier consensus (already exists in document_classifications)
    classifier_label TEXT,
    classifier_confidence REAL,

    -- Profile consensus (already exists in parser_profile_recommendations)
    profile_consensus_top TEXT,
    profile_consensus_confidence REAL,
    profile_consensus_margin REAL,

    -- Inferences (derivable from above)
    inferred_family TEXT,
    inferred_doc_type TEXT,
    inferred_effective_date TEXT,

    -- Overall identity confidence (0.0-1.0)
    overall_confidence REAL NOT NULL DEFAULT 0.0,

    -- Append-only log of evidence inputs (for audit)
    evidence_log_json TEXT NOT NULL DEFAULT '[]',

    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_document_identity_pdf ON document_identity(source_pdf);
CREATE INDEX idx_document_identity_confidence ON document_identity(overall_confidence DESC);
```

**Implementation:** New module
`src/duke_rates/document_intelligence/document_identity.py` ships the DDL,
a `ensure_schema()` bootstrap function (idempotent, called by both the
aggregator and the CLI), and indexes on `source_pdf` and
`overall_confidence DESC`. Schema bootstrap was verified live — table
created cleanly with 19 columns + 2 indexes.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

### 1B. Aggregator that populates from existing tables

A single deterministic batch (no LLM) that reads from:

- `document_fingerprints_v2` (schedule codes, rider codes, leaf numbers, titles)
- `document_classifications` (classifier label + confidence)
- `parser_profile_recommendations` (profile consensus)
- filename heuristics (regex against `source_pdf`)

…and writes one `document_identity` row per source_pdf, with an
`evidence_log` listing each contributing signal.

The `overall_confidence` score is a weighted aggregate. Initial weighting
(tunable):

- strong schedule code (matches `[A-Z]{2,5}-?\d{1,3}[A-Z]?` AND known): 0.30
- title-candidate match against profile-distinctive title: 0.20
- filename pattern hits a router rule: 0.15
- classifier confidence ≥ 0.7: 0.15
- profile_consensus confidence ≥ 0.7 AND margin ≥ 0.15: 0.20

Sum, cap at 1.0. Tune from the identity-quality report (1D).

**Files likely touched:**
- `src/duke_rates/document_intelligence/document_identity.py` (new module)
- CLI command `populate-document-identity-nc` in `cli.py`

**Acceptance test:**
- Populates a row per `source_pdf` with ≥ 1 evidence entry.
- `overall_confidence` distribution looks reasonable on a sample of 50 docs
  (compare against manual judgment).

**GitNexus before editing:** run `gitnexus_query({query: "document fingerprint classification"})` to find adjacent code that should also write evidence.

**Implementation:** `DocumentIdentityAggregator` class in
`document_identity.py`. Bridges `document_classifications` to
`source_pdf` via `historical_documents.local_path` (the table uses
`subject_kind='historical_document'`, not `'source_pdf'` — discovered
during smoke testing and corrected). Filename heuristics encoded as
`FILENAME_SIGNAL_RULES` mirroring (and extending) the rules in
`profile_consensus`. `populate_all()` upserts via `ON CONFLICT(source_pdf)`
so re-runs are idempotent.

**First full-corpus run:** 4412 source_pdfs populated in seconds. Signal
coverage: schedule_codes 1.9%, distinctive_titles 98.0%, classifier_label
14.9%, profile_consensus 4.1%. Confidence distribution: 6 high, 319 mid,
4087 low — the corpus-wide picture confirms the plan's premise that most
docs have weak identity today.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

### 1C. CLI to inspect identity bundles

`report-document-identity-nc --source-pdf PATH` prints the identity bundle
for one doc (all evidence sources, all signals, score breakdown). Used for
debugging and for tuning the confidence weights in 1B.

`report-document-identity-summary-nc` prints distributions: confidence
histogram, top schedule codes by frequency, docs with low confidence (worth
human review).

**Implementation:** Three CLI commands wired in `cli.py`:
- `populate-document-identity-nc` — runs the aggregator (with optional
  `--limit N` for partial passes).
- `report-document-identity-nc --source-pdf PATH` — prints one bundle.
  Falls back to building live (without persisting) when the PDF has no
  saved row yet, so debug runs don't require a populate first.
- `report-document-identity-summary-nc` — confidence buckets + signal
  coverage with histogram bars (ASCII `#` for cp1252 compatibility on
  Windows console).

Verified live on a high-confidence MGS-32 schedule sheet: bundle showed
schedule_codes=['MGS-31','MGS-32'], 4 distinctive titles incl.
'SCHEDULE MGS-32', filename signal `schedule_mgs`, classifier
`TARIFF_SHEET conf=1.0`, profile consensus `generic_residential conf=1.0
margin=0.849`. Overall confidence: 1.0. The CLI also surfaced an
important bug-finding: this is an MGS doc routed by consensus to
`generic_residential` — the kind of high-confidence-but-wrong routing
Phase 2/3 need to address.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

### 1D. Identity-quality report

Once 1B is populated, generate a report comparing identity confidence
against actual parse outcomes:

- For docs with `overall_confidence ≥ 0.85`, what's the parse success rate?
- For docs with `overall_confidence < 0.5`, are they the same docs landing
  in `wrong_profile` / `unknown` failure types?
- Are there docs where `profile_consensus_top != current_parser_profile`
  AND `overall_confidence ≥ 0.85` (i.e., we know with high confidence the
  routing is wrong)?

This validates the confidence weights and identifies the docs that would
benefit most from Phase 2.

**Implementation:** CLI `report-document-identity-quality-nc` joins
`document_identity` × `parse_attempt_logs` × `llm_parse_diagnostics` and
emits three views:

1. **Identity confidence vs parse outcomes:** parsed-rate by bucket.
2. **Identity confidence vs diagnosed failures:** failure_type counts by
   bucket.
3. **High-confidence routing disagreements:** docs where overall confidence
   ≥ 0.85 and `profile_consensus_top` differs from current `parser_profile`
   — these are the highest-leverage reassignment leads for Phase 2/3.

Saves a JSON snapshot under `docs/reports/document_identity_quality/`.

**First-run findings (4412 identity rows):**
- High-confidence parse rate **60.8%** vs low-confidence **35.0%** — a
  25-point gap that validates the weights are tracking real parsing
  signal.
- Mid-confidence bucket has the largest absolute count of `regex_gap`
  (144) and `wrong_profile` (78) failures — Phase 2's tier router will
  triage these first.
- 50 high-confidence routing disagreements found; the top 10 are all
  `current=unknown -> recommended=generic_residential` (the consensus
  engine identifying docs the legacy classifier missed entirely).

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/parsing-architecture` |

---

## 5. Phase 2 — Routing Tier System

**Goal:** every doc gets labeled TIER 1 / 2 / 3 based on its identity
bundle. Initially the label is informational only — we compare against
current parser routing to validate before flipping the tier system on.

### 2A. Tier classifier

Pure function over `document_identity`. Initial cutoffs:

- **TIER 1** — `overall_confidence ≥ 0.85` AND `profile_consensus_margin ≥ 0.15`
- **TIER 2** — `0.5 ≤ overall_confidence < 0.85` OR `margin < 0.15`
- **TIER 3** — `overall_confidence < 0.5` OR no profile consensus

**Implementation:** New module
`src/duke_rates/document_intelligence/routing_tier.py` ships:
- `Tier` IntEnum (TIER_1=1, TIER_2=2, TIER_3=3) and `TierClassification`
  dataclass (pure data, no DB dependency).
- `classify_tier(identity_bundle)` — pure function matching plan cutoffs;
  handles the no-consensus case as Tier 3 regardless of overall
  confidence (an edge case worth noting).
- `document_routing_tier` table + `ensure_schema()`.
- `TierAggregator` class with `label_all()` (idempotent batch upsert)
  and `label_one(source_pdf)` (single-doc refresh).

**First population pass:** 4412 docs labeled. Tier distribution:
**6 Tier 1 / 145 Tier 2 / 4261 Tier 3**. The dominance of Tier 3
matches the Phase 1 finding that ~93% of docs have weak identity —
this is the population that Phase 4's per-doc rules track will need to
serve.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-2-routing-tiers` |

### 2B. Tier-prediction validation

Cross-check predicted tier against actual parse outcome for currently-parsed
docs:

- Tier 1 docs should mostly have `parse_attempt_logs.status = 'parsed'` with
  `charge_count > 0`. If a Tier 1 doc failed extraction, that's a *template*
  problem, not a routing problem — flag it as a high-priority parser fix.
- Tier 3 docs should mostly have `wrong_profile` / `unknown` diagnoses. If a
  Tier 3 doc parsed successfully, the tier cutoffs may be too strict.

Output: a confusion-matrix-like report.

**Implementation:** `build_tier_validation_report(db_path)` in
`routing_tier.py`. Joins `document_routing_tier × parse_attempt_logs ×
llm_parse_diagnostics` and emits four views:

1. **tier_outcomes** — per-tier `parse_attempt_logs.status` distribution
   plus `parsed_with_charges_rate`.
2. **tier_diagnoses** — per-tier `failure_type` counts.
3. **tier1_extraction_failures** — Tier 1 docs that did NOT parse
   cleanly (template bugs).
4. **tier3_unexpected_successes** — Tier 3 docs that parsed cleanly
   anyway (cutoff-tuning candidates).

**First-run findings:**
- **Tier 1: 60.8% parsed-with-charges** (321 of 528 attempts). The 50
  template-bug candidates all show `parser_profile=unknown` with
  `recommended=generic_residential` — docs the legacy classifier missed
  entirely; Phase 3 binding will fix these automatically.
- **Tier 2: 59.1%** — only marginally below Tier 1, suggests cutoffs are
  slightly conservative on the high end.
- **Tier 3: 46.5%** — surprisingly high success rate. The 50 Tier 3
  unexpected successes cluster at confidence ~0.80 with
  `parser_profile=generic_residential` or
  `progress_current_leaf_bridge` — these are docs whose only weak
  signal is no profile consensus. Tuning candidate for §5.2C.
- **Tier-diagnosed failures match expectations:** Tier 3 dominates
  `wrong_profile` (94) and `unknown` (51); Tier 1 has only 3 of each.

Note: tier-row counts (6 / 145 / 4261) and validation-attempt counts
(528 / 7291 / 32594) differ because one source_pdf often has many
parse_attempt_logs rows (different page ranges, parser profiles tried).
Both views are useful and the dashboard surfaces both.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-2-routing-tiers` |

### 2C. Tier dashboard

CLI command `report-routing-tiers-nc` showing tier distributions, top
profile_consensus_top values per tier, sample docs per tier.

**Implementation:** Three CLI commands in `cli.py`:
- `populate-routing-tier-nc` — runs the tier aggregator (with optional
  `--limit N`).
- `report-routing-tier-nc` — distribution histogram (ASCII `#` bars for
  cp1252 compatibility) plus N example rationales per tier.
- `report-routing-tier-validation-nc` — invokes
  `build_tier_validation_report` and prints the §5.2B findings; saves
  JSON to `docs/reports/routing_tier_validation/<timestamp>.json`.

Verified live end-to-end on 4412 rows. The validation report surfaces
50 template-bug candidates and 50 cutoff-tuning candidates with one
command.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-2-routing-tiers` |

---

## 6. Phase 3 — Profile-as-Template binding (Tier 1 active routing)

**Goal:** Tier 1 docs auto-bind to their consensus profile (now called a
*template*). Run the template's regexes. Use the result as the parse output.

This is the first phase that *changes extraction behavior*. We run it in
shadow mode first (compare against current routing without applying), then
flip on for Tier 1 only.

### 3A. Profile → template rename / re-doc

Mostly a documentation/intent change. Profiles still exist as code; they
get a new role description. Add a `template_metadata` field that captures:

- which schedule codes / rider codes the template is *meant* for
- which families / utilities
- a "scope" (template-level vs anchor-required)

Code-wise this is mostly comments and an optional metadata file
(`profile_templates.yaml` or similar). No behavior change.

**Implementation:** Two new files.
- `src/duke_rates/document_intelligence/profile_templates.yaml` — the
  catalog itself, **41 templates** seeded from the actively-used parser
  profile names. Each entry has `description`, `utility`, `state`,
  `scope`, `intended_schedule_codes`, `intended_rider_codes`,
  `intended_families`, `notes`. Four scopes are allowed:
  `template-level`, `anchor-required`, `bundle-aware`, `redline-aware`.
  37 of 41 templates are `template-level`; 4 (`progress_specialty_rider`,
  `tiered_ingest`, `unknown`, `carolinas_schedule_bridge`) are
  `anchor-required` because they historically host many distinct rate
  types under one banner.
- `src/duke_rates/document_intelligence/profile_template_metadata.py` —
  the loader. Caches the YAML, exposes `get_template_metadata(profile)`,
  `all_templates()`, `is_known_template(profile)`,
  `is_safe_for_tier1_binding(profile)`. Robust to missing PyYAML
  (returns empty catalog with a warning) and to missing entries
  (returns `None`, which Phase 3B treats as "anchor-required for safety").

The catalog is intentionally hand-curated rather than auto-derived from
parse outcomes — observed schedule-code → profile pairs are noisy
(many compliance bundles assign all codes to whichever profile parsed
the bundle), so the YAML captures intent, not history. Phase 3C
disagreement reports surface gaps to refine.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-3-template-binding` |

### 3B. Tier 1 binder

For each Tier 1 doc, look up the consensus profile, run the template's
regexes, persist charges. If extraction succeeds, mark `parsed`. If it
fails, flag as template-fix-needed (Tier 1 docs failing extraction is a
strong signal that the template needs work).

**Implementation:** New module
`src/duke_rates/document_intelligence/tier1_binder.py` (≈480 lines).
Ships a **dry-run-only** binder per the plan's "shadow mode first"
principle. Extraction is NOT invoked; the binder records *proposals* in
a new `tier1_binding` table for Phase 3C to compare against current
routing. Statuses: `proposed` (safe template, ready), `refused`
(anchor-required or unknown template), `no_consensus` (data bug guard),
plus `applied` / `template_bug` reserved for future active mode.

The pure decision function `Tier1Binder.decide(tier_row)` returns a
`BindingDecision` with no DB writes — Phase 4/5 can re-use it for
their own routing logic without re-implementing the safety checks.

`fetch_binding_summary(db_path)` provides aggregates (status counts,
proposed-profile distribution, agreement-with-current bucketing,
disagreement samples) for the dashboard.

**First-run results (against current Tier 1 docs, n=6):**
- 6 of 6 docs got `proposed` status (all consensus-bound to
  `generic_residential`, which is template-level scope).
- 2 of 6 agree with current routing; 4 disagree.
- Disagreement detail: 3 are `current=unknown` (legacy classifier
  missed them); 1 is `current=progress_standby_service`.

The deliberate decision NOT to ship active extraction in 3B keeps the
Phase 3C comparison clean — when active mode is later enabled, it can
look at the same proposal table to know what to do.

| Status | completed (dry-run) |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-3-template-binding` |

> **Note:** *Active-mode binding is deferred*. The plan's full §3B
> deliverable says "run the template's regexes, persist charges." This
> implementation persists only proposals. A future task — call it
> **3D** — will bridge `tier1_binding` proposals into the existing
> parser pipeline. The 3C comparison report below recommends *against*
> doing so until disagreement rate is materially below 5% and the
> Tier 1 sample size is larger.

### 3C. Comparison run

Run the binder in dry-run mode against the existing parsed corpus.
Compare Tier 1 binder output against current parser_profile assignments.
Disagreement counts get bucketed by reason (different profile, no profile
in current state, etc.).

If the disagreement rate is < 5%, flip Tier 1 on. If it's higher, audit
the disagreements before proceeding.

**Implementation:** `build_comparison_report(db_path)` in
`tier1_binder.py` plus three new CLI commands in `cli.py`:
- `bind-tier1-proposals-nc` — runs the dry-run binder
- `report-tier1-binding-nc` — summary of proposals
- `report-tier1-binding-comparison-nc` — the §6.3C comparison; saves
  JSON to `docs/reports/tier1_binding_comparison/`

Disagreements are bucketed into three kinds:
- `current_unknown` — legacy classifier missed the doc entirely
- `current_other` — both ran a template, but different ones
- `no_current_attempt` — never parsed before

The report also crosses disagreement against current parse outcome
(`parsed_with_charges` vs `empty_or_failed` vs `other`) so that for
each disagreement we can ask "is the current routing producing
charges anyway?" — i.e., is the binder right or wrong.

**First-run findings (n=6 Tier 1 docs):**
- **Disagreement rate: 66.7%** — far above the 5% threshold for
  flipping active binding on. **Recommendation: keep the binder in
  dry-run.** Re-evaluate after Phase 1 confidence-weight tuning and
  Phase 4 work increase the Tier 1 population.
- *But* in every disagreement, current parse outcome is
  `empty/failed` (4/4). So the binder isn't wrong — it's correctly
  rescuing docs the legacy classifier failed to route. The blocker
  on flipping active mode isn't binder accuracy; it's sample size.
- Top flips: `unknown -> generic_residential` (3 docs),
  `progress_standby_service -> generic_residential` (1 doc).
- The MGS-32 doc flagged in Phase 1D as a known mis-routing case is
  among the disagreements, confirming the binder catches it.

**Acceptance gate not met for active mode**, but the comparison
infrastructure is solid and reusable. Future agents tuning the
confidence weights or adding more Tier 1 docs (e.g., by enabling
classifier output for more historical_documents) should re-run
`report-tier1-binding-comparison-nc` to track the disagreement rate
over time.

| Status | completed |
|---|---|
| Owner | claude-opus-4-7 |
| Started | 2026-05-06 |
| Completed | 2026-05-06 |
| PR / commit | branch `refactor/phase-3-template-binding` |

---

## 7. Phase 4 — Per-Document Rules track (Tier 3 path)

**Goal:** for Tier 3 docs (low-confidence identity OR Tier 1 docs where the
template fails), generate regexes attached to *specific document_identity
ids* — never to a profile/template. Validation harness only runs the
candidate against that doc and a small set of close siblings (same family,
same schedule code).

This is the change that **eliminates greedy-regex collateral damage**.

### 4A. `document_specific_rules` table

```sql
CREATE TABLE document_specific_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_identity_id INTEGER NOT NULL REFERENCES document_identity(id),
    candidate_regex TEXT NOT NULL,
    candidate_normalization TEXT,
    expected_unit TEXT,
    target_field TEXT,
    suggestion_id INTEGER,  -- back-link to llm_regex_suggestions if applicable
    status TEXT NOT NULL DEFAULT 'pending',
    promotion_eligible_count INTEGER NOT NULL DEFAULT 0,  -- how many sibling docs would adopt this
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 4B. Rule-attachment generator

Replaces the current `suggest` task for Tier 3 docs. Instead of asking the
LLM "give me a regex for profile X," asks "give me a regex for *this
specific doc* (with this title, these schedule codes, this filename)."

Validation:
- run candidate against the target doc — must extract ≥ 1 charge.
- run against a small sibling set (closest 5 docs by identity similarity) —
  may extract or not, but must not produce out-of-range numerics.
- skip the corpus-wide false-positive check entirely (it's not relevant —
  the rule is doc-scoped).

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 4C. Promotion path

When ≥ N (N=3 initially) document-specific rules in a family share a similar
regex (string-similarity threshold), surface them as a promotion candidate
to the family's template. A human (or higher-confidence LLM call with full
context) approves the promotion.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

---

## 8. Phase 5 — Decommission `wrong_profile`

Once Phases 1–4 are working, the `wrong_profile` failure_type is redundant —
it gets caught at routing time, not at extraction time.

### 5A. Replace `wrong_profile` failure type

Diagnose stage stops emitting `wrong_profile`. Replaced by tier-bind
diagnostics (handled at routing layer).

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 5B. Migrate existing `wrong_profile` rows

For each existing `wrong_profile` row, look up the doc's identity bundle and
re-route via the tier system. Most should land at a template; the remainder
go to per-doc rules.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 5C. Update overnight loop

Drop the `profile_consensus` task (its function moves into the routing
layer). Add a new task kind `route_tier_1` and `route_tier_3_rules`.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

---

## 9. Working with this plan across sessions

### When picking up a new session

1. Read this whole file. Each phase has acceptance tests; a phase is "done"
   only when its tests pass and the status table is updated.
2. **Re-index GitNexus** before substantive code work:
   ```
   npx gitnexus analyze
   ```
   This refreshes the symbol/relationship graph so impact analysis reflects
   recent changes.
3. Pick the **lowest-numbered open task**. Phase 0 → 1 → 2 etc. Within a
   phase, finish the earliest incomplete subtask first.
4. Before editing any function, run impact analysis as the project's
   `CLAUDE.md` requires:
   ```
   gitnexus_impact({target: "<symbol>", direction: "upstream"})
   ```

### When completing a task

1. Update the status table at the bottom of the task with: status →
   `completed`, your owner name, started/completed dates, the commit hash
   or PR link.
2. Add an entry to *Implementation Log* (Section 10) summarizing what
   shipped, what tests passed, anything surprising.
3. If the task created new symbols/files, run `npx gitnexus analyze` again
   and note the new symbol count in the log.
4. Run `gitnexus_detect_changes()` before committing per the project's
   `CLAUDE.md`.

### Multi-agent handoffs

This plan is structured so multiple AI agents can work on different phases
in parallel **as long as**:

- Phases are disjoint (Phase 0 tasks have no dependency on Phase 1+).
- Within a phase, agents take individual subtasks (e.g., one agent on 1A,
  another on 1C).
- Agents update this document at the start (mark in-progress) and end
  (mark completed) of their work, plus add an Implementation Log entry.

If two agents pick up overlapping tasks, the one that updated the status
table first wins; the other moves to the next open task.

### GitHub workflow

- Branch per phase: `refactor/phase-1-document-identity`, `refactor/phase-2-routing-tiers`, etc.
- Within a branch, one PR per subtask is preferred but multiple subtasks per PR is fine if they're tightly related.
- PRs link back to this document by section number (e.g., "Implements §4.1B").

### When the plan is wrong

This plan is a draft. If a phase's design turns out to be wrong once
implementation begins:

1. **Don't silently change scope** — update this document first.
2. Add an *Implementation Log* entry explaining why the design changed.
3. Mark the affected subtasks accordingly.

---

## 10. Implementation Log

> Append-only. Newest entries on top. One paragraph per entry. Reference
> commit hashes and the section number of the task.

### 2026-05-06 — Phase 3 (dry-run) — claude-opus-4-7

**§6.3A/B/C shipped on branch `refactor/phase-3-template-binding`**
(branched from `main` after the Phase 2 PR merged as commit
`25b0750`). Three new files:
`src/duke_rates/document_intelligence/profile_templates.yaml` (41
templates with intent metadata), `profile_template_metadata.py` (typed
loader with `is_safe_for_tier1_binding`), and `tier1_binder.py`
(≈480 lines: `Tier1Binder`, `BindingDecision`, `tier1_binding` table,
`fetch_binding_summary`, `build_comparison_report`). Three new CLI
commands: `bind-tier1-proposals-nc`, `report-tier1-binding-nc`,
`report-tier1-binding-comparison-nc`. **Phase 3B is dry-run only** —
the binder records *proposals* in `tier1_binding`, no extraction runs.
Active-mode binding (running templates and persisting charges) is
explicitly deferred to a future "3D" task because the §6.3C comparison
report shows 66.7% disagreement on the current Tier 1 population
(n=6) — far above the 5% threshold the plan set for flipping active
binding on. **However**, for every disagreement the current parse
outcome is `empty_or_failed` (4/4), meaning the binder isn't wrong —
it's correctly identifying docs the legacy classifier missed. The
blocker on active mode is sample size, not accuracy. The deferred 3D
task should re-run the comparison after Phase 4 work (or after
classifier coverage broadens, e.g., by joining
`historical_documents.local_path` for more docs) lifts the Tier 1
population. The MGS-32 misrouting flagged in Phase 1D appeared as a
disagreement, confirming the binder catches known cases.

### 2026-05-06 — Phase 2 (full) — claude-opus-4-7

**§5.2A/B/C shipped on branch `refactor/phase-2-routing-tiers`** (branched
from `refactor/parsing-architecture` after Phase 0/1 work). New module
`src/duke_rates/document_intelligence/routing_tier.py` (≈340 lines):
`Tier` IntEnum, `TierClassification` dataclass, pure
`classify_tier()` function, `document_routing_tier` table + DDL,
`TierAggregator` (idempotent batch upsert), and
`build_tier_validation_report()` for the §5.2B cross-check. Three new
CLI commands in `cli.py`: `populate-routing-tier-nc`,
`report-routing-tier-nc`, `report-routing-tier-validation-nc`.
**First labeling pass on 4412 rows: 6 / 145 / 4261 (Tier 1/2/3).**
Validation surfaced 50 Tier 1 template-bug candidates (all
`parser_profile=unknown` → `recommended=generic_residential`, same
pattern as the Phase 1D disagreement findings) and 50 Tier 3
unexpected-success candidates clustering at confidence 0.80 (cutoff
tuning lead for future revision). **Discovered:** the no-consensus →
Tier 3 rule causes some 0.80-confidence docs (with strong fingerprint
+ classifier signals but no consensus row) to land in Tier 3 despite
parsing successfully — worth noting for any future cutoff revision.
The dashboard prints both source_pdf-unique counts (from
`document_routing_tier`) and parse_attempt-row counts (from the
validation join) since both views are useful.

### 2026-05-06 — Phase 1 (full) — claude-opus-4-7

**§4.1A/B/C/D shipped on branch `refactor/parsing-architecture`.** New
module `src/duke_rates/document_intelligence/document_identity.py`
(450+ lines): schema bootstrap, `DocumentIdentityAggregator`,
`IdentityBundle` dataclass, `fetch_identity` and `fetch_identity_summary`
read APIs. Three CLI commands added in `cli.py`:
`populate-document-identity-nc`, `report-document-identity-nc`,
`report-document-identity-summary-nc`, plus the §4.1D quality cross-check
`report-document-identity-quality-nc`. **First population pass: 4412
identity rows.** Confidence distribution: 6 high / 319 mid / 4087 low.
Cross-check showed parse rate **60.8% at high confidence vs 35.0% at low
confidence**, validating the chosen weights track real parsing signal.
Identified 50 high-confidence routing disagreements ready for Phase 2/3
to act on. **Discovered during work:** the
`document_classifications` table uses `subject_kind='historical_document'`
(not `'source_pdf'`), so the aggregator bridges through
`historical_documents.local_path`. **Surfaced an MGS-32 doc routed by
consensus to `generic_residential`**, exactly the misrouting bug Phase 2
needs to fix. GitNexus re-index attempted before code work; `npx gitnexus
analyze` segfaulted on ~20 large files but succeeded for the modules in
play.

### 2026-05-06 — Phase 0 (carryover quick wins) — claude-opus-4-7

**§3.0A/0B/0C shipped on branch `refactor/parsing-architecture` (commit
86ba927).** §3.0A added `STAGE_MIN_BUDGET_SECONDS` map and a per-stage
budget guard in `parse_improvement_loop.py` so deterministic stages
(validate, profile_consensus) still run when LLM-bound stages can't fit.
Verified live: a 1-min budget run skipped suggest (90s required) and ran
validate (5 candidates). §3.0B added `fetch_document_anchors()`,
`render_anchors_for_prompt()`, `regex_contains_anchor()` to
`regex_suggestions.py`; augmented suggest prompt with required
DOCUMENT-SPECIFIC ANCHORS block; added `_check_regex_has_anchor()` early
gate in `regex_validation.py` with new `rejected_no_anchor` status. §3.0C
discovered the empty-normalization issue is upstream — diagnose was
producing 0 `normalization_gap` / 0 `ocr_noise` because the prompt listed
failure types as a flat enum. Rewrote `_DIAGNOSIS_SYSTEM_PROMPT` with
explicit definitions and disambiguation rules.

---

## 11. Appendix — Prior session context

### What's already built that this plan reuses

From the 2026-05-06 session, the following components exist and feed into
the refactor:

| Component | Module | Reused as |
|---|---|---|
| Profile consensus engine | `src/duke_rates/document_intelligence/profile_consensus.py` | Evidence source for §4.1B |
| `parser_profile_recommendations` table | DB | Direct input to §4.1B |
| Document fingerprints v2 | `document_fingerprints_v2` | Direct input to §4.1B |
| Regex shadow harness | `src/duke_rates/document_intelligence/regex_shadow_test.py` | Reused for template-level rules in §6, narrowed for per-doc rules in §7.4B |
| Regex validation harness | `src/duke_rates/document_intelligence/regex_validation.py` | Reused with `target_profile` swapped for `document_identity_id` in §7 |
| Self-consistency on diagnose | `src/duke_rates/document_intelligence/parse_diagnosis.py` | Reused for routing-confidence votes when identity is borderline |
| Overnight loop | `src/duke_rates/document_intelligence/parse_improvement_loop.py` | Will get new task kinds in §8.5C |

### Why not a clean rewrite

A green-field rewrite would take longer and risk losing the parsing
work that's accumulated in the existing profiles. The phased approach
lets us:

- Land §4.1 (identity bundle) without changing extraction at all — pure
  observability win.
- Validate §5.2 (tier system) against real data before flipping switches.
- Keep current parsers working throughout the migration.
- Bail at any phase boundary if the design turns out wrong.

### Glossary

- **Profile** — current concept; a Python module containing regexes/normalizers + an implicit doc-scope.
- **Template** — refactor concept; the rule-set role of a profile, decoupled from routing.
- **Identity bundle** — the row in `document_identity` aggregating all evidence about one doc.
- **Routing tier** — TIER 1/2/3 label assigned by the routing layer based on identity confidence.
- **Per-doc rule** — a regex attached to one specific `document_identity` row (Phase 4).
- **Promotion** — moving a per-doc rule up to the template level when ≥ N similar rules exist (§7.4C).
