import fitz
import os

pdf_dir = 'c:/Python/Duke/Standalone/data/historical/ncuc/e-2-sub-1023'
files = [
    '04efc351-c973-4618-8f57-a1b7a2c71f33.pdf',
    '05f7300f-ed44-46d9-a20f-12dcf1ad470d.pdf',
    '966bf93a-dcd1-4412-96ae-d74b617fb7e2.pdf',
    'e36b8e24-5117-48e3-898e-1a35e3e5d7d7.pdf',
    'fb10c3f0-8c58-479f-aae5-e40f596e2123.pdf'
]

for f in files:
    path = os.path.join(pdf_dir, f)
    print(f"\n--- Analyzing {f} ---")
    try:
        doc = fitz.open(path)
        print(f"Pages: {len(doc)}")
        text = doc[0].get_text()
        print(f"First page text (start):\n{text[:500]}")
        doc.close()
    except Exception as e:
        print(f"Error reading {f}: {e}")
