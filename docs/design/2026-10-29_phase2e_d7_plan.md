# D7 — Large-document intelligence

Base: D6 local commit `2555e98`; branch `codex/d7-large-document-intelligence`.
No production writes, promotions, archive replacement, new models or dependency
installs. The D4 preservation incident remains an unmet overall acceptance gate.

## User-requested stopping checkpoint

The user requested a natural stop, integrated PR/merge and a full handoff.
Deliver D7A segmentation/routing/cache evidence now. Items 3 and 4 below
(adaptive bounded extraction and representation caching), broader hierarchy
work and held-out validation remain D7B. Do not mark the whole D7 milestone
or the original D4–D10 goal complete at this checkpoint.

## Source-led evaluation

Use the actual 110-page JAA, 316-page compliance book, 598-page appeal record,
and 1,097-page DSM/EE filing, with byte hashes and archive aliases checked.
Reference annotations come from original PDF text and rendered source pages,
not stored segmentation or section predictions. Label provenance is assistant
source review; it is not independent human verification or a held-out study.

Score formal tariff-component boundaries separately from coarse non-tariff
regions and supporting financial exhibits. Do not describe a JAA calculation
exhibit as an official tariff. Do not count the DSM/EE calculation exhibits
following the three Leaf 62 pages as additional tariff sheets. Review all
unreadable pages visually; retain incomplete/unknown coverage explicitly.
The tariff book has real source anomalies (a repeated printed Leaf 605 and
headerless continuation tables); preserve them rather than correcting sources.

## Extend existing components

1. Keep PageEvidence, TariffSpan and segment_document as the page/boundary
   contract. Tighten weak density/heading decisions using source context,
   preserve non-tariff transitions, prevent noncontiguous page ownership, and
   retain reasons for boundaries and cautious continuation. Use existing section
   typing/hierarchy where needed rather than introducing another segmenter.
2. Correct triage's sampled-text denominator. Large native-text filings must
   not be routed to full OCR because of their unsampled pages. Keep per-page
   missing-text evidence available for selective handling.
3. Extend bounded extraction with explicit owned/context ranges, safe overlap,
   original page mapping, adaptive failure recovery and strict partial-result
   detection. Reuse the existing page-integrity assessor. Never infer original
   numbering from an internal converter's unverified final chunk.
4. Reuse RepresentationCache for explicitly identified bounded conversions;
   include source bytes, owned/context ranges, converter/method identity and
   options. Do not cache uncertain source identities or silently treat a
   degraded result as a complete successful conversion.
5. Measure page/section recall and false positives together with extraction
   provenance, continuation completeness, time, cache reuse, conversion pages,
   peak RSS, and actual available GPU measurements. No fabricated VRAM figures
   when no GPU conversion is run. Reuse the existing extraction/billing gates.

## Validation and delivery

Existing baseline: 69 focused tests pass. GitNexus refreshed after D6. Shared
classifier/segmenter/features and triage carry CRITICAL graph impact (up to six
direct callers, four processes), reported before edits; bounded conversion is
HIGH. Use targeted fixtures and source-backed boundary references before broad
checks. Preserve compatibility defaults and candidate-only processing. Publish
source review scope, benchmark results and limitations, run staged GitNexus and
AST scope review, and commit D7 locally before beginning D8.
