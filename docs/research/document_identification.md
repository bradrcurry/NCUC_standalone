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

## Non-goals (for this branch)

- Do not change `document_types` taxonomy until Stream C scope is settled.
- Do not delete existing classification rows; treat them as input to a
  gold-set pipeline.
- Do not block Stream A on Stream C — even within the current 12 types,
  the gold-set extraction provides immediate value.
