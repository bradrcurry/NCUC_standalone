from __future__ import annotations

import re
from datetime import datetime
from collections.abc import Iterable
from dataclasses import dataclass

SCHEDULE_CODE_RE = re.compile(
    r"\b(?i:schedule|rate schedule)\s+([A-Z][A-Z0-9]{0,5}(?:-[A-Z0-9]{1,8}){0,3})\b"
)
TITLE_SCHEDULE_CODE_RE = re.compile(
    r"\b(?:schedule|rate)\s*\(?([A-Z][A-Z0-9]{0,5}(?:-[A-Z0-9]{1,8}){0,3})\)?\b",
    re.I,
)
EFFECTIVE_DATE_WITH_DAY_RE = re.compile(
    (
        r"\b(?:effective|eff\.)"
        r"(?:\s+date)?(?:\s+for\s+service\s+rendered\s+(?:on\s+and\s+after|from)|\s+on\s+and\s+after)?"
        r"[:\s]+([A-Za-z]+\s+\d{1,2},\s+\d{4}\d?)"  # allow trailing extra digit from redlined PDFs
    ),
    re.I,
)
EFFECTIVE_MONTH_RE = re.compile(
    (
        r"\b(?:effective|eff\.)"
        r"(?:\s+date)?(?:\s+for\s+service\s+rendered\s+(?:on\s+and\s+after|from)|\s+on\s+and\s+after)?"
        r"[:\s]+([A-Za-z]+\s+\d{4}\d?)"  # allow trailing extra digit from redlined PDFs
    ),
    re.I,
)
FIXED_CHARGE_RE = re.compile(
    (
        r"(?P<label>"
        r"(?:basic\s+(?:customer|facilities)\s+charge|customer\s+charge(?:\s*-\s*[A-Za-z /-]+)?|"
        r"customer\s+chrg(?:\s*-\s*[A-Za-z /-]+)?|monthly\s+service\s+charge|"
        r"service\s+charge)"
        r")\b[\s\S]{0,40}?\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)"
    ),
    re.I,
)
ENERGY_CHARGE_RE = re.compile(
    (
        r"(?P<label>"
        r"(?:energy\s+charge(?:\s*-\s*[A-Za-z0-9<>=/ ()-]+)?|"
        r"energy\s+chrg(?:\s*-\s*[A-Za-z0-9<>=/ ()-]+)?|"
        r"kilowatt-hour\s+charge(?:\s*-\s*[A-Za-z0-9<>=/ ()-]+)?)"
        r")\b[\s\S]{0,40}?\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)"
        r"\s*(?:¢|cents|\$)?\s*(?:per\s*)?(?:kwh|kilowatt-hour)?"
    ),
    re.I,
)
ENERGY_RATE_ONLY_RE = re.compile(
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:¢|cents|\$)\s*(?:per\s*)kwh\b",
    re.I,
)
TOU_ENERGY_RATE_RE = re.compile(
    (
        r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:¢|cents|\$)\s*per\s*"
        r"(?P<period>Critical Peak|On-Peak|Off-Peak|Super Off-Peak|Discount)\s*kWh"
    ),
    re.I,
)
DEMAND_CHARGE_RE = re.compile(
    (
        r"(?P<label>(?:demand\s+charge(?:\s*-\s*[A-Za-z0-9<>=/ ()-]+)?|demand))"
        r"\b[\s\S]{0,40}?\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)"
        r"\s*(?:per\s*)?(?:kw|kilowatt)?"
    ),
    re.I,
)
TOU_RE = re.compile(r"\b(?:time[- ]of[- ]use|tou|on-peak|off-peak|super off-peak)\b", re.I)
RIDER_CODE_RE = re.compile(r"\bRider\s+([A-Z][A-Z0-9-]{1,12})\b")
RIDER_SUFFIX_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9-]{1,12})\s+Rider\b")
RIDER_PAREN_CODE_RE = re.compile(r"\(([A-Z][A-Z0-9-]{1,12})\)\s+Rider\b")
LEAF_RIDER_TITLE_RE = re.compile(
    r"Leaf\s+No\.?\s+\d+\s+([A-Z][A-Za-z0-9/&()' .-]{2,80}?Rider)\b"
)
RIDER_VERSION_RE = re.compile(r"\bRIDER\s+([A-Z][A-Z0-9-]{1,20})\b")
RIDER_HEADING_RE = re.compile(
    r"\n\s*([A-Z][A-Z/&()' .,-]{5,100})\s*\n\s*RIDER\s+[A-Z][A-Z0-9-]{1,20}\b",
    re.I,
)
SCHEDULE_HEADING_RE = re.compile(
    r"\n\s*([A-Z][A-Z/&()' .,-]{5,100})\s*\n\s*SCHEDULE\s+[A-Z][A-Z0-9-]{0,24}\b",
    re.I,
)
ELIGIBILITY_RE = re.compile(r"\b(?:available|applicable)\s+to\b(.+?)(?:\.|\n)", re.I)
RIDER_APPLICABILITY_RE = re.compile(
    r"APPLICABILITY[\s\-–:]+(.+?)(?=\n\s*[A-Z][A-Z /&()'.,-]{4,}\n|$)",
    re.I | re.S,
)
SUMMARY_RATE_MATRIX_RE = re.compile(
    r"\b(?:base rates by rate schedule|rates by rate schedule|summary of rate schedules)\b",
    re.I,
)
SCHEDULE_TOKEN_RE = re.compile(r"\b[A-Z]{2,5}(?:-[A-Z0-9]{1,5})?\b")
RIDER_CODE_STOPWORDS = {"CHARGE", "INCREMENT", "INCREMENTAL", "LEAF", "DUKE"}
DOCKET_NUMBER_RE = re.compile(r"\b([A-Z]{1,3}-\d+(?:,\s*|\s+)SUB\s+\d+)\b", re.I)
# Matches the full NCUC footer line: docket number + optional order date
NCUC_FOOTER_RE = re.compile(
    r"NCUC\s+Docket\s+No\.?\s+"
    r"([A-Z]{1,3}-\d+(?:,\s*|\s+)Sub\s+\d+)"       # group 1: docket number
    r"(?:[,;]\s*Order\s+dated\s+"
    r"([A-Za-z]+\s+\d{1,2},\s+\d{4}))?",             # group 2: order date (optional)
    re.I,
)
# "Supersedes Schedule RES-77" or "Superseding NC Original Leaf No. 500"
SUPERSEDES_LEAF_RE = re.compile(
    r"\bSupersed(?:es|ing)\s+"
    r"((?:(?:NC|SC)\s+)?"                  # optional state prefix inside capture
    r"(?:Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|\w+)"
    r"(?:\s+Revised)?\s+Leaf\s+No\.?\s*\d+)",
    re.I,
)
SUPERSEDES_SCHEDULE_RE = re.compile(
    r"\bSupersed(?:es|ing)\s+(?:Schedule\s+)?([A-Z][A-Z0-9-]{1,20}(?:-\d+)?)\b",
)
NOTICE_FILING_DATE_RE = re.compile(r"\bOn\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})\b")
TITLE_END_CODE_RE = re.compile(r"\b(?:Rider|Schedule)\s+([A-Z][A-Z0-9/-]{1,12})\b")
NOTICE_RIDER_TOKEN_MAP = {
    "CPRE": "CPRE",
    "DSM/EE": "DSM/EE",
    "DEMAND-SIDE MANAGEMENT": "DSM/EE",
    "ENERGY EFFICIENCY": "DSM/EE",
    "DSM": "DSM/EE",
    "EE": "DSM/EE",
    "JAAR": "JAA",
    "JAA": "JAA",
    "JOINT AGENCY ASSET": "JAA",
    "FUEL": "BA",
    "BA": "BA",
    "PBR": "PBR",
    "REPS": "REPS",
    "CEPS": "REPS",
}
NOTICE_SCHEDULE_TOKEN_MAP = {
    "RESIDENTIAL": "RES",
    "SMALL GENERAL SERVICE": "SGS",
    "MEDIUM GENERAL SERVICE": "MGS",
    "LARGE GENERAL SERVICE": "LGS",
    "LIGHTING": "LIGHTING",
}
TIME_RANGE_RE = re.compile(
    (
        r"(\d{1,2}:\d{2}\s*[ap](?:\.?m\.?)?\s*(?:\(midnight\))?\s*"
        r"(?:to|[-–])\s*"
        r"\d{1,2}:\d{2}\s*[ap](?:\.?m\.?)?)"
    ),
    re.I,
)
MONTH_RANGE_RE = re.compile(
    r"For (?:the )?(calendar months of [A-Za-z]+ through [A-Za-z]+|all calendar months)",
    re.I,
)
DAY_PATTERN_RE = re.compile(
    r"(Monday(?:\s+through|\s*[-–]\s*)Friday\*?|Every day, including weekends and holidays|All days(?:\s+including Holidays\*?)?)",
    re.I,
)
APPLICABLE_SCHEDULES_RE = re.compile(
    (
        r"Applicable to Schedules:\s*(.+?)"
        r"(?=\n\s*\n|\n\s*[A-Z][a-z]+ General Service|\n\s*[A-Z][A-Z ]{3,}|$)"
    ),
    re.I | re.S,
)
SCHEDULE_CODE_TOKEN_RE = re.compile(r"\b[A-Z]{1,5}(?:-[A-Z0-9]{1,8}){0,3}\b")
APPLICABLE_SCHEDULE_STOPWORDS = {"LOAD", "CONSTANT", "EE", "DSM", "ONLY", "APPLICABLE"}
RATE_CLASS_HEADINGS = (
    "Residential",
    "Small General Service",
    "Medium General Service",
    "Large General Service",
    "Lighting",
)
SUMMARY_COMPONENT_RIDER_CODES = {
    "BA",
    "JAA",
    "EDIT-4",
    "CPRE",
    "RDM",
    "ESM",
    "PIM",
    "CAR",
}
RESIDENTIAL_STANDARD_SCHEDULES = ["RES", "R-TOUD", "R-TOU", "R-TOU-CPP"]
RESIDENTIAL_TOU_PLUS_EV_SCHEDULES = [*RESIDENTIAL_STANDARD_SCHEDULES, "R-TOU-EV"]
GENERAL_SERVICE_SCHEDULES = ["SGS", "MGS", "LGS"]
KNOWN_RIDER_TITLE_CODES = {
    "ANNUAL BILLING ADJUSTMENTS": "BA",
    "JOINT AGENCY ASSET": "JAA",
    "JOINT AGENCY ADJUSTMENT": "JAA",
    "ENERGY EFFICIENCY": "EE",
    "DEMAND SIDE MANAGEMENT": "DSM",
    "STORM SECURITIZATION": "STS",
    "STORM TRANSITION": "STS",
    "CLEAN POWER RATE ENHANCEMENT": "CPRE",
    "CLEAN ENERGY IMPACT": "CEI",
    "RESIDENTIAL DECOUPLING MECHANISM": "RDM",
    "EARNINGS SHARING MECHANISM": "ESM",
    "PERFORMANCE INCENTIVE MECHANISM": "PIM",
    "CUSTOMER ASSISTANCE RECOVERY": "CAR",
    "ENERGY CONSERVATION DISCOUNT": "RECD",
    "REPS EMF": "REPS",
    "RENEWABLE ENERGY PORTFOLIO STANDARD": "REPS",
    "REPS RIDER": "REPS",
}


