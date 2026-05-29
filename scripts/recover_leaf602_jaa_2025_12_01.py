"""Manual JAA charge recovery for leaf-602 2025-12-01 versions.

The bulk extractor only extracts the 2 demand charges ($/kW) from the
E-2 Sub 1354 Rev 3 JAA tariff sheet effective 2025-12-01, even though the
ProgressJaaRiderProfile correctly extracts all 8 charges when called
directly with the normalized text.

Root cause is unresolved — bulk extractor passes a different text shape
to the profile than direct invocation. Until that is debugged, this
script restores the 4 non-demand residential / SGS / MGS-non-demand /
seasonal charges + traffic-signal + outdoor-lighting that the bulk
extractor drops.

Without these rows, leaf-602 residential JAA picks an older 2024-12-01
value (0.482 c/kWh) instead of the actual 2025-12-01 value (0.464 c/kWh),
which is required for the Progress NC residential bill chain to
reconcile to leaf-600's 2.108 c/kWh.

Idempotent. Re-run after any re-extraction pass.
"""
import datetime as dt
import sqlite3

DB = "data/db/duke_rates.db"

# From E-2 Sub 1354 Rev 3 JAA tariff sheet, Non-Demand Rate Class table:
CHARGES = [
    ("JAA Rate - Residential", 0.00464, "residential",
     "Residential RES, R-TOUD, R-TOU, R-TOU-CPP"),
    ("JAA Rate - Small General Service", 0.00223, "commercial_small",
     "Small General Service SGS, SGS-TOUE, SGS-TOU-CPP"),
    ("JAA Rate - Medium General Service (non-demand)", 0.00411, "commercial_medium",
     "Medium General Service CH-TOUE"),
    ("JAA Rate - Seasonal and Intermittent", 0.01075, "seasonal_intermittent",
     "Seasonal and Intermittent Service SI"),
    ("JAA Rate - Traffic Signal", 0.00343, "traffic_signal",
     "Traffic Signal Service TSS, TFS"),
    ("JAA Rate - Outdoor Lighting", 0.01389, "lighting",
     "Outdoor Lighting Service ALS, SLS, SLR, SFLS"),
]


def main():
    db = sqlite3.connect(DB)
    c = db.cursor()
    c.execute("""SELECT id FROM tariff_versions
                 WHERE family_key='nc-progress-leaf-602'
                   AND effective_start='2025-12-01'
                   AND status NOT IN ('pending_document', 'misregistered_document')""")
    vids = [r[0] for r in c.fetchall()]
    if not vids:
        print("No leaf-602 2025-12-01 version found.")
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    total_inserted = 0
    for vid in vids:
        for label, value, cls, snippet_schedules in CHARGES:
            c.execute("""SELECT id FROM tariff_charges
                         WHERE version_id=? AND charge_label=? AND ABS(rate_value-?) < 1e-7""",
                      (vid, label, value))
            if c.fetchone():
                continue
            c.execute("""INSERT INTO tariff_charges
                (version_id, family_key, charge_type, charge_label, rate_value, rate_unit,
                 customer_class, source_snippet, confidence_score, notes, created_at)
                VALUES (?, 'nc-progress-leaf-602', 'rider_adjustment', ?, ?, '$/kWh', ?, ?, 0.85,
                        'Manually inserted — bulk extractor drops these rows on E-2 Sub 1354 Rev 3 even though ProgressJaaRiderProfile extracts them when called directly', ?)""",
                (vid, label, value, cls, f"{snippet_schedules} ... {value}", now))
            total_inserted += 1
    db.commit()
    print(f"Inserted {total_inserted} JAA charges across {len(vids)} leaf-602 2025-12-01 versions")


if __name__ == "__main__":
    main()
