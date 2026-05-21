"""Tests for CarolinasCpreRiderProfile (DEC RIDER CPRE multi-class adjustment).

The 2026-05-16 audit found `nc-carolinas-rider-ridercpre` was landing on
`unknown`/empty despite having a full 3-class × 5-row CPRE rate matrix
(Prospective Component / Experience Modification Factor / Net Rider Factor /
Regulatory Fee Multiplier / CPRE Factor). The new profile extracts the 4
¢/kWh components per class (skipping the unitless Regulatory Fee Multiplier).
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
    normalize_docling_markdown,
)
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasCpreRiderProfile,
    HistoricalRateParserRegistry,
)


# Realistic excerpt from hd_id=7211 (DEC CPRE Rider, eff 2024-09-01).
CPRE_TEXT = """\
Duke Energy Carolinas, LLC (North Carolina Only)
NC Fourth Revised Leaf No. 127

RIDER CPRE
COMPETITIVE PROCUREMENT OF RENEWABLE ENERGY RIDER

APPLICABILITY
Service supplied under the Company's rate schedules is subject to approved
adjustments to recover costs associated with implementation of the Company's
Competitive Procurement of Renewable Energy (CPRE) Program.

CPRE PROSPECTIVE COMPONENT AND EXPERIENCE MODIFICATION FACTOR

RESIDENTIAL SERVICE
Prospective Component of CPRE 0.0435 ¢/kWh
Experience Modification Factor + (0.0372) ¢/kWh
Net CPRE Rider Factor 0.0063 ¢/kWh
Regulatory Fee Multiplier × 1.001703
CPRE Factor 0.0063 ¢/kWh

GENERAL SERVICE AND LIGHTING
Prospective Component of CPRE 0.0410 ¢/kWh
Experience Modification Factor + (0.0376) ¢/kWh
Net CPRE Rider Factor 0.0034 ¢/kWh
Regulatory Fee Multiplier × 1.001703
CPRE Factor 0.0034 ¢/kWh

INDUSTRIAL SERVICE
Prospective Component of CPRE 0.0403 ¢/kWh
Experience Modification Factor + (0.0349) ¢/kWh
Net CPRE Rider Factor 0.0054 ¢/kWh
Regulatory Fee Multiplier × 1.001703
CPRE Factor 0.0054 ¢/kWh
"""


@pytest.fixture
def profile():
    return CarolinasCpreRiderProfile()


def test_supports_ridercpre_family(profile):
    assert profile.supports({"family_key": "nc-carolinas-rider-ridercpre"}, CPRE_TEXT) is True


def test_supports_alternate_cpre_family_key(profile):
    """Some docs use `nc-carolinas-rider-cpre` instead of the longer form."""
    assert profile.supports({"family_key": "nc-carolinas-rider-cpre"}, CPRE_TEXT) is True


def test_rejects_unrelated_family(profile):
    assert profile.supports({"family_key": "nc-progress-leaf-605"}, CPRE_TEXT) is False


def test_extracts_12_charges_three_classes_four_components(profile):
    charges = profile.extract({"family_key": "nc-carolinas-rider-ridercpre"}, CPRE_TEXT)
    assert len(charges) == 12, f"got {len(charges)}"
    classes = {ch.charge_label.split(" - ")[-1] for ch in charges if ch.charge_label}
    assert classes == {"Residential", "General Service and Lighting", "Industrial"}
    # All charges in $/kWh
    for ch in charges:
        assert ch.rate_unit == "$/kWh"
        assert ch.charge_type == "adjustment"


def test_parenthesized_emf_is_negative(profile):
    charges = profile.extract({"family_key": "nc-carolinas-rider-ridercpre"}, CPRE_TEXT)
    emf_charges = [ch for ch in charges if "Experience Modification" in (ch.charge_label or "")]
    assert len(emf_charges) == 3
    for ch in emf_charges:
        assert ch.rate_value < 0, f"EMF should be negative, got {ch.rate_value}"


def test_cent_to_dollar_conversion(profile):
    """¢/kWh values are stored as $/kWh (divided by 100)."""
    charges = profile.extract({"family_key": "nc-carolinas-rider-ridercpre"}, CPRE_TEXT)
    # Residential Prospective Component = 0.0435 ¢/kWh = $0.000435/kWh
    resid_prospective = next(
        ch for ch in charges
        if "Prospective" in (ch.charge_label or "") and "Residential" in (ch.charge_label or "")
    )
    assert abs(resid_prospective.rate_value - 0.000435) < 1e-9


def test_registry_picks_cpre_for_supported_family():
    registry = HistoricalRateParserRegistry()
    ranked = registry.rank_candidates(
        {"family_key": "nc-carolinas-rider-ridercpre"}, CPRE_TEXT,
    )
    assert ranked[0].name == "carolinas_cpre_rider"
    assert ranked[0].score >= 0.95


def test_handles_post_flatten_text(profile):
    """Production runs `normalize_docling_markdown` before profiles see text."""
    flattened = normalize_docling_markdown(CPRE_TEXT)
    charges = profile.extract({"family_key": "nc-carolinas-rider-ridercpre"}, flattened)
    assert len(charges) == 12


def test_returns_empty_on_unsupported_doc(profile):
    charges = profile.extract({"family_key": "nc-carolinas-rider-ridercpre"}, "no rates here")
    assert charges == []


def test_handles_ocr_replacement_of_cent_symbol(profile):
    """OCR sometimes outputs `�/kWh` instead of `¢/kWh` (encoding fallback)."""
    text_with_ocr_replacement = CPRE_TEXT.replace("¢", "�")
    charges = profile.extract(
        {"family_key": "nc-carolinas-rider-ridercpre"},
        text_with_ocr_replacement,
    )
    assert len(charges) == 12
