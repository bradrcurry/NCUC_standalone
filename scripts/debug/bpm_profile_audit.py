"""Get BPM parser profile info via tariff_versions schema inspection."""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

# Check what columns tariff_versions has
cur.execute("PRAGMA table_info(tariff_versions)")
cols = [r[1] for r in cur.fetchall()]
print("tariff_versions columns:", cols)

# Get versions for BPM family
cur.execute(
    """
    SELECT *
    FROM tariff_versions
    WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
    LIMIT 5
    """
)
rows = cur.fetchall()
if rows:
    print("\nSample BPM tariff_versions:")
    for r in rows:
        d = dict(zip(cols, r))
        print(f"  id={d['id']} source={d.get('source')!r} eff_start={d.get('effective_start')!r}")
        print(f"    profile={d.get('profile_key','N/A')!r}")
        # Print all non-null values
        for k, v in d.items():
            if v is not None and k not in ('id', 'source', 'effective_start'):
                print(f"    {k}={v!r}")
else:
    print("No tariff_versions found for BPM family")

# Check tariff_charges for profile info
cur.execute("PRAGMA table_info(tariff_charges)")
charge_cols = [r[1] for r in cur.fetchall()]
print("\ntariff_charges columns:", charge_cols)

# Get a legitimate BPM charge row
cur.execute(
    """
    SELECT *
    FROM tariff_charges
    WHERE family_key = 'nc-carolinas-rider-BPMPROSPECTIVERIDER'
      AND rate_value < 0.02
      AND rate_value > -0.02
    LIMIT 1
    """
)
r = cur.fetchone()
if r:
    d = dict(zip(charge_cols, r))
    print("\nSample legitimate BPM charge:")
    for k, v in d.items():
        if v is not None:
            print(f"  {k}={v!r:.120s}")

conn.close()
