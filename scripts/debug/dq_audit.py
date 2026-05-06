"""Quick audit of all pending TD-DQ items from technical_debt.md."""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

# DQ-001: phantom -1.0 rider adjustment rows
families = (
    "nc-carolinas-schedule-SGS", "nc-carolinas-schedule-I",
    "nc-carolinas-schedule-PG", "nc-carolinas-schedule-TS",
    "nc-carolinas-doc-SCHEDULEWC",
    "nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE",
    "nc-carolinas-doc-SCHEDULEOPTE",
)
placeholders = ",".join("?" * len(families))
cur.execute(
    f"""SELECT family_key, COUNT(*) as cnt FROM tariff_charges
    WHERE charge_label = 'Rider Adjustment' AND rate_value = -1.0 AND rate_unit = '$/kWh'
    AND family_key IN ({placeholders})
    GROUP BY family_key""",
    families,
)
dq001 = cur.fetchall()
total_dq001 = sum(r[1] for r in dq001)
print(f"DQ-001 (phantom -1.0 rows): {total_dq001} total")
for r in dq001:
    print(f"  {r[0]}: {r[1]} rows")

# DQ-002: demand charge $1.00/kW
wc_families = (
    "nc-carolinas-doc-SCHEDULEWC",
    "nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE",
    "nc-carolinas-doc-SCHEDULEOPTE",
)
cur.execute(
    f"""SELECT family_key, COUNT(*) FROM tariff_charges
    WHERE charge_label = 'Demand Charge' AND rate_value = 1.0 AND rate_unit = '$/kW'
    AND family_key IN ({",".join("?" * len(wc_families))})
    GROUP BY family_key""",
    wc_families,
)
dq002 = cur.fetchall()
total_dq002 = sum(r[1] for r in dq002)
print(f"\nDQ-002 (demand $1.00/kW): {total_dq002} total")
for r in dq002:
    print(f"  {r[0]}: {r[1]} rows")

# DQ-003: BPM phantom rows
cur.execute(
    """SELECT COUNT(*) FROM tariff_charges
    WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
    AND (charge_label LIKE '%Electricity No.%'
         OR charge_label LIKE '%Effective November%'
         OR rate_value >= 0.02)"""
)
print(f"\nDQ-003 (BPM phantom rows): {cur.fetchone()[0]} rows")

# DQ-003b: Show total rows in BPM family for context
cur.execute(
    "SELECT COUNT(*) FROM tariff_charges WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'"
)
print(f"  Total BPM rows: {cur.fetchone()[0]}")

# DQ-004: leaf-501 version 5302 runaway TOU
cur.execute(
    "SELECT COUNT(*) FROM tariff_charges WHERE version_id = 5302 AND charge_label = 'Tou_Energy'"
)
print(f"\nDQ-004 (leaf-501 v5302 Tou_Energy): {cur.fetchone()[0]} rows")

# DQ-005: non-ISO dates
cur.execute(
    """SELECT id, family_key, effective_start FROM tariff_versions
    WHERE effective_start IS NOT NULL
    AND effective_start NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
    ORDER BY family_key"""
)
rows = cur.fetchall()
print(f"\nDQ-005 (non-ISO date versions): {len(rows)} rows")
for r in rows:
    print(f"  id={r[0]} family={r[1]} effective_start={r[2]!r}")

# Extra: check total tariff_charges count
cur.execute("SELECT COUNT(*) FROM tariff_charges")
print(f"\nTotal tariff_charges: {cur.fetchone()[0]:,}")

conn.close()
