"""Parser for Duke Energy Carolinas (NC) leaf-number tariff PDFs.

Extracts structured TariffVersionRecord, TariffChargeRecord, and
RiderApplicabilityRecord objects from NC Carolinas rate schedule and rider PDFs.

NC Carolinas uses a similar leaf structure to NC Progress but with:
- "Duke Energy Carolinas, LLC" company name
- "RATE" section header (not "MONTHLY RATE")
- Roman-numeral charge labels (I., II., III.)
- Tiered energy: "For the first 3,000 kWh per month, per kWh  13.9065¢"
- Rider listing: "Leaf No. 60  Fuel Cost Adjustment Rider"
- Family keys: nc-carolinas-schedule-RS, nc-carolinas-rider-EE, etc.
"""
from __future__ import annotations

import re
from pathlib import Path

from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffVersionRecord,
)

# Reuse rider extraction logic from NC Progress
from duke_rates.parse.nc_progress import _extract_rider_rates
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.historical.ncuc.pipeline.ocr_normalization import normalize_ocr_text

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# "NC Fifty-Ninth Revised Leaf No. 11" / "SC Fifty-Ninth Revised Leaf No. 11"
# Accepts both NC and SC state prefixes for Carolinas SC compatibility
_REVISION_RE = re.compile(
    r'(?:NC|SC)\s+((?:(?:Original|[A-Za-z\-]+)(?:\s+Revised)?\s+)+)?Leaf\s+No\.\s+(\d+)',
    re.I,
)
_SUPERSEDES_RE = re.compile(
    r'Superseding\s+(?:NC|SC)\s+(?:(?:Original|[A-Za-z\-]+)(?:\s+Revised)?\s+)?Leaf\s+No\.\s+\d+',
    re.I,
)

