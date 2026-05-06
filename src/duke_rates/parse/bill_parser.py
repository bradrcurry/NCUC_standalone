from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from duke_rates.models.bill import BillLineItem, BillSection, BillStatementData

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
AMOUNT_PATTERN = re.compile(r"^\$?-?(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}$")
RATE_DETAIL_PATTERN = re.compile(
    r"(?P<quantity>[\d,]+(?:\.\d+)?)"
    r"(?:\s+(?P<unit>kWh|kW))?"
    r"\s*@\s*\$(?P<rate>[\d.]+)"
)
RATE_NAME_PATTERN = re.compile(r"Your current rate is (?P<name>.+?) \((?P<code>[^)]+)\)\.")
SUBPERIOD_PATTERN = re.compile(
    r"^(?P<label>.+?) - (?P<start>[A-Z][a-z]{2} \d{1,2}) to (?P<end>[A-Z][a-z]{2} \d{1,2})$"
)


def parse_bill_text(text: str, *, source_path: str | Path) -> BillStatementData:
    source_path = str(source_path)
    bill = BillStatementData(source_path=source_path)
    bill.account_number = _extract_account_number(text)
    bill.bill_date = _extract_bill_date(text)
    bill.bill_days = _extract_bill_days(text)
    bill.service_start, bill.service_end = _extract_front_page_service_period(
        text,
        bill.bill_date,
    )
    bill.customer_name, bill.service_address_lines = _extract_service_address(text)
    _populate_billing_summary(text, bill)
    bill.lighting_section = _parse_bill_section(text, "Lighting", bill.bill_date)
    bill.electric_section = _parse_bill_section(text, "Electric", bill.bill_date)
    bill.tax_section = _parse_bill_section(text, "Taxes", bill.bill_date)
    bill.due_date = bill.billing_summary.due_date
    return bill


def _extract_account_number(text: str) -> str | None:
    match = re.search(r"\n(\d{4}\s\d{4}\s\d{4})\nYour Energy Bill", text)
    return match.group(1) if match else None


def _extract_bill_date(text: str) -> date | None:
    match = re.search(r"\n([A-Z][a-z]{2} \d{1,2}, \d{4})\n\d+\s+days\nFor service", text)
    return _parse_long_date(match.group(1)) if match else None


def _extract_bill_days(text: str) -> int | None:
    match = re.search(r"\n(\d+)\s+days\nFor service", text)
    return int(match.group(1)) if match else None


def _extract_front_page_service_period(
    text: str,
    bill_date: date | None,
) -> tuple[date | None, date | None]:
    match = re.search(r"For service\s+([A-Z][a-z]{2} \d{1,2}) - ([A-Z][a-z]{2} \d{1,2})", text)
    if not match or bill_date is None:
        return None, None
    end = _parse_month_day(match.group(2), year=bill_date.year)
    if end is None:
        return None, None
    start_year = bill_date.year if MONTHS[match.group(1)[:3]] <= end.month else bill_date.year - 1
    start = _parse_month_day(match.group(1), year=start_year)
    return start, end


def _extract_service_address(text: str) -> tuple[str | None, list[str]]:
    match = re.search(
        r"For service\s+[A-Z][a-z]{2} \d{1,2} - [A-Z][a-z]{2} \d{1,2}\n(.+?)\nBilling summary",
        text,
        re.S,
    )
    if not match:
        return None, []
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    if not lines:
        return None, []
    return lines[0], lines[1:]


