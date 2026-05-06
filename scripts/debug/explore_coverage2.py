import sqlite3
import json

db_path = 'data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== All Discovery Records ===")
c = conn.cursor()
c.execute("select count(*) from ncuc_discovery_records")
print("Total records:", c.fetchone()[0])

c.execute("select count(*) from ncuc_discovery_records where fetch_status = 'success'")
print("Total successful fetch:", c.fetchone()[0])

print("\n=== Multi-Leaf PDFs available for mining in NCUC ===")
c.execute('''
    SELECT id, docket_number, filing_date, file_size_bytes, referenced_leaf_nos_json 
    FROM ncuc_discovery_records 
    WHERE fetch_status = 'success' 
''')
found = 0
for r in c.fetchall():
    try:
        leaves = json.loads(r['referenced_leaf_nos_json'])
        if len(leaves) > 5:
            print(f"ID: {r['id']} | Docket: {r['docket_number']} | Date: {r['filing_date']} | Leaves: {len(leaves)} | Size: {r['file_size_bytes']} bytes")
            found += 1
            if found >= 10: break
    except:
        pass
if found == 0:
    print("None found matching > 5 leaves.")

print("\n=== Let's look at one successful fetch ===")
c.execute("SELECT * FROM ncuc_discovery_records WHERE fetch_status = 'success' LIMIT 1")
row = c.fetchone()
if row:
    print(dict(row))
else:
    print("No successful fetches found!")
    
conn.close()
