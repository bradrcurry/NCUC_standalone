import sqlite3
db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
res = conn.execute("PRAGMA table_info(historical_documents)")
for col in res:
    print(f"  {col[1]} ({col[2]})")
