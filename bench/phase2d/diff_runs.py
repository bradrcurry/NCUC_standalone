"""Phase 2D: field-by-field diff of two bench/run_extraction.py outputs.

Charges are compared per document as multisets of
(charge_type, label, value, unit, tou, season, tier, class); evidence pages are
compared for charges present in both. Prints per-document differences and a
one-line verdict (IDENTICAL / DIFFERENT).

    python bench/phase2d/diff_runs.py <a.json> <b.json> [--evidence]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter


def key(c: dict) -> tuple:
    return (c.get("charge_type"), c.get("charge_label"), c.get("rate_value"), c.get("rate_unit"),
            c.get("tou_period"), c.get("season"), c.get("tier_min"), c.get("tier_max"), c.get("customer_class"))


def page(c: dict):
    ev = c.get("evidence") or {}
    return ev.get("page") or ev.get("source_page")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--evidence", action="store_true", help="also diff evidence pages")
    ap.add_argument("--max", type=int, default=6)
    args = ap.parse_args()
    ra = {r["doc_id"]: r for r in json.load(open(args.a))["results"]}
    rb = {r["doc_id"]: r for r in json.load(open(args.b))["results"]}
    differing = 0
    for doc_id in ra:
        a, b = ra[doc_id], rb.get(doc_id, {"charges": [], "status": "missing"})
        ca, cb = Counter(map(key, a["charges"])), Counter(map(key, b["charges"]))
        only_a, only_b = ca - cb, cb - ca
        ev_diff = 0
        if args.evidence:
            pa = Counter((key(c), page(c)) for c in a["charges"])
            pb = Counter((key(c), page(c)) for c in b["charges"])
            ev_diff = sum(((pa - pb) + (pb - pa)).values())
        if only_a or only_b or a["status"] != b["status"] or ev_diff:
            differing += 1
            print(f"## {doc_id}: {a['status']}->{b['status']} charges {len(a['charges'])}->{len(b['charges'])} "
                  f"only_a={sum(only_a.values())} only_b={sum(only_b.values())} evidence_page_diffs={ev_diff}")
            for k in list(only_a)[: args.max]:
                print("   -", k[1], k[2], k[3], "class=", k[8])
            for k in list(only_b)[: args.max]:
                print("   +", k[1], k[2], k[3], "class=", k[8])
    print("VERDICT:", "IDENTICAL" if not differing else f"DIFFERENT ({differing} docs)")


if __name__ == "__main__":
    main()
