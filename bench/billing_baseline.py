"""Freeze the 12-bill DEP reconciliation as a source-verified baseline.

Each bill is re-parsed from its stored raw text with the working-tree bill
parser (in memory; ``bill_statements`` is not rewritten), reconciled against a
scratch DB, and written with its evidence: the bill PDF hash, the printed
electric total, the front-page "Current Electric Charges", the selected base
tariff version, and per component whether the estimate came from a tariff or
from the observed-history fallback (amounts from saved bills, which is circular
evidence).  ``tests/test_billing_baseline.py`` checks the frozen file offline
and, with ``DUKE_RATES_TEST_LIVE_DB`` set, against a fresh reconciliation.

Usage::

    python bench/billing_baseline.py --db <scratch.db> \
        --out tests/fixtures/billing_baseline/progress_nc_12_bills.json

Refuses the production DB; ``Repository`` runs migrations, so pass a scratch copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRODUCTION_DB = (REPO / "data" / "db" / "duke_rates.db").resolve()
sys.path.insert(0, str(REPO / "src"))

from duke_rates.billing.reconciliation import (  # noqa: E402
    ProgressNCBillReconciliationService,
    _canonical_label,
)
from duke_rates.db.repository import Repository  # noqa: E402
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector  # noqa: E402
from duke_rates.parse.bill_parser import parse_bill_text  # noqa: E402


def estimate_source(details: list[str | None]) -> str:
    text = " ".join(d or "" for d in details)
    if "current bill observed" in text:
        return "same_bill_observed"
    if "observed history" in text:
        return "observed_history"
    return "tariff"


def reconcile_bill(service: ProgressNCBillReconciliationService, stored, raw_text: str) -> dict:
    statement = parse_bill_text(raw_text, source_path=stored.source_path)
    electric = statement.electric_section
    result = service.reconcile(bill_id=stored.id, statement=statement)
    version = result.selected_tariff.version
    estimated_by_key: dict[str, list] = {}
    for item in result.estimate.line_items:
        estimated_by_key.setdefault(_canonical_label(item.label), []).append(item)
    components = {}
    for item in result.line_items:
        est_items = estimated_by_key.get(item.key, [])
        components[item.key] = {
            "actual": item.actual_amount,
            "estimated": item.estimated_amount,
            "delta": item.delta,
            "estimate_source": estimate_source([getattr(i, "details", None) for i in est_items]) if est_items else None,
        }
    return {
        "bill_id": stored.id,
        "bill_date": statement.bill_date.isoformat(),
        "service_start": statement.service_start.isoformat(),
        "service_end": statement.service_end.isoformat(),
        "rate_code": electric.rate_code,
        "billed_kwh": result.billed_kwh,
        "printed_electric_total": electric.total_current_charges,
        "front_page_current_electric_charges": statement.billing_summary.current_electric_charges,
        "actual_line_items": [
            {"label": li.label, "amount": li.amount, "is_subperiod_detail": li.is_subperiod_detail}
            for li in electric.line_items
        ],
        "estimated_electric_total": result.estimated_electric_total,
        "total_delta": result.total_delta,
        "base_tariff": {
            "family_key": version.family_key,
            "source_kind": version.source_kind,
            "document_id": version.document_id,
            "effective_start": version.effective_start,
        },
        "components": components,
    }


def build_baseline(db: Path, bills_text_dir: Path) -> list[dict]:
    if Path(db).resolve() == PRODUCTION_DB:
        raise SystemExit("refusing to run against the production database")
    repository = Repository(Path(db))
    selector = ProgressNCHistoricalTariffSelector(repository)
    # Chains don't change during a read-only run; rebuilding them per bill
    # dominates the runtime on the full DB.
    chains: dict = {}
    build_chains = selector.lineage.build_chains

    def cached_build_chains(*a, **k):
        key = (a, tuple(sorted(k.items())))
        if key not in chains:
            chains[key] = build_chains(*a, **k)
        return chains[key]

    selector.lineage.build_chains = cached_build_chains
    service = ProgressNCBillReconciliationService(selector)
    bills = []
    for stored in repository.list_bill_statements():
        if "Actual Duke Bills" not in stored.source_path:
            continue
        pdf = REPO / stored.source_path
        raw_text = (Path(bills_text_dir) / f"{Path(stored.source_path).stem}.txt").read_text(encoding="utf-8")
        row = reconcile_bill(service, stored, raw_text)
        row["source_pdf"] = Path(stored.source_path).name
        row["source_pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest() if pdf.exists() else None
        bills.append(row)
    bills.sort(key=lambda b: b["bill_date"])
    return bills


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--bills-text-dir", default=str(REPO / "data" / "processed" / "bills"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    bills = build_baseline(Path(args.db), Path(args.bills_text_dir))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"bills": bills}, indent=1) + "\n", encoding="utf-8")
    for b in bills:
        print(b["bill_date"], b["rate_code"], b["printed_electric_total"], b["estimated_electric_total"], b["total_delta"],
              b["base_tariff"]["family_key"], b["base_tariff"]["document_id"], b["base_tariff"]["effective_start"])


if __name__ == "__main__":
    main()
