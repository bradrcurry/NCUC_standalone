# Phase 2E D3: Live Discovery Validation, Ingestion Safety, Verified Document Binding

**Date:** 2026-10-28
**Branch:** `phase2e/d3-live-discovery-binding`, cut from `phase2e/d2-portal-orders-promotion` @ `e9e6b9e`.
**Basis:** D2 report §11–§12, D1 report §8, the D3 brief.

> Sequence note: the acceptance criteria below are the brief's §8 criteria, made measurable. They were
> applied from the start, but this file was written mid-session, after the live pilot had run (the
> pilot was the first work item because its results decided the discovery changes).

## 0. Baseline (measured before any change)

| Fact | Value |
|---|---|
| Production SHA-256 | `fe563a78…cd08`, mtime 2026-10-26 12:36:11, `-wal` 0 bytes (`reports/phase2e_d3/production_sha_before.txt`) |
| Scratch | `d3.db` = byte copy of D2's `d2.db` (production + D1/D2 ledger, provenance v4) |
| D2 network plan | 290 queries (103 `portal_orders`) over 249 `searched_not_found` gaps; no live request ever made |
| `pv.promote` impact (GitNexus) | **CRITICAL**: 3 direct callers (provenance CLI, `ncuc_loader._replace_through_runs`, `BulkExtractor._legacy_promote`), flows `mine_tariff_sheets_nc`, `ingest_ncuc`, `mine_docling_nc` |
| Verified gaps (D1) | 16 gaps / 18 candidates `verified` (assistant page-image review, not human) |
| Date conflicts (D2) | 35 |
| Local candidates on redline pages (D2) | 45 |

## 1. Work items

| # | Item | Approach |
|---|---|---|
| L | Live pilot | ≤10 read-only queries through the existing authenticated integration, chosen across docket types; every attempt ledgered; real pages captured as fixtures; downloaded candidates read against their own pages. |
| V | Shared ingestion validation | One module (`ingestion_validation`) called **inside** `provenance.promote` for tariff-version and rider runs, so no caller can skip it. Unattended pathways fail closed; only the reviewer pathway accepts named overrides; every call recorded (`promotion_validations`). |
| B | Verified document binding | `bind_unbound_document` proposal kind on the D2 lifecycle. Mode decided by checks (attach / replace_redline_source / create_version); no charge created or deleted; new versions are not billable until charges are promoted. |
| Q | Review queue | `evidence_review_items` + CLI; items from the source audit, date conflicts, verified unbound documents, redline candidates, page-reviewed findings. |
| E | Date conflicts | Rule classification of all 35 by cause from the sheet's own pages; corrections only proposed. |
| D | Discovery improvements | Only those the pilot's results support. |

## 2. Acceptance criteria

| # | Criterion | Target |
|---|---|---|
| L1 | Live searches | ≤ 10 distinct queries; each has a `search_attempts` row with params, stop reason, pages reached/total, result count |
| L2 | Access control | No retry loop, no challenge bypass; stop at the first block or interactive login |
| L3 | Verification | Every downloaded candidate checked against its own pages; retrieval reported separately from relevance |
| V1 | Pathways | Every caller of `pv.promote` and every direct `tariff_charges` writer assessed |
| V2 | Contamination | Tests show a redline / redline-duplicate / wrong-date / changed-bytes run cannot reach `tariff_charges` or rider tables through bulk legacy replace, the loader, a direct call, or the CLI without a recorded override |
| V3 | Compatibility | A clean source promotes as before; the existing promotion test suites pass |
| B1 | Rehearsal | ≥ 3 representative verified candidates bound on scratch; approval required; 0 charges deleted |
| B2 | Rollback | Rollback restores every touched table byte-identically |
| B3 | Gap status | `resolved` only after an applied, read-back binding |
| E1 | Conflicts | All 35 classified or explicitly left unresolved, with page evidence; no date written |
| T1 | Tests | New tests pass; full suite passes |
| T2 | Benchmark | 26 documents × 6 configurations; every difference from D2 explained |
| P1 | Production | SHA-256 unchanged |
