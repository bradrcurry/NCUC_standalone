# Document Identification — Research Direction

Branch: `feature/document-identification`
Started: 2026-05-21

## Goal

Improve document identification across all document types in the corpus (not just
rates / riders / tariffs), and produce gold-standard training data so a fast
classifier can be fine-tuned for downstream ML use.

This is exploratory. The deliverables below are sequenced from highest leverage
to lowest, but the work can branch off in any direction as findings surface.

## Current state (2026-05-21 survey)

The repo already has substantial classification infrastructure:

| Component | What it does | State |
|---|---|---|
| `document_types` table | Taxonomy of 12 terminal types across 6 categories | Stable |
| `document_classifications` table | Unified store: stage / label / confidence / evidence / alternatives per (subject, classifier) | 13,379 rows |
| `rule_document_type_v1` classifier | Keyword + title heuristics | Runs on every doc; **avg confidence 0.25, max 0.70** — never high-confidence |
| `embedding_knn_v1` classifier | KNN over Ollama embeddings (currently `qwen3-embedding:0.6b`) | 899 docs |
| `llm_qwen3:8b_v1` classifier | LLM second-opinion | 537 docs; **avg confidence 0.96** |
| Flag classifiers | Boolean signals (has_rate_tables, is_redline, is_proposed, ...) | One per doc per flag |

### Agreement landscape

Per-doc classifier agreement on the `document_type` stage:

```
classifiers=4  distinct_labels=2  →   1 doc
classifiers=4  distinct_labels=3  →   2 docs
classifiers=3  distinct_labels=1  →  33 docs   ← gold standard (all 3 agree)
classifiers=3  distinct_labels=2  → 210 docs
classifiers=3  distinct_labels=3  → 306 docs   ← total disagreement
classifiers=2  distinct_labels=1  → 311 docs   ← strong (2/2 agree)
classifiers=2  distinct_labels=2  →  36 docs
classifiers=1  distinct_labels=1  →  38 docs   ← rule-only, no second opinion
```

**Implication**: ~344 docs (33 + 311) are immediate gold-set candidates. ~306 are
hard cases worth labeling by hand. ~38 need an LLM/embedding pass.

### UNKNOWN bucket pattern

When the rule classifier returns UNKNOWN (145 docs), other classifiers usually
rescue it: 64 → TESTIMONY (embedding), 39 → APPLICATION (llm), 23 →
COMPLIANCE_FILING (llm), 18 → TESTIMONY (llm), 17 → ORDER_FINAL (embedding),
10 → TARIFF_SHEET (embedding). The rule classifier has systematic precision
gaps that the other two cover.

## Research direction

Four parallel work streams, ordered roughly by value.

### Stream A — Gold-set extraction and training data prep (highest leverage)

Turn the agreement landscape into labeled training data the rest of the work
depends on. Concrete deliverables:

1. **`audit-document-type-classifications-nc`** CLI: per-doc agreement report,
   confidence stats, classifier coverage gaps.
2. **`export-gold-set-nc`** CLI: writes a JSONL of high-agreement docs
   (label + text + layout features + provenance) suitable for `transformers`
   fine-tuning.
3. **`export-triage-queue-nc`** CLI: writes the 306 hard cases for
   single-page-app or notebook review with a label-fix UI.
4. Schema for tracking human-confirmed labels (`document_type_gold` table) so
   re-runs are idempotent and disagreement reviews accumulate over time.

### Stream B — Better rule classifier (close the obvious precision gap)

The rule classifier maxes at 0.70 confidence. Its gaps are the easiest wins:

1. **Layout signals** from Docling JSON: table density, signature-block presence,
   letterhead pattern, page count, font-size histogram, header/footer pattern.
   Most current rules only look at title + first-2000-char text sample.
