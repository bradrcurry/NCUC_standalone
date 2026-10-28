"""Phase 2D: convert benchmark excerpts with whichever Docling the running venv has.

The conversion goes through the application's own
``docling_backend.convert_pdf_with_docling`` (``--variant app``), so the only
thing that differs between two runs from different venvs is the installed
dependency stack. Other variants patch the pipeline options the application
builds; they are configuration experiments, not application changes.

Output (one directory per run)::

    <out>/<doc_id>.docling.json.gz   envelope readable by bench/run_extraction.py
    <out>/metrics.jsonl              per document: seconds, pages, tables, peak RSS,
                                     peak VRAM, GPU / CPU utilisation, status
    <out>/run.json                   versions, variant, totals

Resource safeguards: a sampler thread aborts the process (exit 3) when system
available memory drops below ``--min-avail-gb``; the metrics written so far are
kept. Nothing is written to any database or next to a source PDF.

Usage::

    <venv>/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts <dir> --out <dir> \
        [--variant app|tfv2|egret_m|heron101|nonthreaded|egret_m_nonthreaded] [--ocr app|off|auto|tesseract|full] [--docs ...]
        [--whole-source] [--page-range-native]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib.metadata as md
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

BENCH = Path(__file__).resolve().parents[1]
ROOT = BENCH.parent
sys.path.insert(0, str(ROOT / "src"))

VERSIONED = ["docling", "docling-core", "docling-parse", "docling-ibm-models", "torch", "torchvision",
             "transformers", "PyMuPDF", "pdfplumber", "pypdfium2", "rapidocr", "huggingface_hub", "accelerate"]


def _versions() -> dict:
    out = {}
    for name in VERSIONED:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


class Sampler(threading.Thread):
    """Samples process-tree RSS, CPU and system memory every ``interval`` s."""

    def __init__(self, min_avail_gb: float, on_abort, interval: float = 0.2):
        super().__init__(daemon=True)
        self.proc = psutil.Process()
        self.interval, self.min_avail = interval, min_avail_gb * 2**30
        self.on_abort = on_abort
        self.reset()
        self.stop_flag = False

    def reset(self):
        self.peak_rss = 0
        self.min_avail_seen = None
        self.cpu = []

    def run(self):
        self.proc.cpu_percent(None)
        while not self.stop_flag:
            try:
                procs = [self.proc] + self.proc.children(recursive=True)
                rss = sum(p.memory_info().rss for p in procs if p.is_running())
            except psutil.Error:
                rss = 0
            avail = psutil.virtual_memory().available
            self.peak_rss = max(self.peak_rss, rss)
            self.min_avail_seen = avail if self.min_avail_seen is None else min(self.min_avail_seen, avail)
            self.cpu.append(self.proc.cpu_percent(None))
            if avail < self.min_avail:
                self.on_abort(avail)
            time.sleep(self.interval)


class GpuLog:
    """``nvidia-smi`` in loop mode; per-window peak memory.used and mean utilisation."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = open(path, "w", newline="")
        self.p = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,memory.used", "--format=csv,noheader,nounits",
             "-lms", "200"], stdout=self.fh, stderr=subprocess.DEVNULL)

    def window(self, t0: float, t1: float) -> dict:
        self.fh.flush()
        used, util = [], []
        with open(self.path, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 3:
                    continue
                try:
                    ts = time.mktime(time.strptime(row[0].strip().split(".")[0], "%Y/%m/%d %H:%M:%S"))
                    if t0 - 1 <= ts <= t1 + 1:
                        util.append(float(row[1])); used.append(float(row[2]))
                except ValueError:
                    continue
        return {"gpu_mem_used_peak_mib": max(used) if used else None,
                "gpu_util_mean": round(sum(util) / len(util), 1) if util else None,
                "gpu_util_max": max(util) if util else None}

    def close(self):
        self.p.terminate()
        self.fh.close()


def _patch_backend(variant: str, ocr: str):
    """Wrap the application's converter builder to apply a configuration variant."""
    from duke_rates.historical.ncuc.pipeline import docling_backend as backend

    original = backend._build_standard_converter

    def patched(accel_device, *, has_scanned_pages, artifacts_path):
        scanned = has_scanned_pages or ocr in ("tesseract", "full", "auto")
        conv = original(accel_device, has_scanned_pages=scanned, artifacts_path=artifacts_path)
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel import pipeline_options as P
        from docling.datamodel import layout_model_specs as L

        po = conv.format_to_options[InputFormat.PDF].pipeline_options
        if variant == "tfv2":
            po.table_structure_options = P.TableStructureV2Options(do_cell_matching=True)
        elif variant in ("egret_m", "egret_m_nonthreaded"):
            po.layout_options = P.LayoutOptions(model_spec=L.DOCLING_LAYOUT_EGRET_MEDIUM)
        elif variant == "heron101":
            po.layout_options = P.LayoutOptions(model_spec=L.DOCLING_LAYOUT_HERON_101)
        if variant.endswith("nonthreaded"):
            # the pre-2.123 default PDF backend (docling 2.123 made the threaded one the default)
            from docling.backend.docling_parse_backend import DoclingParseDocumentBackend

            conv.format_to_options[InputFormat.PDF].backend = DoclingParseDocumentBackend
        if ocr == "off":
            po.do_ocr = False
        elif ocr == "auto":
            po.do_ocr = True
            po.ocr_options = P.OcrAutoOptions() if hasattr(P, "OcrAutoOptions") else po.ocr_options
        elif ocr == "full":
            po.do_ocr = True
            po.ocr_options.force_full_page_ocr = True
        return conv

    backend._build_standard_converter = patched
    return backend


def _convert_page_range(backend, pdf: Path, page_range: tuple[int, int], accelerator: str) -> dict | None:
    """Docling's own ``page_range`` on the original PDF (no excerpt), app converter options."""
    import torch
    from docling.datamodel.accelerator_options import AcceleratorDevice

    device = AcceleratorDevice.CUDA if accelerator == "cuda" else AcceleratorDevice.CPU
    conv = backend._build_standard_converter(device, has_scanned_pages=False, artifacts_path=None)
    if accelerator == "cuda":
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            result = conv.convert(str(pdf), page_range=page_range)
    else:
        result = conv.convert(str(pdf), page_range=page_range)
    return {"document": result.document.export_to_dict(), "page_count": len(result.document.pages),
            "conversion_status": str(result.status), "backend_version": "phase2d_page_range",
            "accelerator": accelerator}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--excerpts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default=None)
    ap.add_argument("--variant", default="app", choices=("app", "tfv2", "egret_m", "heron101", "nonthreaded", "egret_m_nonthreaded"))
    ap.add_argument("--ocr", default="app", choices=("app", "off", "auto", "tesseract", "full"))
    ap.add_argument("--accelerator", default="cuda")
    ap.add_argument("--docs", nargs="*")
    ap.add_argument("--whole-source", action="store_true", help="convert the whole source PDF, not the excerpt")
    ap.add_argument("--page-range-native", action="store_true",
                    help="convert the source PDF with Docling page_range=span (original page numbers)")
    ap.add_argument("--min-avail-gb", type=float, default=0.8)
    args = ap.parse_args()

    excerpts = Path(args.excerpts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((excerpts / "manifest.json").read_text(encoding="utf-8"))
    metrics_path = out / "metrics.jsonl"
    current = {"doc_id": None}

    def abort(avail):
        with open(metrics_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"doc_id": current["doc_id"], "status": "ABORTED_LOW_MEMORY",
                                 "avail_gb": round(avail / 2**30, 2)}) + "\n")
        os._exit(3)

    sampler = Sampler(args.min_avail_gb, abort)
    sampler.start()
    gpu = GpuLog(out / "nvidia_smi.csv")
    backend = _patch_backend(args.variant, args.ocr)
    try:
        import torch
        cuda = torch.cuda.is_available()
    except ImportError:
        torch, cuda = None, False

    run = {"label": args.label or out.name, "variant": args.variant, "ocr": args.ocr, "python": sys.version,
           "venv": sys.prefix, "versions": _versions(), "hf_home": os.environ.get("HF_HOME"),
           "whole_source": args.whole_source, "page_range_native": args.page_range_native,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t_all = time.perf_counter()
    for item in manifest:
        doc_id = item["doc_id"]
        if args.docs and doc_id not in args.docs:
            continue
        current["doc_id"] = doc_id
        sampler.reset()
        if cuda:
            torch.cuda.reset_peak_memory_stats()
        offset = 0
        t0w, t0 = time.time(), time.perf_counter()
        error = None
        try:
            if args.page_range_native:
                env = _convert_page_range(backend, ROOT / item["source_pdf"], tuple(item["span"]), args.accelerator)
            else:
                src = ROOT / item["source_pdf"] if args.whole_source else excerpts / item["excerpt"]
                offset = 0 if args.whole_source else item["page_offset"]
                with tempfile.TemporaryDirectory() as tmp:
                    local = Path(tmp) / f"{doc_id}.pdf"
                    shutil.copy2(src, local)
                    env = backend.convert_pdf_with_docling(str(local), accelerator=args.accelerator, force=True)
        except Exception as exc:  # recorded, the run continues
            env, error = None, f"{type(exc).__name__}: {exc}"
        secs = time.perf_counter() - t0
        t1w = time.time()
        rec = {"doc_id": doc_id, "seconds": round(secs, 2), "status": "ok" if env and env.get("document") else "FAILED",
               "error": error, "peak_rss_mb": round(sampler.peak_rss / 2**20), "min_avail_gb":
               round((sampler.min_avail_seen or 0) / 2**30, 2),
               "cpu_mean_pct": round(sum(sampler.cpu) / len(sampler.cpu), 1) if sampler.cpu else None,
               "torch_peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 2**20) if cuda else None,
               "torch_peak_reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20) if cuda else None,
               **gpu.window(t0w, t1w)}
        if env and env.get("document"):
            document = env["document"]
            rec.update(pages=len(document.get("pages", {})), tables=len(document.get("tables", [])),
                       texts=len(document.get("texts", [])), conversion_status=str(env.get("conversion_status")))
            envelope = {"origin": f"phase2d:{run['label']}", "backend_version": env.get("backend_version"),
                        "accelerator": env.get("accelerator"), "status": str(env.get("conversion_status")),
                        "page_count": env.get("page_count"), "docling_package_version": run["versions"]["docling"],
                        "page_offset": offset, "variant": args.variant, "ocr": args.ocr,
                        "doc_id": doc_id, "file_sha256": item["source_sha256"], "document": document}
            with gzip.open(out / f"{doc_id}.docling.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(envelope, fh, sort_keys=True, separators=(",", ":"))
        with open(metrics_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{doc_id:28s} {rec['status']:6s} {rec['seconds']:7.2f}s pages={rec.get('pages')} "
              f"tables={rec.get('tables')} rss={rec['peak_rss_mb']}MB vram={rec['gpu_mem_used_peak_mib']}MiB",
              flush=True)
    run["total_seconds"] = round(time.perf_counter() - t_all, 2)
    (out / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    sampler.stop_flag = True
    gpu.close()


if __name__ == "__main__":
    main()
