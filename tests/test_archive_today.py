from pathlib import Path

from duke_rates.historical.archive_today import (
    ARCHIVE_TODAY_BASE,
    ArchiveTodayClient,
    ArchiveTodayProbeResult,
    write_archive_today_markdown_report,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url


def test_archive_today_probe_classifies_rate_limit(monkeypatch) -> None:
    client = ArchiveTodayClient()
    monkeypatch.setattr(
        client.client,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            429,
            "<html>rate limited</html>",
            f"{ARCHIVE_TODAY_BASE}/?url=https%3A%2F%2Fexample.com%2Frate.pdf",
        ),
    )

    try:
        result = client.probe(title="RES", source_url="https://example.com/rate.pdf")
    finally:
        client.close()

    assert result.status == "rate_limited"
    assert result.http_status == 429


def test_write_archive_today_markdown_report(tmp_path: Path) -> None:
    report_path = tmp_path / "archive.md"
    results = [
        ArchiveTodayProbeResult(
            title="RES",
            source_url="https://example.com/rate.pdf",
            search_url=f"{ARCHIVE_TODAY_BASE}/?url=https%3A%2F%2Fexample.com%2Frate.pdf",
            direct_url=f"{ARCHIVE_TODAY_BASE}/https://example.com/rate.pdf",
            status="ok",
            http_status=200,
            final_url=f"{ARCHIVE_TODAY_BASE}/abc123",
            archive_links=[f"{ARCHIVE_TODAY_BASE}/abc123"],
            notes=["found"],
        )
    ]
    write_archive_today_markdown_report(results=results, output_path=report_path)

    text = report_path.read_text(encoding="utf-8")
    assert "Progress NC archive.today Probe" in text
    assert "https://archive.ph/abc123" in text
