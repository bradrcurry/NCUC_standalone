from duke_rates.external.openei import OpenEIClient, _extract_label_from_url


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_openei_lookup_filters_and_normalizes(monkeypatch) -> None:
    payload = {
        "items": [
            {
                "label": "5f-res-2024",
                "name": "Residential Service RES",
                "utility": "Duke Energy Progress",
                "uri": "https://openei.org/apps/USURDB/rate/view/abc",
                "sector": "Residential",
                "source": "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf",
                "startdate": "1727740800",
                "enddate": "1759190400",
                "approved": "1",
                "supercedes": "older-res",
            },
            {
                "label": "5f-gs-2024",
                "name": "General Service SGS",
                "utility": "Duke Energy Progress",
                "uri": "https://openei.org/apps/USURDB/rate/view/def",
                "sector": "Commercial",
                "source": "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-sc/leaf-no-510-schedule-sgs.pdf",
                "startdate": "1727740800",
                "approved": "0",
            },
        ]
    }

    monkeypatch.setattr(
        "duke_rates.external.openei.retry_call",
        lambda fn, **kwargs: fn(),
    )
    client = OpenEIClient(api_key="test-key")
    monkeypatch.setattr(client.client, "get", lambda *args, **kwargs: _FakeResponse(payload))

    try:
        rows = client.lookup_rates(
            utility="Duke Energy Progress",
            state="NC",
            search_text="RES",
            limit=10,
        )
    finally:
        client.close()

    assert len(rows) == 1
    assert rows[0].label == "5f-res-2024"
    assert rows[0].start_date == "2024-10-01"
    assert rows[0].end_date == "2025-09-30"
    assert rows[0].approved is True


def test_extract_label_from_openei_url() -> None:
    url = "https://openei.org/USURDB/rate/view/678abac33d12e18b730b0663"

    assert _extract_label_from_url(url) == "678abac33d12e18b730b0663"


def test_openei_lookup_by_url_uses_extracted_label(monkeypatch) -> None:
    payload = {
        "items": [
            {
                "label": "678abac33d12e18b730b0663",
                "name": "Residential Service",
                "utility": "Duke Energy Florida, LLC",
                "uri": "https://apps.openei.org/USURDB/rate/view/678abac33d12e18b730b0663",
                "source": "https://www.duke-energy.com/rate.pdf",
            }
        ]
    }

    monkeypatch.setattr(
        "duke_rates.external.openei.retry_call",
        lambda fn, **kwargs: fn(),
    )
    client = OpenEIClient(api_key="test-key")
    monkeypatch.setattr(client.client, "get", lambda *args, **kwargs: _FakeResponse(payload))

    try:
        rows = client.lookup_rate_by_url(
            "https://openei.org/USURDB/rate/view/678abac33d12e18b730b0663"
        )
    finally:
        client.close()

    assert len(rows) == 1
    assert rows[0].label == "678abac33d12e18b730b0663"
