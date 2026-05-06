from __future__ import annotations

from pathlib import Path


def extract_pdf_text(path: Path) -> str:
    errors: list[str] = []

    try:
        import fitz  # type: ignore

        with fitz.open(path) as doc:
            return "\n".join(page.get_text("text") for page in doc)
    except Exception as exc:  # pragma: no cover - optional dependency behavior
        errors.append(f"pymupdf: {exc}")

    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(path) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # pragma: no cover - optional dependency behavior
        errors.append(f"pdfplumber: {exc}")

    raise RuntimeError("Unable to extract PDF text. Install duke-rates[pdf]. " + " | ".join(errors))