2. **Negative signals** the rules don't use: "BEFORE THE NORTH CAROLINA
   UTILITIES COMMISSION" → ORDER_FINAL or ORDER_PROCEDURAL; "I, ___, do hereby
   certify" → CERTIFICATE_OF_SERVICE; "REDIRECT EXAMINATION" → TESTIMONY.
3. **Bump the confidence calibration**: currently max 0.70 because the rule
   scorer never reaches its theoretical max. Either lower the divisor, or
   distinguish "rule fired with strong signals" (e.g. 3+ keyword hits) from
   "rule guessed".

### Stream C — Taxonomy extension for non-utility document types

The current 12 types are utility-rate-focused. For "other document types" we
likely need to grow the taxonomy to cover:

- Federal filings (FERC orders, EIA reports)
- Press releases / news
- Legislative records (NCGA bills, hearing transcripts)
- Court filings (appeals, settlements)
- Internal Duke memoranda / strategy documents

This is a taxonomy design exercise that needs domain input. Specific question
for the user: **which non-utility document types are you anticipating ingesting?**

### Stream D — Fine-tune a fast classifier

Once Stream A produces ~1000+ gold-labeled docs, fine-tune a small model
(DistilBERT / TinyLLaMA / qwen-0.5b) on `(text, label)` pairs. Goal: replace
the LLM-second-opinion step (3+ sec per doc) with a sub-100ms inference path
that's at least as accurate. The 12-type taxonomy is small enough that
fine-tuning will work well with even 50 examples per class.

## Open questions (need user input)

1. **Taxonomy scope**: stay within the current 12 types, or grow to handle
   non-utility document types? (See Stream C for examples.)
2. **Labeling cadence**: bulk-export disagreement docs for a labeling pass
   (Notebook? Streamlit?), or hand-fix one-at-a-time as they surface?
3. **Training stack preference**: stick with Ollama / local llama.cpp for
   fine-tuning, or move to HuggingFace `transformers` + PyTorch for the small
   model?
4. **Storage**: the existing `document_classifications` table holds all
   classifier outputs but doesn't separate "human gold" from "machine guess".
   New `document_type_gold` table, or a `is_gold` boolean on the existing
   table? (Recommend separate table — gold rows are append-only and edit-
   audited, classifier rows are overwriteable.)

## First concrete deliverable (proposed)

`audit-document-type-classifications-nc` — a single CLI that:
- groups every NC historical_document by classifier agreement level,
- shows per-classifier confidence stats,
- flags coverage gaps (docs missing LLM or embedding),
- exports an optional JSONL gold-set candidate file with text + label + provenance.

This is one new file, ~150 lines, no schema changes, no model dependencies.
Output drives every other stream — gold-set for Stream A, hard-case labels
for Stream B improvements, training data for Stream D.

## 2026-05-21 progress (Stream B + taxonomy + storage)

User direction locked:
- **Taxonomy**: grow narrowly (added `FERC_ORDER` + `EIA_REPORT`).
- **Next focus**: Stream B (better rule classifier with layout signals).
- **Gold storage**: new `document_type_gold` table (append-only, edit-audited).

### Schema additions
- `document_types` seeded with two new terminal codes:
  - `FERC_ORDER` (category `ORDERS_AND_DECISIONS`)
  - `EIA_REPORT` (category `REPORTS_AND_COMPLIANCE`)
- New `document_type_gold` table for human-confirmed ground truth.
  Separate from `document_classifications` (machine outputs are
  overwriteable, gold rows are append-only with edit audit).

### rule_document_type_v2 shipped
`src/duke_rates/classification/rule_document_type_v2.py` — per-type
pattern classifier with first-class layout features.

Differences from v1:
- 14 type-specific pattern collections (strong / weak / negative) instead
  of two giant TARIFF/PROCEDURAL lists.
- Layout signals (page_count, text_chars, has_tables) from
  `document_fingerprints_v2` are first-class scoring inputs.
