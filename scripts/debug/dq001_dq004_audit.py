"""
Diagnose DQ-001 (phantom -1.0 Rider Adjustment rows) and DQ-004 (leaf-501 v5302 runaway TOU).
"""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

FAMILIES_DQ001 = (
    "nc-carolinas-schedule-SGS", "nc-carolinas-schedule-I",
    "nc-carolinas-schedule-PG", "nc-carolinas-schedule-TS",
    "nc-carolinas-doc-SCHEDULEWC",
    "nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE",
    "nc-carolinas-doc-SCHEDULEOPTE",
)

print("=" * 80)
print("DQ-001: Phantom -1.0 Rider Adjustment rows")
print("=" * 80)

# Count by family
placeholders = ",".join("?" * len(FAMILIES_DQ001))
cur.execute(
    f"""
    SELECT family_key, COUNT(*) as cnt
    FROM tariff_charges
    WHERE charge_label = 'Rider Adjustment'
      AND rate_value = -1.0
      AND rate_unit = '$/kWh'
      AND family_key IN ({placeholders})
    GROUP BY family_key
    """,
    FAMILIES_DQ001,
)
total = 0
for r in cur.fetchall():
    print(f"  {r[0]}: {r[1]} rows")
    total += r[1]
print(f"  Total: {total}")

# Sample source_snippets per family
print("\nSample source_snippets for phantom rows:")
for fkey in FAMILIES_DQ001[:4]:
    cur.execute(
        """
        SELECT source_snippet, charge_label, version_id
        FROM tariff_charges
        WHERE family_key = ? AND charge_label = 'Rider Adjustment'
          AND rate_value = -1.0
        LIMIT 2
        """,
        (fkey,),
    )
    rows = cur.fetchall()
    if rows:
        print(f"\n  [{fkey}]")
        for r in rows:
            print(f"    version_id={r[2]}")
            print(f"    label={r[1]!r}")
            print(f"    snippet={r[0]!r:.200s}")

# Check ALL charges in these families to understand the full picture
print("\nAll charge types in DQ-001 families (to confirm only phantom rows are -1.0):")
cur.execute(
    f"""
    SELECT family_key, charge_label, rate_value, rate_unit, COUNT(*) as cnt
    FROM tariff_charges
    WHERE family_key IN ({placeholders})
    GROUP BY family_key, charge_label, rate_value, rate_unit
    ORDER BY family_key, cnt DESC
    LIMIT 40
    """,
    FAMILIES_DQ001,
)
for r in cur.fetchall():
    print(f"  {r[0][:40]:40s}  {r[1]:30s}  {r[2]:+.4f} {r[3]:6s}  cnt={r[4]}")

print()
print("=" * 80)
print("DQ-004: leaf-501 v5302 runaway TOU rows")
print("=" * 80)

# v5302 details
cur.execute(
    """
    SELECT tv.id, tv.family_key, tv.effective_start, tv.effective_end, COUNT(tc.id) as charge_count
    FROM tariff_versions tv
    LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
    WHERE tv.id = 5302
    GROUP BY tv.id
    """
)
r = cur.fetchone()
if r:
    print(f"  version id={r[0]} family={r[1]} eff_start={r[2]!r} eff_end={r[3]!r} charges={r[4]}")

# Breakdown of charge types in v5302
cur.execute(
    """
    SELECT charge_label, rate_value, rate_unit, season, tou_period, COUNT(*) as cnt
    FROM tariff_charges WHERE version_id = 5302
    GROUP BY charge_label, rate_value, rate_unit, season, tou_period
    ORDER BY cnt DESC
    """
)
print("\n  Charge breakdown for v5302:")
for r in cur.fetchall():
    print(f"    label={r[0]!r:25s} val={r[1]} unit={r[2]} season={r[3]} tou={r[4]} cnt={r[5]}")

# Check neighboring versions for leaf-501 to understand expected structure
cur.execute(
    """
    SELECT tv.id, tv.effective_start, COUNT(tc.id) as charge_count,
           GROUP_CONCAT(DISTINCT tc.charge_label) as labels
    FROM tariff_versions tv
    LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
    WHERE tv.family_key = 'nc-progress-leaf-501'
    GROUP BY tv.id
    ORDER BY tv.effective_start
    """
)
print("\n  All nc-progress-leaf-501 versions:")
for r in cur.fetchall():
    labels = (r[3] or "")[:80]
    print(f"    id={r[0]:5d} eff_start={r[1]!r:15s} charges={r[2]:3d} labels={labels!r}")

conn.close()
