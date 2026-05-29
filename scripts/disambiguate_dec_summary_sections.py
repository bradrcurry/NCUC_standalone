"""Disambiguate DEC rider-SUMMARY rows by source-text schedule-group section.

The DEC SUMMARY tariff (E-7 dockets) lists per-component residential AND
general-service rate adjustments in separate sections. The current
parse_rider_summary regex doesn't recognize the DEC headers (which have a
schedule-list like "RS, RE, ES, RT, RSTC, RETC" between the rate-class name
and the "cents/kWh" marker), so all rows get lumped into a single
"Residential Schedules" rate-class block with customer_class='residential'.

This script re-tags the GS-section rows by looking up each charge's value
in the source text and checking whether it falls inside the
"Residential Schedules" or "General Service Schedules" section.

Run after extract-rates-nc to restore correct customer_class tagging.
Idempotent.

Coverage: nc-carolinas-rider-SUMMARY versions whose source doc contains both
a "Residential Schedules" and a "General Service Schedules" section header
(which is true for the 2021-01-01, 2024-07-01, and 2024-09-01 docs at least).

Also handles the "Total Rider Adjustments" mislabel where 1.7137 c/kWh
is tagged as residential but actually represents the GS schedule-group total.
"""
import argparse
import datetime as dt
import os
import re
import sqlite3
from collections import defaultdict

DB = "data/db/duke_rates.db"


def find_section_for_pos(pos, res_start, gs_start, indus_start, doc_end):
    if res_start <= pos < gs_start:
        return "residential"
    gs_end = indus_start if indus_start > 0 else doc_end
    if gs_start <= pos < gs_end:
        return "general_service"
    if indus_start > 0 and pos >= indus_start:
        return "industrial"
    return None


