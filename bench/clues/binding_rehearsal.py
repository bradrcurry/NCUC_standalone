"""Phase 2E D3: rehearse reviewed document binding on a scratch copy, end to end, and unwind it.

    python bench/clues/binding_rehearsal.py --db SCRATCH --out reports/phase2e_d3/binding

Approvals are made by rehearsal actors; they are not human reviews. Refuses
the production database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from binding_state import state

from duke_rates.db import provenance as pv
from duke_rates.historical.ncuc import document_binding as db_
from duke_rates.historical.ncuc import document_promotion as dp
from duke_rates.historical.ncuc.pipeline.candidate_runs import extract_candidate

APPLY = (461, 399, 417, 569)          # create / replace_redline_source / create / create
SECOND_COPIES = (459, 570)            # same revision as 461 / 569: must block once those are bound
EXPECTED_BLOCKED = (483, 576, 540, 554)
PROPOSER, APPROVER, OPERATOR = "d3-rehearsal-proposer", "d3-rehearsal-approver", "d3-rehearsal-operator"


def main(db: str, out: Path) -> dict:
    if Path(db).resolve() == Path("data/db/duke_rates.db").resolve():
        raise SystemExit("refusing the production database")
    out.mkdir(parents=True, exist_ok=True)
    log: dict = {"before": state(db)}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    applied = []
    for cand in APPLY:
        p = dp.propose(conn, "bind_unbound_document", None, proposed_by=PROPOSER,
                       params=db_.params_from_gap_candidate(conn, cand), evidence={"gap_candidate_id": cand})
        assert p["state"] == "proposed", (cand, p["blocking_failures"])
        dp.approve(conn, p["proposal_id"], approved_by=APPROVER,
                   note="rehearsal on scratch; identity read in D1 page-image review; not a human approval")
        r = dp.apply(conn, p["proposal_id"], applied_by=OPERATOR)
        applied.append((cand, p["proposal_id"], r))
        log.setdefault("applied", []).append({"candidate": cand, "proposal_id": p["proposal_id"], **{
            k: r.get(k) for k in ("mode", "tariff_version_id", "historical_document_id", "created_version",
                                  "previous_historical_document_id", "charges_created", "charges_deleted",
                                  "post_apply_validation", "gap_before")}})
    for cand in SECOND_COPIES + EXPECTED_BLOCKED:
        p = dp.propose(conn, "bind_unbound_document", None, proposed_by=PROPOSER,
                       params=db_.params_from_gap_candidate(conn, cand))
        log.setdefault("blocked", []).append({"candidate": cand, "state": p["state"],
                                              "blocking_failures": p["blocking_failures"],
                                              "mode": p["plan"]["mode"],
                                              "existing": p["plan"].get("same_revision_versions")})
    log["after_apply"] = state(db)

    # Charges come only from a candidate over the bound pages, through the validated promotion.
    cand, pid, r = applied[0]
    conn.close()
    run = extract_candidate(Path(db), r["historical_document_id"], created_by=OPERATOR,
                            version_id=r["tariff_version_id"], structured_mode="hybrid")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    promo = pv.promote(conn, run["run_id"], promoted_by=OPERATOR, reason="rehearsal: bound sheet candidate",
                       acknowledge_review=True)          # default pathway: automated, fails closed
    totals = conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ? AND charge_label LIKE '%TOTAL%'",
                          (r["tariff_version_id"],)).fetchone()[0]
    log["promotion"] = {"run_id": run["run_id"], "validation": promo.get("validation"),
                        "inserted": promo.get("inserted"), "total_rows": totals,
                        "version_status": conn.execute("SELECT status FROM tariff_versions WHERE id = ?",
                                                       (r["tariff_version_id"],)).fetchone()[0]}
    try:
        dp.rollback(conn, pid, by=OPERATOR)
        log["early_rollback"] = "NOT REFUSED"
    except dp.PromotionError as exc:
        log["early_rollback"] = f"refused: {exc}"
    pv.rollback(conn, "tariff_version", r["tariff_version_id"], by=OPERATOR, reason="rehearsal unwind")
    log["unwind"] = [dp.rollback(conn, p, by=OPERATOR) for _, p, _ in reversed(applied)]
    conn.close()
    log["after_unwind"] = state(db)
    log["identical_after_unwind"] = {
        t: log["before"][t]["sha256"] == log["after_unwind"][t]["sha256"]
        for t in log["before"] if t != "gap_status"}
    (out / "rehearsal.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    return log


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    res = main(a.db, a.out)
    print(json.dumps({k: res[k] for k in ("applied", "blocked", "promotion", "early_rollback", "unwind",
                                          "identical_after_unwind")}, indent=1, default=str))
