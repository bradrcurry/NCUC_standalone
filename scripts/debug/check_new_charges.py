import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')

# Total
total = conn.execute('SELECT COUNT(*) FROM tariff_charges').fetchone()[0]
print(f'Total charges: {total:,}')

# Key families
families = [
    'nc-progress-leaf-602', 'nc-progress-leaf-604', 'nc-progress-leaf-607',
    'nc-progress-leaf-608', 'nc-progress-leaf-613',
    'nc-carolinas-rider-sts', 'nc-carolinas-rider-edpr',
    'nc-carolinas-rider-rdm', 'nc-carolinas-rider-pim', 'nc-carolinas-rider-edit4',
]
print('\nKey families:')
for fam in families:
    cnt = conn.execute('SELECT COUNT(*) FROM tariff_charges WHERE family_key = ?', (fam,)).fetchone()[0]
    print(f'  {fam}: {cnt}')

# Check if newly downloaded files are in historical_documents
print('\nNewly downloaded files in historical_documents:')
rows = conn.execute('''
    SELECT hd.id, hd.family_key, hd.effective_start, hd.title
    FROM historical_documents hd
    JOIN ncuc_discovery_records ndr ON (
        ndr.content_hash IS NOT NULL AND hd.content_hash = ndr.content_hash
    )
    WHERE ndr.fetch_status = 'downloaded'
    ORDER BY hd.effective_start
    LIMIT 20
''').fetchall()
for r in rows:
    print(f'  doc {r[0]} | {r[1]} | {r[2]} | {r[3][:50] if r[3] else None}')

# Check ncuc_page_artifacts for newly downloaded files
print('\nPage artifacts for newly downloaded files:')
rows2 = conn.execute('''
    SELECT DISTINCT source_pdf, COUNT(*) as pages
    FROM ncuc_page_artifacts
    WHERE source_pdf LIKE '%ncuc_tariff%' OR source_pdf LIKE '%downloads%'
    GROUP BY source_pdf
    ORDER BY source_pdf
''').fetchall()
for r in rows2:
    print(f'  {r[1]} pages: {r[0][:60]}')

conn.close()
