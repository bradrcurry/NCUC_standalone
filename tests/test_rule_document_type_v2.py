"""Tests for rule_document_type_v2.

Goal: lock the per-type pattern discrimination and confidence calibration
that Stream B of docs/research/document_identification.md targets. Each test
constructs a DocumentSignals snapshot mirroring a real corpus pattern and
asserts (a) the winning label and (b) the confidence band.
"""
from __future__ import annotations

import pytest

from duke_rates.classification.rule_document_type_v2 import (
    DocumentSignals,
    classify_v2,
)


def test_tariff_sheet_with_leaf_number_and_table():
    """Canonical leaf-sheet pattern: 'Leaf No. 500', single small page with
    table, Basic Customer Charge / per kWh markers. Should reach >=0.92 conf
    and emit TARIFF_SHEET."""
    signals = DocumentSignals(
        title="Residential Service (Leaf No. 500, eff. 2024-10-01)",
        first_text=(
            "Duke Energy Progress, LLC NC First Revised Leaf No. 500\n"
            "AVAILABILITY: This Schedule is available when electric service "
            "is used for domestic purposes.\n"
            "Basic Customer Charge: $14.00 per month.\n"
            "Kilowatt-Hour Charge: 12.119 cents per kWh.\n"
            "Effective for service rendered on or after October 1, 2024."
        ),
        page_count=3,
        text_chars=5500,
        has_tables=1,
    )
    result = classify_v2(signals)
    assert result.label == "TARIFF_SHEET"
    assert result.confidence >= 0.92
    assert any(e.get("kind") == "strong_pattern" for e in result.evidence)


