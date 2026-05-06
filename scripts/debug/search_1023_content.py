import fitz
import os

pdf_dir = 'c:/Python/Duke/Standalone/data/historical/ncuc/e-2-sub-1023'
files = os.listdir(pdf_dir)

for f in files:
    if not f.endswith('.pdf'): continue
    path = os.path.join(pdf_dir, f)
    try:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            text = page.get_text()
            if "2013" in text:
                print(f"Found '2013' in {f} page {i+1}")
                # Print a bit of context
                idx = text.find("2013")
                print(f"Context: {text[max(0, idx-50):idx+50].replace('\n', ' ')}")
                # Check for RES-24
                if "RES-24" in text:
                    print(f"!!! FOUND RES-24 in {f} page {i+1}")
        doc.close()
    except Exception as e:
        print(f"Error reading {f}: {e}")
