"""Unit tests for schedule_code_extractor."""

from __future__ import annotations

import pytest

from duke_rates.document_intelligence.schedule_code_extractor import (
    extract_codes,
)


class TestBasicPatterns:
    def test_empty_text(self) -> None:
        result = extract_codes("")
        assert result.codes == []

    def test_schedule_prefix(self) -> None:
        result = extract_codes("RESIDENTIAL SERVICE / SCHEDULE RES / AVAILABILITY")
        assert "RES" in result.codes

    def test_rider_prefix(self) -> None:
        result = extract_codes("RIDER EB (NC) / ENERGYWISE FOR BUSINESS")
        assert "EB" in result.codes

    def test_schedule_no_prefix(self) -> None:
        result = extract_codes("Schedule R-TOU-CPP / Time-of-Use Critical Peak")
        assert "R-TOU-CPP" in result.codes

    def test_compound_code_standalone(self) -> None:
        result = extract_codes("Duke Energy Carolinas Schedule LGS-TOU rates")
        assert "LGS-TOU" in result.codes

    def test_multiple_codes_in_one_section(self) -> None:
        text = "SCHEDULE LGS / LGS-TOU / LGS-HLF / These are related schedules"
        result = extract_codes(text)
        for c in ("LGS", "LGS-TOU", "LGS-HLF"):
            assert c in result.codes


class TestBlocklist:
    def test_common_words_blocked(self) -> None:
        text = "SCHEDULE AVAILABILITY / RIDER APPLICABILITY"
        result = extract_codes(text)
        assert "AVAILABILITY" not in result.codes
        assert "APPLICABILITY" not in result.codes

    def test_docket_numbers_blocked(self) -> None:
        text = "Docket No. E-2, Sub 1305 / SCHEDULE RES"
        result = extract_codes(text)
        assert "E-2" not in result.codes
        # But valid schedule still extracted
        assert "RES" in result.codes

    def test_commission_rule_blocked(self) -> None:
        text = "NCUC Rule R8-55 governs this filing / SCHEDULE LGS-TOU"
        result = extract_codes(text)
        assert "R8-55" not in result.codes
        assert "LGS-TOU" in result.codes

    def test_e7_docket_blocked(self) -> None:
        text = "DOCKET NO. E-7, SUB 1234 / SCHEDULE LGS"
        result = extract_codes(text)
        assert "E-7" not in result.codes

    def test_time_of_use_heading_blocked(self) -> None:
        text = "RESIDENTIAL TIME-OF-USE SCHEDULE R-TOUD-19"
        result = extract_codes(text)
        # TIME-OF-USE is heading text, not a code
        assert "TIME-OF-USE" not in result.codes
        # The actual schedule code is extracted
        assert "R-TOUD-19" in result.codes


class TestKnownShortCodes:
    def test_eb_short_code(self) -> None:
        text = "ENERGYWISE FOR BUSINESS / RIDER EB / Availability"
        result = extract_codes(text)
        assert "EB" in result.codes

    def test_unknown_short_code_rejected(self) -> None:
        # XX is not in _KNOWN_SHORT_CODES and not a compound; should reject
        text = "Schedule XX is not a real code"
        result = extract_codes(text)
        # XX rejected because it's only 2 chars and not in known list
        assert "XX" not in result.codes

    def test_short_code_only_in_title_block(self) -> None:
        # PL is a known short code, but it needs to appear in first 600 chars
        text_far = "x" * 800 + " RIDER PL applies"
        result = extract_codes(text_far)
        # PL is past the title block; should not be picked up by known-short path,
        # but the explicit RIDER prefix is in head region (first 1500 chars)
        assert "PL" in result.codes


class TestNormalization:
    def test_normalizes_to_uppercase(self) -> None:
        result = extract_codes("rider ba applies")
        assert "BA" in result.codes
        # No lowercase variants
        assert "ba" not in result.codes

    def test_strips_schedule_prefix(self) -> None:
        # "SCHEDULE RES" → just "RES" after normalize
        result = extract_codes("SCHEDULE RES")
        assert "RES" in result.codes
        assert "SCHEDULE RES" not in result.codes


class TestEdgeCases:
    def test_only_first_1500_chars_scanned(self) -> None:
        # Codes deeper than 1500 chars should not be picked up.
        text = ("x" * 1600) + " SCHEDULE RES"
        result = extract_codes(text)
        # RES is in known shorts, but title block is only 600 chars; head 1500.
        # RES at offset 1601 is past 1500 — should be missed.
        assert "RES" not in result.codes

    def test_dedup_preserves_first_occurrence(self) -> None:
        text = "SCHEDULE LGS / Some text / SCHEDULE LGS again"
        result = extract_codes(text)
        # LGS appears once, not twice
        assert result.codes.count("LGS") == 1

    def test_sources_annotated(self) -> None:
        text = "RIDER EB / Schedule R-TOU-CPP"
        result = extract_codes(text)
        assert result.sources["EB"] == "explicit_prefix"
        # R-TOU-CPP could match explicit_prefix OR compound — depends on order
        assert result.sources["R-TOU-CPP"] in {"explicit_prefix", "compound"}

    def test_long_compound_code(self) -> None:
        text = "SGS-TOUE-79 schedule details"
        result = extract_codes(text)
        assert "SGS-TOUE-79" in result.codes

    def test_excessively_long_code_rejected(self) -> None:
        # > 25 chars
        text = "SCHEDULE THISISAVERYLONGFAKECODEXXXX"
        result = extract_codes(text)
        # Should not extract a 36-char "code"
        assert all(len(c) <= 25 for c in result.codes)
