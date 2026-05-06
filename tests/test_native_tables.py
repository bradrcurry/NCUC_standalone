from __future__ import annotations

from pathlib import Path

from duke_rates.document_intelligence.models import NormalizationBackend
from duke_rates.document_intelligence.native_tables import (
    extract_native_tables_for_page,
)
from duke_rates.document_intelligence.normalization import NativePdfNormalizer


class _FakePdfplumberPage:
    def __init__(self, *, text: str = "", tables: list[list[list[str | None]]] | None = None) -> None:
        self.width = 612
        self.height = 792
        self._text = text
        self._tables = tables or []

    def extract_text(self) -> str:
        return self._text

    def extract_tables(self):
        return self._tables


def test_extract_native_tables_for_page_prefers_pdfplumber() -> None:
    page = _FakePdfplumberPage(
        tables=[
            [
                ["Label", "Value"],
                ["Customer Charge", "$14.00"],
            ]
        ]
    )

    result = extract_native_tables_for_page(
        source_pdf="sample.pdf",
        pdfplumber_page=page,
        page_number=1,
    )

    assert result.backend == "pdfplumber"
    assert len(result.tables) == 1
    assert result.tables[0].headers == ["Label", "Value"]
    assert result.tables[0].rows == [["Customer Charge", "$14.00"]]
    assert result.tables[0].metadata["table_backend"] == "pdfplumber"


def test_extract_native_tables_for_page_falls_back_to_camelot(monkeypatch) -> None:
    page = _FakePdfplumberPage(tables=[])

    class _FakeDf:
        empty = False

        def fillna(self, _value: str):
            return self

        @property
        def values(self):
            class _Values:
                @staticmethod
                def tolist():
                    return [
                        ["Label", "Value"],
                        ["Energy Charge", "0.1234"],
                    ]

            return _Values()

    class _FakeCamelotTable:
        df = _FakeDf()

    class _FakeCamelotModule:
        @staticmethod
        def read_pdf(_path: str, pages: str):
            assert pages == "2"
            return [_FakeCamelotTable()]

    import sys

    monkeypatch.setitem(sys.modules, "camelot", _FakeCamelotModule)

    result = extract_native_tables_for_page(
        source_pdf="sample.pdf",
        pdfplumber_page=page,
        page_number=2,
    )

    assert result.backend == "camelot"
    assert len(result.tables) == 1
    assert result.tables[0].metadata["table_backend"] == "camelot"
    assert result.tables[0].rows == [["Energy Charge", "0.1234"]]


def test_native_pdf_normalizer_captures_tables(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "native.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    class _FakePdf:
        def __init__(self) -> None:
            self.pages = [
                _FakePdfplumberPage(
                    text="Label Value\nCustomer Charge $14.00",
                    tables=[
                        [
                            ["Label", "Value"],
                            ["Customer Charge", "$14.00"],
                        ]
                    ],
                )
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class _FakePdfPlumberModule:
        @staticmethod
        def open(_path: Path):
            return _FakePdf()

    from duke_rates.document_intelligence import normalization as normalization_module

    monkeypatch.setattr(normalization_module, "pdfplumber", _FakePdfPlumberModule)

    normalizer = NativePdfNormalizer()
    pages, combined, _markdown, metrics, warnings = normalizer.normalize(
        {
            "local_path": str(pdf_path),
            "start_page": 1,
            "end_page": 1,
        },
        raw_text="fallback",
        page_artifacts=None,
    )

    assert not warnings
    assert combined.startswith("Label Value")
    assert metrics.backend == NormalizationBackend.NATIVE_PDF
    assert metrics.table_page_count == 1
    assert len(pages) == 1
    assert pages[0].tables
    assert pages[0].metadata["table_backend"] == "pdfplumber"
