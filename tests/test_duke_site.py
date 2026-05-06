from duke_rates.discovery.duke_site import DukeDiscoveryService


def test_page_priority_prefers_index_pages_before_generic_rates() -> None:
    urls = [
        "https://www.duke-energy.com/home/billing/rates?jur=FL",
        "https://www.duke-energy.com/home/billing/rates/public-notices?jur=FL",
        "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=FL",
    ]
    ordered = sorted(urls, key=DukeDiscoveryService._page_priority)
    assert ordered == [
        "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=FL",
        "https://www.duke-energy.com/home/billing/rates/public-notices?jur=FL",
        "https://www.duke-energy.com/home/billing/rates?jur=FL",
    ]
