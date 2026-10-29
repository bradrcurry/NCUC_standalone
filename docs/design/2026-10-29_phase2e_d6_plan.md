# D6 — Evidence-driven discovery plan

Baseline: local D5 commit `7dc5caa`, branch `codex/d6-evidence-driven-discovery`.
Research database: `scratch/d6.db`, copied from the preserved D5 snapshot.
Production is excluded from all writes. The D4 preservation incident remains an
unmet acceptance gate; this milestone cannot reverse that incident.

Existing infrastructure already provides 30,027 clues, 201 supersession links,
691 typed gaps, 628 local candidates, and 730 successfully fetched order records.
Extend those ledgers and the existing document blob registry. Do not create a
second discovery catalog, graph database, or promotion path.

1. Make completed-search identity sensitive to native artifact contents,
   discovery records, source file identity, and extractor/redline method versions.
   Compute that snapshot once per resolution/planning run. Failed source checks
   are incomplete searches, never verified negative evidence. Require complete,
   readable, clean own-page evidence for a full candidate.
2. Preserve exact page, text and geometry for caption/body docket references,
   separately from portal filing metadata. Multiple printed dockets can locate
   an order without claiming that every referenced docket is its primary filing
   or that the order approves a particular tariff. Reuse cached native artifacts;
   bounded caption reading is a fallback.
3. Extend producer namespaces without changing legacy clue identities. Preserve
   version supersession links. Their existing CHECK constraint supports only
   `supersedes`; any document-level relationships require an additive table,
   with exact supporting clues and explicit inference/review status. Never
   rebuild constrained evidence tables to widen their enums. Proposed revisions
   must not become verified actual supersession merely from a printed header.
4. Make remote retrieval durable and content-addressed through existing storage
   and blob registration. Verify cached files before URL reuse. Hash and validate
   staged downloads, atomically preserve original PDF bytes, and retain candidate
   provenance. Linking and approval remain separate reviewed actions.
5. Test stale negative invalidation, missing/corrupt artifacts, multi-docket
   attribution, incomplete text, deduplication, source mutation, proposal lineage,
   additive migration, and review preservation. Run the 50-page clue benchmark
   with its actual assistant-transcribed attribution, local-first before/after
   discovery measurements, relevant full tests and read-only billing checks.

The detailed implementation may narrow abstractions where existing components
already satisfy the requirement. Any new relationship records remain research
hypotheses unless actual source scope supports corroboration; no automated human
verification labels or accepted rate writes. Prefer archived evidence and D4's
captured inventory; live retrieval only if a specific remaining gap requires it,
bounded and stopped on access denial. Record implementation, limits, evidence,
GitNexus scope and tests before the local milestone commit.
