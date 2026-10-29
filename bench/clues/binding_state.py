"""Phase 2E D3: fingerprint the rows a document binding may touch (rehearsal before/after/unwind).

    python bench/clues/binding_state.py --db SCRATCH --out reports/phase2e_d3/binding/before.json

SHA-256 per table over every row (ordered by id), plus counts, so "rollback
restores the previous state" is a byte comparison, not a spot check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

TABLES = ("tariff_versions", "historical_documents", "tariff_charges", "lineage_links", "lineage_gaps_typed",
          "document_assertions", "effective_date_assessments")


def state(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for t in TABLES:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone():
            continue
        h = hashlib.sha256()
        n = 0
        for row in conn.execute(f"SELECT * FROM {t} ORDER BY id"):
            h.update(json.dumps(row, default=str).encode())
            n += 1
        out[t] = {"rows": n, "sha256": h.hexdigest()}
    out["gap_status"] = dict(conn.execute("SELECT status, COUNT(*) FROM lineage_gaps_typed WHERE active = 1 "
                                          "GROUP BY status ORDER BY status").fetchall())
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = state(a.db)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1)
    print(json.dumps(s, indent=1))
