"""Phase 2B: profiles consuming structured input, validators, candidate provenance."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from duke_rates.document_intelligence.adapters import representation_from_docling
from duke_rates.document_intelligence.charge_validators import (
    run_charge_validators,
    validate_duplicates,
    validate_family_contamination,
    validate_rate_class_consistency,
    validate_revision_changes,
    validate_total_reconciliation,
    validate_units,
)
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.candidate_runs import (
    _evidence_fields,
    _iso_date,
    to_candidate_charge,
)
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    ProgressJaaRiderProfile,
    ProgressRiderAdjustmentMatrixProfile,
    ProgressSingleValueRiderProfile,
    structured_policy,
)
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge
from duke_rates.parse.rider_summary import RiderLineItem, RiderRateClassBlock, RiderSummaryResult

ROOT = Path(__file__).resolve().parents[1]


def _bench_rep(doc_id: str):
    with gzip.open(
        ROOT / f"bench/docling/{doc_id}.docling.json.gz", "rt", encoding="utf-8"
    ) as handle:
        envelope = json.load(handle)
    return representation_from_docling(
        envelope["document"],
        source_pdf=doc_id,
        file_sha256=envelope["file_sha256"],
        backend_version=envelope["backend_version"],
    )


def _doc(family_key: str, mode: str, rep=None, *, local_path: str = "") -> tuple[dict, list]:
    calls: list[int] = []

    def loader():
        calls.append(1)
        return rep

    return {
        "family_key": family_key,
        "local_path": local_path,
        "company": "progress",
        "_structured_mode": mode,
        "_representation_loader": loader,
    }, calls


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_structured_policy_names():
    assert structured_policy({}) == "legacy"
    assert structured_policy({"_structured_mode": "docling"}) == "structured"
    assert structured_policy({"_structured_mode": "native"}) == "structured"
    assert structured_policy({"_structured_mode": "hybrid"}) == "hybrid"


LEAF600_PDF = ROOT / "bench/pdfs/dep_leaf600_2026-04-01.pdf"


def _pdf_text() -> str:
    from duke_rates.document_intelligence.adapters import representation_from_native_pdf

    return representation_from_native_pdf(LEAF600_PDF).raw_text


def test_legacy_mode_never_builds_a_representation():
    doc, calls = _doc("nc-progress-leaf-600", "legacy", local_path=str(LEAF600_PDF))
    legacy_charges = ProgressRiderAdjustmentMatrixProfile().extract(doc, _pdf_text())
    assert calls == [] and legacy_charges
    assert all(c.evidence is None for c in legacy_charges)


def test_hybrid_rider_summary_charges_carry_table_evidence():
    envelope = json.loads(
        (ROOT / "tests/fixtures/docling/dep_leaf600_2026-04-01.docling.json").read_text(
            encoding="utf-8"
        )
    )
    rep = representation_from_docling(
        envelope["doc_json_content"],
        source_pdf="x",
        file_sha256=envelope["file_hash"],
        backend_version="docling_v2",
    )
    doc, calls = _doc("nc-progress-leaf-600", "hybrid", rep, local_path=str(LEAF600_PDF))
    legacy_doc, _ = _doc("nc-progress-leaf-600", "legacy", local_path=str(LEAF600_PDF))
    text = _pdf_text()
    hybrid = ProgressRiderAdjustmentMatrixProfile().extract(doc, text)
    legacy = ProgressRiderAdjustmentMatrixProfile().extract(legacy_doc, text)
    assert calls == [1]
    with_page = [
        c for c in hybrid if c.evidence and (c.evidence.get("page") or c.evidence.get("cells"))
    ]
    assert len(with_page) >= 0.8 * len(hybrid)
    res_total = next(
        c
        for c in hybrid
        if c.charge_label == "Residential Service Schedules Total Rider Adjustments"
    )
    assert res_total.rate_value == pytest.approx(0.02108)
    assert res_total.evidence["cells"][0]["text"] == "2.108"

    # Hybrid never loses a value the text parse found (labels may differ in
    # spacing between parsers, so compare values per charge type and unit).
    def values(charges):
        from collections import Counter

        return Counter((c.charge_type, c.rate_unit, round(c.rate_value, 8)) for c in charges)

    assert len(legacy) > 100
    assert not (values(legacy) - values(hybrid))


def test_jaa_profile_reads_the_class_rate_table():
    rep = _bench_rep("hd1882_jaa_rev3")
    doc, _ = _doc("nc-progress-leaf-602", "hybrid", rep)
    charges = ProgressJaaRiderProfile().extract(doc, "")
    by_label = {(c.charge_label, c.rate_unit): c for c in charges}
    res = by_label[("JAA Rate - Residential", "$/kWh")]
    assert res.rate_value == pytest.approx(0.00464) and res.charge_type == "adjustment"
    lgs = by_label[("JAA Rate - Large General Service", "$/kW")]
    assert lgs.rate_value == pytest.approx(3.03) and lgs.charge_type == "demand"
    assert all(c.evidence and c.evidence["page"] == 1 for c in charges)
    legacy_doc, _ = _doc("nc-progress-leaf-602", "legacy")
    assert ProgressJaaRiderProfile().extract(legacy_doc, "no table here") == []


def test_single_value_hybrid_never_adds_rates_to_a_rate_free_document():
    rep = _bench_rep("hd1882_jaa_rev3")  # has a class-rate table
    doc, _ = _doc("nc-progress-leaf-646", "hybrid", rep)
    assert (
        ProgressSingleValueRiderProfile().extract(doc, "Rider CM terms and conditions only.") == []
    )


def test_bulk_extractor_rejects_unknown_modes(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        BulkExtractor(str(tmp_path / "x.db"), structured_mode="vlm")
    monkeypatch.setenv("DUKE_RATES_STRUCTURED_MODE", "hybrid")
    assert BulkExtractor(str(tmp_path / "x.db")).structured_mode == "hybrid"
    monkeypatch.delenv("DUKE_RATES_STRUCTURED_MODE")
    assert BulkExtractor(str(tmp_path / "x.db")).structured_mode == "legacy"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _c(label, value, unit="$/kWh", ctype="adjustment", cls=None):
    return {
        "charge_type": ctype,
        "charge_label": label,
        "rate_value": value,
        "rate_unit": unit,
        "customer_class": cls,
    }


def test_kwh_and_kw_totals_with_one_label_are_not_duplicates():
    findings = validate_duplicates(
        [
            _c(
                "Large General Service Total Rider Adjustments",
                0.01397,
                "$/kWh",
                "adjustment_total",
            ),
            _c("Large General Service Total Rider Adjustments", 3.03, "$/kW", "adjustment_total"),
        ]
    )
    assert findings == []


def test_conflicting_duplicates_need_review():
    (finding,) = validate_duplicates(
        [_c("Residential - JAA", 0.00464), _c("Residential - JAA", 0.00631)]
    )
    assert finding.level == "review" and finding.code == "conflicting_duplicates"


def test_unit_and_class_validators():
    codes = {
        f.code
        for f in validate_units(
            [_c("Storm - Residential", 0.3, "�/kWh"), _c("Demand Charge", 0.05, "$/kWh", "demand")]
        )
    }
    assert {"unit_has_replacement_character", "type_unit_conflict"} <= codes
    (finding,) = validate_rate_class_consistency(
        [_c("Residential - JAA", 0.1, cls="commercial_large")]
    )
    assert finding.code == "rate_class_inconsistent"


def test_revision_jump_and_family_contamination():
    jump = validate_revision_changes(
        [_c("Lighting - Storm", 0.55, "¢/kWh")], [_c("Lighting - Storm", 0.042, "¢/kWh")]
    )
    assert [f.code for f in jump] == ["revision_value_jump"]
    leaf607_v5723 = [_c("Energy Charge - Summer", 0.1166, "$/kWh", "energy_block")] * 2 + [
        _c("Basic Customer Charge", 14.0, "$/month", "fixed")
    ]
    (finding,) = validate_family_contamination(leaf607_v5723, family_type="rider")
    assert finding.code == "cross_family_contamination"
    assert validate_family_contamination(leaf607_v5723, family_type="rate_schedule") == []


def test_total_reconciliation_is_recomputed_in_decimal():
    block = RiderRateClassBlock(
        rate_class="Residential",
        total_cents_per_kwh=1.0,
        line_items=[RiderLineItem("A", cents_per_kwh=0.3), RiderLineItem("B", cents_per_kwh=0.4)],
    )
    summary = RiderSummaryResult("x", None, None, None, None, None, rate_classes=[block])
    (finding,) = validate_total_reconciliation(summary)
    assert finding.code == "total_mismatch" and finding.level == "review"
    assert run_charge_validators([], summary=summary)


# ---------------------------------------------------------------------------
# Candidate provenance mapping
# ---------------------------------------------------------------------------


def test_candidate_mapping_uses_evidence_without_inventing_any():
    bare = ExtractedCharge(
        "adjustment", "Residential - CAR", 0.00098, "$/kWh", source_snippet="CAR"
    )
    cc = to_candidate_charge(bare, source_pdf_path="x.pdf", source_pdf_sha256="s", profile="p")
    assert (
        cc.page_number is None
        and cc.bbox is None
        and cc.source_quote is None
        and cc.table_ref is None
    )

    structured = ExtractedCharge(
        "adjustment",
        "Residential - CAR",
        0.00098,
        "$/kWh",
        source_snippet="CAR",
        evidence={
            "method": "rider_summary_tables",
            "backend": "docling",
            "backend_version": "docling_v2",
            "page": 1,
            "table_id": "docling:#/tables/0",
            "row": 9,
            "label": {"text": "Customer Assistance Recovery Rider CAR"},
            "values": [
                {
                    "text": "0.098",
                    "col": 1,
                    "printed_unit": "cents /kWh",
                    "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
                }
            ],
            "date": {"text": "1/1/26"},
        },
    )
    cc = to_candidate_charge(
        structured, source_pdf_path="x.pdf", source_pdf_sha256="s", profile="p"
    )
    assert cc.page_number == 1 and cc.bbox == {"x0": 1, "y0": 2, "x1": 3, "y1": 4}
    assert cc.source_quote == "Customer Assistance Recovery Rider CAR 0.098 1/1/26"
    assert cc.printed_value_text == "0.098" and cc.printed_unit == "cents /kWh"
    assert cc.effective_start == "2026-01-01"
    assert cc.table_ref["table_id"] == "docling:#/tables/0" and cc.table_ref["row"] == 9


def test_quote_without_the_value_is_not_evidence():
    fields = _evidence_fields({"page": 2, "text": "Rider CAR", "printed_value_text": "0.098"})
    assert fields["page_number"] == 2 and fields["source_quote"] is None


@pytest.mark.parametrize(
    ("printed", "iso"),
    [("12/1/25", "2025-12-01"), ("4/1/2026", "2026-04-01"), ("soon", None), (None, None)],
)
def test_iso_date(printed, iso):
    assert _iso_date(printed) == iso