@dataclass
class ChargeMatch:
    label: str
    rate: float
    snippet: str
    period: str | None = None
    season: str | None = None
    block_from: float | None = None
    block_to: float | None = None


@dataclass
class RiderReferenceMatch:
    title: str
    code: str | None = None


@dataclass
class RiderAdjustmentMatch:
    rate_class: str
    fuel_adjustment_cents_per_kwh: float | None
    fuel_emf_cents_per_kwh: float | None
    dsm_ee_adjustment_cents_per_kwh: float | None
    dsm_ee_emf_cents_per_kwh: float | None
    net_adjustment_cents_per_kwh: float | None
    applicable_schedules: list[str]


@dataclass
class RiderChargeComponentMatch:
    bill_label: str
    value: float
    unit: str
    rate_class: str | None = None
    applicable_schedules: list[str] | None = None


def extract_first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def extract_matches(pattern: re.Pattern[str], text: str, *, label: str) -> list[ChargeMatch]:
    matches: list[ChargeMatch] = []
    for match in pattern.finditer(text):
        snippet = text[max(0, match.start() - 80) : match.end() + 80].replace("\n", " ")
        try:
            amount = match.groupdict().get("amount", match.group(1))
            rate = float(amount.replace(",", ""))
        except ValueError:
            continue
        if rate == 0.0:
            continue
        derived_label = _normalize_spaces(match.groupdict().get("label") or label)
        matches.append(ChargeMatch(label=derived_label, rate=rate, snippet=snippet))
    return _dedupe_charge_matches(matches)


