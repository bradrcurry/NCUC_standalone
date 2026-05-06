import sqlite3
db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
print("\n--- All Discovery Records for E-2 Sub 1023 ---")
for r in conn.execute("SELECT id, filing_title, filing_date, discovered_url FROM ncuc_discovery_records WHERE docket_number LIKE '%1023%'"):
    print(r)