def _populate_billing_summary(text: str, bill: BillStatementData) -> None:
    block = _extract_block(text, "Billing summary", ["Your usage snapshot"])
    if not block:
        return
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    summary = bill.billing_summary
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "Previous Amount Due" and i + 1 < len(lines):
            summary.previous_amount_due = _parse_amount(lines[i + 1])
            i += 2
            continue
        if line.startswith("Payment Received") and i + 1 < len(lines):
            summary.payment_received = _parse_amount(lines[i + 1])
            payment_date_match = re.search(r"Payment Received\s+([A-Z][a-z]{2} \d{1,2})", line)
            if payment_date_match and bill.bill_date is not None:
                summary.payment_received_date = _parse_month_day(
                    payment_date_match.group(1),
                    year=bill.bill_date.year,
                )
            i += 2
            continue
        if line == "Current Lighting Charges" and i + 1 < len(lines):
            summary.current_lighting_charges = _parse_amount(lines[i + 1])
            i += 2
            continue
        if line == "Current Electric Charges" and i + 1 < len(lines):
            summary.current_electric_charges = _parse_amount(lines[i + 1])
            i += 2
            continue
        if line == "Taxes" and i + 1 < len(lines):
            summary.taxes = _parse_amount(lines[i + 1])
            i += 2
            continue
        if line.startswith("Total Amount Due") and i + 1 < len(lines):
            due_match = re.search(r"Total Amount Due\s+([A-Z][a-z]{2} \d{1,2})", line)
            if due_match and bill.bill_date is not None:
                year = bill.bill_date.year
                if MONTHS[due_match.group(1)[:3]] < bill.bill_date.month:
                    year += 1
                summary.due_date = _parse_month_day(due_match.group(1), year=year)
            summary.total_amount_due = _parse_amount(lines[i + 1])
            i += 2
            continue
        i += 1


def _parse_bill_section(text: str, section_name: str, bill_date: date | None) -> BillSection | None:
    headers = ["Lighting", "Electric", "Taxes"]
    next_headers = [f"Billing details - {name}" for name in headers if name != section_name]
    next_headers.extend(["For a complete listing", "A rider is a mechanism used"])
    block = _extract_block(text, f"Billing details - {section_name}", next_headers)
    if not block:
        return None

    lines = [line.strip() for line in block.splitlines() if line.strip()]
    section = BillSection(name=section_name)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Billing details - "):
            i += 1
            continue
        if line.startswith("Billing Period - "):
            section.billing_period_start, section.billing_period_end = _parse_section_period(
                line,
                bill_date,
            )
            i += 1
            continue
        if line.startswith("Meter - "):
            section.meter_number = line.removeprefix("Meter - ").strip()
            i += 1
            continue
        rate_match = RATE_NAME_PATTERN.match(line)
        if not rate_match and i + 1 < len(lines):
            rate_match = RATE_NAME_PATTERN.match(f"{line} {lines[i + 1]}")
        if rate_match:
            section.rate_name = rate_match.group("name")
            section.rate_code = re.sub(r"\s+", "", rate_match.group("code"))
            i += 2 if i + 1 < len(lines) and lines[i + 1].startswith("(") else 1
            continue
        if line in {
            "For a complete listing of all North Carolina rates and riders, visit",
            "duke-energy.com/rates",
        }:
            break
        if line in {"Total Current Charges", "Total Taxes"} and i + 1 < len(lines):
            section.total_current_charges = _parse_amount(lines[i + 1])
            i += 2
            continue
        if _is_charge_label(line):
            detail_lines: list[str] = []
            j = i + 1
            while (
                j < len(lines)
                and not AMOUNT_PATTERN.match(lines[j])
                and not _is_section_control_line(lines[j])
            ):
                detail_lines.append(lines[j])
                j += 1
            amount = (
                _parse_amount(lines[j])
                if j < len(lines) and AMOUNT_PATTERN.match(lines[j])
                else None
            )
            section.line_items.append(
                _build_line_item(
                    label=line,
                    detail_lines=detail_lines,
                    amount=amount,
                    section=section,
                )
            )
            i = j + 1 if amount is not None else j
            continue
        i += 1
    return section


def _build_line_item(
    *,
    label: str,
    detail_lines: list[str],
    amount: float | None,
    section: BillSection,
) -> BillLineItem:
    item = BillLineItem(label=label, amount=amount)
    subperiod_match = SUBPERIOD_PATTERN.match(label)
    if subperiod_match:
        item.label = subperiod_match.group("label")
        item.is_subperiod_detail = True
        start, end = _infer_subperiod_dates(
            subperiod_match.group("start"),
            subperiod_match.group("end"),
            section=section,
        )
        item.period_start = start
        item.period_end = end

    details = [detail for detail in detail_lines if detail]
    if details:
        for detail in reversed(details):
            match = RATE_DETAIL_PATTERN.search(detail)
            if match:
                item.quantity = _parse_float(match.group("quantity"))
                item.unit = match.group("unit")
                item.rate = _parse_float(match.group("rate"))
                break
        non_rate_details = [detail for detail in details if not RATE_DETAIL_PATTERN.search(detail)]
        if non_rate_details:
            item.detail = " | ".join(non_rate_details)
    return item


