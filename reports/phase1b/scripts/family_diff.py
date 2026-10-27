"""Diff bill-relevant DEP families between two DB snapshots (read-only).

Charges are compared by (family, effective_start, label, rounded value, unit, tou, season, class)
so re-created rows with new ids but equal content count as unchanged.
"""
import sqlite3
import sys
from collections import defaultdict

A, B = sys.argv[1], sys.argv[2]
FAMS = [f"nc-progress-leaf-{n}" for n in (500, 501, 502, 503, 600, 601, 602, 604, 605, 607, 608, 609, 610, 611, 613, 640)]


def norm_date(s):
    from datetime import datetime
    if not s:
        return s
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return s


def load(path):
    c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    cols = {r[1] for r in c.execute("pragma table_info(tariff_charges)")}
    vers, ch, hd = {}, defaultdict(set), {}
    for f in FAMS:
        for vid, eff, hdid in c.execute("select id,effective_start,historical_document_id from tariff_versions where family_key=?", (f,)):
            vers[(f, norm_date(eff))] = (vid, hdid)
        for row in c.execute(
            "select tv.effective_start, tc.charge_label, round(tc.rate_value,6), tc.rate_unit, tc.tou_period, tc.season, "
            + ("tc.customer_class" if "customer_class" in cols else "null")
            + ", tc.notes from tariff_charges tc join tariff_versions tv on tv.id=tc.version_id where tc.family_key=?", (f,)):
            ch[(f, norm_date(row[0]))].add(row[1:7])
        for hid, eff, title, pj in c.execute("select id,effective_start,title,parsed_result_json is not null from historical_documents where family_key=?", (f,)):
            hd[hid] = (f, norm_date(eff), title, pj)
    return vers, ch, hd


va, ca, ha = load(A)
vb, cb, hb = load(B)
print("VERSIONS only in A:", sorted(set(va) - set(vb), key=str))
print("VERSIONS only in B:", sorted(set(vb) - set(va), key=str))
print("HD only in A:", sorted(((k, v) for k, v in ha.items() if k not in hb), key=str))
print("HD only in B:", sorted(((k, v) for k, v in hb.items() if k not in ha), key=str))
print("HD parsed flag changed:", [(k, ha[k], hb[k][3]) for k in ha if k in hb and ha[k][3] != hb[k][3]])
print()
for key in sorted(set(ca) | set(cb), key=str):
    if key[1] and key[1] < "2024-01-01":
        continue
    a, b = ca.get(key, set()), cb.get(key, set())
    if a == b:
        continue
    # ignore pure customer_class / season fill-ins: compare on (label,value,unit,tou)
    core = lambda s: {x[:4] for x in s}
    print(key, f"A={len(a)} B={len(b)}", "core-same" if core(a) == core(b) else "CORE-DIFF")
    for x in sorted(a - b, key=str):
        print("    -", x)
    for x in sorted(b - a, key=str):
        print("    +", x)