- Last-page text scanning for signature regions / certifications.
- Confidence calibration that reaches ≥0.92 on clear cases (vs v1's
  hard ceiling of 0.70).
- Emits all 14 type codes, not just the 5 v1 collapses to.

Initial corpus run (200-doc sample, dry-run, no persistence):

| Metric | v1 | v2 |
|---|---|---|
| docs at ≥0.9 confidence | 0 (corpus-wide) | 197 / 200 |
| docs at <0.5 confidence | most | 0 / 200 |
| emits which terminal types | 5 of 14 | all 14 (in tests) |

v2 disagrees with v1 on 127 / 200 docs. Spot-checks:
- v2 correctly fixes v1 errors like hd=14 "Joint Agency Adjustment Rider"
  (v1=ORDER_FINAL → v2=RIDER) and hd=10 Sykes Exhibit (v1=TESTIMONY →
  v2=RIDER, more accurate).
- v2 over-claims RIDER on base-schedule docs that list applicable riders
  in body (hd=3 "Large General Service", hd=4 "Medium General Service",
  hd=5 "Residential Service"). Needs:
  1. Stronger title-anchored TARIFF_SHEET patterns for "Residential
     Service" / "Large General Service" / "Medium General Service" /
     similar base-class names.
  2. RIDER's strong patterns to require "Rider" appears in title or
     first 200 chars (header region), not anywhere in body.

### Tests
`tests/test_rule_document_type_v2.py` — 13 tests covering each of the
14 types' canonical patterns + layout-signal tiebreaker + UNKNOWN floor
+ weak-only confidence band.

### CLI for iteration
`classify-documents-v2-nc` runs v2 against NC docs and either prints a
v1-vs-v2 comparison report or (with `--write-classifications`) persists
v2 results to `document_classifications` next to v1 so the multi-
classifier agreement vote can incorporate the v2 signal.

Recommended Stream B iteration cycle:
1. `classify-documents-v2-nc --limit 200` (dry-run review)
2. inspect disagreements; tune patterns in `rule_document_type_v2.py`
3. `pytest tests/test_rule_document_type_v2.py`
4. `classify-documents-v2-nc --write-classifications` once stable
5. `audit-document-type-classifications-nc` — gold-set candidates
   should grow as v2 votes are factored in.

## Open Stream B questions

