"""Heuristics for tagging NCUC docket filings as approved vs proposed.

Background: PR #34 added a ``status`` column to ``historical_documents`` and
``tariff_versions`` so the billing engine can ignore not-yet-approved filings
by default. The schema is decoupled from any auto-tagging policy — this module
exists to provide the *policy*, callable from CLI helpers (e.g.
``scripts/set_doc_status.py --auto-detect-docket``).

The detection rule is deliberately conservative:

  A docket is "proposed" iff it contains at least one filing whose
  classification is application/testimony/settlement AND no filing in the
  same docket has classification 'order' (or proceeding_type 'Order').

  Otherwise the docket is "approved" — either the order has been issued
  (so the application is moot) or the docket isn't a proposed-rate case
  at all.

This rule is intentionally docket-scoped, not record-scoped: a single
"Application" PDF doesn't imply proposed status if the same docket also
contains the order that approved it. That guards the historical corpus
from being mis-tagged when only the application title is inspected.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

# Filing classifications that indicate the docket is still in-flight when no
# corresponding order exists yet.
_PROPOSAL_CLASSIFICATIONS: frozenset[str] = frozenset({
    "application",
    "testimony",
    "settlement",
    "compliance_filing",
})

# Filing classifications or proceeding types that prove the docket has been
# acted on by the commission.
_ORDER_CLASSIFICATIONS: frozenset[str] = frozenset({"order"})
_ORDER_PROCEEDING_TYPES: frozenset[str] = frozenset({"order", "Order"})


def docket_has_order(
    classifications: Iterable[str | None],
    proceeding_types: Iterable[str | None],
) -> bool:
    """Return True if any (classification, proceeding_type) row indicates
    a final NCUC order has been issued in this docket."""
    for c in classifications:
        if c and c.lower() in _ORDER_CLASSIFICATIONS:
            return True
    for p in proceeding_types:
        if p and p in _ORDER_PROCEEDING_TYPES:
            return True
    return False


def docket_has_proposal_filing(classifications: Iterable[str | None]) -> bool:
    """Return True if any record carries a 'proposal' filing classification."""
    return any(
        c and c.lower() in _PROPOSAL_CLASSIFICATIONS
        for c in classifications
    )


def compute_docket_status(
    classifications: Iterable[str | None],
    proceeding_types: Iterable[str | None],
) -> str:
    """Return 'proposed' if the docket is in-flight (proposal filings exist
    AND no order has been issued), else 'approved'.

    Pure function — no DB access, fully testable.
    """
    classifications = list(classifications)
    proceeding_types = list(proceeding_types)
    if docket_has_order(classifications, proceeding_types):
        return "approved"
    if docket_has_proposal_filing(classifications):
        return "proposed"
    return "approved"


def detect_docket_status_from_db(
    conn: sqlite3.Connection, docket_number: str
) -> str:
    """Look up all NCUC discovery records for ``docket_number`` and return
    the computed status. Returns 'approved' if the docket has no records."""
    rows = conn.execute(
        """SELECT filing_classification, proceeding_type
             FROM ncuc_discovery_records
            WHERE docket_number = ?""",
        (docket_number,),
    ).fetchall()
    if not rows:
        return "approved"
    classifications = [r[0] for r in rows]
    proceeding_types = [r[1] for r in rows]
    return compute_docket_status(classifications, proceeding_types)


def historical_doc_ids_for_docket(
    conn: sqlite3.Connection, docket_number: str
) -> list[int]:
    """Return historical_documents.id values for all records in the docket
    that have been imported (i.e. their local_path is also tracked on a
    historical_documents row)."""
    rows = conn.execute(
        """SELECT DISTINCT hd.id
             FROM historical_documents hd
             JOIN ncuc_discovery_records dr
               ON dr.local_path = hd.local_path
            WHERE dr.docket_number = ?""",
        (docket_number,),
    ).fetchall()
    return [int(r[0]) for r in rows]
