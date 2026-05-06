"""
Further diagnostics:
- DQ-001: understand the -8.0 rider adjustment (also suspicious) and confirm exact delete criteria
- DQ-004: understand v5302 source and what a correct re-extraction should yield
- DQ-001 root cause: look at which profile produces -1.0 rows
"""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

# ----- DQ-001 deeper -----
print("=== DQ-001: All Rider Adjustment rows in these families ===")
cur.execute("""
    SELECT family_key, charge_label, rate_value, rate_unit, COUNT(*) as cnt
    FROM tariff_charges
    WHERE family_key IN (
        'nc-carolinas-doc-SCHEDULEOPTE',
        'nc-carolinas-doc-SCHEDULEWC',
        'nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE',
        'nc-carolinas-schedule-I',
        'nc-carolinas-schedule-PG',
        'nc-carolinas-schedule-SGS',
        'nc-carolinas-schedule-TS'
    )
    AND charge_type = 'adjustment'
    GROUP BY family_key, charge_label, rate_value, rate_unit
    ORDER BY family_key, rate_value
""")
for r in cur.fetchall():
    print(f"  {r[0][:50]:50s}  {r[1]:25s}  {r[2]:+.4f} {r[3]:6s}  cnt={r[4]}")

# Are there any legitimate Rider Adjustment rows with other values?
print("\n=== Non-(-1.0) non-(-8.0) rider adjustment rows ===")
cur.execute("""
    SELECT family_key, charge_label, rate_value, rate_unit, COUNT(*) as cnt
    FROM tariff_charges
    WHERE family_key IN (
        'nc-carolinas-doc-SCHEDULEOPTE',
        'nc-carolinas-doc-SCHEDULEWC',
        'nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE',
        'nc-carolinas-schedule-I',
        'nc-carolinas-schedule-PG',
        'nc-carolinas-schedule-SGS',
        'nc-carolinas-schedule-TS'
    )
    AND charge_type = 'adjustment'
    AND rate_value NOT IN (-1.0, -8.0)
    GROUP BY family_key, charge_label, rate_value, rate_unit
    ORDER BY family_key, rate_value
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  {r[0][:50]:50s}  {r[1]:25s}  {r[2]:+.4f} {r[3]:6s}  cnt={r[4]}")
else:
    print("  None")

# Source snippet for -8.0 rows
print("\n=== Source snippet for -8.0 Rider Adjustment rows ===")
cur.execute("""
    SELECT DISTINCT source_snippet
    FROM tariff_charges
    WHERE family_key IN (
        'nc-carolinas-doc-SCHEDULEOPTE',
        'nc-carolinas-doc-SCHEDULEWC'
    ) AND charge_type = 'adjustment' AND rate_value = -8.0
    LIMIT 2
""")
for r in cur.fetchall():
    print(f"  snippet={r[0]!r:.300s}")

# ----- DQ-004: v5302 -----
print("\n=== DQ-004: v5302 source info ===")
cols_q = "PRAGMA table_info(tariff_versions)"
cur.execute(cols_q)
cols = [r[1] for r in cur.fetchall()]
cur.execute("SELECT * FROM tariff_versions WHERE id = 5302")
row = cur.fetchone()
d = dict(zip(cols, row))
for k, v in d.items():
    if v is not None:
        print(f"  {k}={v!r:.120s}")

# What document is v5302 linked to?
if d.get("historical_document_id"):
    cur.execute("PRAGMA table_info(historical_documents)")
    hdcols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM historical_documents WHERE id = ?", (d["historical_document_id"],))
    hd = cur.fetchone()
    if hd:
        hd_dict = dict(zip(hdcols, hd))
        print("\n  Linked historical_document:")
        for k, v in hd_dict.items():
            if v is not None:
                print(f"    {k}={v!r:.120s}")

# Version 5301 (previous good version) source snippet samples
print("\n=== v5301 sample charges (2014-06-01, the preceding good version) ===")
cur.execute("""
    SELECT charge_label, rate_value, rate_unit, tou_period, COUNT(*) as cnt
    FROM tariff_charges WHERE version_id = 5301
    GROUP BY charge_label, rate_value, rate_unit, tou_period
    ORDER BY cnt DESC
    LIMIT 25
""")
for r in cur.fetchall():
    print(f"  {r[0]:35s}  {r[1]:+.5f} {r[2]:8s}  tou={r[3]}  cnt={r[4]}")

conn.close()
