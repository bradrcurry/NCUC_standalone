"""
Rate Charge Extraction from Historical Tariff Documents

Extracts customer charges, energy charges, demand charges, and TOU periods
from historical tariff documents using pattern matching and table detection.

Handles two document formats observed in NC Progress tariff books:
  - Dollar format:  "Customer Charge: $12.50 per month"
  - Cent format:    "7.569¢ per on-peak kWh"  (pdfplumber renders ¢ as garbled byte)
  - Split format:   label on one line, value on the next (two-column PDFs)
"""

import re
from typing import List, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


# ¢ cent sign rendered by pdfplumber from various PDF encodings
_CENT_RE = re.compile(
    r'(\d+\.?\d*)\s*'           # digits (e.g. 7.569)
    r'(?:[¢\x82\xa2?]\s*|c\s+)'  # cent symbol variants or "c "
    r'per\s+',
    re.IGNORECASE,
)

# Dollar-amount pattern: "$12.50" or "$ 12.50"
_DOLLAR_RE = re.compile(r'\$\s*(\d+\.?\d*)')

# Generic bare number followed by a known unit keyword
_BARE_RATE_RE = re.compile(
    r'(\d+\.?\d*)\s+'
    r'(?:per\s+k[Ww]h|per\s+k[Ww](?!\s*h)|/\s*k[Ww]h|/\s*k[Ww](?!\s*h))',
    re.IGNORECASE,
)


@dataclass
class ExtractedCharge:
    """Represents a single extracted charge from a tariff document."""
    charge_type: str        # 'fixed', 'energy_block', 'demand', 'tou_energy', 'adjustment'
    charge_label: str
    rate_value: Optional[float]
    rate_unit: str          # '$/month', '$/kWh', '$/kW', 'cents/kWh', '%'
    season: Optional[str] = 'all_year'
    tou_period: Optional[str] = None
    tier_min: Optional[float] = None
    tier_max: Optional[float] = None
    source_snippet: str = ""
    confidence_score: float = 0.0


def _norm_tou(text: str) -> Optional[str]:
    """Return normalised TOU period name or None."""
    t = text.lower()
    if 'shoulder' in t:
        return 'shoulder'
    if 'critical' in t or 'cpp' in t:
        return 'critical_peak'
    if 'on' in t and 'peak' in t:
        return 'on_peak'
    if 'off' in t and 'peak' in t:
        return 'off_peak'
    if 'mid' in t and 'peak' in t:
        return 'mid_peak'
    if 'partial' in t and 'peak' in t:
        return 'partial_peak'
    if 'peak' in t:
        return 'peak'
    return None


def _norm_season(text: str) -> Optional[str]:
    """Return normalised season or None (meaning all_year)."""
    t = text.lower()
    if 'summer' in t or 'june' in t or 'jul' in t or 'aug' in t or 'sept' in t:
        return 'summer'
    if 'winter' in t or 'october' in t or 'nov' in t or 'dec' in t or 'jan' in t:
        return 'winter'
    return None