def disambiguate_version(c, version_id, eff_start, rtp, dry_run):
    if not rtp or not os.path.exists(rtp):
        return 0
    with open(rtp, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    res_start = txt.find("Residential Schedules")
    gs_start = txt.find("General Service Schedules")
    if res_start < 0 or gs_start < 0:
        return 0
    indus_start = txt.find("Industrial Service Schedules")
    doc_end = len(txt)

    c.execute(
        """SELECT tc.id, tc.charge_label, tc.rate_value FROM tariff_charges tc
           WHERE tc.version_id=? AND tc.customer_class='residential'
             AND tc.charge_label LIKE 'Residential Schedules%'
             AND tc.charge_label NOT LIKE '%Total%'""",
        (version_id,),
    )
    rows = c.fetchall()

    # Pair-based: for identical-value duplicates, higher id is GS
    groups = defaultdict(list)
    for cid, lbl, val in rows:
        suffix = lbl.replace("Residential Schedules - ", "").replace("Residential Schedules ", "").strip()
        groups[(suffix, round(val, 7))].append(cid)
    to_retag = set()
    for (suffix, val), ids in groups.items():
        if len(ids) == 2:
            to_retag.add(sorted(ids)[1])

    # Source-text-based: values that only appear in GS section
    for cid, lbl, val in rows:
        if cid in to_retag:
            continue
        val_str = f"{val * 100:.4f}"  # convert $/kWh to c/kWh format used in source
        positions = [m.start() for m in re.finditer(
            r"(?<![0-9])" + re.escape(val_str) + r"(?![0-9])", txt
        )]
        sections = {find_section_for_pos(p, res_start, gs_start, indus_start, doc_end) for p in positions}
        if "general_service" in sections and "residential" not in sections:
            to_retag.add(cid)

    if not to_retag:
        return 0
    if dry_run:
        print(f"  v={version_id} eff={eff_start}: would retag {len(to_retag)} rows")
        return len(to_retag)
    for cid in to_retag:
        c.execute(
            """UPDATE tariff_charges SET customer_class='general_service',
               charge_label=REPLACE(charge_label, 'Residential Schedules', 'General Service Schedules'),
               notes=COALESCE(notes,'') || ' [auto: section-disambiguated to GS]'
               WHERE id=?""",
            (cid,),
        )
    return len(to_retag)


def fix_total_rider_adjustments(c, version_id, eff_start, rtp, dry_run):
    """The 1.7137 c/kWh Total Rider Adjustments value is the GS total, not residential.
    The residential total should be the source-stated value (e.g. 1.4264 for 2024-09)."""
    if not rtp or not os.path.exists(rtp):
        return False
    with open(rtp, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    res_start = txt.find("Residential Schedules")
    gs_start = txt.find("General Service Schedules")
    if res_start < 0 or gs_start < 0:
        return False

    # Find TOTAL lines in each section
    res_total_match = re.search(
        r"TOTAL\s+cents/kWh\s+([\d.]+)", txt[res_start:gs_start], re.I
    )
    gs_total_match = re.search(
        r"TOTAL\s+cents/kWh\s+([\d.]+)", txt[gs_start:], re.I
    )
    res_total = float(res_total_match.group(1)) if res_total_match else None
    gs_total = float(gs_total_match.group(1)) if gs_total_match else None

    if res_total is None and gs_total is None:
        return False

    c.execute(
        """SELECT tc.id, tc.rate_value FROM tariff_charges tc
           WHERE tc.version_id=? AND tc.customer_class='residential'
             AND tc.charge_label='Residential Schedules Total Rider Adjustments'""",
        (version_id,),
    )
    rows = c.fetchall()
    actions = []
    has_res_total = False
    for cid, val in rows:
        cpkwh = val * 100
        if gs_total is not None and abs(cpkwh - gs_total) < 0.001:
            actions.append((cid, "retag_gs", val))
        elif res_total is not None and abs(cpkwh - res_total) < 0.001:
            has_res_total = True

    if dry_run:
        if actions:
            print(f"  v={version_id} eff={eff_start}: would retag {len(actions)} Total row(s) as GS, add residential={not has_res_total}")
        return len(actions) > 0 or (res_total is not None and not has_res_total)

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for cid, action, val in actions:
        c.execute(
            """UPDATE tariff_charges SET customer_class='general_service',
               charge_label='General Service Schedules Total Rider Adjustments',
               notes=COALESCE(notes,'') || ' [auto: GS total mislabeled as residential]'
               WHERE id=?""",
            (cid,),
        )
    if res_total is not None and not has_res_total:
        c.execute(
            """INSERT INTO tariff_charges
              (version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
               season, customer_class, source_snippet, confidence_score, notes, created_at)
              VALUES (?, 'nc-carolinas-rider-SUMMARY', 'rider_adjustment',
                      'Residential Schedules Total Rider Adjustments', ?, '$/kWh',
                      'all_year', 'residential', ?, 0.95,
                      'Auto-inserted by disambiguate_dec_summary_sections.py — parser missed this row', ?)""",
            (version_id, res_total / 100.0, f"TOTAL cents/kWh {res_total}", now),
        )
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry_run = not args.apply
    db = sqlite3.connect(DB)
    c = db.cursor()
    c.execute(
        """SELECT v.id, v.effective_start, hd.raw_text_path
           FROM tariff_versions v LEFT JOIN historical_documents hd ON hd.id=v.historical_document_id
           WHERE v.family_key='nc-carolinas-rider-SUMMARY' AND v.status='approved'
           ORDER BY v.effective_start"""
    )
    total_retag = 0
    total_total_fix = 0
    for vid, eff, rtp in c.fetchall():
        n = disambiguate_version(c, vid, eff, rtp, dry_run)
        total_retag += n
        if fix_total_rider_adjustments(c, vid, eff, rtp, dry_run):
            total_total_fix += 1
    if not dry_run:
        db.commit()
    print(f"\n{'Would retag' if dry_run else 'Retagged'} {total_retag} component rows; "
          f"{'would fix' if dry_run else 'fixed'} Total Rider Adjustments on {total_total_fix} versions")


if __name__ == "__main__":
    main()
