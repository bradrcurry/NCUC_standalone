"""
Fix TD-DQ-NEW-001: DEC 2021 SGS/LGS/I/PG runaway extraction.

Problem:
  The CarolinasGeneralServiceScheduleProfile.extract() calls parse_nc_carolinas_leaf_file()
  with the full PDF path, which reads ALL pages of the 146-page E-7 Sub 1214 bundle.
  Each schedule's historical_document has a narrow page span (3-4 pages), but the profile
  ignores start_page/end_page — so the extractor accumulates demand charges from every
  sub-schedule variant in the bundle, producing 78-624 charges per version.

Fix:
  For each affected version, delete all tariff_charges and re-extract using fitz
  constrained to the correct page span from historical_documents.start_page/end_page.

Affected versions (all from e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf):
  tv=5268  nc-carolinas-schedule-I    2021-12-16  hd=450  pages 105-107  (624 charges)
  tv=5269  nc-carolinas-schedule-LGS  2021-12-16  hd=485  pages 108-110  (624 charges)
  tv=5274  nc-carolinas-schedule-PG   2021-12-16  hd=453  pages 116-119  (624 charges)
  tv=5283  nc-carolinas-schedule-SGS  2021-12-16  hd=484  pages 120-123  (78 charges)

Also fix 2026-01-01 utility_current versions: these have no historical_document link,
so they cannot be fixed by re-extraction without a new PDF download. They are left as-is
(7, 7, 5, 5 charges respectively — may be correct from utility_current source).
"""
import sys
import sqlite3
import logging
from pathlib import Path

import re
import fitz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf
from duke_rates.models.tariff import TariffChargeRecord

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path("data/db/duke_rates.db")

# version_id -> (family_key, hd_id, start_page, end_page, expected_charge_count_range)
# Pages are 1-based (as stored in DB), fitz uses 0-based
TARGETS = {
    5268: ("nc-carolinas-schedule-I",   450, 105, 107, (5, 20)),
    5269: ("nc-carolinas-schedule-LGS", 485, 108, 110, (5, 15)),
    5274: ("nc-carolinas-schedule-PG",  453, 116, 119, (5, 20)),
    5283: ("nc-carolinas-schedule-SGS", 484, 120, 123, (5, 20)),
}

PDF_PATH = Path("data/historical/ncuc/e-7-sub-1214/e31d1ef2-78de-4dab-8f84-5b6b6cb94c30.pdf")


# Schedule I uses a demand-band energy structure that the generic tiered parser misses.
# The structure is: "For the First 125 kWh per kW Billing Demand per Month:" (header)
#   then nested: "For the first 3,000 kWh per month, per kWh  11.2536¢"
# This regex extracts the nested per-kWh sub-tiers directly.
_I_NESTED_TIER_RE = re.compile(
    r'For\s+(the\s+first|the\s+next|all\s+over|all)\s+([\d,]+)\s*kWh\s+per\s+month,\s+per\s+kWh\s*'
    r'([\d]+\.[\d]+)\s*[\u00a2\xa2\ufffd]',
    re.I,
)
_I_ALL_KWH_RE = re.compile(
    r'For\s+all\s+kWh\s+per\s+month,\s+per\s+kWh\s*([\d]+\.[\d]+)\s*[\u00a2\xa2\ufffd]',
    re.I,
)


def extract_schedule_i_energy_charges(text: str, version_id: int, family_key: str) -> list[TariffChargeRecord]:
    """Extract Schedule I energy charges from the demand-banded nested structure.

    Schedule I has energy rates nested within demand bands:
      For the First 125 kWh per kW Billing Demand per Month:
        For the first 3,000 kWh per month, per kWh  11.2536¢
        For the next 87,000 kWh per month, per kWh   6.1669¢
        For all over 90,000 kWh per month, per kWh   5.8888¢
      For the Next 275 kWh per kW Billing Demand per Month:
        For the first 140,000 kWh per month, per kWh  4.9165¢
        For all over 140,000 kWh per month, per kWh   4.7345¢
      For all Over 400 kWh per kW Billing Demand per Month:
        For all kWh per month, per kWh  4.4995¢

    We extract all unique sub-tier rates using the nested line format.
    """
    charges = []
    seen_rates: set[float] = set()
    cumulative = 0.0

    # Extract nested per-kWh sub-tiers
    for m in _I_NESTED_TIER_RE.finditer(text):
        qualifier = m.group(1).lower().strip()
        n_str = m.group(2).replace(",", "")
        rate = float(m.group(3))
        n = float(n_str)

        if rate in seen_rates:
            continue
        seen_rates.add(rate)

        if "first" in qualifier:
            tier_min = 0.0
            tier_max = n
            label = f"Energy Charge (first {int(n):,} kWh)"
        elif "next" in qualifier:
            tier_min = cumulative
            tier_max = cumulative + n
            label = f"Energy Charge (next {int(n):,} kWh)"
        elif "over" in qualifier:
            tier_min = n
            tier_max = None
            label = f"Energy Charge (over {int(n):,} kWh)"
        else:
            tier_min = 0.0
            tier_max = None
            label = "Energy Charge"

        charges.append(TariffChargeRecord(
            version_id=version_id,
            family_key=family_key,
            charge_type="energy_block",
            charge_label=label,
            rate_value=round(rate / 100.0, 6),
            rate_unit="$/kWh",
            tier_min=tier_min,
            tier_max=tier_max,
            season="all_year",
            customer_class=None,
            source_snippet=m.group(0)[:200],
            confidence_score=0.90,
        ))

        if tier_max is not None:
            cumulative = tier_max

    # Also catch the "all kWh" flat-rate band
    m_all = _I_ALL_KWH_RE.search(text)
    if m_all:
        rate = float(m_all.group(1))
        if rate not in seen_rates:
            seen_rates.add(rate)
            charges.append(TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="energy_block",
                charge_label="Energy Charge (all kWh)",
                rate_value=round(rate / 100.0, 6),
                rate_unit="$/kWh",
                tier_min=0.0,
                tier_max=None,
                season="all_year",
                customer_class=None,
                source_snippet=m_all.group(0)[:200],
                confidence_score=0.90,
            ))

    return charges


