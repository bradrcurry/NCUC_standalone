import sqlite3

db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)

tables = ['ncuc_discovery_records', 'historical_documents', 'tariff_versions', 'tariff_families']

for table in tables:
    print(f"\n--- {table} ---")
    try:
        res = conn.execute(f"PRAGMA table_info({table})")
        for col in res:
            print(f"  {col[1]} ({col[2]})")
    except Exception as e:
        print(f"Error: {e}")
