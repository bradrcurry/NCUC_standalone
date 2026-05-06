from duke_rates.discovery.link_extractor import extract_links_from_jss_payload


def test_extract_links_from_jss_payload_from_html_fragment() -> None:
    payload = {
        "sitecore": {
            "route": {
                "fields": {"Title": {"value": "Index of Rate Schedules"}},
                "placeholders": {
                    "jss-public-main": [
                        {
                            "fields": {
                                "Body": {
                                    "value": (
                                        '<table><tr><td><a href="/-/media/pdfs/sample-rate.pdf">'
                                        "Sample Rate</a></td></tr></table>"
                                    )
                                }
                            }
                        }
                    ]
                },
            }
        }
    }
    docs, pages = extract_links_from_jss_payload(
        payload,
        "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=FL",
    )
    assert len(docs) == 1
    assert docs[0]["title"] == "Sample Rate"
    assert pages == []
