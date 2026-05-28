"""Tests for CarolinasMultiClassRateTableProfile.

Covers both the parenthesised-decrement layout (EDIT-4 tax credit) and the
unparenthesised-increment layout (STS storm cost recovery), and confirms
the profile is robust to the docling-flattened table layout where all
columns appear on one line.
"""
from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasMultiClassRateTableProfile,
)


# Docling-flattened EDIT-4 (current DEC tariff sheet, 2026-06-01)
EDIT4_FLATTENED = """\
Duke Energy Carolinas, LLC
NC Second Revised Leaf No. 131
RIDER EDIT-4 EXCESS DEFERRED INCOME TAX RIDER #4
APPLICABILITY
All service supplied under the Company's rate schedules is subject to a
decrement per kilowatt-hour as set forth below.
MONTHLY RATE
Rate Class Applicable Schedules Billing Rate (¢/kWh)

Residential RS, RE, ES, RT, RSTC, RETC (0.5081)
General Service SGS, BC, LGS, TS, OPT-V, HP, PG, S, SGSTC, HLF (0.3033)
Industrial Service I, OPT-V, HP, PG, HLF (0.2371)
Lighting OL, PL, NL (1.3455)
"""

# Docling-flattened STS (storm securitization, positive increments, 2025-07-01)
STS_FLATTENED = """\
Duke Energy Carolinas, LLC
RIDER STS STORM SECURITIZATION
APPLICABILITY
All service supplied under the Company's rate schedules is subject to
approved storm cost recovery adjustments, an increment per kilowatt hour.
Rate Class Applicable Schedules Billing Rate (¢/kWh)

Residential ES, RE, RETC, RS, RSTC, RT 0.0463
General Service BC, HP, LGS, HLF, OPT-V, PG, S, SGS, SGSTC, TS 0.0109
Industrial HP, I, HLF, OPT-V, PG, SGSTC 0.0066
Lighting NL, OL, PL 0.1168
"""

# Pdfplumber-shaped layout: class label / schedules / rate on separate lines
EDIT4_NEWLINE_LAYOUT = """\
RIDER EDIT-4 EXCESS DEFERRED INCOME TAX RIDER
Rate Class
Applicable Schedules
Billing Rate (¢/kWh)
Residential
RS, RE, ES, RT, RSTC, RETC
(0.5081)
General Service
SGS, BC, LGS, TS
(0.3033)
Industrial Service
I, OPT-V, HP
(0.2371)
Lighting
OL, PL, NL
(1.3455)
"""


@pytest.fixture
def profile():
    return CarolinasMultiClassRateTableProfile()


class TestEDIT4:
    def test_flattened_layout_extracts_four_decrements(self, profile):
        doc = {"family_key": "nc-carolinas-rider-EDIT4"}
        assert profile.supports(doc, EDIT4_FLATTENED)
        charges = profile.extract(doc, EDIT4_FLATTENED)
        assert len(charges) == 4
        rates = {c.charge_label: c.rate_value for c in charges}
        # Parenthesised values are decrements — converted to negative $/kWh
        assert rates["Rate Adjustment - Residential"] == pytest.approx(-0.005081)
        assert rates["Rate Adjustment - General Service"] == pytest.approx(-0.003033)
        assert rates["Rate Adjustment - Industrial Service"] == pytest.approx(-0.002371)
        assert rates["Rate Adjustment - Lighting"] == pytest.approx(-0.013455)
        for c in charges:
            assert c.rate_unit == "$/kWh"
            assert c.charge_type == "adjustment"

    def test_newline_layout_still_works(self, profile):
        doc = {"family_key": "nc-carolinas-rider-EDIT4"}
        assert profile.supports(doc, EDIT4_NEWLINE_LAYOUT)
        charges = profile.extract(doc, EDIT4_NEWLINE_LAYOUT)
        assert len(charges) == 4
        rates = {c.charge_label: c.rate_value for c in charges}
        assert rates["Rate Adjustment - Residential"] == pytest.approx(-0.005081)


class TestSTS:
    def test_unparenthesised_increments_extract(self, profile):
        doc = {"family_key": "nc-carolinas-rider-STS"}
        assert profile.supports(doc, STS_FLATTENED)
        charges = profile.extract(doc, STS_FLATTENED)
        assert len(charges) == 4
        rates = {c.charge_label: c.rate_value for c in charges}
        # Unparenthesised values are increments — kept positive
        assert rates["Rate Adjustment - Residential"] == pytest.approx(0.000463)
        assert rates["Rate Adjustment - General Service"] == pytest.approx(0.000109)
        assert rates["Rate Adjustment - Industrial Service"] == pytest.approx(0.000066)
        assert rates["Rate Adjustment - Lighting"] == pytest.approx(0.001168)


# Docling-flattened EDIT-3 (same table structure as EDIT-4, parenthesised
# decrements — but the family_key kept the "RIDER" prefix, so it lands as
# `nc-carolinas-rider-rideredit3`, not `nc-carolinas-rider-edit3`)
EDIT3_FLATTENED = """\
Duke Energy Carolinas, LLC
RIDER EDIT-3 (NC) EXCESS DEFERRED INCOME TAX RIDER #3
APPLICABILITY (North Carolina Only)
All service supplied under the Company's rate schedules is subject to a
decrement per kilowatt-hour as set forth below.
Rate Class Applicable Schedules Billing Rate (¢/kWh)

Residential RS, RE, ES, RT, RSTC, RETC (0.1894)
General Service SGS, BC, LGS, TS, OPT-V, OPT-E, HP, PG, S, SGSTC (0.1132)
Industrial Service I, OPT-V, OPT-E, HP, PG (0.0886)
Lighting OL, PL, NL (0.4875)
"""


class TestEDIT3:
    def test_rideredit3_supported(self, profile):
        doc = {"family_key": "nc-carolinas-rider-RIDEREDIT3"}
        assert profile.supports(doc, EDIT3_FLATTENED)
        charges = profile.extract(doc, EDIT3_FLATTENED)
        assert len(charges) == 4
        rates = {c.charge_label: c.rate_value for c in charges}
        assert rates["Rate Adjustment - Residential"] == pytest.approx(-0.001894)
        assert rates["Rate Adjustment - General Service"] == pytest.approx(-0.001132)
        assert rates["Rate Adjustment - Industrial Service"] == pytest.approx(-0.000886)
        assert rates["Rate Adjustment - Lighting"] == pytest.approx(-0.004875)


class TestSupportGuards:
    def test_wrong_family_key_rejected(self, profile):
        doc = {"family_key": "nc-progress-leaf-604"}
        assert not profile.supports(doc, EDIT4_FLATTENED)

    def test_cover_letter_text_rejected(self, profile):
        """Cover letters and decoupling-status filings lack the rate-class
        table headers, so the profile must not opt to handle them even
        though they may live in an EDIT-4-tagged family_key."""
        cover_letter = """\
Jack E. Jirak
Deputy General Counsel
November 14, 2025
RE: Duke Energy Carolinas, LLC's Decoupling Status Report
Docket No. E-7, Sub 1276
Residential customer adjustments are summarized below.
"""
        doc = {"family_key": "nc-carolinas-rider-EDIT4"}
        # No "rate class" table header → profile declines
        assert not profile.supports(doc, cover_letter)
