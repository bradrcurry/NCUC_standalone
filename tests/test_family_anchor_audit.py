from duke_rates.historical.family_anchor_audit import (
    detect_current_family_anchor_mismatch,
    extract_leaf_number,
    extract_schedule_code_hint,
)


def test_extract_leaf_number_supports_family_keys_and_identifiers() -> None:
    assert extract_leaf_number("nc-progress-leaf-501") == "501"
    assert extract_leaf_number("leaf-504") == "504"
    assert extract_leaf_number("Leaf No. 572") == "572"


def test_extract_schedule_code_hint_ignores_prose_schedule_mentions() -> None:
    assert extract_schedule_code_hint("This Schedule is available for service") is None
    assert extract_schedule_code_hint("SCHEDULE R-TOUD") == "RTOUD"


def test_detect_current_family_anchor_mismatch_for_schedule_conflict() -> None:
    reasons = detect_current_family_anchor_mismatch(
        family_key="nc-progress-leaf-501",
        family_schedule_code="FUEL",
        document_tariff_identifier="leaf-501",
        document_schedule_code="R_TOUD",
        document_title="Residential Service Time-of-Use Schedule R-TOUD",
        page_headings=["SCHEDULE R-TOUD"],
        page_leaf_nos=["501"],
    )

    assert "document_schedule_code_mismatch" in reasons
    assert "mined_schedule_code_mismatch" in reasons


def test_detect_current_family_anchor_ignores_mined_leaf_noise_when_identifier_matches() -> None:
    reasons = detect_current_family_anchor_mismatch(
        family_key="nc-progress-leaf-669",
        family_schedule_code="RIDER_NMB_RY1",
        document_tariff_identifier="leaf-669",
        document_schedule_code="RIDER_NMB_RY1",
        document_title="Net Metering Bridge Rider NMB",
        page_headings=["RIDER NMB"],
        page_leaf_nos=["605"],
    )

    assert reasons == []
