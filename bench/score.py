"""Score benchmark runs against the hand-verified ground truth.

Inputs: ``bench/expected/<doc_id>.json`` (transcribed from source PDFs) and
one or more run files from ``bench/run_extraction.py``.  Matching works in
canonical dollar units ($/kWh, $/kW, $/month, ...): expected cents/kWh are
divided by 100 because profiles emit $/kWh.

Per expected record (``optional`` records excluded) the scorer looks for an
extracted charge with the identical value (|diff| < 1e-9 after rounding to
7 places).  Matching is one-to-one, in two passes: first value + unit +
rate class, then value alone, so a value found only under another class or
unit is counted as a class or unit error rather than a miss.

Metrics (per run, and per document in the JSON):

1. value accuracy  -- expected records whose exact value was extracted
2. unit accuracy   -- of value matches, share with the same unit
3. rate-class accuracy -- of value matches in rider summaries, share whose
   charge label names the expected rate class
4. effective-date accuracy -- documents whose detected date equals the printed one
5. completeness / false positives -- recall, precision, charges on expected-zero docs
6. rider-summary total reconciliation -- printed TOTAL rows found with the right class
7. source evidence -- extracted charges with a snippet; snippet containing the value
8. processing time -- seconds per document
9. provenance coverage (Phase 2B runs) -- extracted charges with a source page,
   a bounding box, a table-cell reference, and a quote that prints the value;
   also the share of value-matched charges that carry a page

Usage::

    python bench/score.py reports/phase0/bench_baseline.json reports/phase0/bench_repaired.json \
        --out reports/phase0/bench_scores
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

BENCH = Path(__file__).resolve().parent

#: expected unit -> (canonical unit, factor to canonical)
EXPECTED_UNITS = {
    "cents_per_kwh": ("$/kWh", Decimal("0.01")),
    "dollars_per_kwh": ("$/kWh", Decimal(1)),
    "dollars_per_kw": ("$/kW", Decimal(1)),
    "dollars_per_kvar": ("$/kVAR", Decimal(1)),
    "dollars_per_month": ("$/month", Decimal(1)),
    "percent": ("%", Decimal(1)),
    "dollars_per_occurrence": ("$/occurrence", Decimal(1)),
    "dollars_one_time": ("$", Decimal(1)),
    "dollars_per_year": ("$/year", Decimal(1)),
}

#: extracted rate_unit -> (canonical unit, factor)
EXTRACTED_UNITS = {
    "$/kwh": ("$/kWh", Decimal(1)),
    "¢/kwh": ("$/kWh", Decimal("0.01")),
    "�/kwh": ("$/kWh", Decimal("0.01")),
    "$/kw": ("$/kW", Decimal(1)),
    "$/kw-month": ("$/kW", Decimal(1)),
    "$/kw/month": ("$/kW", Decimal(1)),
    "$/month": ("$/month", Decimal(1)),
    "$/customer/month": ("$/month", Decimal(1)),
    "$/bill": ("$/month", Decimal(1)),
    "$/kvar": ("$/kVAR", Decimal(1)),
    "%": ("%", Decimal(1)),
    "% of applicable bill": ("%", Decimal(1)),
    "$/year": ("$/year", Decimal(1)),
    "$/event": ("$/occurrence", Decimal(1)),
    "$": ("$", Decimal(1)),
}

SUMMARY_CATEGORIES = {"A", "B"}


def q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0000001"))


def norm_class(text: str) -> str:
    text = re.sub(r"\*+", " ", text or "").lower()
    text = re.sub(r"[^a-z0-9& ]+", " ", text)
    text = re.sub(r"\b(schedules?|service|rs|re|es|rt|rstc|retc|sgs|bc|lgs|ts|s|hlf|opt|v|e|pg|sgstc|pl|ol|nl|gl|wc|i)\b", " ", text)
    return " ".join(text.split())


def class_matches(expected_class: str, charge_label: str) -> bool:
    """Profiles label rider charges "<rate class> - <rider>" or "<rate class>
    Total Rider Adjustments"; class names can themselves contain " - ", so
    compare the expected class against the start of the label."""
    a, b = norm_class(expected_class), norm_class(charge_label)
    if not a or not b:
        return False
    return b.startswith(a) or SequenceMatcher(None, a, b[: len(a)]).ratio() >= 0.9


def canonical_expected(rec: dict) -> tuple[str, Decimal] | None:
    unit = EXPECTED_UNITS.get(rec.get("unit"))
    if unit is None:
        return None
    return unit[0], q(Decimal(str(rec["value"])) * unit[1])


def canonical_extracted(ch: dict) -> tuple[str, Decimal] | None:
    if ch.get("rate_value") is None:
        return None
    raw = (ch.get("rate_unit") or "").strip().lower()
    unit = EXTRACTED_UNITS.get(raw, (ch.get("rate_unit") or "?", Decimal(1)))
    return unit[0], q(Decimal(str(ch["rate_value"])) * unit[1])


def score_doc(expected: dict, corpus_entry: dict, run: dict) -> dict:
    records = [r for r in expected.get("charges", []) if not r.get("optional")]
    extracted = run.get("charges", [])
    canon_ext = [canonical_extracted(c) for c in extracted]
    used = [False] * len(extracted)
    matches: list[tuple[dict, dict, str]] = []  # (expected, extracted, kind)
    is_summary = corpus_entry.get("category", "").split(",")[0] in SUMMARY_CATEGORIES

    def find(rec: dict, need_unit: bool, need_class: bool) -> int | None:
        exp = canonical_expected(rec)
        if exp is None:
            return None
        for i, c in enumerate(canon_ext):
            if used[i] or c is None or c[1] != exp[1]:
                continue
            if need_unit and c[0] != exp[0]:
                continue
            if need_class and not class_matches(rec.get("rate_class", ""), extracted[i].get("charge_label", "")):
                continue
            return i
        return None

    unmatched = []
    pending = list(records)
    for need_unit, need_class in ((True, is_summary), (True, False), (False, False)):
        still = []
        for rec in pending:
            i = find(rec, need_unit, need_class)
            if i is None:
                still.append(rec)
                continue
            used[i] = True
            unit_ok = canon_ext[i][0] == canonical_expected(rec)[0]
            class_ok = class_matches(rec.get("rate_class", ""), extracted[i].get("charge_label", ""))
            matches.append((rec, extracted[i], f"unit_ok={unit_ok},class_ok={class_ok}"))
        pending = still
    unmatched = pending

    value_matched = len(matches)
    unit_ok = sum(1 for rec, ch, _ in matches if canonical_extracted(ch)[0] == canonical_expected(rec)[0])
    class_ok = sum(1 for rec, ch, _ in matches if class_matches(rec.get("rate_class", ""), ch.get("charge_label", "")))
    totals = [r for r in records if r.get("is_total") and not r.get("subtotal")]
    totals_found = sum(
        1 for rec, ch, _ in matches
        if rec.get("is_total") and not rec.get("subtotal")
        and canonical_extracted(ch)[0] == canonical_expected(rec)[0]
        and class_matches(rec.get("rate_class", ""), ch.get("charge_label", ""))
    )
    snippets = sum(1 for c in extracted if (c.get("source_snippet") or "").strip())

    def value_in_snippet(c: dict) -> bool:
        snip = c.get("source_snippet") or ""
        v = c.get("rate_value")
        if v is None:
            return False
        candidates = {f"{v:g}", f"{v * 100:g}", f"{abs(v):g}", f"{abs(v) * 100:g}"}
        return any(s in snip for s in candidates)

    def ev(c: dict) -> dict:
        return c.get("evidence") or {}

    matched_ids = {id(ch) for _, ch, _ in matches}
    return {
        "with_page": sum(1 for c in extracted if ev(c).get("page") is not None),
        "with_bbox": sum(1 for c in extracted if ev(c).get("has_bbox")),
        "with_table_cell": sum(1 for c in extracted if ev(c).get("table_id") and ev(c).get("row") is not None),
        "with_value_quote": sum(1 for c in extracted if ev(c).get("quote_contains_value")),
        "matched_with_page": sum(1 for c in extracted if id(c) in matched_ids and ev(c).get("page") is not None),
        "doc_id": expected["doc_id"],
        "expected_zero": bool(expected.get("expected_zero")),
        "expected": len(records),
        "extracted": len(extracted),
        "value_matched": value_matched,
        "unit_ok": unit_ok,
        "class_ok": class_ok if is_summary else None,
        "class_checked": value_matched if is_summary else 0,
        "totals_expected": len(totals),
        "totals_found": totals_found,
        "false_positives": len(extracted) - value_matched,
        "snippets": snippets,
        "snippets_with_value": sum(1 for c in extracted if value_in_snippet(c)),
        "effective_date_expected": expected.get("effective_date"),
        "effective_date_detected": run.get("effective_date"),
        "effective_date_ok": (
            expected.get("effective_date") is not None
            and run.get("effective_date") == expected.get("effective_date")
        ),
        "status": run.get("status"),
        "profile": run.get("profile"),
        "text_source": run.get("text_source"),
        "seconds": run.get("seconds"),
        "unit_errors": [
            {"label": rec["label"], "rate_class": rec.get("rate_class"), "expected": f"{rec['value']} {rec['unit']}",
             "extracted": f"{ch['rate_value']} {ch['rate_unit']}", "charge_label": ch.get("charge_label")}
            for rec, ch, _ in matches if canonical_extracted(ch)[0] != canonical_expected(rec)[0]
        ],
        "class_errors": [
            {"label": rec["label"], "expected_class": rec.get("rate_class"), "charge_label": ch.get("charge_label"),
             "value": rec["value"]}
            for rec, ch, _ in matches
            if is_summary and not class_matches(rec.get("rate_class", ""), ch.get("charge_label", ""))
        ],
        "missed": [f"{r.get('rate_class')} | {r['label']} | {r['value']} {r['unit']}" for r in unmatched],
    }


def aggregate(docs: list[dict]) -> dict:
    agg = defaultdict(float)
    for d in docs:
        for k in ("expected", "extracted", "value_matched", "unit_ok", "class_checked", "totals_expected",
                  "totals_found", "snippets", "snippets_with_value", "seconds", "with_page",
                  "with_bbox", "with_table_cell", "with_value_quote", "matched_with_page"):
            agg[k] += d[k] or 0
        agg["class_ok"] += d["class_ok"] or 0
        agg["false_positives"] += d["false_positives"]
        if d["expected_zero"]:
            agg["zero_docs"] += 1
            agg["zero_doc_false_positives"] += d["extracted"]
        if d["effective_date_expected"]:
            agg["dated_docs"] += 1
            agg["effective_date_ok"] += 1 if d["effective_date_ok"] else 0

    def ratio(a, b):
        return round(agg[a] / agg[b], 4) if agg[b] else None

    return {
        **{k: (int(v) if k != "seconds" else round(v, 2)) for k, v in agg.items()},
        "value_accuracy (recall)": ratio("value_matched", "expected"),
        "precision": ratio("value_matched", "extracted"),
        "unit_accuracy": ratio("unit_ok", "value_matched"),
        "rate_class_accuracy (summaries)": ratio("class_ok", "class_checked"),
        "effective_date_accuracy": ratio("effective_date_ok", "dated_docs"),
        "total_reconciliation": ratio("totals_found", "totals_expected"),
        "evidence_snippet_rate": ratio("snippets", "extracted"),
        "evidence_value_in_snippet_rate": ratio("snippets_with_value", "extracted"),
        "provenance_page_rate": ratio("with_page", "extracted"),
        "provenance_bbox_rate": ratio("with_bbox", "extracted"),
        "provenance_table_cell_rate": ratio("with_table_cell", "extracted"),
        "provenance_value_quote_rate": ratio("with_value_quote", "extracted"),
        "matched_with_page_rate": ratio("matched_with_page", "value_matched"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import yaml

    corpus = {d["doc_id"]: d for d in yaml.safe_load((BENCH / "corpus.yaml").read_text(encoding="utf-8"))["docs"]}
    scored = {}
    for run_path in args.runs:
        run = json.loads(Path(run_path).read_text(encoding="utf-8"))
        by_doc = {r["doc_id"]: r for r in run["results"]}
        docs = []
        for doc_id, entry in corpus.items():
            expected = json.loads((BENCH / "expected" / f"{doc_id}.json").read_text(encoding="utf-8"))
            docs.append(score_doc(expected, entry, by_doc.get(doc_id, {})))
        scored[run["label"]] = {"aggregate": aggregate(docs), "documents": docs}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(scored, indent=1, default=str), encoding="utf-8")

    labels = list(scored)
    keys = ["expected", "extracted", "value_matched", "value_accuracy (recall)", "precision", "unit_accuracy",
            "rate_class_accuracy (summaries)", "effective_date_accuracy", "total_reconciliation",
            "zero_doc_false_positives", "evidence_snippet_rate", "evidence_value_in_snippet_rate",
            "provenance_page_rate", "provenance_bbox_rate", "provenance_table_cell_rate",
            "provenance_value_quote_rate", "matched_with_page_rate", "seconds"]
    lines = ["# Extraction benchmark v0", "", "| metric | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(scored[l]["aggregate"].get(k)) for l in labels) + " |")
    lines += ["", "## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)", ""]
    lines.append("| doc | " + " | ".join(labels) + " |")
    lines.append("|---|" + "---|" * len(labels))
    for i, doc_id in enumerate(corpus):
        cells = []
        for l in labels:
            d = scored[l]["documents"][i]
            cells.append(
                f"{d['value_matched']}/{d['expected']}, x{d['extracted']}, u{len(d['unit_errors'])}, "
                f"c{len(d['class_errors'])}, {d['status']}, {d['text_source']}, {d['seconds']}"
            )
        lines.append(f"| {doc_id} | " + " | ".join(cells) + " |")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:len(keys) + 4]))


if __name__ == "__main__":
    main()
