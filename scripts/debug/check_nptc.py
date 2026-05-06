import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
rows = conn.execute('''
  SELECT hd.id, hd.family_key, hd.effective_start, hd.title, hd.local_path
  FROM historical_documents hd
  WHERE hd.local_path LIKE '%nptc%'
  ORDER BY hd.effective_start
''').fetchall()
print('=== NPTC docs ===')
for r in rows:
    path = (r[4] or '').replace('\\', '/').split('/')[-1]
    print(f'  doc {r[0]} | family={r[1]} | eff={r[2]} | title={r[3][:50] if r[3] else None}')
    print(f'    {path[:65]}')

# Check page text
rows2 = conn.execute('''
  SELECT pa.source_pdf, pa.page_number, pa.text_content
  FROM ncuc_page_artifacts pa
  WHERE pa.source_pdf LIKE '%nptc%'
  ORDER BY pa.source_pdf, pa.page_number
  LIMIT 4
''').fetchall()
print('\n=== NPTC page text ===')
for r in rows2:
    print(f'[p{r[1]}] {r[2][:400] if r[2] else "EMPTY"}')
conn.close()
