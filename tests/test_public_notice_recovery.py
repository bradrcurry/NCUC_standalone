from duke_rates.historical.public_notices import _extract_notice_documents
from duke_rates.historical.wayback import normalize_archived_target


def test_normalize_archived_target_extracts_original_url() -> None:
    archived = (
        "https://web.archive.org/web/20240519170239/"
        "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf"
    )

    assert (
        normalize_archived_target(archived)
        == "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf"
    )


def test_extract_notice_documents_normalizes_archived_pdf_links() -> None:
    html = """
    <html><body>
      <a href="https://web.archive.org/web/20240519170239/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-608-rider-rdm-ry1.pdf">
        Residential Decoupling Mechanism Rider RDM
      </a>
      <a href="https://web.archive.org/web/20241118190307/https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=older">
        Residential Service Schedule RES
      </a>
    </body></html>
    """

    documents = _extract_notice_documents(html, "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC")

    assert len(documents) == 2
    assert documents[0]["kind"] == "pdf"
    assert any(document["category"] == "rider" for document in documents)
    assert any(document["category"] == "rate" for document in documents)
