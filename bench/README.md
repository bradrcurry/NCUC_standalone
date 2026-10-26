# Extraction benchmark v0

This benchmark is a fixed corpus of 26 Duke tariff documents with ground truth verified by hand. It also includes harnesses that score any code tree's extraction pipeline against that corpus. Nothing here writes to the production database.

## Contents

| Path | What it is |
|---|---|
| `corpus.yaml` | Per document: id, category (A–J, see audit §13), source path, SHA-256, transcribed pages, `expected_zero` |
| `expected/<doc_id>.json` | Expected charges and printed TOTAL checksums, transcribed from the source PDF (text layer plus rendered page images). Never copied from the DB or any parser. `_SUMMARY.md` gives the conventions and per-document notes. |
| `pdfs/` | Copies of source PDFs under 3 MB. Larger ones are referenced by `source_path`. |
| `run_extraction.py` | Runs `BulkExtractor.extract_charges_from_document` from the code tree given by `--src`, over the corpus. It writes JSON. Its classification recorders are disabled, and it refuses the production DB. |
| `score.py` | Scores one or more runs. Reports value, unit, rate-class and effective-date accuracy, recall/precision, false positives on expected-zero documents, TOTAL reconciliation, snippet evidence, and time. |
| `rider_summary_reparse.py` | Re-parses every rider-summary source PDF with the parser at a git ref and with the working tree, using both pdfplumber and PyMuPDF text. Reports merges, footer leaks, unit swaps and checksum pass rate. |
| `docling_page_miner_coverage.py` | D1 check: the share of Docling table-cell decimals that appear in the mined page text. |
| `billing_check.py` | Runs the 12-bill DEP reconciliation against a scratch DB, using a given code tree. |

## Reproducing the Phase 0 numbers

```bash
SP=<scratch dir>
cp data/db/duke_rates.db.bak_pre_phase0_20261026 $SP/bench.db        # scratch copy, never production
git worktree add --detach $SP/baseline_tree 8b77aa4                   # pre-repair code
python bench/run_extraction.py --src $SP/baseline_tree/src --db $SP/bench.db --label baseline --out reports/phase0/bench_baseline.json
python bench/run_extraction.py --src src --db $SP/bench.db --label repaired --out reports/phase0/bench_repaired.json
python bench/score.py reports/phase0/bench_baseline.json reports/phase0/bench_repaired.json --out reports/phase0/bench_scores
python bench/rider_summary_reparse.py --db $SP/bench.db --baseline-ref 8c092b5 --out reports/phase0/rider_reparse
python bench/docling_page_miner_coverage.py --db $SP/bench.db --baseline-src $SP/baseline_tree/src
```

The outputs are deterministic for a fixed DB snapshot: no LLM calls, and no Docling conversion (cached artifacts only).

## Known limitations of v0

- **Line-level effective dates are not scored.** Profiles do not emit them. The document effective date is scored from the ingest heuristic.
- **HP "Incremental" values are not scored.** Rider-summary profiles do not emit the HP Incremental ¢/kWh column as charges. Those expected records count as misses in both runs.
- **"Source evidence" only checks for a snippet.** Profiles store the label as the snippet, so the share of snippets that contain the value is about 3%. Real provenance (page, quote, bbox) needs the persistence redesign (`docs/design/2026-10-26_non_destructive_extraction_proposal.md`).
- **Redline flags are forced off.** The production pipeline would skip whole-document redlines such as blk13; the benchmark extracts them anyway.
- **No large-value test cases.** No corpus document contains a value of 50 or more that begins with 5, so the `normalize_ocr_text` fix (`$55.00` → `$$5.00`) is covered only by unit tests.
