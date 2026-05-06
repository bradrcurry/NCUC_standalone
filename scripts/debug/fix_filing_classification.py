"""Fix filing_classification values in ncuc_discovery_records."""
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')

# Check what we have
rows = conn.execute(
    "SELECT filing_classification, COUNT(*) FROM ncuc_discovery_records GROUP BY filing_classification"
).fetchall()
print("Current filing_classification values:")
for r in rows:
    print(f"  {r[0]}: {r[1]}")

# Fix 'compliance_tariff' -> 'tariff_sheets'
n = conn.execute(
    "UPDATE ncuc_discovery_records SET filing_classification = 'tariff_sheets' WHERE filing_classification = 'compliance_tariff'"
).rowcount
conn.commit()
print(f"\nFixed {n} rows: 'compliance_tariff' -> 'tariff_sheets'")

# Verify
rows2 = conn.execute(
    "SELECT filing_classification, COUNT(*) FROM ncuc_discovery_records GROUP BY filing_classification"
).fetchall()
print("\nUpdated filing_classification values:")
for r in rows2:
    print(f"  {r[0]}: {r[1]}")

conn.close()
