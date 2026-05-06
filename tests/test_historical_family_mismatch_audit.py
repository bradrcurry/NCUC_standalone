from duke_rates.historical.family_mismatch_audit import (
    detect_historical_family_mismatch,
    expected_company_from_family_key,
    extract_rider_code_hint,
    extract_schedule_code_hint,
)


def test_expected_company_from_family_key() -> None:
    assert expected_company_from_family_key("nc-progress-leaf-500") == "progress"
    assert expected_company_from_family_key("nc-carolinas-rider-EE") == "carolinas"
    assert expected_company_from_family_key("unknown") is None


def test_extract_schedule_code_hint() -> None:
    assert extract_schedule_code_hint("SCHEDULE RES\nResidential Service") == "RES"
    assert extract_schedule_code_hint("Schedule R-TOU\nResidential Service") == "RTOU"
    assert extract_schedule_code_hint("No schedule here") is None


def test_extract_rider_code_hint() -> None:
    assert extract_rider_code_hint("POWERSHARE\nRIDER PS\n") == "RIDERPS"
    assert extract_rider_code_hint("No rider here") is None


def test_detect_historical_family_mismatch_for_company_and_schedule() -> None:
    text = """
    Duke Energy Carolinas, LLC Electricity No. 4
    SCHEDULE RT (NC)
    RESIDENTIAL SERVICE, TIME OF USE
    """
    reasons = detect_historical_family_mismatch(
        family_key="nc-progress-leaf-500",
        family_schedule_code="RES",
        text=text,
        state="NC",
    )
    assert "company_text_mismatch" in reasons
    assert "schedule_code_mismatch" in reasons


def test_detect_historical_family_mismatch_accepts_matching_progress_res() -> None:
    text = """
    Duke Energy Progress, LLC NC Second Revised Leaf No. 500
    SCHEDULE RES
    RESIDENTIAL SERVICE
    """
    reasons = detect_historical_family_mismatch(
        family_key="nc-progress-leaf-500",
        family_schedule_code="RES",
        text=text,
        state="NC",
    )
    assert reasons == []


def test_detect_historical_family_mismatch_flags_progress_summary_sheet_mapped_to_leaf_601() -> None:
    text = """
    Duke Energy Progress, LLC NC Sixth Revised Leaf No. 600
    SUMMARY OF RIDER ADJUSTMENTS
    The following is a summary of Rider Adjustments that must be added to the bill.
    Annual Billing Adjustments Rider BA
    Effective for service rendered on and after April 1, 2025
    """
    reasons = detect_historical_family_mismatch(
        family_key="nc-progress-leaf-601",
        family_schedule_code="RIDER BA",
        text=text,
        state="NC",
    )
    assert "summary_sheet_family_mismatch" in reasons


def test_detect_historical_family_mismatch_accepts_matching_progress_rider_ps() -> None:
    text = """
    Duke Energy Progress, LLC NC Original Leaf No. 674
    POWERSHARE NONRESIDENTIAL LOAD CURTAILMENT
    RIDER PS
    PROVISIONS FOR CUSTOMERS SERVED UNDER HP OR LGS-RTP
    """
    reasons = detect_historical_family_mismatch(
        family_key="nc-progress-leaf-674",
        family_schedule_code="RIDER_PS",
        text=text,
        state="NC",
    )
    assert reasons == []


def test_detect_historical_family_mismatch_accepts_matching_progress_rider_us_ry1() -> None:
    text = """
    Duke Energy Progress, LLC NC Original Leaf No. 649
    UNMETERED SERVICE
    RIDER US
    """
    reasons = detect_historical_family_mismatch(
        family_key="nc-progress-leaf-649",
        family_schedule_code="RIDER_US_RY1",
        text=text,
        state="NC",
    )
    assert reasons == []
