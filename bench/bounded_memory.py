"""Memory/time benchmark for bounded large-filing processing (Phase 2C).

Each scenario runs in its own process so peak memory is per scenario. A
watchdog kills a scenario when free system memory drops below
``--min-free-gb`` and records it as ``aborted_low_memory`` instead of letting
the machine swap or crash (the Phase 2B whole-document Docling conversion of
hd 5324 ran out of memory).

    python bench/bounded_memory.py --pdf <filing.pdf> --span 17 31 --out reports/phase2c/bounded_memory.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

SCENARIOS = {
    "native_span": "native text layer + geometry tables, span pages only",
    "native_full": "native text layer + geometry tables, every page",
    "bounded_docling_span": "Docling on the span, 8-page chunks, original page numbers",
    "bounded_docling_full": "Docling on every page, 8-page chunks",
}


def _child(scenario: str, pdf: str, span: tuple[int, int], accelerator: str, chunk: int) -> dict:
    from duke_rates.document_intelligence.adapters import representation_from_native_pdf
    from duke_rates.document_intelligence.bounded import (
        bounded_representation,
        default_converter,
        page_count,
        peak_rss_mb,
    )

    total = page_count(pdf)
    started = time.perf_counter()
    out: dict = {"scenario": scenario, "pdf_pages": total}
    if scenario.startswith("native"):
        rng = span if scenario == "native_span" else (1, total)
        rep = representation_from_native_pdf(pdf, page_range=rng)
        out.update(pages=len(rep.pages), tables=sum(len(p.tables) for p in rep.pages), degraded=False)
    else:
        rng = span if scenario == "bounded_docling_span" else (1, total)
        result = bounded_representation(
            pdf, rng, chunk_pages=chunk, converter=default_converter(accelerator)
        )
        rep = result.representation
        out.update(
            pages=len(rep.pages) if rep else 0,
            first_page=rep.pages[0].page_number if rep and rep.pages else None,
            last_page=rep.pages[-1].page_number if rep and rep.pages else None,
            tables=sum(len(p.tables) for p in rep.pages) if rep else 0,
            chunks=len(result.chunks),
            degraded=result.degraded,
            degraded_chunks=result.degraded_chunks,
        )
    out["seconds"] = round(time.perf_counter() - started, 2)
    out["peak_rss_mb"] = peak_rss_mb()
    return out


def _run(scenario: str, args) -> dict:
    import psutil

    cmd = [
        sys.executable, __file__, "--child", scenario, "--pdf", args.pdf,
        "--span", str(args.span[0]), str(args.span[1]),
        "--accelerator", args.accelerator, "--chunk", str(args.chunk),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    aborted = {"flag": False, "min_free_gb": None}

    def watch():
        while proc.poll() is None:
            free = psutil.virtual_memory().available / 2**30
            aborted["min_free_gb"] = min(free, aborted["min_free_gb"] or free)
            if free < args.min_free_gb:
                aborted["flag"] = True
                proc.kill()
                return
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    stdout, stderr = proc.communicate()
    if aborted["flag"]:
        return {"scenario": scenario, "status": "aborted_low_memory", "min_free_gb": round(aborted["min_free_gb"], 2)}
    lines = [l for l in stdout.splitlines() if l.startswith("{")]
    if proc.returncode != 0 or not lines:
        return {"scenario": scenario, "status": "error", "returncode": proc.returncode, "stderr": stderr[-800:]}
    return {**json.loads(lines[-1]), "status": "ok", "min_free_gb": round(aborted["min_free_gb"] or 0, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--span", nargs=2, type=int, required=True)
    ap.add_argument("--accelerator", default="cuda")
    ap.add_argument("--chunk", type=int, default=8)
    ap.add_argument("--min-free-gb", type=float, default=1.2)
    ap.add_argument("--scenarios", nargs="*", default=list(SCENARIOS))
    ap.add_argument("--out")
    ap.add_argument("--child")
    args = ap.parse_args()
    if args.child:
        print(json.dumps(_child(args.child, args.pdf, tuple(args.span), args.accelerator, args.chunk)))
        return
    results = []
    for scenario in args.scenarios:
        r = _run(scenario, args)
        r["description"] = SCENARIOS[scenario]
        print(json.dumps(r), flush=True)
        results.append(r)
    if args.out:
        Path(args.out).write_text(json.dumps({"pdf": args.pdf, "span": args.span, "results": results}, indent=2))


if __name__ == "__main__":
    main()
