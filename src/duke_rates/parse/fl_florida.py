"""Parser for Duke Energy Florida (PE) sheet-number tariff PDFs.

Extracts structured TariffVersionRecord, TariffChargeRecord, and
RiderApplicabilityRecord objects from FL rate schedule and rider PDFs.

FL tariffs use a different format from NC/SC leaf-based schedules:
- "FORTY-FIFTH REVISED SHEET NO. 6.120" (ordinal word + SHEET NO. N.NNN)
- "CANCELS FORTY-FOURTH REVISED SHEET NO. 6.120"
- "EFFECTIVE: January 1, 2026"
- "RATE SCHEDULE RS-1" header
- "Customer Charge: $ 14.27" (may have metering-level sub-items)
- "Non-Fuel Energy Charge: 8.255¢ per kWh" (flat) or seasonal tiered blocks
- "11.032¢ per On-Peak kWh" (TOU)
- "$8.04 per kW of Billing Demand"
- BA-1 rider referenced by schedule name (not leaf number)
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

# "FORTY-FIFTH REVISED SHEET NO. 6.120" / "ORIGINAL SHEET NO. 6.120"
# Ordinal words used: FIRST, SECOND, ..., THIRTY-NINTH, FORTY-FIFTH, ONE HUNDRED ..., ORIGINAL
# Pattern: optional "ONE HUNDRED AND" prefix, then ordinal, optional REVISED, then SHEET NO. N.NNN
_ORDINAL_WORD = (
    r'(?:ONE\s+HUNDRED\s+AND\s+)?'           # optional century prefix
    r'(?:ORIGINAL|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH'
    r'|ELEVENTH|TWELFTH|THIRTEENTH|FOURTEENTH|FIFTEENTH|SIXTEENTH|SEVENTEENTH'
    r'|EIGHTEENTH|NINETEENTH|TWENTIETH'
    r'|TWENTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|THIRTIETH|THIRTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|FORTIETH|FORTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|FIFTIETH|FIFTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|SIXTIETH|SIXTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|SEVENTIETH|SEVENTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|EIGHTIETH|EIGHTY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|NINETIETH|NINETY[- ](?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH)'
    r'|HUNDREDTH|HUNDRED(?:TH)?(?:\s+AND\s+\w+TH?)?)'
)
_REVISION_RE = re.compile(
    r'(' + _ORDINAL_WORD + r'\s+(?:REVISED\s+)?(?:SHEET\s+)?NO\.\s+[\d.]+)',
    re.I,
)
# "CANCELS FORTY-FOURTH REVISED SHEET NO. 6.120"
_SUPERSEDES_RE = re.compile(
    r'CANCELS\s+(' + _ORDINAL_WORD + r'\s+(?:REVISED\s+)?(?:SHEET\s+)?NO\.\s+[\d.]+)',
    re.I,
)

# "EFFECTIVE: January 1, 2026" or "EFFECTIVE:  January 1, 2026"
_EFFECTIVE_RE = re.compile(
    r'EFFECTIVE:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "RATE SCHEDULE RS-1" — used for identification/validation but not stored
_SCHEDULE_CODE_RE = re.compile(
    r'RATE\s+SCHEDULES?\s+([A-Z][A-Z0-9\-]*)',
    re.I,
)

# Customer Charge — simplest single-dollar form:
# "Customer Charge:  $ 14.27"  or "Customer Charge: $14.27"
# Also handles "Unmetered Account: $10.29" and "Secondary Metering Voltage: $17.92"
_CUSTOMER_CHARGE_SIMPLE_RE = re.compile(
    r'Customer\s+Charge:\s*\$\s*([\d,]+\.?\d*)',
    re.I,
)
# Metering-level sub-lines: "Secondary Metering Voltage:  $17.92"
_METERED_CUSTOMER_CHARGE_RE = re.compile(
    r'(Secondary\s+Metering\s+Voltage|Primary\s+Metering\s+Voltage|Transmission\s+Metering\s+Voltage|Unmetered\s+Account):\s*\$\s*([\d,]+\.?\d*)',
    re.I,
)

# Flat "Non-Fuel Energy Charge: 8.255¢ per kWh"
_NONFUEL_ENERGY_RE = re.compile(
    r'Non-Fuel\s+Energy\s+Charge[s]?:\s*([\d.]+)[¢\u00a2\ufffd]\s*per\s+kWh',
    re.I,
)

# Seasonal tiered energy:
# "(1)  For the calendar months of December through February:
#      First 1,000 kWh  8.708¢ per kWh
#      All additional kWh  10.188¢ per kWh"
# Season header: "months of Month through Month:"
_SEASON_HEADER_RE = re.compile(
    r'(?:For\s+the\s+)?calendar\s+months\s+of\s+([A-Za-z]+\s+through\s+[A-Za-z]+)\s*:',
    re.I,
)
# Tier row within season block: "First 1,000 kWh  8.708¢ per kWh"
_SEASON_TIER_RE = re.compile(
    r'(First\s+[\d,]+\s*kWh|All\s+additional\s+kWh|Next\s+[\d,]+\s*kWh|All\s+kWh|[Oo]ver\s+[\d,]+\s*kWh)'
    r'\s+([\d.]+)[¢\u00a2\ufffd]\s*per\s+kWh',
    re.I,
)

# TOU energy: "11.032¢ per On-Peak kWh"
_TOU_ENERGY_RE = re.compile(
    r'([\d.]+)[¢\u00a2\ufffd]\s*per\s+(On-Peak|Off-Peak|Discount|Super\s*Off-Peak|Shoulder)\s*kWh',
    re.I,
)

_TOU_PERIOD_MAP = {
    "on-peak": "on_peak",
    "off-peak": "off_peak",
    "discount": "discount",
    "super off-peak": "super_off_peak",
    "shoulder": "shoulder",
}

# Demand charge: "Demand Charge:  $8.04 per kW of Billing Demand"
# Also: "Base Demand Charge: $2.81 per kW", "On-Peak Demand Charge: $2.20 per kW"
# Requires "Demand Charge" label to avoid picking up Premium Distribution adders etc.
_DEMAND_RE = re.compile(
    r'((?:Base|Mid-Peak|On-Peak|Off-Peak)\s+Demand\s+Charge|Demand\s+Charge):\s*\$\s*([\d.]+)\s*per\s+kW\b',
    re.I,
)

_SERVICE_CHARGE_RE = re.compile(
    r'(?:A\s+charge\s+of|greater\s+of|charge\s+shall\s+be)\s+\$([\d,]+\.?\d*)',
    re.I,
)

_LOAD_MANAGEMENT_CREDIT_RE = re.compile(
    r'(Electric\s+Space\s+Cooling\d*\s+[A-Z])\s+\$\s*([\d.]+)\s+Per\s+kW\s+([A-Za-z]+\s+thru\s+[A-Za-z]+)',
    re.I,
)

# Seasonal month ranges → season keys
_SEASON_MAP: dict[str, str] = {
    "december through february": "winter",
    "january through february": "winter",
    "march through november": "summer",
    "may through september": "summer",
    "october through april": "winter",
    "june through september": "summer",
    "november through march": "winter",
    "april through october": "summer",
    "march thru november": "summer",
}


def _season_from_range(months_text: str) -> str:
    key = months_text.strip().lower()
    return _SEASON_MAP.get(key, "all_year")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(text: str) -> str | None:
    import datetime
    # Try both zero-padded and non-padded day forms ("January 01, 2026" and "January 1, 2026")
    for fmt in ("%B %d, %Y", "%B %-d, %Y"):
        try:
            dt = datetime.datetime.strptime(text.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _extract_effective_start(text: str) -> str | None:
    m = _EFFECTIVE_RE.search(text)
    return _parse_date(m.group(1)) if m else None


def _build_revision_label(text: str) -> str | None:
    """Extract the first sheet revision label from the document text.

    Returns the full verbatim label, e.g. "FORTY-FIFTH REVISED SHEET NO. 6.120".
    """
    m = _REVISION_RE.search(text)
    return m.group(1).strip() if m else None


def _build_supersedes_label(text: str) -> str | None:
    m = _SUPERSEDES_RE.search(text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_fl_florida_sheet(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse FL Florida sheet PDF text into structured records.

    Returns:
        (version_record, charge_records, rider_applicability_records)
    """
    # --- Version record ---
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

    # --- Determine customer class from schedule code ---
    m_code = _SCHEDULE_CODE_RE.search(text)
    schedule_code = m_code.group(1).upper() if m_code else ""
    if schedule_code.startswith(("RS", "RST", "RSL")):
        default_class = "residential"
    elif schedule_code.startswith("LS"):
        default_class = "lighting"
    else:
        default_class = "general_service"

    # --- Customer charge ---
    # If metering-level lines exist, use secondary (most common billing class)
    metered_matches = list(_METERED_CUSTOMER_CHARGE_RE.finditer(text))
    if metered_matches:
        for m in metered_matches:
            level = m.group(1).strip().lower()
            val = float(m.group(2).replace(",", ""))
            if "secondary" in level:
                cust_class = "secondary"
            elif "primary" in level:
                cust_class = "primary"
            elif "transmission" in level:
                cust_class = "transmission"
            else:
                cust_class = "unmetered"
            snippet = text[max(0, m.start() - 20): m.end() + 20]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label=f"Customer Charge ({m.group(1).strip()})",
                    rate_value=val,
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=cust_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.92,
                )
            )
    else:
        m_cc = _CUSTOMER_CHARGE_SIMPLE_RE.search(text)
        if m_cc:
            snippet = text[max(0, m_cc.start() - 20): m_cc.end() + 20]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label="Customer Charge",
                    rate_value=float(m_cc.group(1).replace(",", "")),
                    rate_unit="$/month",
                    season="all_year",
                    customer_class=default_class,
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.95,
                )
            )

    # --- Service Charges schedule special charges ---
    if schedule_code == "SC-1":
        # SC-1 lists per-transaction charges. Some lines carry two amounts with explicit
        # customer class qualifiers: "shall be $200.00 for residential customers and
        # $1,000.00 for all other customers". Extract each pair with its customer class.
        _SC1_DUAL_RE = re.compile(
            r'\$\s*([\d,]+\.?\d*)\s+for\s+(\w[\w\s]+?)\s+(?:customers?)\s+and\s+'
            r'\$\s*([\d,]+\.?\d*)\s+for\s+(\w[\w\s]+?)\s+customers?',
            re.I,
        )
        for line in text.splitlines():
            if "$" not in line:
                continue
            label_prefix = line.split(":")[0].strip() or "Service Charge"
            # Try dual-amount pattern first (residential vs. commercial split)
            dual = _SC1_DUAL_RE.search(line)
            if dual:
                for val_str, cust_desc in [
                    (dual.group(1), dual.group(2).strip().lower()),
                    (dual.group(3), dual.group(4).strip().lower()),
                ]:
                    cust_class = "residential" if "residential" in cust_desc else "general_service"
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="transaction_charge",
                            charge_label=label_prefix,
                            rate_value=float(val_str.replace(",", "")),
                            rate_unit="$/bill",
                            season="all_year",
                            customer_class=cust_class,
                            source_snippet=line[:source_snippet_max],
                            confidence_score=0.80,
                        )
                    )
                continue
            # Single-amount lines — take only the first dollar value to avoid
            # picking up incidental dollar references in the same sentence
            single = re.search(r'\$([\d,]+\.?\d*)', line)
            if single:
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="transaction_charge",
                        charge_label=label_prefix,
                        rate_value=float(single.group(1).replace(",", "")),
                        rate_unit="$/bill",
                        season="all_year",
                        customer_class=default_class,
                        source_snippet=line[:source_snippet_max],
                        confidence_score=0.80,
                    )
                )

    # --- Load management schedule credits ---
    for m in _LOAD_MANAGEMENT_CREDIT_RE.finditer(text):
        season_key = _season_from_range(m.group(3))
        snippet = text[max(0, m.start() - 20): m.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="credit",
                charge_label=f"Load Management Credit ({m.group(1).strip()})",
                rate_value=float(m.group(2)),
                rate_unit="$/kW",
                season=season_key,
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.82,
            )
        )

    # --- TOU energy charges (check before flat/seasonal) ---
    seen_tou: set[str] = set()
    for m in _TOU_ENERGY_RE.finditer(text):
        rate = float(m.group(1))
        period_raw = m.group(2).strip().lower().replace(" ", "-")
        tou_period = _TOU_PERIOD_MAP.get(period_raw, period_raw)
        dedup_key = f"{tou_period}:{rate}"
        if dedup_key in seen_tou:
            continue
        seen_tou.add(dedup_key)
        snippet = text[max(0, m.start() - 10): m.end() + 20]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="tou_energy",
                charge_label=f"Non-Fuel Energy Charge - {m.group(2).strip().title()}",
                rate_value=round((rate) / 100.0, 6),
                rate_unit="$/kWh",
                tou_period=tou_period,
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.92,
            )
        )

    if not seen_tou:
        # --- Seasonal tiered energy ---
        # Look for "calendar months of Month through Month:" followed by tier rows
        season_matches = list(_SEASON_HEADER_RE.finditer(text))
        if season_matches:
            for i, sm in enumerate(season_matches):
                season_key = _season_from_range(sm.group(1))
                # Scan from this header to the next header (or end of "Non-Fuel Energy" section)
                if i + 1 < len(season_matches):
                    end_pos = season_matches[i + 1].start()
                else:
                    end_pos = len(text)
                block = text[sm.start(): end_pos]
                cumulative_max = 0.0
                for tm in _SEASON_TIER_RE.finditer(block):
                    qualifier = tm.group(1).strip().lower()
                    rate = float(tm.group(2))
                    if "first" in qualifier:
                        n = re.search(r'([\d,]+)', qualifier)
                        cutoff = float(n.group(1).replace(",", "")) if n else None
                        tier_min, tier_max = 0.0, cutoff
                        cumulative_max = cutoff or 0.0
                        label = f"Non-Fuel Energy Charge (first {int(cutoff or 0):,} kWh)"
                    elif "next" in qualifier:
                        n = re.search(r'([\d,]+)', qualifier)
                        block_size = float(n.group(1).replace(",", "")) if n else None
                        tier_min = cumulative_max
                        tier_max = (cumulative_max + block_size) if block_size else None
                        if tier_max:
                            cumulative_max = tier_max
                        label = f"Non-Fuel Energy Charge (next {int(block_size or 0):,} kWh)"
                    elif "additional" in qualifier or "over" in qualifier:
                        n = re.search(r'([\d,]+)', qualifier)
                        cutoff = float(n.group(1).replace(",", "")) if n else cumulative_max
                        tier_min, tier_max = cutoff, None
                        label = "Non-Fuel Energy Charge (additional kWh)"
                    else:
                        tier_min, tier_max = 0.0, None
                        label = "Non-Fuel Energy Charge"
                    snippet = block[max(0, tm.start() - 10): tm.end() + 20]
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=label,
                            rate_value=round((rate) / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=tier_min,
                            tier_max=tier_max,
                            season=season_key,
                            customer_class=default_class,
                            source_snippet=snippet[:source_snippet_max],
                            confidence_score=0.90,
                        )
                    )
        else:
            # Flat non-fuel energy charge
            m_nf = _NONFUEL_ENERGY_RE.search(text)
            if m_nf:
                snippet = text[max(0, m_nf.start() - 10): m_nf.end() + 20]
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="energy_block",
                        charge_label="Non-Fuel Energy Charge",
                        rate_value=round((float(m_nf.group(1))) / 100.0, 6),
                        rate_unit="$/kWh",
                        tier_min=0.0,
                        tier_max=None,
                        season="all_year",
                        customer_class=default_class,
                        source_snippet=snippet[:source_snippet_max],
                        confidence_score=0.92,
                    )
                )

    # --- Demand charges ---
    seen_demand: set[float] = set()
    for m in _DEMAND_RE.finditer(text):
        val = float(m.group(2))
        label = " ".join(m.group(1).split())
        if val in seen_demand or val == 0.0:
            continue
        seen_demand.add(val)
        snippet = text[max(0, m.start() - 30): m.end() + 30]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label=label,
                rate_value=val,
                rate_unit="$/kW",
                season="all_year",
                customer_class=default_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.88,
            )
        )

    # --- Rider / adjustment applicability ---
    # FL schedules reference BA-1 by name rather than leaf/sheet number.
    # Record the applicability if the schedule mentions BA-1.
    if re.search(r'Rate\s+Schedule\s+BA-1', text, re.I):
        ba1_key = _schedule_to_family_key("BA-1", family_key)
        riders.append(
            RiderApplicabilityRecord(
                rider_family_key=ba1_key,
                applies_to_family_key=family_key,
                mandatory=True,
                applicability_notes="Billing Adjustments BA-1 referenced in rate schedule text",
                source_type="tariff_text",
                confidence_score=0.90,
            )
        )

    # Guard against adding BA-1 a second time if the loop below also finds it
    seen_rider_keys: set[str] = {r.rider_family_key for r in riders}
    for m in re.finditer(r'Rate\s+Schedule\s+([A-Z][A-Z0-9\-]+)', text, re.I):
        code = m.group(1).upper()
        if code == schedule_code:
            continue  # skip self-reference
        rkey = _schedule_to_family_key(code, family_key)
        if rkey in seen_rider_keys:
            continue
        seen_rider_keys.add(rkey)
        # Only add BA-1; other cross-references are informational, not riders
        if code == "BA-1":
            riders.append(
                RiderApplicabilityRecord(
                    rider_family_key=rkey,
                    applies_to_family_key=family_key,
                    mandatory=True,
                    applicability_notes=f"Rate Schedule {code} referenced in tariff text",
                    source_type="tariff_text",
                    confidence_score=0.85,
                )
            )

    if not charges and "-BPL" in family_key.upper():
        # Fallback for BPL Rider if no specific charges extracted
        m = re.search(r"\$\s*(\d+\.\d+)", text)
        if m:
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="adjustment",
                    charge_label="BPL Rider Adjustment",
                    rate_value=float(m.group(1)),
                    rate_unit="$/bill",
                    season="all_year",
                    customer_class="all",
                    source_snippet=text[max(0, m.start() - 20): m.end() + 20],
                    confidence_score=0.50,
                )
            )

    return version, charges, riders


def _schedule_to_family_key(schedule_code: str, base_family_key: str) -> str:
    """Map a FL schedule code like 'BA-1' to a family key like 'fl-florida-pe-BA-1'."""
    parts = base_family_key.split("-")
    # Expect: fl-florida-pe-CODE or fl-florida-CODE
    if len(parts) >= 3:
        prefix = "-".join(parts[:3])  # e.g. "fl-florida-pe"
    else:
        prefix = "fl-florida-pe"
    return f"{prefix}-{schedule_code}"


def parse_fl_florida_sheet_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse FL Florida rate schedule sheet."""
    text = extract_pdf_text(path)
    return parse_fl_florida_sheet(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )
