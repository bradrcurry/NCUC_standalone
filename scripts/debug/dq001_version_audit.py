"""Find which profile produced the phantom rows for DQ-001 families."""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

# Get the version_ids for the phantom rows
cur.execute("""
    SELECT DISTINCT version_id, family_key, source_snippet, charge_label, rate_value
    FROM tariff_charges
    WHERE family_key IN (
        'nc-carolinas-schedule-SGS', 'nc-carolinas-schedule-I', 'nc-carolinas-schedule-PG',
        'nc-carolinas-schedule-TS',
        'nc-carolinas-doc-SCHEDULEWC',
        'nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE',
        'nc-carolinas-doc-SCHEDULEOPTE'
    )
    AND charge_type = 'adjustment' AND rate_value IN (-1.0, -8.0)
    LIMIT 10
""")
print("Phantom rows details:")
for r in cur.fetchall():
    print(f"  version_id={r[0]}  family={r[1]}")
    print(f"  label={r[3]!r}  val={r[4]}")
    print(f"  snippet={r[2]!r:.200s}")
    print()

# Get version details for these version_ids
version_ids = set()
cur.execute("""
    SELECT DISTINCT version_id FROM tariff_charges
    WHERE family_key IN (
        'nc-carolinas-schedule-SGS', 'nc-carolinas-schedule-I',
        'nc-carolinas-schedule-PG', 'nc-carolinas-schedule-TS',
        'nc-carolinas-doc-SCHEDULEWC',
        'nc-carolinas-doc-SCHEDULEWCRESIDENTIALWATERHEATINGSERVICE',
        'nc-carolinas-doc-SCHEDULEOPTE'
    ) AND charge_type = 'adjustment' AND rate_value IN (-1.0, -8.0)
""")
vids = [r[0] for r in cur.fetchall()]
print(f"\nVersion IDs with phantom rows: {vids[:10]}")

cur.execute("PRAGMA table_info(tariff_versions)")
cols = [r[1] for r in cur.fetchall()]
if vids:
    placeholders = ",".join("?" * len(vids[:5]))
    cur.execute(
        f"SELECT * FROM tariff_versions WHERE id IN ({placeholders})",
        vids[:5],
    )
    print("\nSample version records:")
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        print(f"\n  id={d['id']} family={d['family_key']} eff_start={d.get('effective_start')!r}")
        for k, v in d.items():
            if v is not None and k not in ('id', 'family_key', 'effective_start'):
                print(f"    {k}={v!r:.120s}")

conn.close()