1. **Pattern tuning**: should RIDER require "Rider" in title/header, or
   keep loose body matching with a stronger negative for base-class
   schedule titles? Recommend the latter — base-class titles ("Residential
   Service", "Large General Service") are easier to enumerate than every
   possible rider naming pattern.
2. **Confidence calibration**: 0.92 target for clear strong-signal cases
   currently sits below LLM (avg 0.96) — keep it lower so the LLM second
   opinion still wins on tie-breakers, or bump to 0.95+ when v2 has
   layout + multiple strong hits?
3. **Persistence threshold**: when `--write-classifications` runs, should
   it skip low-confidence (<0.5) results, or write them all so the audit
   tool can see v2's UNKNOWN calls explicitly?

### 2026-05-21 update — RIDER precision pass

Tightened RIDER's `Rider X` pattern to header-region-only matching
(first 120 chars of body + title). Body mentions count as weak, not
strong. Added base-class service titles ("Residential Service", "Large
General Service", "Medium General Service", "Small General Service") as
TARIFF_SHEET `strong_header` patterns + as RIDER negative patterns.

Same 200-doc sample result after fix:

| Metric | Pre-fix | Post-fix |
|---|---|---|
| v1-vs-v2 disagreements | 127 / 200 | 104 / 200 |
| TARIFF_SHEET label count | 128 | 155 |
| RIDER label count | 59 | 22 |
| High-confidence (>=0.9) | 197 | 193 |

The 23 disagreements that resolved were almost entirely base-schedule
docs (hd=3 LGS, hd=4 MGS, hd=5 RES, …) where v2 had previously
over-claimed RIDER due to body-mention of applicable riders.

### 2026-05-21 update — Stream A gold table seeded

`seed-document-type-gold-nc` CLI populates `document_type_gold` from
classifier-agreement signals. First run with `--execute` at the default
`--min-classifiers 2` against the current NC classification matrix:

| Bucket | Count |
|---|---|
| docs considered | 927 |
| seeded as gold | **344** |
| skipped (classifiers disagree) | 555 |
| skipped (too few classifiers ran) | 28 |
| skipped (already gold) | 0 (first run) |

Seeded label distribution:

| Label | Count |
|---|---|
| TARIFF_SHEET | 176 |
| ORDER_FINAL | 93 |
| TESTIMONY | 75 |

`--min-classifiers 3` (strict mode requiring rule+embedding+LLM
3-way agreement) yields 33 docs (15 TARIFF_SHEET / 12 TESTIMONY /
6 ORDER_FINAL) — the gold-of-gold tier suitable for held-out test
sets. Stored under the same `unanimous_classifier_agreement` source
label; `evidence_json` carries the actual `classifiers` array so
downstream tools can re-derive the tier without a separate column.

Notable absences from the gold set: RIDER, RATE_SCHEDULE,
COVER_LETTER, NOTICE_OF_HEARING, APPLICATION, COMPLIANCE_FILING,
CERTIFICATE_OF_SERVICE, FERC_ORDER, EIA_REPORT — these types had
either insufficient classifier coverage or classifier disagreement.
Stream D fine-tuning needs balanced classes; getting these types
into the gold set is the next concrete labeling target (Stream A
continuation: build `triage-disagreements-nc` to drive human review
on the 555 disagreement docs, weighted toward under-represented
type buckets).

### 2026-05-21 update — v2 backfill + gold re-seed

Ran `classify-documents-v2-nc --write-classifications` corpus-wide.
927 v2 classifications persisted alongside v1/embedding/LLM. Result:

v2 confidence distribution:
- 670 docs at ≥0.9 (vs v1's **0**)
- 135 docs at 0.5-0.9
- 122 docs at <0.5

v2 label distribution (much richer than v1's 5-type collapse):

| Label | v1 | v2 |
|---|---|---|
| TARIFF_SHEET | 668 | 400 |
| ORDER_FINAL | 536 | 186 |
| RIDER | 38 | 121 |
| COVER_LETTER | 161 | 89 |
| TESTIMONY | 478 | 33 |
| APPLICATION | 145 | 31 |
| RATE_SCHEDULE | 16 | 23 |
| CERTIFICATE_OF_SERVICE | 43 | 18 |
| ORDER_PROCEDURAL | 25 | 17 |
| COMPLIANCE_FILING | 123 | 8 |
| NOTICE_OF_HEARING | 13 | 1 |

v2 produces fewer of every type because its higher confidence threshold
and per-type patterns push borderline cases into the explicit
mid-confidence band rather than guessing.

Re-seeded gold table with v2 in the vote: only **1 new row added**
(a COVER_LETTER that v2 broke the tie on). 344 - 207 = **137 prior
v0 gold rows are now stale** (v2 disagrees with the previous unanimous
2-way agreement). Those rows aren't deleted — they remain in the gold
table as point-in-time records but may need human re-review.

Gold table state: 345 rows (344 v0 + 1 v1 from re-seed).

### Stream A continued — triage-disagreements-nc

`triage-disagreements-nc` exports classifier-disagreement docs as a
labeling JSONL queue. Each row carries:

- `hd_id`, `priority`, `family_key`, `title`
- `votes`: per-classifier `{classifier, label, confidence}` array
- `labels_voted`: distinct labels across classifiers
- `majority_label`: most common vote (or first-tied)
- `text_sample`: 2000 chars from the bulk extractor's text path
- `text_source`: which text source the sample came from

Priority weighting (default on): each disagreement doc gets a priority
score equal to the **average** of underrepresented-bucket weights
across its classifier votes. Labels not in gold weight 100;
high-coverage labels (TARIFF_SHEET=176 in gold) weight ~0.6. The
average (not sum) so a doc whose votes are *all* rare ranks above a
doc with one rare + several common votes.

Run with `--label COVER_LETTER --label RIDER` to focus the queue on
specific under-represented types. Output is consumed by a notebook /
Streamlit labeling UI; confirmed labels write back to
`document_type_gold` with `source='human_review'`.

Tests: 5 in `tests/test_triage_disagreements.py` covering the agree-vs-
disagree filter, priority order, label filter, no-weight tiebreaker,
and the row schema.

### 2026-05-21 update — high-confidence subset promotion

`promote-high-confidence-subset-nc` grows gold from disagreement docs
where a subset of classifiers agree at high confidence even when other
classifiers vote differently at lower confidence.

Rule: a doc promotes when N classifiers all vote the same label at
≥ min_confidence, AND no other label has an equally large high-confidence
subset. The lower-confidence dissenting votes are recorded as
`dissenters` in `evidence_json` but don't block promotion.

First corpus run (default `--min-confidence 0.9 --min-subset 2`):

| Bucket | Count |
|---|---|
| docs considered | 927 |
| skipped (already gold) | 345 |
| no qualifying subset | 486 |
| high-conf disagreement | 0 |
| **promoted** | **96** |

Label distribution of promoted rows:

| Label | New rows |
|---|---|
| TARIFF_SHEET | 54 |
| TESTIMONY | 16 |
| APPLICATION | 7 |
| ORDER_FINAL | 7 |
| CERTIFICATE_OF_SERVICE | 3 |
| RIDER | 3 |
| COMPLIANCE_FILING | 2 |
| ORDER_PROCEDURAL | 2 |
| COVER_LETTER | 1 |
| RATE_SCHEDULE | 1 |

**6 of 9 previously-empty type buckets filled** in one pass:
RIDER, APPLICATION, CERT_OF_SERVICE, COMPLIANCE_FILING,
ORDER_PROCEDURAL, RATE_SCHEDULE.

Gold table state: **441 rows** total, 10 of 14 types represented.

Still empty: NOTICE_OF_HEARING (only 1 v2 vote corpus-wide),
FERC_ORDER, EIA_REPORT (no docs of these types in NC corpus).

### Tests
`tests/test_promote_high_confidence_subset.py` — 6 tests covering the
subset-size rule, min_confidence threshold, high-conf disagreement
skip, already-gold skip, and dry-run no-write.

### 2026-05-21 update — Stream D baseline shipped

First Stream D deliverable: `train-document-type-baseline-nc` fits a
TF-IDF + LogisticRegression baseline on the 441-row gold set. This is
the intentionally-minimal first cut — its purpose is to put a number
on the board that any later fine-tuned model (DistilBERT, qwen-fine-
tuned) must beat to justify the added complexity.

#### Architecture

- `duke_rates.classification.baseline_classifier` module — pure-Python,
  no DB access. Takes a `TrainingDataset(hd_ids, labels, texts)` and
  returns a `TrainingResult` with the fitted vectorizer + model + per-
  class metrics.
- TF-IDF over the first 2000 chars of doc text. n-grams (1, 2), min_df=2,
  max_df=0.95, max_features=20k.
- `LogisticRegression(class_weight='balanced', solver='lbfgs',
  max_iter=2000)`. Multinomial logistic over the 10 classes that have
  any gold examples.
- Stratified train/val split for classes with ≥5 gold samples; rare
  classes (4 or fewer samples) are pinned to train-only. Per-class
  metrics report n=0 for the rare classes — they need more gold before
  they can be evaluated.

#### Results (first run, 2026-05-21)

```
rows used:                 441  (0 skipped no-text)
train rows:                355
val rows:                   86
classes:                    10
train-only (rare):  ['CERTIFICATE_OF_SERVICE', 'COMPLIANCE_FILING',
                     'COVER_LETTER', 'ORDER_PROCEDURAL',
                     'RATE_SCHEDULE', 'RIDER']
val accuracy:             0.884
train accuracy (ref):     0.966
```

Per-class on the validation split:

| Class | P | R | F1 | n |
|---|---|---|---|---|
| TARIFF_SHEET | 0.98 | 0.98 | 0.98 | 46 |
| TESTIMONY | 0.89 | 0.89 | 0.89 | 18 |
| ORDER_FINAL | 0.83 | 0.75 | 0.79 | 20 |
| APPLICATION | 0.00 | 0.00 | 0.00 | 2 |
| **weighted avg** | **0.90** | **0.88** | **0.89** | 86 |
| macro avg | 0.54 | 0.52 | 0.53 | 86 |

The macro F1 of 0.53 reflects rare-class undersampling, not classifier
weakness. The 0.88 val accuracy + 0.89 weighted F1 is the number to
beat. Train accuracy 0.966 with val 0.884 indicates modest overfitting
— acceptable for a baseline; n-gram pruning or stronger regularization
can shave the gap when needed.

#### Artifact

`models/baseline_document_type_v0.joblib` (gitignored; regenerate via
the CLI). Contains: `{vectorizer, model, classes, metrics}`. Loadable
in any sklearn-compatible environment for offline evaluation.

#### Stream D next iterations (priority-ordered)

1. **Grow gold to ≥20 samples per class.** APPLICATION, RIDER,
   CERTIFICATE_OF_SERVICE, COMPLIANCE_FILING, ORDER_PROCEDURAL,
   RATE_SCHEDULE, COVER_LETTER all currently have 1–7 gold rows.
   Triage queue's underrepresented-bucket weighting is the right
   feeder for this.
2. **5-fold CV on the current gold set** as a more honest accuracy
   number (single 80/20 split has high variance at n=441).
3. **DistilBERT or qwen-finetuned alternative** once gold set hits
   ~50/class. The baseline number above lets us say "beat 0.89
   weighted F1" rather than "make it work."
4. **Confusion-matrix-driven pattern tuning**: feed v2's misclassif-
   ication patterns back into `rule_document_type_v2`. ORDER_FINAL
   recall of 0.75 suggests it's losing ~5 val docs to a competitor
   — likely APPLICATION or COMPLIANCE_FILING.

### Cover-letter bundle signal (intentional)

Docs whose `family_key` says tariff/rider but whose body starts with a
cover-letter pattern (`VIA ELECTRONIC FILING`, "Jack Jirak / Molly
Jagannathan / etc., Deputy General Counsel ...") classify as
COVER_LETTER even though their family_key says they should be the
schedule. This is **intended**: the v2 classifier reads the doc's
*content*, not its `family_key` label. When the two disagree, it's a
high-signal hint that the bundle metadata is wrong — the PDF wraps a
transmittal letter that the importer mistakenly tagged with the
schedule family-key.

Examples surfaced on the 200-doc sample:
- hd=176 family_key=nc-progress-leaf-572 ("Street Lighting Service"),
  but body starts with cover letter + accounting tables.
- hd=20 family_key=nc-progress-leaf-606 ("Demand Side Management
  Rider"), but body starts with a Troutman Sanders cover letter.

Future audit: cross-reference `v2_label='COVER_LETTER'` with
`family_key LIKE 'nc-progress-leaf-%'` to surface these mismatches
as a labeling/import-cleanup queue.

## Non-goals (for this branch)

- Do not change `document_types` taxonomy until Stream C scope is settled.
- Do not delete existing classification rows; treat them as input to a
  gold-set pipeline.
- Do not block Stream A on Stream C — even within the current 12 types,
  the gold-set extraction provides immediate value.
