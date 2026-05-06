"""
Audit DQ-005: Show all non-ISO effective_start rows in tariff_versions,
classify by type (parseable, ambiguous, PATH-style family key, etc.)
so we can plan the normalization.
"""
import re
import sqlite3
from datetime import datetime

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

def parse_fuzzy_date(s: str) -> str | None:
    """Try to parse a human-readable date string to ISO YYYY-MM-DD."""
    s = s.strip().replace("\n", " ").replace("  ", " ")
    # Try standard formats
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Try "Month D, YYYY" with single digit
    m = re.match(
        r"^(\w+)\s+(\d{1,2}),?\s+(\d{4})$", s, re.I
    )
    if m:
        month_name, day, year = m.groups()
        month_num = MONTH_MAP.get(month_name.lower())
        if month_num:
            return f"{int(year):04d}-{month_num:02d}-{int(day):02d}"
    return None


conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

cur.execute(
    """
    SELECT id, family_key, effective_start, effective_end
    FROM tariff_versions
    WHERE effective_start IS NOT NULL
      AND effective_start NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
    ORDER BY family_key, effective_start
    """
)
rows = cur.fetchall()

print(f"Total non-ISO date rows: {len(rows)}\n")

for r in rows:
    vid, fkey, eff_start, eff_end = r
    parsed = parse_fuzzy_date(eff_start)
    path_style = fkey.startswith("/pdfs/")

    # Check if an ISO-format version already exists for the same family + parsed date
    if parsed:
        cur.execute(
            """
            SELECT id, effective_start FROM tariff_versions
            WHERE family_key = ?
              AND effective_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
              AND effective_start LIKE ?
            """,
            (fkey, parsed + "%"),
        )
        iso_dupes = cur.fetchall()
    else:
        iso_dupes = []

    # Check charge count
    cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,))
    charge_count = cur.fetchone()[0]

    flag = ""
    if path_style:
        flag = " [PATH-KEY]"
    if iso_dupes:
        flag += f" [HAS-ISO-DUPE: {iso_dupes[0][0]}={iso_dupes[0][1]}]"
    if not parsed:
        flag += " [UNPARSEABLE]"

    print(
        f"  id={vid:5d}  charges={charge_count:3d}  "
        f"parsed={parsed or 'FAIL'!r}  "
        f"raw={eff_start!r:.40s}  "
        f"family={fkey:.60s}{flag}"
    )

conn.close()
