import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')

# Get the full content for pages that have rate values
targets = [
    ('nc-carolinas-rider-RDM', 'RDM'),
    ('nc-carolinas-rider-PIM', 'PIM'),
    ('nc-carolinas-rider-MRM', 'MRM'),
    ('nc-carolinas-rider-EDIT4', 'EDIT4'),
]

for fk, name in targets:
    # Get the standalone tariff sheet page
    rows = conn.execute("""
        SELECT pa.page_number, pa.text_content, hd.local_path
        FROM ncuc_page_artifacts pa
        JOIN historical_documents hd ON hd.local_path = pa.source_pdf
        WHERE hd.family_key = ?
        AND (hd.local_path LIKE '%-rider-%' OR hd.local_path LIKE '%ncride%' OR hd.local_path LIKE '%ncschedule%')
        ORDER BY pa.page_number
        LIMIT 3
    """, (fk,)).fetchall()
    print(f'\n=== {fk} ({name}) - TARIFF SHEET PAGES ===')
    for r in rows:
        fname = r[2].split('\\')[-1].split('/')[-1]
        print(f'  [p{r[0]}] {fname[:60]}')
        print(r[1])
        print()
conn.close()