# "Effective for service rendered on and after January 1, 2026"
# Also: "Effective for bills rendered on and after ..."
# Also: "Effective for service on and after ..."
# Also: "Effective for service rendered from January 1, 2026 through ..."
_EFFECTIVE_RE = re.compile(
    r'Effective\s+for\s+(?:service|bills?)\s+rendered\s+(?:on\s+and\s+after|from)\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)
# Fallback: "Effective for service on and after January 1, 2026"
_EFFECTIVE_ALT_RE = re.compile(
    r'Effective\s+for\s+(?:service|bills?)\s+on\s+and\s+after\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "I. Basic Customer Charge per month  $14.00"
_CUSTOMER_CHARGE_RE = re.compile(
    r'Basic\s+Customer\s+Charge\s+per\s+month\s+\$\s*([\d,]+\.?\d*)',
    re.I,
)
# Also: "Customer Charge per month  $14.00" (without "Basic")
_CUSTOMER_CHARGE_ALT_RE = re.compile(
    r'Customer\s+Charge\s+per\s+month\s+\$\s*([\d,]+\.?\d*)',
    re.I,
)
_FACILITIES_CHARGE_RE = re.compile(
    r'Basic\s+Facilities\s+Charge\s+per\s+month\s+\$?\s*([\d,]+\.?\d*)',
    re.I,
)
# Inline format: "$3.32 Basic Facilities Charge" (TS schedule, amount before label)
_INLINE_FACILITIES_CHARGE_RE = re.compile(
    r'^\$\s*([\d,]+\.?\d*)\s+Basic\s+Facilities\s+Charge',
    re.I | re.M,
)
# Nantahala format: "Basic Customer Charge - $ 6.40 for single-phase service"
# fitz splits this as "Basic Customer Charge\n-\n$ 6.40" so use re.S and stop before "plus"
_NANTAHALA_CUSTOMER_CHARGE_RE = re.compile(
    r'Basic\s+Customer\s+Charge\s*\n?\s*-\s*\n?\s*\$\s*([\d,]+\.?\d*)(?!\s*(?:per\s+kW|plus))',
    re.I | re.S,
)
# "X.XXXX cents per Kwh for the first N Kwh" or "X.XXXX cents/kWh for the first N kWh"
# (TS schedule and Nantahala-area older format)
_CENTS_PER_KWH_TIERED_RE = re.compile(
    r'([\d]+\.[\d]+)\s+cents\s*(?:per|/)\s*[Kk][Ww][Hh]\s+for\s+(the\s+(?:first|next)\s+[\d,]+\s*[Kk][Ww][Hh]|all\s+over\s+[\d,]+\s*[Kk][Ww][Hh]|all\s+additional\s+[Kk][Ww][Hh]|all\s+[Kk][Ww][Hh])',
    re.I,
)

# Cent symbol variants seen in scanned e-7 sheets:
# ¢ (\u00a2), £ (\u00a3, OCR corruption), \ufffd (replacement char),
# and single letters c/d/e/p that appear as cent artifacts in some OCR runs.
# The pattern requires the letter be followed by whitespace, end-of-line, or
# another digit sequence to avoid matching mid-word 'c'.
_CENT_CHARS = r'[¢\u00a2\u00a3\ufffd]|(?<=[0-9])[cCdDeEpP](?=\s|$)'

# Flat energy rate: "12.2603¢" on its own line or after "Energy Charge"
_FLAT_ENERGY_RE = re.compile(
    r'([\d]+\.[\d]+)\s*(?:[¢\u00a2\u00a3\ufffd]|[cCdDeEpP](?=\s|$))\s*$',
    re.M,
)
_INLINE_FLAT_ENERGY_RE = re.compile(
    r'(?:For\s+)?All\s+kWh(?:\s+used)?\s+per\s+month,\s+per\s+kWh\s+([\d]+\.[\d]+)\s*(?:[¢\u00a2\u00a3\ufffd]|[cCdDeEpP](?=\s|$))',
    re.I,
)
# Rate on next line: "For all kWh used per month, per kWh*\n9.67010" (RS schedule 2013+)
_NEWLINE_FLAT_ENERGY_RE = re.compile(
    r'(?:For\s+)?All\s+kWh(?:\s+used)?\s+per\s+month,\s+per\s+kWh\*?\s+([\d]+\.[\d]+)',
    re.I,
)

# Tiered energy: "For the first 3,000 kWh per month, per kWh  13.9065¢"
# Also handles multi-line format and OCR cent-symbol corruptions (£, c, d, e, p).
_TIERED_ENERGY_RE = re.compile(
    r'For\s+(the\s+first\s+[\d,]+\s*kWh|the\s+next\s+[\d,]+\s*kWh|all\s+over\s+[\d,]+\s*kWh|all\s+kWh|the\s+(?:next|remaining)\s+kWh)'
    r'.*?'
    r'([\d]+\.[\d]+)\s*(?:[¢\u00a2\u00a3\ufffd]|[cCdDeEpP](?=\s|$|\n))',
    re.I | re.S,
)

# TOU energy: "On-Peak Energy per month, per kWh  17.1204¢"
_TOU_ENERGY_RE = re.compile(
    r'(On-Peak|Off-Peak|Discount|Super\s*Off-Peak|Shoulder)\s+Energy.*?'
    r'([\d]+\.[\d]+)\s*(?:[¢\u00a2\u00a3\ufffd]|[cCdDeEpP](?=\s|$|\n))',
    re.I | re.S,
)

_TOU_PERIOD_MAP = {
    "on-peak": "on_peak",
    "off-peak": "off_peak",
    "discount": "discount",
    "super off-peak": "super_off_peak",
    "superoff-peak": "super_off_peak",
    "shoulder": "shoulder",
}

# Demand charge — two forms:
# 1. Single-line: "$5.90 per kW" or "per kW  $5.90"
# 2. Multi-line (Carolinas): "per kW\n$5.90" after a qualifier line
_DEMAND_RE = re.compile(
    r'(?:\$\s*([\d.]+)\s*(?:per\s+kW\b)|per\s+kW\s*\$?\s*([\d.]+))',
    re.I,
)
# Carolinas multi-line: "per kW\n[whitespace]\n$X.XX" or "per kW\n$X.XX"
_DEMAND_MULTILINE_RE = re.compile(
    r'per\s+kW\s*\n\s*\n?\s*\$\s*([\d.]+)',
    re.I,
)

# Rider leaf listing: "Leaf No. 60  Fuel Cost Adjustment Rider"
# Also handles multi-number storm references: "Leaf Nos. 119 and 133"
_RIDER_LEAF_RE = re.compile(
    r'Leaf\s+No\.\s+(\d+)\s+(?!\d)',   # single leaf number followed by non-digit
    re.I,
)
_RIDER_LEAFS_RE = re.compile(
    r'Leaf\s+Nos?\.\s+([\d]+(?:\s+and\s+[\d]+)*)',   # "Leaf Nos. 119 and 133"
    re.I,
)

# Three-phase surcharge: same as Progress
_THREE_PHASE_RE = re.compile(
    r'single.phase\s+service\s+plus\s+\$?([\d.]+)',
    re.I,
)


def _parse_month_date(text: str) -> str | None:
    import datetime
    try:
        dt = datetime.datetime.strptime(text.strip(), "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _extract_effective_start(text: str) -> str | None:
    m = _EFFECTIVE_RE.search(text)
    if m:
        return _parse_month_date(m.group(1))
    m = _EFFECTIVE_ALT_RE.search(text)
    if m:
        return _parse_month_date(m.group(1))
    return None


def _build_revision_label(text: str) -> str | None:
    # Match full label including state prefix (NC or SC)
    m = re.search(
        r'((?:NC|SC)\s+(?:(?:(?:Original|[A-Za-z\-]+)(?:\s+Revised)?\s+)+)?Leaf\s+No\.\s+\d+)',
        text, re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def _build_supersedes_label(text: str) -> str | None:
    m = _SUPERSEDES_RE.search(text)
    return m.group(0).replace("Superseding ", "").strip() if m else None


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------


def parse_nc_carolinas_leaf(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse NC Carolinas leaf PDF text into structured records.

    Returns:
        (version_record, charge_records, rider_applicability_records)
    """
    # Normalize OCR artifacts (cent-sign corruption, ligatures, etc.) before regex matching.
    text = normalize_ocr_text(text)

    # Guard: multi-schedule PDFs contain Schedule OL (Outdoor Lighting Service) after
    # the target schedule rate table. For non-OL families, truncate text at the
    # OL section header to prevent phantom Rider Adjustment rows (-1.0/-8.0 $/kWh).
    _fk_lower = family_key.lower()
    if "outdoor" not in _fk_lower and "-ol" not in _fk_lower and "lighting" not in _fk_lower:
        _ol_m = re.search(r"(?m)^OUTDOOR LIGHTING SERVICE", text, re.I)
        if _ol_m:
            text = text[: _ol_m.start()]

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

    # --- Basic customer / facilities charge ---
    m_cc = _CUSTOMER_CHARGE_RE.search(text)
    if not m_cc:
        m_cc = _CUSTOMER_CHARGE_ALT_RE.search(text)
    label = "Basic Customer Charge"
    if not m_cc:
        m_cc = _FACILITIES_CHARGE_RE.search(text)
        label = "Basic Facilities Charge"
    if not m_cc:
        m_cc = _INLINE_FACILITIES_CHARGE_RE.search(text)
        label = "Basic Facilities Charge"
    if not m_cc:
        m_cc = _NANTAHALA_CUSTOMER_CHARGE_RE.search(text)
        label = "Basic Customer Charge"
    if m_cc:
        snippet = text[max(0, m_cc.start() - 20): m_cc.end() + 20]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="fixed",
                charge_label=label,
                rate_value=float(m_cc.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                customer_class="residential",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.95,
            )
        )

    # --- Three-phase surcharge ---
    m3 = _THREE_PHASE_RE.search(text)
    if m3:
        snippet = text[max(0, m3.start() - 20): m3.end() + 20]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="fixed",
                charge_label="Three-Phase Surcharge",
                rate_value=float(m3.group(1).rstrip(".")),
                rate_unit="$/month",
                season="all_year",
                customer_class="residential",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.90,
            )
        )

    # --- Energy charges ---
    # Try TOU first (On-Peak/Off-Peak energy lines)
    seen_tou: set[str] = set()
    for m in _TOU_ENERGY_RE.finditer(text):
        period_raw = m.group(1).strip().lower().replace(" ", "-")
        tou_period = _TOU_PERIOD_MAP.get(period_raw, period_raw)
        rate = float(m.group(2))
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
                charge_label=f"Energy Charge - {m.group(1).strip().title()}",
                rate_value=round((rate) / 100.0, 6),
                rate_unit="$/kWh",
                tou_period=tou_period,
                season="all_year",
                customer_class="residential",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.90,
            )
        )

    if not seen_tou:
        # Try tiered energy block rates
        tier_matches = list(_TIERED_ENERGY_RE.finditer(text))
        if tier_matches:
            # Parse tier structure
            tiers: list[tuple[float | None, float | None, float]] = []
            cumulative_max = 0.0
            for m in tier_matches:
                qualifier = m.group(1).strip().lower()
                rate = float(m.group(2))
                if "first" in qualifier:
                    n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                    cutoff = float(n_match.group(1).replace(",", "")) if n_match else None
                    tiers.append((0.0, cutoff, rate))
                    cumulative_max = cutoff or 0.0
                elif "next" in qualifier:
                    n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                    block = float(n_match.group(1).replace(",", "")) if n_match else None
                    tier_min = cumulative_max
                    tier_max = (cumulative_max + block) if block else None
                    tiers.append((tier_min, tier_max, rate))
                    if tier_max:
                        cumulative_max = tier_max
                elif "over" in qualifier:
                    n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                    cutoff = float(n_match.group(1).replace(",", "")) if n_match else None
                    tiers.append((cutoff, None, rate))
                elif "all kwh" in qualifier:
                    tiers.append((0.0, None, rate))
                else:
                    tiers.append((cumulative_max or None, None, rate))

            for tier_min, tier_max, rate in tiers:
                if tier_max is None and tier_min == 0.0:
                    label = "Energy Charge"
                elif tier_min == 0.0:
                    label = f"Energy Charge (first {int(tier_max or 0):,} kWh)"
                elif tier_max is None:
                    label = f"Energy Charge (over {int(tier_min):,} kWh)"
                else:
                    label = f"Energy Charge (next {int(tier_max - tier_min):,} kWh)"
                source_snippet = text[
                    max(0, tier_matches[0].start() - 10): tier_matches[-1].end() + 10
                ][:source_snippet_max]
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
                        season="all_year",
                        customer_class="residential",
                        source_snippet=source_snippet,
                        confidence_score=0.90,
                    )
                )
        else:
            # Try "X.XXXX cents per Kwh for the first N Kwh" format (TS and older schedules)
            cents_matches = list(_CENTS_PER_KWH_TIERED_RE.finditer(text))
            if cents_matches:
                cumulative_max = 0.0
                for m in cents_matches:
                    rate = float(m.group(1))
                    qualifier = m.group(2).strip().lower()
                    if "first" in qualifier:
                        n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                        cutoff = float(n_match.group(1).replace(",", "")) if n_match else None
                        tier_min, tier_max = 0.0, cutoff
                        cumulative_max = cutoff or 0.0
                    elif "next" in qualifier:
                        n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                        block = float(n_match.group(1).replace(",", "")) if n_match else None
                        tier_min = cumulative_max
                        tier_max = (cumulative_max + block) if block else None
                        if tier_max:
                            cumulative_max = tier_max
                    elif "over" in qualifier or "additional" in qualifier:
                        n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                        cutoff = float(n_match.group(1).replace(",", "")) if n_match else cumulative_max
                        tier_min, tier_max = cutoff, None
                    else:
                        tier_min, tier_max = 0.0, None
                    if tier_max is None and tier_min == 0.0:
                        lbl = "Energy Charge"
                    elif tier_min == 0.0:
                        lbl = f"Energy Charge (first {int(tier_max or 0):,} kWh)"
                    elif tier_max is None:
                        lbl = f"Energy Charge (over {int(tier_min or 0):,} kWh)"
                    else:
                        lbl = f"Energy Charge (next {int(tier_max - tier_min):,} kWh)"
                    snippet = text[max(0, m.start() - 10): m.end() + 20][:source_snippet_max]
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=lbl,
                            rate_value=round(rate / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=tier_min,
                            tier_max=tier_max,
                            season="all_year",
                            customer_class="residential",
                            source_snippet=snippet,
                            confidence_score=0.88,
                        )
                    )
            else:
                # Flat rate: look for standalone cent value after "Energy Charge"
                energy_idx = text.find("Energy Charge")
                if energy_idx >= 0:
                    search_window = text[energy_idx: energy_idx + 300]
                    m_flat = _FLAT_ENERGY_RE.search(search_window)
                    if not m_flat:
                        m_flat = _INLINE_FLAT_ENERGY_RE.search(search_window)
                    if not m_flat:
                        m_flat = _NEWLINE_FLAT_ENERGY_RE.search(search_window)
                    if m_flat:
                        rate = float(m_flat.group(1))
                        snippet = search_window[:source_snippet_max]
                        charges.append(
                            TariffChargeRecord(
                                version_id=version_id,
                                family_key=family_key,
                                charge_type="energy_block",
                                charge_label="Energy Charge",
                                rate_value=round((rate) / 100.0, 6),
                                rate_unit="$/kWh",
                                tier_min=0.0,
                                tier_max=None,
                                season="all_year",
                                customer_class="residential",
                                source_snippet=snippet,
                                confidence_score=0.88,
                            )
                        )
                else:
                    m_flat = _INLINE_FLAT_ENERGY_RE.search(text)
                    if m_flat:
                        rate = float(m_flat.group(1))
                        snippet = text[max(0, m_flat.start() - 40): m_flat.end() + 40][:source_snippet_max]
                        charges.append(
                            TariffChargeRecord(
                                version_id=version_id,
                                family_key=family_key,
                                charge_type="energy_block",
                                charge_label="Energy Charge",
                                rate_value=round((rate) / 100.0, 6),
                                rate_unit="$/kWh",
                                tier_min=0.0,
                                tier_max=None,
                                season="all_year",
                                customer_class="residential",
                                source_snippet=snippet,
                                confidence_score=0.88,
                            )
                        )

    # --- Demand charges ---
    # Deduplicate: some two-column layouts repeat the value
    seen_demand: set[float] = set()

    def _add_demand(val: float, snippet: str) -> None:
        if val in seen_demand or val == 0.0:
            return
        seen_demand.add(val)
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="demand",
                charge_label="Demand Charge",
                rate_value=val,
                rate_unit="$/kW",
                season="all_year",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.85,
            )
        )

    for m in _DEMAND_RE.finditer(text):
        value = m.group(1) or m.group(2)
        if value:
            _add_demand(float(value), text[max(0, m.start() - 30): m.end() + 20])

    for m in _DEMAND_MULTILINE_RE.finditer(text):
        _add_demand(float(m.group(1)), text[max(0, m.start() - 60): m.end() + 20])

    # --- Rider applicability ---
    # Carolinas lists riders as: "Leaf No. 60  Fuel Cost Adjustment Rider"
    # Storm may appear as: "Leaf Nos. 119 and 133"
    seen_rider_keys: set[str] = set()

    def _add_rider(leaf_no: str) -> None:
        rider_key = _leaf_to_family_key(leaf_no, family_key)
        if rider_key in seen_rider_keys:
            return
        seen_rider_keys.add(rider_key)
        riders.append(
            RiderApplicabilityRecord(
                rider_family_key=rider_key,
                applies_to_family_key=family_key,
                mandatory=True,
                applicability_notes=f"Leaf No. {leaf_no} applicable per rate schedule text",
                source_type="tariff_text",
                confidence_score=0.90,
            )
        )

    # Extract the primary leaf number so we don't link the schedule to itself
    primary_leaf = None
    m_primary = _REVISION_RE.search(text)
    if m_primary:
        primary_leaf = m_primary.group(2)

    for m in _RIDER_LEAF_RE.finditer(text):
        leaf_no = m.group(1)
        if leaf_no == primary_leaf:
            continue  # skip self-reference
        _add_rider(leaf_no)

    for m in _RIDER_LEAFS_RE.finditer(text):
        # "119 and 133"
        all_nums = re.findall(r'\d+', m.group(1))
        for leaf_no in all_nums:
            if leaf_no == primary_leaf:
                continue
            _add_rider(leaf_no)

    # --- Rider rate extraction (for rider PDFs) ---
    is_rider_leaf = bool(re.search(r'RIDER\s+\w+|This Rider applies', text, re.I))
    is_rider_ba = bool(re.search(r'Billing Adjustment Factors', text, re.I))
    if is_rider_leaf and not is_rider_ba:
        _extract_rider_rates(
            text, version_id, family_key, source_snippet_max, charges
        )

    if not charges and "-CEI" in family_key.upper():
        # Fallback for Commercial Equipment Incentive rider
        m = re.search(r"(\d+\.\d+)\s*¢", text)
        if m:
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="adjustment",
                    charge_label="CEI Rider Adjustment",
                    rate_value=round((float(m.group(1))) / 100.0, 6),
                    rate_unit="$/kWh",
                    season="all_year",
                    customer_class="all",
                    source_snippet=text[max(0, m.start() - 20): m.end() + 20],
                    confidence_score=0.50,
                )
            )

    return version, charges, riders


def _leaf_to_family_key(leaf_no: str, base_family_key: str) -> str:
    """Map a leaf number to a carolinas family key.

    Uses the base schedule's family_key prefix to infer the company/state.
    E.g. "nc-carolinas-schedule-RS" → rider leaf 60 → "nc-carolinas-leaf-60"
    """
    # Extract state+company prefix from base family key
    parts = base_family_key.split("-")
    # Expect: state-company-type-code (e.g. "nc-carolinas-schedule-RS")
    if len(parts) >= 2:
        prefix = f"{parts[0]}-{parts[1]}"
    else:
        prefix = "nc-carolinas"
    return f"{prefix}-leaf-{leaf_no}"


def parse_nc_carolinas_leaf_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse NC Carolinas leaf.

    For Rider BA-style multi-column adjustment tables, falls back to pdfplumber
    which preserves row structure better than PyMuPDF.
    """
    text = extract_pdf_text(path)
    version, charges, riders = parse_nc_carolinas_leaf(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )

    # Rider BA fallback for multi-column tables
    if "Billing Adjustment Factors" in text and ("RIDER" in text):
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(path) as pdf:
                # Use strict tolerance to better parse tabular columns
                plumber_text = "\n".join(p.extract_text(x_tolerance=2) or "" for p in pdf.pages)

            _, plumber_charges, _ = parse_nc_carolinas_leaf(
                plumber_text,
                version_id=version_id,
                family_key=family_key,
                document_id=document_id,
            )
            adjustment_charges = [c for c in plumber_charges if c.charge_type == "adjustment"]
            if adjustment_charges:
                charges[:] = [c for c in charges if c.charge_type != "adjustment"]
                charges.extend(adjustment_charges)
        except Exception as e:
            import logging
            log = logging.getLogger("duke_rates.parse.nc_carolinas")
            log.warning("pdfplumber fallback table parsing failed for %s: %s", family_key, e)

    return version, charges, riders
