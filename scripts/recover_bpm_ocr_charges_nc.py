"""Recover BPM rider charges from OCR-mangled DEC tariff sheet.

The DEC BPM (Bulk Power Marketing) rider tariff sheet at
`data/raw/historical/ncuc/e-7/e-7-nodate-duke-energy-carolinas-llc-s-revised-bpm-rider-tariff-sheet.pdf`
has its cent-sign characters OCR-mangled to 'j!/kWh', 'f!/kWh', 'tf/kWh', 'ji/kWh', '*/kWh'.
The `carolinas_single_value_rider` parser profile cannot recognize these as ¢/kWh,
so it extracts 0 charges.

This script inserts the four manually-recovered values directly into `tariff_charges`
for the 2012-07-01 BPMPPTTRUEUP version. Idempotent — skips inserts that already exist.

The proper fix is a parser-level OCR cent-symbol normalization (similar to the
Carolinas TOU fix in profile parse_nc_carolinas_leaf). This script is a stopgap.
"""
import datetime as dt
import sqlite3

DB = "data/db/duke_rates.db"

# Recovered values from raw OCR text:
#   "Total Adjustment 0.0715 j!/kWh"  ← garbage cent
#   "BPM Net Revenues + Non-Firm Pt-to-Pt Transmission Rate Adjustment 0.0691 f!/kWh"
#   "Base BPM Net Revenues decrement 0.0642 tf/kWh"  (established Jan 1, 2010 in E-7 Sub 909)
#   "Non-Firm Point-to-Point Transmission Decrement 0.0067 ji/kWh"
CHARGES = [
    ("Total Adjustment", 0.000715, "$/kWh", "all",
     'OCR rendered as "0.0715 j!/kWh"; real unit is ¢/kWh; value is 0.0715 ¢/kWh'),
    ("Rate Adjustment - BPM Net Revenues + Non-Firm Pt-to-Pt Transmission",
     0.000691, "$/kWh", "all",
     'OCR rendered as "0.0691 f!/kWh"'),
    ("Base BPM Net Revenues Decrement (established 2010-01-01, E-7 Sub 909)",
     -0.000642, "$/kWh", "all",
     'OCR rendered as "0.0642 tf/kWh"'),
    ("Non-Firm Point-to-Point Transmission Decrement",
     -0.000067, "$/kWh", "all",
     'OCR rendered as "0.0067 ji/kWh"'),
]


def main():
    db = sqlite3.connect(DB)
    c = db.cursor()
    c.execute("""SELECT id FROM tariff_versions
                 WHERE family_key='nc-carolinas-rider-BPMPPTTRUEUP'
                   AND effective_start='2012-07-01'
                   AND status != 'pending_document'""")
    row = c.fetchone()
    if not row:
        print("BPMPPTTRUEUP 2012-07-01 version not found; nothing to do.")
        return
    vid = row[0]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    inserted = 0
    for label, value, unit, cls, snippet in CHARGES:
        c.execute("""SELECT id FROM tariff_charges
                     WHERE version_id=? AND charge_label=? AND ABS(rate_value-?) < 1e-6""",
                  (vid, label, value))
        if c.fetchone():
            continue
        c.execute("""INSERT INTO tariff_charges
            (version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
             customer_class, source_snippet, confidence_score, notes, created_at)
            VALUES (?, 'nc-carolinas-rider-BPMPPTTRUEUP', 'rider_adjustment', ?, ?, ?, ?, ?, 0.75,
                    'OCR cent-symbol recovered manually; parser profile cannot read mangled ¢/kWh', ?)""",
            (vid, label, value, unit, cls, snippet, now))
        inserted += 1

    db.commit()
    print(f"BPMPPTTRUEUP 2012-07-01 (version {vid}): inserted {inserted} charges")


if __name__ == "__main__":
    main()
