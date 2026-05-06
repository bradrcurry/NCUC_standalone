import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
for family in ['nc-carolinas-rider-STS','nc-carolinas-rider-EDPR','nc-progress-leaf-602']:
    rows = conn.execute('''
      SELECT hd.id, hd.local_path, hd.effective_start,
             COUNT(pa.id) as page_count
      FROM historical_documents hd
      LEFT JOIN ncuc_page_artifacts pa ON pa.source_pdf = hd.local_path
      WHERE hd.family_key = ?
      GROUP BY hd.id
    ''', (family,)).fetchall()
    print(f'=== {family} ===')
    for r in rows:
        path = r[1] or ''
        fname = path.split('/')[-1].split('\\')[-1]
        print(f'  doc {r[0]} | pages={r[3]} | eff={r[2]} | {fname[:65]}')
conn.close()
