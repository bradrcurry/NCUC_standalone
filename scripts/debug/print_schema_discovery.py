import sqlite3

db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)

res = conn.execute(f"PRAGMA table_info(ncuc_discovery_records)")
for col in res:
    print(f"  {col[1]} ({col[2]})")
