from __future__ import annotations

from pathlib import Path

import fitz

from duke_rates.benchmark import redline_analysis_bench as bench


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_parse_json_object_handles_wrapped_json() -> None:
    payload = bench._parse_json_object('Result:\n{"redline_present": true, "page_role": "redline"}\nThanks')
    assert payload == {"redline_present": True, "page_role": "redline"}


def test_parse_json_object_handles_loose_fenced_json() -> None:
    payload = bench._parse_json_object(
        """```json
        {
            redline_present: true,
            page_role: 'clean',
            notes: 'sample'
        }
        ```"""
    )
    assert payload == {
        "redline_present": True,
        "page_role": "clean",
        "notes": "sample",
    }


def test_run_redline_analysis_uses_native_signals_and_glm_payload(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "redline.pdf"
    _make_pdf(pdf_path, "NC Twenty-FirstSecond Revised Leaf No. 64")

    monkeypatch.setattr(
        bench,
        "_analyze_page_image_with_glm",
        lambda *args, **kwargs: {
            "redline_present": True,
            "page_role": "redline",
            "visual_evidence": ["duplicated revision labels", "strike-through styling"],
            "before_after_examples": [{"before": "Twenty-First", "after": "Twenty-Second"}],
            "notes": "Appears to be a marked-up revision page.",
        },
    )

    result = bench.run_redline_analysis(
        str(pdf_path),
        page_number=1,
        label="sample",
    )

    assert result["redline_present"] is True
    assert result["page_role"] == "redline"
    assert isinstance(result["native_redline_signals"], list)
    assert result["before_after_examples"][0]["after"] == "Twenty-Second"


def test_write_redline_analysis_markdown_creates_table(tmp_path: Path) -> None:
    output_path = tmp_path / "redline.md"
    bench.write_redline_analysis_markdown(
        [
            {
                "label": "case-a",
                "pdf_path": "sample.pdf",
                "page_number": 4,
                "native_text_chars": 100,
                "native_redline_signals": ["run_together_leaf"],
                "redline_present": True,
                "page_role": "redline",
                "visual_evidence": ["duplicated revision labels"],
                "before_after_examples": [{"before": "Fourth", "after": "Fifth"}],
                "notes": "sample note",
            }
        ],
        str(output_path),
    )

    content = output_path.read_text(encoding="utf-8")
    assert "case-a" in content
    assert "Fourth" in content
    assert "Fifth" in content
