"""Dump full 12-bill reconciliation detail (selected versions + every line) for one DB.

Usage: python bill_dump.py <src_dir> <scratch_db> <out_json>
Refuses the production DB.
"""
import json
import sys
from pathlib import Path

REPO = Path(r"C:\Python\Duke\Standalone")
PROD = (REPO / "data" / "db" / "duke_rates.db").resolve()

src, db, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
only = sys.argv[4] if len(sys.argv) > 4 else None  # optional bill_date filter
if db.resolve() == PROD:
    raise SystemExit("refusing production DB")
sys.path.insert(0, str(Path(src).resolve()))

from duke_rates.billing.reconciliation import ProgressNCBillReconciliationService  # noqa: E402
from duke_rates.db.repository import Repository  # noqa: E402
from duke_rates.historical.tariff_selector import ProgressNCHistoricalTariffSelector  # noqa: E402
from duke_rates.models.bill import BillStatementData  # noqa: E402

repo = Repository(db)
sel = ProgressNCHistoricalTariffSelector(repo)
_chains = {}
_orig = sel.lineage.build_chains
def _cached(*a, **k):  # memoize: chains don't change during a read-only run
    key = (a, tuple(sorted(k.items())))
    if key not in _chains:
        _chains[key] = _orig(*a, **k)
    return _chains[key]
sel.lineage.build_chains = _cached
svc = ProgressNCBillReconciliationService(sel)
rows = []
for stored in repo.list_bill_statements():
    if "Actual Duke Bills" not in stored.source_path:
        continue
    st = BillStatementData.model_validate_json(stored.statement_json)
    if st.electric_section is None:
        continue
    if only and str(stored.bill_date) != only:
        continue
    rec = {"bill_id": stored.id, "bill_date": str(stored.bill_date), "source": stored.source_path}
    try:
        r = svc.reconcile(bill_id=stored.id, statement=st)
        v = r.selected_tariff.version
        sch = r.selected_tariff.schedule
        rec.update(
            rate_code=r.rate_code, kwh=r.billed_kwh, actual=r.actual_electric_total,
            est=r.estimated_electric_total, delta=r.total_delta,
            version=v.model_dump(mode="json"),
            schedule=sch.model_dump(mode="json"),
            est_lines=[i.model_dump(mode="json") for i in r.estimate.line_items],
            recon=[i.model_dump(mode="json") for i in r.line_items],
            actual_lines=[i.model_dump(mode="json") for i in st.electric_section.line_items],
            notes=r.notes,
        )
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)
    rows.append(rec)
rows.sort(key=lambda x: x["bill_date"])
out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
for x in rows:
    print(x["bill_date"], x.get("rate_code"), x.get("kwh"), x.get("actual"), x.get("est"), x.get("delta"),
          (x.get("version") or {}).get("family_key"), (x.get("version") or {}).get("document_id"),
          (x.get("version") or {}).get("effective_start"), x.get("error", ""))
