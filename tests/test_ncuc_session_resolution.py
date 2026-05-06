from __future__ import annotations


class _FakeLocator:
    def __init__(self, links):
        self._links = links

    def evaluate_all(self, _script: str):
        return list(self._links)


class _FakePage:
    def __init__(self, links):
        self._links = links

    def goto(self, *_args, **_kwargs):
        return None

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def fill(self, *_args, **_kwargs):
        return None

    def click(self, *_args, **_kwargs):
        return None

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def locator(self, _selector: str):
        return _FakeLocator(self._links)


def test_resolve_docket_ids_returns_normalized_and_partial_matches(monkeypatch):
    from duke_rates.historical.ncuc import session as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    page = _FakePage(
        [
            {
                "text": "E-2 Sub 1023",
                "href": "https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
            {
                "text": "E-2, Sub 1023 Duke Energy Progress",
                "href": "https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            },
            {
                "text": "E-2 Sub 1099",
                "href": "https://starw1.ncuc.gov/NCUC/PSC/DocketDetails.aspx?DocketId=cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        ]
    )

    matches = mod.resolve_docket_ids(page, "E-2, Sub 1023")

    assert len(matches) == 2
    assert matches[0]["match_type"] in {"exact", "normalized_exact"}
    assert matches[0]["docket_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert matches[1]["match_type"] in {"partial", "same_base_and_sub"}
    assert matches[1]["docket_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
