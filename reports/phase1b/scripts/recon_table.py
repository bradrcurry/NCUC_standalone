"""Build markdown reconciliation tables from bill_dump.py outputs + the March 21 report."""
import json
import sys
from pathlib import Path

S = Path(__file__).resolve().parents[1] / "dumps"
REPO = Path(r"C:\Python\Duke\Standalone")
march = {r["bill_date"]: r for r in json.loads((REPO / "data/processed/bill_accuracy/progress_nc_actual_bill_accuracy.json").read_text())}
runs = {name: {b["bill_date"]: b for b in json.loads((S / f"dump_{name}.json").read_text())} for name in sys.argv[1:]}
names = sys.argv[1:]

KEYS = ["basic_customer_charge", "energy_charge", "storm_recovery_charge", "summary_rider_adjustments",
        "clean_energy_rider", "energy_conservation_credit"]


def fallback_actual(b):
    return b["actual"] if b["actual"] is not None else round(sum(x["amount"] or 0 for x in b["actual_lines"]), 2)


print("| Bill | Rate | kWh | Actual | March-21 est (Δ) | " + " | ".join(f"{n} est (Δ)" for n in names) + " | Base version used (" + names[-1] + ") |")
print("|---|---|---|---|---|" + "---|" * len(names) + "---|")
for d in sorted(march):
    m = march[d]
    first = runs[names[0]][d]
    act = fallback_actual(first)
    cells = []
    for n in names:
        b = runs[n][d]
        cells.append(f"{b['est']:.2f} ({act - b['est']:+.2f})")
    v = runs[names[-1]][d]["version"]
    vlabel = f"{'hd' if v['source_kind'] == 'historical' else 'doc'} {v['document_id']} eff {v['effective_start']}"
    print(f"| {d} | {first['rate_code']} | {first['kwh']:g} | {act:.2f} | {m['estimated_electric_total']:.2f} ({m['total_delta']:+.2f}) | "
          + " | ".join(cells) + f" | {vlabel} |")

print()
print("Component detail (actual / estimate per run):")
print()
print("| Bill | Component | Actual | " + " | ".join(names) + " |")
print("|---|---|---|" + "---|" * len(names))
for d in sorted(march):
    for k in KEYS:
        recs = [{x["key"]: x for x in runs[n][d]["recon"]}.get(k) for n in names]
        if not any(recs):
            continue
        a = next((r["actual_amount"] for r in recs if r and r["actual_amount"] is not None), None)
        ests = [f"{r['estimated_amount']:.2f}" if r and r["estimated_amount"] is not None else "—" for r in recs]
        diff = len(set(ests)) > 1 or (a is not None and ests[-1] != "—" and abs(float(ests[-1]) - a) > 0.005)
        if not diff:
            continue
        print(f"| {d} | {k} | {a if a is not None else '—'} | " + " | ".join(ests) + " |")
