"""Parser for Duke Energy Indiana (IURC No. 16) tariff PDFs.

Extracts structured TariffVersionRecord, TariffChargeRecord, and
RiderApplicabilityRecord objects from Indiana rate schedule PDFs.

Indiana tariff format differs from NC/SC/FL:
- "IURC NO. 16 / Original Tariff No. 6" (not NCUC leaf-based)
- "First Revised Tariff No. 10-B / Cancels and supersedes Original Tariff No. 10-B"
- "Issued: January 29, 2025 / Effective: February 27, 2025"
- "RATE RS − RESIDENTIAL ELECTRIC SERVICE" schedule header
- "Connection Charge ...... $13.70" (dotted-leader format)
- "First 300 kWh ...... $0.186556 per kWh" tiered energy ($/kWh, not cents)
- "Over 1000 kWh ...... $0.123051 per kWh"
- "Next 700 kWh ...... $0.135777 per kWh"
- Multi-level connection charges: "Secondary ...... $31.90"
- "Each kW of Billing Maximum Load ...... $20.51 per kW" (demand)
- "Demand Charge ...... $7.95 per kW" (LLF-style)
- TOU: "Peak ...... $0.214198 per kWh" (labeled by period, no ¢)
- Rider applicability via Appendix A (Tariff Nos. 60, 62, 65, 66, 67, 68, 70, 72, 73, 74)
"""
from __future__ import annotations

import re
from pathlib import Path

from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffVersionRecord,
)
from duke_rates.parse.pdf_text import extract_pdf_text

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# "Original Tariff No. 6" / "First Revised Tariff No. 10-B"
# Also handles decimal sub-tariffs: "Tariff No. 6.5"
_REVISION_RE = re.compile(
    r'((?:Original|First\s+Revised|Second\s+Revised|Third\s+Revised|Fourth\s+Revised|'
    r'Fifth\s+Revised|\w+\s+Revised)\s+Tariff\s+No\.\s+[\w.\-]+)',
    re.I,
)
# "Cancels and supersedes Original Tariff No. 12"
_SUPERSEDES_RE = re.compile(
    r'Cancels\s+and\s+supersedes\s+'
    r'((?:Original|First\s+Revised|Second\s+Revised|Third\s+Revised|Fourth\s+Revised|'
    r'Fifth\s+Revised|\w+\s+Revised)\s+Tariff\s+No\.\s+[\w.\-]+)',
    re.I,
)

