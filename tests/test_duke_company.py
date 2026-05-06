from duke_rates.parse.normalization import normalize_company
from duke_rates.utils.duke_company import (
    detect_duke_company,
    is_duke_company_related,
    normalize_duke_company,
)


def test_detect_duke_company_progress_aliases() -> None:
    assert detect_duke_company("Progress Energy Carolinas, Inc.") == {"progress"}
    assert detect_duke_company("Carolina Power & Light Company") == {"progress"}
    assert detect_duke_company("CP&L residential tariff") == {"progress"}


def test_detect_duke_company_carolinas_aliases() -> None:
    assert detect_duke_company("Duke Energy Carolinas, LLC") == {"carolinas"}
    assert detect_duke_company("Duke Power rate schedule riders") == {"carolinas"}


def test_normalize_duke_company_prefers_fallback_when_both_companies_present() -> None:
    text = "Duke Energy Carolinas and Duke Energy Progress Rider GSA-4 NC Tariffs"
    assert normalize_duke_company(text, fallback="progress", state="NC") == "progress"
    assert normalize_duke_company(text, fallback="carolinas", state="NC") == "carolinas"


def test_normalize_company_uses_central_duke_alias_catalog() -> None:
    title = "Residential Service Tariff"
    text = "This filing was submitted by Carolina Power & Light Company for North Carolina."
    assert normalize_company(title, text, fallback=None, state="NC") == "progress"


def test_is_duke_company_related_checks_canonical_company() -> None:
    assert is_duke_company_related("Progress Energy Carolinas rider filing", "progress")
    assert not is_duke_company_related("Duke Power rate filing", "progress")
