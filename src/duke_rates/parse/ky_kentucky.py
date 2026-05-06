"""Parser for Duke Energy Kentucky (KY.P.S.C. Electric No. 2) tariff PDFs.

Extracts structured TariffVersionRecord, TariffChargeRecord, and
RiderApplicabilityRecord objects from Kentucky rate schedule PDFs.

Kentucky tariff format:
- "KY.P.S.C. Electric No. 2" header
- "Twenty-First Revised Sheet No. 30" / "Cancels and Supersedes / Twentieth Revised Sheet No. 30"
- "Issued: December 5, 2025 / Effective: January 2, 2026"
- "RATE RS" / "RATE DS" / "RATE DT" / "RATE TT" schedule header
- Column-aligned charges (no dotted leaders):
    Customer Charge            $14.75  per month
    All kilowatt hours         $0.126104  per kWh
    First 15   kilowatts       $  0.00   per kW
    Additional kilowatts       $  13.39  per kW
    First 6,000 kWh            $0.126995 per kWh
    Next 300 kWh/kW            $0.085527 per kWh
    Additional  kWh            $0.062852 per kWh
- Multi-class customer charges: Single Phase / Three Phase / Primary Voltage Service
- Seasonal TOU demand: "Summer / On Peak kW $16.16 per kW"
- Seasonal TOU energy: "Summer On Peak kWh $0.064689 per kWh"
- Riders listed explicitly: "Sheet No. 76, Rider ESM, ..."
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

# "Twenty-First Revised Sheet No. 30" / "Nineteenth Revised Sheet No. 44"
# Also "Original Sheet No. N"
_SHEET_RE = re.compile(
    r'((?:Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|'
    r'Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|'
    r'Nineteenth|Twentieth|Twenty-First|Twenty-Second|Twenty-Third|Twenty-Fourth|Twenty-Fifth|'
    r'Thirtieth|\w+-?\w*)\s+Revised\s+Sheet\s+No\.\s+[\w.\-]+|'
    r'Original\s+Sheet\s+No\.\s+[\w.\-]+)',
    re.I,
)

# "Cancels and Supersedes" followed (possibly on the next line after an address column) by the sheet label
# KY PDFs lay these out in a two-column header: "Cancels and Supersedes / 1262 Cox Road" on one line,
# then "Twentieth Revised Sheet No. 30 / Erlanger, KY 41018" on the next.
# We allow up to ~100 characters of non-matching content between "Supersedes" and the sheet label.
_SUPERSEDES_RE = re.compile(
    r'Cancels\s+and\s+Supersedes.{0,120}?'
    r'((?:Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|'
    r'Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|'
    r'Nineteenth|Twentieth|Twenty-First|Twenty-Second|Twenty-Third|Twenty-Fourth|Twenty-Fifth|'
    r'Thirtieth|\w+-?\w*)\s+Revised\s+Sheet\s+No\.\s+[\w.\-]+|'
    r'Original\s+Sheet\s+No\.\s+[\w.\-]+)',
    re.I | re.S,
)

# "Effective:  January 2, 2026"
_EFFECTIVE_RE = re.compile(
    r'Effective:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "RATE RS" / "RATE DS" / "RATE DT" / "RATE TT" / "RATE GS-FL"
# Schedule header line (no dash required — KY just uses "RATE XX" alone on its line)
_SCHEDULE_CODE_RE = re.compile(
    r'^\s*RATE\s+([A-Z][A-Z0-9.\-]*)\s*$',
    re.M | re.I,
)

# ---------------------------------------------------------------------------
# Multi-line charge helper
# ---------------------------------------------------------------------------
# KY PDFs extract with lots of whitespace between the label and dollar amount.
# Example:
#   "(a)  Customer Charge \n \n  \n \n \n$14.75          per month"
#   "    All kilowatt hours \n \n  \n \n \n$0.126104  per kWh"
#
# We use a helper that allows up to ~100 whitespace-padded characters between
# the label anchor and the dollar sign.
_WS = r'[\s]{0,120}'   # permissive whitespace gap (no non-whitespace chars between)

# ---------------------------------------------------------------------------
# Customer charge patterns
# ---------------------------------------------------------------------------

# Single-class customer charge (label then $ amount, possibly multi-line):
# "(a)  Customer Charge \n \n  \n \n \n$14.75          per month"
_CUSTOMER_CHARGE_SIMPLE_RE = re.compile(
    r'Customer\s+Charge' + _WS + r'\$\s*([\d,]+\.?\d*)\s+per\s+month',
    re.I,
)

# Multi-class customer charges on sub-lines:
# "    Single Phase Service       $  15.00      per month"
# "    Three Phase Service        $  30.00      per month"
# "    Single Phase               $  63.50      per month"  (DT format)
# "    Three Phase                $ 127.00      per month"  (DT format)
# "    Primary Voltage Service    $ 120.00      per month"
# "    Primary Voltage Service (12.5 or 34.5 kV)  $120.00  per month"
_CUSTOMER_CHARGE_MULTI_RE = re.compile(
    r'(Single\s+Phase(?:\s+Service)?|Three\s+Phase(?:\s+Service)?|Primary\s+Voltage\s+Service[^\n$]*?)'
    + _WS + r'\$\s*([\d,]+\.?\d*)\s+per\s+month',
    re.I,
)

# ---------------------------------------------------------------------------
# Energy charge patterns
# ---------------------------------------------------------------------------

# Flat all-kWh energy:
# "All kilowatt hours \n \n  \n \n \n$0.126104  per kWh"
# "All kWh   $N per kWh"
# "(b)  Energy Charge \n...\n$N per kWh"  — avoid matching this broadly;
#   instead catch it only when label is immediately followed by $
_FLAT_ENERGY_RE = re.compile(
    r'(?:All\s+kilowatt[-\s]hours?|All\s+kWh)' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh',
    re.I,
)

# "Energy Charge" flat (no tier qualifiers) — only match if no tier label on same/adjacent line
_ENERGY_CHARGE_FLAT_RE = re.compile(
    r'(?<!\w)Energy\s+Charge' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh',
    re.I,
)

# Tiered energy:
# "First 6,000 kWh    $0.126995 per kWh"
# "First 300 kWh/kW   $0.078643 per kWh"  (kWh/kW ratio tiers)
# "Next 300 kWh/kW    $0.085527 per kWh"
# "Additional  kWh    $0.062852 per kWh"
_TIER_ENERGY_RE = re.compile(
    r'(First\s+[\d,]+\s*kWh(?:/kW)?|Next\s+[\d,]+\s*kWh(?:/kW)?|Additional\s+kWh|Over\s+[\d,]+\s*kWh)'
    + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh',
    re.I,
)

# Seasonal TOU energy detection: the "Energy Charge" section header indicates TOU energy.
# We look for the (c) Energy Charge block and use separate season+period + $ patterns.
# Pattern 1 (DT-style): period label and $ on same/nearby line, season prefix on same line
#   "Summer On Peak kWh  $0.064689 per kWh"
# Pattern 2 (TT-style): season on own line, period on next, $ after whitespace
#   "Summer \n...\n On  Peak kWh \n...\n $ 0.075182 per kWh"
# Strategy: find each "On Peak kWh" / "Off Peak kWh" occurrence and look backward for season.
_TOU_ENERGY_PERIOD_RE = re.compile(
    r'(On\s+Peak\s+kWh|Off\s+Peak\s+kWh)' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kWh',
    re.I,
)
# Used to check whether the energy section exists at all
_ENERGY_CHARGE_SECTION_RE = re.compile(r'\(\s*c\s*\)\s+Energy\s+Charge', re.I)

# ---------------------------------------------------------------------------
# Demand charge patterns
# ---------------------------------------------------------------------------

# Simple flat demand: "All kilowatts   $10.44  per kW"
_FLAT_DEMAND_RE = re.compile(
    r'All\s+kilowatts' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)

# Tiered demand:
# "First 15   kilowatts   \n...\n$  0.00   per kW"
# "Additional kilowatts   \n...\n$  13.39  per kW"
_TIERED_DEMAND_FIRST_RE = re.compile(
    r'First\s+([\d,]+)\s+kilowatts' + _WS + r'\$\s*([\d,]+\.?\d*)\s+per\s+kW\b',
    re.I,
)
_TIERED_DEMAND_ADDITIONAL_RE = re.compile(
    r'Additional\s+kilowatts' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)

# Seasonal TOU demand:
# "    On Peak kW    \n...\n$   16.16   per kW"
# "    Off Peak kW   \n...\n$     1.45  per kW"
# "    Distribution kW   \n...\n$     6.96  per kW"
_SUMMER_BLOCK_RE = re.compile(r'\bSummer\b\s*\n', re.I)
_WINTER_BLOCK_RE = re.compile(r'\bWinter\b\s*\n', re.I)
_TOU_DEMAND_ON_PEAK_RE = re.compile(
    r'On\s+Peak\s+kW' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)
_TOU_DEMAND_OFF_PEAK_RE = re.compile(
    r'Off\s+Peak\s+kW' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)
_TOU_DEMAND_DIST_RE = re.compile(
    r'Distribution\s+kW' + _WS + r'\$\s*([\d,]+\.[\d]+)\s+per\s+kW\b',
    re.I,
)

# ---------------------------------------------------------------------------
# Rider listing pattern
# ---------------------------------------------------------------------------

# "Sheet No. 76, Rider ESM, Environmental Surcharge Mechanism Rider"
_RIDER_SHEET_RE = re.compile(
    r'Sheet\s+No\.\s+(\d+)',
    re.I,
)

# ---------------------------------------------------------------------------
# TOU period mapping
# ---------------------------------------------------------------------------

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
    """Return the first (non-supersedes) sheet revision label."""
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
    if code.startswith("RS"):
        return "residential"
    if code in ("SL", "TL", "OL", "LED", "UOLS", "NSU", "NSP", "SC", "SE"):
        return "lighting"
    return "general_service"


def _extract_rider_sheet_numbers(text: str) -> list[int]:
    """Extract sheet numbers from the Applicable Riders section."""
    # Find the section header line "Applicable Riders" (standalone, not embedded in prose)
    # KY PDFs use "2. Applicable Riders" or "Applicable Riders\n" as the section header.
    # The word also appears in preamble: "applicable riders, shall not exceed..."
    # We look for the section marker that is followed by a newline (i.e. the heading).
    riders_start = -1
    for m in re.finditer(r'Applicable\s+Riders\s*\n', text, re.I):
        riders_start = m.start()
        break
    if riders_start == -1:
        return []
    section = text[riders_start: riders_start + 800]
    # Truncate at sentinel markers that indicate we've left the rider list
    for sentinel in ("the minimum charge", "late payment", "ky.p.s.c.", "service provisions", "metering"):
        idx = section.lower().find(sentinel)
        if idx != -1:
            section = section[:idx]
            break
    return [int(m.group(1)) for m in _RIDER_SHEET_RE.finditer(section)]


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_ky_kentucky_tariff(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse Kentucky KY.P.S.C. Electric No. 2 tariff PDF text into structured records.

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

    # --- Customer Charge ---
    # Check for multi-class sub-lines first (Single Phase / Three Phase / Primary Voltage)
    multi_cc = list(_CUSTOMER_CHARGE_MULTI_RE.finditer(text))
    if multi_cc:
        for m in multi_cc:
            label_raw = m.group(1).strip().lower()
            val = float(m.group(2).replace(",", ""))
            if "primary" in label_raw:
                cust_class = "primary"
            elif "three" in label_raw:
                cust_class = "three_phase"
            else:
                cust_class = default_class
            # Normalize label: strip parenthetical voltage qualifier
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
        m_cc = _CUSTOMER_CHARGE_SIMPLE_RE.search(text)
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
    # Priority: TOU seasonal energy > tiered energy > flat energy
    #
    # TOU energy detection: look for "On Peak kWh" / "Off Peak kWh" patterns.
    # For each match, look backward up to 200 chars for a "Summer" or "Winter" label
    # to determine the season.

    tou_period_matches = list(_TOU_ENERGY_PERIOD_RE.finditer(text))
    if tou_period_matches:
        # Build a combined list of season headers and TOU period matches, sorted by position.
        # As we iterate, track the current season state.
        season_headers = list(re.finditer(r'\b(Summer|Winter)\b', text, re.I))
        # We'll assign season to each TOU match by finding the most recent season header before it.
        def _season_for_pos(pos: int) -> str:
            """Return most recent season name before pos, or 'all_year' if none."""
            last_season = None
            for sh in season_headers:
                if sh.start() < pos:
                    last_season = sh.group(1).lower()
                else:
                    break
            return last_season or "all_year"

        for m in tou_period_matches:
            period_label = re.sub(r'\s+', ' ', m.group(1).strip()).lower()
            rate = float(m.group(2).replace(",", ""))
            tou_period = "on_peak" if "on peak" in period_label else "off_peak"
            season = _season_for_pos(m.start())
            label = f"Energy Charge - {season.title()} {'On' if tou_period == 'on_peak' else 'Off'} Peak"
            snippet = text[max(0, m.start() - 30): m.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="tou_energy",
                    charge_label=label,
                    rate_value=rate,
                    rate_unit="$/kWh",
                    tou_period=tou_period,
                    season=season,
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.92,
                )
            )
    else:
        tier_matches = list(_TIER_ENERGY_RE.finditer(text))
        if tier_matches:
            # Tiered energy (DS: First 6000/Next 300/Additional, DP: First 300/Additional)
            cumulative_max = 0.0
            for m in tier_matches:
                qualifier = m.group(1).strip().lower()
                rate = float(m.group(2).replace(",", ""))
                is_kwh_per_kw = "kwh/kw" in qualifier  # kWh/kW ratio tiers
                if "first" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    cutoff = float(n.group(1).replace(",", "")) if n else None
                    tier_min, tier_max = 0.0, cutoff
                    cumulative_max = cutoff or 0.0
                    if is_kwh_per_kw:
                        label = f"Energy Charge (first {int(cutoff or 0):,} kWh/kW)"
                    else:
                        label = f"Energy Charge (first {int(cutoff or 0):,} kWh)"
                elif "next" in qualifier:
                    n = re.search(r'([\d,]+)', qualifier)
                    block_size = float(n.group(1).replace(",", "")) if n else None
                    tier_min = cumulative_max
                    tier_max = (cumulative_max + block_size) if block_size else None
                    if tier_max:
                        cumulative_max = tier_max
                    if is_kwh_per_kw:
                        label = f"Energy Charge (next {int(block_size or 0):,} kWh/kW)"
                    else:
                        label = f"Energy Charge (next {int(block_size or 0):,} kWh)"
                else:
                    # "Additional kWh" / "Over N kWh"
                    if "over" in qualifier:
                        n = re.search(r'([\d,]+)', qualifier)
                        cutoff = float(n.group(1).replace(",", "")) if n else cumulative_max
                        tier_min = cutoff
                    else:
                        tier_min = cumulative_max
                    tier_max = None
                    label = "Energy Charge (additional kWh)"
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
                        confidence_score=0.92,
                    )
                )
        else:
            # Flat energy: try "All kilowatt hours" / "All kWh" first, then "Energy Charge"
            m_fe = _FLAT_ENERGY_RE.search(text) or _ENERGY_CHARGE_FLAT_RE.search(text)
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

    # Check for TOU demand first (DT / TT rates)
    # Look for seasonal sub-blocks: Summer then Winter
    summer_m = _SUMMER_BLOCK_RE.search(text)
    winter_m = _WINTER_BLOCK_RE.search(text)

    if summer_m and winter_m:
        # TOU seasonal demand: parse each seasonal block separately
        # Build block boundaries: summer, winter, then rest
        blocks = []
        if summer_m.start() < winter_m.start():
            blocks = [
                ("summer", text[summer_m.start(): winter_m.start()]),
                ("winter", text[winter_m.start(): winter_m.start() + 400]),
            ]
        else:
            blocks = [
                ("winter", text[winter_m.start(): summer_m.start()]),
                ("summer", text[summer_m.start(): summer_m.start() + 400]),
            ]

        for season_name, block in blocks:
            m_on = _TOU_DEMAND_ON_PEAK_RE.search(block)
            m_off = _TOU_DEMAND_OFF_PEAK_RE.search(block)
            if m_on:
                rate = float(m_on.group(1).replace(",", ""))
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="demand",
                        charge_label=f"Demand Charge - On Peak ({season_name.title()})",
                        rate_value=rate,
                        rate_unit="$/kW",
                        tou_period="on_peak",
                        season=season_name,
                        customer_class=default_class,
                        source_snippet=block[max(0, m_on.start() - 20): m_on.end() + 30][:source_snippet_max],
                        confidence_score=0.90,
                    )
                )
            if m_off:
                rate = float(m_off.group(1).replace(",", ""))
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="demand",
                        charge_label=f"Demand Charge - Off Peak ({season_name.title()})",
                        rate_value=rate,
                        rate_unit="$/kW",
                        tou_period="off_peak",
                        season=season_name,
                        customer_class=default_class,
                        source_snippet=block[max(0, m_off.start() - 20): m_off.end() + 30][:source_snippet_max],
                        confidence_score=0.90,
                    )
                )

        # Distribution kW (appears once, outside seasonal blocks)
        m_dist = _TOU_DEMAND_DIST_RE.search(text)
        if m_dist:
            rate = float(m_dist.group(1).replace(",", ""))
            snippet = text[max(0, m_dist.start() - 20): m_dist.end() + 30]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="demand",
                    charge_label="Demand Charge - Distribution kW",
                    rate_value=rate,
                    rate_unit="$/kW",
                    tou_period=None,
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.88,
                )
            )
    else:
        # Non-TOU demand: flat or tiered
        m_flat = _FLAT_DEMAND_RE.search(text)
        m_first = _TIERED_DEMAND_FIRST_RE.search(text)
        m_add = _TIERED_DEMAND_ADDITIONAL_RE.search(text)

        if m_first and m_add:
            # Tiered demand: First N kW free / Additional kW at $X
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
            # Additional kW demand charge
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
    # KY rates list riders explicitly: "Sheet No. 76, Rider ESM, ..."
    sheet_numbers = _extract_rider_sheet_numbers(text)
    for sheet_no in sheet_numbers:
        rkey = f"ky-kentucky-sheet-{sheet_no}"
        riders.append(
            RiderApplicabilityRecord(
                rider_family_key=rkey,
                applies_to_family_key=family_key,
                mandatory=True,
                applicability_notes=f"KY Sheet No. {sheet_no} listed in Applicable Riders section",
                source_type="tariff_text",
                confidence_score=0.92,
            )
        )

    return version, charges, riders


def parse_ky_kentucky_tariff_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse Kentucky tariff."""
    text = extract_pdf_text(path)
    return parse_ky_kentucky_tariff(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )
