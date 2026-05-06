import sqlite3
db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
print("\n--- Historical Documents for E-2 Sub 1023 ---")
for r in conn.execute("SELECT id, family_key, title, local_path FROM historical_documents WHERE local_path LIKE '%e-2-sub-1023%'"):
    print(r)
