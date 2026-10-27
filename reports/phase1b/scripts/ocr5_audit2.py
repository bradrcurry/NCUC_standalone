"""Second-pass leading-5 audit using every text source we hold for a charge's PDF.

Pool = raw sidecar (.txt/.ocr.txt) + docling plain_text + docling tables json + page artifacts,
matched by the PDF basename.  For stored value v (v != 0):
  corrupting token: (?<!\\w)5<v-string>   (what the defect rewrote)
  clean token:      (?<![\\w.])<v-string> not preceded by a 5
Confidence:
  HIGH   - snippet has '$$', or corrupting tokens exist and NO clean token for v in the pool
  MEDIUM - both corrupting and clean tokens exist
Read-only.
"""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path, PureWindowsPath

DB, OUT = sys.argv[1], Path(sys.argv[2])
REPO = Path(r"C:\Python\Duke\Standalone")
c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row


def base(p):
    return PureWindowsPath(p).name.lower() if p else None


pool = defaultdict(list)
for r in c.execute("select source_pdf, plain_text_content, tables_json_content, text_sidecar_path from docling_artifacts"):
    b = base(r["source_pdf"])
    for t in (r["plain_text_content"], r["tables_json_content"]):
        if t:
            pool[b].append(t)
for r in c.execute("select source_pdf, text_content from ncuc_page_artifacts where text_content like '%5%'"):
    if r["text_content"]:
        pool[base(r["source_pdf"])].append(r["text_content"])


def sidecars(path):
    out = []
    if not path:
        return out
    p = Path(path)
    if not p.is_absolute():
        p = REPO / p
    for cand in (p, Path(str(p) + ".txt"), Path(str(p) + ".ocr.txt")):
        if cand.suffix.lower() == ".txt" and cand.is_file():
            out.append(cand.read_text(encoding="utf-8", errors="replace"))
    return out


def fmt_variants(v):
    s = set()
    for nd in range(0, 6):
        t = f"{abs(v):.{nd}f}"
        if float(t) == round(abs(v), 6) and "." in t:
            s.add(t)
    return s


rows = []
q = """
select tc.id, tc.family_key, tc.charge_label, tc.rate_value, tc.rate_unit, tc.source_snippet, tc.customer_class,
       tc.created_at, tv.effective_start, tv.id vid, tv.source_pdf, hd.id hd_id, hd.raw_text_path, hd.local_path
from tariff_charges tc join tariff_versions tv on tv.id = tc.version_id
left join historical_documents hd on hd.id = tv.historical_document_id
where tc.rate_value is not null and tc.rate_value != 0
"""
for r in c.execute(q):
    texts = sidecars(r["raw_text_path"]) + sidecars(r["local_path"])
    for key in {base(r["local_path"]), base(r["source_pdf"])} - {None}:
        texts += pool.get(key, [])
    snip = r["source_snippet"] or ""
    best = None
    for vs in fmt_variants(r["rate_value"]):
        bad_re = re.compile(r"(?<!\w)5" + re.escape(vs) + r"(?!\d)")
        clean_re = re.compile(r"(?<![\w.])" + re.escape(vs) + r"(?!\d)")
        nb = sum(len(bad_re.findall(t)) for t in texts)
        nc = sum(len(clean_re.findall(t)) for t in texts)
        dd = ("$$" + vs) in snip
        if nb or dd:
            conf = "HIGH" if dd or nc == 0 else "MEDIUM"
            cand = {"vs": vs, "raw_token": "5" + vs, "n_corrupting": nb, "n_clean": nc, "snippet_$$": dd, "conf": conf}
            if best is None or (conf == "HIGH" and best["conf"] != "HIGH"):
                best = cand
    if best:
        rows.append({"id": r["id"], "family": r["family_key"], "eff": r["effective_start"], "vid": r["vid"],
                     "hd_id": r["hd_id"], "label": r["charge_label"], "value": r["rate_value"], "unit": r["rate_unit"],
                     "class": r["customer_class"], "created": r["created_at"], "n_texts": len(texts),
                     "snippet": snip[:200], **best})

OUT.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
for x in sorted(rows, key=lambda x: (x["conf"], x["family"])):
    print(x["conf"], x["id"], x["family"], x["eff"], repr(x["label"])[:45], x["value"], x["unit"],
          "raw", x["raw_token"], "bad", x["n_corrupting"], "clean", x["n_clean"], "$$", x["snippet_$$"])
print("total", len(rows))
