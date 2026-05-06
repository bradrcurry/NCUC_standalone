from duke_rates.db.repository import Repository
from duke_rates.parse.pdf_text import extract_pdf_text
from pathlib import Path
import sqlite3
import re

repo = Repository(Path("c:/Python/Duke/Standalone/data/db/duke_rates.db"))

states_companies = [('FL', 'florida'), ('NC', 'progress'), ('NC', 'carolinas')]

with sqlite3.connect("c:/Python/Duke/Standalone/data/db/duke_rates.db") as conn:
    cursor = conn.cursor()
    for state, company in states_companies:
        print(f"\n=====================================")
        print(f"--- SAMPLE {state} {company.upper()} ---")
        print(f"=====================================")
        
        # Find families that actually have charges
        pattern = f"%{state.lower()}-{company.lower()}%"
        if company == "progress":
            pattern = f"%{state.lower()}-progress%"
        elif company == "florida":
            pattern = f"%fl-florida%"
        elif company == "carolinas":
            pattern = f"%{state.lower()}-carolinas%"
            
        cursor.execute("SELECT DISTINCT family_key FROM tariff_charges WHERE family_key LIKE ? LIMIT 2", (pattern,))
        # also we need to verify the family string
        fkeys = [r[0] for r in cursor.fetchall()]
        
        for fkey in fkeys:
            # We need the document ID
            cursor.execute("SELECT current_document_id FROM tariff_families WHERE family_key = ?", (fkey,))
            res = cursor.fetchone()
            if not res or not res[0]: continue
            
            doc = repo.get_document(res[0])
            if not doc or not doc.local_path: continue
            
            pdf_path = Path(doc.local_path)
            if not pdf_path.is_file(): continue
                
            print(f"\n--- Family: {fkey} ---")
            print(f"PDF Path: {pdf_path.name}")
            
            # Original text preview
            text = extract_pdf_text(pdf_path)
            
            # Show parsed charges
            print(f"--- PARSED CHARGES FROM DB ---")
            cursor.execute("SELECT charge_label, rate_value, rate_unit, charge_type, tier_min, tier_max, source_snippet FROM tariff_charges WHERE family_key = ?", (fkey,))
            charges = cursor.fetchall()
            for r in charges:
                print(f"  {r[3]:<12} | {r[0]:<40} | {r[1]} {r[2]} | min: {r[4]} max: {r[5]}")
            
            print(f"\n--- TEXT CONTEXT AROUND CHARGES ---")
            for r in charges:
                snippet = r[6]
                if snippet:
                    snippet_clean = snippet.replace('\n', ' ')
                    print(f"  [Snippet]: {snippet_clean}")
            
            print("\n" + "-"*60)
