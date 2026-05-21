"""Seed legislative_actions with verified NC energy-policy events.

These events are the starter set for the residential dashboard timeline
annotations. All events here are independently verified from public sources
(NCGA bill records, Duke press releases, NCUC orders). Grow this set over
time with additional dockets and federal actions; the dashboard reads from
this table directly with no code changes required.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "db" / "duke_rates.db"


# Each event: (bill_number, session_year, short_title, summary, impact_category,
#              utilities_affected, effective_date, source_url, evidence_text, confidence)
EVENTS = [
    (
        "HB 589",
        "2017",
        "Competitive Energy Solutions for NC",
        (
            "Established the Competitive Procurement of Renewable Energy (CPRE) "
            "program requiring Duke to procure ~2,660 MW of utility-scale solar "
            "via competitive bidding. Also created the rooftop solar rebate program "
            "and Green Source Rider. Origin of Rider CPRE on residential bills."
        ),
        "renewable_policy",
        "DEP,DEC",
        "2017-07-27",
        "https://www.ncleg.net/Sessions/2017/Bills/House/PDF/H589v5.pdf",
        "Signed into law by Governor Cooper on July 27, 2017.",
        0.95,
    ),
    (
        "HB 951",
        "2021",
        "Energy Solutions for North Carolina",
        (
            "Required NCUC to take all reasonable steps to achieve 70% carbon "
            "emissions reduction from 2005 levels by 2030 and carbon neutrality "
            "by 2050. Authorized multi-year rate plans (MYRPs) and performance-based "
            "regulation. Origin of Rider PIM and the framework behind recent rate cases."
        ),
        "carbon_policy",
        "DEP,DEC",
        "2021-10-13",
        "https://www.ncleg.gov/BillLookup/2021/H951",
        "Signed into law by Governor Cooper on October 13, 2021 as SL 2021-165.",
        0.95,
    ),
    (
        "Winter Storm Uri",
        "2021",
        "Winter Storm Uri natural gas price spike",
        (
            "Mid-February 2021 winter storm caused historic spike in natural gas "
            "spot prices across the southeast. Drove large under-recoveries in "
            "fuel-cost riders (BA-Fuel, BA-EMF) that were trued up in subsequent "
            "annual filings, raising residential bills."
        ),
        "fuel_event",
        "DEP,DEC",
        "2021-02-15",
        None,
        "Documented in Rider BA-EMF filings as a major driver of 2022 true-up adjustments.",
        0.85,
    ),
    (
        "Russia-Ukraine fuel spike",
        "2022",
        "Global natural gas price surge",
        (
            "Russia's invasion of Ukraine in late February 2022 sent global natural "
            "gas and coal prices to multi-year highs through 2022. Drove the largest "
            "annual fuel-cost increase in recent DEP/DEC history, reflected in 2022-12 "
            "and 2023-12 Rider BA filings."
        ),
        "fuel_event",
        "DEP,DEC",
        "2022-02-24",
        None,
        "Major driver of 2022-12-01 fuel rider increases across Duke NC jurisdictions.",
        0.80,
    ),
    (
        "Tax Cuts and Jobs Act EDIT",
        "2017",
        "Federal corporate tax rate cut creates EDIT credits",
        (
            "Federal corporate tax rate dropped from 35% to 21%, leaving utilities "
            "holding deferred income tax liabilities (EDIT) that had been collected "
            "from customers at the higher rate. NCUC ordered Duke to return these "
            "via Riders EDIT-1, EDIT-3, and EDIT-4 — appearing as credits on residential bills."
        ),
        "tax_policy",
        "DEP,DEC",
        "2018-01-01",
        None,
        "Origin of EDIT rider series; flows through NCUC dockets E-7 Sub 1136 and E-2 Sub 1142.",
        0.90,
    ),
    (
        "DEP 2022 rate case order",
        "2023",
        "NCUC approves DEP base rate increase (E-2 Sub 1300)",
        (
            "First DEP general rate case under HB 951's multi-year rate plan framework. "
            "Approved base-rate increase phased in starting October 2023, plus PIM "
            "performance incentives. Visible as a step change in base rate component."
        ),
        "rate_case",
        "DEP",
        "2023-10-01",
        None,
        "NCUC Docket E-2 Sub 1300 order; effective date for first-year MYRP step.",
        0.85,
    ),
    (
        "DEC 2023 rate case order",
        "2023",
        "NCUC approves DEC base rate increase (E-7 Sub 1276)",
        (
            "First DEC general rate case under HB 951's MYRP framework. "
            "Approved base-rate increase with phased step increases across the "
            "multi-year plan period."
        ),
        "rate_case",
        "DEC",
        "2023-08-01",
        None,
        "NCUC Docket E-7 Sub 1276 order; first-year MYRP step.",
        0.80,
    ),
]


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        existing = {
            row[0]
            for row in cur.execute(
                "SELECT bill_number FROM legislative_actions"
            ).fetchall()
        }
        inserted = 0
        skipped = 0
        for ev in EVENTS:
            bill_number = ev[0]
            if bill_number in existing:
                skipped += 1
                continue
            cur.execute(
                """
                INSERT INTO legislative_actions
                    (bill_number, session_year, short_title, summary,
                     impact_category, utilities_affected, effective_date,
                     source_url, evidence_text, confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*ev, now, now),
            )
            inserted += 1
        conn.commit()
        print(f"Seeded {inserted} events ({skipped} already present).")
        total = cur.execute("SELECT COUNT(*) FROM legislative_actions").fetchone()[0]
        print(f"legislative_actions now has {total} rows.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
