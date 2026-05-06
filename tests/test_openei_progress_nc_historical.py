from duke_rates.external.openei import OpenEIRateReference
from duke_rates.historical.openei_progress_nc import (
    _candidate_target_keys,
    _extract_source_urls,
    _looks_like_progress_nc_reference,
    _rank_snapshots,
)
from duke_rates.historical.wayback import WaybackSnapshot


def test_extract_source_urls_handles_multiline_field() -> None:
    source = (
        "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf\r\n"
        "https://www.duke-energy.com/pdfs/RR1-NC-Rider-BA-dep.pdf"
    )

    assert _extract_source_urls(source) == [
        "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
        "https://www.duke-energy.com/pdfs/RR1-NC-Rider-BA-dep.pdf",
    ]


def test_looks_like_progress_nc_reference_accepts_progress_nc_pdf() -> None:
    reference = OpenEIRateReference(
        label="res-36",
        name="Residential Service - Schedule RES-36 - Single Phase",
        utility="Progress Energy Carolinas Inc",
        source_parent_uri="https://www.duke-energy.com/rates/progress-north-carolina.asp",
    )

    assert _looks_like_progress_nc_reference(
        reference,
        "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
    )


def test_rank_snapshots_prefers_capture_on_or_after_reference_start() -> None:
    snapshots = [
        WaybackSnapshot(
            timestamp="20140901000000",
            original_url="https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
            status_code="200",
            mimetype="application/pdf",
        ),
        WaybackSnapshot(
            timestamp="20141002000000",
            original_url="https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
            status_code="200",
            mimetype="application/pdf",
        ),
    ]

    ranked = _rank_snapshots(snapshots, start_date="2014-10-01", end_date="2015-09-30")

    assert ranked[0].timestamp == "20141002000000"


def test_candidate_target_keys_extracts_bill_relevant_codes() -> None:
    reference = OpenEIRateReference(
        label="5409d8a85257a3b9738e3cb7",
        name="RR1 NC Rider BA dep",
        utility="Progress Energy Carolinas Inc",
    )

    keys = _candidate_target_keys(
        reference,
        "https://www.duke-energy.com/pdfs/RR1-NC-Rider-BA-dep.pdf",
        "RR1 NC Rider BA dep",
    )

    assert "BA" in keys


def test_candidate_target_keys_maps_old_all_energy_tou_to_current_family() -> None:
    reference = OpenEIRateReference(
        label="540a040f5257a326748e3cb9",
        name="Residential All Energy Time of Use R-TOUE-28- Single Phase",
        utility="Progress Energy Carolinas Inc",
    )

    keys = _candidate_target_keys(
        reference,
        "https://www.duke-energy.com/pdfs/R3-NC-Schedule-R-TOUE-dep.pdf",
        "Residential All Energy Time of Use R-TOUE-28- Single Phase",
    )

    assert "R-TOU" in keys
