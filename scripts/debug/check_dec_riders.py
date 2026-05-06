import sqlite3, json
conn = sqlite3.connect('data/db/duke_rates.db')

print('=== DEC Rider charge summary ===')
rows = conn.execute('''
    SELECT family_key, COUNT(*) as charges
    FROM tariff_charges
    WHERE family_key LIKE 'nc-carolinas-rider-%'
    GROUP BY family_key
    ORDER BY family_key
''').fetchall()
for r in rows:
    print(f'  {r[0]}: {r[1]}')

print('\n=== DEC historical_documents ===')
rows2 = conn.execute('''
    SELECT id, family_key, effective_start, title, parsed_result_json
    FROM historical_documents
    WHERE family_key LIKE 'nc-carolinas-rider-%'
    ORDER BY family_key, effective_start
''').fetchall()
for r in rows2:
    parsed = json.loads(r[4]) if r[4] else {}
    status = parsed.get('bulk_extract_status', parsed.get('status', ''))
    charges_count = parsed.get('charges_count', parsed.get('extracted_charges', ''))
    print(f'  doc {r[0]} | {r[1]} | {r[2]} | status={status} | charges={charges_count}')

print(f'\nTotal DEC rider docs: {len(rows2)}')
conn.close()
