import sqlite3, os
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT family_keys_json, filing_title, filing_date, file_size_bytes, local_path
  FROM ncuc_discovery_records
  WHERE fetch_status = 'downloaded'
  ORDER BY filing_date, id
''').fetchall()
print(f'Downloaded records: {len(rows)}')
print()
for r in rows:
    families = r[0] or '[]'
    lp = r[4] or ''
    path = os.path.basename(lp) if lp else ''
    print(f'  {r[2]} | {families[:45]} | {r[1][:50]}')
    print(f'    {path[:65]} ({(r[3] or 0):,} bytes)')
conn.close()
