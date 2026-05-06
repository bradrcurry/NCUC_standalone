import json
from duke_rates.config import get_settings
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document

conn = connect(get_settings().database_path)
conn.row_factory = __import__('sqlite3').Row
row = conn.execute("SELECT * FROM ncuc_discovery_records WHERE id=1124").fetchone()
conn.close()

local_path = row["local_path"]
print(f"File: {local_path}")
t = triage_pdf(local_path)
print(f"Triage: route={t.route_recommendation}, class={t.likely_document_class}")

pages = mine_document_pages(local_path)
print(f"Miner: mined {len(pages)} pages")
spans = segment_document(pages, parent_discovery_id=1124)
print(f"Spans: formed {len(spans)}")
for s in spans:
    print(f" Span {s.start_page}-{s.end_page}: {s.doc_type}, {s.extracted_schedule_titles}, {s.extracted_leaf_nos}")