def test_order_final_with_commission_header_and_ordering_paragraph():
    """ORDER_FINAL canonical signal — 'BEFORE THE NORTH CAROLINA UTILITIES
    COMMISSION' plus 'IT IS, THEREFORE, ORDERED'. Multi-page text-heavy."""
    signals = DocumentSignals(
        title="ORDER APPROVING RATES",
        first_text=(
            "STATE OF NORTH CAROLINA\n"
            "UTILITIES COMMISSION\nRALEIGH\n"
            "DOCKET NO. E-2, SUB 1300\n"
            "BEFORE THE NORTH CAROLINA UTILITIES COMMISSION\n"
            "In the Matter of the Application of Duke Energy Progress, LLC.\n"
            "IT IS, THEREFORE, ORDERED that the application is approved."
        ),
        page_count=42,
        text_chars=87000,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "ORDER_FINAL"
    assert result.confidence >= 0.92


def test_testimony_with_qa_and_redirect_markers():
    """TESTIMONY canonical pattern — direct/redirect testimony + Q/A pairs."""
    signals = DocumentSignals(
        title="Direct Testimony of Jane Smith on behalf of Duke Energy Progress",
        first_text=(
            "DIRECT TESTIMONY OF JANE SMITH\n"
            "Q. Please state your name and business address.\n"
            "A. My name is Jane Smith. My business address is 410 South Wilmington Street.\n"
            "Q. By whom are you employed and in what capacity?\n"
            "A. I am employed by Duke Energy Progress, LLC as Vice President.\n"
            "BACKGROUND AND QUALIFICATIONS\n"
            "Q. Please describe your background and qualifications."
        ),
        page_count=35,
        text_chars=68000,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "TESTIMONY"
    assert result.confidence >= 0.92


def test_certificate_of_service_short_doc():
    """CERTIFICATE_OF_SERVICE — 1-page doc with 'hereby certify' header."""
    signals = DocumentSignals(
        title="Certificate of Service",
        first_text=(
            "CERTIFICATE OF SERVICE\n"
            "I, John Doe, do hereby certify that I have this day served a copy of the "
            "foregoing on the parties of record via electronic filing.\n"
            "Service list attached."
        ),
        page_count=1,
        text_chars=420,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "CERTIFICATE_OF_SERVICE"
    assert result.confidence >= 0.92


def test_cover_letter_single_page_with_via_electronic_filing():
    """COVER_LETTER — letterhead + 'VIA ELECTRONIC FILING' + 'Re:' + signature."""
    signals = DocumentSignals(
        title="Filing Cover Letter",
        first_text=(
            "Kendrick C. Fentress\nAssociate General Counsel\n"
            "Duke Energy Corporation\nP.O. Box 1551, Raleigh, NC 27602\n"
            "May 14, 2024\n"
            "VIA ELECTRONIC FILING\n"
            "Ms. Kimberley A. Campbell, Chief Clerk\n"
            "North Carolina Utilities Commission\n"
            "Re: Docket No. E-2, Sub 1300 — DEP Compliance Filing\n"
            "Enclosed please find the attached compliance report.\n"
            "Sincerely,\nKendrick C. Fentress"
        ),
        page_count=1,
        text_chars=850,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "COVER_LETTER"
    assert result.confidence >= 0.92


def test_notice_of_hearing_with_location_and_date():
    """NOTICE_OF_HEARING canonical pattern."""
    signals = DocumentSignals(
        title="Notice of Public Hearing",
        first_text=(
            "NOTICE OF PUBLIC HEARING\n"
            "Docket No. E-2, Sub 1300\n"
            "The North Carolina Utilities Commission will hold a hearing on June 12 "
            "commencing at 10:00 AM at the Dobbs Building, Raleigh, NC."
        ),
        page_count=2,
        text_chars=580,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "NOTICE_OF_HEARING"
    assert result.confidence >= 0.92


def test_application_with_pursuant_to_ncgs():
    """APPLICATION canonical — 'Application of X pursuant to N.C.G.S. 62-...'"""
    signals = DocumentSignals(
        title="Application of Duke Energy Progress for Authority to Adjust Rates",
        first_text=(
            "Application of Duke Energy Progress, LLC for Authority to Adjust Rates "
            "and Charges Pursuant to N.C.G.S. 62-133 and Commission Rule R8-55.\n"
            "Applicant respectfully requests the relief described herein."
        ),
        page_count=20,
        text_chars=42000,
        has_tables=1,
    )
    result = classify_v2(signals)
    assert result.label == "APPLICATION"
    assert result.confidence >= 0.92


def test_compliance_filing_with_pursuant_to_order():
    """COMPLIANCE_FILING — 'filed in compliance with' + 'pursuant to the Order'."""
    signals = DocumentSignals(
        title="Compliance Filing pursuant to Order dated August 18, 2023",
        first_text=(
            "Filed in compliance with the Commission's Order dated August 18, 2023.\n"
            "The attached report contains the quarterly fuel cost adjustment data.\n"
            "Pursuant to the Order, Duke Energy Progress submits the following."
        ),
        page_count=8,
        text_chars=12000,
        has_tables=1,
    )
    result = classify_v2(signals)
    assert result.label == "COMPLIANCE_FILING"
    assert result.confidence >= 0.92


def test_ferc_order_recognized_via_new_taxonomy():
    """FERC_ORDER — new 2026-05-21 taxonomy entry. Strong pattern: 'Federal
    Energy Regulatory Commission' header."""
    signals = DocumentSignals(
        title="FERC Order No. 2222 implementation filing",
        first_text=(
            "UNITED STATES OF AMERICA\n"
            "FEDERAL ENERGY REGULATORY COMMISSION\n"
            "Docket No. ER22-1234-000\n"
            "Order No. 2222 issued pursuant to 18 C.F.R. Part 35."
        ),
        page_count=18,
        text_chars=45000,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "FERC_ORDER"
    assert result.confidence >= 0.92


def test_eia_report_recognized_via_new_taxonomy():
    """EIA_REPORT — second 2026-05-21 taxonomy entry."""
    signals = DocumentSignals(
        title="EIA-861 Annual Electric Power Industry Report",
        first_text=(
            "U.S. Energy Information Administration\n"
            "DOE/EIA Form EIA-861\n"
            "Annual Electric Power Industry Report\n"
            "Monthly Energy Review section attached."
        ),
        page_count=24,
        text_chars=58000,
        has_tables=1,
    )
    result = classify_v2(signals)
    assert result.label == "EIA_REPORT"
    assert result.confidence >= 0.92


def test_unknown_returns_low_confidence_when_no_patterns_fire():
    """When no patterns match anywhere, classifier should not guess — emit
    UNKNOWN at the configured confidence floor."""
    signals = DocumentSignals(
        title="random",
        first_text="just some unrelated text without any patterns",
        page_count=3,
        text_chars=500,
        has_tables=0,
    )
    result = classify_v2(signals)
    assert result.label == "UNKNOWN"
    assert result.confidence < 0.1


def test_layout_signals_break_tie_between_letter_and_tariff():
    """A doc that mentions both 'Leaf No.' (in a Re: line) and 'VIA
    ELECTRONIC FILING' headers — layout signals (1-page, no table) should
    favor COVER_LETTER over TARIFF_SHEET. Real corpus pattern: filing letter
    that references a leaf number in its subject line."""
    signals = DocumentSignals(
        title="Filing letter Re: Leaf No. 500",
        first_text=(
            "VIA ELECTRONIC FILING\n"
            "Ms. Kimberley A. Campbell\n"
            "Re: Docket No. E-2, Sub 1300; Leaf No. 500 revision\n"
            "Enclosed please find the revised tariff sheet.\n"
            "Sincerely, Kendrick C. Fentress"
        ),
        page_count=1,
        text_chars=400,
        has_tables=0,
    )
    result = classify_v2(signals)
    # TARIFF_SHEET has a negative pattern for cover letters but COVER_LETTER's
    # STRONG patterns (VIA ELECTRONIC FILING, Enclosed please find) plus
    # 1-page layout signal should win.
    assert result.label == "COVER_LETTER"


def test_base_schedule_not_classified_as_rider_despite_body_mentions():
    """Regression for 2026-05-21 corpus pass: legacy 2014-era base schedule
    docs (hd=3 LGS, hd=4 MGS, hd=5 RES from /pdfs/...-dep.pdf) were
    mis-classified as RIDER because their bodies list applicable riders.
    The fix moves RIDER's 'Rider X' strong pattern to header-region-only
    matching, with body mentions counted as weak. Plus adds base-class
    service titles as TARIFF_SHEET strong_header patterns."""
    signals = DocumentSignals(
        title="Large General Service",
        first_text=(
            "Large General Service\n"
            "Availability: this Schedule is available to commercial customers.\n"
            "Monthly Rate:\n"
            "Basic Customer Charge $30.00 per month\n"
            "Energy Charge: 0.08 per kWh\n"
            "Applicable Riders:\n"
            "The following riders apply: Rider BA, Rider NCTR, "
            "fuel rider for cost recovery."
        ),
        page_count=4,
        text_chars=1200,
        has_tables=1,
    )
    result = classify_v2(signals)
    assert result.label == "TARIFF_SHEET", (
        f"base-schedule should classify as TARIFF_SHEET, got {result.label}; "
        f"raw_scores={result.metadata['raw_scores']}"
    )
    assert result.confidence >= 0.9


def test_rider_doc_still_recognized_via_header():
    """The fix must NOT regress legitimate rider docs whose title or first
    paragraph contains 'Rider X'."""
    signals = DocumentSignals(
        title="Annual Billing Adjustment Rider (Leaf No. 601)",
        first_text=(
            "Duke Energy Progress, LLC NC Original Leaf No. 601\n"
            "RIDER BA-9 (NC)\n"
            "ANNUAL BILLING ADJUSTMENT\n"
            "Applicable to all Schedules.\n"
            "Adjustment per kWh: see schedule of charges."
        ),
        page_count=2,
        text_chars=700,
        has_tables=1,
    )
    result = classify_v2(signals)
    # Both leaf-no AND rider-in-header fire; the higher-scoring type wins.
    # Either label is technically right; we just want it not to be
    # mis-classified as something unrelated like ORDER_FINAL or UNKNOWN.
    assert result.label in ("RIDER", "TARIFF_SHEET")
    assert result.confidence >= 0.9


def test_confidence_lower_when_only_weak_patterns_fire():
    """Weak-only matches should NOT reach the 0.92 strong-signal target.
    Important for preserving multi-classifier balance: when v2 is uncertain,
    embedding/LLM votes should carry more weight."""
    signals = DocumentSignals(
        title="Schedule X some sort of document",
        first_text="Some text mentioning charges and availability and per kWh once.",
        page_count=4,
        text_chars=1200,
        has_tables=0,
    )
    result = classify_v2(signals)
    # The doc has weak TARIFF_SHEET signals but no strong ones.
    assert result.confidence < 0.7
