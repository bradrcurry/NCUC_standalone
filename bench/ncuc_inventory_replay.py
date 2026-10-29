"""Reproduce D4 completeness, timing and candidate/ledger recovery without network access.

Run with .venv312/Scripts/python.exe bench/ncuc_inventory_replay.py --out <report.json>.
The six sanitized live captures are committed under tests/fixtures/d4_portal.
Only a temporary, newly created database is written; production is never opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from duke_rates.db.provenance_schema import ensure_provenance_schema
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc import remote_discovery as rd
from duke_rates.historical.ncuc.docket_inventory import RETRIEVER_VERSION, walk_docket_inventory
from duke_rates.historical.ncuc.search_ledger import canonical_params

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("--repetitions must be between 1 and 20")
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests/fixtures/d4_portal"
    pages = {n: (fixtures / f"docket_inventory_e7_1026_page{n}.html").read_text(
        encoding="utf-8") for n in range(1, 7)}
    hashes = {str(n): hashlib.sha256(html.encode()).hexdigest() for n, html in pages.items()}
    times = {"first_page_reader": [], "complete_inventory": []}
    for _ in range(args.repetitions):
        started = time.perf_counter()
        baseline = rd.docket_inventory_outcome(pages[1], [], docket_label="E-7 Sub 1026")
        times["first_page_reader"].append(time.perf_counter() - started)
        current = [pages[1]]
        page = SimpleNamespace(content=lambda current=current: current[0],
                               title=lambda: "Docket Details")
        started = time.perf_counter()
        final = walk_docket_inventory(
            page, docket_label="E-7 Sub 1026", max_pages=6, retries=0,
            advance=lambda href, current=current: current.__setitem__(
                0, pages[int(re.search(r"Page\$(\d+)", href)[1])]),
        )
        times["complete_inventory"].append(time.perf_counter() - started)
        assert final.complete and final.pages_reached == final.pages_total == 6
        assert len(final.results) == 568
    report = {
        "retriever": RETRIEVER_VERSION, "evidence": "sanitized live captures, offline replay",
        "network_requests": 0, "pdf_downloads": 0, "repetitions": args.repetitions,
        "fixture_sha256": hashes,
        "fixture_hash_encoding": "UTF-8 text with universal-newline normalization",
        "baseline": baseline.summary(), "final": final.summary(),
        "restricted_entries": sum(r.availability == "restricted" for r in final.results),
        "public_orders": sum(rd.is_order_row(r) and r.availability != "restricted"
                             for r in final.results),
        "timing_s": {key: {"median": statistics.median(values), "runs": values}
                     for key, values in times.items()},
    }
    params = {"method": "docket_inventory", "docket_number": "E-7 Sub 1026",
              "evidence": "offline_captured_replay", "capture_sha256": hashes}
    plan = {"queries": [{"channel": "portal_orders", "params": params,
                         "params_hash": canonical_params("portal_orders", params)[1],
                         "targets": [{"docket": "E-7 Sub 1026"}]}]}
    with tempfile.TemporaryDirectory(prefix="duke_d4_inventory_") as temporary:
        conn = connect(Path(temporary) / "replay.db")
        ensure_provenance_schema(conn)
        report["partial_run"] = rd.execute_plan(conn, plan, created_by="d4-offline-benchmark",
                                                docket_inventory=lambda p: baseline)
        report["recovered_run"] = rd.execute_plan(conn, plan, created_by="d4-offline-benchmark",
                                                  docket_inventory=lambda p: final)
        report["repeated_run"] = rd.execute_plan(conn, plan, created_by="d4-offline-benchmark",
                                                 docket_inventory=lambda p: final)
        report["candidate_count"] = conn.execute(
            "SELECT COUNT(*) FROM remote_document_candidates").fetchone()[0]
        report["search_attempts"] = [dict(row) for row in conn.execute(
            "SELECT outcome, stop_reason, result_count, pages_reached, pages_total, error_detail, "
            "params_json, result_refs_json FROM search_attempts ORDER BY id")]
        assert report["candidate_count"] == 83
        assert report["repeated_run"]["repeated_searches_avoided"] == 1
        assert [a["outcome"] for a in report["search_attempts"]] == ["incomplete", "hits"]
        conn.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "baseline", "final", "restricted_entries", "public_orders", "timing_s",
        "candidate_count", "repeated_run")}, indent=2))
