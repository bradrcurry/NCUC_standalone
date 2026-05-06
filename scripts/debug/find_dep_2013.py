import sqlite3

db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)

print("\n--- Discovery Records Progress 2013 ---")
for r in conn.execute("SELECT id, docket_number, filing_title, filing_date FROM ncuc_discovery_records WHERE (filing_title LIKE '%Progress%' OR filing_title LIKE '%DEP%') AND filing_date LIKE '%2013%'"):
    print(r)
