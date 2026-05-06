from __future__ import annotations

import pytest
from duke_rates.discovery.classifier import classify_document_url, extract_rev_token


def test_nc_progress_leaf():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-500-schedule-res.pdf?rev=abc123"
    result = classify_document_url(url, state="NC", company="progress")
    assert result["tariff_identifier"] == "leaf-500"
    assert result["schedule_code"] == "RES"
    assert result["rev_token"] == "abc123"


def test_nc_progress_rider():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/dep-nc/leaf-no-640-rider-cei.pdf"
    result = classify_document_url(url)
    assert result["tariff_identifier"] == "leaf-640"
    assert result["schedule_code"] == "RIDER_CEI"


def test_nc_carolinas_rider():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/212287/dec-nc-rider-cei.pdf?rev=deadbeef"
    result = classify_document_url(url)
    assert result["tariff_identifier"] == "rider-CEI"
    assert result["schedule_code"] == "CEI"
    assert result["rev_token"] == "deadbeef"


def test_fl_pe_rates():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/rates-fl/pe-rates-rs-001.pdf"
    result = classify_document_url(url, state="FL", company="florida")
    assert result["tariff_identifier"] == "pe-RS-001"
    assert result["schedule_code"] == "RS"


def test_in_tariff_no():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-in/iurc-16/005-de-in-tariff-no-07-rate-rs.pdf"
    result = classify_document_url(url, state="IN", company="indiana")
    assert result["tariff_identifier"] == "tariff-07"
    assert result["schedule_code"] is not None


def test_ky_sheet_no():
    url = "https://www.duke-energy.com/-/media/pdfs/for-your-home/rates/electric-ky/sheet-no-10-ky-e-rate-rs.pdf"
    result = classify_document_url(url, state="KY", company="kentucky")
    assert result["tariff_identifier"] == "sheet-10"


def test_unrecognized_url():
    url = "https://www.duke-energy.com/-/media/pdfs/rates/de--ky-coverpg.pdf"
    result = classify_document_url(url)
    assert result["tariff_identifier"] is None
    assert result["schedule_code"] is None


def test_rev_token_extraction():
    assert extract_rev_token("https://example.com/file.pdf?rev=abc123def456") == "abc123def456"
    assert extract_rev_token("https://example.com/file.pdf") is None
    assert extract_rev_token("https://example.com/file.pdf?foo=bar&rev=deadbeef") == "deadbeef"
