"""Backfill rider_applicability for NC riders that currently have no schedule links.

Maps each rider family to its applicable schedule families using the rider_descriptions
seed taxonomy (RES/SGS/MGS/LGS/SI/HP/OL...) plus per-rider knowledge for codes not
covered by the seed table.

DEC schedule mapping (short_code -> family_key):
    RES -> nc-carolinas-schedule-RS         (Residential Service)
    SGS -> nc-carolinas-schedule-SGS        (Small General Service)
    MGS -> (none, fold into SGS / LGS)
    LGS -> nc-carolinas-schedule-LGS        (Large General Service)
    SI  -> nc-carolinas-schedule-I          (Industrial)
    HP  -> nc-carolinas-schedule-HP         (Hourly Pricing)
    OL  -> nc-carolinas-schedule-OL         (Outdoor Lighting)

Progress (DEP) schedule mapping (short_code -> family_key):
    RES   -> nc-progress-leaf-500
    R-TOUD-> nc-progress-leaf-501
    R-TOU -> nc-progress-leaf-502
    R-TOU-CPP -> nc-progress-leaf-503
    R-TOU-EV  -> nc-progress-leaf-504
    SGS   -> nc-progress-leaf-520
    MGS   -> nc-progress-leaf-530
    LGS   -> nc-progress-leaf-533
    OL    -> nc-progress-leaf-570/571
    SLR   -> nc-progress-leaf-572

Run with --apply to commit; otherwise print plan.
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys

DB = "data/db/duke_rates.db"


DEC_SCHED_MAP = {
    "RES": ["nc-carolinas-schedule-RS"],
    "SGS": ["nc-carolinas-schedule-SGS"],
    "MGS": [],
    "LGS": ["nc-carolinas-schedule-LGS"],
    "SI": ["nc-carolinas-schedule-I"],
    "HP": ["nc-carolinas-schedule-HP"],
    "OL": ["nc-carolinas-schedule-OL"],
    "SFLS": ["nc-carolinas-schedule-FL"],
    "TSS": ["nc-carolinas-schedule-TS"],
}

DEP_RESIDENTIAL = [
    "nc-progress-leaf-500",
    "nc-progress-leaf-501",
    "nc-progress-leaf-502",
    "nc-progress-leaf-503",
    "nc-progress-leaf-504",
]
DEP_RESIDENTIAL_PLUS_SL = DEP_RESIDENTIAL + [
    "nc-progress-leaf-571",  # SLS
    "nc-progress-leaf-572",  # SLR
]
DEP_SGS_LGS = [
    "nc-progress-leaf-520",  # SGS-NM
    "nc-progress-leaf-521",  # SGS-TOU
    "nc-progress-leaf-530",  # MGS / TR
    "nc-progress-leaf-533",  # LGS
    "nc-progress-leaf-535",
    "nc-progress-leaf-536",
]
DEP_ALL_NON_LIGHTING = DEP_RESIDENTIAL + DEP_SGS_LGS + [
    "nc-progress-leaf-590",  # SLED / industrial
]

# Per-family curated applicability where the rider_descriptions seed is too generic.
# These reflect the rider's actual "Applicable to:" header on the published sheet.
PROGRESS_RIDER_APPLICABILITY = {
    # DSM/EE recovery riders typically apply to residential and small/general schedules.
    "nc-progress-leaf-640": ("RECD", DEP_RESIDENTIAL, "Residential Energy Conservation Discount — residential schedules only"),
    "nc-progress-leaf-641": ("EE-RIDER", DEP_RESIDENTIAL, "EE recovery — residential schedules"),
    "nc-progress-leaf-642": ("DSM-EE", DEP_ALL_NON_LIGHTING, "DSM/EE recovery — applies to all non-lighting classes"),
    "nc-progress-leaf-643": ("DSM-EE-2", DEP_ALL_NON_LIGHTING, "DSM/EE rider variant"),
    "nc-progress-leaf-644": ("DSM-1", DEP_ALL_NON_LIGHTING, "DSM rider"),
    "nc-progress-leaf-645": ("EE-1", DEP_ALL_NON_LIGHTING, "EE rider variant"),
    "nc-progress-leaf-646": ("EE-2", DEP_ALL_NON_LIGHTING, "EE rider variant"),
    "nc-progress-leaf-647": ("EE-3", DEP_ALL_NON_LIGHTING, "EE rider variant"),
    "nc-progress-leaf-648": ("TR",  DEP_RESIDENTIAL, "Tax Reform rider — residential"),
    "nc-progress-leaf-649": ("DSM-2", DEP_ALL_NON_LIGHTING, "DSM rider variant"),
    "nc-progress-leaf-650": ("BPM", DEP_RESIDENTIAL, "Bill Payment Mechanism (residential)"),
    "nc-progress-leaf-651": ("REPS-1", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-652": ("REPS-2", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-653": ("DSM-3", DEP_ALL_NON_LIGHTING, "DSM rider variant"),
    "nc-progress-leaf-654": ("EE-4", DEP_ALL_NON_LIGHTING, "EE rider variant"),
    "nc-progress-leaf-655": ("REPS-3", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-656": ("REPS-4", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-657": ("REPS-5", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-658": ("REPS-6", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-659": ("REPS-7", DEP_ALL_NON_LIGHTING, "REPS legacy variant"),
    "nc-progress-leaf-660": ("EE-PILOT", DEP_RESIDENTIAL, "EE residential pilot"),
    "nc-progress-leaf-661": ("EE-FUEL", DEP_ALL_NON_LIGHTING, "EE/fuel recovery"),
    "nc-progress-leaf-662": ("EPPWP", DEP_RESIDENTIAL, "Equal Payment Plan WeatherProtect — residential"),
    "nc-progress-leaf-663": ("NM-PROG", DEP_RESIDENTIAL, "Net Metering — residential"),
    "nc-progress-leaf-664": ("SOLAR-1", DEP_RESIDENTIAL, "Solar pilot — residential"),
    "nc-progress-leaf-665": ("SOLAR-2", DEP_RESIDENTIAL, "Solar pilot — residential"),
    "nc-progress-leaf-666": ("SOLAR-3", DEP_RESIDENTIAL, "Solar pilot — residential"),
    "nc-progress-leaf-667": ("EV-1", DEP_RESIDENTIAL, "EV charging pilot — residential"),
    "nc-progress-leaf-668": ("EV-2", DEP_RESIDENTIAL, "EV charging pilot — residential"),
    "nc-progress-leaf-669": ("EV-3", DEP_RESIDENTIAL, "EV charging pilot — residential"),
    "nc-progress-leaf-670": ("RSC", DEP_RESIDENTIAL, "Residential Solar Choice"),
    "nc-progress-leaf-671": ("RSC-2", DEP_RESIDENTIAL, "Residential Solar Choice variant"),
    "nc-progress-leaf-672": ("CEI", DEP_ALL_NON_LIGHTING, "Clean Energy Impact rider"),
    "nc-progress-leaf-674": ("RIDER-EE", DEP_ALL_NON_LIGHTING, "EE rider"),
}

# DEC carolinas riders.
DEC_RESIDENTIAL = ["nc-carolinas-schedule-RS"]
DEC_ALL = [
    "nc-carolinas-schedule-RS",
    "nc-carolinas-schedule-SGS",
    "nc-carolinas-schedule-LGS",
    "nc-carolinas-schedule-OPT-E",
    "nc-carolinas-schedule-OPT-H",
    "nc-carolinas-schedule-OPT-I",
    "nc-carolinas-schedule-OPT-G",
    "nc-carolinas-schedule-I",
    "nc-carolinas-schedule-HP",
]
DEC_RES_AND_GS = [
    "nc-carolinas-schedule-RS",
    "nc-carolinas-schedule-SGS",
    "nc-carolinas-schedule-LGS",
]

DEC_RIDER_APPLICABILITY = {
    "nc-carolinas-rider-CAR": ("CAR", DEC_ALL, "Customer Assistance Recovery — applied broadly"),
    "nc-carolinas-rider-CEI": ("CEI", DEC_ALL, "Clean Energy Impact rider"),
    "nc-carolinas-rider-CEPS": ("CEPS", DEC_ALL, "Clean Energy Plan Surcharge (formerly REPS)"),
    "nc-carolinas-rider-EB":  ("EB",  DEC_ALL, "Energy Beacon / EE rider"),
    "nc-carolinas-rider-EC":  ("EC",  DEC_ALL, "EDIT Class C"),
    "nc-carolinas-rider-ED":  ("ED",  DEC_ALL, "EDIT Class D / Excess Deferred Income Tax"),
    "nc-carolinas-rider-ER":  ("ER",  DEC_ALL, "EDIT Class E / refund"),
    "nc-carolinas-rider-GS":  ("GS",  ["nc-carolinas-schedule-SGS","nc-carolinas-schedule-LGS"], "General Service rider — applies to GS schedules"),
    "nc-carolinas-rider-GSA": ("GSA", ["nc-carolinas-schedule-SGS","nc-carolinas-schedule-LGS"], "General Service adjustment"),
    "nc-carolinas-rider-IQHEU":("IQHEU",DEC_RESIDENTIAL, "Income-Qualified Home Energy Use — residential"),
    "nc-carolinas-rider-IS":  ("IS",  ["nc-carolinas-schedule-I"], "Industrial Service rider"),
    "nc-carolinas-rider-MRM": ("MRM", DEC_RESIDENTIAL, "Residential Mid-Range Modifier / decoupling"),
    "nc-carolinas-rider-NM":  ("NM",  DEC_RESIDENTIAL, "Net Metering — residential"),
    "nc-carolinas-rider-NMB": ("NMB", DEC_RESIDENTIAL, "Net Metering Bridge — residential"),
    "nc-carolinas-rider-PM":  ("PM",  DEC_ALL, "Performance Mechanism / PIM"),
    "nc-carolinas-rider-PS":  ("PS",  DEC_RESIDENTIAL, "Prepay Service rider — residential"),
    "nc-carolinas-rider-RDM": ("RDM", DEC_RESIDENTIAL, "Residential Decoupling Mechanism"),
    "nc-carolinas-rider-RIDERCPRE":  ("CPRE", DEC_ALL, "Competitive Procurement of Renewable Energy"),
    "nc-carolinas-rider-RIDEREDIT3": ("EDIT-3", DEC_ALL, "EDIT-3 / amortization"),
    "nc-carolinas-rider-RIDERLC":    ("LC", DEC_ALL, "Liability Charge rider"),
    "nc-carolinas-rider-RIDERNPTC":  ("NPTC", DEC_ALL, "Nuclear / Plant Tax Credit rider"),
    "nc-carolinas-rider-RSC": ("RSC", DEC_RESIDENTIAL, "Residential Solar Choice"),
    "nc-carolinas-rider-SBES":("SBES", DEC_ALL, "Schedule-based Energy Surcharge"),
    "nc-carolinas-rider-SCG": ("SCG", DEC_ALL, "Storm Cost Generation rider"),
    "nc-carolinas-rider-SSR": ("SSR", DEC_ALL, "Severe Storm Recovery rider"),
    "nc-carolinas-rider-STS": ("STS", DEC_ALL, "Storm Securitization rider — broad"),
    "nc-carolinas-rider-US":  ("US",  ["nc-carolinas-schedule-I","nc-carolinas-schedule-LGS"], "Utility Service / industrial rider"),
}


def existing_schedules(c):
    c.execute("SELECT DISTINCT family_key FROM tariff_versions")
    return {r[0] for r in c.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    c = db.cursor()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "+00:00"

    valid = existing_schedules(c)

    plan = []
    for rider_fk, (code, schedules, note) in {**PROGRESS_RIDER_APPLICABILITY, **DEC_RIDER_APPLICABILITY}.items():
        # Skip if rider family doesn't exist
        c.execute("SELECT 1 FROM tariff_versions WHERE family_key=? LIMIT 1", (rider_fk,))
        if not c.fetchone():
            continue
        # Skip if rider already has applicability rows (idempotent)
        c.execute("SELECT COUNT(*) FROM rider_applicability WHERE rider_family_key=?", (rider_fk,))
        if c.fetchone()[0] > 0:
            continue
        for sched_fk in schedules:
            if sched_fk not in valid:
                continue
            plan.append((rider_fk, sched_fk, code, note))

    print(f"Plan: insert {len(plan)} applicability rows")
    rider_counts = {}
    for r, s, code, note in plan:
        rider_counts[r] = rider_counts.get(r, 0) + 1
    for r, n in sorted(rider_counts.items()):
        print(f"  {r:40s} -> {n} schedules")

    if not args.apply:
        print("\n--dry run-- (use --apply to commit)")
        return

    inserted = 0
    for rider_fk, sched_fk, code, note in plan:
        c.execute("""
            INSERT INTO rider_applicability
              (rider_family_key, applies_to_family_key, mandatory, applicability_notes,
               source_type, confidence_score, created_at, enrollment_type, in_rider_summary)
            VALUES (?, ?, 1, ?, 'manual', 0.8, ?, 'mandatory', 1)
        """, (rider_fk, sched_fk, f"Seeded {code}: {note}", now))
        inserted += 1
    db.commit()
    print(f"\nApplied: inserted {inserted} rider_applicability rows.")


if __name__ == "__main__":
    main()
