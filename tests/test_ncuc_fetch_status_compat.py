from duke_rates.models.ncuc import NcucFetchStatus


def test_ncuc_fetch_status_accepts_legacy_downloaded_value() -> None:
    assert NcucFetchStatus("downloaded") == NcucFetchStatus.DOWNLOADED
