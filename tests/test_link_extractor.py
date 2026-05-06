from duke_rates.discovery.link_extractor import extract_jss_state, extract_links_from_html


def test_extract_jss_state_and_links() -> None:
    html = """
    <html>
      <body>
        <a href="/docs/tariff.pdf">Tariff PDF</a>
        <script type="application/json" id="__JSS_STATE__">
          {"sitecore": {"route": {"fields": {"Document": {"value": "/-/media/test/rider.pdf"}}}}}
        </script>
      </body>
    </html>
    """
    state = extract_jss_state(html)
    docs, pages = extract_links_from_html(html, "https://www.duke-energy.com/home/billing/rates")
    assert state is not None
    assert len(docs) == 2
    assert pages == []
