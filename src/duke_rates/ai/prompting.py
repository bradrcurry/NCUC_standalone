from __future__ import annotations


def build_tariff_extraction_prompt(text: str) -> str:
    return (
        "Extract tariff metadata from the following Duke Energy tariff text. "
        "Return a concise JSON object with schedule code, effective date, fixed charges, "
        "energy charges, demand charges, riders, and uncertainties.\n\n"
        f"{text[:12000]}"
    )
