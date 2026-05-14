"""Parser for Duke Energy Progress (NC) leaf-number tariff PDFs.

Extracts structured TariffVersionRecord and TariffChargeRecord objects
from the text of NC Progress rate schedule and rider PDFs.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from duke_rates.models.tariff import (
    RiderApplicabilityRecord,
    TariffChargeRecord,
    TariffVersionRecord,
)
from duke_rates.parse.pdf_text import extract_pdf_text

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# "NC Second Revised Leaf No. 500" / "SC Fifth Revised Leaf No. 500" / "NC Original Leaf No."
# Accepts both NC and SC state prefixes for Progress SC compatibility
_REVISION_RE = re.compile(
    r'(?:NC|SC)\s+((?:Original|First|Second|Third|Fourth|Fifth|[\w]+)(?:\s+Revised)?\s+)?Leaf\s+No\.\s+(\d+)',
    re.I,
)
_SUPERSEDES_RE = re.compile(
    r'Superseding\s+(?:NC|SC)\s+((?:Original|First|Second|Third|Fourth|Fifth|[\w]+)(?:\s+Revised)?\s+)?Leaf\s+No\.\s+\d+',
    re.I,
)

# "Effective for service rendered on and after October 1, 2025"
_EFFECTIVE_RE = re.compile(
    r'Effective\s+for\s+service\s+rendered\s+on\s+and\s+after\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.I,
)

# "NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023"
_DOCKET_RE = re.compile(r'NCUC\s+Docket\s+No\.\s+([^\n,]+)', re.I)

# Basic Customer Charge: $14.00 per month  (Format A/B — RES schedule)
# Pre-2010 scanned OCR renders "$" as "S" (capital S): "S6.75 per month".
_CUSTOMER_CHARGE_RE = re.compile(
    r'Basic\s+Customer\s+Charge:\s*[\$S]?([\d,]+\.?\d*)\s*per\s+month',
    re.I,
)
# $21.00 Customer Charge  (Format C — SGS/LGS/commercial schedules in compliance bundles)
_DOLLAR_CUSTOMER_CHARGE_RE = re.compile(
    r'[\$]([\d,]+\.?\d*)\s+Customer\s+Charge',
    re.I,
)

# Kilowatt-Hour Charge lines:
#   12.623¢ per kWh for all kWh
#   12.623¢ per kWh for the first 800 kWh
#   11.623¢ per kWh for the additional kWh
_KWH_RATE_RE = re.compile(
    r'([\d]+\.[\d]+)[¢�\u00a2]\s*per\s+kWh\s+for\s+(.+?)(?:\n|$)',
    re.I,
)

# Season column headers from the two-column table layout
# Format A (2023+ standalone): "May – September" / "October – April"
_SUMMER_HEADER_RE = re.compile(r'May\s*[-–]\s*September', re.I)
_WINTER_HEADER_RE = re.compile(r'October\s*[-–]\s*April', re.I)
# Format B (2008–2022 compliance bundles): "Bills Rendered During July - October"
# / "Bills Rendered During November - June"
# "Julv" is an OCR artifact for "July" that fitz produces from pre-2010 scanned filings.
_BUNDLE_SUMMER_HEADER_RE = re.compile(r'Bills\s+Rendered\s+During\s+Jul[yv]\s*[-–]\s*October', re.I)
_BUNDLE_WINTER_HEADER_RE = re.compile(r'Bills\s+Rendered\s+During\s+November\s*[-–]\s*June', re.I)
# Format C (SGS-TOUE/commercial TOU compliance bundles): "June through September" / "October through May"
# fitz splits "service used during the calendar" from "months of June through September" across lines
_COMM_SUMMER_HEADER_RE = re.compile(r'months\s+of\s+june\s+through\s+september', re.I)
_COMM_WINTER_HEADER_RE = re.compile(r'months\s+of\s+october\s+through\s+may', re.I)

# Format B bare kWh rate: "10.211¢ per kWh" with NO "for ..." qualifier
# Used in compliance bundle two-season RES layout — each season column has one bare rate line.
# Pre-2013 filings omit the ¢ symbol entirely: "9.6780 per kWh" — match both forms.
# Additional pre-2013 OCR artifacts handled:
#   "^" caret for ¢: "10.634^ per kWh"
#   "?!", "j", "\xa0" etc. after digits: "9.536?! per kWh", "9.356j per kWh"
#   no space before "per": "10.3560perkWh"
#   leading "l" + "O" misread for "1" + "0": "lO.3560perkWh" → captured as "0.3560" via fallback
# The [^\d\s]{0,3} allows up to 3 junk characters (¢, ^, ?, !, j, etc.) after the number.
_BARE_KWH_RATE_RE = re.compile(
    r'([1-9][\d]*\.[\d]+)[^\d\s]{0,3}\s*per\s*kWh\s*(?:\n|$)',
    re.I,
)
# Supplemental: "lO.NNNN" OCR artifact — "l" and "O" misread for "1" and "0"
_BARE_KWH_RATE_LO_RE = re.compile(
    r'lO\.([\d]+)[^\d\s]{0,3}\s*per\s*kWh\s*(?:\n|$)',
    re.I,
)

# Rider leaf references: "Leaf No. 601 Rider BA"
_RIDER_LEAF_RE = re.compile(r'Leaf\s+No\.\s+(\d+)\s+Rider\s+(\w+)', re.I)

# Storm securitization rider reference — matches "Storm Securitization Rider (Leaf No. NNN"
# or "Storm Securitization charge (Leaf No. NNN" etc.
_STORM_LEAF_RE = re.compile(
    r'Storm\s+Securitization\s+\w+\s+\(Leaf\s+No\.\s+(\d+)', re.I
)

# Rider BA net adjustment table: "Residential .262 0.518 0.663 0.106 1.549"
# The last numeric column is the net ¢/kWh adjustment; preceding columns may have "(EE Only)" text
_RIDER_BA_CLASS_RE = re.compile(
    r'^(Residential|Small General Service|Medium General Service|Large General Service|Lighting)'
    r'.+?\s([-]?\d+\.\d+)\s*$',
    re.M,
)

# Single scalar rider rate line: "X.XXX¢/kWh" or "X.XXX $/kWh" or "$X.XX per kWh"
_SCALAR_RIDER_RATE_RE = re.compile(
    r'([-\d.]+)[¢\u00a2\ufffd]\s*/?\s*kWh',
    re.I,
)

# Three-phase surcharge: "The bill computed for single-phase service plus $9.00"
_THREE_PHASE_RE = re.compile(
    r'single.phase\s+service\s+plus\s+\$?([\d.]+)',
    re.I,
)

# TOU energy charges:
# "29.905¢ per On-Peak kWh" / "11.321¢ per Off-Peak kWh" / "7.372¢ per Discount kWh"
# Older DEP residential TOU sheets can omit the cent glyph entirely:
# "6.9480 per On-Peak kWh" / "5.5411 per Off-Peak kWh"
# Pre-2013 OCR renders ¢ as "^": "6.760^ per on-peak kWh"
_TOU_RATE_RE = re.compile(
    r'([\d]+\.[\d]+)(?:[¢\u00a2\ufffd\^])?\s*per\s+(On-Peak|Off-Peak|Discount|Super\s*Off-Peak|Shoulder)\s*kWh',
    re.I,
)

_TOU_PERIOD_MAP = {
    "on-peak": "on_peak",
    "off-peak": "off_peak",
    "discount": "discount",  # NC Progress TOU discount charging period (EV overnight)
    "super off-peak": "super_off_peak",
    "superoff-peak": "super_off_peak",
    "shoulder": "shoulder",
}

# Basic customer charge as standalone dollar amount on its own line: "$14.00"
# Pre-2013 OCR renders "$" as "S": "S9.85" on its own line.
_STANDALONE_CHARGE_RE = re.compile(r'^[\$S]\s*([\d,]+\.?\d*)\s*$', re.M)

# Demand charge: "$ X.XX per kW", "$1.95 per On-Peak kW", "$3.82 per Max kW"
# Optional qualifier between "per" and "kW" captures labeled demand components.
# Pre-2013 OCR renders "$" as "S" (capital S): "S5.02 per kW for all on-peak".
# The (?<!\w) lookbehind prevents "S" from matching inside words.
_DEMAND_RATE_RE = re.compile(
    r'(?<!\w)[\$S]\s*([\d.]+)\s*per\s+(On-Peak\s+|Off-Peak\s+|Max\s+|Maximum\s+|Billing\s+)?kW\b',
    re.I,
)

# Maps qualifier text → charge label suffix used in _extract_demand_label()
_DEMAND_QUALIFIER_LABEL = {
    "on-peak": "Demand Charge - On-Peak",
    "off-peak": "Demand Charge - Off-Peak",
    "max": "Demand Charge - Maximum",
    "maximum": "Demand Charge - Maximum",
    "billing": "Demand Charge",
}

# Minimum bill: "$X.XX per month" (if found outside customer charge context)
_MINIMUM_RE = re.compile(
    r'Minimum\s+(?:Monthly\s+)?(?:Bill|Charge)[:\s]+\$?([\d.]+)\s*(?:per\s+month)?',
    re.I,
)

# Rider rate table row: "Residential  RES, R-TOUD, R-TOU,  0.00464"
# Matches class name followed by schedules then a terminal numeric value
# Handles both $/kWh values (like 0.00464) and ¢/kWh values (like 0.216 or (0.249))
_RIDER_TABLE_ROW_RE = re.compile(
    r'^(Residential|Small General Service|Medium General(?:\s+Service)?|Large General Service'
    r'|General Service\s*(?:\(\w+\))?|Industrial(?:\s+Service)?'
    r'|Lighting|Outdoor Lighting(?:\s+Service)?'
    r'|Seasonal(?:\s+and\s+Intermittent(?:\s+Service)?)?'
    r'|Traffic Signal(?:\s+Service)?'
    r'|Church|Agricultural)'
    r'[^$\n]{0,120}'
    r'(\([\d.]+\)|[\d]+\.[\d]+)\s*$',
    re.M | re.I,
)

_RIDER_INLINE_DOLLAR_ROW_RE = re.compile(
    r'^(Residential|Small General Service|Medium General(?:\s+Service)?|Large General Service'
    r'|Lighting|Outdoor Lighting(?:\s+Service)?)\s+\$([\d.]+)\s*$',
    re.M | re.I,
)

# Rider sentence rate: "is X.XXX¢ per kilowatt-hour" (single-value riders like RDM, PIM, ESM)
# Also handles parenthetical negatives: "is (0.012)¢ per kilowatt-hour" (ESM decremental rate)
_RIDER_SENTENCE_RATE_RE = re.compile(
    r'is\s+(\(?[-\d.]+\)?)(?:[¢\u00a2\ufffd]|[\s-]*cents?)\s*per\s+kilowatt.hour',
    re.I,
)

# Rider prose narrative rate: "increase/decrease of X.XXX cents per kWh"
# Found in customer notices and multi-rider adjustment orders (JAA, REPS, DSM notices).
# Used as a last-resort Pattern 5 fallback when no table-based pattern matches.
_RIDER_PROSE_RATE_RE = re.compile(
    r'(?:increase|decrease)\s+of\s+([\d.]+)\s+cents\s+per\s+k[Ww][Hh]',
    re.I,
)

# Percentage-of-bill credit rider: "RECD Credit = 5% times the stated ... charges"
# Used for the Residential Energy Conservation Discount (RECD, Leaf 640) and similar
# percent-of-bill riders that cannot be expressed as a ¢/kWh value.
_RIDER_PCT_CREDIT_RE = re.compile(
    r'([\d.]+)%\s+times\s+the\s+stated\s+(?:kilowatt|kw)',
    re.I,
)

# Rider labeled rate: "Net CPRE Rider Factor  0.001 ¢/kWh" per section header
_RIDER_LABELED_RATE_RE = re.compile(
    r'Net\s+\w+\s+Rider\s+Factor\s+([-]?[\d.]+)\s*[¢\u00a2\ufffd]/kWh',
    re.I,
)

# Dollar-per-kWh rider table: "Residential $0.00098"
_RIDER_DOLLAR_KWH_RE = re.compile(
    r'^(Residential|Small General Service|Medium General|Large General'
    r'|Lighting|Seasonal)\s+\$([\d.]+)',
    re.M | re.I,
)

# Section header identifying customer class for CPRE-style riders
_RIDER_SECTION_HEADER_RE = re.compile(
    r'^(RESIDENTIAL SERVICE|SMALL GENERAL SERVICE|MEDIUM GENERAL SERVICE'
    r'|LARGE GENERAL SERVICE|LIGHTING SERVICE|LIGHTING)\s*$',
    re.M | re.I,
)

_RIDER_SECTION_CLASS_MAP = {
    "residential service": "residential",
    "small general service": "commercial_small",
    "medium general service": "commercial_medium",
    "large general service": "commercial_large",
    "lighting service": "lighting",
    "lighting": "lighting",
}


def _parse_month_date(text: str) -> str | None:
    """Convert 'October 1, 2025' → 'YYYY-MM-DD' ISO string."""
    import datetime

    try:
        dt = datetime.datetime.strptime(text.strip(), "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _build_revision_label(text: str) -> str | None:
    """Extract the first revision label from PDF text."""
    # Match both "NC X Revised Leaf No. NNN" and "SC X Revised Leaf No. NNN"
    m = re.search(
        r'((?:NC|SC)\s+(?:(?:Original|First|Second|Third|Fourth|Fifth|[\w]+)(?:\s+Revised)?\s+)?Leaf\s+No\.\s+\d+)',
        text, re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def _build_supersedes_label(text: str) -> str | None:
    m = _SUPERSEDES_RE.search(text)
    if not m:
        return None
    return m.group(0).replace("Superseding ", "").strip()


def _extract_effective_start(text: str) -> str | None:
    m = _EFFECTIVE_RE.search(text)
    return _parse_month_date(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Season detection for two-column rate tables
# ---------------------------------------------------------------------------

def _has_two_season_columns(text: str) -> bool:
    """True for Format A (standalone) or Format B (compliance bundle) two-season tables."""
    standalone = _SUMMER_HEADER_RE.search(text) and _WINTER_HEADER_RE.search(text)
    bundle = _BUNDLE_SUMMER_HEADER_RE.search(text) and _BUNDLE_WINTER_HEADER_RE.search(text)
    commercial = _COMM_SUMMER_HEADER_RE.search(text) and _COMM_WINTER_HEADER_RE.search(text)
    return bool(standalone or bundle or commercial)


def _is_bundle_format(text: str) -> bool:
    """True when the PDF uses the compliance-bundle two-season header style."""
    return bool(
        _BUNDLE_SUMMER_HEADER_RE.search(text) and _BUNDLE_WINTER_HEADER_RE.search(text)
    )


def _is_commercial_tou_format(text: str) -> bool:
    """True for SGS-TOUE/commercial TOU compliance bundles with June-Sep/Oct-May column headers."""
    return bool(
        _COMM_SUMMER_HEADER_RE.search(text) and _COMM_WINTER_HEADER_RE.search(text)
    )


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_nc_progress_leaf(
    text: str,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
    source_snippet_max: int = 200,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Parse NC Progress leaf PDF text into structured records.

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

    # --- Basic customer charge ---
    for m in _CUSTOMER_CHARGE_RE.finditer(text):
        snippet = text[max(0, m.start() - 20): m.end() + 20]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="fixed",
                charge_label="Basic Customer Charge",
                rate_value=float(m.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                customer_class="residential",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.95,
            )
        )
        break  # Usually appears once; avoid duplicates from two-column layout

    # --- Fallback: standalone "$14.00" customer charge (TOU schedules omit "per month") ---
    if not charges:
        # Check for "$14.00" or "S14.00" near "Basic Customer Charge:" label (S = OCR for $)
        for m in re.finditer(r'Basic\s+Customer\s+Charge:\s*\n?\s*[\$S]\s*([\d,]+\.?\d*)', text, re.I):
            snippet = text[max(0, m.start() - 10): m.end() + 20]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label="Basic Customer Charge",
                    rate_value=float(m.group(1).replace(",", "")),
                    rate_unit="$/month",
                    season="all_year",
                    customer_class="residential",
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.90,
                )
            )
            break

    # --- Fallback: "$21.00 Customer Charge" (SGS/LGS/commercial bundle format) ---
    if not any(c.charge_label == "Basic Customer Charge" for c in charges):
        m_cc = _DOLLAR_CUSTOMER_CHARGE_RE.search(text)
        if m_cc:
            snippet = text[max(0, m_cc.start() - 10): m_cc.end() + 20]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="fixed",
                    charge_label="Basic Customer Charge",
                    rate_value=float(m_cc.group(1).replace(",", "")),
                    rate_unit="$/month",
                    season="all_year",
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.88,
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

    # --- kWh energy charges ---
    # Three layout formats are in use:
    #
    # Format A (2023+ standalone leaf PDFs) — two-column, season headers "May–September" /
    # "October–April", rate lines include "for all kWh", "for the first N kWh",
    # "for the additional kWh".
    #
    # Format B (2015–2022 compliance bundles, RES schedule) — two-column, season headers
    # "Bills Rendered During July - October" / "Bills Rendered During November - June",
    # rate lines are bare "X.XXX¢ per kWh" with no "for ..." qualifier.  Two bare rates
    # appear per block: the first is summer (column 1), the second is winter (column 2).
    #
    # Format C (2015–2022 compliance bundles, SGS/LGS schedules) — single-column, block
    # tiers use "for the first N kWh", "for the next N kWh", "for all additional kWh".
    # No season split — "all_year" tiered energy.
    #
    # Strategy: detect format from headers, then apply the appropriate parsing path.

    _LINE_RATE_RE = re.compile(
        r'([\d]+\.[\d]+)[¢\u00a2\ufffd]\s*per\s+kWh\s+for\s+'
        r'(all\s+kWh|the\s+first\s+[\d,]+\s*kWh|the\s+next\s+[\d,]+\s*kWh|the\s+additional\s+kWh|all\s+additional\s+kWh)',
        re.I,
    )

    two_seasons = _has_two_season_columns(text)
    bundle_format = _is_bundle_format(text)
    winter_tier_cutoff: float | None = None
    prev_tier_cutoff: float | None = None  # running upper bound for "next N kWh" tiers

    if bundle_format and two_seasons:
        # --- Format B: bare rates in two-column compliance bundle (RES schedule) ---
        # Collect all bare kWh rate values in document order.
        # Merge normal matches with "lO.NNNN" OCR artifact matches (keyed by position).
        _rate_positions: dict[int, float] = {}
        for m in _BARE_KWH_RATE_RE.finditer(text):
            _rate_positions[m.start()] = float(m.group(1))
        for m in _BARE_KWH_RATE_LO_RE.finditer(text):
            _rate_positions[m.start()] = float(f"10.{m.group(1)}")
        bare_rates = [v for _, v in sorted(_rate_positions.items())]
        # The two-column layout produces exactly 2 bare rates: [summer_rate, winter_rate].
        if len(bare_rates) >= 2:
            summer_rate, winter_rate = bare_rates[0], bare_rates[1]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="energy_block",
                    charge_label="Energy Charge - Summer",
                    rate_value=round(summer_rate / 100.0, 6),
                    rate_unit="$/kWh",
                    tier_min=0.0,
                    tier_max=None,
                    season="summer",
                    customer_class="residential",
                    source_snippet="(bundle format B, summer rate)",
                    confidence_score=0.88,
                )
            )
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="energy_block",
                    charge_label="Energy Charge - Winter",
                    rate_value=round(winter_rate / 100.0, 6),
                    rate_unit="$/kWh",
                    tier_min=0.0,
                    tier_max=None,
                    season="winter",
                    customer_class="residential",
                    source_snippet="(bundle format B, winter rate)",
                    confidence_score=0.88,
                )
            )
    else:
        # --- Format A and Format C: "for ..." qualified rate lines ---
        for line in text.splitlines():
            line_matches = list(_LINE_RATE_RE.finditer(line))
            if not line_matches:
                continue

            for m in line_matches:
                rate = float(m.group(1))
                qualifier = m.group(2).strip().lower()

                if "all kwh" in qualifier and "additional" not in qualifier:
                    # Format A summer "for all kWh" (flat unlimited summer rate)
                    season = "summer" if two_seasons else "all_year"
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=f"Energy Charge{' - Summer' if two_seasons else ''}",
                            rate_value=round(rate / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=0.0,
                            tier_max=None,
                            season=season,
                            customer_class="residential",
                            source_snippet=line[:source_snippet_max],
                            confidence_score=0.90,
                        )
                    )

                elif "the first" in qualifier:
                    n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                    cutoff = float(n_match.group(1).replace(",", "")) if n_match else None
                    winter_tier_cutoff = cutoff
                    prev_tier_cutoff = cutoff
                    season = "winter" if two_seasons else "all_year"
                    label = "Energy Charge - Winter (first block)" if two_seasons else "Energy Charge (first block)"
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=label,
                            rate_value=round(rate / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=0.0,
                            tier_max=cutoff,
                            season=season,
                            customer_class="residential",
                            source_snippet=line[:source_snippet_max],
                            confidence_score=0.90,
                        )
                    )

                elif "the next" in qualifier:
                    # Format C: intermediate tier "for the next N kWh"
                    n_match = re.search(r'([\d,]+)\s*kwh', qualifier)
                    block_size = float(n_match.group(1).replace(",", "")) if n_match else None
                    tier_min = prev_tier_cutoff
                    tier_max = (prev_tier_cutoff + block_size) if (prev_tier_cutoff is not None and block_size) else None
                    prev_tier_cutoff = tier_max
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label="Energy Charge (next block)",
                            rate_value=round(rate / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=tier_min,
                            tier_max=tier_max,
                            season="all_year",
                            customer_class="residential",
                            source_snippet=line[:source_snippet_max],
                            confidence_score=0.88,
                        )
                    )

                elif "additional" in qualifier or "excess" in qualifier:
                    season = "winter" if two_seasons else "all_year"
                    label = "Energy Charge - Winter (additional)" if two_seasons else "Energy Charge (additional)"
                    charges.append(
                        TariffChargeRecord(
                            version_id=version_id,
                            family_key=family_key,
                            charge_type="energy_block",
                            charge_label=label,
                            rate_value=round(rate / 100.0, 6),
                            rate_unit="$/kWh",
                            tier_min=winter_tier_cutoff if two_seasons else prev_tier_cutoff,
                            tier_max=None,
                            season=season,
                            customer_class="residential",
                            source_snippet=line[:source_snippet_max],
                            confidence_score=0.90,
                        )
                    )

    # --- TOU energy charges ---
    # Pattern: "29.905¢ per On-Peak kWh" / "11.321¢ per Off-Peak kWh" / "7.372¢ per Discount kWh"
    # Two-column layouts (fitz output): summer and winter rates for each period appear on consecutive
    # single-match lines. Detect when the same TOU period appears twice in sequence and assign
    # summer to the first occurrence, winter to the second.
    comm_two_col = _is_commercial_tou_format(text)
    seen_tou: set[str] = set()
    if comm_two_col:
        # Collect all TOU rate matches in order, then pair consecutive same-period matches as summer/winter
        all_tou: list[tuple[str, float, str]] = []  # (period, rate, line)
        for line in text.splitlines():
            for m in _TOU_RATE_RE.finditer(line):
                period_raw = m.group(2).strip().lower().replace(" ", "-")
                tou_period = _TOU_PERIOD_MAP.get(period_raw, period_raw)
                all_tou.append((tou_period, float(m.group(1)), line))
        # Pair consecutive same-period items: first = summer, second = winter
        seen_periods: dict[str, int] = {}  # period -> occurrence count so far
        for tou_period, rate, line in all_tou:
            count = seen_periods.get(tou_period, 0)
            season = "summer" if count == 0 else ("winter" if count == 1 else "all_year")
            seen_periods[tou_period] = count + 1
            dedup_key = f"{tou_period}:{rate}:{season}"
            if dedup_key in seen_tou:
                continue
            seen_tou.add(dedup_key)
            period_label = tou_period.replace("_", "-").title()
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="tou_energy",
                    charge_label=f"Energy Charge - {period_label}",
                    rate_value=round(rate / 100.0, 6),
                    rate_unit="$/kWh",
                    tou_period=tou_period,
                    season=season,
                    customer_class="residential",
                    source_snippet=line[:source_snippet_max],
                    confidence_score=0.90,
                )
            )
    else:
        for line in text.splitlines():
            for m in _TOU_RATE_RE.finditer(line):
                rate = float(m.group(1))
                period_raw = m.group(2).strip().lower().replace(" ", "-")
                tou_period = _TOU_PERIOD_MAP.get(period_raw, period_raw)
                dedup_key = f"{tou_period}:{rate}:all_year"
                if dedup_key in seen_tou:
                    continue
                seen_tou.add(dedup_key)
                charges.append(
                    TariffChargeRecord(
                        version_id=version_id,
                        family_key=family_key,
                        charge_type="tou_energy",
                        charge_label=f"Energy Charge - {m.group(2).title()}",
                        rate_value=round(rate / 100.0, 6),
                        rate_unit="$/kWh",
                        tou_period=tou_period,
                        season="all_year",
                        customer_class="residential",
                        source_snippet=line[:source_snippet_max],
                        confidence_score=0.90,
                    )
                )

    # --- Demand charges ---
    # Deduplicate per line: two-column tables repeat the same value twice on one line.
    # Dedup key includes label so "on-peak" and "max" demand at different values are separate rows.
    seen_demand: set[tuple] = set()
    for line in text.splitlines():
        line_entries: list[tuple[float, str]] = []
        for m in _DEMAND_RATE_RE.finditer(line):
            val = float(m.group(1))
            qualifier = (m.group(2) or "").strip().lower()
            label = _DEMAND_QUALIFIER_LABEL.get(qualifier, "Demand Charge")
            entry = (val, label)
            if entry not in line_entries:
                line_entries.append(entry)
        for val, label in line_entries:
            dedup_key = (round(val, 4), label)
            if dedup_key in seen_demand:
                continue
            seen_demand.add(dedup_key)
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="demand",
                    charge_label=label,
                    rate_value=val,
                    rate_unit="$/kW",
                    season="all_year",
                    source_snippet=line[:source_snippet_max],
                    confidence_score=0.85,
                )
            )

    # --- Minimum bill ---
    for m in _MINIMUM_RE.finditer(text):
        snippet = text[max(0, m.start() - 20): m.end() + 40]
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="minimum",
                charge_label="Minimum Monthly Bill",
                rate_value=float(m.group(1)),
                rate_unit="$/month",
                season="all_year",
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.85,
            )
        )

    # --- Rider applicability ---
    # Derive state+company prefix from family_key (e.g. "sc-progress-leaf-500" → "sc-progress")
    # so that rider links use the correct state prefix for SC Progress families
    _fk_parts = family_key.split("-")
    _leaf_prefix = f"{_fk_parts[0]}-{_fk_parts[1]}" if len(_fk_parts) >= 2 else "nc-progress"

    seen_rider_keys: set[str] = set()
    for m in _RIDER_LEAF_RE.finditer(text):
        leaf_no = m.group(1)
        rider_code = m.group(2).upper()
        rider_family_key = f"{_leaf_prefix}-leaf-{leaf_no}"
        if rider_family_key in seen_rider_keys:
            continue
        seen_rider_keys.add(rider_family_key)
        riders.append(
            RiderApplicabilityRecord(
                rider_family_key=rider_family_key,
                applies_to_family_key=family_key,
                mandatory=True,
                applicability_notes=(
                    f"Rider {rider_code} (Leaf No. {leaf_no}) "
                    "applicable per rate schedule text"
                ),
                source_type="tariff_text",
                confidence_score=0.90,
            )
        )

    # Storm securitization rider (separate section)
    m_storm = _STORM_LEAF_RE.search(text)
    if m_storm:
        storm_leaf = m_storm.group(1)
        storm_key = f"{_leaf_prefix}-leaf-{storm_leaf}"
        # Avoid duplicate if already captured by RIDER_LEAF_RE
        if storm_key not in seen_rider_keys:
            riders.append(
                RiderApplicabilityRecord(
                    rider_family_key=storm_key,
                    applies_to_family_key=family_key,
                    mandatory=True,
                    applicability_notes=(
                        f"Storm Securitization Rider (Leaf No. {storm_leaf}) "
                        "per rate schedule text"
                    ),
                    source_type="tariff_text",
                    confidence_score=0.90,
                )
            )

    # --- Rider BA net adjustment table (Leaf No. 601) ---
    # Multi-column table: pdfplumber preserves row structure; PyMuPDF breaks columns.
    # Strategy: try the full-row regex first; if zero matches, fall back to pdfplumber.
    _RIDER_CLASS_MAP = {
        "residential": "residential",
        "small general service": "commercial_small",
        "medium general service": "commercial_medium",
        "large general service": "commercial_large",
        "lighting": "lighting",
    }
    is_ba_table = bool(re.search(r'Billing Adjustment Factors|Rider\s+BA', text, re.I))
    if is_ba_table and _RIDER_BA_CLASS_RE.search(text):
        # pdfplumber text: rows are intact
        for m in _RIDER_BA_CLASS_RE.finditer(text):
            class_name = m.group(1).strip().lower()
            net_value_str = m.group(2).strip()
            customer_class = _RIDER_CLASS_MAP.get(class_name, class_name)
            try:
                net_value = float(net_value_str)
            except ValueError:
                continue
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="adjustment",
                    charge_label=f"Billing Adjustment - {m.group(1).strip()}",
                    rate_value=round((net_value) / 100.0, 6),
                    rate_unit="$/kWh",
                    season="all_year",
                    customer_class=customer_class,
                    source_snippet=m.group(0)[:source_snippet_max],
                    confidence_score=0.80,
                )
            )
    # (PyMuPDF interleaves columns; Rider BA table will be parsed via pdfplumber
    # when parse_nc_progress_leaf_file is called — see that function below)

    # --- Other rider rate extraction ---
    # Only applies when parsing a rider leaf (family_key contains "leaf-60" or similar)
    # Skip for Rider BA: that table requires pdfplumber (handled in parse_nc_progress_leaf_file)
    is_rider_leaf = bool(re.search(r'RIDER\s+\w+|This Rider applies', text, re.I))
    is_rider_ba = bool(re.search(r'Billing Adjustment Factors', text, re.I))
    if is_rider_leaf and not is_rider_ba:
        _extract_rider_rates(
            text, version_id, family_key, source_snippet_max, charges
        )

    return version, charges, riders


# ---------------------------------------------------------------------------
# Rider rate extraction helpers
# ---------------------------------------------------------------------------

_RIDER_CLASS_NAME_MAP = {
    "residential": "residential",
    "small general service": "commercial_small",
    "medium general service": "commercial_medium",
    "medium general": "commercial_medium",
    "large general service": "commercial_large",
    "large general": "commercial_large",
    "general service (small)": "commercial_small",
    "general service (medium)": "commercial_medium",
    "general service (large)": "commercial_large",
    "general service (constant load)": "commercial_small",
    "general service": "commercial",
    "lighting": "lighting",
    "outdoor lighting service": "lighting",
    "outdoor lighting": "lighting",
    "seasonal": "seasonal",
    "seasonal and intermittent service": "commercial",
    "seasonal and intermittent": "commercial",
    "traffic signal service": "commercial",
    "traffic signal": "commercial",
    "sports field lighting": "lighting",
    "industrial service": "industrial",
    "industrial": "industrial",
    "church": "commercial",
    "agricultural": "commercial",
}

# Regex to detect a class name line in a multi-line rider table
_RIDER_CLASS_LINE_RE = re.compile(
    r'^(Residential|Small General Service|Medium General(?:\s+Service)?'
    r'|Large General(?:\s+Service)?'
    r'|General Service\s*\([^)]+\)'
    r'|General Service'
    r'|Industrial(?:\s+Service)?'
    r'|Lighting|Outdoor Lighting(?:\s+Service)?|Sports Field Lighting'
    r'|Seasonal(?:\s+and\s+Intermittent(?:\s+Service)?)?'
    r'|Traffic Signal(?:\s+Service)?'
    r'|Church|Agricultural)\s*$',
    re.M | re.I,
)

# Section-break sentinel: all-caps schedule section headers that indicate we've left
# the target schedule's rate table and entered an unrelated schedule section.
# When the multi-line scanner encounters one of these, it should stop producing rows.
_UNRELATED_SECTION_RE = re.compile(
    r'^OUTDOOR LIGHTING SERVICE|^TRAFFIC SIGNAL SERVICE|^STREET LIGHTING SERVICE',
    re.I,
)

# A standalone numeric value on its own line (with optional parentheses for negatives)
_STANDALONE_VALUE_RE = re.compile(
    r'^\s*(\([\d.]+\)|[-]?[\d]+\.[\d]+)\s*$',
    re.M,
)

# Dollar-amount on its own line: "$0.00098"
_STANDALONE_DOLLAR_RE = re.compile(
    r'^\s*\$([\d.]+)\s*$',
    re.M,
)


def _extract_rider_rates(
    text: str,
    version_id: int,
    family_key: str,
    source_snippet_max: int,
    charges: list[TariffChargeRecord],
) -> None:
    """Extract rider adjustment rates from various rider leaf formats.

    Handles four patterns:
    1. Sentence rate: "is X.XXX¢ per kilowatt-hour" (applies to all or current section)
    2. Table row ($/kWh): "Residential  RES,...  0.216" or "(0.249)"
    3. Labeled factor: "Net XXX Rider Factor  0.001 ¢/kWh"
    4. Dollar table: "Residential $0.00098"
    """

    def _class_from_name(name: str) -> str:
        # Normalize internal whitespace (handles multi-line matches like "Medium General\nService")
        key = re.sub(r'\s+', ' ', name.strip()).lower()
        return _RIDER_CLASS_NAME_MAP.get(key, "all")

    def _parse_paren_value(s: str) -> float:
        """Convert '(0.249)' → -0.249, '0.216' → 0.216."""
        s = s.strip()
        if s.startswith("(") and s.endswith(")"):
            return -float(s[1:-1])
        return float(s)

    # Pre-seed dedup set with any adjustment charges already added (e.g., from Rider BA table)
    already_added_classes: set[str] = {
        c.customer_class
        for c in charges
        if c.charge_type == "adjustment" and c.family_key == family_key
    }
    added: set[tuple[str, float]] = set()

    def _add(customer_class: str, rate_value: float, rate_unit: str, snippet: str) -> None:
        # Skip classes already populated by Rider BA table extraction
        if customer_class in already_added_classes:
            return
        key = (customer_class, round(rate_value, 6))
        if key in added:
            return
        added.add(key)
        charges.append(
            TariffChargeRecord(
                version_id=version_id,
                family_key=family_key,
                charge_type="adjustment",
                charge_label="Rider Adjustment",
                rate_value=rate_value,
                rate_unit=rate_unit,
                season="all_year",
                customer_class=customer_class,
                source_snippet=snippet[:source_snippet_max],
                confidence_score=0.80,
            )
        )

    # --- Pattern 1: "is X.XXX¢ per kilowatt-hour" (sentence-style, all-class riders) ---
    m_sent = _RIDER_SENTENCE_RATE_RE.search(text)
    if m_sent:
        rate_val = _parse_paren_value(m_sent.group(1))  # handles (0.012) → -0.012
        snippet = text[max(0, m_sent.start() - 40): m_sent.end() + 40]
        _add("all", round((rate_val) / 100.0, 6), "$/kWh", snippet)
        return  # Sentence-style riders don't have per-class breakdown

    # --- Pattern 3: Labeled factor per section header (CPRE-style) ---
    # "Net CPRE Rider Factor  0.001 ¢/kWh" appearing after a section header
    section_headers = list(_RIDER_SECTION_HEADER_RE.finditer(text))
    labeled_matches = list(_RIDER_LABELED_RATE_RE.finditer(text))
    if section_headers and labeled_matches:
        for lm in labeled_matches:
            # Find the section header that precedes this labeled rate
            preceding = [sh for sh in section_headers if sh.start() < lm.start()]
            if preceding:
                header_text = preceding[-1].group(1).strip().lower()
                customer_class = _RIDER_SECTION_CLASS_MAP.get(header_text, "all")
            else:
                customer_class = "all"
            rate_val = float(lm.group(1))
            snippet = text[max(0, lm.start() - 60): lm.end() + 40]
            _add(customer_class, round((rate_val) / 100.0, 6), "$/kWh", snippet)
        if added:
            return

    # Also handle single labeled factor without section headers
    if labeled_matches and not section_headers:
        for lm in labeled_matches:
            rate_val = float(lm.group(1))
            snippet = text[max(0, lm.start() - 40): lm.end() + 40]
            _add("all", round((rate_val) / 100.0, 6), "$/kWh", snippet)
        if added:
            return

    # --- Inline dollar table rows (e.g. "Small General Service $1.12") ---
    inline_dollar_rows = list(_RIDER_INLINE_DOLLAR_ROW_RE.finditer(text))
    if inline_dollar_rows:
        header_dollar_per_bill = bool(re.search(r'\$/bill', text, re.I))
        for row in inline_dollar_rows:
            customer_class = _class_from_name(row.group(1))
            rate_val = float(row.group(2))
            if header_dollar_per_bill and customer_class != "residential":
                rate_unit = "$/bill"
            else:
                rate_unit = "$/kWh"
            snippet = text[max(0, row.start() - 20): row.end() + 40]
            _add(customer_class, rate_val, rate_unit, snippet)
        if added:
            return

    # --- Multi-line table parser ---
    # Handles PyMuPDF output where class/schedules/rate are on separate lines.
    # Strategy: find class name lines, then scan forward up to 8 lines
    # for a standalone numeric value.
    # Determine rate_unit from the table header: "$/kWh" or "¢/kWh" or "(¢/kWh)" for decrements.
    header_dollar = bool(re.search(r'\$/kWh|dollars\s+per\s+kilowatt.hour', text, re.I))
    # Cents header: "(¢/kWh)" or "¢/kWh" or "cents per kilowatt" — values need /100 conversion
    header_cents = bool(re.search(r'[¢c]/kWh|cents?\s+per\s+kilowatt', text, re.I))
    # If header says "$/kWh for Residential; $/bill for all General Service", detect mixed
    header_dollar_per_bill = bool(re.search(r'\$/bill', text, re.I))

    # Find position of per-kW demand-rate section header so we can skip those rows
    # Match "Demand Rate Classes (dollars per kilowatt)" but NOT "Non-Demand Rate Class"
    # Also handles OCR garbling like "Deilland Rate Classes" from scanned PDFs.
    demand_section_start = -1
    m_demand_section = re.search(
        r'(?<!Non-)(?:Demand|D\w{2,6}and)\s+Rate\s+Class(?:es)?\s*\(dollars\s+per\s+kilowatt\b',
        text, re.I
    )
    if m_demand_section:
        demand_section_start = m_demand_section.start()

    class_line_matches = list(_RIDER_CLASS_LINE_RE.finditer(text))
    if class_line_matches:
        lines = text.splitlines()
        # Build a map from char offset to line index for fast lookup
        line_start_offsets: list[int] = []
        offset = 0
        for line in lines:
            line_start_offsets.append(offset)
            offset += len(line) + 1  # +1 for \n

        def _char_to_line(char_offset: int) -> int:
            # binary search
            lo, hi = 0, len(line_start_offsets) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_start_offsets[mid] <= char_offset:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        for cm in class_line_matches:
            class_name = cm.group(1).strip()
            customer_class = _class_from_name(class_name)
            in_demand_section = demand_section_start >= 0 and cm.start() >= demand_section_start
            start_line = _char_to_line(cm.start())

            # Guard: if this class name line appears immediately after an unrelated section
            # header (e.g. "OUTDOOR LIGHTING SERVICE"), skip the entire section.
            # This prevents multi-schedule PDFs from bleeding OL/TS rows into adjacent families.
            preceding_context = "\n".join(lines[max(0, start_line - 4): start_line])
            if _UNRELATED_SECTION_RE.search(preceding_context):
                continue

            # Scan up to 8 following lines for a standalone numeric value
            for li in range(start_line + 1, min(start_line + 9, len(lines))):
                candidate = lines[li].strip()
                if not candidate:
                    continue
                # Stop if we've hit an unrelated section header
                if _UNRELATED_SECTION_RE.match(candidate):
                    break
                # Check for standalone dollar amount first
                dm = re.match(r'^\$\s*([\d.]+)\s*$', candidate)
                if dm:
                    rate_val = float(dm.group(1))
                    if in_demand_section:
                        rate_unit = "$/kW"
                    elif header_dollar_per_bill and customer_class != "residential":
                        rate_unit = "$/bill"
                    else:
                        rate_unit = "$/kWh"
                    snippet = "\n".join(lines[start_line:li + 1])[:source_snippet_max]
                    _add(customer_class, rate_val, rate_unit, snippet)
                    break
                # Check for standalone numeric (possibly parenthesized)
                nm = re.match(r'^\s*(\([\d.]+\)|[-]?[\d]+\.[\d]+)\s*$', candidate)
                if nm:
                    raw_val = nm.group(1).strip()
                    rate_val = _parse_paren_value(raw_val)
                    if in_demand_section:
                        rate_unit = "$/kW"
                    else:
                        # Bare numerics with a ¢/kWh header need /100 conversion
                        if header_cents and not header_dollar:
                            rate_val = round(rate_val / 100.0, 6)
                        rate_unit = "$/kWh"
                    snippet = "\n".join(lines[start_line:li + 1])[:source_snippet_max]
                    _add(customer_class, rate_val, rate_unit, snippet)
                    break
                # If the line starts a new class name, stop searching
                if _RIDER_CLASS_LINE_RE.match(candidate):
                    break
        if added:
            return

    # --- Fallback: single-line table row "Residential  RES,...  0.216" ---
    table_matches = list(_RIDER_TABLE_ROW_RE.finditer(text))
    if table_matches:
        for tm in table_matches:
            class_name = tm.group(1).strip()
            customer_class = _class_from_name(class_name)
            raw_val = tm.group(2).strip()
            rate_val = _parse_paren_value(raw_val)
            in_demand_section = demand_section_start >= 0 and tm.start() >= demand_section_start
            if in_demand_section:
                rate_unit = "$/kW"
            else:
                # Bare numerics with a ¢/kWh header need /100 conversion
                if header_cents and not header_dollar:
                    rate_val = round(rate_val / 100.0, 6)
                rate_unit = "$/kWh"
            snippet = text[max(0, tm.start() - 20): tm.end() + 40]
            _add(customer_class, rate_val, rate_unit, snippet)
        if added:
            return

    # --- Pattern 5: Narrative prose rate (last-resort, residential only, low confidence) ---
    # Catches "increase/decrease of X.XXX cents per kWh" found in customer notices and
    # multi-rider adjustment orders (JAA, REPS, DSM).  Only fires when nothing else matched.
    if not added:
        m_prose = _RIDER_PROSE_RATE_RE.search(text)
        if m_prose:
            rate_val = float(m_prose.group(1))
            snippet = text[max(0, m_prose.start() - 80): m_prose.end() + 80]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="adjustment",
                    charge_label="Rider Adjustment",
                    rate_value=round(rate_val / 100.0, 6),
                    rate_unit="$/kWh",
                    season="all_year",
                    customer_class="residential",
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.65,  # lower — prose form, residential class only
                )
            )
            return

    # --- Pattern 6: Percentage-of-bill credit (e.g. RECD: "5% times the stated charges") ---
    # These riders apply a percentage discount rather than a fixed ¢/kWh rate.
    # Store the percentage value itself (e.g. 5.0) in rate_value with unit "%".
    if not added:
        m_pct = _RIDER_PCT_CREDIT_RE.search(text)
        if m_pct:
            pct_val = float(m_pct.group(1))
            snippet = text[max(0, m_pct.start() - 80): m_pct.end() + 80]
            charges.append(
                TariffChargeRecord(
                    version_id=version_id,
                    family_key=family_key,
                    charge_type="adjustment",
                    charge_label="Rider Percentage Credit",
                    rate_value=pct_val,
                    rate_unit="%",
                    season="all_year",
                    customer_class="residential",
                    source_snippet=snippet[:source_snippet_max],
                    confidence_score=0.80,
                )
            )


def parse_nc_progress_leaf_file(
    path: Path,
    *,
    version_id: int,
    family_key: str,
    document_id: int | None = None,
) -> tuple[TariffVersionRecord, list[TariffChargeRecord], list[RiderApplicabilityRecord]]:
    """Extract PDF text then parse.

    For Rider BA (multi-column adjustment table), falls back to pdfplumber which
    preserves row structure better than PyMuPDF.
    """
    text = extract_pdf_text(path)
    version, charges, riders = parse_nc_progress_leaf(
        text,
        version_id=version_id,
        family_key=family_key,
        document_id=document_id,
    )

    # Rider BA: always parse via pdfplumber (which preserves row structure).
    # PyMuPDF interleaves multi-column table cells; pdfplumber maintains row order.
    if "Billing Adjustment Factors" in text and ("RIDER BA" in text or "Rider BA" in text):
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(path) as pdf:
                plumber_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            _, plumber_charges, _ = parse_nc_progress_leaf(
                plumber_text,
                version_id=version_id,
                family_key=family_key,
                document_id=document_id,
            )
            adjustment_charges = [c for c in plumber_charges if c.charge_type == "adjustment"]
            if adjustment_charges:
                # Replace any adjustment charges from multi-line parser with pdfplumber results
                charges[:] = [c for c in charges if c.charge_type != "adjustment"]
                charges.extend(adjustment_charges)
        except Exception:
            pass  # pdfplumber not available or parse failed; skip

    return version, charges, riders
