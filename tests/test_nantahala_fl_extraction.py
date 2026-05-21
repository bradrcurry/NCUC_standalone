"""Tests for Nantahala Power and Light SCHEDULE "FL" extraction.

The 2026-05-16 audit found 2 Carolinas docs (hd_id=886, 889) using a legacy
Nantahala Power and Light tariff format that Docling renders as a Markdown
table:

  | LUMENS | kWh | LUMINAIRE                                              |
  |--------|-----|--------------------------------------------------------|
  | 27,500 | 114 | 250 Watt High Pressure Sodium, ... pole $ 10.12        |
  |        |     | Special floodlighting wood pole (40 foot) ... $ 5.25   |

The existing `_extract_fl` extractor expected the modern DEC three-column
format (`Existing Pole`, `New Pole`, `Underground`). A new branch
`_extract_fl_nantahala` handles the Nantahala table format and bounds
extraction to the FL section to avoid cross-attribution from neighboring
YL/SL/OL sections in the same compliance book.
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasLightingScheduleProfile,
)


# Realistic excerpt from hd_id=889 (Nantahala FL, eff. 2000-09-02).
NANTAHALA_FL_TEXT = """\
NANTAHALA POWER AND LIGHT
SCHEDULE "FL"
FLOODLIGHTING SERVICE
APPLICABILITY
This Schedule is applicable to unmetered service supplied for the floodlighting of areas.

MONTHLY RATE PER UNIT
| LUMENS | kWh | LUMINAIRE |
|---|---|---|
| 27,500 | 114 | 250 Watt High Pressure Sodium, attached to existing pole $ 10.12 |
| 34,000 | 180 | 400 Watt Metal Halide, attached to existing pole $ 14.38 |
| 110,000 | 435 | 1000 Watt Metal Halide, full night attached to existing pole $34.10 |
| 110,000 | 217 | 1000 Watt Metal Halide. half night attached to existing pole $23.18 |
|  |  | Special floodlighting wood pole (40 foot) used only for floodlighting and one span of secondary- served overhead $ 5.25 |
|  |  | Special floodlighting wood pole (40 foot) used only for floodlighting and one span of secondary - served underground $ 6.90 |

ADJUSTMENT
The customer's monthly bill shall be adjusted in accordance with Schedule "CP".
"""


# A YL section in the SAME compliance book, before the FL header — must NOT
# leak through as Floodlighting.
NANTAHALA_FL_WITH_YL_NEIGHBOR = """\
SCHEDULE "YL"
YARD LIGHTING SERVICE

MONTHLY RATE PER UNIT
| LUMENS | kWh | LUMINAIRE |
|---|---|---|
| 7,900 | 83 | 175 Watt mercury vapor yard light, attached to existing Company secondary pole $ 7.29 |
| 27,500 | 114 | 250 Watt high pressure sodium yard light, attached to existing Company secondary pole $ 12.85 |

ADJUSTMENT
The customer's monthly bill shall be adjusted in accordance with Schedule "CP".

SCHEDULE "FL"
FLOODLIGHTING SERVICE

MONTHLY RATE PER UNIT
| LUMENS | kWh | LUMINAIRE |
|---|---|---|
| 27,500 | 114 | 250 Watt High Pressure Sodium, attached to existing pole $ 10.12 |
|  |  | Special floodlighting wood pole (40 foot) used only for floodlighting and one span of secondary - served overhead $ 5.25 |
"""


@pytest.fixture
def profile():
    return CarolinasLightingScheduleProfile()


def test_supports_nantahala_fl_doc(profile):
    assert profile.supports({"family_key": "nc-carolinas-schedule-fl"}, NANTAHALA_FL_TEXT) is True


def test_extracts_six_rates_from_nantahala_fl(profile):
    charges = profile.extract({"family_key": "nc-carolinas-schedule-fl"}, NANTAHALA_FL_TEXT)
    values = sorted(ch.rate_value for ch in charges)
    assert values == [5.25, 6.90, 10.12, 14.38, 23.18, 34.10]
    for ch in charges:
        assert ch.rate_unit == "$/month"
        assert ch.charge_type == "fixed"
        assert "Floodlighting" in (ch.charge_label or "")


def test_extract_does_not_leak_yl_rates_into_fl(profile):
    """When a YL section appears in the same doc, only FL rows should extract.

    The FL section is identified by "Special floodlighting wood pole" markers
    that don't appear in YL/OL/SL sections.
    """
    charges = profile.extract(
        {"family_key": "nc-carolinas-schedule-fl"},
        NANTAHALA_FL_WITH_YL_NEIGHBOR,
    )
    values = sorted(ch.rate_value for ch in charges)
    # Should include the FL rates ($10.12, $5.25) but NOT the YL rates ($7.29, $12.85)
    assert 7.29 not in values
    assert 12.85 not in values
    assert 5.25 in values
    assert 10.12 in values


def test_extract_returns_empty_when_no_table_rows(profile):
    charges = profile.extract(
        {"family_key": "nc-carolinas-schedule-fl"},
        "SCHEDULE FL FLOODLIGHTING SERVICE\nNo rate table here.",
    )
    assert charges == []


def test_extract_handles_post_flatten_text(profile):
    """Production path runs `normalize_docling_markdown` BEFORE profiles see
    the text, which flattens `| col | col | col |` rows to whitespace-
    delimited form (e.g. `27,500  114  250 Watt...$ 10.12`). The regex must
    match both raw-markdown AND post-flatten forms — otherwise tests pass
    while production silently extracts 0 charges (the bug we hit on
    hd_id=886/889 in the 2026-05-16 production run).
    """
    from duke_rates.historical.ncuc.pipeline.ocr_normalization import (
        normalize_docling_markdown,
    )
    flattened = normalize_docling_markdown(NANTAHALA_FL_TEXT)
    # Sanity: the flattened form must not contain `|` table pipes anymore
    assert "| 27,500" not in flattened
    assert "27,500" in flattened
    charges = profile.extract({"family_key": "nc-carolinas-schedule-fl"}, flattened)
    assert len(charges) == 6
    values = sorted(ch.rate_value for ch in charges)
    assert values == [5.25, 6.90, 10.12, 14.38, 23.18, 34.10]