def extract_effective_date(text: str) -> str | None:
    candidates: list[str] = []
    probe = _normalized_probe_text(text)
    for pattern in (EFFECTIVE_DATE_WITH_DAY_RE, EFFECTIVE_MONTH_RE):
        for match in pattern.finditer(probe):
            value = _normalize_spaces(match.group(1))
            # Fix redlined PDFs where a 5-digit year appears (e.g. "20234" = old 2023 + new digit 4 → 2024)
            value = re.sub(
                r"\b(\d{4})(\d)\b",
                lambda m: str((int(m.group(1)) // 10) * 10 + int(m.group(2))),
                value,
            )
            candidates.append(value)

    if not candidates:
        return None

    dated_candidates: list[tuple[datetime, str]] = []
    for candidate in candidates:
        parsed = _parse_effective_candidate(candidate)
        if parsed is not None:
            dated_candidates.append((parsed, candidate))

    if dated_candidates:
        dated_candidates.sort(key=lambda item: item[0])
        return dated_candidates[-1][1]
    return candidates[0]


def _parse_effective_candidate(value: str) -> datetime | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_schedule_code(title: str, text: str) -> str | None:
    probe = _normalized_probe_text(text)
    for candidate in (
        extract_first(SCHEDULE_CODE_RE, probe),
        extract_first(TITLE_SCHEDULE_CODE_RE, title),
        _extract_schedule_code_from_top_lines(probe),
    ):
        if candidate:
            return candidate.upper()
    return None


def extract_schedule_title(title: str, text: str) -> str:
    heading = extract_first(SCHEDULE_HEADING_RE, f"\n{_normalized_probe_text(text)}")
    if heading:
        return _normalize_heading(heading)
    return title


def extract_rider_title(title: str, text: str) -> str:
    heading = extract_first(RIDER_HEADING_RE, f"\n{_normalized_probe_text(text)}")
    if heading:
        return _normalize_heading(heading)
    return title


def extract_rider_version(text: str) -> str | None:
    value = extract_first(RIDER_VERSION_RE, _normalized_probe_text(text))
    return value.upper() if value else None


def extract_rider_applicability(text: str) -> str | None:
    for pattern in (RIDER_APPLICABILITY_RE, ELIGIBILITY_RE):
        value = extract_first(pattern, _normalized_probe_text(text))
        if value:
            return _normalize_spaces(value)
    return None


def extract_applicable_schedule_codes(text: str) -> list[str]:
    probe = _normalized_probe_text(text)
    codes: list[str] = []
    for match in APPLICABLE_SCHEDULES_RE.finditer(probe):
        codes.extend(SCHEDULE_CODE_TOKEN_RE.findall(match.group(1).upper()))
    return _dedupe_strings(
        code for code in codes if code not in APPLICABLE_SCHEDULE_STOPWORDS
    )


def extract_rider_adjustment_rows(text: str) -> list[RiderAdjustmentMatch]:
    probe = _normalized_probe_text(text)
    rows: list[RiderAdjustmentMatch] = []
    for rate_class in RATE_CLASS_HEADINGS:
        block = _extract_rate_class_block(probe, rate_class)
        if not block:
            continue
        numbers = re.findall(r"-?(?:\d+(?:\.\d+)?|\.\d+)", block)
        if len(numbers) < 5:
            continue
        schedules_text = _extract_applicable_schedules_text(block)
        applicable_schedules = _extract_schedule_codes_from_text(schedules_text)
        rows.append(
            RiderAdjustmentMatch(
                rate_class=_normalize_heading(rate_class),
                fuel_adjustment_cents_per_kwh=_float_or_none(numbers[0]),
                fuel_emf_cents_per_kwh=_float_or_none(numbers[1]),
                dsm_ee_adjustment_cents_per_kwh=_float_or_none(numbers[2]),
                dsm_ee_emf_cents_per_kwh=_float_or_none(numbers[3]),
                net_adjustment_cents_per_kwh=_float_or_none(numbers[4]),
                applicable_schedules=applicable_schedules,
            )
        )
    return rows


def extract_rider_charge_components(
    text: str,
    *,
    rider_code: str | None,
) -> list[RiderChargeComponentMatch]:
    code = (rider_code or "").upper()
    matches: list[RiderChargeComponentMatch] = []
    if "SUMMARY OF RIDER ADJUSTMENTS" in text.upper():
        summary_total = _extract_residential_summary_total_cents(text)
        if summary_total is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Summary of Rider Adjustments",
                    rate_class="Residential",
                    value=summary_total,
                    unit="cents_per_kwh",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
    if code == "BA":
        summary_rate = _extract_residential_ba_net_adjustment(text)
        if summary_rate is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Summary of Rider Adjustments",
                    rate_class="Residential",
                    value=summary_rate,
                    unit="cents_per_kwh",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
        clean_energy_charge = _extract_clean_energy_monthly_charge(text)
        if clean_energy_charge is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Clean Energy Rider",
                    rate_class="Residential",
                    value=clean_energy_charge,
                    unit="fixed_monthly",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
    if code in SUMMARY_COMPONENT_RIDER_CODES - {"BA"}:
        summary_component_rate = _extract_summary_component_rate(text, rider_code=code)
        if summary_component_rate is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Summary of Rider Adjustments",
                    rate_class="Residential",
                    value=summary_component_rate,
                    unit="cents_per_kwh",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
    if code in {"STS", "STS-2"}:
        storm_rate = _extract_residential_table_rate(text)
        if storm_rate is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Storm Recovery Charge",
                    rate_class="Residential",
                    value=storm_rate,
                    unit="cents_per_kwh",
                    applicable_schedules=RESIDENTIAL_TOU_PLUS_EV_SCHEDULES,
                )
            )
    if code == "RECD":
        discount_percent = _extract_recd_percent(text)
        if discount_percent is not None:
            matches.append(
                RiderChargeComponentMatch(
                    bill_label="Energy Conservation Credit",
                    rate_class="Residential",
                    value=discount_percent,
                    unit="percent_of_energy_charges",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
    if code == "REPS":
        for component in _extract_reps_fixed_charges(text):
            matches.append(component)
    if code in {"EE", "DSM"}:
        for component in _extract_dsm_ee_notice_components(text, rider_code=code):
            matches.append(component)
        for component in _extract_dsm_ee_order_table_components(text, rider_code=code):
            if not any(c.rate_class == component.rate_class for c in matches):
                matches.append(component)
    if code == "CEI":
        for component in _extract_cei_monthly_charges(text):
            matches.append(component)
    return matches


def extract_rider_references(text: str) -> list[RiderReferenceMatch]:
    seen: set[tuple[str | None, str]] = set()
    references: list[RiderReferenceMatch] = []

    for match in LEAF_RIDER_TITLE_RE.finditer(text):
        title = _normalize_spaces(match.group(1))
        code = _extract_code_from_rider_title(title)
        key = (code, title)
        if key not in seen:
            seen.add(key)
            references.append(RiderReferenceMatch(title=title, code=code))

    for pattern, formatter in (
        (RIDER_CODE_RE, lambda code: f"Rider {code}"),
        (RIDER_SUFFIX_CODE_RE, lambda code: f"{code} Rider"),
        (RIDER_PAREN_CODE_RE, lambda code: f"{code} Rider"),
    ):
        for match in pattern.finditer(text):
            code = match.group(1).upper()
            if code in RIDER_CODE_STOPWORDS:
                continue
            title = formatter(code)
            key = (code, title)
            if key not in seen:
                seen.add(key)
                references.append(RiderReferenceMatch(title=title, code=code))

    return references


def extract_rider_code(title: str, text: str) -> str | None:
    for candidate in (
        _extract_code_from_rider_title(title),
        _extract_rider_code_from_text(f"{title}\n{_normalized_probe_text(text)[:1200]}"),
    ):
        if candidate:
            return candidate
    return None


def extract_docket_numbers(text: str) -> list[str]:
    return _dedupe_strings(
        _normalize_spaces(match.group(1)).replace("SUB", "Sub")
        for match in DOCKET_NUMBER_RE.finditer(text)
    )


def extract_docket_footer(text: str) -> tuple[str | None, str | None]:
    """Return ``(docket_number, order_date)`` from a rate sheet footer.

    Example footer::

        NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023

    Returns ``('E-2, Sub 1300', 'August 18, 2023')``.  Either element may be
    ``None`` if not present.
    """
    probe = _normalized_probe_text(text)
    m = NCUC_FOOTER_RE.search(probe)
    if not m:
        return None, None
    docket = _normalize_spaces(m.group(1))
    order_date = _normalize_spaces(m.group(2)) if m.group(2) else None
    return docket, order_date


def extract_supersedes(text: str) -> str | None:
    """Return the superseded leaf or schedule label from a rate sheet footer.

    Handles both forms:

    * ``"Supersedes Schedule RES-77"``  → ``"RES-77"``
    * ``"Superseding NC Original Leaf No. 500"``  → ``"NC Original Leaf No. 500"``
    """
    probe = _normalized_probe_text(text)
    # Prefer the leaf form (more specific) over the schedule-code form
    for pattern in (SUPERSEDES_LEAF_RE, SUPERSEDES_SCHEDULE_RE):
        m = pattern.search(probe)
        if m:
            return _normalize_spaces(m.group(1))
    return None


def extract_notice_filing_date(text: str) -> str | None:
    return extract_first(NOTICE_FILING_DATE_RE, text)


def extract_notice_rider_codes(text: str) -> list[str]:
    found: list[str] = []
    probe = text.upper().replace("SIDE MANAGEMENT", "DSM").replace("ENERGY EFFICIENCY", "EE")
    for token, normalized in NOTICE_RIDER_TOKEN_MAP.items():
        if token in probe:
            found.append(normalized)
    return _dedupe_strings(found)


def extract_notice_schedule_codes(text: str) -> list[str]:
    probe = " ".join(text.upper().split())
    matches: list[str] = []
    for token, code in NOTICE_SCHEDULE_TOKEN_MAP.items():
        if token in probe:
            matches.append(code)
    return _dedupe_strings(matches)


def extract_notice_customer_classes(text: str) -> list[str]:
    probe = " ".join(text.split())
    matches: list[str] = []
    for token in NOTICE_SCHEDULE_TOKEN_MAP:
        if token.title() in probe or token in probe.upper():
            matches.append(token.title())
    return _dedupe_strings(matches)


def extract_energy_charge_matches(text: str) -> list[ChargeMatch]:
    seasonal_matches = _extract_seasonal_block_energy_matches(text)
    tou_matches: list[ChargeMatch] = []
    for match in TOU_ENERGY_RATE_RE.finditer(text):
        snippet = text[max(0, match.start() - 80) : match.end() + 80].replace("\n", " ")
        rate = _fix_redlined_rate(match.group("amount").replace(",", ""))
        if rate is None:
            continue
        period = _normalize_period_name(match.group("period"))
        if rate == 0.0:
            continue
        tou_matches.append(
            ChargeMatch(
                label=f"Energy Charge - {period}",
                rate=rate,
                snippet=snippet,
                period=period,
            )
        )

    matches = seasonal_matches or (
        tou_matches[:] if tou_matches else extract_matches(ENERGY_CHARGE_RE, text, label="energy charge")
    )
    known_rates = {match.rate for match in matches}

    for tou_match in tou_matches:
        if any(
            existing.rate == tou_match.rate and existing.period == tou_match.period
            for existing in matches
        ):
            continue
        matches.append(tou_match)
        known_rates.add(tou_match.rate)

    for match in ENERGY_RATE_ONLY_RE.finditer(text):
        if tou_matches:
            continue
        snippet = text[max(0, match.start() - 80) : match.end() + 80].replace("\n", " ")
        # Skip rates in SSI/experimental footnotes (DEC RS tariff)
        if re.search(r"supplemental security income|SSI|experimental rate", snippet, re.I):
            continue
        rate = _fix_redlined_rate(match.group("amount").replace(",", ""))
        if rate is None or rate in known_rates:
            continue
        matches.append(ChargeMatch(label="energy charge", rate=rate, snippet=snippet))
        known_rates.add(rate)

    return _dedupe_charge_matches(matches)


def extract_demand_charge_matches(text: str) -> list[ChargeMatch]:
    matches = extract_matches(DEMAND_CHARGE_RE, text, label="demand charge")
    filtered: list[ChargeMatch] = []
    for match in matches:
        snippet_lower = match.snippet.lower()
        if not (re.search(r"\b(?:kw|kilowatt)\b", snippet_lower) and "kwh" not in snippet_lower):
            continue
        # Reject implausible values: real $/kW rates are $0.10–$80.
        # Values > 100 are billing thresholds or kW quantities, not rates.
        if match.rate is not None and match.rate > 100:
            continue
        # Reject values that look like kW quantities (whole numbers without $ context)
        # e.g. "Demand 1000 kW" where no "$" appears nearby
        if match.rate is not None and match.rate > 80:
            snippet_around = match.snippet
            if "$" not in snippet_around and "dollar" not in snippet_lower:
                continue
        filtered.append(match)
    return filtered


def extract_tou_periods(text: str) -> list[dict[str, str | list[str] | None]]:
    dep_table_periods = _extract_dep_tou_table_periods(text)
    if dep_table_periods:
        return dep_table_periods

    periods: list[dict[str, str | list[str] | None]] = []
    for name in ("Critical Peak", "On-Peak", "Discount", "Off-Peak", "Super Off-Peak"):
        section = _extract_tou_section(text, name)
        if not section:
            continue
        months = MONTH_RANGE_RE.findall(section)
        days = DAY_PATTERN_RE.findall(section)
        times = TIME_RANGE_RE.findall(section)
        if not times:
            periods.append(
                {
                    "name": name,
                    "months": [],
                    "weekday_hours": None,
                    "weekend_hours": None,
                }
            )
            continue
        for index, time_range in enumerate(times):
            day_text = days[index] if index < len(days) else None
            periods.append(
                {
                    "name": name,
                    "months": [_normalize_spaces(months[index])] if index < len(months) else [],
                    "weekday_hours": _normalize_spaces(time_range),
                    "weekend_hours": _normalize_spaces(time_range)
                    if day_text and day_text.lower().startswith("every day")
                    else None,
                }
            )
    return periods


def extract_riders(text: str) -> list[str]:
    codes = {reference.code for reference in extract_rider_references(text) if reference.code}
    return sorted(codes)


def summarize_text(text: str, *, max_chars: int = 800) -> str:
    return text[:max_chars].strip()


def has_tou(text: str) -> bool:
    return bool(TOU_RE.search(text))


def likely_customer_class(text: str) -> str | None:
    lowered = text.lower()
    for value in ("residential", "general service", "commercial", "industrial"):
        if value in lowered:
            return value
    return None


def iter_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def looks_like_summary_rate_matrix(title: str, text: str) -> bool:
    probe = f"{title}\n{text[:2000]}"
    if SUMMARY_RATE_MATRIX_RE.search(probe):
        return True

    if "rate schedule" not in probe.lower():
        return False

    tokens = {
        token
        for token in SCHEDULE_TOKEN_RE.findall(text[:800])
        if any(char.isdigit() for char in token) or "-" in token or len(token) >= 3
    }
    return len(tokens) >= 8


def _normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_probe_text(text: str) -> str:
    normalized = text.replace("-\n", "-").replace("\u00ad", "")
    normalized = re.sub(r"(?i)\bafter(from|between)\b", r"after \1", normalized)
    normalized = re.sub(r"(?i)\bon\s+and\s+after(from|between)\b", r"on and after \1", normalized)
    month_names = (
        r"January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    normalized = re.sub(
        rf"\b(?:{month_names})\s+((?:{month_names})\s+\d{{1,2}},\s+\d{{4}}\d?)",
        r"\1",
        normalized,
    )
    return normalized


def _float_or_none(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    normalized = value.strip()
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return float(normalized)
    except ValueError:
        return None


def _fix_redlined_rate(raw: "str | float") -> "float | None":
    """Parse and fix rates garbled by PDF redline text extraction.

    Handles two failure modes from redlined PDFs:

    1. **ValueError before conversion** — the regex matched a string like
       ``"14.3914.94"`` (old rate 14.39 concatenated with new rate 14.94).
       ``float("14.3914.94")`` raises ValueError.  We extract the rightmost
       decimal fragment as the current/new rate.

    2. **Implausible float** — concatenation survived ``float()`` (e.g.
       ``"12.91811.661"`` → 12918.11661).  Value > 100 is impossible for a
       real ¢/kWh or $/kWh rate; we apply the same rightmost-fragment fix.

    Accepts either a raw string (pre-float-conversion) or an already-converted
    float so callers do not need a separate try/except.  Returns ``None`` when
    the value cannot be recovered (caller should skip the match).
    """
    s = str(raw).strip()
    try:
        rate = float(s)
    except ValueError:
        # e.g. "14.3914.94" — two floats concatenated; take the rightmost
        m = re.search(r"(\d{1,3}\.\d{1,4})$", s)
        if m:
            candidate = float(m.group(1))
            if 0.001 <= candidate <= 100:
                return candidate
        return None

    # Implausibly large value — same rightmost-fragment extraction
    if rate > 100:
        m = re.search(r"(\d{1,3}\.\d{1,4})$", s)
        if m:
            candidate = float(m.group(1))
            if 0.001 <= candidate <= 100:
                return candidate

    return rate if rate > 0 else None


def _dedupe_charge_matches(matches: list[ChargeMatch]) -> list[ChargeMatch]:
    deduped: list[ChargeMatch] = []
    seen: set[tuple[str, float, str | None, float | None, float | None]] = set()
    for match in matches:
        key = (
            match.label.lower(),
            match.rate,
            match.season,
            match.block_from,
            match.block_to,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _extract_bills_rendered_seasonal_matches(block: str) -> list[ChargeMatch]:
    """Extract seasonal rates from the 'Bills Rendered During' two-column PDF layout.

    Pre-2023 DEP RES schedules present summer/winter rates side-by-side::

        Bills Rendered During July - October  Bills Rendered During November - June
        Kilowatt-Hour Charge:                 Kilowatt-Hour Charge:
        10.992¢ per kWh                       10.491¢ per kWh

    Depending on the PDF extraction method, both column headers may appear on a
    single merged line (pdfplumber full-page) or on separate lines (leaf-split
    text from pypdf).  We handle both cases.
    """
    # Normalise to a single flat string to detect the presence of both seasons
    flat = _normalize_spaces(block)
    header_m = re.search(
        r"Bills Rendered During ([A-Za-z]+ - [A-Za-z]+)"
        r"(?:.{0,80}?)Bills Rendered During ([A-Za-z]+ - [A-Za-z]+)",
        flat, re.I,
    )
    if not header_m:
        return []

    season1 = _normalize_spaces(header_m.group(1))  # e.g. "July - October"
    season2 = _normalize_spaces(header_m.group(2))  # e.g. "November - June"

    # Collect all ¢/kWh rate values that appear after the second season header.
    # In the two-column layout there will be exactly two: [summer_rate, winter_rate].
    after_header = flat[header_m.end():]
    all_rates = re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*[¢\xa2]\s*per\s*kWh", after_header, re.I)
    if len(all_rates) < 2:
        return []

    rate1 = _fix_redlined_rate(all_rates[0].replace(",", ""))
    rate2 = _fix_redlined_rate(all_rates[1].replace(",", ""))
    if rate1 is None or rate2 is None:
        return []

    return [
        ChargeMatch(
            label="Kilowatt-Hour Charge",
            rate=rate1,
            snippet=after_header[:120],
            season=season1,
            block_from=None,
            block_to=None,
        ),
        ChargeMatch(
            label="Kilowatt-Hour Charge",
            rate=rate2,
            snippet=after_header[:120],
            season=season2,
            block_from=None,
            block_to=None,
        ),
    ]


def _extract_for_billing_months_seasonal_matches(block: str) -> list[ChargeMatch]:
    """Extract seasonal rates from DEC RS 'For the billing months of X - Y' format.

    DEC Schedule RS (NC) uses::

        II. Energy Charges
        For the billing months of July - October
        For all kWh used per month, per kWh*  9.3826¢
        For the billing months of November - June
        For all kWh used per month, per kWh*  9.3826¢
    """
    flat = _normalize_spaces(block)
    # Must have at least one "For the billing months of" header
    if not re.search(r"For the billing months of", flat, re.I):
        return []

    results: list[ChargeMatch] = []
    season_header_re = re.compile(r"For the billing months of ([A-Za-z]+\s*[–-]\s*[A-Za-z]+)", re.I)
    # Split on season headers, processing each segment
    segments = season_header_re.split(flat)
    # segments: [pre, season1, text1, season2, text2, ...]
    i = 1
    while i + 1 < len(segments):
        season_raw = _normalize_spaces(segments[i]).replace("\u2013", "-")
        seg_text = segments[i + 1]
        # Find the first kWh rate in this segment (before the next season header).
        # DEC RS format: "per kWh* 9.3826¢" (number AFTER "per kWh", ¢ is suffix)
        # DEP format: "9.3826¢ per kWh" (number BEFORE "per kWh")
        rate_m = re.search(
            r"per\s+kWh\*?\s+(\d[\d,]*(?:\.\d+)?)\s*\xa2"  # DEC: per kWh* 9.3826¢
            r"|(\d[\d,]*(?:\.\d+)?)\s*[¢\xa2]\s*per\s*kWh",  # DEP: 9.3826¢ per kWh
            seg_text, re.I,
        )
        if rate_m:
            # group(1) = DEC format, group(2) = DEP format
            raw_rate = rate_m.group(1) or rate_m.group(2)
            rate = _fix_redlined_rate(raw_rate.replace(",", ""))
            if rate is not None:
                results.append(
                    ChargeMatch(
                        label="Kilowatt-Hour Charge",
                        rate=rate,
                        snippet=seg_text[:120],
                        season=season_raw,
                        block_from=None,
                        block_to=None,
                    )
                )
        i += 2
    return results


def _extract_seasonal_block_energy_matches(text: str) -> list[ChargeMatch]:
    # Look for "MONTHLY RATE" (DEP format) or standalone "RATE" section (DEC format)
    monthly_rate_index = text.find("MONTHLY RATE")
    if monthly_rate_index < 0:
        # Try DEC-style: "RATE\n" section header
        rate_m = re.search(r"\bRATE\b", text)
        monthly_rate_index = rate_m.start() if rate_m else -1
    if monthly_rate_index < 0:
        return []
    block = text[monthly_rate_index : monthly_rate_index + 2000]

    # Check for DEC's "For the billing months of X - Y" format first
    for_billing = _extract_for_billing_months_seasonal_matches(block)
    if for_billing:
        return for_billing

    # Check for the pre-2023 "Bills Rendered During" two-column layout
    bills_rendered = _extract_bills_rendered_seasonal_matches(block)
    if bills_rendered:
        return bills_rendered

    has_may_sep = "Service used during May - September" in block
    has_oct_apr = "Service used during October - April" in block
    season_matches: list[ChargeMatch] = []
    current_season: str | None = None
    for raw_line in block.splitlines():
        line = _normalize_spaces(raw_line)
        if not line:
            continue
        season_match = re.match(r"Service used during ([A-Za-z]+ - [A-Za-z]+)", line, re.I)
        if season_match:
            current_season = season_match.group(1)
            continue
        if "per kWh" not in line or "charge" in line.lower():
            continue
        rate_matches = re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*¢\s*per\s*kWh", line, re.I)
        if not rate_matches:
            continue
        # Take the last match: in redlined PDFs multiple ¢/kWh occurrences on a line
        # means the last is the current/new rate.
        rate = _fix_redlined_rate(rate_matches[-1].replace(",", ""))
        if rate is None:
            continue
        lowered = line.lower()
        block_from: float | None = None
        block_to: float | None = None
        season = current_season
        if "for all kwh" in lowered:
            block_from = 0.0
            if has_may_sep and has_oct_apr:
                season = "May - September"
        elif "for the first" in lowered:
            block_limit_match = re.search(r"for the first\s+(\d[\d,]*)\s*kwh", line, re.I)
            if block_limit_match:
                block_from = 0.0
                block_to = float(block_limit_match.group(1).replace(",", ""))
            if has_may_sep and has_oct_apr:
                season = "October - April"
        elif "for the additional kwh" in lowered:
            block_from = 800.0
            if has_may_sep and has_oct_apr:
                season = "October - April"
        season_matches.append(
            ChargeMatch(
                label="Kilowatt-Hour Charge",
                rate=rate,
                snippet=line,
                season=season,
                block_from=block_from,
                block_to=block_to,
            )
        )
    return season_matches


def _extract_code_from_rider_title(title: str) -> str | None:
    normalized_title = _normalize_spaces(title).upper()
    for phrase, code in KNOWN_RIDER_TITLE_CODES.items():
        if phrase in normalized_title:
            return code
    suffix_match = RIDER_SUFFIX_CODE_RE.fullmatch(title)
    if suffix_match:
        code = suffix_match.group(1).upper()
        return None if code in RIDER_CODE_STOPWORDS else code
    prefix_match = RIDER_CODE_RE.fullmatch(title)
    if prefix_match:
        code = prefix_match.group(1).upper()
        return None if code in RIDER_CODE_STOPWORDS else code
    title_suffix_match = TITLE_END_CODE_RE.search(title)
    if title_suffix_match:
        code = title_suffix_match.group(1).upper().strip("()/")
        return None if code in RIDER_CODE_STOPWORDS else code
    return None


def _extract_rider_code_from_text(text: str) -> str | None:
    for pattern in (
        RIDER_VERSION_RE,
        RIDER_CODE_RE,
        RIDER_SUFFIX_CODE_RE,
        RIDER_PAREN_CODE_RE,
        TITLE_END_CODE_RE,
    ):
        match = pattern.search(text)
        if not match:
            continue
        code = match.group(1).upper().strip("()/")
        if "-" in code:
            code = code.split("-", maxsplit=1)[0]
        if code not in RIDER_CODE_STOPWORDS:
            return code
    return None


def _extract_schedule_code_from_top_lines(text: str) -> str | None:
    for line in text.splitlines()[:20]:
        stripped = _normalize_spaces(line).upper()
        if not stripped or stripped.startswith("SHEET "):
            continue
        matches = SCHEDULE_CODE_TOKEN_RE.findall(stripped)
        for candidate in matches:
            if candidate in {"DUKE", "ENERGY", "PROGRESS", "CAROLINAS"}:
                continue
            if "-" in candidate or candidate in {"RES", "SGS", "MGS", "LGS", "RS"}:
                return candidate
    return None


def _extract_rate_class_block(text: str, rate_class: str) -> str | None:
    start = text.find(rate_class)
    if start < 0:
        return None
    next_positions = [
        text.find(candidate, start + len(rate_class))
        for candidate in (
            *[heading for heading in RATE_CLASS_HEADINGS if heading != rate_class],
            "Billing Adjustment Factors Description:",
            "Demand Side Management/Energy Efficiency",
            "APPLICABILITY – RATES NOT INCLUDED IN TARIFF CHARGES",
            "SALES TAX",
        )
    ]
    candidates = [position for position in next_positions if position > start]
    end = min(candidates) if candidates else len(text)
    return text[start:end]


def _extract_applicable_schedules_text(block: str) -> str:
    match = re.search(r"Applicable(?:\s+to)?\s+Schedule\(s\)\s*:?", block, re.I)
    if not match:
        match = re.search(r"Applicable(?:\s+to)?\s+Schedules\s*:?", block, re.I)
    if not match:
        return ""
    tail = block[match.end() :]
    lines: list[str] = []
    for raw_line in tail.splitlines():
        line = _normalize_spaces(raw_line)
        if not line:
            if lines:
                break
            continue
        if line.startswith("*") or line.startswith("Billing Adjustment Factors Description"):
            break
        lines.append(line)
        if "&" in line or "," in line:
            continue
    return " ".join(lines)


def _extract_schedule_codes_from_text(text: str) -> list[str]:
    return _dedupe_strings(
        code
        for code in SCHEDULE_CODE_TOKEN_RE.findall(text.upper())
        if code not in APPLICABLE_SCHEDULE_STOPWORDS
    )


def _extract_tou_section(text: str, period_name: str) -> str | None:
    rating_block = text[text.find("Rating Periods:") :] if "Rating Periods:" in text else text
    pattern = re.compile(
        (
            rf"(?:\([a-z]\)\s+)?{re.escape(period_name)}\s+Periods?\s*[:\-].*?"
            rf"(?=(?:\([a-z]\)\s+[A-Za-z-]+\s+Periods?)|(?:[A-Za-z-]+\s+Periods?\s*[:\-])|SECTION NO\.|$)"
        ),
        re.I | re.S,
    )
    match = pattern.search(rating_block)
    return match.group(0) if match else None


def _normalize_period_name(value: str) -> str:
    normalized = _normalize_spaces(value).replace("on-peak", "On-Peak")
    normalized = normalized.replace("off-peak", "Off-Peak")
    normalized = normalized.replace("super Off-Peak", "Super Off-Peak")
    normalized = normalized.replace("critical peak", "Critical Peak")
    if normalized.lower() == "discount":
        return "Discount"
    return normalized


def _extract_dep_tou_table_periods(text: str) -> list[dict[str, str | list[str] | None]]:
    probe = _normalized_probe_text(text)
    if "DETERMINATION OF ON-PEAK" not in probe:
        return []

    block = probe[probe.find("DETERMINATION OF ON-PEAK") :]
    rows: list[dict[str, str | list[str] | None]] = []

    if re.search(r"On-Peak Period:\s+Monday", block, re.I):
        summer_times = TIME_RANGE_RE.findall(block)
        if len(summer_times) >= 2:
            rows.append(
                {
                    "name": "On-Peak",
                    "months": ["May through September"],
                    "weekday_hours": _normalize_spaces(summer_times[0]),
                    "weekend_hours": None,
                }
            )
            rows.append(
                {
                    "name": "On-Peak",
                    "months": ["October through April"],
                    "weekday_hours": _normalize_spaces(summer_times[1]),
                    "weekend_hours": None,
                }
            )
        if len(summer_times) >= 4:
            rows.append(
                {
                    "name": "Discount",
                    "months": ["May through September"],
                    "weekday_hours": _normalize_spaces(summer_times[2]),
                    "weekend_hours": _normalize_spaces(summer_times[2]),
                }
            )
            rows.append(
                {
                    "name": "Discount",
                    "months": ["October through April"],
                    "weekday_hours": f"{_normalize_spaces(summer_times[3])}; {_normalize_spaces(summer_times[4])}" if len(summer_times) >= 5 else _normalize_spaces(summer_times[3]),
                    "weekend_hours": f"{_normalize_spaces(summer_times[3])}; {_normalize_spaces(summer_times[4])}" if len(summer_times) >= 5 else _normalize_spaces(summer_times[3]),
                }
            )
        if "All hours that are not On-Peak" in block:
            rows.append(
                {
                    "name": "Off-Peak",
                    "months": ["all calendar months"],
                    "weekday_hours": None,
                    "weekend_hours": None,
                }
            )
        return rows

    return []


def _normalize_heading(value: str) -> str:
    words = _normalize_spaces(value).split(" ")
    normalized_words: list[str] = []
    for word in words:
        if "-" in word:
            normalized_words.append("-".join(_normalize_heading(part) for part in word.split("-")))
        elif word.isupper() and len(word) <= 4:
            normalized_words.append(word)
        else:
            normalized_words.append(word.capitalize())
    return " ".join(normalized_words)


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_spaces(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _extract_residential_summary_total_cents(text: str) -> float | None:
    match = re.search(
        r"Residential Service Schedules.*?TOTAL cents/kWh\s+(-?\d+(?:\.\d+)?)",
        _normalized_probe_text(text),
        re.I | re.S,
    )
    return _float_or_none(match.group(1)) if match else None


def _extract_residential_ba_net_adjustment(text: str) -> float | None:
    for row in extract_rider_adjustment_rows(text):
        if row.rate_class == "Residential":
            return row.net_adjustment_cents_per_kwh
    return None


def _extract_clean_energy_monthly_charge(text: str) -> float | None:
    match = re.search(
        (
            r"Residential\s+\$\s*(\d+(?:\.\d+)?)\s*per month\s+"
            r"\$\s*(\d+(?:\.\d+)?)\s*per month\s+"
            r"\$\s*(\d+(?:\.\d+)?)\s*per month"
        ),
        _normalized_probe_text(text),
        re.I | re.S,
    )
    if not match:
        return None
    return _float_or_none(match.group(3))


def _extract_residential_table_rate(text: str) -> float | None:
    probe = " ".join(_normalized_probe_text(text).split())
    match = re.search(
        (
            r"Residential\s+(?:Applicable\s+to\s+Schedules:\s+)?RES\b.*?"
            r"(\(?-?\d+(?:\.\d+)?\)?)\s+"
            r"(?=Small General Service|General Service \(Small\)|"
            r"Medium General Service|General Service \(Medium\)|"
            r"Seasonal and Intermittent Service|Traffic Signal Service|"
            r"Outdoor Lighting|Lighting|Demand Rate Classes|$)"
        ),
        probe,
        re.I | re.S,
    )
    if match:
        return _float_or_none(match.group(1))

    fallback = re.search(
        r"Residential\b.*?(\(?-?\d+(?:\.\d+)?\)?)\s*¢\s*per\s*kilowatt-hour",
        probe,
        re.I | re.S,
    )
    return _float_or_none(fallback.group(1)) if fallback else None


def _extract_summary_component_rate(text: str, *, rider_code: str) -> float | None:
    probe = _normalized_probe_text(text)
    if rider_code == "CPRE":
        match = re.search(
            r"Net\s+CPRE\s+Rider\s+Factor\s+(\(?-?\d+(?:\.\d+)?\)?)\s*¢/kWh",
            probe,
            re.I,
        )
        return _float_or_none(match.group(1)) if match else None
    if rider_code in {"RDM", "ESM", "PIM"}:
        match = re.search(
            r"is\s+(\(?-?\d+(?:\.\d+)?\)?)\s*¢\s*per\s*kilowatt-hour",
            probe,
            re.I,
        )
        return _float_or_none(match.group(1)) if match else None

    residential_rate = _extract_residential_table_rate(probe)
    if residential_rate is None:
        return None

    if rider_code in {"JAA", "CAR"} and (
        "$/kWh" in probe or "dollars per kilowatt-hour" in probe.lower()
    ):
        return round(residential_rate * 100.0, 3)
    return residential_rate


def _extract_recd_percent(text: str) -> float | None:
    match = re.search(
        r"RECD Credit\s*=\s*(\d+(?:\.\d+)?)%\s+times the stated kilowatt and kilowatt-hour charges",
        _normalized_probe_text(text),
        re.I,
    )
    return _float_or_none(match.group(1)) if match else None


def _extract_reps_fixed_charges(text: str) -> list[RiderChargeComponentMatch]:
    probe = " ".join(_normalized_probe_text(text).split())
    patterns = (
        (
            r"monthly REPS riders per customer account.*?"
            r"\$([0-9]+(?:\.[0-9]+)?) for residential accounts"
        ),
        (
            r"monthly REPS EMF riders per customer account.*?"
            r"\$([0-9]+(?:\.[0-9]+)?) for residential accounts"
        ),
        r"REPS rider charges.*?Residential\s*-\s*\$([0-9]+(?:\.[0-9]+)?)",
        (
            r"following total REPS rates.*?"
            r"\$([0-9]+(?:\.[0-9]+)?) per month for residential customers"
        ),
        (
            r"combined monthly REPS and REPS EMF Rider charges per customer account.*?"
            r"\$([0-9]+(?:\.[0-9]+)?) for residential customers"
        ),
    )
    matches: list[RiderChargeComponentMatch] = []
    for pattern in patterns:
        match = re.search(pattern, probe, re.I)
        if not match:
            continue
        value = _float_or_none(match.group(1))
        if value is None:
            continue
        bill_label = "REPS Rider"
        if "EMF" in match.group(0).upper():
            bill_label = "REPS EMF Rider"
        matches.append(
            RiderChargeComponentMatch(
                bill_label=bill_label,
                rate_class="Residential",
                value=value,
                unit="fixed_monthly",
                applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
            )
        )
    deduped: list[RiderChargeComponentMatch] = []
    seen: set[tuple[str, float]] = set()
    for component in matches:
        key = (component.bill_label, component.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(component)
    return deduped


def _extract_dsm_ee_order_table_components(
    text: str,
    *,
    rider_code: str,
) -> list[RiderChargeComponentMatch]:
    """Extract DSM/EE rates from tabular order text like:
    '0.611 cents per kWh for the Residential class'
    or 'are: 0.611 cents per kWh for the Residential class'
    """
    probe = " ".join(_normalized_probe_text(text).split())
    label = "Energy Efficiency Rider" if rider_code.upper() == "EE" else "Demand Side Management Rider"
    noun = "EE" if rider_code.upper() == "EE" else "DSM"
    components: list[RiderChargeComponentMatch] = []

    # Pattern: "X.XXX cents per kWh for the Residential class"
    rate_class_pattern = re.compile(
        rf"(\d+(?:\.\d+)?)\s+cents?\s+per\s+kwh\s+for\s+(?:the\s+)?({noun}[^.;]{{0,30}}?"
        r"(?:residential|general service|lighting)[^.;]{0,30}?(?:class|customers?)?)",
        re.I,
    )
    # Pattern covering "appropriate forward-looking DSM/EE rates ... are: X.XXX cents per kWh for the Residential class"
    bulk_pattern = re.compile(
        r"(?:dsm/ee|dsm|ee)\s+rates?[^.;]{0,120}?are\s*:\s*([\d.]+)\s+cents?\s+per\s+kwh\s+"
        r"for\s+(?:the\s+)?(?P<class>residential)[^.;]{0,60}?"
        r"(?:,\s*([\d.]+)\s+cents?\s+per\s+kwh\s+for\s+(?:the\s+)?(?P<gs>general\s+service)[^.;]{0,60}?)?"
        r"(?:,\s*([\d.]+)\s+cents?\s+per\s+kwh\s+for\s+(?:the\s+)?(?P<lighting>lighting))?",
        re.I,
    )
    bulk_match = bulk_pattern.search(probe)
    if bulk_match:
        res_val = _float_or_none(bulk_match.group(1))
        if res_val is not None:
            components.append(
                RiderChargeComponentMatch(
                    bill_label=label,
                    rate_class="Residential",
                    value=res_val,
                    unit="cents_per_kwh",
                    applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                )
            )
        gs_val = _float_or_none(bulk_match.group(3))
        if gs_val is not None:
            components.append(
                RiderChargeComponentMatch(
                    bill_label=label,
                    rate_class="General Service",
                    value=gs_val,
                    unit="cents_per_kwh",
                    applicable_schedules=GENERAL_SERVICE_SCHEDULES,
                )
            )
        lighting_val = _float_or_none(bulk_match.group(5))
        if lighting_val is not None:
            components.append(
                RiderChargeComponentMatch(
                    bill_label=label,
                    rate_class="Lighting",
                    value=lighting_val,
                    unit="cents_per_kwh",
                    applicable_schedules=["SLS", "SLR"],
                )
            )
    if not components:
        for match in rate_class_pattern.finditer(probe):
            val = _float_or_none(match.group(1))
            context = match.group(2).upper()
            if val is None:
                continue
            if "RESIDENTIAL" in context:
                components.append(
                    RiderChargeComponentMatch(
                        bill_label=label,
                        rate_class="Residential",
                        value=val,
                        unit="cents_per_kwh",
                        applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                    )
                )
    return components


def _extract_cei_monthly_charges(text: str) -> list[RiderChargeComponentMatch]:
    """Extract CEI (Clean Energy Impact) per-customer-per-month charges.
    Typical format: 'Residential ... $X.XX per month' or '$X.XX per customer per month'.
    """
    probe = " ".join(_normalized_probe_text(text).split())
    components: list[RiderChargeComponentMatch] = []

    # Pattern: "Residential $X.XX per month" (possibly per customer account)
    res_pattern = re.compile(
        r"residential[^.;$]{0,60}?\$\s*(\d+(?:\.\d+)?)\s*(?:per customer\s+)?per\s+(?:customer\s+)?(?:account\s+)?month",
        re.I,
    )
    # Pattern: "$X.XX per month for residential"
    res_pattern_alt = re.compile(
        r"\$\s*(\d+(?:\.\d+)?)\s*(?:per customer\s+)?per\s+month\s+for\s+(?:the\s+)?residential",
        re.I,
    )
    seen_values: set[float] = set()
    for pattern in (res_pattern, res_pattern_alt):
        match = pattern.search(probe)
        if match:
            val = _float_or_none(match.group(1))
            if val is not None and val not in seen_values:
                seen_values.add(val)
                components.append(
                    RiderChargeComponentMatch(
                        bill_label="Clean Energy Impact Rider",
                        rate_class="Residential",
                        value=val,
                        unit="fixed_monthly",
                        applicable_schedules=RESIDENTIAL_STANDARD_SCHEDULES,
                    )
                )
    return components


def _extract_dsm_ee_notice_components(
    text: str,
    *,
    rider_code: str,
) -> list[RiderChargeComponentMatch]:
    probe = " ".join(_normalized_probe_text(text).split())
    lowered_code = rider_code.upper()
    label = "Energy Efficiency Rider" if lowered_code == "EE" else "Demand Side Management Rider"
    noun = "EE" if lowered_code == "EE" else "DSM"
    schedule_scope = RESIDENTIAL_STANDARD_SCHEDULES
    components: list[RiderChargeComponentMatch] = []

    residential_pattern = re.compile(
        (
            rf"residential customers would see (?:a|an) {noun} rider "
            rf"(increase|decrease) of ([0-9]+(?:\.[0-9]+)?) cents per kwh"
        ),
        re.I,
    )
    general_service_pattern = re.compile(
        (
            rf"general service customers.*?{noun} rider "
            rf"(increase|decrease) of ([0-9]+(?:\.[0-9]+)?) cents per kwh"
        ),
        re.I,
    )
    lighting_pattern = re.compile(
        (
            rf"lighting customers would see (?:a|an) {noun} rider "
            rf"(increase|decrease) of ([0-9]+(?:\.[0-9]+)?) cents per kwh"
        ),
        re.I,
    )

    for pattern, rate_class, applicable_schedules in (
        (residential_pattern, "Residential", schedule_scope),
        (general_service_pattern, "General Service", GENERAL_SERVICE_SCHEDULES),
        (lighting_pattern, "Lighting", ["SLS", "SLR"]),
    ):
        match = pattern.search(probe)
        if not match:
            continue
        value = _float_or_none(match.group(2))
        if value is None:
            continue
        if match.group(1).lower() == "decrease":
            value *= -1
        components.append(
            RiderChargeComponentMatch(
                bill_label=label,
                rate_class=rate_class,
                value=value,
                unit="cents_per_kwh",
                applicable_schedules=applicable_schedules,
            )
        )
    return components
