"""Detailed BPM audit — clean output for analysis."""
import sqlite3

conn = sqlite3.connect("data/db/duke_rates.db")
cur = conn.cursor()

FAMILY = "nc-carolinas-rider-BPMPROSPECTIVERIDER"

# ---- Phantom breakdown ----
print("=" * 80)
print("PHANTOM ROWS (to be deleted)")
print("=" * 80)
cur.execute(
    """
    SELECT charge_label, rate_value, rate_unit, COUNT(*) as cnt
    FROM tariff_charges
    WHERE family_key = ?
      AND (
        charge_label LIKE '%Electricity No.%'
        OR charge_label LIKE '%Effective November%'
        OR rate_value >= 0.02
      )
    GROUP BY charge_label, rate_value, rate_unit
    ORDER BY cnt DESC
    """,
    (FAMILY,),
)
for r in cur.fetchall():
    print(f"  cnt={r[3]:5d}  val={r[1]:+.6f}  unit={r[2]:6s}  label={r[0]!r:.80s}")

# ---- Surviving rows ----
print()
print("=" * 80)
print("SURVIVING ROWS (after delete)")
print("=" * 80)
cur.execute(
    """
    SELECT charge_label, rate_value, rate_unit, COUNT(*) as cnt
    FROM tariff_charges
    WHERE family_key = ?
      AND NOT (
        charge_label LIKE '%Electricity No.%'
        OR charge_label LIKE '%Effective November%'
        OR rate_value >= 0.02
      )
    GROUP BY charge_label, rate_value, rate_unit
    ORDER BY cnt DESC
    """,
    (FAMILY,),
)
rows = cur.fetchall()
for r in rows:
    print(f"  cnt={r[3]:5d}  val={r[1]:+.6f}  unit={r[2]:6s}  label={r[0]!r:.80s}")
print(f"\n  Total surviving: {sum(r[3] for r in rows)}")

# ---- Source snippet for key phantom types ----
print()
print("=" * 80)
print("PHANTOM SOURCE SNIPPETS (diagnosis)")
print("=" * 80)
# Electricity No. phantom
cur.execute(
    """SELECT charge_label, rate_value, source_snippet
       FROM tariff_charges
       WHERE family_key = ? AND charge_label LIKE '%Electricity No.%'
       LIMIT 1""",
    (FAMILY,),
)
r = cur.fetchone()
if r:
    print(f"\n[Electricity No. phantom]")
    print(f"  label  = {r[0]!r}")
    print(f"  value  = {r[1]}")
    print(f"  snippet= {r[2]!r}")

# Leaf No. phantom (rate >= 0.02 but not Electricity No.)
cur.execute(
    """SELECT charge_label, rate_value, source_snippet
       FROM tariff_charges
       WHERE family_key = ?
         AND rate_value >= 0.02
         AND charge_label NOT LIKE '%Electricity No.%'
         AND charge_label NOT LIKE '%Effective November%'
       LIMIT 3""",
    (FAMILY,),
)
for r in cur.fetchall():
    print(f"\n[High-value phantom (rate >= 0.02)]")
    print(f"  label  = {r[0]!r}")
    print(f"  value  = {r[1]}")
    print(f"  snippet= {r[2]!r}")

conn.close()