# "Effective: February 27, 2025"
_EFFECTIVE_RE = re.compile(
    r'Effective:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "RATE RS − RESIDENTIAL ELECTRIC SERVICE" / "RATE CS -" / "RATE HLF -"
# The dash may be Unicode minus (−, U+2212) or ASCII hyphen
_SCHEDULE_CODE_RE = re.compile(
    r'RATE\s+([A-Z][A-Z0-9.\-]*)\s+[−\-]',
    re.I,
)

# ---------------------------------------------------------------------------
# Charge line patterns (dotted-leader format: "Label .... $N.NN [per kWh]")
# ---------------------------------------------------------------------------

# Connection Charge (single flat): "Connection Charge ..... $13.70"
# Also "Connection Charge Per Month" on its own line followed by dollar line
_CONNECTION_CHARGE_RE = re.compile(
    r'Connection\s+Charge[^$\n]*?\$\s*([\d,]+\.?\d*)',
    re.I,
)

# Multi-level connection charges for large commercial/industrial (LLF/HLF):
# "Secondary ..... $31.90"
# "Primary and Primary Direct ..... $125.61"
# "Transmission ..... $855.37"
_MULTI_LEVEL_CONN_RE = re.compile(
    r'^(Secondary|Primary(?:\s+and\s+Primary\s+Direct)?|Primary\s+Direct|Transmission)'
    r'[.\u2026\s_]+\$\s*([\d,]+\.?\d*)\s*$',
    re.M | re.I,
)

# Tiered energy ($/kWh format — Indiana stores rates directly as $/kWh):
# "First 300 kWh ..... $0.186556 per kWh"
# "Next 700 kWh…..... $0.135777 per kWh"  (CS uses Unicode ellipsis U+2026)
# "Over 1000 kWh ..... $0.123051 per kWh"
# Also "All kWh ..... $N per kWh"
# Leader: any mix of dots, ellipsis (…), spaces, and underscores
_LEADER = r'[.\u2026\s_]+'
_TIER_ENERGY_RE = re.compile(
    r'(First\s+[\d,]+\s*kWh|Next\s+[\d,]+\s*kWh|Over\s+[\d,]+\s*kWh|All\s+kWh)'
    + _LEADER + r'\$\s*([\d,]+\.[\d]+)\s*per\s+kWh',
    re.I,
)

# Flat energy charge (LLF/HLF pattern):
# "Energy Charge ..... $0.100790 per kWh"
# "For All Energy Used Per Month ..... $0.044002 per kWh" (HLF per-voltage block)
_FLAT_ENERGY_RE = re.compile(
    r'(?:Energy\s+Charge|For\s+All\s+Energy\s+Used[^$\n]*?)' + _LEADER + r'\$\s*([\d,]+\.[\d]+)\s*per\s+kWh',
    re.I,
)

# TOU labeled energy charges (no ¢, direct $/kWh):
# "Peak ..... $0.214198 per kWh"
# "Off-Peak ..... $0.142799 per kWh"
# "Discount ..... $0.085679 per kWh"
_TOU_LABELED_RE = re.compile(
    r'^(Peak|Off-Peak|Discount|Super\s*Off-Peak|On-Peak|Mid-Peak)\s*[.\u2026]+\s*\$\s*([\d,]+\.[\d]+)\s*per\s+kWh',
    re.M | re.I,
)

# Demand charges:
# "Demand Charge ..... $7.95 per kW" (LLF flat demand)
_DEMAND_FLAT_RE = re.compile(
    r'Demand\s+Charge[^$\n]*?\$\s*([\d,]+\.[\d]+)\s*per\s+kW\b',
    re.I,
)

# "Each kW of Billing Maximum Load ..... $20.51 per kW"
# This appears multiple times with different voltage level headers
_MAX_LOAD_RE = re.compile(
    r'Each\s+kW\s+of\s+Billing\s+Maximum\s+Load[^$\n]*?\$\s*([\d,]+\.[\d]+)\s*per\s+kW\b',
    re.I,
)

# Voltage level headers that precede "Each kW of Billing Maximum Load":
# "Transmission Line Service at nominal voltage of 138,000, 230,000 or 345,000 Volts"
# "Primary Service at nominal voltage of 2,400 to 34,500 Volts"
# "Secondary Service at nominal voltage of 480 Volts or lower"
_VOLTAGE_LEVEL_RE = re.compile(
    r'^(Transmission\s+Line\s+Service\s+at\s+nominal\s+voltage\s+of\s+[\d,\s]+(?:or\s+[\d,]+)?\s+Volts'
    r'|Primary\s+Direct\s+Service\s+at\s+nominal\s+voltage\s+of\s+[\d,]+\s+to\s+[\d,]+\s+Volts'
    r'|Primary\s+Service\s+at\s+nominal\s+voltage\s+of\s+[\d,]+\s+to\s+[\d,]+\s+Volts'
    r'|Secondary\s+Service\s+at\s+nominal\s+voltage\s+of\s+[\d]+\s+Volts\s+or\s+lower)',
    re.M | re.I,
)

# kVAr charge: "For Each kVAr of the Monthly Billed kVAr Demand ..... $0.34 per kVAr"
_KVAR_RE = re.compile(
    r'For\s+Each\s+kVAr[^$\n]*?\$\s*([\d,]+\.[\d]+)\s*per\s+kVAr',
    re.I,
)

# Municipal siren flat charge: "$10.43 per delivery point"
_SIREN_RE = re.compile(
    r'municipal\s+siren[^$\n]*?\$\s*([\d,]+\.[\d]+)\s*per\s+delivery\s+point',
    re.I,
)

# Rider applicability: "Subject to the riders listed on Appendix A"
_APPENDIX_A_RE = re.compile(r'Subject\s+to\s+the\s+riders\s+listed\s+on\s+Appendix\s+A', re.I)

# Indiana standard rider tariff numbers (from Appendix A)
_RIDER_TARIFF_NUMBERS = [60, 62, 65, 66, 67, 68, 70, 72, 73, 74]

_TOU_PERIOD_MAP = {
    "peak": "on_peak",
    "on-peak": "on_peak",
    "off-peak": "off_peak",
    "discount": "discount",
    "super off-peak": "super_off_peak",
    "mid-peak": "mid_peak",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(text: str) -> str | None:
    import datetime
    for fmt in ("%B %d, %Y", "%B %-d, %Y"):
        try:
            return datetime.datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _extract_effective_start(text: str) -> str | None:
    m = _EFFECTIVE_RE.search(text)
    return _parse_date(m.group(1)) if m else None


def _build_revision_label(text: str) -> str | None:
    """Return the first (non-supersedes) tariff revision label."""
    # Find all revision matches and return the first one that isn't
    # immediately preceded by 'supersedes' or 'cancels'
    for m in _REVISION_RE.finditer(text):
        preceding = text[max(0, m.start() - 50): m.start()].lower()
        if "supersedes" not in preceding and "cancels" not in preceding:
            return m.group(1).strip()
    return None


def _build_supersedes_label(text: str) -> str | None:
    m = _SUPERSEDES_RE.search(text)
    return m.group(1).strip() if m else None


def _schedule_code_from_text(text: str) -> str:
    m = _SCHEDULE_CODE_RE.search(text)
    return m.group(1).upper() if m else ""


def _customer_class_for_schedule(schedule_code: str) -> str:
    code = schedule_code.upper()
    if code.startswith("RS"):
        return "residential"
    if code.startswith("SL") or code.startswith("LED") or code.startswith("MHLS") or code.startswith("UOLS") or code.startswith("MOLS"):
        return "lighting"
    return "general_service"


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_in_indiana_tariff(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse Indiana IURC No. 16 tariff PDF text into structured records.

    Returns:
        (version_record, charge_records, rider_applicability_records)
    """
    version = TariffVersionRecord(
        id=None,
        family_key=family_key,
        document_id=document_id,
        effective_start=_extract_effective_start(text),
        revision_label=_build_revision_label(text),
        supersedes_label=_build_supersedes_label(text),
        source_type="utility_current",
        confidence_score=0.85,
    )

    charges: list[TariffChargeRecord] = []
    riders: list[RiderApplicabilityRecord] = []

    schedule_code = _schedule_code_from_text(text)
    default_class = _customer_class_for_schedule(schedule_code)

    # --- Connection Charges ---
    # First check for multi-level (secondary/primary/transmission) connection charges
    multi_level = list(_MULTI_LEVEL_CONN_RE.finditer(text))
    if multi_level:
        for m in multi_level:
            level = m.group(1).strip().lower()
            val = float(m.group(2).replace(",", ""))
            if "secondary" in level:
                cust_class = "secondary"
            elif "transmission" in level:
                cust_class = "transmission"
            else:
                cust_class = "primary"
            snippet = text[max(0, m.start() - 20): m.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label=f"Connection Charge ({m.group(1).strip().title()})",
                    rate_value=val,
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=cust_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.92,
                )
            )
    else:
        # Single flat connection charge
        m_cc = _CONNECTION_CHARGE_RE.search(text)
        if m_cc:
            snippet = text[max(0, m_cc.start() - 20): m_cc.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label="Connection Charge",
                    rate_value=float(m_cc.group(1).replace(",", "")),
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.95,
                )
            )

    # --- TOU energy charges ---
    # Check for labeled TOU periods before tiered/flat energy
    tou_matches = list(_TOU_LABELED_RE.finditer(text))
    if tou_matches:
        for m in tou_matches:
            period_raw = m.group(1).strip().lower()
            tou_period = _TOU_PERIOD_MAP.get(period_raw, period_raw.replace(" ", "_"))
            rate = float(m.group(2).replace(",", ""))
            snippet = text[max(0, m.start() - 10): m.end() + 20]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="tou_energy",
                    charge_label=f"Energy Charge - {m.group(1).strip().title()}",
                    rate_value=rate,
                    rate_unit="$/kWh",
                    tou_period=tou_period,
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.92,
                )
            )
    else:
        # --- Tiered energy (RS/CS style: First N kWh, Next N kWh, Over N kWh) ---
        tier_matches = list(_TIER_ENERGY_RE.finditer(text))
        if tier_matches:
            cumulative_max = 0.0
            for m in tier_matches:
                qualifier = m.group(1).strip().lower()
                rate = float(m.group(2).replace(",", ""))
                if "first" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    cutoff = float(n.group(1).replace(",", "")) if n else None
                    tier_min, tier_max = 0.0, cutoff
                    cumulative_max = cutoff or 0.0
                    label = f"Energy Charge (first {int(cutoff or 0):,} kWh)"
                elif "next" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    block_size = float(n.group(1).replace(",", "")) if n else None
                    tier_min = cumulative_max
                    tier_max = (cumulative_max + block_size) if block_size else None
                    if tier_max:
                        cumulative_max = tier_max
                    label = f"Energy Charge (next {int(block_size or 0):,} kWh)"
                elif "over" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    cutoff = float(n.group(1).replace(",", "")) if n else cumulative_max
                    tier_min, tier_max = cutoff, None
                    label = "Energy Charge (over threshold)"
                else:
                    tier_min, tier_max = 0.0, None
                    label = "Energy Charge"
                snippet = text[max(0, m.start() - 10): m.end() + 30]
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="energy_block",
                        charge_label=label,
                        rate_value=rate,
                        rate_unit="$/kWh",
                        tier_min=tier_min,
                        tier_max=tier_max,
                        season="all_year",
                        customer_class=default_class,
                        source_snippet=snippet[:source_snippet_max],
                        confidence_score=0.93,
                    )
                )
        else:
            # Flat energy charge (LLF/HLF per-voltage-level or simple flat)
            # For multi-level tariffs, each voltage level block has its own energy rate.
            # Extract all voltage-level headers, then find the energy rate in each block.
            voltage_matches = list(_VOLTAGE_LEVEL_RE.finditer(text))
            if voltage_matches:
                for i, vm in enumerate(voltage_matches):
                    end_pos = voltage_matches[i + 1].start() if i + 1 < len(voltage_matches) else len(text)
                    block = text[vm.start(): end_pos]
                    em = _FLAT_ENERGY_RE.search(block)
                    if em:
                        rate = float(em.group(1).replace(",", ""))
                        voltage_label = vm.group(1).strip()
                        # Determine customer class from voltage level
                        if "transmission" in voltage_label.lower():
                            cust_class = "transmission"
                        elif "primary direct" in voltage_label.lower():
                            cust_class = "primary"
                        elif "primary" in voltage_label.lower():
                            cust_class = "primary"
                        else:
                            cust_class = "secondary"
                        snippet = block[max(0, em.start() - 20): em.end() + 30]
                        charges.append(
                            TariffChargeRecord(
                                version_id=version_id,
                                family_key=family_key,
                                charge_type="energy_block",
                                charge_label=f"Energy Charge ({voltage_label.split(' at ')[0].strip().title()})",
                                rate_value=rate,
                                rate_unit="$/kWh",
                                tier_min=0.0,
                                tier_max=None,
                                season="all_year",
                                customer_class=cust_class,
                                source_snippet=snippet[:source_snippet_max],
                                confidence_score=0.90,
                            )
                        )
            else:
                m_fe = _FLAT_ENERGY_RE.search(text)
                if m_fe:
                    rate = float(m_fe.group(1).replace(",", ""))
                    snippet = text[max(0, m_fe.start() - 20): m_fe.end() + 30]
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label="Energy Charge",
                            rate_value=rate,
                            rate_unit="$/kWh",
                            tier_min=0.0,
                            tier_max=None,
                            season="all_year",
                            customer_class=default_class,
                            source_snippet=snippet[:source_snippet_max],
                            confidence_score=0.90,
                        )
                    )

    # --- Demand charges ---
    # Flat demand (LLF): "Demand Charge ..... $7.95 per kW"
    m_dem = _DEMAND_FLAT_RE.search(text)
    if m_dem:
        snippet = text[max(0, m_dem.start() - 20): m_dem.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label="Demand Charge",
                rate_value=float(m_dem.group(1).replace(",", "")),
                rate_unit="$/kW",
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.90,
            )
        )

    # Maximum load charges (HLF multi-voltage):
    # Each "Each kW of Billing Maximum Load" line is preceded by a voltage level header
    voltage_matches = list(_VOLTAGE_LEVEL_RE.finditer(text))
    if voltage_matches:
        for i, vm in enumerate(voltage_matches):
            end_pos = voltage_matches[i + 1].start() if i + 1 < len(voltage_matches) else len(text)
            block = text[vm.start(): end_pos]
            mm = _MAX_LOAD_RE.search(block)
            if mm:
                rate = float(mm.group(1).replace(",", ""))
                voltage_label = vm.group(1).strip()
                if "transmission" in voltage_label.lower():
                    cust_class = "transmission"
                elif "primary direct" in voltage_label.lower():
                    cust_class = "primary"
                elif "primary" in voltage_label.lower():
                    cust_class = "primary"
                else:
                    cust_class = "secondary"
                snippet = block[max(0, mm.start() - 20): mm.end() + 30]
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="demand",
                        charge_label=f"Maximum Load Charge ({voltage_label.split(' at ')[0].strip().title()})",
                        rate_value=rate,
                        rate_unit="$/kW",
                        season="all_year",
                        customer_class=cust_class,
                        source_snippet=snippet[:source_snippet_max],
                        confidence_score=0.90,
                    )
                )
    else:
        # Fallback: flat max load line without voltage context
        mm = _MAX_LOAD_RE.search(text)
        if mm and not m_dem:  # Don't double-add if we already have a demand charge
            snippet = text[max(0, mm.start() - 20): mm.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="demand",
                    charge_label="Maximum Load Charge",
                    rate_value=float(mm.group(1).replace(",", "")),
                    rate_unit="$/kW",
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.88,
                )
            )

    # --- kVAr reactive power charge ---
    m_kvar = _KVAR_RE.search(text)
    if m_kvar:
        snippet = text[max(0, m_kvar.start() - 20): m_kvar.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label="kVAr Reactive Power Charge",
                rate_value=float(m_kvar.group(1).replace(",", "")),
                rate_unit="$/kVAr",
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.85,
            )
        )

    # --- Rider applicability ---
    # Indiana schedules reference riders via "Subject to the riders listed on Appendix A"
    if _APPENDIX_A_RE.search(text):
        for tariff_no in _RIDER_TARIFF_NUMBERS:
            rkey = f"in-indiana-tariff-{tariff_no}"
            riders.append(
                RiderApplicabilityRecord(
                    rider_family_key=rkey,
                    applies_to_family_key=family_key,
                    mandatory=True,
                    applicability_notes=f"Indiana Tariff No. {tariff_no} listed in Appendix A",
                    source_type="tariff_text",
                    confidence_score=0.90,
                )
            )

    return version, charges, riders


def parse_in_indiana_tariff_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse Indiana tariff."""
    text = extract_pdf_text(path)
    return parse_in_indiana_tariff(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )
