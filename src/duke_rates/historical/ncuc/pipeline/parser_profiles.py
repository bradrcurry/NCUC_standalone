from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Protocol

from duke_rates.historical.ncuc.pipeline.rate_extractor import (
    ExtractedCharge,
    ResidentialRateExtractor,
)
from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
    normalize_ocr_label,
    normalize_ocr_money_line,
)
from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf, parse_nc_carolinas_leaf_file
from duke_rates.parse.nc_progress import parse_nc_progress_leaf, parse_nc_progress_leaf_file
from duke_rates.parse.rider_summary import parse_rider_summary, parse_rider_summary_from_pdf
from duke_rates.utils.duke_company import detect_duke_company


class HistoricalRateParserProfile(Protocol):
    """Strategy interface for historical rate extraction."""

    name: str

    def supports(self, doc: dict, text: str) -> bool: ...
    def score(self, doc: dict, text: str) -> float: ...

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]: ...


@dataclass(frozen=True)
class ParserProfileCandidate:
    name: str
    score: float
    supported: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParserProfileSignals:
    family_key: str
    company: str
    title: str
    text_lower: str
    is_current_progress_pdf: bool
    is_current_carolinas_pdf: bool
    leaf_no: str | None
    has_summary_text: bool
    has_tou_terms: bool
    has_discount_term: bool
    has_demand_charge_term: bool
    has_progress_company_text: bool
    has_carolinas_company_text: bool
    has_rs_marker: bool
    has_flat_rate_markers: bool
    has_page_bounds: bool

    def to_metadata(self) -> dict[str, object]:
        return asdict(self)


def _convert_progress_tariff_charges(charges: list[object]) -> list[ExtractedCharge]:
    extracted: list[ExtractedCharge] = []
    for charge in charges:
        customer_class = getattr(charge, "customer_class", None)
        label = charge.charge_label or "Rider Adjustment"
        if label == "Rider Adjustment" and customer_class:
            label = f"Rider Adjustment - {customer_class}"
        extracted.append(
            ExtractedCharge(
                charge_type=charge.charge_type,
                charge_label=label,
                rate_value=charge.rate_value,
                rate_unit=charge.rate_unit or "",
                season=charge.season,
                tou_period=charge.tou_period,
                tier_min=charge.tier_min,
                tier_max=charge.tier_max,
                source_snippet=charge.source_snippet or "",
                confidence_score=charge.confidence_score,
            )
        )
    return extracted


@dataclass
class GenericResidentialProfile:
    """
    Default profile for NCUC historical residential-ish leafs.

    This preserves the current behavior while giving us a clean place to split
    rules by company / era / document structure later.
    """

    name: str = "generic_residential"

    def __post_init__(self) -> None:
        self.extractor = ResidentialRateExtractor()

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if not family_key.startswith(("nc-progress-leaf-", "nc-carolinas-leaf-")):
            return False
        lowered = text.lower()
        title = (doc.get("title") or "").lower()
        has_residential_signal = (
            "residential" in lowered
            or "residential" in title
            or "schedule rs" in lowered
            or "residential service" in lowered
        )
        has_rate_shape = (
            "basic customer charge" in lowered
            or "basic facilities charge" in lowered
            or "per kwh" in lowered
            or "per kilowatt-hour" in lowered
            or "on-peak" in lowered
            or "off-peak" in lowered
        )
        return has_residential_signal and has_rate_shape

    def score(self, doc: dict, text: str) -> float:
        return 0.1 if self.supports(doc, text) else 0.0

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        return self.extractor.extract_from_text(text, doc.get("effective_start"))


@dataclass
class UnsupportedDocumentProfile:
    """Null profile used when no specialized or generic profile actually supports a document."""

    name: str = "unknown"

    def supports(self, doc: dict, text: str) -> bool:
        return True

    def score(self, doc: dict, text: str) -> float:
        return 0.0

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        return []


@dataclass
class ProgressResidentialTouProfile:
    """
    Narrow profile for modern DEP residential TOU leaves.

    Right now it delegates to the same extractor, but it is intentionally split
    out so future TOU-era logic can diverge without touching the generic parser.
    """

    name: str = "progress_residential_tou"

    def __post_init__(self) -> None:
        self.extractor = ResidentialRateExtractor()

    _SEASON_PATTERNS = (
        (re.compile(r"service\s+used\s+during\s+(?:may|june)(?:\s+through\s+|\s*-\s*|\s+to\s+)september", re.I), "summer"),
        (re.compile(r"service\s+used\s+during\s+october(?:\s+through\s+|\s*-\s*|\s+to\s+)(?:april|may)", re.I), "winter"),
        (re.compile(r"\bsummer\b", re.I), "summer"),
        (re.compile(r"\bwinter\b", re.I), "winter"),
    )
    _TOU_RATE_PATTERNS = (
        re.compile(
            r"(?P<value>\d+\.?\d*)\s*(?:[¢\x82\xa2]|\bc\b)\s*per\s+"
            r"(?P<period>super\s+off[- ]peak|critical[- ]peak|mid[- ]peak|on[- ]peak|off[- ]peak|shoulder|discount)\s*kwh",
            re.I,
        ),
        re.compile(
            r"\$?\s*(?P<value>\d+\.?\d*)\s*(?:per|/)\s+"
            r"(?P<period>super\s+off[- ]peak|critical[- ]peak|mid[- ]peak|on[- ]peak|off[- ]peak|shoulder|discount)\s*kwh",
            re.I,
        ),
    )
    _DEMAND_RATE_PATTERNS = (
        re.compile(
            r"(?P<label>(?:base|billing|maximum|on[- ]peak|critical[- ]peak|mid[- ]peak|off[- ]peak)[^:\n]{0,60}?demand(?:\s+charge)?)"
            r"\s*:?\s*\$?\s*(?P<value>\d+\.?\d*)\s*(?:per|/)\s*kw\b",
            re.I,
        ),
        re.compile(
            r"(?P<label>demand(?:\s+charge)?)\s*:?\s*\$?\s*(?P<value>\d+\.?\d*)\s*(?:per|/)\s*kw\b",
            re.I,
        ),
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in {
            "nc-progress-leaf-502",
            "nc-progress-leaf-503",
            "nc-progress-leaf-504",
        }:
            return False
        lowered = text.lower()
        return any(token in lowered for token in ("on-peak", "off-peak", "time-of-use", "critical peak"))

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.8
        if any(token in lowered for token in ("discount", "super off-peak", "critical peak")):
            score += 0.08
        if "demand charge" in lowered:
            score += 0.04
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        generic = self.extractor.extract_from_text(text, doc.get("effective_start"))
        tou_charges = self._extract_tou_rates(text)
        demand_charges = self._extract_demand_rates(text)

        merged = [
            charge
            for charge in generic
            if charge.charge_type not in {"tou_energy", "demand"}
        ]
        if generic and not demand_charges:
            merged.extend(charge for charge in generic if charge.charge_type == "demand")
        merged.extend(tou_charges or [charge for charge in generic if charge.charge_type == "tou_energy"])
        merged.extend(demand_charges)
        return self._dedupe_charges(merged)

    # Two-column seasonal header: "months of June through September: months of October through May:"
    _TWO_COL_SUMMER_HEADER_RE = re.compile(
        r"months\s+of\s+(?:may|june)\s+through\s+september", re.I
    )
    _TWO_COL_WINTER_HEADER_RE = re.compile(
        r"months\s+of\s+october\s+through\s+(?:april|may)", re.I
    )

    def _extract_tou_rates(self, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_season: str | None = None
        two_col_seasonal = False  # True when summer/winter are side-by-side columns

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            # Detect two-column seasonal layout: both summer and winter headers on the same line(s)
            if self._TWO_COL_SUMMER_HEADER_RE.search(line) and self._TWO_COL_WINTER_HEADER_RE.search(line):
                two_col_seasonal = True
                continue

            season = self._detect_season(line)
            if season and not two_col_seasonal:
                current_season = season

            for pattern in self._TOU_RATE_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue

                try:
                    value = float(match.group("value"))
                except ValueError:
                    continue

                is_cent = "¢" in line or "\x82" in line or "\xa2" in line or re.search(r"\bc\b", line, re.I)
                if is_cent:
                    value = value / 100.0

                period_label = match.group("period").strip()
                period_key = self._normalize_period(period_label)
                label = f"{period_key.replace('_', ' ').title()} Energy Charge"

                if two_col_seasonal:
                    # Find all matches on this line — first is summer, second is winter
                    all_matches = list(pattern.finditer(line))
                    if len(all_matches) >= 2:
                        for col_match, col_season in zip(all_matches[:2], ("summer", "winter")):
                            try:
                                col_val = float(col_match.group("value"))
                            except ValueError:
                                continue
                            if is_cent:
                                col_val = col_val / 100.0
                            col_period = self._normalize_period(col_match.group("period").strip())
                            charges.append(
                                ExtractedCharge(
                                    charge_type="tou_energy",
                                    charge_label=f"{col_period.replace('_', ' ').title()} Energy Charge",
                                    rate_value=col_val,
                                    rate_unit="$/kWh",
                                    season=col_season,
                                    tou_period=col_period,
                                    tier_min=None,
                                    tier_max=None,
                                    source_snippet=line[:100],
                                    confidence_score=0.94,
                                )
                            )
                    else:
                        # Only one match on line — still seasonal context but can't split
                        charges.append(
                            ExtractedCharge(
                                charge_type="tou_energy",
                                charge_label=label,
                                rate_value=value,
                                rate_unit="$/kWh",
                                season=current_season or "all_year",
                                tou_period=period_key,
                                tier_min=None,
                                tier_max=None,
                                source_snippet=line[:100],
                                confidence_score=0.9,
                            )
                        )
                else:
                    charges.append(
                        ExtractedCharge(
                            charge_type="tou_energy",
                            charge_label=label,
                            rate_value=value,
                            rate_unit="$/kWh",
                            season=current_season or "all_year",
                            tou_period=period_key,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=line[:100],
                            confidence_score=0.94 if current_season else 0.9,
                        )
                    )
                break

        return charges

    def _extract_demand_rates(self, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_season: str | None = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            season = self._detect_season(line)
            if season:
                current_season = season

            for pattern in self._DEMAND_RATE_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue

                try:
                    value = float(match.group("value"))
                except ValueError:
                    continue

                label = re.sub(r"\s+", " ", match.group("label")).strip().rstrip(":")
                charges.append(
                    ExtractedCharge(
                        charge_type="demand",
                        charge_label=label.title(),
                        rate_value=value,
                        rate_unit="$/kW",
                        season=current_season or "all_year",
                        tou_period=self._normalize_demand_period(label),
                        tier_min=None,
                        tier_max=None,
                        source_snippet=line[:100],
                        confidence_score=0.92 if "on-peak" in label.lower() or "base" in label.lower() else 0.88,
                    )
                )
                break

        return charges

    @classmethod
    def _detect_season(cls, line: str) -> str | None:
        for pattern, season in cls._SEASON_PATTERNS:
            if pattern.search(line):
                return season
        return None

    @staticmethod
    def _normalize_period(period: str) -> str:
        normalized = period.lower().replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return {
            "on peak": "on_peak",
            "off peak": "off_peak",
            "mid peak": "mid_peak",
            "critical peak": "critical_peak",
            "super off peak": "super_off_peak",
            "shoulder": "shoulder",
            "discount": "discount",
        }.get(normalized, normalized.replace(" ", "_"))

    @classmethod
    def _normalize_demand_period(cls, label: str) -> str | None:
        lowered = label.lower()
        if "base" in lowered or "billing" in lowered or "maximum" in lowered:
            return "base"
        normalized = re.sub(r"\bdemand(?:\s+charge)?\b", "", lowered)
        normalized = re.sub(r"\s+", " ", normalized).strip(" :-")
        return cls._normalize_period(normalized) if "peak" in normalized else None

    @staticmethod
    def _dedupe_charges(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                charge.rate_value,
                charge.rate_unit,
                charge.season,
                charge.tou_period,
                charge.tier_min,
                charge.tier_max,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressResidentialFlatProfile:
    """Profile for Progress residential flat sheets like historical Leaf 500."""

    name: str = "progress_residential_flat"

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in {"nc-progress-leaf-500", "nc-progress-leaf-505"}:
            return False

        lowered = text.lower()
        has_company_signal = "progress" in detect_duke_company(lowered)
        has_flat_rate_signal = "basic customer charge" in lowered and "per kwh" in lowered
        has_tou_signal = any(
            token in lowered
            for token in ("on-peak", "off-peak", "time of use", "time-of-use", "critical peak")
        )
        return has_company_signal and has_flat_rate_signal and not has_tou_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        family_key = (doc.get("family_key") or "").lower()
        score = 0.84
        if family_key == "nc-progress-leaf-500":
            score += 0.08
        if "for all kwh" in text.lower():
            score += 0.03
        return min(score, 0.95)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-leaf-500"
        # Compliance bundles use a two-column rate table that pdfplumber drops.
        # Re-extract with fitz when the document has page bounds, so the columnar
        # kWh rates are preserved.
        path = Path(doc.get("local_path") or "")
        start_page = doc.get("start_page")
        end_page = doc.get("end_page")
        if path.is_file() and path.suffix.lower() == ".pdf" and start_page is not None:
            try:
                import fitz  # type: ignore
                fitz_doc = fitz.open(str(path))
                end_idx = int(end_page) if end_page is not None else int(start_page)
                fitz_text = "\n".join(
                    fitz_doc[pg].get_text()
                    for pg in range(int(start_page) - 1, end_idx)
                    if pg < len(fitz_doc)
                )
                fitz_doc.close()
                if fitz_text.strip():
                    text = fitz_text
            except Exception:
                pass  # fall through to pdfplumber-extracted text

        _, charges, _ = parse_nc_progress_leaf(
            text,
            version_id=0,
            family_key=family_key,
        )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class ProgressCurrentLeafBridgeProfile:
    """Bridge current-style DEP leaf PDFs into the historical extraction path."""

    name: str = "progress_current_leaf_bridge"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-501",
        "nc-progress-leaf-520",
        "nc-progress-leaf-532",
        "nc-progress-leaf-533",
        "nc-progress-leaf-535",
        "nc-progress-leaf-674",
    }

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        # Compliance bundles: accept when page bounds are set and family markers match,
        # even if "leaf no." is absent from pdfplumber-extracted text.
        if doc.get("start_page") is not None:
            return self._has_family_markers(family_key, lowered)
        if not self._is_current_progress_pdf(doc):
            return False
        if "leaf no." not in lowered:
            return False
        return self._has_family_markers(family_key, lowered)

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0

        family_key = (doc.get("family_key") or "").lower()
        lowered = text.lower()
        score = 0.86
        if family_key == "nc-progress-leaf-501" and ("r-toud" in lowered or "time-of-use demand" in lowered):
            score += 0.07
        elif family_key == "nc-progress-leaf-520" and (
            "schedule sgs" in lowered or "small general service" in lowered
        ):
            score += 0.06
        elif family_key == "nc-progress-leaf-532" and (
            "schedule lgs" in lowered or "large general service" in lowered
        ):
            score += 0.06
        elif family_key == "nc-progress-leaf-533" and (
            "schedule lgs-tou" in lowered or ("large general service" in lowered and "time-of-use" in lowered)
        ):
            score += 0.06
        elif family_key == "nc-progress-leaf-535" and (
            "schedule hp" in lowered or "high load factor" in lowered or "high power" in lowered
        ):
            score += 0.06
        elif family_key == "nc-progress-leaf-674" and (
            "rider ps" in lowered or "partial requirements" in lowered
        ):
            score += 0.06
        if "demand charge" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-leaf-501"
        path = Path(doc.get("local_path") or "")
        start_page = doc.get("start_page")
        end_page = doc.get("end_page")
        # Compliance bundles have page bounds — use fitz for accurate two-column table extraction.
        if path.is_file() and path.suffix.lower() == ".pdf" and start_page is not None:
            try:
                import fitz  # type: ignore
                fitz_doc = fitz.open(str(path))
                end_idx = int(end_page) if end_page is not None else int(start_page)
                fitz_text = "\n".join(
                    fitz_doc[pg].get_text()
                    for pg in range(int(start_page) - 1, end_idx)
                    if pg < len(fitz_doc)
                )
                fitz_doc.close()
                if fitz_text.strip():
                    _, charges, _ = parse_nc_progress_leaf(
                        fitz_text,
                        version_id=0,
                        family_key=family_key,
                    )
                    return _convert_progress_tariff_charges(charges)
            except Exception:
                pass  # fall through to full-file extraction
        if path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(
                path,
                version_id=0,
                family_key=family_key,
            )
        else:
            _, charges, _ = parse_nc_progress_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )

        return _convert_progress_tariff_charges(charges)

    @staticmethod
    def _is_current_progress_pdf(doc: dict) -> bool:
        local_path = str(doc.get("local_path") or "").replace("/", "\\").lower()
        return "\\raw\\nc\\progress\\" in local_path

    @staticmethod
    def _has_family_markers(family_key: str, lowered: str) -> bool:
        if family_key == "nc-progress-leaf-501":
            return "r-toud" in lowered or ("time-of-use" in lowered and "demand" in lowered)
        if family_key == "nc-progress-leaf-520":
            return "schedule sgs" in lowered or "small general service" in lowered
        if family_key == "nc-progress-leaf-532":
            return "schedule lgs" in lowered or "large general service" in lowered
        if family_key == "nc-progress-leaf-533":
            return "schedule lgs-tou" in lowered or (
                "large general service" in lowered and "time-of-use" in lowered
            )
        if family_key == "nc-progress-leaf-535":
            return "schedule hp" in lowered or "high load factor" in lowered or "high power" in lowered
        if family_key == "nc-progress-leaf-674":
            return "rider ps" in lowered or "partial requirements" in lowered
        return False


@dataclass
class ProgressSpecialtyRiderProfile:
    """Profile for current-style DEP specialty riders with explicit fee/credit language."""

    name: str = "progress_specialty_rider"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-654",
        "nc-progress-leaf-655",
        "nc-progress-leaf-668",
        "nc-progress-leaf-669",
        "nc-progress-leaf-670",
    }

    _NFS_NOTIFICATION_RE = re.compile(
        r"Non-Firm Standby Notification Customer Charge:\s*\$?([\d.]+)",
        re.I,
    )
    _NFS_DELIVERY_RE = re.compile(
        r"(Transmission System|Distribution System)[^\n$]{0,80}\$([\d.]+)/kWh",
        re.I,
    )
    _NFS_ADDER_RE = re.compile(
        r"([\d.]+)\s+cents\s+per\s+kWh\s+of\s+Incremental\s+Load\s+for\s+the\s+Incentive\s+Margin",
        re.I,
    )
    _LLC_CUSTOMER_CHARGE_RE = re.compile(r"Customer Charge:\s*\$([\d.]+)", re.I)
    _LLC_DISCOUNT_RE = re.compile(r"discount\s*=\s*\$([\d.]+)\s+per\s+k[wW]", re.I)
    _LLC_LEVEL1_RE = re.compile(
        r"\$([\d.]+)\s+per\s+kilowatt-hour\s+for\s+all\s+kilowatt-hours\s+attributable\s+to\s+premium\s+demand",
        re.I,
    )
    _LLC_LEVEL2_RE = re.compile(
        r"\$([\d.]+)\s+for\s+each\s+k[wW]\s+of\s+premium\s+demand",
        re.I,
    )
    _SOLAR_MONTHLY_CREDIT_RE = re.compile(
        r"Monthly Credit for Net Excess Energy,\s*per\s*kWh\s+\$?([\d.]+)",
        re.I,
    )
    _SOLAR_NET_EXCESS_RE = re.compile(
        r"Net Excess Energy Credit per month,\s*per\s*kWh\s+([\d.]+)\s*[¢\u00a2]",
        re.I,
    )
    _SOLAR_NON_BYPASSABLE_RE = re.compile(
        r"Non-Bypassable Charge per month,\s*per\s+Nameplate Capacity\s+kW\s+\$([\d.]+)",
        re.I,
    )
    _SOLAR_GRID_ACCESS_RE = re.compile(
        r"Grid Access Fee per month,\s*per\s+Nameplate Capacity\s+kW(?: above 15 kW)?\s+\$([\d.]+)",
        re.I,
    )
    _SOLAR_MINIMUM_BILL_RE = re.compile(r"minimum bill of\s+\$([\d.]+)", re.I)
    _SOLAR_TOU_COMPONENT_RE = re.compile(
        r"(On-Peak|Off-Peak|Discount)\s+Energy per month,\s*per\s+kWh\s+([\d.]+)\s*[¢\u00a2]",
        re.I,
    )
    _SOLAR_ALL_ENERGY_RE = re.compile(
        r"All Energy per month,\s*per\s+kWh\s+([\d.]+)\s*[¢\u00a2]",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        if not ProgressCurrentLeafBridgeProfile._is_current_progress_pdf(doc):
            return False
        lowered = text.lower()
        if "leaf no." not in lowered:
            return False
        return self._has_family_markers(family_key, lowered)

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0

        family_key = (doc.get("family_key") or "").lower()
        lowered = text.lower()
        score = 0.87
        if family_key == "nc-progress-leaf-654" and "non-firm standby" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-655" and "large load curtailable" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-668" and "monthly credit for net excess energy" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-669" and "net excess energy credit" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-670" and "net excess energy credit" in lowered:
            score += 0.05
        if "monthly rate" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = (doc.get("family_key") or "").lower()
        if family_key == "nc-progress-leaf-654":
            return self._extract_leaf_654(text)
        if family_key == "nc-progress-leaf-655":
            return self._extract_leaf_655(text)
        if family_key == "nc-progress-leaf-668":
            return self._extract_leaf_668(text)
        if family_key == "nc-progress-leaf-669":
            return self._extract_leaf_669(text)
        if family_key == "nc-progress-leaf-670":
            return self._extract_leaf_670(text)
        return []

    @staticmethod
    def _has_family_markers(family_key: str, lowered: str) -> bool:
        if family_key == "nc-progress-leaf-654":
            return "rider nfs" in lowered and "non-firm standby" in lowered
        if family_key == "nc-progress-leaf-655":
            return "rider llc" in lowered and "large load curtailable" in lowered
        if family_key == "nc-progress-leaf-668":
            return "rider nsc" in lowered and "non-residential solar choice" in lowered
        if family_key == "nc-progress-leaf-669":
            return "rider nmb" in lowered and "net metering bridge" in lowered
        if family_key == "nc-progress-leaf-670":
            return "rider rsc" in lowered and "residential solar choice" in lowered
        return False

    def _extract_leaf_654(self, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._NFS_NOTIFICATION_RE.search(text):
            charges.append(self._charge("fixed", "Non-Firm Standby Notification Customer Charge", float(match.group(1)), "$/month"))
        for match in self._NFS_DELIVERY_RE.finditer(text):
            system = re.sub(r"\s+", " ", match.group(1)).strip().title()
            charges.append(self._charge("adjustment", f"Non-Firm Standby Service Delivery Charge - {system}", float(match.group(2)), "$/kWh"))
        if match := self._NFS_ADDER_RE.search(text):
            charges.append(self._charge("adjustment", "Incentive Margin Adder", float(match.group(1)) / 100.0, "$/kWh"))
        return self._dedupe(charges)

    def _extract_leaf_655(self, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._LLC_CUSTOMER_CHARGE_RE.search(text):
            charges.append(self._charge("fixed", "Customer Charge", float(match.group(1)), "$/month"))
        if match := self._LLC_DISCOUNT_RE.search(text):
            charges.append(self._charge("adjustment", "Curtailable Demand Credit", float(match.group(1)), "$/kW"))
        if match := self._LLC_LEVEL1_RE.search(text):
            charges.append(self._charge("adjustment", "Premium Demand Charge - Level 1", float(match.group(1)), "$/kWh"))
        if match := self._LLC_LEVEL2_RE.search(text):
            charges.append(self._charge("adjustment", "Premium Demand Charge - Level 2", float(match.group(1)), "$/kW"))
        return self._dedupe(charges)

    def _extract_leaf_668(self, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._SOLAR_MONTHLY_CREDIT_RE.search(text):
            charges.append(self._charge("adjustment", "Monthly Credit for Net Excess Energy", float(match.group(1)), "$/kWh"))
        return charges

    def _extract_leaf_669(self, text: str) -> list[ExtractedCharge]:
        return self._extract_solar_credit_rider(text, include_all_energy=True)

    def _extract_leaf_670(self, text: str) -> list[ExtractedCharge]:
        return self._extract_solar_credit_rider(text, include_all_energy=False)

    def _extract_solar_credit_rider(self, text: str, *, include_all_energy: bool) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._SOLAR_NET_EXCESS_RE.search(text):
            charges.append(self._charge("adjustment", "Net Excess Energy Credit", float(match.group(1)) / 100.0, "$/kWh"))
        if match := self._SOLAR_NON_BYPASSABLE_RE.search(text):
            charges.append(self._charge("fixed", "Non-Bypassable Charge", float(match.group(1)), "$/kW-month"))
        if match := self._SOLAR_GRID_ACCESS_RE.search(text):
            charges.append(self._charge("fixed", "Grid Access Fee", float(match.group(1)), "$/kW-month"))
        if match := self._SOLAR_MINIMUM_BILL_RE.search(text):
            charges.append(self._charge("fixed", "Minimum Bill", float(match.group(1)), "$/month"))
        if include_all_energy and (match := self._SOLAR_ALL_ENERGY_RE.search(text)):
            charges.append(
                self._charge(
                    "energy",
                    "Customer and Distribution Energy Charge",
                    float(match.group(1)) / 100.0,
                    "$/kWh",
                )
            )
        for match in self._SOLAR_TOU_COMPONENT_RE.finditer(text):
            period = match.group(1).lower().replace("-", "_")
            charges.append(
                self._charge(
                    "tou_energy",
                    f"{match.group(1).title()} Customer and Distribution Energy Charge",
                    float(match.group(2)) / 100.0,
                    "$/kWh",
                    tou_period=period,
                )
            )
        return self._dedupe(charges)

    @staticmethod
    def _charge(
        charge_type: str,
        charge_label: str,
        rate_value: float,
        rate_unit: str,
        *,
        tou_period: str | None = None,
    ) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=tou_period,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str, str | None]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                charge.rate_value,
                charge.rate_unit,
                charge.tou_period,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressEnergywiseBusinessProfile:
    """Profile for EnergyWise for Business credits and incentives (Leaf 706)."""

    name: str = "progress_energywise_business"
    _CYCLING_CREDIT_RE = re.compile(
        r"(\d{1,3})%\s+(summer|non-winter)[-\s]cycling\s+(?:level|option)\s*-\s*\$([\d.]+)\s+per\s+load\s+control\s+device",
        re.I,
    )
    _WINTER_CREDIT_RE = re.compile(
        r"additional\s+\$([\d.]+)\s+per\s+thermostat",
        re.I,
    )
    _BYOKW_RE = re.compile(
        r"\$([\d.]+)\s+per\s+(?:average\s+)?k[wW]\s+reduced\s+during\s+the\s+events",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in {"nc-progress-leaf-706", "nc-carolinas-rider-eb"}:
            return False
        lowered = text.lower()
        return "energywise for business" in lowered and "control credits" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "summer cycling" in lowered or "non-winter cycling" in lowered:
            score += 0.03
        if "thermostat" in lowered:
            score += 0.02
        if "bring your own kw" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        for match in self._CYCLING_CREDIT_RE.finditer(text):
            percent = match.group(1)
            season_label = "Summer" if match.group(2).strip().lower() == "summer" else "Non-Winter"
            charges.append(
                self._charge(
                    "credit",
                    f"{season_label} Control Credit - {percent}% Cycling",
                    float(match.group(3)),
                    "$/device-year",
                )
            )
        if match := self._WINTER_CREDIT_RE.search(text):
            charges.append(
                self._charge(
                    "credit",
                    "Winter Control Credit",
                    float(match.group(1)),
                    "$/thermostat-year",
                )
            )
        if match := self._BYOKW_RE.search(text):
            charges.append(
                self._charge(
                    "credit",
                    "Bring Your Own kW Incentive",
                    float(match.group(1)),
                    "$/kW",
                )
            )
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressSunSenseSolarRebateProfile:
    """Profile for DEP SunSense Solar Rebate sheets with explicit payment and credit terms."""

    name: str = "progress_sunsense_solar_rebate"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-716"}
    _PARTICIPATION_PAYMENT_RE = re.compile(
        r"one-time participation payment of\s*\$([\d.]+)\s+per\s+kilowatt",
        re.I,
    )
    _SSR_CREDIT_RE = re.compile(
        r"SSR Credit\s*=\s*\$([\d.]+)\s+per\s+kilowatt",
        re.I,
    )
    _EARLY_TERMINATION_RE = re.compile(
        r"early termination charge equal to\s*\$([\d.]+)\s+per\s+kilowatt",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "sunsense solar rebate" in lowered and "ssr credit" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "one-time participation payment" in lowered:
            score += 0.03
        if "early termination charge" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._PARTICIPATION_PAYMENT_RE.search(text):
            charges.append(self._charge("adjustment", "Participation Payment", float(match.group(1)), "$/kW"))
        if match := self._SSR_CREDIT_RE.search(text):
            charges.append(self._charge("adjustment", "SSR Credit", float(match.group(1)), "$/kW-month"))
        if match := self._EARLY_TERMINATION_RE.search(text):
            charges.append(self._charge("adjustment", "Early Termination Charge", float(match.group(1)), "$/kW"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressSolarRebateRiderProfile:
    """Profile for DEP Solar Rebate Rider SRR (Leaf 663) one-time $/watt payments."""

    name: str = "progress_solar_rebate_rider"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-663"}
    _REBATE_RE = re.compile(
        r"(Nonresidential|Residential|Non-Profit)[^\$]{0,100}\$([\d.]+)\s+per\s+watt",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "solar rebate rider srr" in lowered and "per watt" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "rebate payment" in lowered:
            score += 0.05
        if "ac nameplate" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        for match in self._REBATE_RE.finditer(text):
            label_type = match.group(1).strip().title()
            charges.append(
                self._charge(
                    "rebate",
                    f"{label_type} Solar Rebate Payment",
                    float(match.group(2)),
                    "$/watt",
                )
            )
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressMeterRelatedOptionalProgramsProfile:
    """Profile for DEP Rider MROP sheets and DEC Rider MRM sheets with explicit monthly fees and set-up charges."""

    name: str = "progress_meter_related_optional_programs"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-661",       # DEP Rider MROP — Meter-Related Optional Programs
        "nc-carolinas-rider-mrm",     # DEC Rider MRM — Manually Read Meter Rider (same format)
    }
    _TOTALMETER_OPTION1_RE = re.compile(r"Option 1:[^\$]{0,120}\$\s*([\d.]+)", re.I | re.S)
    _TOTALMETER_OPTION2_RE = re.compile(r"Option 2:[^\$]{0,120}\$\s*([\d.]+)", re.I | re.S)
    _TOTALMETER_TERMINATION_RE = re.compile(r"termination of TotalMeter[^\$]{0,160}\$\s*([\d.]+)", re.I | re.S)
    _EPO_TOTALIZED_RE = re.compile(
        r"Rate for totalized meter data only[^\$]{0,80}\$\s*([\d.]+)\s+per totalized account",
        re.I | re.S,
    )
    _EPO_PER_METER_RE = re.compile(
        r"Rate for meter data per individual meter[^\$]{0,80}\$\s*([\d.]+)\s+per meter",
        re.I | re.S,
    )
    _SETUP_PER_METER_RE = re.compile(r"Set-up fee per meter[^\$]{0,40}\$\s*([\d.]+)", re.I | re.S)
    _SETUP_TOTALIZED_RE = re.compile(
        r"Set-up fee for totalized meter data only[^\$]{0,40}\$\s*([\d.]+)",
        re.I | re.S,
    )
    _MRM_INITIAL_SETUP_RE = re.compile(r"Initial Set-[Uu]p Fee[^\$]{0,40}\$\s*([\d.]+)", re.I | re.S)
    _MRM_MONTHLY_RE = re.compile(r"Monthly Rate For MRM[^\$]{0,40}\$\s*([\d.]+)", re.I | re.S)
    # DEC Rider MRM uses "Rate per month" (no "Monthly Rate For MRM" heading)
    _DEC_MRM_MONTHLY_RE = re.compile(r"Rate per month\s+([\d.]+)", re.I | re.S)
    _MRM_EARLY_TERMINATION_RE = re.compile(r"Early Termination Charge[^\$]{0,80}\$\s*([\d.]+)", re.I | re.S)
    _DEC_MRM_SERVICE_CHARGE_RE = re.compile(r"service charge[^\$]{0,20}\$\s*([\d.]+)", re.I | re.S)
    _NON_STANDARD_METER_RE = re.compile(
        r"Monthly Rate for non-standard meter with interval data capability[^\$]{0,60}\$\s*([\d.]+)\s+per month",
        re.I | re.S,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        if family_key == "nc-carolinas-rider-mrm":
            return "rider mrm" in lowered and "manually read meter" in lowered
        return "rider mrop" in lowered and "meter-related optional programs" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        family_key = (doc.get("family_key") or "").lower()
        if family_key == "nc-carolinas-rider-mrm":
            score = 0.90
            if "initial set-up fee" in lowered:
                score += 0.03
            if "monthly rate for mrm" in lowered or "monthly rate" in lowered:
                score += 0.03
            return min(score, 0.98)
        score = 0.89
        if "totalmeter" in lowered:
            score += 0.03
        if "energy profiler online" in lowered:
            score += 0.03
        if "manually read metering" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        family_key = (doc.get("family_key") or "").lower()
        if family_key == "nc-carolinas-rider-mrm":
            # DEC Rider MRM: Initial Set-Up Fee + Rate per month + optional service charge
            for pattern, label, unit in (
                (self._MRM_INITIAL_SETUP_RE, "MRM Initial Set-Up Fee", "$"),
                (self._DEC_MRM_MONTHLY_RE, "MRM Monthly Rate", "$/month"),
                (self._MRM_EARLY_TERMINATION_RE, "MRM Early Termination Charge", "$"),
            ):
                if match := pattern.search(text):
                    charges.append(self._charge("fixed", label, float(match.group(1)), unit))
            return self._dedupe(charges)
        for pattern, label, unit in (
            (self._TOTALMETER_OPTION1_RE, "TotalMeter Monthly Rate - Option 1", "$/month"),
            (self._TOTALMETER_OPTION2_RE, "TotalMeter Monthly Rate - Option 2", "$/month"),
            (self._TOTALMETER_TERMINATION_RE, "TotalMeter Early Termination Charge", "$"),
            (self._EPO_TOTALIZED_RE, "EPO Monthly Rate - Totalized Account", "$/account-month"),
            (self._EPO_PER_METER_RE, "EPO Monthly Rate - Per Meter", "$/meter-month"),
            (self._SETUP_PER_METER_RE, "EPO Set-up Fee - Per Meter", "$"),
            (self._SETUP_TOTALIZED_RE, "EPO Set-up Fee - Totalized Account", "$"),
            (self._MRM_INITIAL_SETUP_RE, "MRM Initial Set-up Fee", "$"),
            (self._MRM_MONTHLY_RE, "MRM Monthly Rate", "$/month"),
            (self._MRM_EARLY_TERMINATION_RE, "MRM Early Termination Charge", "$"),
            (self._NON_STANDARD_METER_RE, "Non-Standard Meter Monthly Rate", "$/month"),
        ):
            if match := pattern.search(text):
                charges.append(self._charge("fixed", label, float(match.group(1)), unit))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressStandbyServiceProfile:
    """Profile for DEP Rider SS sheets with explicit standby and reservation charges."""

    name: str = "progress_standby_service"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-653"}
    _GEN_RES_NON_TOU_RE = re.compile(
        r"non-time-of-use demand rate schedules with less than\s*60% planning capacity factor\s*-\s*\$([\d.]+)/kW",
        re.I,
    )
    _GEN_RES_TOU_RE = re.compile(
        r"(?<!non-)time-of-use demand rate schedules with less than\s*60% planning capacity factor\s*-\s*\$([\d.]+)/kW",
        re.I,
    )
    _GEN_RES_HIGH_CF_RE = re.compile(
        r"60% or greater planning capacity factor\s*-\s*\$([\d.]+)/kW",
        re.I,
    )
    _STANDBY_DELIVERY_RE = re.compile(
        r"(Transmission System|Distribution System)[^\n$]{0,80}\$([\d.]+)/kW",
        re.I,
    )
    _INCENTIVE_MARGIN_RE = re.compile(
        r"([\d.]+)\s+cents?\s+per\s+kWh\s+of\s+Incremental Load for the Incentive Margin",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "supplementary and firm standby service" in lowered and "generation reservation charge" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "standby service delivery charge" in lowered:
            score += 0.03
        if "incremental load for the incentive margin" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._GEN_RES_NON_TOU_RE.search(text):
            charges.append(self._charge("fixed", "Generation Reservation Charge - Non-TOU <60% Capacity Factor", float(match.group(1)), "$/kW"))
        if match := self._GEN_RES_TOU_RE.search(text):
            charges.append(self._charge("fixed", "Generation Reservation Charge - TOU <60% Capacity Factor", float(match.group(1)), "$/kW"))
        if match := self._GEN_RES_HIGH_CF_RE.search(text):
            charges.append(self._charge("fixed", "Generation Reservation Charge - >=60% Capacity Factor", float(match.group(1)), "$/kW"))
        for match in self._STANDBY_DELIVERY_RE.finditer(text):
            system = re.sub(r"\s+", " ", match.group(1)).strip().title()
            charges.append(self._charge("fixed", f"Standby Service Delivery Charge - {system}", float(match.group(2)), "$/kW"))
        if match := self._INCENTIVE_MARGIN_RE.search(text):
            charges.append(self._charge("adjustment", "Incentive Margin Adder", float(match.group(1)) / 100.0, "$/kWh"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressStreetLightingProfile:
    """Profile for DEP street/area lighting schedules (Leaf 570 ALS, 571 SLS, 572 SLR).

    Extracts per-fixture monthly charges from the rate tables in these schedules.

    The PDF text for these schedules splits each table column onto its own line.
    Leaf 570/571 pattern (per line sequence):
        "LED 30"          — fixture label
        "30"              — wattage (pure-integer line, skip)
        "$8.19"           — monthly rate
        "10"              — kWh (pure-integer line, skip)
    Leaf 572 pattern (per line sequence):
        "7,000 lumen mercury vapor..."  — lumen/fixture type label
        "$1.61"  or  "1.23"            — monthly per-customer charge
    """

    name: str = "progress_street_lighting"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-570",
        "nc-progress-leaf-571",
        "nc-progress-leaf-572",
    }
    # pdfplumber consolidated format: "LED 30 30 $7.45 10" or "LED 130 Amber Roadway 130 $27.29 44"
    _FIXTURE_CONSOLIDATED_RE = re.compile(
        r"^(LED\s+\d+(?:\s+(?![\d.]+\s)[\w\s]+?)?)\s+\d+\s+\$?([\d]+\.[\d]+)\s+\d+\s*$",
        re.I,
    )
    # Legacy lumen fixtures: "20,000 lumen (metal halide) 2 23.19 94"
    # Note: (?:\d\s+)? matches an optional footnote digit (e.g. "3" in "40,000 lumen (metal halide)3 24.67 160")
    # when followed by whitespace. Using \d? previously consumed the leading digit of 2-digit rates.
    _LUMEN_CONSOLIDATED_RE = re.compile(
        r"^([\d,]+\s+lumen[^$\n]{0,60}?)\s+(?:\d\s+)?\$?\s*([\d]+\.[\d]+)\s+\d+\s*$",
        re.I,
    )
    # pdfplumber SLR format: rates appear as standalone values after density+lumen labels
    # SLR consolidated: "7,000 lumen mercury vapor  $1.61" or after density context
    _SLR_DENSITY_RATE_RE = re.compile(
        r"(7,000\s+lumen[^$\n]{0,50}?|9,500\s+lumen[^$\n]{0,50}?|LED\s+50\s+light\s+emitting\s+diode[^$\n]{0,20}?)\s+\$?([\d]+\.[\d]+)\s*$",
        re.I,
    )
    # Fallback for line-based (fitz) extraction
    _LABEL_RE = re.compile(
        r"^(LED\s+\d+(?:\s+\S.*)?|[\d,]+\s+lumen\b.*)$",
        re.I,
    )
    _RATE_LINE_RE = re.compile(r"^\$?\s*([\d]+\.[\d]+)\s*$")
    _PURE_INT_RE = re.compile(r"^\d+$")
    # SLR label: density+lumen combinations
    _SLR_LABEL_RE = re.compile(
        r"^(?:7,000\s+lumen|9,500\s+lumen|LED\s+50\s+light\s+emitting\s+diode)",
        re.I,
    )
    _SLR_DENSITY_RE = re.compile(
        r"^(\d+\s+light(?:\s+per\s+\d+\s+customers?[^:]*)?)",
        re.I,
    )
    _MONTHLY_RATE_HEADER_RE = re.compile(r"MONTHLY\s+RATE", re.I)
    _SECTION_END_RE = re.compile(
        r"^(RIDERS|CONTRACT PERIOD|SERVICE|AVAILABILITY|ANNEXATION|NONREFUNDABLE|SALES TAX|STORM)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "monthly rate" in lowered and (
            "per fixture" in lowered or "per customer" in lowered
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        family_key = (doc.get("family_key") or "").lower()
        score = 0.88
        lowered = text.lower()
        if family_key == "nc-progress-leaf-571" and "schedule sls" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-572" and "schedule slr" in lowered:
            score += 0.05
        elif family_key == "nc-progress-leaf-570" and "schedule als" in lowered:
            score += 0.05
        if "led" in lowered:
            score += 0.02
        return min(score, 0.96)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = (doc.get("family_key") or "").lower()
        if family_key == "nc-progress-leaf-572":
            # Try consolidated (pdfplumber) format first, fall back to line-based (fitz)
            charges = self._extract_slr_consolidated(text)
            if not charges:
                charges = self._extract_slr(text)
            return charges
        # Try consolidated (pdfplumber) format first, fall back to line-based (fitz)
        charges = self._extract_fixture_consolidated(text)
        if not charges:
            charges = self._extract_fixture_table(text)
        return charges

    def _extract_fixture_consolidated(self, text: str) -> list[ExtractedCharge]:
        """Extract from pdfplumber consolidated format: 'LED 30 30 $7.45 10' per line."""
        charges: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        in_rate_section = False
        for line in text.split("\n"):
            stripped = line.strip()
            if not in_rate_section:
                if self._MONTHLY_RATE_HEADER_RE.search(stripped):
                    in_rate_section = True
                continue
            # Stop at non-rate sections (but not "MONTHLY RATE for Masterpiece..." which is still rates)
            if self._SECTION_END_RE.match(stripped):
                in_rate_section = False
                continue

            m = self._FIXTURE_CONSOLIDATED_RE.match(stripped)
            if not m:
                m2 = self._LUMEN_CONSOLIDATED_RE.match(stripped)
                if m2:
                    label = re.sub(r"\s+", " ", m2.group(1)).strip().rstrip("*123456789 ").strip()
                    rate = float(m2.group(2))
                    if label and 0 < rate < 500:
                        key = (label.lower(), rate)
                        if key not in seen:
                            seen.add(key)
                            charges.append(ExtractedCharge(
                                charge_type="fixed",
                                charge_label=f"Street Lighting Monthly Charge - {label}",
                                rate_value=rate,
                                rate_unit="$/fixture/month",
                                season="all_year",
                                tou_period=None,
                                tier_min=None,
                                tier_max=None,
                                source_snippet=stripped[:100],
                                confidence_score=0.95,
            ))
                continue
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            rate = float(m.group(2))
            if not label or rate <= 0 or rate > 500:
                continue
            key = (label.lower(), rate)
            if key in seen:
                continue
            seen.add(key)
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label=f"Street Lighting Monthly Charge - {label}",
                rate_value=rate,
                rate_unit="$/fixture/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet=stripped[:100],
                confidence_score=0.95,
            ))
        return charges

    def _extract_fixture_table(self, text: str) -> list[ExtractedCharge]:
        """Line-sequence extraction for leaf-570/571 LED/legacy fixture tables.

        The column sequence per fixture row is:
        label line → wattage line (pure int) → $rate line → kWh line (pure int)
        """
        charges: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        lines = text.split("\n")
        in_rate_section = False
        current_label: str | None = None

        for line in lines:
            stripped = line.strip()
            if not in_rate_section:
                if self._MONTHLY_RATE_HEADER_RE.search(stripped):
                    in_rate_section = True
                continue
            if self._SECTION_END_RE.match(stripped) and stripped != "MONTHLY RATE":
                # Keep going — there are multiple sections per schedule
                current_label = None
                continue

            # Check for a fixture label line
            label_match = self._LABEL_RE.match(stripped)
            if label_match:
                current_label = re.sub(r"\s+", " ", stripped).rstrip("*123456789 ").strip()
                continue

            # Skip pure-integer lines (wattage or kWh column)
            if self._PURE_INT_RE.match(stripped):
                continue

            # Check for a rate line
            rate_match = self._RATE_LINE_RE.match(stripped)
            if rate_match and current_label:
                rate = float(rate_match.group(1))
                if 0 < rate < 500:
                    key = (current_label.lower(), rate)
                    if key not in seen:
                        seen.add(key)
                        charges.append(ExtractedCharge(
                            charge_type="fixed",
                            charge_label=f"Street Lighting Monthly Charge - {current_label}",
                            rate_value=rate,
                            rate_unit="$/fixture/month",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=f"{current_label} ${rate}",
                            confidence_score=0.95,
            ))
                # Don't reset label — next rate may follow immediately (no label repeat)

        return charges

    def _extract_slr_consolidated(self, text: str) -> list[ExtractedCharge]:
        """Extract from pdfplumber consolidated SLR format.

        Lines like:
          '7,000 lumen mercury vapor1 or 9,500 lumen sodium vapor $1.61'
          'LED 50 light emitting diode 1.23'
        preceded by density context lines like:
          '1 light per 10 customers or major fraction thereof:'
        """
        charges: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        in_rate_section = False
        current_density = ""

        for line in text.split("\n"):
            stripped = line.strip()
            if not in_rate_section:
                if self._MONTHLY_RATE_HEADER_RE.search(stripped):
                    in_rate_section = True
                continue
            if self._SECTION_END_RE.match(stripped):
                in_rate_section = False
                current_density = ""
                continue

            density_m = self._SLR_DENSITY_RE.match(stripped)
            if density_m:
                current_density = stripped.rstrip(":").strip()
                continue

            rate_m = self._SLR_DENSITY_RATE_RE.search(stripped)
            if rate_m:
                lumen_label = re.sub(r"(\w)\d\b", r"\1", rate_m.group(1)).strip()
                rate = float(rate_m.group(2))
                if 0 < rate < 100:
                    full_label = f"{current_density} - {lumen_label}" if current_density else lumen_label
                    key = (full_label.lower()[:80], rate)
                    if key not in seen:
                        seen.add(key)
                        charges.append(ExtractedCharge(
                            charge_type="fixed",
                            charge_label=f"SLR Monthly Charge - {full_label}",
                            rate_value=rate,
                            rate_unit="$/customer/month",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=stripped[:100],
                            confidence_score=0.95,
            ))

        return charges

    def _extract_slr(self, text: str) -> list[ExtractedCharge]:
        """Line-sequence extraction for leaf-572 SLR per-customer table.

        Pattern per row:
            "7,000 lumen mercury vapor..." or "LED 50 light emitting diode"  — label
            "$1.61" or "1.23"                                                  — rate
        """
        charges: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        lines = text.split("\n")
        in_rate_section = False
        current_label: str | None = None
        current_density: str = ""

        for line in lines:
            stripped = line.strip()
            if not in_rate_section:
                if self._MONTHLY_RATE_HEADER_RE.search(stripped):
                    in_rate_section = True
                continue
            if self._SECTION_END_RE.match(stripped):
                in_rate_section = False
                current_label = None
                current_density = ""
                continue

            # Track density context (e.g. "1 light per 10 customers...")
            density_match = self._SLR_DENSITY_RE.match(stripped)
            if density_match:
                current_density = stripped.rstrip(":").strip()
                current_label = None
                continue

            if self._SLR_LABEL_RE.match(stripped):
                # Strip inline footnote digits (e.g. "mercury vapor1")
                lumen_type = re.sub(r"(\w)\d\b", r"\1", stripped).strip()
                current_label = f"{current_density} - {lumen_type}" if current_density else lumen_type
                continue

            rate_match = self._RATE_LINE_RE.match(stripped)
            if rate_match and current_label:
                rate = float(rate_match.group(1))
                if 0 < rate < 100:
                    key = (current_label.lower(), rate)
                    if key not in seen:
                        seen.add(key)
                        charges.append(ExtractedCharge(
                            charge_type="fixed",
                            charge_label=f"SLR Monthly Charge - {current_label}",
                            rate_value=rate,
                            rate_unit="$/customer/month",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=f"{current_label} ${rate}",
                            confidence_score=0.95,
            ))
                current_label = None  # rate consumed; wait for next label

        return charges


@dataclass
class ProgressTrafficSignalServiceProfile:
    """Profile for DEP Schedule TSS (Leaf 574) — Traffic Signal Service.

    Extracts per-signal monthly rates from the 4-column watt/hour table.
    pdfplumber consolidates each row as:
      "Blinker Signal with One Lamp.............. $ 2.25 / 19 $3.03/ 28 $4.06 / 33 $5.68 / 49"
    Columns: [16hr/70W, 24hr/70W, 16hr/150W, 24hr/150W]
    """

    name: str = "progress_traffic_signal_service"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-574"}

    # Row pattern: signal label, then 4 price/kWh pairs
    # Each pair: $X.XX / YY  (with optional spaces around $, /)
    _ROW_RE = re.compile(
        r"^(.+?)\s*[\.\s]{3,}\s*"                  # signal label (dots or spaces as filler)
        r"\$?\s*([\d.]+)\s*/\s*\d+"                 # 16hr/70W price (kWh ignored)
        r"\s+\$?\s*([\d.]+)\s*/\s*\d+"              # 24hr/70W
        r"\s+\$?\s*([\d.]+)\s*/\s*\d+"              # 16hr/150W
        r"\s+\$?\s*([\d.]+)\s*/\s*\d+",             # 24hr/150W
        re.I,
    )
    # Simpler fallback: label + any 4 dollar values
    _ROW_FALLBACK_RE = re.compile(
        r"^(.+?)\s*[\.\s]{2,}\s*\$?\s*([\d.]+)\s+\$?\s*([\d.]+)\s+\$?\s*([\d.]+)\s+\$?\s*([\d.]+)",
        re.I,
    )

    _SKIP_WORDS = {"type of signal", "operating", "hours/kwh", "multi-direction", "the rate", "used as"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "schedule tss" in lowered and "traffic signal" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        has_table = "monthly rate per signal" in lowered or "blinker" in lowered
        return 0.93 if has_table else 0.80

    # Prefix-only lines that carry the signal type context for the next data row
    _PREFIX_RE = re.compile(r"^(Blinker Signal with|One-way Signal with)\s*$", re.I)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        col_labels = [
            ("16hr_70w", "16 Hours/70W"),
            ("24hr_70w", "24 Hours/70W"),
            ("16hr_150w", "16 Hours/150W"),
            ("24hr_150w", "24 Hours/150W"),
        ]
        lines = text.splitlines()
        pending_prefix: str | None = None
        for line in lines:
            line = line.strip()
            if not line:
                pending_prefix = None
                continue
            low = line.lower()
            # Capture prefix lines ("Blinker Signal with", "One-way Signal with")
            pm = self._PREFIX_RE.match(line)
            if pm:
                pending_prefix = pm.group(1)
                continue
            # Skip header/footer lines
            if any(sw in low for sw in self._SKIP_WORDS):
                pending_prefix = None
                continue
            m = self._ROW_RE.match(line) or self._ROW_FALLBACK_RE.match(line)
            if not m:
                pending_prefix = None
                continue
            raw_label = m.group(1).rstrip(". ").strip()
            if len(raw_label) < 5 or raw_label[0].isdigit():
                pending_prefix = None
                continue
            # Prepend signal type prefix if available
            if pending_prefix:
                label = f"{pending_prefix} {raw_label}"
                pending_prefix = None
            else:
                label = raw_label
            for i, (col_key, col_name) in enumerate(col_labels):
                val_str = m.group(i + 2)
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                if val <= 0:
                    continue
                charges.append(
                    ExtractedCharge(
                        charge_type="fixed",
                        charge_label=f"{label} ({col_name})",
                        rate_value=val,
                        rate_unit="$/signal/month",
                        season="all_year",
                        tou_period=col_key,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=line[:120],
                        confidence_score=0.88,
                    )
                )
        return charges


@dataclass
class ProgressFluctuatingLoadRiderProfile:
    """Profile for DEP Rider No. 9 (Leaf 650) — Highly Fluctuating or Intermittent Load.

    Extracts the fixed $/kVa demand supplement charge.
    """

    name: str = "progress_fluctuating_load_rider"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-650"}
    _KVA_RATE_RE = re.compile(r"\$([\d.]+)\s+per\s+kVa\b", re.I)

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "highly fluctuating or intermittent load" in lowered and "rider no. 9" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        return 0.93 if self._KVA_RATE_RE.search(text) else 0.80

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        match = self._KVA_RATE_RE.search(text)
        if not match:
            return []
        return [
            ExtractedCharge(
                charge_type="demand",
                charge_label="Highly Fluctuating Load Supplement",
                rate_value=float(match.group(1)),
                rate_unit="$/kVa",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet=match.group(0)[:100],
                confidence_score=0.93,
            )
        ]


@dataclass
class ProgressCustomerAssistanceRecoveryProfile:
    """Profile for DEP Rider CAR sheets with residential and general-service adjustments."""

    name: str = "progress_customer_assistance_recovery"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-611"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return (
            "customer assistance recovery rider" in lowered
            and "monthly rate" in lowered
            and ("$/bill" in lowered or "$/kwh" in lowered)
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "rate class" in lowered:
            score += 0.03
        if "general service" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-leaf-611"
        path = Path(doc.get("local_path") or "")
        if doc.get("start_page") is not None and doc.get("end_page") is not None:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(path, version_id=0, family_key=family_key)
        else:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        return _convert_progress_tariff_charges(charges)


@dataclass
class ProgressStormSecuritizationProfile:
    """Profile for DEP Rider STS sheets with per-class storm recovery rates."""

    name: str = "progress_storm_securitization"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-613", "nc-progress-leaf-607"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return (
            "storm securitization" in lowered
            and "monthly rate" in lowered
            and ("billing rate" in lowered or "¢/kwh" in lowered or "c/kwh" in lowered)
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "rate class" in lowered:
            score += 0.03
        if "applicable schedules" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-leaf-613"
        path = Path(doc.get("local_path") or "")
        if path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(path, version_id=0, family_key=family_key)
        else:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        return _convert_progress_tariff_charges(charges)


@dataclass
class ProgressGreenPowerProgramProfile:
    """Profile for DEP NC GreenPower rider sheets with a fixed per-block charge."""

    name: str = "progress_greenpower_program"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-642", "nc-progress-leaf-643"}
    _BLOCK_RATE_RE = re.compile(r"\$([\d.]+)\s+per\s+block", re.I)

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return ("greenpower program" in lowered or "renewable rider ren" in lowered) and "per block" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.9
        lowered = text.lower()
        if "monthly rate" in lowered:
            score += 0.03
        if "renewable resources" in lowered:
            score += 0.02
        if "renewable rider ren" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        if not (match := self._BLOCK_RATE_RE.search(text)):
            return []
        family_key = (doc.get("family_key") or "").lower()
        label = "Renewable Rider REN Block Charge" if family_key == "nc-progress-leaf-643" else "GreenPower Block Charge"
        return [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label=label,
                rate_value=float(match.group(1)),
                rate_unit="$/block",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="per block",
                confidence_score=0.92,
            )
        ]


@dataclass
class ProgressPowerPairPilotProfile:
    """Profile for PowerPair pilot incentive schedules (Leaf 770)."""

    name: str = "progress_powerpair_pilot"
    # Matches "$0.36/Watt-AC" or "$X.XX per Watt" formats
    _SOLAR_INCENTIVE_RE = re.compile(
        r"\$([\d.]+)\s*(?:/|per\s+)watt(?:-ac)?",
        re.I,
    )
    # Matches "$240/kWh" (single value) or "$200-$300 per kWh" (range) formats
    _BATTERY_INCENTIVE_SINGLE_RE = re.compile(
        r"\$([\d.]+)\s*/\s*(?:kilowatt[-\s]*hours?|kwh)",
        re.I,
    )
    _BATTERY_INCENTIVE_RANGE_RE = re.compile(
        r"\$([\d.]+)\s*(?:-|–|—|\$)\s*\$?([\d.]+)\s+per\s+(?:kilowatt[-\s]*hours?|kwh)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key != "nc-progress-leaf-770":
            return False
        lowered = text.lower()
        return "powerpair" in lowered and "incentive" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "solar and battery installation" in lowered:
            score += 0.03
        if "/watt" in lowered or "per watt" in lowered or "/kwh" in lowered or "per kilowatt hour" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._SOLAR_INCENTIVE_RE.search(text):
            charges.append(
                self._charge(
                    "incentive",
                    "PowerPair Solar Incentive",
                    float(match.group(1)),
                    "$/W",
                )
            )
        # Try single-value battery incentive first (e.g. "$240/kWh")
        if match := self._BATTERY_INCENTIVE_SINGLE_RE.search(text):
            charges.append(
                self._charge(
                    "incentive",
                    "PowerPair Battery Incentive",
                    float(match.group(1)),
                    "$/kWh",
                )
            )
        elif match := self._BATTERY_INCENTIVE_RANGE_RE.search(text):
            charges.append(
                self._charge(
                    "incentive",
                    "PowerPair Battery Incentive Minimum",
                    float(match.group(1)),
                    "$/kWh",
                )
            )
            charges.append(
                self._charge(
                    "incentive",
                    "PowerPair Battery Incentive Maximum",
                    float(match.group(2)),
                    "$/kWh",
                )
            )
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressDemandResponseAutomationProfile:
    """Profile for Demand Response Automation Rider DRA sheets (Leaf 717)."""

    name: str = "progress_demand_response_automation"
    _MONTHLY_AVAILABILITY_RE = re.compile(
        r"Monthly Availability Credit\s*=\s*\$([\d.]+)\s*/\s*kW",
        re.I,
    )
    _EVENT_PERFORMANCE_RE = re.compile(
        r"Event Performance Credit\s*=\s*\$([\d.]+)\s*/\s*kW",
        re.I,
    )
    _PARTICIPATION_INCENTIVE_RE = re.compile(
        r"Participant Incentive,\s+in the amount of \$([\d.]+)\s*/\s*kW",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key != "nc-progress-leaf-717":
            return False
        lowered = text.lower()
        return "rider dra" in lowered and (
            "monthly availability credit" in lowered
            or "event performance credit" in lowered
            or "participant incentive" in lowered
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "monthly availability credit" in lowered:
            score += 0.03
        if "event performance credit" in lowered:
            score += 0.03
        if "participant incentive" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._MONTHLY_AVAILABILITY_RE.search(text):
            charges.append(self._charge("credit", "Monthly Availability Credit", float(match.group(1)), "$/kW"))
        if match := self._EVENT_PERFORMANCE_RE.search(text):
            charges.append(self._charge("credit", "Event Performance Credit", float(match.group(1)), "$/kW"))
        if match := self._PARTICIPATION_INCENTIVE_RE.search(text):
            charges.append(self._charge("incentive", "Participant Incentive", float(match.group(1)), "$/kW"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressLoadControlWinterProfile:
    """Profile for DEP Residential Load Control (Asheville Area) Rider LC-WIN (Leaf 714).

    Extracts fixed bill credit incentives for load control participation.
    """

    name: str = "progress_load_control_winter"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-714"}
    _INITIAL_CREDIT_RE = re.compile(
        r"Initial One-Time Bill Credit of\s*\$([\d.]+)",
        re.I,
    )
    _ANNUAL_CREDIT_RE = re.compile(
        r"Annual Bill Credit of\s*\$([\d.]+)",
        re.I,
    )
    _REFERRAL_RE = re.compile(
        r"receive a\s*\$([\d.]+)\s+incentive for each new program participant",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider lc-win" in lowered or ("lc-win" in lowered and "load control" in lowered)

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "bill credit" in lowered:
            score += 0.05
        if "annual bill credit" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        seen_values: set[float] = set()
        for match in self._INITIAL_CREDIT_RE.finditer(text):
            val = float(match.group(1))
            if val not in seen_values:
                seen_values.add(val)
                charges.append(self._charge("credit", "Initial Bill Credit", val, "$/enrollment"))
        for match in self._ANNUAL_CREDIT_RE.finditer(text):
            val = float(match.group(1))
            label = "Annual Bill Credit"
            key = (label, val)
            if key not in {(c.charge_label, c.rate_value) for c in charges}:
                charges.append(self._charge("credit", label, val, "$/year"))
        if match := self._REFERRAL_RE.search(text):
            charges.append(self._charge("credit", "Referral Incentive", float(match.group(1)), "$/referral"))
        return charges

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.90,
        )


@dataclass
class ProgressIncomeQualifiedLoadControlProfile:
    """Profile for DEP Residential Income-Qualified Load Control Program RIQLC (Leaf 725).

    Extracts fixed bill credit / incentive amounts for load control device and thermostat options.
    """

    name: str = "progress_income_qualified_load_control"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-725"}
    # Leaf 725 table pattern: "Heat Strip Load Control Device Winter  $50  $40"
    _LEAF725_TABLE_RE = re.compile(
        r"Heat Strip Load Control Device Winter\s*\$([\d.]+)\s*\$([\d.]+)",
        re.I,
    )
    _LEAF725_THERM_RE = re.compile(
        r"Thermostat Internet Connected Winter Focused\s*\$([\d.]+)\s*\$([\d.]+)",
        re.I,
    )
    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "income-qualified" in lowered and "load control" in lowered and (
            "riqlc" in lowered or "load control device" in lowered
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "payment of incentives" in lowered:
            score += 0.05
        if "initial incentive" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        # Leaf 725 structured table
        if match := self._LEAF725_TABLE_RE.search(text):
            charges.append(self._charge("credit", "LCD Winter - Initial Incentive", float(match.group(1)), "$/enrollment"))
            charges.append(self._charge("credit", "LCD Winter - Annual Incentive", float(match.group(2)), "$/year"))
        if match := self._LEAF725_THERM_RE.search(text):
            charges.append(self._charge("credit", "Thermostat Winter - Initial Incentive", float(match.group(1)), "$/enrollment"))
            charges.append(self._charge("credit", "Thermostat Winter - Annual Incentive", float(match.group(2)), "$/year"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.90,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (charge.charge_type, charge.charge_label, round(float(charge.rate_value), 6), charge.rate_unit or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressBillingAdjustmentsProfile:
    """Profile for Progress Rider BA billing adjustment tables (Leaf 601)."""

    name: str = "progress_billing_adjustments"
    _NOTICE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(
                r"Residential(?:\s+rates?)?\s*(?:[–-]|will)?\s*(?:an?\s+)?"
                r"(increase|decrease)(?:\s+of)?\s+\(?([0-9]*\.[0-9]+)\)?\s+cents?\s+per\s+kilowatt(?:-|\s)?hour",
                re.I,
            ),
            "Billing Adjustment Notice - Residential",
        ),
        (
            re.compile(
                r"Small,\s*Medium,\s*and\s*Large\s*General\s*Service\s*\(EE\s*component\)\s*"
                r"(?:[–-]|will)?\s*(?:an?\s+)?(increase|decrease)(?:\s+of)?\s+\(?([0-9]*\.[0-9]+)\)?\s+cents?\s+per\s+kwh",
                re.I,
            ),
            "Billing Adjustment Notice - General Service EE",
        ),
        (
            re.compile(
                r"Small,\s*Medium,\s*and\s*Large\s*General\s*Service\s*\(DSM\s*component\)\s*"
                r"(?:[–-]|will)?\s*(?:an?\s+)?(increase|decrease)(?:\s+of)?\s+\(?([0-9]*\.[0-9]+)\)?\s+cents?\s+per\s+kwh",
                re.I,
            ),
            "Billing Adjustment Notice - General Service DSM",
        ),
        (
            re.compile(
                r"Lighting(?:\s+rates?)?\s*(?:[–-]|will)?\s*(?:an?\s+)?"
                r"(increase|decrease)(?:\s+of)?\s+\(?([0-9]*\.[0-9]+)\)?\s+cents?\s+per\s+kwh",
                re.I,
            ),
            "Billing Adjustment Notice - Lighting",
        ),
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key != "nc-progress-leaf-601":
            return False
        lowered = text.lower()
        has_leaf_table = "billing adjustment factors" in lowered and "rider ba" in lowered
        has_notice_rates = "annual billing adjustments rider ba" in lowered and (
            "the net changes in the dsm and ee rates" in lowered
            or "the rate changes associated with dep" in lowered
        )
        return has_leaf_table or has_notice_rates

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.9
        lowered = text.lower()
        if "net adjustment" in lowered:
            score += 0.03
        if "applicable to schedules" in lowered:
            score += 0.03
        if "the net changes in the dsm and ee rates" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-leaf-601"
        lowered = text.lower()

        # Detect notice-style narrative text: these are prose paragraphs describing
        # rate changes, not the billing adjustment factors table. The leaf parser may
        # partially match incidental numbers — skip it for notice text and go straight
        # to the dedicated notice extractor.
        is_notice = "annual billing adjustments rider ba" in lowered and (
            "the net changes in the dsm and ee rates" in lowered
            or "the rate changes associated with dep" in lowered
        )
        if is_notice:
            return self._extract_notice_adjustments(text)

        path = Path(doc.get("local_path") or "")
        if path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(
                path,
                version_id=0,
                family_key=family_key,
            )
        else:
            _, charges, _ = parse_nc_progress_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )

        extracted: list[ExtractedCharge] = []
        for charge in charges:
            if charge.charge_type != "adjustment":
                continue
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or (
                        f"Rider Adjustment - {charge.customer_class}" if charge.customer_class else "Rider Adjustment"
                    ),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted or self._extract_notice_adjustments(text)

    @classmethod
    def _extract_notice_adjustments(cls, text: str) -> list[ExtractedCharge]:
        extracted: list[ExtractedCharge] = []
        for pattern, label in cls._NOTICE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            direction = match.group(1).lower()
            raw_value = match.group(2)
            rate_value = cls._parse_notice_rate(direction, raw_value)
            extracted.append(
                ExtractedCharge(
                    charge_type="adjustment",
                    charge_label=label,
                    rate_value=rate_value,
                    rate_unit="$/kWh",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=match.group(0)[:100],
                    confidence_score=0.86,
                )
            )
        return extracted

    @staticmethod
    def _parse_notice_rate(direction: str, raw_value: str) -> float:
        normalized = raw_value.strip()
        if normalized.startswith("."):
            normalized = f"0{normalized}"
        value = float(normalized)
        if direction == "decrease":
            value *= -1.0
        return value / 100.0


@dataclass
class ProgressSingleValueRiderProfile:
    """Profile for single-value Progress riders like RDM, ESM, PIM, JAA, STS, CPRE, RECD."""

    name: str = "progress_single_value_rider"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-602",   # JAA — Joint Agency Asset
        "nc-progress-leaf-603",   # REPS — Renewable Energy Portfolio Standard
        "nc-progress-leaf-604",   # ED  — Economic Development
        "nc-progress-leaf-605",   # CPRE — Competitive Procurement Renewable Energy
        "nc-progress-leaf-606",   # DSM — Demand Side Management
        "nc-progress-leaf-607",   # STS — Storm Securitization
        "nc-progress-leaf-608",   # RDM — Revenue Decoupling Mechanism
        "nc-progress-leaf-609",   # ESM — Energy Star Multi-Family
        "nc-progress-leaf-610",   # PIM — Performance Incentive Mechanism
        "nc-progress-leaf-611",   # CAR — Customer Assistance Recovery
        "nc-progress-leaf-640",   # RECD — Residential Energy Conservation Discount
        "nc-progress-leaf-641",   # NM  — Net Metering
        "nc-progress-leaf-590",   # PP — Purchased Power Schedule
        "nc-progress-leaf-591",   # PP variant
        "nc-progress-leaf-592",   # PPBE — Purchased Power Blend and Extend
        "nc-progress-leaf-646",   # rider
        "nc-progress-leaf-647",   # rider
        "nc-progress-leaf-648",   # rider
        "nc-progress-leaf-649",   # rider
        "nc-progress-leaf-651",   # rider
        "nc-progress-leaf-652",   # rider
        "nc-progress-leaf-655",   # rider
        "nc-progress-leaf-656",   # rider
        "nc-progress-leaf-657",   # rider
        "nc-progress-leaf-662",   # rider
        "nc-progress-leaf-700",   # rider
        "nc-progress-leaf-702",   # SSP — Non-Residential Smart $aver
        "nc-progress-leaf-705",   # rider
        "nc-progress-leaf-708",   # RNC — Residential New Construction
        "nc-progress-leaf-719",   # rider
        "nc-progress-leaf-722",   # rider
        "nc-progress-leaf-724",   # rider
        "nc-progress-leaf-664",   # SSR — Shared Solar Rider
        # DEC (Carolinas) equivalent riders — same format as Progress single-value riders
        "nc-carolinas-rider-rdm",   # RDM — Revenue Decoupling Mechanism
        "nc-carolinas-rider-pim",   # PIM — Performance Incentive Mechanism
        "nc-carolinas-rider-edit4", # EDIT4 — Excess Deferred Income Tax
        "nc-carolinas-rider-sts",   # STS — Storm Securitization (multi-class table)
        # NOTE: nc-carolinas-rider-cei intentionally excluded — CEI rate is market-based
        # (annual CEEA price set by solar REC market), not a fixed extractable $/kWh value.
        # NOTE: nc-progress-leaf-641 (NM) and nc-progress-leaf-664 (SSR) are in _SUPPORTED_FAMILIES
        # but correctly extract 0 charges: NM is a billing credit mechanism (no fixed rate),
        # SSR rate is site-specific per contract (not a tariff-filed fixed value).
    }
    _RELAXED_SELECTION_FAMILIES = {
        "nc-progress-leaf-602",   # JAA — some valid spans omit explicit kWh wording
        "nc-progress-leaf-640",   # RECD — uses % credit, not ¢/kWh; needs relaxed kwh gate
        # Purchased Power schedules — use price schedules not traditional rate tables
        "nc-progress-leaf-590",   # PP — Purchased Power Schedule
        "nc-progress-leaf-591",   # PP — Terms & Conditions (legal text, no rates)
        "nc-progress-leaf-592",   # PPBE — Purchased Power Blend and Extend
        # Riders with rate data but using different terminology (no explicit kWh marker)
        "nc-progress-leaf-646",   # Rider CM (terms-only, no rates)
        "nc-progress-leaf-647",   # Rider 28 (terms-only, no rates)
        "nc-progress-leaf-648",   # Rider TR
        "nc-progress-leaf-649",   # Rider US
        "nc-progress-leaf-651",   # Rider 7
        "nc-progress-leaf-652",   # Rider 57
        "nc-progress-leaf-656",   # Rider 68 — Dispatched Power
        "nc-progress-leaf-657",   # Rider IPS — Interruptible Power Service
        "nc-progress-leaf-662",   # Rider EPPWP — Enhanced Power Purchased Power
        # Energy efficiency / DSM programs
        "nc-progress-leaf-700",   # Program NSSEE
        "nc-progress-leaf-702",   # SSP — Non-Residential Smart $aver
        "nc-progress-leaf-724",   # YFB — Your Fixed Bill
    }

    @staticmethod
    def _has_kwh_rate_marker(lowered: str) -> bool:
        return bool(
            re.search(r"per\s+kilowatt-?hour", lowered)
            or "per kwh" in lowered
            or "cents per kilowatt" in lowered
            or "/kwh" in lowered
        )

    @staticmethod
    def _has_recd_marker(lowered: str) -> bool:
        """True only for genuine RECD (Residential Energy Conservation Discount) documents.

        Requires explicit RECD-specific language to prevent CPRE hearing orders
        (which share the leaf-640 number in some dockets) from matching this profile.
        """
        return (
            "energy conservation discount" in lowered
            or "recd credit" in lowered
            or "rider recd" in lowered
        )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()

        # leaf-640 (RECD) requires explicit RECD-specific content to distinguish it
        # from misassigned CPRE hearing orders that reference the same leaf number.
        if family_key == "nc-progress-leaf-640":
            return self._has_recd_marker(lowered)

        has_rate = (
            "monthly rate" in lowered
            or "rider" in lowered
        )
        has_kwh = self._has_kwh_rate_marker(lowered)
        has_leaf = "leaf no." in lowered or "rider " in lowered
        if family_key in self._RELAXED_SELECTION_FAMILIES:
            return has_rate and (has_kwh or has_leaf)
        return has_rate and has_kwh and has_leaf

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.88
        lowered = text.lower()
        if "approved decremental rate" in lowered or "approved incremental rate" in lowered:
            score += 0.05
        if "rider " in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        _, charges, _ = parse_nc_progress_leaf(
            text,
            version_id=0,
            family_key=doc.get("family_key") or "nc-progress-leaf-609",
        )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            if charge.charge_type != "adjustment":
                continue
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or "Rider Adjustment",
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class ProgressRecoveryRiderProfile:
    """Profile for the high-volume DEP Recovery Rider family."""

    name: str = "progress_recovery_rider"
    _SUPPORTED_FAMILIES = {"nc-progress-rider-recoveryrider"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        title = (doc.get("title") or "").lower()
        has_recovery_signal = "recovery rider" in lowered or "recovery rider" in title
        has_rate_signal = "monthly rate" in lowered or "cost recovery" in lowered
        return has_recovery_signal and has_rate_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "cost recovery" in lowered:
            score += 0.03
        if "monthly rate" in lowered:
            score += 0.03
        if "applicability" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-rider-RECOVERYRIDER"
        path = Path(doc.get("local_path") or "")
        if doc.get("start_page") is not None and doc.get("end_page") is not None:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(path, version_id=0, family_key=family_key)
        else:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        return _convert_progress_tariff_charges(charges)


@dataclass
class ProgressManagementEnergyEfficiencyCostRecoveryRiderProfile:
    """Profile for the DEP management and energy-efficiency cost recovery rider."""

    name: str = "progress_management_energy_efficiency_cost_recovery_rider"
    _SUPPORTED_FAMILIES = {"nc-progress-rider-managementandenergyefficiencycostrecoveryrider"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        title = (doc.get("title") or "").lower()
        has_rider_signal = "management and energy efficiency cost recovery rider" in lowered or "management and energy efficiency cost recovery rider" in title
        has_rate_signal = "monthly rate" in lowered or "cost recovery" in lowered
        return has_rider_signal and has_rate_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "cost recovery" in lowered:
            score += 0.03
        if "monthly rate" in lowered:
            score += 0.03
        if "applicability" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER"
        path = Path(doc.get("local_path") or "")
        if doc.get("start_page") is not None and doc.get("end_page") is not None:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(path, version_id=0, family_key=family_key)
        else:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        return _convert_progress_tariff_charges(charges)


@dataclass
class ProgressComplianceReportAndCostRecoveryRiderProfile:
    """Profile for the DEP compliance report and cost recovery rider."""

    name: str = "progress_compliance_report_and_cost_recovery_rider"
    _SUPPORTED_FAMILIES = {"nc-progress-rider-compliancereportandcostrecoveryrider"}

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        title = (doc.get("title") or "").lower()
        has_rider_signal = "compliance report and cost recovery rider" in lowered or "compliance report and cost recovery rider" in title
        has_rate_signal = "monthly rate" in lowered or "cost recovery" in lowered
        return has_rider_signal and has_rate_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "cost recovery" in lowered:
            score += 0.03
        if "monthly rate" in lowered:
            score += 0.03
        if "applicability" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER"
        path = Path(doc.get("local_path") or "")
        if doc.get("start_page") is not None and doc.get("end_page") is not None:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_progress_leaf_file(path, version_id=0, family_key=family_key)
        else:
            _, charges, _ = parse_nc_progress_leaf(text, version_id=0, family_key=family_key)
        return _convert_progress_tariff_charges(charges)


@dataclass
class ProgressRiderAdjustmentMatrixProfile:
    """Profile for Progress Leaf 600-style rider adjustment summary tables."""

    name: str = "progress_rider_adjustment_matrix"

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key != "nc-progress-leaf-600":
            return False
        lowered = text.lower()
        if "summary of rider adjustments" not in lowered and "rider adjustments" not in lowered:
            return False
        # Require actual table content — numeric rate values in decimal format (e.g., 0.631)
        # to avoid false matches when the full PDF text includes a rider summary from another page.
        return bool(re.search(r"\b\d+\.\d{3,}\b", text))

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        return 0.96

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        leaf_no = None
        family_key = (doc.get("family_key") or "").lower()
        leaf_match = re.search(r"leaf-(\d+)", family_key)
        if leaf_match:
            leaf_no = leaf_match.group(1)

        # Prefer coordinate-aware PDF extraction (handles multi-column, bold, indent)
        # Fall back to text-based parser if PDF path is unavailable or unreadable
        local_path = doc.get("local_path", "") or ""
        pdf_path = None
        if local_path:
            from pathlib import Path
            p = Path(local_path)
            if p.exists():
                pdf_path = str(p)

        if pdf_path:
            try:
                summary = parse_rider_summary_from_pdf(pdf_path, leaf_no=leaf_no)
            except Exception:
                summary = parse_rider_summary(text, source_pdf=local_path, leaf_no=leaf_no)
        else:
            summary = parse_rider_summary(text, source_pdf=local_path, leaf_no=leaf_no)

        charges: list[ExtractedCharge] = []
        for block in summary.rate_classes:
            for item in block.line_items:
                if item.is_section_header:
                    continue
                # Preserve rider-coded subtotals like BA Net Adjustment as the
                # rider-level rollup, but still skip unlabeled subtotal residue.
                if item.is_subtotal and not item.rider_code:
                    continue
                # Skip TOTAL rows — already captured as adjustment_total via block totals.
                if item.is_total:
                    continue
                if item.cents_per_kwh is not None:
                    charges.append(
                        ExtractedCharge(
                            charge_type="adjustment",
                            charge_label=f"{block.rate_class} - {item.rider_code or item.label}",
                            rate_value=item.cents_per_kwh / 100.0,
                            rate_unit="$/kWh",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=item.label[:100],
                            confidence_score=0.95 if item.rider_code else 0.9,
                        )
                    )
                if item.dollars_per_kw is not None:
                    charges.append(
                        ExtractedCharge(
                            charge_type="adjustment",
                            charge_label=f"{block.rate_class} - {item.rider_code or item.label}",
                            rate_value=item.dollars_per_kw,
                            rate_unit="$/kW",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=item.label[:100],
                            confidence_score=0.95 if item.rider_code else 0.9,
                        )
                    )

            if block.total_cents_per_kwh is not None:
                charges.append(
                    ExtractedCharge(
                        charge_type="adjustment_total",
                        charge_label=f"{block.rate_class} Total Rider Adjustments",
                        rate_value=block.total_cents_per_kwh / 100.0,
                        rate_unit="$/kWh",
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=f"{block.rate_class} total cents/kWh",
                        confidence_score=0.97,
                    )
                )
            if block.total_dollars_per_kw is not None:
                charges.append(
                    ExtractedCharge(
                        charge_type="adjustment_total",
                        charge_label=f"{block.rate_class} Total Rider Adjustments",
                        rate_value=block.total_dollars_per_kw,
                        rate_unit="$/kW",
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=f"{block.rate_class} total dollars/kW",
                        confidence_score=0.97,
                    )
                )

        return charges


@dataclass
class CarolinasRiderAdjustmentMatrixProfile:
    """Profile for Carolinas/Duke Power rider summary tables (leaf-99 style)."""

    name: str = "carolinas_rider_adjustment_matrix"
    _RATE_CLASS_HEADING_RE = re.compile(
        r"^(Schedule(?:s)?\s+.+|Residential.+|General Service.+|Lighting Schedules.+)$",
        re.I,
    )
    _VALUE_RE = re.compile(r"^-?\d+\.\d+$")

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        company = (doc.get("company") or "").lower()
        lowered = text.lower()
        if "summary of rider adjustments" not in lowered:
            return False
        # Reject Progress families — they have their own adjustment matrix profile
        if family_key.startswith("nc-progress-"):
            return False
        # Require actual rate-class table content to avoid false matches from span
        # documents where the full PDF text includes a rider summary on another page.
        # A genuine rider summary table has a rate-class section header AND numeric
        # rate values in the ¢/kWh decimal format (e.g., 0.631, -0.249).
        has_rate_class = bool(re.search(
            r"(?:residential|general service|lighting|industrial|schedule|commercial)"
            r"\s+(?:service\s+)?schedules?",
            lowered,
        ))
        has_rate_values = bool(re.search(r"\b\d+\.\d{3,}\b", text))
        if not (has_rate_class and has_rate_values):
            return False
        return (
            company == "carolinas"
            or family_key in {"nc-carolinas-rider-summary", "nc-carolinas-leaf-99"}
            or family_key.startswith("nc-carolinas-")
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.9
        family_key = (doc.get("family_key") or "").lower()
        if family_key in {"nc-carolinas-rider-summary", "nc-carolinas-leaf-99"}:
            score += 0.06
        if "leaf no. 99" in text.lower() or "leaf no 99" in text.lower():
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        leaf_no = None
        family_key = (doc.get("family_key") or "").lower()
        leaf_match = re.search(r"leaf-(\d+)", family_key)
        if leaf_match:
            leaf_no = leaf_match.group(1)
        elif "leaf no. 99" in text.lower() or "leaf no 99" in text.lower():
            leaf_no = "99"

        summary = parse_rider_summary(
            text,
            source_pdf=doc.get("local_path", "") or "",
            leaf_no=leaf_no,
        )

        charges: list[ExtractedCharge] = []
        for block in summary.rate_classes:
            for item in block.line_items:
                if item.is_section_header:
                    continue
                # Skip BA Net Adjustment subtotals — BA sub-items (Fuel/EMF/DSM/EE)
                # already capture the detail; storing the subtotal too would double-count.
                if item.is_subtotal:
                    continue
                # Skip TOTAL rows — already captured as adjustment_total via block totals.
                if item.is_total:
                    continue
                if item.cents_per_kwh is not None:
                    charges.append(
                        ExtractedCharge(
                            charge_type="adjustment",
                            charge_label=f"{block.rate_class} - {item.rider_code or item.label}",
                            rate_value=item.cents_per_kwh / 100.0,
                            rate_unit="$/kWh",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=item.label[:100],
                            confidence_score=0.95 if item.rider_code else 0.9,
                        )
                    )
                if item.dollars_per_kw is not None:
                    charges.append(
                        ExtractedCharge(
                            charge_type="adjustment",
                            charge_label=f"{block.rate_class} - {item.rider_code or item.label}",
                            rate_value=item.dollars_per_kw,
                            rate_unit="$/kW",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=item.label[:100],
                            confidence_score=0.95 if item.rider_code else 0.9,
                        )
                    )

            if block.total_cents_per_kwh is not None:
                charges.append(
                    ExtractedCharge(
                        charge_type="adjustment_total",
                        charge_label=f"{block.rate_class} Total Rider Adjustments",
                        rate_value=block.total_cents_per_kwh / 100.0,
                        rate_unit="$/kWh",
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=f"{block.rate_class} total cents/kWh",
                        confidence_score=0.97,
                    )
                )
            if block.total_dollars_per_kw is not None:
                charges.append(
                    ExtractedCharge(
                        charge_type="adjustment_total",
                        charge_label=f"{block.rate_class} Total Rider Adjustments",
                        rate_value=block.total_dollars_per_kw,
                        rate_unit="$/kW",
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=f"{block.rate_class} total dollars/kW",
                        confidence_score=0.97,
                    )
                )
        if charges:
            return charges
        return self._extract_legacy_columnar_totals(text)

    def _extract_legacy_columnar_totals(self, text: str) -> list[ExtractedCharge]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        headings = self._extract_rate_class_headings(lines)
        if not headings:
            return []

        totals: list[ExtractedCharge] = []
        blocks = self._extract_value_blocks(lines)
        for index, block in enumerate(blocks):
            total_value = block.get("total_value")
            if total_value is None:
                continue
            heading = headings[index % len(headings)]
            qualifier = block.get("qualifier")
            label = f"{heading} {qualifier} Total Rider Adjustments".strip()
            totals.append(
                ExtractedCharge(
                    charge_type="adjustment_total",
                    charge_label=label,
                    rate_value=total_value / 100.0,
                    rate_unit="$/kWh",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=label[:100],
                    confidence_score=0.88,
                )
            )
        return totals

    def _extract_rate_class_headings(self, lines: list[str]) -> list[str]:
        headings: list[str] = []
        collecting = False
        for line in lines:
            lowered = line.lower()
            if "summary of rider adjustments" in lowered:
                collecting = True
                continue
            if not collecting:
                continue
            if self._looks_like_value_header(line):
                break
            if self._RATE_CLASS_HEADING_RE.match(line):
                headings.append(line)
        return headings

    def _extract_value_blocks(self, lines: list[str]) -> list[dict[str, object]]:
        blocks: list[dict[str, object]] = []
        index = 0
        while index < len(lines):
            if not self._looks_like_value_header(lines[index]):
                index += 1
                continue
            index += 1
            qualifier_parts: list[str] = []
            while index < len(lines):
                line = lines[index]
                if self._looks_like_value_header(line) or self._looks_like_effective_header(line):
                    break
                if self._VALUE_RE.match(line):
                    break
                qualifier_parts.append(line)
                index += 1
            values: list[float] = []
            while index < len(lines):
                line = lines[index]
                if self._looks_like_value_header(line) or self._looks_like_effective_header(line):
                    break
                if value := self._parse_numeric_value(line):
                    values.append(value)
                index += 1
            if values:
                blocks.append(
                    {
                        "qualifier": " ".join(qualifier_parts).strip(),
                        "total_value": values[-1],
                    }
                )
        return blocks

    @staticmethod
    def _looks_like_value_header(line: str) -> bool:
        lowered = line.lower().replace("\\", "/")
        return "cents/kwh" in lowered or "ccnts/k" in lowered

    @staticmethod
    def _looks_like_effective_header(line: str) -> bool:
        lowered = line.lower()
        return lowered == "effective" or lowered == "date" or lowered == "effective date"

    @staticmethod
    def _parse_numeric_value(line: str) -> float | None:
        normalized = line.strip().replace("J.", "0.").replace(").", "0.")
        normalized = normalized.replace("O.", "0.").replace("l.", "1.")
        normalized = normalized.replace(" ", "")
        if not re.fullmatch(r"-?\d+\.\d+", normalized):
            return None
        return float(normalized)


@dataclass
class CarolinasEnergyEfficiencyRiderProfile:
    """Profile for DEC Rider EE sheets with explicit numeric rider-adjustment values."""

    name: str = "carolinas_energy_efficiency_rider"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-ee"}
    _TOTAL_RESIDENTIAL_RE = re.compile(
        r"Total Residential Rate\s*\(?\s*(-?\d+\.\d+)\)?\s*[¢c]?\s*per\s*kWh",
        re.I,
    )
    _TOTAL_NONRESIDENTIAL_RE = re.compile(
        r"Total Nonresidential\s*\(?\s*(-?\d+\.\d+)\)?\s*[¢c]?\s*per\s*kWh",
        re.I,
    )
    _OLD_PAIR_RE = re.compile(
        r"Residential\s+Non-?Residential\s+(-?\d+\.\d+)\s*[¢c$]?\s*p(?:er)?\s*kWh\s+(-?\d+\.\d+)\s*[¢c$]?\s*p(?:er)?\s*kWh",
        re.I,
    )
    _OLD_RESIDENTIAL_RE = re.compile(
        r"Residential\s+(-?\d+\.\d+)\s*[¢c$]?\s*(?:per\s*kWh|p(?:er)?kWh)",
        re.I,
    )
    _OLD_NONRES_TOTAL_RE = re.compile(
        r"Vintage\s*1\s*Total\s+(-?\d+\.\d+)\s*[¢c$]?\s*(?:per\s*kWh|p(?:er)?kWh)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider ee" in lowered and "energy efficiency rider" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "energy efficiency rider adjustments" in lowered:
            score += 0.03
        if "total residential rate" in lowered or "total nonresidential" in lowered:
            score += 0.03
        elif "vintage 1 total" in lowered or self._OLD_PAIR_RE.search(self._normalize(text)):
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        normalized = self._normalize(text)
        charges: list[ExtractedCharge] = []

        if match := self._TOTAL_RESIDENTIAL_RE.search(normalized):
            charges.append(self._charge("Residential Total Rider EE", self._to_float(match.group(1))))
        if match := self._TOTAL_NONRESIDENTIAL_RE.search(normalized):
            charges.append(self._charge("Nonresidential Total Rider EE", self._to_float(match.group(1))))

        if not charges and (match := self._OLD_PAIR_RE.search(normalized)):
            charges.append(self._charge("Residential Rider EE", self._to_float(match.group(1))))
            charges.append(self._charge("Nonresidential Rider EE", self._to_float(match.group(2))))

        if not any(charge.charge_label.startswith("Residential") for charge in charges):
            if match := self._OLD_RESIDENTIAL_RE.search(normalized):
                charges.append(self._charge("Residential Rider EE", self._to_float(match.group(1))))

        if not any(charge.charge_label.startswith("Nonresidential") for charge in charges):
            if match := self._OLD_NONRES_TOTAL_RE.search(normalized):
                charges.append(self._charge("Nonresidential Rider EE", self._to_float(match.group(1))))

        return self._dedupe(charges)

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = text.replace("¢", "c")
        normalized = normalized.replace("perkWh", " per kWh")
        normalized = normalized.replace("pcrkWh", " per kWh")
        normalized = normalized.replace("pcr kWh", " per kWh")
        normalized = normalized.replace("pcrkwh", " per kWh")
        normalized = normalized.replace("perkilowatt-hour", "per kWh")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    @staticmethod
    def _to_float(value: str) -> float:
        normalized = value.strip().replace("(", "-").replace(")", "")
        return float(normalized)

    @staticmethod
    def _charge(charge_label: str, cents_per_kwh: float) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type="adjustment",
            charge_label=charge_label,
            rate_value=cents_per_kwh / 100.0,
            rate_unit="$/kWh",
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        for charge in charges:
            key = (charge.charge_label, round(float(charge.rate_value), 6))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasResidentialFlatProfile:
    """Profile for Carolinas/Duke Power residential RS-style historical sheets."""

    name: str = "carolinas_residential_flat"

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in {
            "nc-carolinas-schedule-rs",
            "nc-carolinas-schedule-es",
            "nc-carolinas-leaf-11",
        } and not family_key.startswith("nc-carolinas-"):
            return False

        lowered = text.lower()
        has_company_signal = "carolinas" in detect_duke_company(lowered)
        has_flat_rate_signal = any(
            token in lowered for token in ("basic customer charge", "basic facilities charge")
        ) and "energy charge" in lowered
        has_tou_signal = any(token in lowered for token in ("on-peak", "off-peak", "time of use", "optional time of use"))

        # Schedule S (Unmetered Signs) is a niche flat-rate Carolinas schedule
        # that the existing parse_nc_carolinas_leaf extractor handles for the
        # Basic Customer Charge. Skip the RS-specific keyword gate and rely on
        # family + flat-rate + non-TOU signals.
        if family_key == "nc-carolinas-schedule-s":
            return (
                has_company_signal
                and "schedule s" in lowered
                and "unmetered" in lowered
                and has_flat_rate_signal
                and not has_tou_signal
            )

        has_rs_signal = any(
            token in lowered
            for token in (
                "schedule rs",
                "rate schedule rs",
                "residential schedules rs",
                "schedule es",
                "energy star",
                "schedule re",
                "schedule ret",
                "schedule bc",
                "building construction service",
            )
        )
        return has_company_signal and has_rs_signal and has_flat_rate_signal and not has_tou_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        family_key = (doc.get("family_key") or "").lower()
        score = 0.82
        if family_key in {"nc-carolinas-schedule-rs", "nc-carolinas-leaf-11"}:
            score += 0.06
        elif family_key == "nc-carolinas-schedule-es":
            score += 0.05
        if "for the billing months of" in text.lower():
            score += 0.03
        return min(score, 0.95)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        _, charges, _ = parse_nc_carolinas_leaf(
            text,
            version_id=0,
            family_key=doc.get("family_key") or "nc-carolinas-schedule-RS",
        )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class CarolinasResidentialTouProfile:
    """Profile for DEC historical residential time-of-use schedules.

    Covers RT (Residential Service TOU), OPT-E/OPTV (Optional TOU Service),
    RETC (Residential Energy TOU Control), RSTC (Residential Service TOU Control),
    SGSTC (Small General Service TOU Control), and similar TOU variants.
    All use the same parse_nc_carolinas_leaf extractor.
    """

    name: str = "carolinas_residential_tou"

    _SUPPORTED_FAMILIES = {
        "nc-carolinas-schedule-rt",
        "nc-carolinas-schedule-opt-e",
        "nc-carolinas-schedule-optv",
        "nc-carolinas-schedule-opt-v",
        "nc-carolinas-schedule-retc",
        "nc-carolinas-schedule-rstc",
        "nc-carolinas-schedule-sgstc",
        "nc-carolinas-doc-schedulertresidentialservicetimeofuse",
    }

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        has_company_signal = "carolinas" in detect_duke_company(lowered)
        has_family_signal = any(
            signal in lowered
            for signal in (
                "schedule rt",
                "schedule opt-e",
                "schedule opt",
                "schedule retc",
                "schedule rstc",
                "schedule sgstc",
                "residential service, time of use",
                "optional time-of-use",
                "optional time of use",
                "residential energy time-of-use",
                "residential service time-of-use control",
                "small general service time-of-use",
            )
        )
        has_tou_signal = any(token in lowered for token in ("on-peak", "off-peak", "time of use", "time-of-use"))
        has_rate_signal = "customer charge" in lowered or "facilities charge" in lowered
        return has_company_signal and has_family_signal and has_tou_signal and has_rate_signal

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "discount" in lowered or "super off-peak" in lowered:
            score += 0.02
        if "demand charge" in lowered:
            score -= 0.05
        return max(0.0, min(score, 0.96))

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-carolinas-schedule-RT"
        path = Path(doc.get("local_path") or "")
        start_page = doc.get("start_page")
        end_page = doc.get("end_page")

        if path.is_file() and path.suffix.lower() == ".pdf" and start_page and end_page:
            try:
                import fitz  # type: ignore
                with fitz.open(path) as _doc:
                    bounded_text = "\n".join(
                        _doc[pg].get_text("text")
                        for pg in range(start_page - 1, end_page)
                        if pg < len(_doc)
                    )
                _, charges, _ = parse_nc_carolinas_leaf(
                    bounded_text,
                    version_id=0,
                    family_key=family_key,
                )
            except Exception:
                _, charges, _ = parse_nc_carolinas_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_carolinas_leaf_file(
                path,
                version_id=0,
                family_key=family_key,
            )
        else:
            _, charges, _ = parse_nc_carolinas_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class CarolinasCurrentLeafBridgeProfile:
    """Bridge current-style DEC schedule PDFs into the historical extraction path."""

    name: str = "carolinas_current_leaf_bridge"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-schedule-hlf",
    }

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        if not self._is_current_carolinas_pdf(doc):
            return False
        lowered = text.lower()
        if "leaf no." not in lowered:
            return False
        return "schedule hlf" in lowered or "high load factor" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.86
        if "demand" in lowered:
            score += 0.03
        if "customer charge" in lowered:
            score += 0.02
        return min(score, 0.96)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-carolinas-schedule-HLF"
        path = Path(doc.get("local_path") or "")
        if path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_carolinas_leaf_file(
                path,
                version_id=0,
                family_key=family_key,
            )
        else:
            _, charges, _ = parse_nc_carolinas_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted

    @staticmethod
    def _is_current_carolinas_pdf(doc: dict) -> bool:
        local_path = str(doc.get("local_path") or "").replace("/", "\\").lower()
        return "data\\raw\\nc\\carolinas\\" in local_path


@dataclass
class CarolinasCustomerAssistanceRecoveryProfile:
    """Profile for DEC Rider CAR customer assistance recovery sheets."""

    name: str = "carolinas_customer_assistance_recovery"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-car"}
    _ROW_RE = re.compile(
        r"(Residential|General Service|Industrial)\s+.+?\$\s*([\d.]+)",
        re.I | re.S,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return (
            "customer assistance recovery" in lowered
            and "monthly rate" in lowered
            and ("$/kwh" in lowered or "$/bill" in lowered)
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "rate class" in lowered:
            score += 0.03
        if "applicable schedules" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-carolinas-rider-CAR"
        path = Path(doc.get("local_path") or "")
        if path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_carolinas_leaf_file(path, version_id=0, family_key=family_key)
            return _convert_progress_tariff_charges(charges)

        matches = list(self._ROW_RE.finditer(text))
        if matches:
            extracted: list[ExtractedCharge] = []
            for match in matches:
                label = match.group(1).strip()
                value = float(match.group(2))
                customer_class = {
                    "residential": "residential",
                    "general service": "commercial",
                    "industrial": "industrial",
                }[label.lower()]
                rate_unit = "$/kWh" if customer_class == "residential" else "$/bill"
                extracted.append(
                    ExtractedCharge(
                        charge_type="adjustment",
                        charge_label="Rider Adjustment",
                        rate_value=value,
                        rate_unit=rate_unit,
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=match.group(0)[:120],
                        confidence_score=0.92,
                    )
                )
            return extracted

        _, charges, _ = parse_nc_carolinas_leaf(text, version_id=0, family_key=family_key)
        if charges:
            return _convert_progress_tariff_charges(charges)

        return []


@dataclass
class CarolinasNuclearProductionTaxCreditsProfile:
    """Profile for DEC Rider NPTC nuclear production tax credit leaves."""

    name: str = "carolinas_nuclear_production_tax_credits"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-rider-ridernptc",
        "nc-carolinas-rider-nptc",
    }
    _RATE_RE = re.compile(
        r"(?:decremental\s+rate|approved\s+decremental\s+rate)[^()\n]{0,120}\(?([\d.]+)¢\)?\s+per\s+kilowatt-hour",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "nuclear production tax credits" in lowered and "per kilowatt-hour" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        return 0.94 if self._RATE_RE.search(text) else 0.85

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        match = self._RATE_RE.search(text)
        if not match:
            return []
        return [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label="Nuclear Production Tax Credit Rider",
                rate_value=-(float(match.group(1)) / 100.0),
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet=match.group(0)[:120],
                confidence_score=0.93,
            )
        ]


@dataclass
class CarolinasSingleValueRiderProfile:
    """Profile for Carolinas single-value riders like EDPR and BPM True-Up."""

    name: str = "carolinas_single_value_rider"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-rider-edpr",
        "nc-carolinas-rider-bpmppttrueup",
        "nc-carolinas-rider-bpmprospectiverider",
        "nc-carolinas-rider-prospectiverider",
        "nc-carolinas-rider-ps",
        "nc-carolinas-rider-riderlc",
    }
    _RATE_RE = re.compile(
        r"\(?([\-]?[\d.]+)\)?\s*(?:¢|c)\s*/?\s*kwh|\(?([\-]?[\d.]+)\)?\s*(?:¢|c)\s*per\s+kilowatt-?hour|\(?([\-]?[\d.]+)\)?\s*cents\s+per\s+kwh|\(?([\-]?[\d.]+)\)?\s*[pf¢¢(i]\s*/?\s*kwh",
        re.I,
    )
    _NUMBER_RE = re.compile(r"[-]?[\d]+\.[\d]+")
    _OCRISH_KWH_RATE_RE = re.compile(
        r"\(?([-]?[\d]+\.[\d]+)\)?[^\n\r0-9]{0,8}kwh|\(?([-]?[\d]+\.[\d]+)\)?\s*(?:¢|c)\s*per\s+kilowatt-?hour|\(?([-]?[\d]+\.[\d]+)\)?\s*cents\s+per\s+kwh",
        re.I,
    )
    _EDPR_LABEL_RE = re.compile(
        r"existing\s*dsm\s*program\s*(?:costs\s*)?rate\s*adjustment\s*per\s*kilowatt[-\s]*hour",
        re.I,
    )
    # Matches the "Change in Rates" or "Change in Existing DSM Costs" line with an OCR-tolerant
    # kWh suffix (p/kWh, ¢/kWh, f/kWh, (i/kWh, etc. are all OCR variants of ¢/kWh)
    _EDPR_CHANGE_RE = re.compile(
        r"change\s+in\s+rates?\s+([\-]?[\d.]+)\s*[^\n]{0,6}kwh",
        re.I,
    )
    _EDPR_DSM_CHANGE_RE = re.compile(
        r"change\s+in\s+existing\s+dsm\s+costs?\s+([\-]?[\d.]+)\s*[^\n]{0,6}kwh",
        re.I,
    )
    _BPM_LABEL_RE = re.compile(
        r"bpm\s*net\s*revenues\s*and\s*non[-\s]*firm\s*point[-\s]*to[-\s]*point\s*transmission\s*revenues\s*rate\s*adjustment",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        # New riders (PS, RIDERLC, PROSPECTIVERIDER) use the shared prior-use fallback
        # instead of the explicit BPM/EDPR text markers.
        if family_key in {
            "nc-carolinas-rider-ps",
            "nc-carolinas-rider-riderlc",
            "nc-carolinas-rider-prospectiverider",
            "nc-carolinas-rider-bpmprospectiverider",
        }:
            return (
                ("per kilowatt-hour" in lowered or "per kilowatt hour" in lowered
                 or "c/kwh" in lowered or "/kwh" in lowered or "cents per kwh" in lowered)
            )
        return (
            ("existing dsm program" in lowered or "bpm true-up rider" in lowered or "bpm prospective rider" in lowered)
            and ("per kilowatt-hour" in lowered or "per kilowatt hour" in lowered or "c/kwh" in lowered or "/kwh" in lowered or "cents per kwh" in lowered)
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "approved decremental rate" in lowered or "approved incremental rate" in lowered:
            score += 0.04
        if "adjustment rider" in lowered or "true-up rider" in lowered:
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        label = "Rider Adjustment"
        family_key = (doc.get("family_key") or "").lower()
        value: float | None = None
        source_snippet = ""

        if family_key == "nc-carolinas-rider-edpr":
            label = "Existing DSM Program Rider"
            # Try "Change in Rates" line first — that's the actual filed rate value
            m_change = self._EDPR_CHANGE_RE.search(text)
            if m_change:
                raw = float(m_change.group(1))
                value = raw / 100.0
                source_snippet = m_change.group(0)[:120]
            else:
                # Try "Change in Existing DSM Costs" line
                m_dsm = self._EDPR_DSM_CHANGE_RE.search(text)
                if m_dsm:
                    raw = float(m_dsm.group(1))
                    value = raw / 100.0
                    source_snippet = m_dsm.group(0)[:120]
                else:
                    value, source_snippet = self._extract_marked_kwh_rate(
                        text,
                        prefer_positive=False,
                        anchor_regex=self._EDPR_LABEL_RE,
                    )
        elif family_key in {"nc-carolinas-rider-bpmppttrueup", "nc-carolinas-rider-bpmprospectiverider"}:
            label = "BPM True-Up Rider"
            value, source_snippet = self._extract_marked_kwh_rate(
                text,
                prefer_positive=True,
                anchor_regex=self._BPM_LABEL_RE,
            )

        if value is None:
            match = self._RATE_RE.search(text)
            if not match:
                return []
            value_str = match.group(1) or match.group(2) or match.group(3) or match.group(4)
            raw = float(value_str)
            value = raw / 100.0
            source_snippet = match.group(0)[:120]
            if "(" in match.group(0) and ")" in match.group(0) and value > 0:
                value = -value

        return [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label=label,
                rate_value=value,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet=source_snippet,
                confidence_score=0.92,
            )
        ]

    def _extract_marked_kwh_rate(
        self,
        text: str,
        *,
        prefer_positive: bool,
        anchor_regex: re.Pattern[str] | None = None,
    ) -> tuple[float | None, str]:
        search_text = text
        if anchor_regex is not None:
            anchor = anchor_regex.search(text)
            if anchor is not None:
                search_text = text[anchor.start(): anchor.start() + 320]
        candidates: list[tuple[float, str]] = []
        for match in self._OCRISH_KWH_RATE_RE.finditer(search_text):
            value_str = match.group(1) or match.group(2) or match.group(3)
            if not value_str:
                continue
            raw = float(value_str)
            if abs(raw) >= 10:
                continue
            start, end = match.span()
            snippet = search_text[max(0, start - 16): min(len(search_text), end + 16)]
            if "(" in snippet and ")" in snippet and raw > 0:
                raw = -raw
            candidates.append((raw / 100.0, snippet[:120]))
        if prefer_positive:
            for value, snippet in candidates:
                if value > 0:
                    return value, snippet
        if candidates:
            return candidates[0]
        return None, ""


@dataclass
class CarolinasGeneralServiceScheduleProfile:
    """Profile for historical Carolinas PG/LGS and similar general-service schedule sheets."""

    name: str = "carolinas_general_service_schedule"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-schedule-pg",
        "nc-carolinas-schedule-lgs",
        "nc-carolinas-schedule-sgs",
        "nc-carolinas-doc-scheduleoptioptionalpowerservicetimeofuseindustr",
        "nc-carolinas-doc-scheduleiindustrialservice",
        "nc-carolinas-doc-schedulelgslargegeneralservice",
    }

    @staticmethod
    def _has_leaf_marker(text: str) -> bool:
        return bool(re.search(r"leaf\s*no\.?", text, re.I))

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        if "carolinas" not in detect_duke_company(lowered):
            return False
        # Nantahala-area schedules (Duke Energy Carolinas, LLC, Nantahala Area) use
        # "Page N of N" footers instead of "Leaf No." — skip leaf marker check for them.
        is_nantahala = "nantahala" in lowered
        if not is_nantahala and not self._has_leaf_marker(text):
            return False
        if family_key == "nc-carolinas-schedule-pg":
            return "schedule pg" in lowered or "parallel generation" in lowered
        if family_key in {
            "nc-carolinas-schedule-lgs",
            "nc-carolinas-schedule-sgs",
            "nc-carolinas-doc-schedulelgslargegeneralservice",
        }:
            return (
                "schedule lgs" in lowered
                or "large general service" in lowered
                or "schedule sgs" in lowered
                or "small general service" in lowered
                or "schedule sg" in lowered
            )
        if family_key == "nc-carolinas-doc-scheduleoptioptionalpowerservicetimeofuseindustr":
            return "schedule opt-i" in lowered or "optional power service" in lowered
        if family_key == "nc-carolinas-doc-scheduleiindustrialservice":
            return "schedule i" in lowered and "industrial service" in lowered
        return False

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        family_key = (doc.get("family_key") or "").lower()
        lowered = text.lower()
        score = 0.87
        if family_key == "nc-carolinas-schedule-pg":
            score += 0.03
        if family_key in {
            "nc-carolinas-schedule-lgs",
            "nc-carolinas-schedule-sgs",
            "nc-carolinas-doc-schedulelgslargegeneralservice",
        }:
            score += 0.03
        if "basic customer charge" in lowered or "customer charge" in lowered:
            score += 0.02
        if "energy charge" in lowered:
            score += 0.02
        if "demand charge" in lowered or "billing demand" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-carolinas-schedule-PG"
        path = Path(doc.get("local_path") or "")
        start_page = doc.get("start_page")
        end_page = doc.get("end_page")

        # If the document has explicit page bounds, extract only those pages to avoid
        # reading other schedules from multi-schedule compliance bundles.
        if path.is_file() and path.suffix.lower() == ".pdf" and start_page and end_page:
            try:
                import fitz  # type: ignore
                with fitz.open(path) as _doc:
                    bounded_text = "\n".join(
                        _doc[pg].get_text("text")
                        for pg in range(start_page - 1, end_page)
                        if pg < len(_doc)
                    )
                _, charges, _ = parse_nc_carolinas_leaf(
                    bounded_text,
                    version_id=0,
                    family_key=family_key,
                )
            except Exception:
                _, charges, _ = parse_nc_carolinas_leaf(text, version_id=0, family_key=family_key)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            _, charges, _ = parse_nc_carolinas_leaf_file(
                path,
                version_id=0,
                family_key=family_key,
            )
        else:
            _, charges, _ = parse_nc_carolinas_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class CarolinasScheduleBridgeProfile:
    """Bridge profile for historical Carolinas schedule sheets parsed by the shared leaf parser."""

    name: str = "carolinas_schedule_bridge"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-schedule-i",
        "nc-carolinas-doc-scheduleiindustrialservice",
        "nc-carolinas-doc-scheduleopte",
        "nc-carolinas-doc-scheduleoptg",
        "nc-carolinas-schedule-ts",
        "nc-carolinas-schedule-opt-e",
        "nc-carolinas-schedule-opt-g",
        "nc-carolinas-schedule-opt-h",
        "nc-carolinas-schedule-opt-i",
        "nc-carolinas-schedule-bc",
        "nc-carolinas-schedule-it",
        "nc-carolinas-schedule-nl",
        "nc-carolinas-schedule-hp",
        "nc-carolinas-schedule-ppbe",
        "nc-carolinas-schedule-hlf",
        "nc-carolinas-schedule-wc",
        "nc-carolinas-schedule-ret",
        "nc-carolinas-schedule-rst",
        "nc-carolinas-schedule-sgst",
        "nc-carolinas-doc-schedulewc",
        "nc-carolinas-doc-schedulewcresidentialwaterheatingservice",
    }

    @staticmethod
    def _has_leaf_marker(text: str) -> bool:
        return bool(re.search(r"leaf\s*no\.?", text, re.I))

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        if "carolinas" not in detect_duke_company(lowered):
            return False
        if not self._has_leaf_marker(text):
            return False
        family_markers = {
            "nc-carolinas-schedule-i": ("schedule i", "industrial service"),
            "nc-carolinas-doc-scheduleiindustrialservice": ("schedule i", "industrial service"),
            "nc-carolinas-doc-scheduleopte": ("schedule opt-e", "optional power service"),
            "nc-carolinas-doc-scheduleoptg": ("schedule opt-g", "general service"),
            "nc-carolinas-schedule-ts": ("schedule ts", "traffic signal service"),
            "nc-carolinas-schedule-opt-e": ("schedule opt-e", "optional power service"),
            "nc-carolinas-schedule-opt-g": ("schedule opt-g", "general service"),
            "nc-carolinas-schedule-opt-h": ("schedule opt-h", "optional power service"),
            "nc-carolinas-schedule-opt-i": ("schedule opt-i", "optional power service"),
            "nc-carolinas-schedule-bc": ("schedule bc", "building construction service"),
            "nc-carolinas-schedule-it": ("schedule it", "interruptible"),
            "nc-carolinas-schedule-nl": ("schedule nl", "night"),
            "nc-carolinas-schedule-hp": ("schedule hp", "hourly pricing"),
            "nc-carolinas-schedule-ppbe": ("ppbe", "purchased power"),
            "nc-carolinas-schedule-hlf": ("schedule hlf", "high load factor"),
            "nc-carolinas-schedule-wc": ("schedule wc", "water heating"),
            "nc-carolinas-schedule-ret": ("schedule ret", "residential"),
            "nc-carolinas-schedule-rst": ("schedule rst", "residential"),
            "nc-carolinas-schedule-sgst": ("schedule sgst", "general service"),
            "nc-carolinas-doc-schedulewc": ("schedule wc", "residential water heating service"),
            "nc-carolinas-doc-schedulewcresidentialwaterheatingservice": ("schedule wc", "residential water heating service"),
        }
        required_tokens = family_markers.get(family_key)
        return bool(required_tokens and all(token in lowered for token in required_tokens))

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        family_key = (doc.get("family_key") or "").lower()
        score = 0.88
        if family_key in {"nc-carolinas-schedule-i", "nc-carolinas-doc-scheduleiindustrialservice"}:
            score += 0.03
        if family_key == "nc-carolinas-doc-scheduleopte":
            score += 0.03
        if "basic facilities charge" in lowered:
            score += 0.02
        if "energy charge" in lowered:
            score += 0.02
        if "demand charge" in lowered or "billing demand" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = doc.get("family_key") or "nc-carolinas-schedule-I"
        # Prefer the pre-bounded text passed in (page range already applied by pipeline).
        # Only fall back to full-file parsing when text is empty or missing, to avoid
        # reading all pages of a multi-schedule PDF (which causes wrong-schedule matches).
        if text and text.strip():
            _, charges, _ = parse_nc_carolinas_leaf(
                text,
                version_id=0,
                family_key=family_key,
            )
        else:
            path = Path(doc.get("local_path") or "")
            if path.is_file() and path.suffix.lower() == ".pdf":
                _, charges, _ = parse_nc_carolinas_leaf_file(
                    path,
                    version_id=0,
                    family_key=family_key,
                )
            else:
                charges = []
        extracted: list[ExtractedCharge] = []
        for charge in charges:
            extracted.append(
                ExtractedCharge(
                    charge_type=charge.charge_type,
                    charge_label=charge.charge_label or charge.charge_type.replace("_", " ").title(),
                    rate_value=charge.rate_value,
                    rate_unit=charge.rate_unit or "",
                    season=charge.season,
                    tou_period=charge.tou_period,
                    tier_min=charge.tier_min,
                    tier_max=charge.tier_max,
                    source_snippet=charge.source_snippet or "",
                    confidence_score=charge.confidence_score,
                )
            )
        return extracted


@dataclass
class CarolinasLightingScheduleProfile:
    """Profile for Carolinas lighting schedule tables."""

    name: str = "carolinas_lighting_schedule"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-schedule-ol",
        "nc-carolinas-schedule-pl",
        "nc-carolinas-schedule-fl",
        "nc-carolinas-schedule-yl",
        "nc-carolinas-schedule-gl",
        "nc-carolinas-doc-scheduleplstreetandpubliclightingservice",
        "nc-carolinas-doc-floodlightingservice",
        "nc-carolinas-doc-scheduleflfloodlightingservice",
        "nc-carolinas-doc-scheduleylyardlightingservice",
        "nc-carolinas-doc-governmentallightingservice",
    }
    _MONEY_RE = re.compile(r"\$\s*([\d.]+)")
    _SIMPLE_VALUE_RE = re.compile(
        r"^(?P<label>.+?)\s+\$?\s*(?P<value>[\d.]+|NA)$",
        re.I,
    )
    _PL_ROW_RE = re.compile(
        r"^(?:[\d,]+\s+\d+\s+)?(?P<label>.+?)\s+\$?\s*(?P<inside>[\d.]+|NA)\s+\$?\s*(?P<outside>[\d.]+|NA)$",
        re.I,
    )
    _THREE_COLUMN_ROW_RE = re.compile(
        r"^(?:[\d,]+\s+\d+\s+)?(?P<label>.+?)\s+\$?\s*(?P<existing>[\d.]+|NA)\s+\$?\s*(?P<new>[\d.]+|NA)\s+\$?\s*(?P<underground>[\d.]+|NA)$",
        re.I,
    )
    _FL_ROW_RE = re.compile(
        r"^(?:[\d,]+\s+\d+\s+)?(?P<label>.+?)\s+\$?\s*(?P<existing>[\d.]+|NA)\s+\$?\s*(?P<new>[\d.]+|NA)\s+\$?\s*(?P<underground>[\d.]+|NA)$",
        re.I,
    )
    _PL_CATEGORY_MAP = {
        "high pressure sodium vapor": "High Pressure Sodium Vapor",
        "metal halide": "Metal Halide",
        "mercury vapor": "Mercury Vapor",
        "incandescent": "Incandescent",
    }
    _FL_CATEGORY_MAP = {
        "high pressure sodium vapor": "High Pressure Sodium Vapor",
        "high pressure sodium 1 'apor": "High Pressure Sodium Vapor",
        "high pressure sodium 1'apor": "High Pressure Sodium Vapor",
        "metal halide": "Metal Halide",
    }

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return (
            ("schedule ol" in lowered and "outdoor lighting service" in lowered)
            or ("schedule yl" in lowered and "yard lighting service" in lowered)
            or ("schedule gl" in lowered and "governmental lighting service" in lowered)
            or ("schedule pl" in lowered and "per month per luminaire" in lowered)
            or ("schedule fl" in lowered and "floodlighting service" in lowered)
        )

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "per month per luminaire" in lowered:
            score += 0.03
        if "underground charges" in lowered or "underground" in lowered:
            score += 0.02
        if "per month per unit" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = (doc.get("family_key") or "").lower()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if family_key == "nc-carolinas-schedule-ol":
            return self._extract_ol(lines)
        if family_key in {
            "nc-carolinas-schedule-pl",
            "nc-carolinas-doc-scheduleplstreetandpubliclightingservice",
        }:
            return self._extract_pl(lines)
        if family_key in {
            "nc-carolinas-schedule-fl",
            "nc-carolinas-doc-floodlightingservice",
            "nc-carolinas-doc-scheduleflfloodlightingservice",
        }:
            return self._extract_fl(lines)
        if family_key in {
            "nc-carolinas-schedule-yl",
            "nc-carolinas-doc-scheduleylyardlightingservice",
        }:
            return self._extract_yl(lines)
        if family_key in {
            "nc-carolinas-schedule-gl",
            "nc-carolinas-doc-governmentallightingservice",
        }:
            return self._extract_gl(lines)
        return []

    def _extract_ol(self, lines: list[str]) -> list[ExtractedCharge]:
        line_row_charges = self._extract_three_column_line_rows(lines, default_category="Outdoor Lighting")
        if line_row_charges:
            return self._dedupe(line_row_charges)
        return self._extract_three_column_sequential(
            lines,
            service_heading="SCHEDULE OL",
            service_label="Outdoor Lighting",
        )

    def _extract_gl(self, lines: list[str]) -> list[ExtractedCharge]:
        line_row_charges = self._extract_three_column_line_rows(lines, default_category="Governmental Lighting")
        if line_row_charges:
            return self._dedupe(line_row_charges)
        return self._extract_three_column_sequential(
            lines,
            service_heading="SCHEDULE GL",
            service_label="Governmental Lighting",
        )

    def _extract_yl(self, lines: list[str]) -> list[ExtractedCharge]:
        row_charges = self._extract_yl_line_rows(lines)
        if row_charges:
            return self._dedupe(row_charges)

        charges: list[ExtractedCharge] = []
        try:
            start_idx = lines.index("MONTHLY RATE PER UNIT")
        except ValueError:
            return []

        labels: list[str] = []
        idx = start_idx + 1
        while idx < len(lines):
            line = lines[idx]
            upper = line.upper()
            if upper in {"RIDERS", "PAYMENT", "CONTRACT TERM", "EXTRA FACILITIES"}:
                break
            if "PER MONTH PER UNIT" in upper:
                idx += 1
                continue
            if "POLES" == upper:
                idx += 1
                break
            if self._is_money_or_na(line) or re.fullmatch(r"[\d,]+", line) or re.fullmatch(r"\d+", line):
                idx += 1
                continue
            if "luminaire style" in upper or "lumens" in upper or "month" in upper:
                idx += 1
                continue
            if "(" in line and "luminaire" not in line.lower():
                labels.append(line)
            elif any(token in line.lower() for token in ("watt", "pole", "traditional", "fiberglass", "secondary")):
                labels.append(line)
            idx += 1

        value_lines = [self._parse_money_or_na(self._normalize_ocr_money_line(line)) for line in lines[start_idx + 1 : idx] if self._parse_money_or_na(self._normalize_ocr_money_line(line)) is not None]
        if labels and value_lines:
            charges.extend(
                self._build_sequential_charges(
                    category="Yard Lighting",
                    labels=labels,
                    values=value_lines,
                    column_label="Per Month Per Unit",
                )
            )

        for raw_line in lines[idx:]:
            line = self._normalize_ocr_money_line(raw_line)
            if "$" not in line:
                continue
            match = self._SIMPLE_VALUE_RE.match(line)
            if not match:
                continue
            value = self._parse_money_or_na(match.group("value"))
            if value is None:
                continue
            label = self._normalize_table_label(match.group("label"))
            if "pole" not in label.lower():
                continue
            charges.append(
                self._make_fixed_charge(
                    charge_label=f"Yard Lighting - {label}",
                    rate_value=value,
                )
            )
        return self._dedupe(charges)

    def _extract_pl(self, lines: list[str]) -> list[ExtractedCharge]:
        line_row_charges = self._extract_pl_line_rows(lines)
        if line_row_charges:
            return self._dedupe(line_row_charges)

        charges: list[ExtractedCharge] = []
        try:
            hps_idx = lines.index("High Pressure Sodium Vapor")
            inside_idx = lines.index("Inside")
            outside_idx = lines.index("Outside")
            hps_labels = [line for line in lines[hps_idx + 1 : inside_idx - 1] if not self._is_money_or_na(line)]
            inside_values, inside_next = self._collect_value_lines(lines, inside_idx + 2)
            outside_values, outside_next = self._collect_value_lines(lines, outside_idx + 2)
            charges.extend(
                self._build_sequential_charges(
                    category="High Pressure Sodium Vapor",
                    labels=hps_labels,
                    values=inside_values,
                    column_label="Inside Municipal Limits",
                )
            )
            charges.extend(
                self._build_sequential_charges(
                    category="High Pressure Sodium Vapor",
                    labels=hps_labels,
                    values=outside_values,
                    column_label="Outside Municipal Limits",
                )
            )

            if outside_next is not None and outside_next < len(lines) and lines[outside_next] == "Metal Halide":
                metal_labels = [line for line in lines[outside_next + 1 : outside_next + 2] if not self._is_money_or_na(line)]
                metal_values, metal_next = self._collect_value_lines(lines, outside_next + 2)
                if metal_values:
                    if len(metal_values) >= 1:
                        charges.extend(
                            self._build_sequential_charges(
                                category="Metal Halide",
                                labels=metal_labels or ["Urban"],
                                values=metal_values[:1],
                                column_label="Inside Municipal Limits",
                            )
                        )
                    if len(metal_values) >= 2:
                        charges.extend(
                            self._build_sequential_charges(
                                category="Metal Halide",
                                labels=metal_labels or ["Urban"],
                                values=metal_values[1:2],
                                column_label="Outside Municipal Limits",
                            )
                        )
                if metal_next is not None and metal_next < len(lines) and lines[metal_next] == "Mercury Vapor *":
                    mercury_labels = [line for line in lines[metal_next + 1 :] if not self._is_money_or_na(line)]
                    mercury_values, _ = self._collect_value_lines(lines, metal_next + 1 + len(mercury_labels))
                    midpoint = min(len(mercury_labels), len(mercury_values))
                    charges.extend(
                        self._build_sequential_charges(
                            category="Mercury/Incandescent",
                            labels=mercury_labels,
                            values=mercury_values[:midpoint],
                            column_label="Monthly Charge",
                        )
                    )
        except ValueError:
            return []
        return self._dedupe(charges)

    def _extract_fl(self, lines: list[str]) -> list[ExtractedCharge]:
        line_row_charges = self._extract_fl_line_rows(lines)
        if line_row_charges:
            return self._dedupe(line_row_charges)

        charges: list[ExtractedCharge] = []
        try:
            hps_idx = lines.index("High Pressure Sodium Vapor")
            existing_idx = lines.index("Existing Pole (1)")
            new_idx = lines.index("New Pole")
            underground_idx = lines.index("Underground")
        except ValueError:
            return []

        label_lines = [line for line in lines[hps_idx:existing_idx] if not self._is_money_or_na(line)]
        labels = self._merge_fl_labels(label_lines)
        existing_and_new_values, _ = self._collect_value_lines(lines, new_idx + 1)
        label_count = len(labels)
        existing_values = existing_and_new_values[:label_count]
        new_values = existing_and_new_values[label_count : label_count * 2]
        underground_values, _ = self._collect_value_lines(lines, underground_idx + 1)

        charges.extend(
            self._build_sequential_charges(
                category="Floodlighting",
                labels=labels,
                values=existing_values,
                column_label="Existing Pole",
            )
        )
        charges.extend(
            self._build_sequential_charges(
                category="Floodlighting",
                labels=labels,
                values=new_values,
                column_label="New Pole",
            )
        )
        charges.extend(
            self._build_sequential_charges(
                category="Floodlighting",
                labels=labels,
                values=underground_values,
                column_label="Underground",
            )
        )
        return self._dedupe(charges)

    def _extract_pl_line_rows(self, lines: list[str]) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_category: str | None = None
        for raw_line in lines:
            line = self._normalize_ocr_money_line(raw_line)
            normalized_heading = self._normalize_heading(line)
            category = self._PL_CATEGORY_MAP.get(normalized_heading)
            if category:
                current_category = category
                continue
            if not current_category or "$" not in line:
                continue
            match = self._PL_ROW_RE.match(line)
            if not match:
                continue
            label = self._normalize_table_label(match.group("label"))
            inside_value = self._parse_money_or_na(match.group("inside"))
            outside_value = self._parse_money_or_na(match.group("outside"))
            if inside_value is not None:
                charges.append(
                    self._make_fixed_charge(
                        charge_label=f"{current_category} - {label} - Inside Municipal Limits",
                        rate_value=inside_value,
                    )
                )
            if outside_value is not None:
                charges.append(
                    self._make_fixed_charge(
                        charge_label=f"{current_category} - {label} - Outside Municipal Limits",
                        rate_value=outside_value,
                    )
                )
        return charges

    def _extract_fl_line_rows(self, lines: list[str]) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_category: str | None = None
        for raw_line in lines:
            line = self._normalize_ocr_money_line(raw_line)
            normalized_heading = self._normalize_heading(line)
            category = self._FL_CATEGORY_MAP.get(normalized_heading)
            if category:
                current_category = category
                continue
            if not current_category or "$" not in line:
                continue
            match = self._FL_ROW_RE.match(line)
            if not match:
                continue
            label = self._normalize_table_label(match.group("label"))
            existing_value = self._parse_money_or_na(match.group("existing"))
            new_value = self._parse_money_or_na(match.group("new"))
            underground_value = self._parse_money_or_na(match.group("underground"))
            base_label = f"Floodlighting - {current_category} - {label}"
            if existing_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - Existing Pole", rate_value=existing_value))
            if new_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - New Pole", rate_value=new_value))
            if underground_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - Underground", rate_value=underground_value))
        return charges

    def _extract_three_column_line_rows(self, lines: list[str], *, default_category: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_category = default_category
        for raw_line in lines:
            line = self._normalize_ocr_money_line(raw_line)
            normalized_heading = self._normalize_heading(line)
            category = self._FL_CATEGORY_MAP.get(normalized_heading)
            if category:
                current_category = category
                continue
            if "$" not in line:
                continue
            match = self._THREE_COLUMN_ROW_RE.match(line)
            if not match:
                continue
            label = self._normalize_table_label(match.group("label"))
            existing_value = self._parse_money_or_na(match.group("existing"))
            new_value = self._parse_money_or_na(match.group("new"))
            underground_value = self._parse_money_or_na(match.group("underground"))
            base_label = f"{current_category} - {label}"
            if existing_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - Existing Pole", rate_value=existing_value))
            if new_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - New Pole", rate_value=new_value))
            if underground_value is not None:
                charges.append(self._make_fixed_charge(charge_label=f"{base_label} - Underground", rate_value=underground_value))
        return charges

    def _extract_three_column_sequential(
        self,
        lines: list[str],
        *,
        service_heading: str,
        service_label: str,
    ) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        try:
            rate_idx = next(idx for idx, line in enumerate(lines) if line.upper().startswith("RATE"))
        except StopIteration:
            return []

        labels: list[str] = []
        idx = rate_idx + 1
        while idx < len(lines):
            line = lines[idx]
            normalized = line.upper()
            if "EXISTING POLE" in normalized:
                break
            if service_heading in normalized or "AVAILABILITY" in normalized:
                idx += 1
                continue
            if self._is_money_or_na(line) or re.fullmatch(r"[\d,]+", line) or re.fullmatch(r"\d+", line):
                idx += 1
                continue
            if any(token in normalized for token in ("LAMP RATING", "LUMENS", "MONTH", "STYLE", "RATE:", "(A)", "(B)")):
                idx += 1
                continue
            if any(token in line.lower() for token in ("urban", "suburban", "post top", "area", "floodlight")) or "(" in line or "high pressure" in line.lower() or "metal halide" in line.lower() or "mercury vapor" in line.lower():
                labels.append(line)
            idx += 1

        if idx >= len(lines):
            return []

        existing_values, next_idx = self._collect_value_lines(lines, idx + 1)
        if next_idx is None:
            return []
        new_values, next_idx = self._collect_value_lines(lines, next_idx + 1)
        if next_idx is None:
            return []
        underground_values, _ = self._collect_value_lines(lines, next_idx + 1)

        charges.extend(
            self._build_sequential_charges(
                category=service_label,
                labels=labels,
                values=existing_values,
                column_label="Existing Pole",
            )
        )
        charges.extend(
            self._build_sequential_charges(
                category=service_label,
                labels=labels,
                values=new_values,
                column_label="New Pole",
            )
        )
        charges.extend(
            self._build_sequential_charges(
                category=service_label,
                labels=labels,
                values=underground_values,
                column_label="Underground",
            )
        )
        return self._dedupe(charges)

    def _extract_yl_line_rows(self, lines: list[str]) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        current_section = "Yard Lighting"
        for raw_line in lines:
            line = self._normalize_ocr_money_line(raw_line)
            upper = line.upper()
            if upper == "POLES":
                current_section = "Poles"
                continue
            if "$" not in line:
                continue
            match = self._SIMPLE_VALUE_RE.match(line)
            if not match:
                continue
            value = self._parse_money_or_na(match.group("value"))
            if value is None:
                continue
            label = self._normalize_table_label(match.group("label"))
            if current_section == "Poles":
                if "pole" not in label.lower():
                    continue
                charges.append(self._make_fixed_charge(charge_label=f"Yard Lighting - {label}", rate_value=value))
                continue
            if any(token in label.lower() for token in ("watt", "luminaire", "fiberglass", "traditional", "secondary pole", "existing company")):
                charges.append(self._make_fixed_charge(charge_label=f"Yard Lighting - {label}", rate_value=value))
        return charges

    @staticmethod
    def _normalize_heading(line: str) -> str:
        lowered = line.lower().strip().rstrip("*")
        lowered = re.sub(r"\s+", " ", lowered)
        return lowered

    @staticmethod
    def _normalize_table_label(label: str) -> str:
        return normalize_ocr_label(label)

    @staticmethod
    def _normalize_ocr_money_line(line: str) -> str:
        return normalize_ocr_money_line(line)

    @staticmethod
    def _make_fixed_charge(*, charge_label: str, rate_value: float) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type="fixed",
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit="$/month",
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.9,
        )

    def _merge_fl_labels(self, lines: list[str]) -> list[str]:
        labels: list[str] = []
        current_category: str | None = None
        for line in lines:
            if line in {"High Pressure Sodium Vapor", "Metal Halide"}:
                current_category = line
                continue
            if not current_category:
                continue
            labels.append(f"Floodlighting - {current_category} - {line}")
        return labels

    def _build_sequential_charges(
        self,
        *,
        category: str,
        labels: list[str],
        values: list[float | None],
        column_label: str,
    ) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        seen_labels: dict[str, int] = {}
        for idx, value in enumerate(values):
            if value is None:
                continue
            if idx < len(labels):
                raw_label = labels[idx]
                label = raw_label if raw_label.startswith(f"{category} -") or raw_label.startswith("Floodlighting -") else f"{category} - {raw_label}"
            else:
                label = f"{category} #{idx + 1}"
            full_label = f"{label} - {column_label}"
            seen_labels[full_label] = seen_labels.get(full_label, 0) + 1
            if seen_labels[full_label] > 1:
                full_label = f"{full_label} ({seen_labels[full_label]})"
            charges.append(
                ExtractedCharge(
                    charge_type="fixed",
                    charge_label=full_label,
                    rate_value=value,
                    rate_unit="$/month",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=f"{label} {column_label}"[:100],
                    confidence_score=0.88,
                )
            )
        return charges

    def _collect_value_lines(self, lines: list[str], start_index: int) -> tuple[list[float | None], int | None]:
        values: list[float | None] = []
        idx = start_index
        while idx < len(lines):
            line = lines[idx]
            if self._is_money_or_na(line):
                values.append(self._parse_money_or_na(line))
                idx += 1
                continue
            break
        return values, idx if idx < len(lines) else None

    def _parse_money_or_na(self, line: str) -> float | None:
        if line.strip().upper() == "NA":
            return None
        match = self._MONEY_RE.search(line)
        if match:
            return float(match.group(1))
        try:
            return float(line.strip())
        except ValueError:
            return None

    def _is_money_or_na(self, line: str) -> bool:
        return line.strip().upper() == "NA" or bool(self._MONEY_RE.search(line))

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class ProgressResidentialLoadControlProfile:
    """Profile for DEP Residential Service Load Control Rider LC (Leaf 715)."""

    name: str = "progress_residential_load_control"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-715"}
    _INITIAL_LCD_RE = re.compile(
        r"Company-provided HVAC Load Control Device\(s\)\s*-\s*One Time\s*\$([\d.]+)\s*per\s*residence",
        re.I,
    )
    _INITIAL_THERM_WINTER_RE = re.compile(
        r"Winter-Focused Participants with Customer-provided eligible Thermostat\(s\)\s*-\s*One Time\s*\$([\d.]+)",
        re.I,
    )
    _INITIAL_THERM_SUMMER_RE = re.compile(
        r"Summer-Only Participants with Customer-provided eligible Thermostat\(s\)\s*-\s*One Time\s*\$([\d.]+)",
        re.I,
    )
    _ANNUAL_SUMMER_RE = re.compile(
        r"Qualified Summer-Only Cooling System Controls\s*-\s*\$([\d.]+)\s*per\s*residence",
        re.I,
    )
    _ANNUAL_WINTER_RE = re.compile(
        r"Qualified Winter-Focused System Controls\s*-\s*\$([\d.]+)\s*per\s*residence",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider lc" in lowered and "load control" in lowered and "payment of incentives" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "initial incentive" in lowered:
            score += 0.04
        if "annual incentive" in lowered:
            score += 0.04
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._INITIAL_LCD_RE.search(text):
            charges.append(self._charge("credit", "HVAC Load Control Device - Initial Incentive", float(match.group(1)), "$/enrollment"))
        if match := self._INITIAL_THERM_WINTER_RE.search(text):
            charges.append(self._charge("credit", "Thermostat Winter - Initial Incentive", float(match.group(1)), "$/enrollment"))
        if match := self._INITIAL_THERM_SUMMER_RE.search(text):
            charges.append(self._charge("credit", "Thermostat Summer - Initial Incentive", float(match.group(1)), "$/enrollment"))
        if match := self._ANNUAL_SUMMER_RE.search(text):
            charges.append(self._charge("credit", "Summer Control - Annual Incentive", float(match.group(1)), "$/year"))
        if match := self._ANNUAL_WINTER_RE.search(text):
            charges.append(self._charge("credit", "Winter Control - Annual Incentive", float(match.group(1)), "$/year"))
        return ProgressIncomeQualifiedLoadControlProfile._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.90,
        )


@dataclass
class CarolinasSmallCustomerGeneratorProfile:
    """Profile for DEC Rider SCG sheets with explicit monthly supplemental/standby charges."""

    name: str = "carolinas_small_customer_generator"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-scg"}
    _SUPPLEMENTAL_CHARGE_RE = re.compile(
        r"Supplemental Basic (?P<label>Customer|Facilities) Charge per month[:\s]+\$?(?P<value>[\d.]+)",
        re.I,
    )
    # Standby charge is per KW for systems > 20 KW
    _STANDBY_KW_RE = re.compile(
        r"For systems more than 20 KW\s+\$\s*([\d.]+)\s+per\s+KW",
        re.I,
    )
    # Legacy: flat standby charge format
    _STANDBY_CHARGE_RE = re.compile(
        r"Standby Charge per month(?:,\s*if applicable)?:\s*\$?([\d.]+)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider scg" in lowered and "small customer generator" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.88
        if "supplemental basic" in lowered and "charge per month" in lowered:
            score += 0.04
        if "standby charge" in lowered or "for systems more than 20 kw" in lowered:
            score += 0.04
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._SUPPLEMENTAL_CHARGE_RE.search(text):
            label_kind = match.group("label").title()
            charges.append(
                self._charge(
                    "fixed",
                    f"Supplemental Basic {label_kind} Charge",
                    float(match.group(2)),
                    "$/month",
                )
            )
        if match := self._STANDBY_KW_RE.search(text):
            charges.append(self._charge("demand", "Standby Charge (Systems >20 KW)", float(match.group(1)), "$/kW/month"))
        elif match := self._STANDBY_CHARGE_RE.search(text):
            charges.append(self._charge("fixed", "Standby Charge", float(match.group(1)), "$/month"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(
        charge_type: str,
        charge_label: str,
        rate_value: float,
        rate_unit: str,
    ) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasNetMeteringRiderProfile:
    """Profile for DEC Rider NM sheets with explicit standby/minimum-bill terms."""

    name: str = "carolinas_net_metering_rider"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-nm"}
    _STANDBY_RE = re.compile(
        r"standby charge(?:\s+of)?\s+\$?\s*([\d.]+)\s+per\s+k[wW]\s+per\s+month",
        re.I,
    )
    _MIN_BILL_ADDER_RE = re.compile(
        r"minimum bill set at \$\s*([\d.]+)\s+more than the basic facilities charge",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider nm" in lowered and "net metering" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.88
        if self._STANDBY_RE.search(text):
            score += 0.04
        if self._MIN_BILL_ADDER_RE.search(text):
            score += 0.03
        if "non-bypassable charge" in lowered:
            score += 0.01
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._STANDBY_RE.search(text):
            charges.append(self._charge("demand", "Standby Charge", float(match.group(1)), "$/kW-month", match.group(0)))
        if match := self._MIN_BILL_ADDER_RE.search(text):
            charges.append(self._charge("fixed", "Minimum Bill Adder", float(match.group(1)), "$/month", match.group(0)))
        return self._dedupe(charges)

    @staticmethod
    def _charge(
        charge_type: str,
        charge_label: str,
        rate_value: float,
        rate_unit: str,
        source_snippet: str,
    ) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=source_snippet[:100],
            confidence_score=0.9,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class GreenSourceAdvantageRiderProfile:
    """Profile for GSA riders with explicit administrative charges."""

    name: str = "green_source_advantage_rider"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-665",
        "nc-carolinas-rider-gsa",
    }
    _ADMIN_CHARGE_RE = re.compile(
        r"administrative charge .*?\$([\d,]+(?:\.\d+)?)\s+per\s+customer\s+account",
        re.I | re.S,
    )
    _ADDITIONAL_ACCOUNT_RE = re.compile(
        r"additional \$([\d,]+(?:\.\d+)?)\s+charge per additional account",
        re.I,
    )
    _APPLICATION_FEE_RE = re.compile(
        r"\$([\d,]+(?:\.\d+)?)\s+nonrefundable application fee",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider gsa" in lowered and "green source advantage" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.9
        if "administrative charge" in lowered:
            score += 0.04
        if "additional account billed" in lowered:
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._ADMIN_CHARGE_RE.search(text):
            charges.append(self._charge("fixed", "GSA Administrative Charge", self._to_float(match.group(1)), "$/month"))
        if match := self._ADDITIONAL_ACCOUNT_RE.search(text):
            charges.append(self._charge("fixed", "Additional Account Charge", self._to_float(match.group(1)), "$/month"))
        if match := self._APPLICATION_FEE_RE.search(text):
            charges.append(self._charge("fixed", "Application Fee", self._to_float(match.group(1)), "$"))
        return self._dedupe(charges)

    @staticmethod
    def _to_float(value: str) -> float:
        return float(value.replace(",", ""))

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, float, str]] = set()
        for charge in charges:
            key = (charge.charge_label, round(float(charge.rate_value), 6), charge.rate_unit or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasEconomicDevelopmentRiderProfile:
    """Profile for DEC Rider EC sheets with explicit percentage credits."""

    name: str = "carolinas_economic_development_rider"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-ec"}
    _CREDIT_RE = re.compile(r"(?:months?|monihs)\s+(\d+\s*-\s*\d+)\s+(\d+(?:\.\d+)?)%", re.I)
    _AFTER_MONTH_RE = re.compile(r"after month\s+(\d+)\s+(\d+(?:\.\d+)?)%", re.I)

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider ec" in lowered and "economic development" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.89
        if "application of credit" in lowered:
            score += 0.04
        if self._CREDIT_RE.search(text):
            score += 0.03
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        for match in self._CREDIT_RE.finditer(text):
            charges.append(
                ExtractedCharge(
                    charge_type="adjustment",
                    charge_label=f"Months {match.group(1)} Rider Credit",
                    rate_value=float(match.group(2)) / 100.0,
                    rate_unit="% of applicable bill",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=match.group(0)[:100],
                    confidence_score=0.9,
                )
            )
        for match in self._AFTER_MONTH_RE.finditer(text):
            charges.append(
                ExtractedCharge(
                    charge_type="adjustment",
                    charge_label=f"After Month {match.group(1)} Rider Credit",
                    rate_value=float(match.group(2)) / 100.0,
                    rate_unit="% of applicable bill",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=match.group(0)[:100],
                    confidence_score=0.9,
                )
            )
        return self._dedupe(charges)

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        for charge in charges:
            key = (charge.charge_label, round(float(charge.rate_value), 6))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasInterruptibleServiceRiderProfile:
    """Profile for DEC Rider IS sheets with explicit credit and penalty constants."""

    name: str = "carolinas_interruptible_service_rider"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-is"}
    _CREDIT_RE = re.compile(r"Credit\s*=\s*EID\s*x\s*S?\$?([\d.]+)\s*/\s*KW", re.I)
    _PENALTY_RE = re.compile(r"Penalty\s*[-=]\s*[A-Z]*KWP\s*[xX]\s*\$?([\d.]+)", re.I)

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        return "rider is" in lowered and "interruptible power service" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.89
        if self._CREDIT_RE.search(text):
            score += 0.04
        if self._PENALTY_RE.search(text):
            score += 0.04
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._CREDIT_RE.search(text):
            charges.append(self._charge("adjustment", "Interruptible Credit", float(match.group(1)), "$/kW"))
        if match := self._PENALTY_RE.search(text):
            charges.append(self._charge("adjustment", "Penalty Charge", float(match.group(1)), "$/kW"))
        return self._dedupe(charges)

    @staticmethod
    def _charge(charge_type: str, charge_label: str, rate_value: float, rate_unit: str) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.91,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, float]] = set()
        for charge in charges:
            key = (charge.charge_label, round(float(charge.rate_value), 6))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasSolarChoiceRiderProfile:
    """Profile for DEC/DEP solar/net-metering riders with explicit fee/credit language."""

    name: str = "carolinas_solar_choice_rider"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-rider-nmb",
        "nc-carolinas-rider-nsc",
        "nc-carolinas-rider-rsc",
        "nc-progress-leaf-670",  # DEP equivalent of RSC — identical rate structure
    }

    _MONTHLY_CREDIT_RE = re.compile(
        r"Monthly Credit for Net Excess Energy,\s*per\s*kWh\s*\$?([\d.]+)",
        re.I,
    )
    _NET_EXCESS_CREDIT_RE = re.compile(
        r"Net Excess Energy Credit per month,\s*per\s*kWh\s*([\d.]+)\s*[¢\u00a2c]",
        re.I,
    )
    _NON_BYPASSABLE_RE = re.compile(
        r"Non-Bypassable Charge per month,\s*per\s+Nameplate Capacity\s+kW(?:\s*(?:AC|DC))?\s*\$?([\d.]+)",
        re.I,
    )
    _GRID_ACCESS_FEE_RE = re.compile(
        r"Grid Access Fee per month,\s*per\s+Nameplate Capacity\s+kW[^\n]*?\$([\d.]+)",
        re.I,
    )
    _ON_PEAK_ENERGY_RE = re.compile(
        r"on-peak energy per month,\s*per\s*kwh\s+([\d.]+)\s*[¢\u00a2c]",
        re.I,
    )
    _OFF_PEAK_ENERGY_RE = re.compile(
        r"off-peak energy per month,\s*per\s*kwh\s+([\d.]+)\s*[¢\u00a2c]",
        re.I,
    )
    _DISCOUNT_ENERGY_RE = re.compile(
        r"discount energy per month,\s*per\s*kwh\s+([\d.]+)\s*[¢\u00a2c]",
        re.I,
    )
    _MINIMUM_BILL_RE = re.compile(r"monthly minimum bill of \$([\d.]+)", re.I)
    _STANDBY_CHARGE_RE = re.compile(
        r"Standby Charge of \$([\d.]+)\s*per\s*kW\s*per\s*month",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        if "leaf no." not in lowered:
            return False
        # RSC and DEP leaf-670 (Residential Solar Choice) — historical and current versions supported
        if family_key == "nc-progress-leaf-670" and ProgressCurrentLeafBridgeProfile._is_current_progress_pdf(doc):
            return False
        if family_key in {"nc-carolinas-rider-rsc", "nc-progress-leaf-670"}:
            return "rider rsc" in lowered and "residential solar choice" in lowered
        # nmb and nsc require current-PDF path (they have a distinct current-leaf-only structure)
        if not CarolinasCurrentLeafBridgeProfile._is_current_carolinas_pdf(doc):
            return False
        if family_key == "nc-carolinas-rider-nmb":
            return "rider nmb" in lowered and "net metering bridge" in lowered
        if family_key == "nc-carolinas-rider-nsc":
            return "rider nsc" in lowered and "non-residential solar choice" in lowered
        return False

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        family_key = (doc.get("family_key") or "").lower()
        lowered = text.lower()
        score = 0.87
        if family_key == "nc-carolinas-rider-nmb" and "net excess energy credit" in lowered:
            score += 0.05
        if family_key == "nc-carolinas-rider-nsc" and "monthly credit for net excess energy" in lowered:
            score += 0.05
        if family_key in {"nc-carolinas-rider-rsc", "nc-progress-leaf-670"}:
            if "net excess energy credit" in lowered:
                score += 0.05
            if "non-bypassable charge" in lowered:
                score += 0.03
            if "grid access fee" in lowered:
                score += 0.02
        if "minimum bill" in lowered or "standby charge" in lowered:
            score += 0.02
        return min(score, 0.97)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        family_key = (doc.get("family_key") or "").lower()
        charges: list[ExtractedCharge] = []
        if family_key == "nc-carolinas-rider-nmb":
            if match := self._NET_EXCESS_CREDIT_RE.search(text):
                charges.append(self._charge("adjustment", "Net Excess Energy Credit", float(match.group(1)) / 100.0, "$/kWh"))
            if match := self._NON_BYPASSABLE_RE.search(text):
                charges.append(self._charge("fixed", "Non-Bypassable Charge", float(match.group(1)), "$/kW-month"))
            if match := self._MINIMUM_BILL_RE.search(text):
                charges.append(self._charge("fixed", "Minimum Bill", float(match.group(1)), "$/month"))
        elif family_key == "nc-carolinas-rider-nsc":
            if match := self._MONTHLY_CREDIT_RE.search(text):
                charges.append(self._charge("adjustment", "Monthly Credit for Net Excess Energy", float(match.group(1)), "$/kWh"))
            if match := self._STANDBY_CHARGE_RE.search(text):
                charges.append(self._charge("fixed", "Standby Charge", float(match.group(1)), "$/kW-month"))
        elif family_key in {"nc-carolinas-rider-rsc", "nc-progress-leaf-670"}:
            charges.extend(self._extract_rsc(text))
        return self._dedupe(charges)

    def _extract_rsc(self, text: str) -> list[ExtractedCharge]:
        """Extract Rider RSC (Residential Solar Choice) charges."""
        charges: list[ExtractedCharge] = []
        if match := self._NET_EXCESS_CREDIT_RE.search(text):
            charges.append(self._charge("adjustment", "Net Excess Energy Credit", float(match.group(1)) / 100.0, "$/kWh"))
        if match := self._NON_BYPASSABLE_RE.search(text):
            charges.append(self._charge("fixed", "Non-Bypassable Charge", float(match.group(1)), "$/kW-month"))
        if match := self._GRID_ACCESS_FEE_RE.search(text):
            charges.append(self._charge("fixed", "Grid Access Fee", float(match.group(1)), "$/kW-month"))
        if match := self._ON_PEAK_ENERGY_RE.search(text):
            charges.append(self._charge("energy", "On-Peak Energy", float(match.group(1)) / 100.0, "$/kWh", tou_period="on_peak"))
        if match := self._OFF_PEAK_ENERGY_RE.search(text):
            charges.append(self._charge("energy", "Off-Peak Energy", float(match.group(1)) / 100.0, "$/kWh", tou_period="off_peak"))
        if match := self._DISCOUNT_ENERGY_RE.search(text):
            charges.append(self._charge("energy", "Discount Energy", float(match.group(1)) / 100.0, "$/kWh", tou_period="super_off_peak"))
        if match := self._MINIMUM_BILL_RE.search(text):
            charges.append(self._charge("fixed", "Minimum Bill", float(match.group(1)), "$/month"))
        return charges

    @staticmethod
    def _charge(
        charge_type: str,
        charge_label: str,
        rate_value: float,
        rate_unit: str,
        tou_period: str | None = None,
    ) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=tou_period,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )

    @staticmethod
    def _dedupe(charges: list[ExtractedCharge]) -> list[ExtractedCharge]:
        deduped: list[ExtractedCharge] = []
        seen: set[tuple[str, str, float, str]] = set()
        for charge in charges:
            key = (
                charge.charge_type,
                charge.charge_label,
                round(float(charge.rate_value), 6),
                charge.rate_unit or "",
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(charge)
        return deduped


@dataclass
class CarolinasFuelCostAdjRiderProfile:
    """Profile for DEC Fuel Cost Adjustment Rider (FCAR) sheets.

    Extracts:
    - Base Fuel Cost (¢/kWh) — single value embedded in all rate schedules
    - Fuel Cost Adjustment Factor (¢/kWh) — one per customer class:
        Residential, General Service and Lighting, Industrial
    """

    name: str = "carolinas_fuel_cost_adj_rider"
    _SUPPORTED_FAMILIES = {"nc-carolinas-rider-fcar"}

    # "2.3182 ¢ per kilowatt hour" or "2.3182¢/kWh"
    # The phrase "is X.XXXX ¢ per kilowatt hour" may be on the next line after "Base Fuel Cost"
    # Note: pdfplumber sometimes decodes ¢ (U+00A2) as:
    #   U+FFFD (replacement char '\ufffd'), or garbled OCR chars (£, f, ff, 0, etc.)
    # Also allow "kilowatt-\nhour" (hyphenated word wrap across lines)
    # The garbled-OCR fallback accepts any 1-4 non-space chars (digit or non-digit) before /kWh,
    # since bad PDF encodings produce "0/kWh", "£/kWh", "ff/kWh", etc.
    _CENTS_UNIT = r"(?:¢|\ufffd|\S{0,4})\s*(?:per\s+kilowatt[\s\-]?\s*hour|/\s*kwh)"
    _BASE_FUEL_COST_RE = re.compile(
        r"base\s+fuel\s+cost[\s\S]{0,200}?(-?[\d]+\.[\d]+)\s*" + _CENTS_UNIT,
        re.I,
    )

    # "Fuel Cost Adjustment Factor: -0.6177 ¢/kWh"
    # Matches the final per-class factor line (the billable output, not the components).
    # Note: avoid [:\-]? optional separator because the '-' would consume the sign of
    # a negative value. Use [: ]? instead so the sign stays in the capture group.
    _CLASS_FACTOR_RE = re.compile(
        r"fuel\s+cost\s+adjustment\s+factor\s*[: ]?\s*(-?[\d]+\.[\d]+)\s*" + _CENTS_UNIT,
        re.I,
    )

    # Class header anchors — used to determine which factor belongs to which class.
    # Headers appear as:
    #   "RESIDENTIAL SERVICE September 1, 2016"  (historical, month follows)
    #   "RESIDENTIAL SERVICE Fuel and Fuel Related Costs" (2024+, "Fuel" follows)
    #   "Residential Service —"  (with em-dash)
    _MONTH_OR_FUEL = r"(?:september|october|november|december|january|february|march|april|may|june|july|august|\d|fuel)"
    _RESIDENTIAL_HEADER_RE = re.compile(
        r"residential\s+service(?:\s*[—\-]\s*|\s+" + _MONTH_OR_FUEL + r"|$)",
        re.I | re.M,
    )
    _GS_LIGHTING_HEADER_RE = re.compile(
        r"general\s+service\s+and\s+lighting(?:\s*[—\-]\s*|\s+" + _MONTH_OR_FUEL + r")",
        re.I,
    )
    _INDUSTRIAL_HEADER_RE = re.compile(
        r"industrial\s+service(?:\s*[—\-]\s*|\s+" + _MONTH_OR_FUEL + r"|$)",
        re.I | re.M,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key != "nc-carolinas-rider-fcar":
            return False
        lowered = text.lower()
        has_unit = (
            "¢" in text or "\ufffd" in text
            or "c/kwh" in lowered or "per kilowatt" in lowered
            or "/kwh" in lowered or "per kwh" in lowered or "perkwh" in lowered
        )
        # Standard Leaf 60 tariff format
        if "fuel cost adjustment" in lowered and has_unit:
            return True
        # Annual application format: "fuel and fuel-related costs factors ... Residential - X.XXXX¢ per kWh"
        if "fuel and fuel-related" in lowered and ("residential" in lowered) and has_unit:
            return True
        return False

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.88
        if "base fuel cost" in lowered:
            score += 0.04
        if "fuel cost adjustment factor" in lowered:
            score += 0.04
        if "residential service" in lowered and "general service" in lowered:
            score += 0.02
        return min(score, 0.98)

    # 2024+ format: "Residential: 2.6287¢ per kilowatt-hour, General Service/Lighting: 2.2596¢ ..."
    _BASE_RES_RE = re.compile(
        r"residential\s*:\s*(-?[\d]+\.[\d]+)\s*" + _CENTS_UNIT, re.I
    )
    _BASE_GS_RE = re.compile(
        r"general\s+service\s*/?\s*lighting\s*:\s*(-?[\d]+\.[\d]+)\s*" + _CENTS_UNIT, re.I
    )
    _BASE_IND_RE = re.compile(
        r"industrial\s*:\s*(-?[\d]+\.[\d]+)\s*" + _CENTS_UNIT, re.I
    )

    # Annual application format: "composite fuel and fuel-related costs factors" section
    # "Residential - 1.7014¢ per kWh" — note ¢ may be garbled as Ã or other OCR artifact
    # _APP_CLASS_RE is more permissive: matches "X.XXXX<anything0-4chars> per kWh"
    # "composite fuel and fuel-related costs factors" — OCR may mangle "fuel" as "ruel"
    # and "related" as "relaled" etc. Use permissive pattern: composite + costs/factors keywords
    _APP_COMPOSITE_HEADER_RE = re.compile(
        r"composite\s+\w+\s+and\s+\w+[- ]\w+\s+costs?\s+factors?",
        re.I,
    )
    _APP_CLASS_RE = re.compile(
        r"(Residential|Commercial|Industrial|General Service)\s*[-–]\s*([\d]+\.[\d]+)\s*\S{0,4}\s*per\s*kWh",
        re.I,
    )

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []

        # Try 2024+ per-class inline Base Fuel Cost format first
        base_section_end = 0
        base_m = self._BASE_FUEL_COST_RE.search(text)
        if base_m:
            base_section_window = text[base_m.start(): base_m.start() + 400]
            res_m = self._BASE_RES_RE.search(base_section_window)
            gs_m = self._BASE_GS_RE.search(base_section_window)
            ind_m = self._BASE_IND_RE.search(base_section_window)
            if res_m or gs_m or ind_m:
                # Multi-class format — emit one charge per class
                for label, m in [
                    ("Residential Base Fuel Cost", res_m),
                    ("General Service and Lighting Base Fuel Cost", gs_m),
                    ("Industrial Base Fuel Cost", ind_m),
                ]:
                    if m:
                        raw = float(m.group(1))
                        charges.append(ExtractedCharge(
                            charge_type="adjustment",
                            charge_label=label,
                            rate_value=raw / 100.0,
                            rate_unit="$/kWh",
                            season="all_year",
                            tou_period=None,
                            tier_min=None,
                            tier_max=None,
                            source_snippet=m.group(0)[:120],
                            confidence_score=0.95,
            ))
                base_section_end = base_m.start() + 400
            else:
                # Classic single-value format
                raw = float(base_m.group(1))
                charges.append(ExtractedCharge(
                    charge_type="adjustment",
                    charge_label="Base Fuel Cost",
                    rate_value=raw / 100.0,
                    rate_unit="$/kWh",
                    season="all_year",
                    tou_period=None,
                    tier_min=None,
                    tier_max=None,
                    source_snippet=base_m.group(0)[:120],
                    confidence_score=0.95,
            ))
                base_section_end = base_m.end()

        # Per-class Fuel Cost Adjustment Factors
        # Strategy: find each class header, then find the next "Fuel Cost Adjustment Factor" line
        class_specs = [
            ("Residential Fuel Cost Adjustment Factor", self._RESIDENTIAL_HEADER_RE),
            ("General Service and Lighting Fuel Cost Adjustment Factor", self._GS_LIGHTING_HEADER_RE),
            ("Industrial Fuel Cost Adjustment Factor", self._INDUSTRIAL_HEADER_RE),
        ]
        seen_values: set[float] = set()
        for label, header_re in class_specs:
            header_m = header_re.search(text)
            if header_m is None:
                continue
            # Look for Fuel Cost Adjustment Factor within 600 chars of the class header
            search_window = text[header_m.start(): header_m.start() + 600]
            factor_m = self._CLASS_FACTOR_RE.search(search_window)
            if factor_m is None:
                continue
            raw = float(factor_m.group(1))
            # Avoid duplicate values (sometimes Residential and GS share identical factors)
            key = round(raw, 6)
            if key in seen_values:
                continue
            seen_values.add(key)
            charges.append(ExtractedCharge(
                charge_type="adjustment",
                charge_label=label,
                rate_value=raw / 100.0,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet=factor_m.group(0)[:120],
                confidence_score=0.95,
            ))

        # Annual application format: extract composite factors from the application summary page
        # Targets "composite fuel and fuel-related costs factors" section with
        # "Residential - 1.7014¢ per kWh" style lines (¢ may be garbled by OCR)
        if not charges:
            composite_m = self._APP_COMPOSITE_HEADER_RE.search(text)
            if composite_m:
                # Search within 800 chars after the composite header
                window = text[composite_m.start(): composite_m.start() + 800]
                seen_app: set[str] = set()
                for m in self._APP_CLASS_RE.finditer(window):
                    class_name = m.group(1).strip().title()
                    raw = float(m.group(2))
                    # Deduplicate by label (first match wins = Version A / primary rate)
                    if class_name in seen_app:
                        continue
                    seen_app.add(class_name)
                    label = f"{class_name} Composite Fuel Factor"
                    charges.append(ExtractedCharge(
                        charge_type="adjustment",
                        charge_label=label,
                        rate_value=raw / 100.0,
                        rate_unit="$/kWh",
                        season="all_year",
                        tou_period=None,
                        tier_min=None,
                        tier_max=None,
                        source_snippet=m.group(0)[:120],
                        confidence_score=0.95,
            ))

        return charges


@dataclass
class CarolinasFlatFeeRiderProfile:
    """Profile for flat per-month or per-block riders for both DEC and DEP companies."""

    name: str = "carolinas_flat_fee_rider"
    _SUPPORTED_FAMILIES = {
        "nc-carolinas-rider-car",   # CAR — Carolinas Carbon Offset Program  ($4/month/block)
        "nc-carolinas-rider-ed",    # ED  — EV Managed Charging Program      ($19.99/month)
        "nc-carolinas-rider-pm",    # PM  — Power Manager Load Control Rider
        # EB (EnergyWise for Business) is a per-load incentive program, not a flat-fee rider
        # It pays customers $50-$135 per enrolled load control device — needs its own profile
        "nc-progress-leaf-644",     # COP — Carbon Offset Program ($4/block/month)
        "nc-progress-leaf-666",     # GR  — Go Renewable ($4/block of REC/month)
        "nc-progress-leaf-718",     # CAP — Customer Assistance Program Credit ($42/month fixed credit)
    }
    # DEC variant: "$X per month per block"
    _PER_MONTH_BLOCK_RE = re.compile(
        r"\$\s*([\d.]+)\s+per\s+month\s+per\s+block",
        re.I,
    )
    # DEP variant: "$X per block [of ...] per month"
    _PER_BLOCK_MONTH_RE = re.compile(
        r"\$\s*([\d.]+)\s+per\s+block(?:\s+of\s+[^$\n]{0,60})?\s+per\s+month",
        re.I,
    )
    _PER_MONTH_RE = re.compile(
        r"(?:subscription\s+rate|monthly\s+(?:rate|charge|fee))[^\$\n]{0,60}\$\s*([\d.]+)\s+per\s+month",
        re.I,
    )
    _DOLLAR_PER_MONTH_RE = re.compile(
        r"\$\s*([\d.]+)\s+per\s+month",
        re.I,
    )
    # "monthly bill credit is $42" or "monthly credit is $42"
    _MONTHLY_CREDIT_RE = re.compile(
        r"monthly\s+(?:bill\s+)?credit\s+is\s+\$\s*([\d.]+)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        lowered = text.lower()
        # CAP-style: "monthly bill credit" without "per month"
        if "monthly bill credit" in lowered or "monthly credit" in lowered:
            return True
        return "per month" in lowered and ("rider" in lowered or "program" in lowered)

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        lowered = text.lower()
        score = 0.88
        if "per month per block" in lowered:
            score += 0.05
        if "availability" in lowered and ("north carolina" in lowered or "nc" in lowered):
            score += 0.02
        return min(score, 0.98)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        family_key = (doc.get("family_key") or "").lower()

        # Per-block monthly fee — DEC variant: "$X per month per block"
        if m := self._PER_MONTH_BLOCK_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Monthly Fee per Block",
                rate_value=float(m.group(1)),
                rate_unit="$/block-month",
                season="all_year",
                tou_period=None, tier_min=None, tier_max=None,
                source_snippet=m.group(0)[:100],
                confidence_score=0.95,
            ))
            return charges

        # Per-block monthly fee — DEP variant: "$X per block [of ...] per month"
        if m := self._PER_BLOCK_MONTH_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Monthly Fee per Block",
                rate_value=float(m.group(1)),
                rate_unit="$/block-month",
                season="all_year",
                tou_period=None, tier_min=None, tier_max=None,
                source_snippet=m.group(0)[:100],
                confidence_score=0.95,
            ))
            return charges

        # Explicit subscription/monthly rate
        if m := self._PER_MONTH_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Monthly Program Rate",
                rate_value=float(m.group(1)),
                rate_unit="$/month",
                season="all_year",
                tou_period=None, tier_min=None, tier_max=None,
                source_snippet=m.group(0)[:100],
                confidence_score=0.95,
            ))
            return charges

        # CAP-style: "monthly bill credit is $42"
        if m := self._MONTHLY_CREDIT_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="credit",
                charge_label="Monthly Bill Credit",
                rate_value=float(m.group(1)),
                rate_unit="$/month",
                season="all_year",
                tou_period=None, tier_min=None, tier_max=None,
                source_snippet=m.group(0)[:100],
                confidence_score=0.95,
            ))
            return charges

        # Generic $ X per month fallback
        if m := self._DOLLAR_PER_MONTH_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Monthly Fee",
                rate_value=float(m.group(1)),
                rate_unit="$/month",
                season="all_year",
                tou_period=None, tier_min=None, tier_max=None,
                source_snippet=m.group(0)[:100],
                confidence_score=0.95,
            ))
        return charges

@dataclass
class ProgressMediumGeneralServiceProfile:
    """Profile for DEP Medium General Service (Leaf 524, 525)."""

    name: str = "progress_mgs"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-524", "nc-progress-leaf-525"}

    _CUSTOMER_CHARGE_RE = re.compile(
        r"Basic\s*Customer\s*Charge:\s*[\$S]?([\d,]+\.?\d*)",
        re.I,
    )
    _DEMAND_CHARGE_RE = re.compile(
        r"Billing\s*Demand:\s*[\$S]?([\d,]+\.?\d*)\s*per\s*kW",
        re.I,
    )
    _DEMAND_CHARGE_BLOCK_RE = re.compile(
        r"[\$S]?([\d,]+\.?\d*)\s*per\s*kW\s*for\s*(all\s+Base|all\s+Mid-Peak|all\s+On-Peak)\s*Billing\s*Demand",
        re.I,
    )
    _ENERGY_CHARGE_RE = re.compile(
        r"([\d,]+\.?\d*)\s*[¢\u00a2^]\s*per\s*kWh\s*for\s*all\s*kWh",
        re.I,
    )
    _TOU_ENERGY_CHARGE_RE = re.compile(
        r"([\d,]+\.?\d*)\s*[¢\u00a2^]\s*per\s*(On-Peak|Off-Peak|Discount|Super\s*Off-Peak)\s*kWh",
        re.I,
    )
    _THREE_PHASE_RE = re.compile(
        r"single-phase\s+service\s+plus\s*[\$S]?([\d,]+\.?\d*)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key in self._SUPPORTED_FAMILIES:
            return True
        lowered = text.lower()
        return "schedule mgs" in lowered or "medium general service" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.95
        family_key = (doc.get("family_key") or "").lower()
        if family_key in self._SUPPORTED_FAMILIES:
            score += 0.02
        if "billing demand" in text.lower():
            score += 0.01
        return min(score, 0.99)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        
        # Customer Charge
        if match := self._CUSTOMER_CHARGE_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Basic Customer Charge",
                rate_value=float(match.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))
            
        # Three-Phase Surcharge
        if match := self._THREE_PHASE_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Three-Phase Surcharge",
                rate_value=float(match.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))

        # Demand Charges (Blocked/TOU)
        for match in self._DEMAND_CHARGE_BLOCK_RE.finditer(text):
            qualifier = match.group(2).strip().title()
            charges.append(ExtractedCharge(
                charge_type="demand",
                charge_label=f"Billing Demand - {qualifier}",
                rate_value=float(match.group(1).replace(",", "")),
                rate_unit="$/kW",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))
            
        # Basic Demand (if no blocked demand found)
        if not any("Billing Demand -" in c.charge_label for c in charges):
            if match := self._DEMAND_CHARGE_RE.search(text):
                charges.append(ExtractedCharge(
                    charge_type="demand",
                    charge_label="Billing Demand",
                    rate_value=float(match.group(1).replace(",", "")),
                    rate_unit="$/kW",
                    season="all_year",
                    source_snippet=match.group(0)[:100],
                    confidence_score=0.95,
            ))

        # Energy Charges (Flat)
        if match := self._ENERGY_CHARGE_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="energy",
                charge_label="Energy Charge",
                rate_value=float(match.group(1).replace(",", "")) / 100.0,
                rate_unit="$/kWh",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))
            
        # TOU Energy Charges
        for match in self._TOU_ENERGY_CHARGE_RE.finditer(text):
            period = match.group(2).strip().lower().replace(" ", "_")
            charges.append(ExtractedCharge(
                charge_type="energy",
                charge_label=f"Energy Charge - {match.group(2).strip().title()}",
                rate_value=float(match.group(1).replace(",", "")) / 100.0,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=period,
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))

        return charges


@dataclass
class ProgressResidentialTOUEVProfile:
    """Profile for DEP Residential TOU-EV (Leaf 504)."""

    name: str = "progress_res_tou_ev"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-504"}

    _CUSTOMER_CHARGE_RE = re.compile(
        r"Basic\s*Customer\s*Charge:\s*[\$S]?([\d,]+\.?\d*)",
        re.I,
    )
    _ENERGY_CHARGE_RE = re.compile(
        r"([\d,]+\.?\d*)\s*[¢\u00a2^]\s*per\s*(Standard|Discount)\s*kWh",
        re.I,
    )
    _THREE_PHASE_RE = re.compile(
        r"single-phase\s+service\s+plus\s*[\$S]?([\d,]+\.?\d*)",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key in self._SUPPORTED_FAMILIES:
            return True
        lowered = text.lower()
        return "schedule r-tou-ev" in lowered or "residential service pilot" in lowered

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        score = 0.95
        family_key = (doc.get("family_key") or "").lower()
        if family_key in self._SUPPORTED_FAMILIES:
            score += 0.02
        if "discount period" in text.lower():
            score += 0.01
        return min(score, 0.99)

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        
        # Customer Charge
        if match := self._CUSTOMER_CHARGE_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Basic Customer Charge",
                rate_value=float(match.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))
            
        # Three-Phase Surcharge
        if match := self._THREE_PHASE_RE.search(text):
            charges.append(ExtractedCharge(
                charge_type="fixed",
                charge_label="Three-Phase Surcharge",
                rate_value=float(match.group(1).replace(",", "")),
                rate_unit="$/month",
                season="all_year",
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))

        # TOU Energy Charges
        for match in self._ENERGY_CHARGE_RE.finditer(text):
            period = match.group(2).strip().lower()
            charges.append(ExtractedCharge(
                charge_type="energy",
                charge_label=f"Energy Charge - {match.group(2).strip().title()}",
                rate_value=float(match.group(1).replace(",", "")) / 100.0,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=period,
                source_snippet=match.group(0)[:100],
                confidence_score=0.95,
            ))

        return charges


@dataclass
class ZeroChargeProgramProfile:
    """Profile for procedural/program documents that have zero explicit charges."""

    name: str = "zero_charge_program"
    _SUPPORTED_FAMILIES = {
        "nc-progress-leaf-703",  # Neighborhood Energy Saver Program
        "nc-progress-leaf-641",  # Net Metering NM
        "nc-progress-leaf-707",  # HERP
        "nc-progress-leaf-713",  # REEAD
        "nc-carolinas-rider-cei", # Clean Energy Impact
    }

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        return family_key in self._SUPPORTED_FAMILIES

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        return 0.99

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        return []


@dataclass
class ProgressLargeGeneralServiceRTPProfile:
    """Profile for DEP Large General Service RTP (Leaf 534)."""

    name: str = "progress_lgs_rtp"
    _SUPPORTED_FAMILIES = {"nc-progress-leaf-534"}

    _ADMIN_CHARGE_RE = re.compile(
        r"RTP Administrative Charge:\s*\$?([\d.]+)",
        re.I,
    )
    _ADDER_RE = re.compile(
        r"ADDER\s*=\s*([\d.]+)\s*cents\s*per\s*kWh",
        re.I,
    )
    _PF_ADJ_RE = re.compile(
        r"Power Factor Adjustment(?:[^\$]{0,40})\$?([\d.]+)",
        re.I,
    )
    _TRANSMISSION_WITHOUT_RE = re.compile(
        r"Transmission System[^\$]+without transformation\s*\$?([\d.]+)\s*/\s*kW",
        re.I,
    )
    _TRANSMISSION_WITH_RE = re.compile(
        r"Transmission System[^\$]+with one transformation\s*\$?([\d.]+)\s*/\s*kW",
        re.I,
    )
    _DISTRIBUTION_WITHOUT_RE = re.compile(
        r"Distribution System[^\$]+without transformation\s*\$?([\d.]+)\s*/\s*kW",
        re.I,
    )
    _DISTRIBUTION_WITH_RE = re.compile(
        r"Distribution System[^\$]+with one transformation\s*\$?([\d.]+)\s*/\s*kW",
        re.I,
    )

    def supports(self, doc: dict, text: str) -> bool:
        family_key = (doc.get("family_key") or "").lower()
        if family_key not in self._SUPPORTED_FAMILIES:
            return False
        return "schedule lgs-rtp" in text.lower() or "large general service" in text.lower()

    def score(self, doc: dict, text: str) -> float:
        if not self.supports(doc, text):
            return 0.0
        return 0.95

    def extract(self, doc: dict, text: str) -> list[ExtractedCharge]:
        charges: list[ExtractedCharge] = []
        if match := self._ADMIN_CHARGE_RE.search(text):
            charges.append(self._charge("fixed", "RTP Administrative Charge", float(match.group(1)), "$/month"))
        if match := self._ADDER_RE.search(text):
            charges.append(self._charge("energy", "RTP Adder", float(match.group(1)) / 100.0, "$/kWh"))
        if match := self._PF_ADJ_RE.search(text):
            charges.append(self._charge("adjustment", "Power Factor Adjustment", float(match.group(1)), "$/kVAR"))
        if match := self._TRANSMISSION_WITHOUT_RE.search(text):
            charges.append(self._charge("demand", "Transmission Without Transformation", float(match.group(1)), "$/kW"))
        if match := self._TRANSMISSION_WITH_RE.search(text):
            charges.append(self._charge("demand", "Transmission With Transformation", float(match.group(1)), "$/kW"))
        if match := self._DISTRIBUTION_WITHOUT_RE.search(text):
            charges.append(self._charge("demand", "Distribution Without Transformation", float(match.group(1)), "$/kW"))
        if match := self._DISTRIBUTION_WITH_RE.search(text):
            charges.append(self._charge("demand", "Distribution With Transformation", float(match.group(1)), "$/kW"))
        return charges

    @staticmethod
    def _charge(
        charge_type: str,
        charge_label: str,
        rate_value: float,
        rate_unit: str,
    ) -> ExtractedCharge:
        return ExtractedCharge(
            charge_type=charge_type,
            charge_label=charge_label,
            rate_value=rate_value,
            rate_unit=rate_unit,
            season="all_year",
            tou_period=None,
            tier_min=None,
            tier_max=None,
            source_snippet=charge_label[:100],
            confidence_score=0.92,
        )


class HistoricalRateParserRegistry:
    def __init__(self) -> None:
        self._profiles: list[HistoricalRateParserProfile] = [
            ZeroChargeProgramProfile(),
            ProgressLargeGeneralServiceRTPProfile(),
            ProgressRiderAdjustmentMatrixProfile(),
            ProgressPowerPairPilotProfile(),
            ProgressEnergywiseBusinessProfile(),
            ProgressSolarRebateRiderProfile(),
            ProgressSunSenseSolarRebateProfile(),
            ProgressMeterRelatedOptionalProgramsProfile(),
            ProgressStreetLightingProfile(),
            ProgressTrafficSignalServiceProfile(),
            ProgressStandbyServiceProfile(),
            ProgressFluctuatingLoadRiderProfile(),
            ProgressCustomerAssistanceRecoveryProfile(),
            ProgressStormSecuritizationProfile(),
            ProgressGreenPowerProgramProfile(),
            ProgressDemandResponseAutomationProfile(),
            ProgressLoadControlWinterProfile(),
            ProgressResidentialLoadControlProfile(),
            ProgressIncomeQualifiedLoadControlProfile(),
            ProgressSpecialtyRiderProfile(),
            ProgressCurrentLeafBridgeProfile(),
            ProgressBillingAdjustmentsProfile(),
            ProgressSingleValueRiderProfile(),
            ProgressRecoveryRiderProfile(),
            ProgressManagementEnergyEfficiencyCostRecoveryRiderProfile(),
            ProgressComplianceReportAndCostRecoveryRiderProfile(),
            ProgressResidentialTouProfile(),
            ProgressMediumGeneralServiceProfile(),
            ProgressResidentialTOUEVProfile(),
            ProgressResidentialFlatProfile(),
            CarolinasRiderAdjustmentMatrixProfile(),
            CarolinasSmallCustomerGeneratorProfile(),
            CarolinasNetMeteringRiderProfile(),
            GreenSourceAdvantageRiderProfile(),
            CarolinasEnergyEfficiencyRiderProfile(),
            CarolinasEconomicDevelopmentRiderProfile(),
            CarolinasInterruptibleServiceRiderProfile(),
            CarolinasSolarChoiceRiderProfile(),
            CarolinasLightingScheduleProfile(),
            CarolinasGeneralServiceScheduleProfile(),
            CarolinasScheduleBridgeProfile(),
            CarolinasCurrentLeafBridgeProfile(),
            CarolinasCustomerAssistanceRecoveryProfile(),
            CarolinasNuclearProductionTaxCreditsProfile(),
            CarolinasSingleValueRiderProfile(),
            CarolinasResidentialTouProfile(),
            CarolinasResidentialFlatProfile(),
            CarolinasFuelCostAdjRiderProfile(),
            CarolinasFlatFeeRiderProfile(),
            GenericResidentialProfile(),
        ]

    @staticmethod
    def _build_signals(doc: dict, text: str) -> ParserProfileSignals:
        family_key = (doc.get("family_key") or "").lower()
        company = (doc.get("company") or "").lower()
        title = (doc.get("title") or "").lower()
        local_path = str(doc.get("local_path") or "").replace("/", "\\").lower()
        lowered = text.lower()
        leaf_match = re.search(r"leaf-(\d+)", family_key)
        leaf_no = leaf_match.group(1) if leaf_match else None
        has_tou_terms = any(
            token in lowered for token in ("on-peak", "off-peak", "time-of-use", "time of use", "critical peak")
        )
        return ParserProfileSignals(
            family_key=family_key,
            company=company,
            title=title,
            text_lower=lowered,
            is_current_progress_pdf="\\raw\\nc\\progress\\" in local_path,
            is_current_carolinas_pdf="\\raw\\nc\\carolinas\\" in local_path,
            leaf_no=leaf_no,
            has_summary_text="summary of rider adjustments" in lowered or "summary of rider adjustments" in title,
            has_tou_terms=has_tou_terms,
            has_discount_term="discount" in lowered or "super off-peak" in lowered,
            has_demand_charge_term="demand charge" in lowered,
            has_progress_company_text="progress" in detect_duke_company(lowered),
            has_carolinas_company_text="carolinas" in detect_duke_company(lowered),
            has_rs_marker=any(
                token in lowered
                for token in (
                    "schedule rs",
                    "rate schedule rs",
                    "residential schedules rs",
                    "schedule es",
                    "energy star",
                    "schedule re",
                    "schedule ret",
                    "schedule bc",
                    "building construction service",
                )
            ),
            has_flat_rate_markers=any(
                token in lowered for token in ("basic customer charge", "basic facilities charge")
            ) and "energy charge" in lowered,
            has_page_bounds=doc.get("start_page") is not None,
        )

    def build_signals(self, doc: dict, text: str) -> ParserProfileSignals:
        return self._build_signals(doc, text)

    def get_profile(self, profile_name: str) -> HistoricalRateParserProfile | None:
        for profile in self._profiles:
            if profile.name == profile_name:
                return profile
        return None

    @staticmethod
    def _score_profile(profile_name: str, signals: ParserProfileSignals) -> tuple[float, tuple[str, ...]]:
        reasons: list[str] = []

        if profile_name == "zero_charge_program":
            if signals.family_key not in {
                "nc-progress-leaf-703",
                "nc-progress-leaf-641",
                "nc-progress-leaf-707",
                "nc-progress-leaf-713",
                "nc-carolinas-rider-cei",
            }:
                return 0.0, ()
            return 0.99, ("zero_charge_program_explicit_match",)

        if profile_name == "progress_lgs_rtp":
            if signals.family_key != "nc-progress-leaf-534":
                return 0.0, ()
            if "schedule lgs-rtp" in signals.text_lower or "large general service" in signals.text_lower:
                return 0.96, ("lgs_rtp", "schedule_match")
            return 0.0, ()

        if profile_name == "progress_rider_adjustment_matrix":
            if signals.family_key != "nc-progress-leaf-600" or not signals.has_summary_text:
                return 0.0, ()
            reasons.extend(("family=leaf600", "summary_text"))
            return 0.96, tuple(reasons)

        if profile_name == "progress_recovery_rider":
            if signals.family_key != "nc-progress-rider-recoveryrider":
                return 0.0, ()
            lowered = signals.text_lower
            title = signals.title
            if "recovery rider" not in lowered and "recovery rider" not in title:
                return 0.0, ()
            if "monthly rate" not in lowered and "cost recovery" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("recovery_rider")
            if "cost recovery" in lowered:
                score += 0.03
                reasons.append("cost_recovery_rider")
            if "monthly rate" in lowered:
                score += 0.03
                reasons.append("monthly_rate")
            if "applicability" in lowered:
                score += 0.02
                reasons.append("applicability")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_management_energy_efficiency_cost_recovery_rider":
            if signals.family_key != "nc-progress-rider-managementandenergyefficiencycostrecoveryrider":
                return 0.0, ()
            lowered = signals.text_lower
            title = signals.title
            if "management and energy efficiency cost recovery rider" not in lowered and "management and energy efficiency cost recovery rider" not in title:
                return 0.0, ()
            if "monthly rate" not in lowered and "cost recovery" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("management_energy_efficiency_cost_recovery_rider")
            if "cost recovery" in lowered:
                score += 0.03
                reasons.append("cost_recovery_rider")
            if "monthly rate" in lowered:
                score += 0.03
                reasons.append("monthly_rate")
            if "applicability" in lowered:
                score += 0.02
                reasons.append("applicability")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_compliance_report_and_cost_recovery_rider":
            if signals.family_key != "nc-progress-rider-compliancereportandcostrecoveryrider":
                return 0.0, ()
            lowered = signals.text_lower
            title = signals.title
            if "compliance report and cost recovery rider" not in lowered and "compliance report and cost recovery rider" not in title:
                return 0.0, ()
            if "monthly rate" not in lowered and "cost recovery" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("compliance_report_and_cost_recovery_rider")
            if "cost recovery" in lowered:
                score += 0.03
                reasons.append("cost_recovery_rider")
            if "monthly rate" in lowered:
                score += 0.03
                reasons.append("monthly_rate")
            if "applicability" in lowered:
                score += 0.02
                reasons.append("applicability")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_residential_tou":
            if signals.family_key not in {"nc-progress-leaf-502", "nc-progress-leaf-503", "nc-progress-leaf-504"}:
                return 0.0, ()
            if not signals.has_tou_terms:
                return 0.0, ()
            score = 0.8
            reasons.extend(("progress_tou_family", "tou_terms"))
            if signals.has_discount_term:
                score += 0.08
                reasons.append("discount_terms")
            if signals.has_demand_charge_term:
                score += 0.04
                reasons.append("demand_terms")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_residential_flat":
            family_match = signals.family_key in {"nc-progress-leaf-500", "nc-progress-leaf-505"}
            company_match = signals.company == "progress" or signals.has_progress_company_text
            has_flat_rate_signal = signals.has_flat_rate_markers or (
                "basic customer charge" in signals.text_lower and "per kwh" in signals.text_lower
            )
            if not family_match or not company_match or not has_flat_rate_signal or signals.has_tou_terms:
                return 0.0, ()
            score = 0.84
            reasons.extend(("progress_family", "flat_rate_markers"))
            if signals.family_key == "nc-progress-leaf-500":
                score += 0.08
                reasons.append("leaf500")
            if "for all kwh" in signals.text_lower:
                score += 0.03
                reasons.append("all_kwh")
            return min(score, 0.95), tuple(reasons)

        if profile_name == "progress_current_leaf_bridge":
            family_tokens = {
                "nc-progress-leaf-501": ("leaf501_r_toud", "r-toud"),
                "nc-progress-leaf-520": ("leaf520_sgs", "schedule sgs"),
                "nc-progress-leaf-521": ("leaf521_sgs_toue", "schedule sgs-toue"),
                "nc-progress-leaf-532": ("leaf532_lgs", "schedule lgs"),
                "nc-progress-leaf-533": ("leaf533_lgs_tou", "schedule lgs-tou"),
                "nc-progress-leaf-535": ("leaf535_hp", "schedule hp"),
                "nc-progress-leaf-674": ("leaf674_rider_ps", "rider ps"),
            }
            family_reason, marker = family_tokens.get(signals.family_key, (None, None))
            # Allow compliance-bundle docs (has_page_bounds) in addition to current PDFs.
            if not family_reason or (not signals.is_current_progress_pdf and not signals.has_page_bounds):
                return 0.0, ()
            text_match = marker in signals.text_lower
            if signals.family_key == "nc-progress-leaf-501":
                text_match = text_match or ("time-of-use" in signals.text_lower and "demand" in signals.text_lower)
            elif signals.family_key == "nc-progress-leaf-520":
                text_match = text_match or "small general service" in signals.text_lower
            elif signals.family_key == "nc-progress-leaf-521":
                text_match = text_match or ("small general service" in signals.text_lower and "time-of-use" in signals.text_lower)
            elif signals.family_key == "nc-progress-leaf-532":
                text_match = text_match or "large general service" in signals.text_lower
            elif signals.family_key == "nc-progress-leaf-533":
                text_match = text_match or ("large general service" in signals.text_lower and "time-of-use" in signals.text_lower)
            elif signals.family_key == "nc-progress-leaf-535":
                text_match = text_match or "high load factor" in signals.text_lower or "high power" in signals.text_lower
            elif signals.family_key == "nc-progress-leaf-674":
                text_match = text_match or "partial requirements" in signals.text_lower
            if not text_match:
                return 0.0, ()
            score = 0.86
            reasons.extend(("current_progress_pdf", family_reason))
            if signals.has_demand_charge_term:
                score += 0.02
                reasons.append("demand_terms")
            if signals.family_key == "nc-progress-leaf-501" and signals.has_tou_terms:
                score += 0.05
                reasons.append("tou_terms")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_specialty_rider":
            family_tokens = {
                "nc-progress-leaf-654": ("leaf654_rider_nfs", "rider nfs", "non-firm standby"),
                "nc-progress-leaf-655": ("leaf655_rider_llc", "rider llc", "large load curtailable"),
                "nc-progress-leaf-668": ("leaf668_rider_nsc", "rider nsc", "non-residential solar choice"),
                "nc-progress-leaf-669": ("leaf669_rider_nmb", "rider nmb", "net metering bridge"),
                "nc-progress-leaf-670": ("leaf670_rider_rsc", "rider rsc", "residential solar choice"),
            }
            family_info = family_tokens.get(signals.family_key)
            if not family_info or not signals.is_current_progress_pdf:
                return 0.0, ()
            family_reason, marker_a, marker_b = family_info
            if marker_a not in signals.text_lower or marker_b not in signals.text_lower:
                return 0.0, ()
            score = 0.87
            reasons.extend(("current_progress_pdf", family_reason))
            if "monthly rate" in signals.text_lower:
                score += 0.02
                reasons.append("monthly_rate")
            if "credit" in signals.text_lower:
                score += 0.02
                reasons.append("credit_terms")
            if "demand" in signals.text_lower:
                score += 0.02
                reasons.append("demand_terms")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_energywise_business":
            if signals.family_key not in {"nc-progress-leaf-706", "nc-carolinas-rider-eb"}:
                return 0.0, ()
            if "energywise for business" not in signals.text_lower or "control credits" not in signals.text_lower:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf706_ewb", "control_credits"))
            if "summer cycling" in signals.text_lower or "non-winter cycling" in signals.text_lower:
                score += 0.03
                reasons.append("non_winter_cycling" if "non-winter cycling" in signals.text_lower else "summer_cycling")
            if "bring your own kw" in signals.text_lower:
                score += 0.02
                reasons.append("bring_your_own_kw")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_sunsense_solar_rebate":
            if signals.family_key != "nc-progress-leaf-716":
                return 0.0, ()
            lowered = signals.text_lower
            if "sunsense solar rebate" not in lowered or "ssr credit" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf716_ssr", "ssr_credit"))
            if "one-time participation payment" in lowered:
                score += 0.03
                reasons.append("participation_payment")
            if "early termination charge" in lowered:
                score += 0.02
                reasons.append("termination_charge")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_solar_rebate_rider":
            if signals.family_key != "nc-progress-leaf-663":
                return 0.0, ()
            lowered = signals.text_lower
            if "solar rebate rider srr" not in lowered or "per watt" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf663_srr", "per_watt_terms"))
            if "rebate payment" in lowered:
                score += 0.05
                reasons.append("rebate_payment")
            if "ac nameplate" in lowered:
                score += 0.03
                reasons.append("ac_nameplate")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_meter_related_optional_programs":
            lowered = signals.text_lower
            # DEC Rider MRM — Manually Read Meter Rider (same fee structure as DEP MROP)
            if signals.family_key == "nc-carolinas-rider-mrm":
                if "rider mrm" not in lowered or "manually read meter" not in lowered:
                    return 0.0, ()
                score = 0.90
                reasons.extend(("carolinas_mrm", "manually_read_meter_rider"))
                if "initial set-up fee" in lowered:
                    score += 0.03
                    reasons.append("setup_fee")
                if "monthly rate" in lowered:
                    score += 0.02
                    reasons.append("monthly_rate")
                return min(score, 0.98), tuple(reasons)
            if signals.family_key != "nc-progress-leaf-661":
                return 0.0, ()
            if "rider mrop" not in lowered or "meter-related optional programs" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf661_mrop", "meter_optional_programs"))
            if "totalmeter" in lowered:
                score += 0.02
                reasons.append("totalmeter")
            if "energy profiler online" in lowered:
                score += 0.03
                reasons.append("energy_profiler_online")
            if "manually read metering" in lowered:
                score += 0.03
                reasons.append("manually_read_metering")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_standby_service":
            if signals.family_key != "nc-progress-leaf-653":
                return 0.0, ()
            lowered = signals.text_lower
            if "supplementary and firm standby service" not in lowered:
                return 0.0, ()
            if "generation reservation charge" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf653_standby_service", "generation_reservation_charge"))
            if "standby service delivery charge" in lowered:
                score += 0.03
                reasons.append("standby_delivery_charge")
            if "incremental load for the incentive margin" in lowered:
                score += 0.03
                reasons.append("incentive_margin")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_traffic_signal_service":
            if signals.family_key != "nc-progress-leaf-574":
                return 0.0, ()
            lowered = signals.text_lower
            if "schedule tss" not in lowered or "traffic signal" not in lowered:
                return 0.0, ()
            score = 0.88
            reasons.extend(("leaf574_tss", "traffic_signal_schedule"))
            if "monthly rate per signal" in lowered:
                score += 0.05
                reasons.append("per_signal_rate_table")
            if "blinker" in lowered:
                score += 0.03
                reasons.append("blinker_signal")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_street_lighting":
            if signals.family_key not in {"nc-progress-leaf-570", "nc-progress-leaf-571", "nc-progress-leaf-572"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "monthly rate" not in lowered:
                return 0.0, ()
            has_fixture = "per fixture" in lowered or "per customer" in lowered
            if not has_fixture:
                return 0.0, ()
            score = 0.88
            reasons.extend(("street_lighting_family", "monthly_rate"))
            if signals.family_key == "nc-progress-leaf-571" and "schedule sls" in lowered:
                score += 0.05
                reasons.append("sls_schedule")
            elif signals.family_key == "nc-progress-leaf-572" and "schedule slr" in lowered:
                score += 0.05
                reasons.append("slr_schedule")
            elif signals.family_key == "nc-progress-leaf-570" and "schedule als" in lowered:
                score += 0.05
                reasons.append("als_schedule")
            if "led" in lowered:
                score += 0.02
                reasons.append("led_fixture")
            return min(score, 0.96), tuple(reasons)

        if profile_name == "progress_fluctuating_load_rider":
            if signals.family_key != "nc-progress-leaf-650":
                return 0.0, ()
            lowered = signals.text_lower
            if "highly fluctuating or intermittent load" not in lowered or "rider no. 9" not in lowered:
                return 0.0, ()
            score = 0.91
            reasons.extend(("leaf650_rider9", "fluctuating_load"))
            if "per kva" in lowered:
                score += 0.04
                reasons.append("kva_rate")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_customer_assistance_recovery":
            if signals.family_key != "nc-progress-leaf-611":
                return 0.0, ()
            lowered = signals.text_lower
            if "customer assistance recovery rider" not in lowered or "monthly rate" not in lowered:
                return 0.0, ()
            if "$/bill" not in lowered and "$/kwh" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf611_car", "billing_table"))
            if "rate class" in lowered:
                score += 0.03
                reasons.append("rate_class")
            if "general service" in lowered:
                score += 0.03
                reasons.append("general_service")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_storm_securitization":
            if signals.family_key not in {"nc-progress-leaf-613", "nc-progress-leaf-607"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "storm securitization" not in lowered or "monthly rate" not in lowered:
                return 0.0, ()
            if "billing rate" not in lowered and "¢/kwh" not in lowered and "c/kwh" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf613_sts", "billing_rate_table"))
            if "applicable schedules" in lowered:
                score += 0.03
                reasons.append("applicable_schedules")
            if "rate class" in lowered:
                score += 0.03
                reasons.append("rate_class")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_greenpower_program":
            if signals.family_key not in {"nc-progress-leaf-642", "nc-progress-leaf-643"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "per block" not in lowered:
                return 0.0, ()
            if "greenpower program" not in lowered and "renewable rider ren" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(
                (
                    "leaf643_renewable_ren" if signals.family_key == "nc-progress-leaf-643" else "leaf642_greenpower",
                    "per_block",
                )
            )
            if "monthly rate" in lowered:
                score += 0.03
                reasons.append("monthly_rate")
            if "renewable rider ren" in lowered:
                score += 0.02
                reasons.append("renewable_rider")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "progress_demand_response_automation":
            if signals.family_key != "nc-progress-leaf-717":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider dra" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("leaf717_dra")
            if "monthly availability credit" in lowered:
                score += 0.03
                reasons.append("availability_credit")
            if "event performance credit" in lowered:
                score += 0.03
                reasons.append("event_credit")
            if "participant incentive" in lowered:
                score += 0.02
                reasons.append("participant_incentive")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_load_control_winter":
            if signals.family_key != "nc-progress-leaf-714":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider lc-win" not in lowered and "lc-win" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("leaf714_lc_win")
            if "bill credit" in lowered:
                score += 0.05
                reasons.append("bill_credit")
            if "annual bill credit" in lowered:
                score += 0.03
                reasons.append("load_control_winter")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_income_qualified_load_control":
            if signals.family_key != "nc-progress-leaf-725":
                return 0.0, ()
            lowered = signals.text_lower
            if "income-qualified" not in lowered or "load control" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.append("leaf725_riqlc")
            reasons.append("income_qualified_load_control")
            if "payment of incentives" in lowered:
                score += 0.05
                reasons.append("initial_incentive")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_mgs":
            if signals.family_key not in {"nc-progress-leaf-524", "nc-progress-leaf-525"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "medium general service" not in lowered and "schedule mgs" not in lowered:
                return 0.0, ()
            score = 0.92
            reasons.append("mgs_family")
            if "billing demand" in lowered:
                score += 0.03
                reasons.append("billing_demand")
            if "on-peak" in lowered or "off-peak" in lowered:
                score += 0.02
                reasons.append("tou_terms")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_res_tou_ev":
            if signals.family_key != "nc-progress-leaf-504":
                return 0.0, ()
            lowered = signals.text_lower
            if "schedule r-tou-ev" not in lowered and "residential service pilot" not in lowered:
                return 0.0, ()
            score = 0.92
            reasons.append("res_tou_ev_family")
            if "discount period" in lowered:
                score += 0.04
                reasons.append("discount_period")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_residential_load_control":
            if signals.family_key != "nc-progress-leaf-715":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider lc" not in lowered or "load control" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf715_lc", "load_control"))
            if "payment of incentives" in lowered:
                score += 0.05
                reasons.append("payment_of_incentives")
            if "annual incentive" in lowered:
                score += 0.03
                reasons.append("annual_incentive")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_powerpair_pilot":
            if signals.family_key != "nc-progress-leaf-770":
                return 0.0, ()
            if "powerpair" not in signals.text_lower or "incentive" not in signals.text_lower:
                return 0.0, ()
            score = 0.9
            reasons.extend(("leaf770_powerpair", "incentive_terms"))
            if "solar and battery installation" in signals.text_lower:
                score += 0.03
                reasons.append("pilot_terms")
            if "per watt" in signals.text_lower or "per kilowatt hour" in signals.text_lower or "per kwh" in signals.text_lower:
                score += 0.03
                reasons.append("rate_terms")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_billing_adjustments":
            if signals.family_key != "nc-progress-leaf-601":
                return 0.0, ()
            has_leaf_table = "billing adjustment factors" in signals.text_lower and "rider ba" in signals.text_lower
            has_notice_rates = "annual billing adjustments rider ba" in signals.text_lower and (
                "the net changes in the dsm and ee rates" in signals.text_lower
                or "the rate changes associated with dep" in signals.text_lower
            )
            if not has_leaf_table and not has_notice_rates:
                return 0.0, ()
            score = 0.9
            reasons.append("family=leaf601")
            if has_leaf_table:
                reasons.append("billing_adjustment_factors")
            if has_notice_rates:
                reasons.append("ba_notice_rates")
            if "net adjustment" in signals.text_lower:
                score += 0.03
                reasons.append("net_adjustment")
            if "applicable to schedules" in signals.text_lower:
                score += 0.03
                reasons.append("schedule_applicability")
            if "the net changes in the dsm and ee rates" in signals.text_lower:
                score += 0.02
                reasons.append("notice_net_changes")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "progress_single_value_rider":
            _svr_families = {
                "nc-progress-leaf-602", "nc-progress-leaf-603",
                "nc-progress-leaf-604", "nc-progress-leaf-605", "nc-progress-leaf-606",
                "nc-progress-leaf-607", "nc-progress-leaf-608", "nc-progress-leaf-609",
                "nc-progress-leaf-590", "nc-progress-leaf-591", "nc-progress-leaf-592",
                "nc-progress-leaf-610", "nc-progress-leaf-611", "nc-progress-leaf-640",
                "nc-progress-leaf-641", "nc-progress-leaf-646", "nc-progress-leaf-647",
                "nc-progress-leaf-648", "nc-progress-leaf-649", "nc-progress-leaf-651",
                "nc-progress-leaf-652", "nc-progress-leaf-655", "nc-progress-leaf-656",
                "nc-progress-leaf-657", "nc-progress-leaf-662", "nc-progress-leaf-663",
                # DEC equivalent riders
                "nc-carolinas-rider-rdm", "nc-carolinas-rider-pim", "nc-carolinas-rider-edit4",
                "nc-carolinas-rider-sts", "nc-carolinas-rider-cei",
                "nc-progress-leaf-700", "nc-progress-leaf-702", "nc-progress-leaf-705",
                "nc-progress-leaf-708", "nc-progress-leaf-719", "nc-progress-leaf-722",
                "nc-progress-leaf-724",
            }
            if signals.family_key not in _svr_families:
                return 0.0, ()
            has_rate = "monthly rate" in signals.text_lower or "rider" in signals.text_lower
            has_kwh = ProgressSingleValueRiderProfile._has_kwh_rate_marker(signals.text_lower)
            has_leaf = "leaf no." in signals.text_lower or "rider " in signals.text_lower
            relaxed_family = signals.family_key in ProgressSingleValueRiderProfile._RELAXED_SELECTION_FAMILIES
            if relaxed_family:
                if not has_rate or not (has_kwh or has_leaf):
                    return 0.0, ()
            elif not has_rate or not has_kwh:
                return 0.0, ()
            score = 0.88
            reasons.append("single_value_rider_family")
            if has_kwh:
                reasons.append("kwh_rate")
            if relaxed_family and not has_kwh and has_leaf:
                score -= 0.04
                reasons.append("relaxed_family_selection")
            if "approved decremental rate" in signals.text_lower or "approved incremental rate" in signals.text_lower:
                score += 0.05
                reasons.append("approved_rate_sentence")
            if "leaf no." in signals.text_lower:
                score += 0.02
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_rider_adjustment_matrix":
            # Exclude Progress families — they have their own adjustment matrix profile
            if signals.family_key.startswith("nc-progress-"):
                return 0.0, ()
            family_match = signals.family_key in {"nc-carolinas-rider-summary", "nc-carolinas-leaf-99"}
            company_match = signals.company == "carolinas" or signals.has_carolinas_company_text
            if not signals.has_summary_text or not (family_match or company_match):
                return 0.0, ()
            score = 0.9
            reasons.append("summary_text")
            if company_match:
                score += 0.03
                reasons.append("carolinas_company")
            if signals.leaf_no == "99" or "leaf no. 99" in signals.text_lower or "leaf no 99" in signals.text_lower:
                score += 0.03
                reasons.append("leaf99")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_small_customer_generator":
            if signals.family_key != "nc-carolinas-rider-scg":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider scg" not in lowered or "small customer generator" not in lowered:
                return 0.0, ()
            score = 0.88
            reasons.extend(("rider_scg", "small_customer_generator"))
            if "supplemental basic" in lowered and "charge per month" in lowered:
                score += 0.04
                reasons.append("supplemental_charge")
            if "standby charge" in lowered or "for systems more than 20 kw" in lowered:
                score += 0.04
                reasons.append("standby_charge")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "green_source_advantage_rider":
            if signals.family_key not in {"nc-progress-leaf-665", "nc-carolinas-rider-gsa"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "rider gsa" not in lowered or "green source advantage" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("rider_gsa", "green_source_advantage"))
            if "administrative charge" in lowered:
                score += 0.04
                reasons.append("administrative_charge")
            if "additional account billed" in lowered:
                score += 0.02
                reasons.append("additional_account_charge")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_economic_development_rider":
            if signals.family_key != "nc-carolinas-rider-ec":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider ec" not in lowered or "economic development" not in lowered:
                return 0.0, ()
            score = 0.89
            reasons.extend(("rider_ec", "economic_development"))
            if "application of credit" in lowered:
                score += 0.04
                reasons.append("credit_schedule")
            if "months 1-12" in lowered or "monihs 1-12" in lowered:
                score += 0.03
                reasons.append("percentage_credit_values")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_interruptible_service_rider":
            if signals.family_key != "nc-carolinas-rider-is":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider is" not in lowered or "interruptible power service" not in lowered:
                return 0.0, ()
            score = 0.89
            reasons.extend(("rider_is", "interruptible_power"))
            if "credit = eid" in lowered:
                score += 0.04
                reasons.append("interruptible_credit")
            if "penalty" in lowered and "$10.00" in lowered:
                score += 0.04
                reasons.append("penalty_charge")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_residential_flat":
            has_tou_signal = signals.has_tou_terms or "optional time of use" in signals.text_lower
            family_match = signals.family_key in {"nc-carolinas-schedule-rs", "nc-carolinas-schedule-es", "nc-carolinas-leaf-11"} or signals.family_key.startswith("nc-carolinas-")
            company_match = signals.company == "carolinas" or signals.has_carolinas_company_text
            # Schedule S (Unmetered Signs) is a niche flat-rate Carolinas schedule
            # whose text uses "Schedule S" rather than the RS keyword family. Allow
            # it through with a family-specific marker check.
            is_schedule_s = (
                signals.family_key == "nc-carolinas-schedule-s"
                and "schedule s" in signals.text_lower
                and "unmetered" in signals.text_lower
            )
            if not family_match or not company_match or not signals.has_flat_rate_markers or has_tou_signal:
                return 0.0, ()
            if not signals.has_rs_marker and not is_schedule_s:
                return 0.0, ()
            score = 0.82
            if is_schedule_s:
                reasons.extend(("carolinas_schedule_s", "unmetered_signs", "flat_rate_markers"))
            else:
                reasons.extend(("carolinas_family", "rs_marker", "flat_rate_markers"))
            if signals.family_key in {"nc-carolinas-schedule-rs", "nc-carolinas-leaf-11"}:
                score += 0.06
                reasons.append("specific_rs_family")
            elif signals.family_key == "nc-carolinas-schedule-es":
                score += 0.05
                reasons.append("specific_es_family")
            if "for the billing months of" in signals.text_lower:
                score += 0.03
                reasons.append("seasonal_months")
            return min(score, 0.95), tuple(reasons)

        if profile_name == "carolinas_residential_tou":
            if signals.family_key not in {
                "nc-carolinas-schedule-rt",
                "nc-carolinas-schedule-opt-e",
                "nc-carolinas-schedule-optv",
                "nc-carolinas-schedule-opt-v",
                "nc-carolinas-schedule-retc",
                "nc-carolinas-schedule-rstc",
                "nc-carolinas-schedule-sgstc",
                "nc-carolinas-doc-schedulertresidentialservicetimeofuse",
            }:
                return 0.0, ()
            company_match = signals.company == "carolinas" or signals.has_carolinas_company_text
            has_family_marker = any(
                marker in signals.text_lower
                for marker in (
                    "schedule rt",
                    "schedule opt-e",
                    "schedule opt",
                    "schedule retc",
                    "schedule rstc",
                    "schedule sgstc",
                    "residential service, time of use",
                    "optional time-of-use",
                    "optional time of use",
                    "residential energy time-of-use",
                    "residential service time-of-use control",
                    "small general service time-of-use",
                )
            )
            has_rate_marker = "customer charge" in signals.text_lower or "facilities charge" in signals.text_lower
            if not company_match or not has_family_marker or not signals.has_tou_terms or not has_rate_marker:
                return 0.0, ()
            score = 0.9
            reasons.extend(("carolinas_tou_schedule", "tou_terms", "rate_markers"))
            if "discount" in signals.text_lower or "super off-peak" in signals.text_lower:
                score += 0.02
                reasons.append("discount_period")
            if signals.has_demand_charge_term:
                score -= 0.05
                reasons.append("demand_terms")
            return max(0.0, min(score, 0.96)), tuple(reasons)

        if profile_name == "carolinas_current_leaf_bridge":
            if signals.family_key != "nc-carolinas-schedule-hlf" or not signals.is_current_carolinas_pdf:
                return 0.0, ()
            if "schedule hlf" not in signals.text_lower and "high load factor" not in signals.text_lower:
                return 0.0, ()
            score = 0.86
            reasons.extend(("current_carolinas_pdf", "hlf_schedule"))
            if signals.has_demand_charge_term:
                score += 0.03
                reasons.append("demand_terms")
            if "customer charge" in signals.text_lower:
                score += 0.02
                reasons.append("customer_charge")
            return min(score, 0.96), tuple(reasons)

        if profile_name == "carolinas_customer_assistance_recovery":
            if signals.family_key != "nc-carolinas-rider-car":
                return 0.0, ()
            lowered = signals.text_lower
            if "customer assistance recovery" not in lowered or "monthly rate" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("rider_car", "customer_assistance_recovery"))
            if "$/kwh" in lowered:
                score += 0.03
                reasons.append("kwh_rate")
            if "$/bill" in lowered:
                score += 0.03
                reasons.append("bill_rate")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_nuclear_production_tax_credits":
            if signals.family_key not in {"nc-carolinas-rider-ridernptc", "nc-carolinas-rider-nptc"}:
                return 0.0, ()
            lowered = signals.text_lower
            if "nuclear production tax credits" not in lowered or "per kilowatt-hour" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("rider_nptc", "nuclear_ptc"))
            if "decremental rate" in lowered:
                score += 0.05
                reasons.append("decremental_rate")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_single_value_rider":
            if signals.family_key not in {
                "nc-carolinas-rider-edpr",
                "nc-carolinas-rider-bpmppttrueup",
                "nc-carolinas-rider-bpmprospectiverider",
                "nc-carolinas-rider-prospectiverider",
                "nc-carolinas-rider-ps",
                "nc-carolinas-rider-riderlc",
            }:
                return 0.0, ()
            lowered = signals.text_lower
            _new_rider_families = {
                "nc-carolinas-rider-ps",
                "nc-carolinas-rider-riderlc",
                "nc-carolinas-rider-prospectiverider",
                "nc-carolinas-rider-bpmprospectiverider",
            }
            if signals.family_key in _new_rider_families:
                if not ("per kilowatt-hour" in lowered or "c/kwh" in lowered or "/kwh" in lowered):
                    return 0.0, ()
            else:
                if not (
                    ("existing dsm program" in lowered or "bpm true-up rider" in lowered or "bpm prospective rider" in lowered)
                    and ("per kilowatt-hour" in lowered or "c/kwh" in lowered or "/kwh" in lowered)
                ):
                    return 0.0, ()
            score = 0.9
            reasons.extend(("carolinas_single_value", "kwh_rate"))
            if "approved decremental rate" in lowered or "approved incremental rate" in lowered:
                score += 0.04
                reasons.append("approved_rate_sentence")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_general_service_schedule":
            family_tokens = {
                "nc-carolinas-schedule-pg": ("pg_schedule", ("schedule pg", "parallel generation")),
                "nc-carolinas-schedule-lgs": ("lgs_schedule", ("schedule lgs", "large general service")),
                "nc-carolinas-schedule-sgs": ("sgs_schedule", ("schedule sgs", "small general service")),
                "nc-carolinas-doc-schedulelgslargegeneralservice": ("lgs_schedule", ("schedule lgs", "large general service")),
                "nc-carolinas-doc-scheduleoptioptionalpowerservicetimeofuseindustr": (
                    "opti_schedule",
                    ("schedule opt-i", "optional power service"),
                ),
                "nc-carolinas-doc-scheduleiindustrialservice": (
                    "industrial_schedule",
                    ("schedule i", "industrial service"),
                ),
            }
            family_info = family_tokens.get(signals.family_key)
            if not family_info:
                return 0.0, ()
            family_reason, required_tokens = family_info
            if not (signals.has_carolinas_company_text or signals.company == "carolinas"):
                return 0.0, ()
            is_nantahala = "nantahala" in signals.text_lower
            if not is_nantahala and not CarolinasGeneralServiceScheduleProfile._has_leaf_marker(signals.text_lower):
                return 0.0, ()
            if not any(token in signals.text_lower for token in required_tokens):
                return 0.0, ()
            score = 0.88
            reasons.extend(("carolinas_general_service", family_reason))
            if "customer charge" in signals.text_lower:
                score += 0.02
                reasons.append("customer_charge")
            if "energy charge" in signals.text_lower:
                score += 0.02
                reasons.append("energy_charge")
            if signals.has_demand_charge_term or "billing demand" in signals.text_lower:
                score += 0.02
                reasons.append("demand_terms")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "carolinas_lighting_schedule":
            if signals.family_key not in {
                "nc-carolinas-schedule-ol",
                "nc-carolinas-schedule-pl",
                "nc-carolinas-schedule-fl",
                "nc-carolinas-schedule-yl",
                "nc-carolinas-schedule-gl",
                "nc-carolinas-doc-floodlightingservice",
                "nc-carolinas-doc-scheduleplstreetandpubliclightingservice",
                "nc-carolinas-doc-scheduleflfloodlightingservice",
                "nc-carolinas-doc-scheduleylyardlightingservice",
                "nc-carolinas-doc-governmentallightingservice",
            }:
                return 0.0, ()
            lowered = signals.text_lower
            if "schedule ol" in lowered and "outdoor lighting service" in lowered:
                score = 0.93
                reasons.extend(("lighting_schedule", "schedule_ol", "luminaire_rates"))
                return score, tuple(reasons)
            if "schedule pl" in lowered and "per month per luminaire" in lowered:
                score = 0.93
                reasons.extend(("lighting_schedule", "schedule_pl", "luminaire_rates"))
                return score, tuple(reasons)
            if "schedule fl" in lowered and "floodlighting service" in lowered:
                score = 0.93
                reasons.extend(("lighting_schedule", "schedule_fl", "luminaire_rates"))
                return score, tuple(reasons)
            if "schedule yl" in lowered and "yard lighting service" in lowered:
                score = 0.93
                reasons.extend(("lighting_schedule", "schedule_yl", "per_unit_rates"))
                return score, tuple(reasons)
            if "schedule gl" in lowered and "governmental lighting service" in lowered:
                score = 0.93
                reasons.extend(("lighting_schedule", "schedule_gl", "luminaire_rates"))
                return score, tuple(reasons)
            return 0.0, ()

        if profile_name == "carolinas_schedule_bridge":
            family_tokens = {
                "nc-carolinas-schedule-i": ("industrial_schedule", ("schedule i", "industrial service")),
                "nc-carolinas-doc-scheduleiindustrialservice": ("industrial_schedule", ("schedule i", "industrial service")),
                "nc-carolinas-doc-scheduleopte": ("opte_schedule", ("schedule opt-e", "optional power service")),
                "nc-carolinas-doc-scheduleoptg": ("optg_schedule", ("schedule opt-g", "general service")),
                "nc-carolinas-schedule-ts": ("ts_schedule", ("schedule ts", "traffic signal service")),
                "nc-carolinas-schedule-opt-e": ("opte_schedule", ("schedule opt-e", "optional power service")),
                "nc-carolinas-schedule-opt-g": ("optg_schedule", ("schedule opt-g", "general service")),
                "nc-carolinas-schedule-opt-h": ("opth_schedule", ("schedule opt-h", "optional power service")),
                "nc-carolinas-schedule-opt-i": ("opti_schedule", ("schedule opt-i", "optional power service")),
                "nc-carolinas-schedule-bc": ("bc_schedule", ("schedule bc", "building construction service")),
                "nc-carolinas-schedule-it": ("it_schedule", ("schedule it", "interruptible")),
                "nc-carolinas-schedule-nl": ("nl_schedule", ("schedule nl", "night")),
                "nc-carolinas-schedule-hp": ("hp_schedule", ("schedule hp", "hourly pricing")),
                "nc-carolinas-schedule-ppbe": ("ppbe_schedule", ("ppbe", "purchased power")),
                "nc-carolinas-schedule-hlf": ("hlf_schedule", ("schedule hlf", "high load factor")),
                "nc-carolinas-schedule-wc": ("wc_schedule", ("schedule wc", "water heating")),
                "nc-carolinas-schedule-ret": ("ret_schedule", ("schedule ret", "residential")),
                "nc-carolinas-schedule-rst": ("rst_schedule", ("schedule rst", "residential")),
                "nc-carolinas-schedule-sgst": ("sgst_schedule", ("schedule sgst", "general service")),
                "nc-carolinas-doc-schedulewc": ("wc_schedule", ("schedule wc", "residential water heating service")),
                "nc-carolinas-doc-schedulewcresidentialwaterheatingservice": ("wc_schedule", ("schedule wc", "residential water heating service")),
            }
            family_info = family_tokens.get(signals.family_key)
            if not family_info:
                return 0.0, ()
            family_reason, required_tokens = family_info
            if not (signals.has_carolinas_company_text or signals.company == "carolinas"):
                return 0.0, ()
            if not CarolinasScheduleBridgeProfile._has_leaf_marker(signals.text_lower):
                return 0.0, ()
            if not all(token in signals.text_lower for token in required_tokens):
                return 0.0, ()
            score = 0.88
            reasons.extend(("carolinas_schedule_bridge", family_reason))
            if "basic facilities charge" in signals.text_lower:
                score += 0.02
                reasons.append("facilities_charge")
            if "energy charge" in signals.text_lower:
                score += 0.02
                reasons.append("energy_charge")
            if signals.has_demand_charge_term or "billing demand" in signals.text_lower:
                score += 0.02
                reasons.append("demand_terms")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "carolinas_solar_choice_rider":
            # RSC and DEP leaf-670 (Residential Solar Choice) — historical and current versions supported
            if signals.family_key in {"nc-carolinas-rider-rsc", "nc-progress-leaf-670"}:
                if signals.family_key == "nc-progress-leaf-670" and signals.is_current_progress_pdf:
                    return 0.0, ()
                if "rider rsc" not in signals.text_lower or "residential solar choice" not in signals.text_lower:
                    return 0.0, ()
                score = 0.87
                reasons.extend(("rider_rsc", "residential_solar_choice"))
                if "net excess energy credit" in signals.text_lower:
                    score += 0.05
                    reasons.append("net_excess_credit")
                if "non-bypassable charge" in signals.text_lower:
                    score += 0.03
                    reasons.append("non_bypassable_charge")
                if "grid access fee" in signals.text_lower:
                    score += 0.02
                    reasons.append("grid_access_fee")
                return min(score, 0.97), tuple(reasons)
            # NMB and NSC require current-PDF path
            family_tokens = {
                "nc-carolinas-rider-nmb": ("rider_nmb", "rider nmb", "net metering bridge"),
                "nc-carolinas-rider-nsc": ("rider_nsc", "rider nsc", "non-residential solar choice"),
            }
            family_info = family_tokens.get(signals.family_key)
            if not family_info or not signals.is_current_carolinas_pdf:
                return 0.0, ()
            family_reason, marker_a, marker_b = family_info
            if marker_a not in signals.text_lower or marker_b not in signals.text_lower:
                return 0.0, ()
            score = 0.87
            reasons.extend(("current_carolinas_pdf", family_reason))
            if "credit" in signals.text_lower:
                score += 0.03
                reasons.append("credit_terms")
            if "minimum bill" in signals.text_lower or "standby charge" in signals.text_lower:
                score += 0.03
                reasons.append("fixed_charge_terms")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "carolinas_net_metering_rider":
            if signals.family_key != "nc-carolinas-rider-nm":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider nm" not in lowered or "net metering" not in lowered:
                return 0.0, ()
            score = 0.88
            reasons.extend(("rider_nm", "net_metering"))
            if "standby charge" in lowered:
                score += 0.04
                reasons.append("standby_charge")
            if "minimum bill" in lowered:
                score += 0.03
                reasons.append("minimum_bill")
            if "non-bypassable charge" in lowered:
                score += 0.01
                reasons.append("non_bypassable_charge")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "carolinas_energy_efficiency_rider":
            if signals.family_key != "nc-carolinas-rider-ee":
                return 0.0, ()
            lowered = signals.text_lower
            if "rider ee" not in lowered or "energy efficiency rider" not in lowered:
                return 0.0, ()
            score = 0.9
            reasons.extend(("rider_ee", "energy_efficiency_rider"))
            if "energy efficiency rider adjustments" in lowered:
                score += 0.03
                reasons.append("ee_adjustments")
            if "total residential rate" in lowered or "total nonresidential" in lowered or "vintage 1 total" in lowered:
                score += 0.03
                reasons.append("explicit_rate_values")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_fuel_cost_adj_rider":
            if signals.family_key != "nc-carolinas-rider-fcar":
                return 0.0, ()
            lowered = signals.text_lower
            score = 0.0
            if "fuel cost adjustment" in lowered:
                score = 0.88
                reasons.append("fcar_language")
            # Annual application format uses "fuel and fuel-related" instead of "fuel cost adjustment"
            elif "fuel and fuel-related" in lowered and "residential" in lowered:
                score = 0.86
                reasons.append("fcar_application_language")
            if "base fuel cost" in lowered:
                score += 0.04
                reasons.append("base_fuel_cost")
            if "fuel cost adjustment factor" in lowered:
                score += 0.04
                reasons.append("fcar_factor_line")
            if "composite" in lowered and "fuel" in lowered:
                score += 0.03
                reasons.append("composite_fuel_factor")
            if "residential service" in lowered and "general service" in lowered:
                score += 0.02
                reasons.append("multi_class_structure")
            return min(score, 0.98), tuple(reasons)

        if profile_name == "carolinas_flat_fee_rider":
            if signals.family_key not in {
                "nc-carolinas-rider-car",
                "nc-carolinas-rider-ed",
                "nc-carolinas-rider-pm",
                "nc-progress-leaf-644",
                "nc-progress-leaf-666",
                "nc-progress-leaf-718",
            }:
                return 0.0, ()
            lowered = signals.text_lower
            score = 0.0
            if "per month" in lowered:
                score = 0.85
                reasons.append("per_month_fee")
            if "per month per block" in lowered:
                score += 0.08
                reasons.append("per_month_per_block")
            if "subscription rate" in lowered or "monthly rate" in lowered or "monthly charge" in lowered:
                score = max(score, 0.85)
                reasons.append("monthly_charge_language")
            return min(score, 0.97), tuple(reasons)

        if profile_name == "generic_residential":
            family_match = signals.family_key.startswith(("nc-progress-leaf-", "nc-carolinas-leaf-"))
            has_residential_signal = (
                "residential" in signals.text_lower
                or "residential" in signals.title
                or signals.has_rs_marker
            )
            has_rate_shape = (
                signals.has_flat_rate_markers
                or signals.has_tou_terms
                or "per kwh" in signals.text_lower
                or "per kilowatt-hour" in signals.text_lower
            )
            if family_match and has_residential_signal and has_rate_shape:
                return 0.1, ("generic_family_fallback", "residential_signal")
            return 0.0, ()

        return 0.0, ()

    def rank_candidates(self, doc: dict, text: str) -> list[ParserProfileCandidate]:
        signals = self._build_signals(doc, text)
        ranked: list[ParserProfileCandidate] = []
        for profile in self._profiles:
            score, reasons = self._score_profile(profile.name, signals)
            ranked.append(
                ParserProfileCandidate(
                    name=profile.name,
                    score=score,
                    supported=score > 0,
                    reasons=reasons,
                )
            )
        return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)

    def select(self, doc: dict, text: str) -> HistoricalRateParserProfile:
        ranked = self.rank_candidates(doc, text)
        if not ranked:
            return UnsupportedDocumentProfile()

        selected_name = ranked[0].name if ranked[0].score > 0 else UnsupportedDocumentProfile().name
        return self.get_profile(selected_name) or UnsupportedDocumentProfile()

    def recommend_fallback_sequence(
        self,
        doc: dict,
        text: str,
        *,
        ranked_candidates: list[ParserProfileCandidate] | None = None,
        selected_name: str | None = None,
        limit: int | None = None,
    ) -> list[ParserProfileCandidate]:
        ranked = ranked_candidates or self.rank_candidates(doc, text)
        selected = selected_name or (ranked[0].name if ranked else None)
        recommended = [
            candidate
            for candidate in ranked
            if candidate.supported and candidate.score > 0 and candidate.name != selected
        ]
        if limit is not None:
            return recommended[:limit]
        return recommended
