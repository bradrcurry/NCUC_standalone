# NCUC Historical Tariff Recovery Pipeline

Operational usage guidance for humans and other AI agents lives in
[document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md).
Use that guide first if the goal is to operate the pipeline rather than redesign it.

## Context

We are building a Python system to recover historical versions of electric utility tariff sheets filed by Duke Energy Progress (DEP) with the North Carolina Utilities Commission (NCUC). DEP files tariff sheets as PDFs with the NCUC; each sheet covers a specific rate schedule or rider and is identified by a **leaf number** (e.g., "Leaf No. 602") in the 500–802 range for DEP's North Carolina tariffs.

### Goal

For each of 111 tariff families (e.g., `nc-progress-leaf-602` = "Joint Agency Asset Rider JAA"), we want to build a time series of historical versions — ideally one PDF per rate case filing showing what the tariff looked like at that point in time. Currently, most families have only 1 version (the current tariff sheet downloaded directly from Duke's website). We are trying to recover older versions from NCUC regulatory docket filings.

---

## Pipeline Overview

### Step 1: Discovery via NCUC Authenticated Portal

We authenticate with the NCUC's `starw1.ncuc.gov` portal using Playwright (headless Chrome with saved credentials). We run parametric searches against `PSCDocumentDetailsPageNCUC.aspx` to find filings in relevant dockets (primarily `E-2 Sub 1000+` series, which cover DEP rate cases). Results are stored as `NcucDiscoveryRecord` objects in SQLite.

### Step 2: Download

Each discovery record has a `ViewFile.aspx?Id=GUID` URL. We use the authenticated Playwright session with `page.expect_download()` to capture the PDF bytes and save locally. PDFs range from small single-leaf filings (30KB) to large multi-leaf compliance tariff books (18MB, 159 pages).

### Step 3: Content Mining And Triage

We use a staged triage and mining flow rather than treating every PDF the same.

Stage A uses fast document triage to decide whether a file should stay on the
native-text path, be segmented as a large tariff book, or be routed toward OCR.

Current triage signals include:
- sampled text density
- likely scanned / OCR-required status
- tariff vs procedural vocabulary
- structural complexity
- `gpu_ocr_candidate` for especially difficult OCR cases

After triage, the miner uses `pdfplumber` to extract text from each PDF or span.
It looks for:

- **Leaf number patterns**: regex `Leaf\s+No\.?\s+(\d{3,4})` — e.g., "Leaf No. 602"
- **Schedule/rider codes**: regex patterns for uppercase codes like `RIDER JAA`, `SCHEDULE RES`, etc.
- Whether the PDF looks like a tariff exhibit (contains terms like "AVAILABILITY", "RATE", "RIDER", "per kWh") vs. a procedural filing (motion, order, brief)

Results (extracted leaf nos, schedule codes, whether it contains tariff text) are stored back to the discovery record's `referenced_leaf_nos_json` and `referenced_schedule_codes_json` columns.

### Step 4: Family Matching (Exhibit Selection)

For each tariff family, we run an exhibit selector that scans all downloaded discovery records and scores them. A record matches a family if any of these are true:

- Its `referenced_leaf_nos` contains the target leaf number
- Its `referenced_schedule_codes` contains the target schedule code
- The mined metadata extracted the leaf/code
- The docket number is in a known set of related dockets for that family
- The family key appears in the record's pre-assigned `family_keys`

Matched records are scored (0–100) based on: title keywords (e.g., "compliance tariff" +5, "DEP" +3), whether the leaf code appears in context, filing type classification (tariff_sheet vs. application vs. order), and text profile matching (per-family keyword include/exclude lists). Top-scoring matches above a threshold are imported as `historical_document` records.

Company normalization is now explicit. Literal filing titles and paths are used
to distinguish Progress-family vs Carolinas / Duke Power filings before family
matching, which prevents cross-company contamination.
When filing metadata is ambiguous or mislabeled, importer-side company inference
 can now also use mined page text/header evidence before choosing the company
 family namespace.

### Step 5: Version Creation And Extraction

Each imported `historical_document` gets a `tariff_versions` row linking it to
the family. Effective dates come from parsing bounded page-span text rather than
entire filing text where possible.

Charge extraction is now moving toward parser profiles:
- generic residential fallback
- company / family / format-specific historical profiles
- current dedicated profiles include:
  - `progress_residential_tou`
  - `progress_residential_flat`
  - `progress_billing_adjustments`
  - `progress_rider_adjustment_matrix`
  - `progress_demand_response_automation`
  - `carolinas_residential_flat`
  - `carolinas_rider_adjustment_matrix`
  - `carolinas_lighting_schedule`
  - `carolinas_small_customer_generator`

This is intended to stop the historical extractor from becoming one large shared
regex file.

Separately, the older NCUC ingest path still uses JSON as an intermediate
handoff between parsing and SQLite loading. That remains useful for audit/debug,
but it is no longer the desired long-term default architecture.

---

## Current Limitations / Problems

### 1. OCR and Docling paths integrated
Some older filings are scanned images; triage can identify them and score their
OCR likelihood. OCR work is queued with persistent sidecar artifacts.

Docling is now optional backend for:
- OCR-heavy scans
- table-heavy rider summaries and compliance books
- weak or empty historical parses
- future LLM-assisted explanation/relationship work

Docling artifacts (JSON, text, tables) are cached by file hash and backend version,
separate from plain OCR sidecars so outputs are reused without reconversion.

The bridge reconstructs `PageEvidence` from stored Docling JSON without re-running
Docling, preserving the native-text and OCR paths as the fast default. Docling
outputs feed into the existing family matching, segmentation, and parser profiles
using `mine-docling-nc` selective operator command.

### 2. Multi-leaf compliance books
Many filings are large PDF "books" containing dozens of tariff sheets (e.g., a full rate case compliance tariff with 50+ leaves). The miner extracts *all* leaf numbers from the whole document, but the exhibit selector then matches the entire PDF to *each* leaf — even when only a few pages are relevant to a given family.

### 3. Leaf numbers absent from many filings
Older Duke Power era filings (pre-2005) use old leaf numbering (1–150 range, not 500+ range). DEP-branded compliance filings post-2012 use the 500+ numbering. Many filings in between have no leaf numbers at all — they reference schedules by name only (e.g., "Schedule RES-79") without a numeric leaf identifier.

### 4. Schedule code ambiguity
Short codes like "BA", "JAA", "EE" appear in many unrelated documents (as part of NCUC case numbers, party names, etc.). False positives are common.

### 5. Span-aware storage and stale-stage repair are now working together
Large PDFs are now segmented into bounded `TariffSpan` objects, which materially
improves family matching and downstream provenance. Stale-stage reparsing now
also refreshes missing/outdated page and span artifacts before re-extraction,
so stale historical documents can actually be cleared instead of cycling
through repeated extraction-only reruns.

### 6. Targeted reprocessing exists, and parser-profile dependency mapping now has a first pass
Weak historical parses can now be queued and reparsed selectively instead of
running broad full-document sweeps. Cached page and span artifacts now exist,
and parser-profile impact rules can now target both known family/profile
clusters and documents whose latest selector diagnostics indicate they were
close to using a changed profile. The remaining gap is deeper dependency
coverage so routing/scoring changes can be propagated without manually encoding
every affected cluster.
Recent matching hardening also improved heading-based family selection for
descriptive rider titles, so headings like `FUEL COST ADJUSTMENT RIDER` can
bind to abbreviated family codes such as `FUELCOSTADJRDR`.
That matching now also tolerates inline/merged PDF text forms such as
`FUEL COST ADJUSTMENT RIDER (NC) APPLICABILITY ...`, and importer reruns are
more idempotent when a corrected family mapping points back to an already-known
archived URL.
More recent Carolinas-focused hardening also fixed mixed cover-letter plus
tariff-sheet attachments: revised leaf pages are now classified as `tariff`
based on structural signals such as leaf/revised headers, and family matching
can seed span matching from filing-level hints like `filing_title`,
selected/derived mined titles, and referenced rider codes. That recovered
additional live Carolinas imports such as `Rider EE` and `Rider SCG` without
requiring broad rescans.
The latest Carolinas family-modeling pass also added long-form rider aliases
for cases like `EDPR` and `BPM`, and tightened short-code alias matching so
codes such as `PM` no longer match accidentally inside `BPM Rider`. Combined
with the stricter cover-letter segmentation, that improved live recovery for
`EDPR` and `BPMPPTTRUEUP` while eliminating known stale false positives.
The importer can also now preserve strong unmatched historical tariff spans by
creating provisional historical family rows when no curated family exists yet.
That path is intentionally narrow and is meant for cases like
`SMART ENERGY NOW PROGRAM (NC)`, where the document is clearly tariff-like and
historically relevant but absent from the current curated family catalog.
Because descriptive heading mining now recognizes `PROGRAM` headings, those
documents can be retained and revisited later instead of disappearing from the
historical archive.
Those provisional families are now surfaced through explicit review tooling:
- `list-provisional-families`
- `promote-provisional-family`
- `list-historical-only-families`
- `attach-current-document-to-family`

Recent Progress rider hardening also added a dedicated
`progress_single_value_rider` profile for one-value rider leaves such as
`nc-progress-leaf-608`, `609`, and `610`. Those leaves now bypass the older
`order` skip path, parse as bounded tariff sheets, and are treated as strong
outcomes when they correctly produce a single adjustment charge rather than
being misclassified as weak merely because the charge set is intentionally
sparse.
The importer also now rejects long low-signal spans before family assignment or
provisional-family creation when they lack leaf numbers and schedule/rider
markers. That guardrail was added after live testing showed that broad rate
design study reports could be re-imported as tariff documents simply because
they mentioned topics such as hourly pricing.

That review loop is already being used in practice; the live
`SMART ENERGY NOW PROGRAM (NC)` family has been promoted out of provisional
status after validation, and it now appears through the historical-only family
review surface because it still has no current-document anchor. Historical-only
family review now also includes suggested current-document candidates derived
from existing `documents` rows, and candidate strength now also consults
first-page mined current-document evidence such as extracted headings and leaf
numbers when metadata alone is too weak. When the historical family already has
known leaf continuity, that evidence is now used to suppress otherwise-plausible
but wrong-leaf candidate programs or riders. That gives operators a narrower
review path before doing broader manual searches. If a suggestion is confirmed,
the family can now be explicitly linked to that `documents.id` without direct
DB edits.
The historical-only review surface now also exposes a simple status split:
- `review_candidates` when at least one plausible current anchor exists
- `unresolved` when no plausible current anchor is currently suggested

That makes it easier to separate “needs attachment review” from “needs deeper
manual research or new family/catalog work.”
We also now have a targeted historical-family mismatch audit for already-linked
historical documents. That audit compares bounded PDF text against the assigned
family's expected company and schedule code so legacy misattachments can be
purged as contamination rather than mistaken for parser weakness. It was used
to remove five clearly wrong `nc-progress-leaf-500` mappings that were actually
Carolinas / Duke Power `RE`, `RT`, `RST`, and `RIDER PMLC` documents.
The same audit now normalizes rider/program code variants such as
`RIDER_US_RY1`, `RIDER NFS-14`, and `RIDER PS`, which made it practical to use
the audit on rider-heavy families like `leaf-649` and `leaf-674` without
over-purging legitimate Progress rider leaves.
The same audit now also covers rider-summary lineage mistakes. In practice,
that means stale rows where a `SUMMARY OF RIDER ADJUSTMENTS` sheet was stored
under `nc-progress-leaf-601` can be detected, purged, re-mined, and then
re-extracted under `nc-progress-leaf-600` with
`progress_rider_adjustment_matrix` instead of being mistaken for a Rider BA
parser failure.
The latest backlog review also exposed a distinct operator queue for weak
historical rows that still point at whole PDFs instead of bounded page spans.
Those rows now have a dedicated review surface:
- `python -m duke_rates list-weak-unbounded-historical-nc`

That command classifies each row into a simple next action:
- `add_profile_or_current_parser_bridge`
- `remine_from_discovery_record`
- `retire_legacy_raw_attachment`
- `retire_bundle_reference_residue`
- `manual_lineage_review`

Legacy raw rows in that queue can now also infer a `discovery_record_id` from
their stored regulator `local_file` metadata, which turns a meaningful share
of the old whole-PDF backlog into an actual remine queue instead of leaving it
as opaque manual review.

A second operator surface now lists the subset of those legacy raw rows that
already have bounded same-family regulator replacements:
- `python -m duke_rates list-redundant-legacy-raw-historical-nc`

That makes it possible to retire obsolete whole-PDF residue after successful
remine work instead of leaving duplicate weak rows in the backlog.

A third operator surface now isolates the harder bundle leftovers:
- `python -m duke_rates list-bundle-reference-legacy-raw-historical-nc`

That report is for cases like discovery `1124`, where old raw rider
attachments survive only because their leaf numbers appear inside rider
application tables on already-bounded host schedules. Those rows should be
retired, not re-mined or given new parsers.

This makes it easier to separate true parser gaps on current-style PDFs from
older legacy raw attachments that should be re-mined, migrated, or retired
before more extraction rules are written.
That queue has already been used to peel off a meaningful current-PDF cohort
from `generic_residential` into dedicated profiles:
- `progress_current_leaf_bridge`
  - current DEP leafs `501`, `520`, `535`, and `674`
- `progress_specialty_rider`
  - current DEP specialty riders `654`, `655`, `668`, and `670`
- `carolinas_current_leaf_bridge`
  - current DEC `nc-carolinas-schedule-HLF`
- `carolinas_solar_choice_rider`
  - current DEC `nc-carolinas-rider-NMB` and `nc-carolinas-rider-NSC`

After those reruns, the weak unbounded DEP backlog became a legacy
raw-attachment problem rather than a current-PDF parser-coverage problem.
The remaining `1124` bundle residue (`602`, `605`, `610`, `718`) has now also
been retired, so the Progress weak-unbounded legacy queue is currently clear.
The stale-stage queue has also been driven back to `0`, and the Carolinas
weak-unbounded current-PDF queue is now down to one remaining row:
`nc-carolinas-schedule-PP`.

That last Carolinas row has now also been repaired through the new
`repair-historical-current-snapshot` path, so both the Progress and Carolinas
weak-unbounded queues are currently `0`.

The review backlog is also now measured more honestly:
- `parse-review-queue` and `parse-review-summary` now count only the latest
  parse attempt per source/page/stage
- `reconcile-skipped-parse-reviews` can promote old skipped rows from stale
  `needs_review` status into accepted rule outcomes
- targeted family cleanup now also applies the requested family/profile/source
  filter before truncating to the requeue limit, which makes narrow cleanup
  passes reliable
- recent live cleanup cycles also:
  - moved `nc-progress-leaf-672` into `skipped_formula` for real `Rider CEI`
    formula-only sheets
  - moved Carolinas `PL` / `FL` lighting schedules into
    `carolinas_lighting_schedule`
  - moved true `nc-carolinas-rider-SCG` sheets into
    `carolinas_small_customer_generator`
  - moved SCG continuation/incidental-reference pages into `skipped_reference`
- widened formula-only skips for:
  - `nc-progress-leaf-712`
  - `nc-progress-leaf-721`
  - `nc-progress-leaf-723`
  - `nc-progress-leaf-640`
  - `nc-progress-leaf-663`
- added dedicated Progress profiles for:
  - `progress_sunsense_solar_rebate` on `nc-progress-leaf-716`
  - `progress_meter_related_optional_programs` on `nc-progress-leaf-661`
  - `progress_standby_service` on `nc-progress-leaf-653`
  - `progress_greenpower_program` on `nc-progress-leaf-642`
- `parse-review-queue` and `parse-review-summary` are now lineage-aware:
  - deleted historical documents are ignored
  - current family/company metadata overrides stale historical attempt metadata
  - repeated reruns of the same historical document collapse to the latest
    operational attempt instead of inflating the queue
- after those cleanup cycles, the active backlog is now `68 needs_review`, not
  the older inflated count that included superseded attempts and stale lineage
  metadata

The Carolinas compliance-book cleanup also now has a dedicated residue path for
heading-only bounded spans such as `TYPE OF SERVICE` and
`Effective for service`. Those rows can now be surfaced with
`list-placeholder-heading-historical-nc` and removed with
`retire-historical-document`. That path has already been used live to retire
`19` placeholder heading rows that were inflating `generic_residential`
review volume without representing real tariff families.
Recent bounded-profile cleanup also added:
- `carolinas_energy_efficiency_rider`
- `carolinas_economic_development_rider`
- `carolinas_interruptible_service_rider`
- `green_source_advantage_rider`
- `carolinas_schedule_bridge`

Those profiles recovered explicit numeric extraction for Carolinas `Rider EE`,
`Rider EC`, `Rider IS`, and for GSA administrative charges on both the
Carolinas and Progress NC historical branches. The new
`carolinas_schedule_bridge` also recovered bounded historical Carolinas
schedule sheets such as `SCHEDULE I`, `SCHEDULE OPT-E`, `SCHEDULE TS`, and
`SCHEDULE WC` that had been falling through `generic_residential`.

The shared Carolinas leaf parser now also recognizes `Basic Facilities Charge`
rows directly. That improved fixed-charge recovery across historical Carolinas
schedule books without needing one-off parsers for each family.

The Carolinas lighting profile also widened again:
- `carolinas_lighting_schedule` now covers `OL`, `PL`, `FL`, `YL`, and `GL`
- the legacy Carolinas leaf-99 summary path now has a fallback extractor for
  old columnar total-only matrices
- the stale `TRAFFIC SIGNAL SERVICE` alias row has been retired once the
  correctly bounded `nc-carolinas-schedule-TS` row was reparsed strongly

Recent live cleanup used that path to retire obsolete raw rows for:
- `nc-progress-leaf-609`
- `nc-progress-leaf-640`
- `nc-progress-leaf-572`

The importer also now reuses single-family legacy raw attachment hints during
remine. In practice, that means discovery-backed remine passes can prefer the
intended historical family over generic provisional placeholders when the old
legacy evidence is unambiguous.
We also now have a current-anchor mismatch review surface for `tariff_families`
that already have a `current_document_id`. That review compares the family row
against the anchored current PDF's `schedule_code`, `tariff_identifier`, and
first-page mined headings/leaf numbers. This makes catalog contradictions
visible before they spill into profile targeting or parser work. When the
anchor itself is trusted, operators can now also sync the family row's title,
schedule code, and tariff identifier from the anchored current document instead
of editing `tariff_families` by hand. That flow has already been used to clean
up low-risk DEP current-anchor mismatches for:
- `nc-progress-leaf-609`
- `nc-progress-leaf-662`
- `nc-progress-leaf-670`
The former DEP migration-review cases:
- `nc-progress-leaf-501`
- `nc-progress-leaf-504`
- `nc-progress-leaf-607`

have now been resolved into separate current vs historical lineages:
- current anchors remain on `nc-progress-leaf-501`, `504`, and `607`
- historical-only families now preserve the older meanings as:
  - `nc-progress-doc-FUELCHARGEADJUSTMENT`
  - `nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY`
  - `nc-progress-doc-STORMRECOVERYRIDER`

The live current-anchor mismatch queue for DEP is now `0`.
The next DEP extraction-quality improvement after that cleanup has been a
dedicated `progress_billing_adjustments` profile for `nc-progress-leaf-601`
Rider BA sheets. That profile reuses the existing Progress-specific Rider BA
table parser and is already producing structured adjustment rows for true
historical `leaf-601` sheets. The main remaining `leaf-601` noise now appears
to be family-matching contamination from `leaf-600` summary spans that were
attached to Rider BA instead of the rider-summary family.

### 7. Effective date extraction is fragile
Dates appear in many formats ("Effective December 1, 2020", "Applicable beginning with service rendered on or after January 1, 2019", etc.) and sometimes only in headers/footers that `pdfplumber` doesn't reliably extract.

### 8. Coverage is sparse
Of 111 tariff families, only ~31 have 2+ historical versions. The remaining 80 have just the current sheet. Most filings in NCUC's portal for relevant dockets don't contain leaf numbers — they're orders, testimony, briefs, etc. We rely on a small fraction of "compliance tariff" filings that actually contain the tariff text.

---

## Current Implementation Direction

The current implementation direction is:

1. Keep triage, segmentation, and family matching generic and CPU-first.
2. Expand historical extraction through parser profiles rather than one shared extractor.
3. Add a separate OCR queue for `OCR_REQUIRED` documents.
4. Reserve GPU-backed OCR/layout for the subset of documents marked as
   `gpu_ocr_candidate`.
5. Feed OCR output back into the same page-mining and parser-profile flow instead
   of building a disconnected OCR-only pipeline.
6. Add an optional Docling path for documents where structure, layout, table
   recovery, or richer confidence signals matter more than raw OCR text alone.
7. Cache Docling JSON / text / table artifacts separately from plain OCR
   sidecars and invalidate them by source hash, Docling backend version, and
   accelerator mode.
8. Use Docling confidence grades and page-quality signals as selective
   reprocess inputs rather than running heavy conversion on every document.
9. Move the older JSON-to-DB ingest path toward direct DB writes with optional
   JSON export only when explicitly requested.
10. Use targeted reprocessing records to re-run weak or changed documents instead
   of sweeping the entire archive after every parser change.
11. Use shared stage versions to detect stale OCR/page/span/parser artifacts and
   queue only the documents whose cached stages are out of date.
   Stale reprocess runs now also bootstrap missing `tariff_versions` and refresh
   page/span artifacts before extraction, which cleared the live stale-stage
   backlog under the current stage versions.
12. Use parser-profile dependency rules to preview or queue only the historical
   families/documents affected by a specific parser-profile change.
13. Persist fallback recommendations and conservatively retry alternate profiles
    when the initial historical extraction returns no charges, and for weak
    parses only when an alternate profile is materially better.
    Current fallback scoring now considers:
    - charge-count gain
    - charge-type coverage gain
    - TOU/season coverage gain
    - basic field completeness gain
11. Continue closing family-catalog gaps after parser fixes.
    Some Carolinas filings now produce correct tariff spans but still do not
    import because no matching family/alias exists yet for the extracted
    document class. Those should be handled as targeted family-modeling work,
    not as generic parser regressions.
12. Keep bulk extraction version-aware.
    The `extract-rates-nc` operator path now targets only historical documents
    that already have a linked `tariff_version`, and it explicitly reports the
    count of otherwise-extractable historical documents that are still missing
    that linkage. This prevents confusing “processed N documents, inserted 0”
    runs that were actually just missing-version no-ops.
13. Treat obvious reference-only historical documents as skips, not parser
    failures.
    Some historical sheets are real tariff-family artifacts but not bill-rate
    tables in the extraction sense, such as service-regulation pages,
    line-extension plans, or EV pilot program terms. These should leave the
    pipeline as reviewed `skipped_*` outcomes when they clearly lack billable
    rate structures, rather than padding the `generic_residential` weak queue.
14. Bundle-style legacy remine is now partially stabilized.
    The importer now:
    - splits book-style tariff PDFs on distinct schedule-title transitions even
      without leaf headers
    - ignores generic provisional families like `TYPEOFSERVICE` during normal
      family matching
    - applies legacy raw-attachment hints per span instead of only when a
      whole regulator PDF points to one family
    Live effect:
    - `E-2, Sub 1142` now remines into bounded DEP schedule sections and
      recreates `nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY`
    - the Progress weak-unbounded legacy raw queue is down to six rows
    - discovery `957` legacy residue can now be retired when cached page
      evidence shows the regulator PDF is procedural-only and lacks real tariff
      structure
    - the remaining Progress weak-unbounded legacy raw queue is now down to
      four rows, all from discovery `1124`, and they are correctly classified
      as lineage review rather than more blind remine work
    - the old `957` residue (`leaf-613`, `leaf-672`) has been retired from the
      live DB, confirming the queue can now separate procedural false positives
      from true remine candidates

## Planned Parsing-Improvement Layer

The next major improvement is not just more parsing rules. It is a document
intelligence layer that makes parser improvement data-driven.

Planned additions:

1. Document and span fingerprints
- persist feature summaries for each file or bounded span
- include text density, OCR confidence, layout complexity, utility aliases,
  schedule/leaf/rider candidates, date cues, and table signals
- use those fingerprints to support a relationship index for documents that are
  not currently parsed into tariff data but are still relevant to:
  - rates
  - riders
  - dockets
  - revisions / effective periods
  - causal/supporting topics like fuel costs or rider justifications
- support an optional LLM-assisted enrichment pass that can:
  - explain document purpose in plain language
  - suggest better document classes / subtypes
  - infer likely relationships not yet captured by rules
  - identify candidate extraction targets for future parser work

2. Parser-profile diagnostics
- record which parser profile was chosen
- record alternate profiles considered
- retain confidence, review flags, missing fields, and failure reasons
- persist review outcomes so weak parses can later be accepted, corrected, or rejected
- current CLI support:
  - `parse-review-queue`
  - `record-parse-review`
  - `parse-review-summary`
- correction summaries now track recurring correction categories such as
  charge-value, date, tariff-identity, and rate-structure fixes
- targeted reprocessing support now exists through:
  - `enqueue-reprocess-nc`
  - `show-reprocess-queue-nc`
  - `process-reprocess-queue-nc`
- stale-stage support now exists through:
  - `show-stale-historical-nc`
  - `enqueue-stale-reprocess-nc`
- parser-profile impact support now exists through:
  - `show-profile-impact-nc`
  - `enqueue-profile-impact-nc`
  - impact targeting now consults persisted candidate-profile reasons and
    document signals from the latest historical parse attempt
- parser fallback sequencing now exists in the historical bulk extractor:
  - recommendation order is persisted in parse diagnostics
  - alternate profiles are auto-tried only for empty first-pass extractions
- OCR queue/cache support now exists through:
  - `enqueue-ocr-nc`
  - `show-ocr-queue-nc`
  - `process-ocr-queue-nc`

3. Evidence retention from existing PDFs
- keep more than final extracted fields
- preserve useful intermediate artifacts such as:
  - header/footer snippets
  - candidate dates
  - table rows
  - OCR text
  - layout clues
  - family-match evidence
- current cached artifact support includes:
  - mined page evidence
  - bounded span artifacts
  - OCR text/page sidecars
- planned heavy-analysis artifact support includes:
  - Docling JSON / structured document output
  - Docling plain-text exports
  - Docling table exports
  - Docling page/document confidence grades

4. Relationship mapping across non-tariff documents
- build a queryable map of how downloaded documents relate to:
  - tariff families and rider codes
  - historical document revisions
  - dockets / filing series
  - relevant time windows
  - explanatory or causal evidence topics
- use that map to:
  - find supporting documents after the fact
  - re-digest documents that were previously considered irrelevant
  - target new parsers at already-downloaded filings instead of rediscovering everything

5. LLM-assisted analysis layer
- use an LLM as an assistive layer for:
  - document-purpose explanation
  - relationship suggestion
  - topic tagging
  - parser-target suggestion
  - ambiguity resolution support
- keep strict guardrails:
  - LLM output should be stored with confidence and evidence snippets
  - LLM output should not silently replace deterministic parsed values
  - authoritative DB facts should still come from rules, OCR/text extraction, or reviewed workflows
  - LLM analysis should help decide what to inspect, explain, or parse next
  - for layout-heavy or table-heavy documents, a future Docling-backed chunking
    / serialization layer should be preferred over flat OCR text when sending
    document evidence into LLM analysis

4. Gold-set evaluation corpus
- maintain a labeled set of representative documents and spans
- use it to regression-test parser changes and identify which document classes
  need new profiles

This is the path intended to make the pipeline reusable beyond NCUC and better
at learning from documents already downloaded.
