"""Test DEC rider extraction using nc_carolinas parser or nc_progress parser."""
import sqlite3
from duke_rates.parse.nc_progress import parse_nc_progress_leaf

conn = sqlite3.connect('data/db/duke_rates.db')

tests = [
    ('nc-carolinas-rider-RDM', 'RDM'),
    ('nc-carolinas-rider-PIM', 'PIM'),
    ('nc-carolinas-rider-MRM', 'MRM'),
    ('nc-carolinas-rider-EDIT4', 'EDIT4'),
]

for fk, name in tests:
    row = conn.execute("""
        SELECT pa.text_content
        FROM ncuc_page_artifacts pa
        JOIN historical_documents hd ON hd.local_path = pa.source_pdf
        WHERE hd.family_key = ?
        AND (hd.local_path LIKE '%-rider-%' OR hd.local_path LIKE '%ncride%')
        ORDER BY pa.page_number
        LIMIT 1
    """, (fk,)).fetchone()
    if not row:
        print(f'\n=== {name}: NO PAGE FOUND ===')
        continue

    text = row[0]
    version, charges, riders = parse_nc_progress_leaf(
        text, version_id=99999, family_key=fk
    )
    print(f'\n=== {name}: {len(charges)} charges ===')
    for c in charges:
        print(f'  {c.customer_class} | {c.rate_value} {c.rate_unit} | {c.charge_type} | snippet: {c.source_snippet[:60]}')

conn.close()