class ResidentialRateExtractor:
    """
    Extract rates from Progress NC tariff documents.

    Handles three line formats:
      1. Same-line dollar:  "Customer Charge: $12.50 per month"
      2. Same-line cent:    "7.569¢ per on-peak kWh"
      3. Split value:       header line followed immediately by a bare value line
                            e.g. "Basic Customer Charge:"  then  "$16.85"
    """

    # ---- label-detection patterns ----------------------------------------
    CUSTOMER_CHARGE_LABELS = re.compile(
        r'(?:basic\s+)?customer\s+(?:service\s+)?charge'
        r'|base\s+charge'
        r'|monthly\s+(?:service\s+)?charge'
        r'|account\s+charge'
        r'|minimum\s+(?:charge|bill)',
        re.IGNORECASE,
    )

    ENERGY_CHARGE_LABELS = re.compile(
        r'(?:k[Ww]h\s+)?energy\s+charge'
        r'|per\s+k[Ww]h'
        r'|electricity\s+charge'
        r'|(?:on|off|shoulder|mid|critical)[- ]peak\s+k[Ww]h',
        re.IGNORECASE,
    )

    DEMAND_CHARGE_LABELS = re.compile(
        r'(?:on[- ]peak\s+)?(?:k[Ww]\s+)?demand\s+charge'
        r'|per\s+k[Ww](?!\s*h)'
        r'|maximum\s+demand'
        r'|billing\s+demand\s+charge',
        re.IGNORECASE,
    )

    TOU_ENERGY_LABELS = re.compile(
        r'(?:on|off|shoulder|mid|critical|partial)[- ]?peak'
        r'|shoulder\s+k[Ww]h'
        r'|time[- ]of[- ]use'
        r'|tou',
        re.IGNORECASE,
    )

    SEASON_LABELS = re.compile(
        r'(?:june|july|august|september|summer)'
        r'|(?:october|november|december|january|february|march|winter)',
        re.IGNORECASE,
    )

    # tier-boundary sentinel: lines like "For the first 350 kWh"
    TIER_BOUNDARY = re.compile(
        r'for\s+(?:the\s+)?(?:first|next|all\s+over)|above',
        re.IGNORECASE,
    )

    def extract_from_text(self, text: str, effective_start: Optional[str] = None) -> List[ExtractedCharge]:
        """Extract charges from raw document text."""
        charges = []
        lines = [ln.rstrip() for ln in text.split('\n')]

        i = 0
        while i < len(lines):
            line = lines[i]

            # ---- skip tier-boundary lines (kWh / kW thresholds, not rates) --
            if self.TIER_BOUNDARY.search(line):
                i += 1
                continue

            # ---- try to extract a charge from this line ---------------------
            charge = None

            if self.CUSTOMER_CHARGE_LABELS.search(line):
                charge = self._extract_charge(line, 'fixed', lines, i)

            elif self.DEMAND_CHARGE_LABELS.search(line):
                charge = self._extract_charge(line, 'demand', lines, i)

            elif self.ENERGY_CHARGE_LABELS.search(line):
                charge = self._extract_charge(line, 'energy_block', lines, i)

            # cent-format TOU line: "7.569¢ per on-peak kWh"
            elif _CENT_RE.search(line) and self.TOU_ENERGY_LABELS.search(line):
                charge = self._parse_cent_line(line)

            # cent-format plain energy: "7.460¢ per kWh"
            elif _CENT_RE.search(line) and re.search(r'per\s+k[Ww]h', line, re.I):
                charge = self._parse_cent_line(line)

            if charge:
                charges.append(charge)

            i += 1

        return charges

    # ------------------------------------------------------------------
    def _extract_charge(self, line: str, charge_type: str,
                        lines: List[str], idx: int) -> Optional[ExtractedCharge]:
        """
        Try to parse a charge from the current line.
        If no value is found on the line, peek at the next non-empty line.
        """
        value, unit = self._parse_value_and_unit(line)

        # Split format: look one line ahead for a bare dollar amount
        if value is None and idx + 1 < len(lines):
            next_line = lines[idx + 1].strip()
            dollar_m = _DOLLAR_RE.match(next_line)
            if dollar_m and len(next_line) < 30:
                value = float(dollar_m.group(1))
                unit = self._infer_unit_from_context(line, charge_type)

        if value is None:
            return None

        # Skip implausible values: numbers that look like tier thresholds
        if charge_type == 'demand' and value > 200:
            return None
        if charge_type in ('energy_block', 'tou_energy') and unit in ('$/kWh', 'cents/kWh') and value > 1.0:
            return None  # >$1/kWh is unreasonable
        if charge_type == 'fixed' and value > 500:
            return None

        tou_period = _norm_tou(line)
        season = _norm_season(line) or 'all_year'

        # Refine charge type for TOU energy lines
        if tou_period and charge_type == 'energy_block':
            charge_type = 'tou_energy'

        label_m = re.match(r'^([^$\d¢]+)', line.strip())
        label = label_m.group(1).strip().rstrip(':').strip() if label_m else charge_type.title()

        confidence = 0.85
        if tou_period:
            confidence += 0.05
        if season != 'all_year':
            confidence += 0.05
        confidence = min(confidence, 0.95)

        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=label,
            rate_value=value,
            rate_unit=unit,
            season=season,
            tou_period=tou_period,
            tier_min=None,
            tier_max=None,
            source_snippet=line[:100],
            confidence_score=confidence,
        )

    def _parse_cent_line(self, line: str) -> Optional[ExtractedCharge]:
        """Parse a cent-format rate line like '7.569¢ per on-peak kWh'."""
        m = _CENT_RE.search(line)
        if not m:
            return None
        try:
            cents = float(m.group(1))
        except ValueError:
            return None

        # Convert cents to dollars
        value = cents / 100.0
        unit = '$/kWh'

        tou_period = _norm_tou(line)
        season = _norm_season(line) or 'all_year'
        charge_type = 'tou_energy' if tou_period else 'energy_block'

        label_m = re.match(r'^([^$\d¢?]+)', line.strip())
        label = label_m.group(1).strip().rstrip(':').strip() if label_m else charge_type.title()
        if not label:
            label = f"{tou_period or 'energy'} rate"

        confidence = 0.88
        if tou_period:
            confidence += 0.05
        if season != 'all_year':
            confidence += 0.02
        confidence = min(confidence, 0.95)

        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=label,
            rate_value=value,
            rate_unit=unit,
            season=season,
            tou_period=tou_period,
            tier_min=None,
            tier_max=None,
            source_snippet=line[:100],
            confidence_score=confidence,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_value_and_unit(line: str):
        """Return (float_value, unit_str) or (None, None)."""

        # 1. cent format: "7.569¢ per kWh" — must check before dollar
        m = _CENT_RE.search(line)
        if m:
            try:
                return float(m.group(1)) / 100.0, '$/kWh'
            except ValueError:
                pass

        # 2. dollar format: "$12.50"
        m = _DOLLAR_RE.search(line)
        if m:
            try:
                val = float(m.group(1))
                unit = '$/month'
                if re.search(r'/\s*k[Ww]h|per\s+k[Ww]h', line, re.I):
                    unit = '$/kWh'
                elif re.search(r'/\s*k[Ww](?!\s*h)|per\s+k[Ww](?!\s*h)', line, re.I):
                    unit = '$/kW'
                return val, unit
            except ValueError:
                pass

        # 3. bare rate: "0.0549 per kWh"
        m = _BARE_RATE_RE.search(line)
        if m:
            try:
                val = float(m.group(1))
                unit = '$/kWh' if re.search(r'k[Ww]h', m.group(0), re.I) else '$/kW'
                return val, unit
            except ValueError:
                pass

        return None, None

    @staticmethod
    def _infer_unit_from_context(label_line: str, charge_type: str) -> str:
        """Guess the unit from the label line when value is on the next line."""
        if charge_type == 'fixed':
            return '$/month'
        if charge_type == 'demand':
            return '$/kW'
        if re.search(r'k[Ww]h', label_line, re.I):
            return '$/kWh'
        if re.search(r'k[Ww](?!\s*h)', label_line, re.I):
            return '$/kW'
        return '$/unit'


def extract_charges_from_document(text: str, family_key: str,
                                  effective_start: Optional[str] = None) -> List[ExtractedCharge]:
    """
    Main extraction function.

    Args:
        text: Full document text from PDF
        family_key: Family identifier (e.g., 'nc-progress-leaf-500')
        effective_start: Effective date of the tariff version

    Returns:
        List of ExtractedCharge objects
    """
    extractor = ResidentialRateExtractor()
    charges = extractor.extract_from_text(text, effective_start)
    logger.info(f"Extracted {len(charges)} charges from {family_key} "
                f"(effective {effective_start or 'unknown'})")
    return charges
