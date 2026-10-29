# PR #25 — RAG metadata prefix review

Reviewed at the user's request as part of the goal-session closeout.
PR: https://github.com/bradrcurry/NCUC_standalone/pull/25
Head: `d317800b76f20ed26c4ffc00253058c7dc1f415f`.

## Decision

**Set aside; do not merge or cherry-pick the complete change at this checkpoint.**
Git can merge it cleanly, and 76 current RAG tests pass with the candidate module
loaded in an isolated test process. The issue is the PR's explicitly reported
citation-grounding regression, not a textual conflict or broken Python API.
The user authorized setting aside PRs with significant issues and assessing
useful changes for later preservation. The original branch/commit and open PR
remain intact.

## What changes and what already exists

The sole changed file is `document_intelligence/rag_generator.py`: 18 added lines
and one replaced line in `_build_context_blocks`. It prepends section type,
up to five schedule codes, rider codes and leaf numbers to each source excerpt.
No retrieval algorithm, metadata storage, debug endpoint or citation validator
is added.

Those metadata fields already exist on RetrievalHit. The existing `citation()`
already includes the filename, page range and best schedule/rider/leaf code.
The new behavior is presenting fuller metadata lists and section type to the
LLM as additional prompt context. That can aid disambiguation, but the fields
are index/classification interpretations and are not independently established
source facts or approval evidence.

## Significant issue

The PR's historical 18-case evaluation reports:

| Metric | Before | Candidate |
| --- | --- | --- |
| Answered | 11/18 | 12/18 |
| Keyword match | 0.818 | 0.909 |
| Grounded among answered cases | 1.000 | 0.917 |
| Correct refusal | 1.000 | 1.000 |

It identifies `lgs_demand_charge` as changing from cited/grounded to uncited/
ungrounded, and explains that the model sometimes treats metadata as standalone
fact. These are historical PR-author results, not a fresh live-model evaluation
reproduced in this review. The current compatibility tests do not establish
that the grounding regression has been fixed.

This goal prioritizes source traceability and correctness over increased recall.
The existing system prompt requires citations for every factual claim. A
reported violation of that requirement is material even though one additional
question is answered and the text merge is clean. Moreover, `is_grounded` only
checks that at least one citation exists; a high aggregate value is not proof
that all facts are grounded. The PR contains no compensating grounding gate.

## Cherry-pick assessment

There is no independently validated, non-regressing retrieval/debug hunk to
extract. All novel functionality is the metadata-to-prompt presentation that
produces the reported trade-off. Underlying metadata/citation functionality is
already present, so no data fields are lost by holding this PR.

Preserve the small formatting change as a candidate for a focused follow-up:
label classification-derived values as unverified retrieval hints; require
source-text support and citations even when metadata is present; preserve
refusal when metadata names a schedule but the excerpt lacks the requested
fact; rerun an evidence-focused/held-out evaluation, including the known demand
charge regression. Ship only after citation/source grounding does not regress.
No new feature flag or unvalidated partial prompt change is introduced now.

## Verification

- Remote PR reported MERGEABLE/CLEAN, with no status checks configured.
- `git merge-tree --write-tree` against the integrated goal chain returned a
  tree with exit 0 and no conflict entries. The RAG file is otherwise identical
  between current goal code and the PR head apart from the stated hunk.
- Candidate module execution in an isolated process: **76 passed in 0.61 s**
  across generator, retriever and eval tests; no live LLM evaluation or model
  download was performed.
- GitNexus context/impact: one direct caller, one reported affected process,
  LOW risk. No RAG source file was edited in the goal checkout.
- Final post-merge compatibility state is included in the publication receipt.

This is a conditional hold, not rejection or deletion of the original work.
