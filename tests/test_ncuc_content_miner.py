from duke_rates.historical.ncuc.content_miner import (
    _contains_tariff_text,
    _derive_title_from_text,
    _extract_effective_date,
    _extract_known_rider_codes,
    _extract_priority_codes,
    _merge_unique,
    _sanitize_rider_codes,
    _sanitize_schedule_codes,
    _should_replace_title,
)


def test_ncuc_content_miner_extracts_effective_date() -> None:
    text = """
    Duke Energy Progress, LLC
    Schedule 605
    Effective for service rendered on and after January 1, 2020
    """
    assert _extract_effective_date(text) == "January 1, 2020"


def test_ncuc_content_miner_derives_title() -> None:
    text = """

    Duke Energy Progress, LLC
    Rider No. 640
    Clean Power Rate Enhancement
    """
    assert _derive_title_from_text(text) == "Rider No. 640"


def test_ncuc_content_miner_prefers_tariff_block_title() -> None:
    text = """
    LAW OFFICE OF
    ROBERT W. KAYLOR, P.A.
    Duke Energy Progress, LLC R-1
    (North Carolina Only)
    RESIDENTIAL SERVICE
    SCHEDULE RES-42A
    """
    assert _derive_title_from_text(text) == "RESIDENTIAL SERVICE SCHEDULE RES-42A"


def test_ncuc_content_miner_derives_proposed_rider_title() -> None:
    text = """
    Supplemental Testimony of Bryan L. Sykes
    Summary of CPRE Proposed Rider
    Proposed Rider CPRE (NC)
    """
    assert _derive_title_from_text(text) == "Summary of CPRE Proposed Rider"


def test_ncuc_content_miner_detects_tariff_text() -> None:
    text = "NC Revised Leaf No. 640 Effective for service rendered on and after January 1, 2020"
    assert _contains_tariff_text(text) is True


def test_ncuc_content_miner_extracts_priority_codes() -> None:
    text = "Rider 640 and Schedule 605 are included, but 999 is not relevant."
    assert _extract_priority_codes(text) == ["605", "640"] or _extract_priority_codes(text) == [
        "640",
        "605",
    ]


def test_ncuc_content_miner_extracts_known_rider_codes_only() -> None:
    text = "Rider CPRE applies, but rider SHOULD does not and rider OF does not."
    assert _extract_known_rider_codes(text) == ["CPRE"]


def test_ncuc_content_miner_merges_unique_values() -> None:
    assert _merge_unique(["605", "640"], ["640", "672"], ["605"]) == ["605", "640", "672"]


def test_ncuc_content_miner_sanitizes_existing_codes() -> None:
    assert _sanitize_schedule_codes(["605", "OF", "18"]) == ["605"]
    assert _sanitize_rider_codes(["CPRE", "SHOULD"]) == ["CPRE"]


def test_ncuc_content_miner_replaces_generic_cover_title() -> None:
    assert _should_replace_title("LAW OFFICE OF", "RESIDENTIAL SERVICE SCHEDULE RES-42A") is True
    assert _should_replace_title("RESIDENTIAL SERVICE SCHEDULE RES-42A", "RIDER NO. 640") is False
