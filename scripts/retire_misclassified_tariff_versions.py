"""Retire tariff_versions whose underlying document is procedural, not a tariff.

The NCUC import pipeline's family-key assignment is greedy: any PDF that
mentions a rider/schedule name can get bound into that family's lineage,
including the cover letter, order, testimony, and compliance-filing PDFs
that accompany the actual tariff sheet. These auxiliary docs end up as
tariff_versions rows that the engine treats as legitimate rate sheets;
when they happen to be the *latest* version in the family, they can even
displace the real charged version in _select_version's ORDER BY
effective_start DESC pick.

The document_classifications table already has the signal we need: each
historical_document has been independently classified by
  - rule_document_type_v2 (rule-based)
  - embedding_knn_v1 (nearest-neighbour against the gold corpus)
  - llm_qwen3:8b_v1 (LLM)

Rule (intentionally conservative, agreement-based):
  Retire a tariff_versions row when ALL of:
    1. The row has zero tariff_charges anchored to it
    2. >= 2 of the three independent classifiers label the document as
       procedural — one of:
         COVER_LETTER, ORDER_FINAL, ORDER_PROCEDURAL,
         TESTIMONY, APPLICATION, CERTIFICATE_OF_SERVICE
    3. No classifier labels the doc as TARIFF_SHEET or RATE_SCHEDULE
       (i.e. no disagreement from a positive tariff signal)

Effect: deletes the tariff_versions row (and any dependent tariff_charges,
which by rule (1) should be zero). Keeps the historical_documents row so
the doc remains discoverable and any future re-classification can re-mint
a tariff_versions row.

Idempotent. Dry run by default; --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

DB_PATH = Path("data/db/duke_rates.db")

PROCEDURAL_LABELS = frozenset({
    "COVER_LETTER",
    "ORDER_FINAL",
    "ORDER_PROCEDURAL",
    "TESTIMONY",
    "APPLICATION",
    "CERTIFICATE_OF_SERVICE",
    "NOTICE_OF_HEARING",
})

# If any classifier strongly says it IS a tariff, we never retire.
# RIDER is a positive tariff signal — riders are rate sheets too.
# COMPLIANCE_FILING is deliberately omitted: it's ambiguous (sometimes the
# actual revised tariff sheet, sometimes just a cover letter for one).
POSITIVE_TARIFF_LABELS = frozenset({
    "TARIFF_SHEET",
    "RATE_SCHEDULE",
    "RIDER",
})

# Classifiers we consider as independent votes
CLASSIFIER_KEYS = (
    "rule_document_type_v2",
    "embedding_knn_v1",
    "llm_qwen3:8b_v1",
)


def latest_classifications(
    conn: sqlite3.Connection, subject_id: int
) -> dict[str, str]:
    """Return {classifier_name: label} for the latest (un-superseded) row
    per classifier for this historical_document."""
    out: dict[str, str] = {}
    for r in conn.execute(
        """SELECT classifier, label FROM document_classifications
             WHERE subject_kind='historical_document'
               AND subject_id=?
               AND superseded_by IS NULL""",
        (subject_id,),
    ):
        out[r[0]] = r[1]
    return out


def is_procedural_by_consensus(labels: dict[str, str]) -> tuple[bool, dict]:
    """Apply the agreement rule. Returns (retire, breakdown_dict)."""
    votes_procedural = []
    votes_tariff = []
    for k in CLASSIFIER_KEYS:
        v = labels.get(k)
        if v in PROCEDURAL_LABELS:
            votes_procedural.append((k, v))
        elif v in POSITIVE_TARIFF_LABELS:
            votes_tariff.append((k, v))
    breakdown = {
        "procedural": votes_procedural,
        "tariff": votes_tariff,
        "labels": {k: labels.get(k, "-") for k in CLASSIFIER_KEYS},
    }
    if votes_tariff:
        return False, breakdown
    return len(votes_procedural) >= 2, breakdown


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true", help="Write changes")
    ap.add_argument(
        "--state-company",
        default=None,
        help="Filter to e.g. 'NC/carolinas' (default: all)",
    )
    ap.add_argument(
        "--limit-show",
        type=int,
        default=30,
        help="Max rows to display in the report",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    where_state = ""
    params: tuple = ()
    if args.state_company:
        state, company = args.state_company.split("/", 1)
        where_state = "AND hd.state=? AND hd.company=?"
        params = (state, company)

    # Find zero-charge tariff_versions and their docs
    rows = conn.execute(
        f"""SELECT tv.id AS vid, tv.family_key AS tv_family,
                  hd.id AS hd_id, hd.family_key AS hd_family,
                  hd.effective_start, hd.state, hd.company, hd.title
             FROM tariff_versions tv
             JOIN historical_documents hd ON hd.id = tv.historical_document_id
            WHERE NOT EXISTS (
                  SELECT 1 FROM tariff_charges tc WHERE tc.version_id=tv.id)
              {where_state}""",
        params,
    ).fetchall()

    candidates: list[tuple[sqlite3.Row, dict]] = []
    examined = 0
    no_classifications = 0
    has_tariff_vote = 0
    insufficient_procedural = 0
    for row in rows:
        examined += 1
        labels = latest_classifications(conn, int(row["hd_id"]))
        if not any(labels.get(k) for k in CLASSIFIER_KEYS):
            no_classifications += 1
            continue
        retire, breakdown = is_procedural_by_consensus(labels)
        if breakdown["tariff"]:
            has_tariff_vote += 1
        if not retire:
            if not breakdown["tariff"]:
                insufficient_procedural += 1
            continue
        candidates.append((row, breakdown))

    print("=== Candidate retirements ===")
    print(f"  examined (zero-charge versions): {examined}")
    print(f"  no classifications on file:      {no_classifications}")
    print(f"  has positive TARIFF vote:        {has_tariff_vote}")
    print(f"  insufficient procedural votes:   {insufficient_procedural}")
    print(f"  -> to retire:                    {len(candidates)}")
    print()

    by_fam: Counter[str] = Counter(c[0]["tv_family"] for c in candidates)
    print("Top affected families:")
    for fam, n in by_fam.most_common(15):
        print(f"  {n:>3}  {fam}")
    print()

    print(f"Sample (showing up to {args.limit_show}):")
    print(
        f'{"hd":>5} {"vid":>5} {"family_key":<40} {"v2":<18} {"knn":<18} {"qwen":<22}'
    )
    print("-" * 130)
    for row, br in candidates[: args.limit_show]:
        labs = br["labels"]
        print(
            f'{row["hd_id"]:>5} {row["vid"]:>5} '
            f'{row["tv_family"]:<40} '
            f'{labs["rule_document_type_v2"]:<18} '
            f'{labs["embedding_knn_v1"]:<18} '
            f'{labs["llm_qwen3:8b_v1"]:<22}'
        )

    if not args.apply:
        print()
        print("DRY RUN — pass --apply to retire these tariff_versions.")
        return 0

    cur = conn.cursor()
    n_charges_deleted = 0
    for row, _ in candidates:
        vid = int(row["vid"])
        # Defensive: shouldn't have any charges by rule (1), but clean up
        # anyway in case the row state changed between scan and apply.
        n = cur.execute(
            "DELETE FROM tariff_charges WHERE version_id = ?", (vid,)
        ).rowcount
        n_charges_deleted += n
        cur.execute("DELETE FROM tariff_versions WHERE id = ?", (vid,))
    conn.commit()
    print()
    print(
        f"Retired {len(candidates)} tariff_versions "
        f"({n_charges_deleted} stray charges cleaned up)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
