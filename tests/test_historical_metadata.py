from duke_rates.historical.metadata import extract_historical_metadata


def test_extract_historical_metadata_with_superseding_range() -> None:
    text = """
    Schedule RES
    NC First Revised Leaf No. 500
    Superseding NC Original Leaf No. 500
    Effective for service rendered from October 1, 2024 through September 30, 2025
    """

    metadata = extract_historical_metadata(text)

    assert metadata["revision_label"] == "NC First Revised Leaf No. 500"
    assert metadata["supersedes_label"] == "NC Original Leaf No. 500"
    assert metadata["leaf_no"] == "500"
    assert metadata["effective_start"] == "October 1, 2024"
    assert metadata["effective_end"] == "September 30, 2025"


def test_extract_historical_metadata_with_on_after_clause() -> None:
    text = """
    Residential Service
    NC Second Revised Leaf No. 500
    Effective for service rendered on and after October 1, 2025
    """

    metadata = extract_historical_metadata(text)

    assert metadata["revision_label"] == "NC Second Revised Leaf No. 500"
    assert metadata["supersedes_label"] is None
    assert metadata["leaf_no"] == "500"
    assert metadata["effective_start"] == "October 1, 2025"
    assert metadata["effective_end"] is None
