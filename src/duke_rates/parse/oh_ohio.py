"""Parser for Duke Energy Ohio (P.U.C.O. Electric No. 19) tariff PDFs.

Extracts structured TariffVersionRecord, TariffChargeRecord, and
RiderApplicabilityRecord objects from Ohio rate schedule PDFs.

Ohio tariff format:
- "P.U.C.O. Electric No. 19" header
- "Sheet No. 30.18" (decimal revision) / "Cancels and Supersedes / Sheet No. 30.17"
- "Issued: December 16, 2022 / Effective: January 3, 2023"
- "RATE RS" / "RATE DS" / "RATE DM" / "RATE TD" schedule header
- "Distribution Charges" section (not "Base Rate")
- Column-aligned charges with Summer/Winter two-column layout:
    Summer Period    Winter Period
    Customer Charge  $8.00 per month  $8.00 per month
    Energy Charge
      First 2,800 kWh  $0.048863 per kWh  $0.048863 per kWh
      Additional kWh   $0.004339 per kWh  $0.004339 per kWh
- TOU seasonal energy: "On Peak kilowatt-hours  $0.079950  $0.063519 per kWh"
- All rates reference "Sheet No. 85, Applicable Riders" for rider listing
- Rider sets are defined in Sheet 85 (parsed or hardcoded per rate group)
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

# "Sheet No. 30.18" / "Sheet No. 85.2" / "Sheet No. 1"
_SHEET_RE = re.compile(
    r'Sheet\s+No\.?\s+([\d]+(?:\.\d+)?)',
    re.I,
)

# "Cancels and Supersedes ... Sheet No. 30.17"
# OH layout: "Cancels and Supersedes / 139 East Fourth Street / Sheet No. 30.17 / Cincinnati..."
_SUPERSEDES_RE = re.compile(
    r'Cancels\s+and\s+Supersedes.{0,120}?(Sheet\s+No\.?\s+[\d]+(?:\.\d+)?)',
    re.I | re.S,
)

# "Effective:  January 3, 2023" or "Effective: June 1, 2025"
_EFFECTIVE_RE = re.compile(
    r'Effective:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "RATE RS" / "RATE DS" / "RATE DM" / "RATE TD" etc.
_SCHEDULE_CODE_RE = re.compile(
    r'^\s*RATE\s+([A-Z][A-Z0-9.\-]*)\s*$',
    re.M | re.I,
)

# ---------------------------------------------------------------------------
# Multi-line charge helper
# ---------------------------------------------------------------------------
# OH PDFs also have whitespace between label and dollar amount (similar to KY).
_WS = r'[\s]{0,120}'

# ---------------------------------------------------------------------------
# Customer charge patterns
# ---------------------------------------------------------------------------

# Simple single-class customer charge:
# "    Customer Charge\n$8.00 per month\n $8.00 per month"  (seasonal two-col)
# or  "    Customer Charge\n$8.00 per month"
# We capture the FIRST dollar amount (summer or all-year)
_CUSTOMER_CHARGE_RE = re.compile(
    r'Customer\s+Charge' + _WS + r'\$\s*([\d,]+\.?\d*)\s+per\s+month',
    re.I,
)

# Multi-class customer charges:
# "Single Phase\n$12.00 per month\n$12.00 per month"
# "Three Phase\n$24.00 per month\n$24.00 per month"
# "Single Phase Service\n$23.00\n"  (DS — no "per month" on dollar line)
# "Single and/or Three Phase Service\n$46.00"  (DS)
# "Primary Voltage Service (12.5 or 34.5 kV)\n$100.00 per month"
_CUSTOMER_CHARGE_MULTI_RE = re.compile(
    r'(Single\s+Phase(?:\s+Service)?'
    r'|(?:Single\s+and/or\s+)?Three\s+Phase(?:\s+Service)?'
    r'|Primary\s+Voltage\s+Service[^\n$]*?)'
    + _WS + r'\$\s*([\d,]+\.?\d*)(?:\s+per\s+month)?(?=\s)',
    re.I,
)

# ---------------------------------------------------------------------------
# Energy charge patterns
# ---------------------------------------------------------------------------

# Flat energy (RS-style):
# "Energy Charge\n$0.039693 per kWh"
_FLAT_ENERGY_RE = re.compile(
    r'(?:Energy\s+Charge|All\s+kilowatt[-\s]hours?|All\s+kWh)'
    + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh',
    re.I,
)

# Tiered seasonal energy (two-column format):
# "First 1,000 kilowatt-hours\n$0.039693 per kWh\n $0.039298 per kWh"
# "First 2,800 kWh\n$0.048863 per kWh\n$0.048863 per kWh"
# "Additional  kilowatt-hours\n$0.039693 per kWh\n $0.021706 per kWh"
# "Additional  kWh\n$0.004339 per kWh\n$0.004339 per kWh"
# "Next 3,200 kWh\n$0.004339 per kWh\n$0.004339 per kWh"
# "Kilowatt-hours in excess of 150 times...\n$X per kWh\n$X per kWh"
_TIER_ENERGY_RE = re.compile(
    r'(First\s+[\d,]+\s*(?:kilowatt-hours?|kWh)'
    r'|Next\s+[\d,]+\s*(?:kilowatt-hours?|kWh)'
    r'|Additional\s+(?:kilowatt-hours?|kWh)'
    r'|Over\s+[\d,]+\s*(?:kilowatt-hours?|kWh)'
    r'|Kilowatt-hours?\s+in\s+excess[^\n]*(?:\n[^\n$]*)*?)'
    + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh'
    + r'(?:' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh)?',
    re.I,
)

# TOU energy (seasonal two-column format):
# "On Peak  kilowatt-hours\n$0.079950 per kWh\n$0.063519 per kWh"
# "Off Peak kilowatt-hours\n$0.013960 per kWh\n$0.013976 per kWh"
_TOU_ENERGY_RE = re.compile(
    r'(On\s+Peak\s+(?:kilowatt-hours?|kWh)'
    r'|Off\s+Peak\s+(?:kilowatt-hours?|kWh))'
    + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh'
    + r'(?:' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh)?',
    re.I,
)

# ---------------------------------------------------------------------------
# Demand charge patterns
# ---------------------------------------------------------------------------

# Flat demand: "All kilowatts\n$6.9678 per kW"
_FLAT_DEMAND_RE = re.compile(
    r'All\s+kilowatts' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)

# Tiered demand: "First 15 kilowatts ... $0.00" / "Additional kilowatts ... $X"
_TIERED_DEMAND_FIRST_RE = re.compile(
    r'First\s+([\d,]+)\s+kilowatts' + _WS + r'\$\s*([\d,]+\.?\d*)\s+per\s+kW\b',
    re.I,
)
_TIERED_DEMAND_ADDITIONAL_RE = re.compile(
    r'Additional\s+kilowatts' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)

# ---------------------------------------------------------------------------
# Rider applicability
# ---------------------------------------------------------------------------

# Ohio: all rates reference "Sheet No. 85, Applicable Riders"
# We parse the Sheet 85 table to determine which rider sheets apply per rate group.
# Fallback: if we can't parse the table, use these hardcoded rider sets.

# RS/ORH/TD/TD-CPP/RS3P/RSLI/DM/EH/CUR group
_RIDER_SHEETS_RESIDENTIAL = [77, 80, 83, 84, 86, 88, 89, 100, 101, 103, 105, 108,
                              110, 111, 112, 115, 119, 122, 126, 128]
# DS/DP group (no 122)
_RIDER_SHEETS_COMMERCIAL = [77, 80, 83, 84, 86, 88, 89, 100, 101, 103, 105, 108,
                             110, 111, 112, 115, 119, 126, 128]
# SL/TL/OL/NSU/NSP/SC/SE/UOLS/LED/GS-FL group (no 122)
_RIDER_SHEETS_LIGHTING = [77, 80, 83, 84, 86, 88, 89, 100, 101, 103, 105, 108,
                           110, 111, 112, 115, 126, 128]

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
    """Return the first sheet reference (the current version, not the superseded one)."""
    for m in _SHEET_RE.finditer(text):
        preceding = text[max(0, m.start() - 60): m.start()].lower()
        if "supersedes" not in preceding and "cancels" not in preceding:
            return m.group(0).strip()
    return None


def _build_supersedes_label(text: str) -> str | None:
    m = _SUPERSEDES_RE.search(text)
    return m.group(1).strip() if m else None


def _schedule_code_from_text(text: str) -> str:
    m = _SCHEDULE_CODE_RE.search(text)
    return m.group(1).upper() if m else ""


def _customer_class_for_schedule(schedule_code: str) -> str:
    code = schedule_code.upper()
    if code in ("RS", "ORH", "RS3P", "RSLI", "CUR"):
        return "residential"
    if code in ("SL", "TL", "OL", "LED", "UOLS", "NSU", "NSP", "SC", "SE", "GS-FL", "GSFL"):
        return "lighting"
    return "general_service"


def _rider_sheets_for_schedule(schedule_code: str) -> list[int]:
    code = schedule_code.upper()
    if code in ("RS", "ORH", "RS3P", "RSLI", "CUR", "TD", "TD-CPP", "TD-CPP", "DM", "EH", "TS"):
        return _RIDER_SHEETS_RESIDENTIAL
    if code in ("SL", "TL", "OL", "LED", "UOLS", "NSU", "NSP", "SC", "SE", "GS-FL", "GSFL"):
        return _RIDER_SHEETS_LIGHTING
    return _RIDER_SHEETS_COMMERCIAL


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_oh_ohio_tariff(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse Ohio P.U.C.O. Electric No. 19 tariff PDF text into structured records.

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

    # Detect if this is a seasonal two-column rate (has "Summer Period" in header area)
    is_seasonal = bool(re.search(r'Summer\s+Period', text, re.I))

    # --- Customer Charge ---
    multi_cc = list(_CUSTOMER_CHARGE_MULTI_RE.finditer(text))
    if multi_cc:
        seen_classes: set[str] = set()
        for m in multi_cc:
            label_raw = m.group(1).strip().lower()
            val = float(m.group(2).replace(",", ""))
            if "primary" in label_raw:
                cust_class = "primary"
            elif "three" in label_raw:
                cust_class = "three_phase"
            else:
                cust_class = default_class
            if cust_class in seen_classes:
                continue
            seen_classes.add(cust_class)
            label_clean = re.sub(r'\s*\([^)]*\)', '', m.group(1).strip()).strip().title()
            snippet = text[max(0, m.start() - 30): m.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label=f"Customer Charge ({label_clean})",
                    rate_value=val,
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=cust_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.93,
                )
            )
    else:
        m_cc = _CUSTOMER_CHARGE_RE.search(text)
        if m_cc:
            val = float(m_cc.group(1).replace(",", ""))
            snippet = text[max(0, m_cc.start() - 20): m_cc.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label="Customer Charge",
                    rate_value=val,
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.95,
                )
            )

    # --- Energy charges ---
    # Priority: TOU energy (On/Off Peak) > tiered seasonal > flat

    tou_matches = list(_TOU_ENERGY_RE.finditer(text))
    if tou_matches:
        # TOU seasonal energy (TD rate): Summer column + Winter column
        for m in tou_matches:
            period_label = re.sub(r'\s+', ' ', m.group(1).strip()).lower()
            summer_rate = float(m.group(2).replace(",", ""))
            winter_rate = float(m.group(3).replace(",", "")) if m.group(3) else None
            tou_period = "on_peak" if "on peak" in period_label else "off_peak"
            snippet = text[max(0, m.start() - 20): m.end() + 40]

            if winter_rate is not None and abs(summer_rate - winter_rate) > 1e-7:
                # Different rates: emit Summer + Winter records
                for season_name, rate in [("summer", summer_rate), ("winter", winter_rate)]:
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="tou_energy",
                            charge_label=f"Energy Charge - {season_name.title()} {'On' if tou_period == 'on_peak' else 'Off'} Peak",
                            rate_value=rate,
                            rate_unit="$/kWh",
                            tou_period=tou_period,
                            season=season_name,
                            customer_class=default_class,
                            source_snippet=snippet[:source_snippet_max],
                            confidence_score=0.92,
                        )
                    )
            else:
                # Same rate for both: emit all_year
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="tou_energy",
                        charge_label=f"Energy Charge - {'On' if tou_period == 'on_peak' else 'Off'} Peak",
                        rate_value=summer_rate,
                        rate_unit="$/kWh",
                        tou_period=tou_period,
                        season="all_year",
                        customer_class=default_class,
                        source_snippet=snippet[:source_snippet_max],
                        confidence_score=0.92,
                    )
                )

    else:
        tier_matches = list(_TIER_ENERGY_RE.finditer(text))
        if tier_matches:
            # Tiered seasonal energy (DM/ORH): each tier has summer + optional winter rate
            cumulative_max = 0.0
            for m in tier_matches:
                qualifier = re.sub(r'\s+', ' ', m.group(1).strip()).lower()
                summer_rate = float(m.group(2).replace(",", ""))
                winter_rate = float(m.group(3).replace(",", "")) if m.group(3) else None
                snippet = text[max(0, m.start() - 10): m.end() + 40]

                # Determine tier boundaries
                if "first" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    cutoff = float(n.group(1).replace(",", "")) if n else None
                    tier_min, tier_max = 0.0, cutoff
                    cumulative_max = cutoff or 0.0
                    label_base = f"Energy Charge (first {int(cutoff or 0):,} kWh)"
                elif "next" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    block = float(n.group(1).replace(",", "")) if n else None
                    tier_min = cumulative_max
                    tier_max = (cumulative_max + block) if block else None
                    if tier_max:
                        cumulative_max = tier_max
                    label_base = f"Energy Charge (next {int(block or 0):,} kWh)"
                elif "excess" in qualifier:
                    # "Kilowatt-hours in excess of 150 times monthly demand"
                    tier_min = cumulative_max
                    tier_max = None
                    label_base = "Energy Charge (excess of demand multiple)"
                else:
                    # "Additional kWh"
                    tier_min = cumulative_max
                    tier_max = None
                    label_base = "Energy Charge (additional kWh)"

                if winter_rate is not None and abs(summer_rate - winter_rate) > 1e-7:
                    for season_name, rate in [("summer", summer_rate), ("winter", winter_rate)]:
                        charges.append(
                            TariffChargeRecord(
                                version_id=version_id,
                                family_key=family_key,
                                charge_type="energy_block",
                                charge_label=f"{label_base} ({season_name.title()})",
                                rate_value=rate,
                                rate_unit="$/kWh",
                                tier_min=tier_min,
                                tier_max=tier_max,
                                season=season_name,
                                customer_class=default_class,
                                source_snippet=snippet[:source_snippet_max],
                                confidence_score=0.90,
                            )
                        )
                else:
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=label_base,
                            rate_value=summer_rate,
                            rate_unit="$/kWh",
                            tier_min=tier_min,
                            tier_max=tier_max,
                            season="all_year",
                            customer_class=default_class,
                            source_snippet=snippet[:source_snippet_max],
                            confidence_score=0.92,
                        )
                    )

        else:
            # Flat energy
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
    m_flat = _FLAT_DEMAND_RE.search(text)
    m_first = _TIERED_DEMAND_FIRST_RE.search(text)
    m_add = _TIERED_DEMAND_ADDITIONAL_RE.search(text)

    if m_first and m_add:
        first_kw = float(m_first.group(1).replace(",", ""))
        first_rate = float(m_first.group(2).replace(",", ""))
        add_rate = float(m_add.group(1).replace(",", ""))
        if first_rate > 0.0:
            snippet = text[max(0, m_first.start() - 20): m_first.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="demand",
                    charge_label=f"Demand Charge (first {int(first_kw):,} kW)",
                    rate_value=first_rate,
                    rate_unit="$/kW",
                    tier_min=0.0,
                    tier_max=first_kw,
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.90,
                )
            )
        snippet = text[max(0, m_add.start() - 20): m_add.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label=f"Demand Charge (additional kW over {int(first_kw):,})",
                rate_value=add_rate,
                rate_unit="$/kW",
                tier_min=first_kw,
                tier_max=None,
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.92,
            )
        )
    elif m_flat:
        rate = float(m_flat.group(1).replace(",", ""))
        snippet = text[max(0, m_flat.start() - 20): m_flat.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label="Demand Charge",
                rate_value=rate,
                rate_unit="$/kW",
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.90,
            )
        )

    # --- Rider applicability ---
    # Ohio uses a single "Sheet No. 85, Applicable Riders" reference from all rates.
    # Always emit rider links when the schedule has charges (i.e. is a real rate schedule).
    if charges or re.search(r'Sheet\s+No\.\s+85', text, re.I):
        rider_sheet_numbers = _rider_sheets_for_schedule(schedule_code)
        for sheet_no in rider_sheet_numbers:
            rkey = f"oh-ohio-sheet-{sheet_no}"
            riders.append(
                RiderApplicabilityRecord(
                    rider_family_key=rkey,
                    applies_to_family_key=family_key,
                    mandatory=True,
                    applicability_notes=f"OH Sheet No. {sheet_no} per Sheet 85 Applicable Riders table",
                    source_type="tariff_text",
                    confidence_score=0.88,
                )
            )

    return version, charges, riders


def parse_oh_ohio_tariff_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse Ohio tariff."""
    text = extract_pdf_text(path)
    return parse_oh_ohio_tariff(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )
