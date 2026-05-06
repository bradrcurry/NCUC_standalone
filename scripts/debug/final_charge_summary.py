import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')

total = conn.execute('SELECT COUNT(*) FROM tariff_charges').fetchone()[0]
print(f'Total charges: {total:,}')

dep_total = conn.execute(
    "SELECT COUNT(*) FROM tariff_charges WHERE family_key LIKE 'nc-progress-%'"
).fetchone()[0]
dec_total = conn.execute(
    "SELECT COUNT(*) FROM tariff_charges WHERE family_key LIKE 'nc-carolinas-%'"
).fetchone()[0]
print(f'DEP NC: {dep_total:,}')
print(f'DEC NC: {dec_total:,}')

print('\n=== Key rider families ===')
key_families = [
    'nc-progress-leaf-602', 'nc-progress-leaf-604', 'nc-progress-leaf-607',
    'nc-progress-leaf-608', 'nc-progress-leaf-613',
    'nc-carolinas-rider-STS', 'nc-carolinas-rider-EDPR',
    'nc-carolinas-rider-RDM', 'nc-carolinas-rider-PIM', 'nc-carolinas-rider-EDIT4',
]
for fam in key_families:
    cnt = conn.execute('SELECT COUNT(*) FROM tariff_charges WHERE family_key = ?', (fam,)).fetchone()[0]
    print(f'  {fam}: {cnt}')

conn.close()
