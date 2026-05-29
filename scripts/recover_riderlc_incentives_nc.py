"""Manual extraction of DEC RIDER LC (Residential Equipment Control) incentives.

RIDER LC is a residential load-control program with a complex incentive
schedule (Initial Incentive + Annual Incentive for HVAC, water heater, and
battery participation). The OCR output of the official tariff sheet
(NC Ninth Revised Leaf No. 71, eff. 2025-01-01, E-7 Sub 1032) is heavily
mangled — the table cells render as fragments like "$50* 56", "\\$50 40",
"$150 50" with cross-line splits and missing column boundaries.

Writing a robust parser regex for this is impractical without first cleaning
up the OCR output. Until that happens, this script inserts the canonical
incentive values manually for the 2025-01-01 RIDERLC version. The values
come from the published tariff sheet:

  https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-nc/
    ncriderlc-pdf.pdf

The incentives are CREDITS to participating customers, not c/kWh adders, so
they don't contribute to bill-summary reconciliation — but they document
real per-customer credit amounts that can support bill reconstruction for
RIDERLC participants.

Idempotent. Re-run after extract-rates-nc to restore charges that the bulk
extractor's carolinas_single_value_rider profile drops.
"""
import datetime as dt
import sqlite3

DB = "data/db/duke_rates.db"


# Per the E-7 Sub 1032 / Ninth Revised Leaf No. 71 RIDER LC tariff sheet:
# (label, value, unit, season, customer_class, notes)
INCENTIVES = [
    ("AC/Heat Pump Load Control Device - Initial Incentive (Non-Winter)",
     50.0, "$/event", "non_winter", "residential", "*Limited participation for non-winter months"),
    ("AC/Heat Pump Load Control Device - Annual Incentive (Non-Winter)",
     56.0, "$/year", "non_winter", "residential", None),
    ("Heat Strip Load Control Device - Initial Incentive (Winter)",
     50.0, "$/event", "winter", "residential", None),
    ("Heat Strip Load Control Device - Annual Incentive (Winter)",
     40.0, "$/year", "winter", "residential", None),
    ("Thermostat Internet Connected - Annual Incentive (Non-Winter)",
     50.0, "$/year", "non_winter", "residential",
     "Closed to new participants for the Non-Winter option"),
    ("Thermostat Internet Connected - Initial Incentive (Winter Focused)",
     150.0, "$/event", "winter", "residential", None),
    ("Thermostat Internet Connected - Annual Incentive (Winter Focused)",
     50.0, "$/year", "winter", "residential", None),
    ("Water Heater Load Control Device - Initial Incentive",
     25.0, "$/event", "all_year", "residential", None),
    ("Water Heater Load Control Device - Annual Incentive",
     25.0, "$/year", "all_year", "residential", None),
    ("Water Heater Internet Connected - Initial Incentive",
     25.0, "$/event", "all_year", "residential", None),
    ("Water Heater Internet Connected - Annual Incentive",
     25.0, "$/year", "all_year", "residential", None),
    ("Battery Internet Connected - Monthly Incentive (per kW nameplate)",
     6.50, "$/kW-month", "all_year", "residential",
     "Based on nameplate continuous discharge capacity adjusted by EM&V capability factor"),
]


def main():
    db = sqlite3.connect(DB)
    c = db.cursor()
    c.execute("""SELECT id FROM tariff_versions
                 WHERE family_key='nc-carolinas-rider-RIDERLC'
                   AND effective_start='2025-01-01'
                   AND status NOT IN ('pending_document', 'misregistered_document')""")
    row = c.fetchone()
    if not row:
        print("RIDERLC 2025-01-01 version not found.")
        return
    vid = row[0]

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    for label, value, unit, season, cls, notes in INCENTIVES:
        c.execute("""SELECT id FROM tariff_charges
                     WHERE version_id=? AND charge_label=? AND ABS(rate_value-?) < 1e-6""",
                  (vid, label, value))
        if c.fetchone():
            continue
        full_notes = "Manually inserted — RIDER LC incentive table OCR-mangled; published tariff sheet ncriderlc-pdf.pdf"
        if notes:
            full_notes += "; " + notes
        c.execute("""INSERT INTO tariff_charges
            (version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
             season, customer_class, source_snippet, confidence_score, notes, created_at)
            VALUES (?, 'nc-carolinas-rider-RIDERLC', 'incentive', ?, ?, ?, ?, ?, ?, 0.85, ?, ?)""",
            (vid, label, value, unit, season, cls, label, full_notes, now))
        inserted += 1

    db.commit()
    print(f"Inserted {inserted} RIDERLC incentives for 2025-01-01 (version {vid})")


if __name__ == "__main__":
    main()
