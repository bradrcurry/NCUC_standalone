import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT hd.id, hd.family_key, hd.leaf_no, hd.effective_start, hd.title, hd.local_path
  FROM historical_documents hd
  WHERE hd.family_key = 'nc-carolinas-rider-STS'
  ORDER BY hd.effective_start
''').fetchall()
print('=== DEC nc-carolinas-rider-STS docs ===')
for r in rows:
    path = (r[5] or '').split('/')[-1].split('\\')[-1]
    print(f'  doc {r[0]} | leaf={r[2]} | eff={r[3]} | title={r[4][:50] if r[4] else None}')
    print(f'    path: {path[:65]}')

# Also look for any STS-named docs in DEC that might be separate storm riders
rows2 = conn.execute('''
  SELECT hd.id, hd.family_key, hd.leaf_no, hd.effective_start, hd.title
  FROM historical_documents hd
  WHERE hd.state = 'NC' AND hd.company = 'carolinas'
  AND (hd.title LIKE '%Storm%' OR hd.title LIKE '%Securitization%' OR hd.leaf_no = '133')
  ORDER BY hd.effective_start
''').fetchall()
print('\n=== All DEC Storm-related docs ===')
for r in rows2:
    print(f'  doc {r[0]} | family={r[1]} | leaf={r[2]} | eff={r[3]} | title={r[4][:60] if r[4] else None}')
conn.close()
