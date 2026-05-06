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

- [ ] **Phase 0 — Quick wins from prior session** (carryover, not refactor)
  - [ ] 0A. Deadline-cutoff fix in overnight wrapper
  - [ ] 0B. Anti-greedy suggest prompt with profile-specific anchors
  - [ ] 0C. Investigate why normalization suggestions are always 0
- [ ] **Phase 1 — Document Identity Layer** (foundation, zero behavior change)
  - [ ] 1A. `document_identity` table + DDL
  - [ ] 1B. Aggregator that populates from existing tables
  - [ ] 1C. CLI to inspect identity bundles
  - [ ] 1D. Identity-quality report
- [ ] **Phase 2 — Routing Tier System** (decision layer, no extraction change)
  - [ ] 2A. Tier classifier
  - [ ] 2B. Tier-prediction validation against existing parses
  - [ ] 2C. Tier dashboard
- [ ] **Phase 3 — Profile-as-Template binding** (Tier 1 active routing)
  - [ ] 3A. Profile → template rename / re-doc
  - [ ] 3B. Tier 1 binder (auto-bind high-confidence docs)
  - [ ] 3C. Compare binder output vs current parser routing
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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 1C. CLI to inspect identity bundles

`report-document-identity-nc --source-pdf PATH` prints the identity bundle
for one doc (all evidence sources, all signals, score breakdown). Used for
debugging and for tuning the confidence weights in 1B.

`report-document-identity-summary-nc` prints distributions: confidence
histogram, top schedule codes by frequency, docs with low confidence (worth
human review).

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 2B. Tier-prediction validation

Cross-check predicted tier against actual parse outcome for currently-parsed
docs:

- Tier 1 docs should mostly have `parse_attempt_logs.status = 'parsed'` with
  `charge_count > 0`. If a Tier 1 doc failed extraction, that's a *template*
  problem, not a routing problem — flag it as a high-priority parser fix.
- Tier 3 docs should mostly have `wrong_profile` / `unknown` diagnoses. If a
  Tier 3 doc parsed successfully, the tier cutoffs may be too strict.

Output: a confusion-matrix-like report.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 2C. Tier dashboard

CLI command `report-routing-tiers-nc` showing tier distributions, top
profile_consensus_top values per tier, sample docs per tier.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 3B. Tier 1 binder

For each Tier 1 doc, look up the consensus profile, run the template's
regexes, persist charges. If extraction succeeds, mark `parsed`. If it
fails, flag as template-fix-needed (Tier 1 docs failing extraction is a
strong signal that the template needs work).

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

### 3C. Comparison run

Run the binder in dry-run mode against the existing parsed corpus.
Compare Tier 1 binder output against current parser_profile assignments.
Disagreement counts get bucketed by reason (different profile, no profile
in current state, etc.).

If the disagreement rate is < 5%, flip Tier 1 on. If it's higher, audit
the disagreements before proceeding.

| Status | pending |
|---|---|
| Owner | unassigned |
| Started | — |
| Completed | — |
| PR / commit | — |

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

*(empty — no work has been done yet against this plan)*

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