def _extract_block(text: str, start_header: str, end_headers: list[str]) -> str | None:
    start = text.find(start_header)
    if start == -1:
        return None
    end_positions = [text.find(header, start + len(start_header)) for header in end_headers]
    end_candidates = [pos for pos in end_positions if pos != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def _infer_subperiod_dates(
    start_label: str,
    end_label: str,
    *,
    section: BillSection,
) -> tuple[date | None, date | None]:
    section_start = section.billing_period_start
    section_end = section.billing_period_end
    if section_start is None and section_end is None:
        return None, None

    candidate_years: list[int] = []
    if section_start is not None:
        candidate_years.append(section_start.year)
    if section_end is not None and section_end.year not in candidate_years:
        candidate_years.append(section_end.year)

    start_candidates = [
        candidate
        for year in candidate_years
        if (candidate := _parse_month_day(start_label, year=year)) is not None
    ]
    end_candidates = [
        candidate
        for year in candidate_years
        if (candidate := _parse_month_day(end_label, year=year)) is not None
    ]

    valid_pairs: list[tuple[date, date]] = []
    for start in start_candidates:
        for end in end_candidates:
            if start > end:
                continue
            if section_start is not None and start < section_start:
                continue
            if section_end is not None and end > section_end:
                continue
            valid_pairs.append((start, end))

    if valid_pairs:
        return min(valid_pairs, key=lambda pair: (pair[0], pair[1]))

    start = start_candidates[0] if start_candidates else None
    end = end_candidates[0] if end_candidates else None
    if start and end and start > end and end.year == start.year:
        adjusted = _parse_month_day(start_label, year=start.year - 1)
        if adjusted is not None:
            start = adjusted
    return start, end


def _parse_section_period(line: str, bill_date: date | None) -> tuple[date | None, date | None]:
    match = re.search(
        r"Billing Period - ([A-Z][a-z]{2} \d{1,2} \d{2}) to ([A-Z][a-z]{2} \d{1,2} \d{2})",
        line,
    )
    if match:
        return _parse_short_year_date(match.group(1)), _parse_short_year_date(match.group(2))
    short_match = re.search(
        r"Billing Period - ([A-Z][a-z]{2} \d{1,2}) to ([A-Z][a-z]{2} \d{1,2})",
        line,
    )
    if short_match and bill_date is not None:
        end = _parse_month_day(short_match.group(2), year=bill_date.year)
        if end is None:
            return None, None
        start_year = (
            bill_date.year
            if MONTHS[short_match.group(1)[:3]] <= end.month
            else bill_date.year - 1
        )
        start = _parse_month_day(short_match.group(1), year=start_year)
        return start, end
    return None, None


def _is_section_control_line(line: str) -> bool:
    return line.startswith(
        (
            "Billing details - ",
            "Billing Period - ",
            "Meter - ",
            "Your current rate is ",
            "For a complete listing",
            "A rider is a mechanism used",
            "Billing details - Taxes continued",
            "Total Current Charges",
            "Total Taxes",
        )
    )


def _is_charge_label(line: str) -> bool:
    if AMOUNT_PATTERN.match(line):
        return False
    if _is_section_control_line(line):
        return False
    return bool(re.search(r"\b(Charge|Credit|Adjustments|Rider|Tax|Taxes)\b", line))


def _parse_amount(value: str) -> float | None:
    value = value.strip().replace("$", "").replace(",", "")
    return float(value) if value else None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    return float(value.replace(",", ""))


def _parse_long_date(value: str) -> date:
    month_text, day_text, year_text = re.match(
        r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})",
        value,
    ).groups()
    return date(int(year_text), MONTHS[month_text], int(day_text))


def _parse_month_day(value: str, *, year: int) -> date | None:
    match = re.match(r"([A-Z][a-z]{2}) (\d{1,2})", value)
    if not match:
        return None
    return date(year, MONTHS[match.group(1)], int(match.group(2)))


def _parse_short_year_date(value: str) -> date | None:
    match = re.match(r"([A-Z][a-z]{2}) (\d{1,2}) (\d{2})", value)
    if not match:
        return None
    return date(2000 + int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
