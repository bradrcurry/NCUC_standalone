from duke_rates.historical.wayback import WaybackClient


class _FakeResponse:
    def __init__(self, payload: list[list[str]]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[list[str]]:
        return self._payload


def test_lookup_capture_history_preserves_multiple_timestamps(monkeypatch) -> None:
    payload = [
        ["timestamp", "original", "statuscode", "mimetype"],
        [
            "20200101000000",
            "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
            "200",
            "application/pdf",
        ],
        [
            "20210101000000",
            "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
            "200",
            "application/pdf",
        ],
    ]
    client = WaybackClient()
    monkeypatch.setattr(client.client, "get", lambda *args, **kwargs: _FakeResponse(payload))

    try:
        rows = client.lookup_capture_history(
            "https://www.duke-energy.com/pdfs/R1-NC-Schedule-RES-dep.pdf",
            from_year=2020,
            limit=10,
        )
    finally:
        client.close()

    assert len(rows) == 2
    assert rows[0].timestamp == "20200101000000"
    assert rows[1].timestamp == "20210101000000"