def extract_bounded_text(pdf_path: Path, start_page: int, end_page: int) -> str:
    """Extract text from pages [start_page, end_page] inclusive (1-based page numbers)."""
    with fitz.open(pdf_path) as doc:
        pages_text = []
        for pg_num in range(start_page - 1, end_page):  # convert to 0-based
            if pg_num < len(doc):
                pages_text.append(doc[pg_num].get_text("text"))
        return "\n".join(pages_text)


def run_fix(dry_run: bool = False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if not PDF_PATH.exists():
        log.error("Source PDF not found: %s", PDF_PATH)
        return

    log.info("Source PDF: %s (%d pages)", PDF_PATH.name,
             fitz.open(PDF_PATH).page_count)

    total_deleted = 0
    total_inserted = 0

    for version_id, (family_key, hd_id, start_pg, end_pg, expected_range) in TARGETS.items():
        log.info("\n--- %s tv=%d hd=%d pages=%d-%d ---",
                 family_key, version_id, hd_id, start_pg, end_pg)

        # Check current state
        cur.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=?", (version_id,))
        current_count = cur.fetchone()[0]
        log.info("  Current charges: %d", current_count)

        # Extract bounded text
        text = extract_bounded_text(PDF_PATH, start_pg, end_pg)
        log.info("  Extracted %d chars from pages %d-%d", len(text), start_pg, end_pg)

        # Parse
        _, charges, _ = parse_nc_carolinas_leaf(
            text,
            version_id=version_id,
            family_key=family_key,
        )

        # Schedule I: the generic tiered parser misses nested demand-band energy structure.
        # Supplement with dedicated extractor for energy charges.
        if family_key == "nc-carolinas-schedule-I":
            energy_charges = extract_schedule_i_energy_charges(text, version_id, family_key)
            if energy_charges:
                # Replace any energy charges from the generic parser (likely wrong)
                charges = [c for c in charges if c.charge_type not in ("energy_block", "tou_energy")]
                charges.extend(energy_charges)
                log.info("  Schedule I: replaced generic energy with %d custom-extracted charges",
                         len(energy_charges))

        log.info("  Parsed %d charges", len(charges))

        # Validate
        lo, hi = expected_range
        if len(charges) < lo:
            log.warning("  WARNING: only %d charges parsed (expected %d-%d) — check parser",
                        len(charges), lo, hi)
        elif len(charges) > hi:
            log.warning("  WARNING: %d charges parsed (expected %d-%d) — possible contamination",
                        len(charges), lo, hi)
        else:
            log.info("  Charge count %d is within expected range %d-%d [OK]", len(charges), lo, hi)

        # Show parsed charges
        for c in charges:
            log.info("    %s | %s | %.6f %s | season=%s tier=%s-%s",
                     c.charge_type, c.charge_label, c.rate_value,
                     c.rate_unit, c.season, c.tier_min, c.tier_max)

        if dry_run:
            log.info("  DRY RUN: skipping DELETE/INSERT")
            continue

        # Delete existing charges
        cur.execute("DELETE FROM tariff_charges WHERE version_id=?", (version_id,))
        deleted = cur.rowcount
        total_deleted += deleted
        log.info("  Deleted %d old charges", deleted)

        # Insert new charges
        inserted = 0
        for charge in charges:
            cur.execute("""
                INSERT INTO tariff_charges (
                    version_id, family_key, charge_type, charge_label,
                    rate_value, rate_unit, tier_min, tier_max,
                    tou_period, season, customer_class,
                    source_snippet, confidence_score, notes, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """, (
                version_id, family_key,
                charge.charge_type, charge.charge_label,
                charge.rate_value, charge.rate_unit,
                charge.tier_min, charge.tier_max,
                charge.tou_period, charge.season, charge.customer_class,
                charge.source_snippet, charge.confidence_score,
                "fix_dq_new001 bounded reextract",
            ))
            inserted += 1
        total_inserted += inserted
        log.info("  Inserted %d new charges", inserted)

    if not dry_run:
        conn.commit()
        log.info("\n=== SUMMARY ===")
        log.info("Total deleted: %d", total_deleted)
        log.info("Total inserted: %d", total_inserted)
    else:
        log.info("\n=== DRY RUN complete — no changes made ===")

    conn.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run_fix(dry_run=dry_run)
