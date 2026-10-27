"""Audit stored values for the pre-6400551 leading-5 -> '$' OCR normalization defect.

Defect (both rules): r"(?<!\\w)5(?=\\d+\\.\\d+)" -> "$".  So raw "55.00" -> "$5.00",
"515.27" -> "$15.27", "50.123" -> "$0.123".  The stored value is the raw value with the
leading 5 removed.

For each tariff_charges row (and each charge in historical_documents.parsed_result_json)
we look at the RAW source text (pre-normalization sidecar) for a token 5<rest> where
float(<rest>) == stored value (or stored*100 / stored/100 for unit conversion), and at the
snippet for '$$' signatures.  Read-only (immutable URI).
"""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = sys.argv[1]
OUT = Path(sys.argv[2])
REPO = Path(r"C:\Python\Duke\Standalone")

c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row

TOK = re.compile(r"(?<!\w)5(\d+\.\d+)")
ANY = re.compile(r"(?<![\w.])(\d+\.\d+)")
_text_cache: dict[str, str | None] = {}


def load_text(path):
    if not path:
        return None
    if path in _text_cache:
        return _text_cache[path]
    p = Path(path)
    if not p.is_absolute():
        p = REPO / p
    t = None
    for cand in (p, Path(str(p) + ".txt"), Path(str(p) + ".ocr.txt")):
        if cand.exists() and cand.is_file() and cand.suffix.lower() == ".txt":
            t = cand.read_text(encoding="utf-8", errors="replace")
            break
    _text_cache[path] = t
    return t


def scan(text):
    """Map value -> (#corrupting tokens, #clean tokens) found in raw text."""
    bad = defaultdict(list)
    for m in TOK.finditer(text):
        bad[round(float(m.group(1)), 6)].append(m.group(0))
    clean = defaultdict(int)
    for m in ANY.finditer(text):
        s = m.start()
        prev = text[s - 1] if s else ""
        clean[round(float(m.group(1)), 6)] += 1
    return bad, clean


scan_cache = {}


def check(value, text_path, snippet):
    if value is None:
        return None
    sig = []
    if snippet and "$$" in snippet:
        sig.append("snippet_double_dollar")
    text = load_text(text_path)
    hit = None
    if text:
        if text_path not in scan_cache:
            scan_cache[text_path] = scan(text)
        bad, clean = scan_cache[text_path]
        for mult in (1, 100, 0.01):
            v = round(abs(value) * mult, 6)
            if v in bad:
                # clean count includes the inner "5"-stripped digits? no: ANY needs non-word before.
                hit = {"mult": mult, "raw_tokens": sorted(set(bad[v]))[:5], "n_corrupting": len(bad[v]),
                       "n_clean": clean.get(v, 0)}
                break
    if hit:
        sig.append("raw_text_5prefix")
    if not sig:
        return None
    return sig, hit


def raw_original(value, mult):
    # rebuild the raw token text: "5" + the stripped representation
    return None


rows = []
# tariff_charges via version -> historical_document raw text
q = """
select tc.id, tc.family_key, tc.charge_label, tc.rate_value, tc.rate_unit, tc.source_snippet, tc.customer_class,
       tv.effective_start, tv.id vid, hd.id hd_id, hd.raw_text_path, hd.local_path
from tariff_charges tc join tariff_versions tv on tv.id = tc.version_id
left join historical_documents hd on hd.id = tv.historical_document_id
"""
for r in c.execute(q):
    res = check(r["rate_value"], r["raw_text_path"] or r["local_path"], r["source_snippet"])
    if res:
        sig, hit = res
        rows.append({"table": "tariff_charges", "id": r["id"], "family": r["family_key"], "eff": r["effective_start"],
                     "label": r["charge_label"], "value": r["rate_value"], "unit": r["rate_unit"],
                     "class": r["customer_class"], "hd_id": r["hd_id"], "signals": sig, "hit": hit,
                     "snippet": (r["source_snippet"] or "")[:200]})

# parsed_result_json charges (used by billing selector)
for r in c.execute("select id, family_key, effective_start, raw_text_path, local_path, parsed_result_json from historical_documents where parsed_result_json is not null"):
    pr = json.loads(r["parsed_result_json"])
    sch = pr.get("schedule") or {}
    rid = pr.get("rider") or {}
    charges = (sch.get("charges") or []) + (sch.get("energy_charges") or []) + (rid.get("charge_components") or [])
    for ch in charges:
        val = ch.get("rate") if "rate" in ch else ch.get("value")
        if not isinstance(val, (int, float)):
            continue
        res = check(val, r["raw_text_path"] or r["local_path"], ch.get("source_text") or ch.get("snippet"))
        if res:
            sig, hit = res
            rows.append({"table": "historical_documents.parsed_result_json", "id": r["id"], "family": r["family_key"],
                         "eff": r["effective_start"], "label": ch.get("label") or ch.get("bill_label"), "value": val,
                         "unit": ch.get("unit"), "signals": sig, "hit": hit})

OUT.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
by = defaultdict(int)
for x in rows:
    by[(x["table"], tuple(x["signals"]))] += 1
print("candidates", len(rows))
for k, v in sorted(by.items()):
    print(" ", k, v)
print("texts loaded", sum(1 for v in _text_cache.values() if v), "/", len(_text_cache))
