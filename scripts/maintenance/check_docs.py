import sqlite3, json
conn = sqlite3.connect('data/db/duke_rates.db')
conn.row_factory = sqlite3.Row
count = conn.execute("SELECT COUNT(*) FROM historical_documents").fetchone()[0]
print(f"Total historical documents: {count}")
sample = conn.execute("SELECT * FROM historical_documents ORDER BY id DESC LIMIT 2").fetchall()
print(json.dumps([dict(d) for d in sample], default=str, indent=2))
conn.close()
