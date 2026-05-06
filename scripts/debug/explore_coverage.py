import sqlite3
import json
import logging

db_path = 'data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== Coverage by Tariff Family ===")
c = conn.cursor()
c.execute('''
    SELECT tf.state, tf.company, tf.family_type, count(tf.id) as num_families,
           sum(case when tv.id is not null then 1 else 0 end) as total_versions,
           count(tv.historical_document_id) as total_historical_versions
    FROM tariff_families tf
    LEFT JOIN tariff_versions tv ON tf.family_key = tv.family_key
    GROUP BY tf.state, tf.company, tf.family_type
    ORDER BY tf.state, tf.company
''')
for r in c.fetchall():
    print(dict(r))

print("\n=== Families with Low Historical Coverage (DEP/DEC) ===")
c.execute('''
    SELECT tf.family_key, tf.title, count(tv.historical_document_id) as hist_count
    FROM tariff_families tf
    LEFT JOIN tariff_versions tv ON tf.family_key = tv.family_key
    WHERE tf.company IN ('progress', 'carolinas') AND tf.state = 'NC'
    GROUP BY tf.family_key
    HAVING hist_count < 2
    ORDER BY hist_count ASC, tf.family_key
    LIMIT 20
''')
for r in c.fetchall():
    print(f"{r['family_key']}: {r['title']} (Historical Versions: {r['hist_count']})")

print("\n=== Multi-Leaf PDFs available for mining in NCUC ===")
c.execute('''
    SELECT id, docket_number, filing_date, file_size_bytes, referenced_leaf_nos_json 
    FROM ncuc_discovery_records 
    WHERE fetch_status = 'success' 
    LIMIT 50
''')
found = 0
for r in c.fetchall():
    leaves = json.loads(r['referenced_leaf_nos_json'])
    if len(leaves) > 5:
        print(f"ID: {r['id']} | Docket: {r['docket_number']} | Date: {r['filing_date']} | Leaves: {len(leaves)} | Size: {r['file_size_bytes']} bytes")
        found += 1
        if found >= 10: break

conn.close()
