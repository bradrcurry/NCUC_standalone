from datetime import datetime
from pathlib import Path

from duke_rates.billing.calculators import UsageInput
from duke_rates.models.document import StoredDocument
from duke_rates.models.parse_result import DocumentParseResult, ParseStatus
from duke_rates.models.rate_schedule import (
    DemandCharge,
    EnergyCharge,
    FixedCharge,
    RateScheduleData,
)
from duke_rates.selection import (
    canonical_tariff_key,
    estimation_score,
    is_estimatable_schedule,
    supports_usage_input,
)


def _document(*, doc_id: int, title: str) -> StoredDocument:
    now = datetime(2026, 1, 1)
    return StoredDocument(
        id=doc_id,
        title=title,
        source_page_url="https://example.com/source",
        document_url="https://example.com/doc.pdf",
        state="FL",
        company="florida",
        category="rate",
        kind="pdf",
        local_path=Path("data/raw/example.pdf"),
        content_hash=f"hash-{doc_id}",
        retrieved_at=now,
        discovered_at=now,
    )


def _result(
    *,
    tariff_id: str,
    title: str,
    schedule_code: str,
    review_flags: list[str] | None = None,
) -> DocumentParseResult:
    return DocumentParseResult(
        document_id=1,
        status=ParseStatus.PARSED,
        parser_name="test",
        review_flags=review_flags or [],
        schedule=RateScheduleData(
            tariff_id=tariff_id,
            state="FL",
            company="florida",
            schedule_code=schedule_code,
            schedule_title=title,
            fixed_charges=[FixedCharge(label="Customer charge", amount=10.0)],
            energy_charges=[EnergyCharge(label="Energy charge", rate=0.1)],
            raw_summary="Base Rates by Rate Schedule" if review_flags else "Schedule RS-1",
        ),
    )


def test_canonical_tariff_key_prefers_state_company_schedule_code() -> None:
    result = _result(
        tariff_id="fl_florida_rs-1_as-of-february-2026",
        title="As of February 2026",
        schedule_code="RS-1",
    )
    assert canonical_tariff_key(result) == "fl:florida:rs-1"


def test_estimation_score_prefers_leaf_schedule_over_summary_matrix() -> None:
    leaf_doc = _document(doc_id=10, title="Schedule RS-1")
    summary_doc = _document(doc_id=20, title="As of February 2026")
    leaf_result = _result(
        tariff_id="fl_florida_rs-1_schedule-rs-1",
        title="Schedule RS-1",
        schedule_code="RS-1",
    )
    summary_result = _result(
        tariff_id="fl_florida_rs-1_as-of-february-2026",
        title="As of February 2026",
        schedule_code="RS-1",
        review_flags=["Summary/matrix rate document detected"],
    )
    assert estimation_score(leaf_doc, leaf_result) > estimation_score(summary_doc, summary_result)


def test_is_estimatable_schedule_requires_usage_sensitive_charge() -> None:
    fixed_only = DocumentParseResult(
        document_id=2,
        status=ParseStatus.PARSED,
        parser_name="test",
        schedule=RateScheduleData(
            tariff_id="fl_florida_rst-1",
            state="FL",
            company="florida",
            schedule_code="RST-1",
            schedule_title="Residential Service (Optional Time of Use)",
            fixed_charges=[FixedCharge(label="Customer charge", amount=14.27)],
        ),
    )
    energy_based = _result(
        tariff_id="fl_florida_rs-1",
        title="Residential Service",
        schedule_code="RS-1",
    )
    assert not is_estimatable_schedule(fixed_only)
    assert is_estimatable_schedule(energy_based)


def test_supports_usage_input_requires_matching_charge_type() -> None:
    demand_only = DocumentParseResult(
        document_id=3,
        status=ParseStatus.PARSED,
        parser_name="test",
        schedule=RateScheduleData(
            tariff_id="fl_florida_gsd-1",
            state="FL",
            company="florida",
            schedule_code="GSD-1",
            schedule_title="General Service - Demand",
            fixed_charges=[FixedCharge(label="Customer charge", amount=20.0)],
            demand_charges=[DemandCharge(label="Demand", rate=8.0)],
        ),
    )
    energy_based = _result(
        tariff_id="fl_florida_rs-1",
        title="Residential Service",
        schedule_code="RS-1",
    )
    monthly_usage = UsageInput(monthly_kwh=1200)
    assert not supports_usage_input(demand_only, monthly_usage)
    assert supports_usage_input(energy_based, monthly_usage)


def test_supports_usage_input_rejects_tou_schedule_without_interval_data() -> None:
    tou_result = DocumentParseResult(
        document_id=4,
        status=ParseStatus.PARSED,
        parser_name="test",
        schedule=RateScheduleData(
            tariff_id="fl_florida_rst-1",
            state="FL",
            company="florida",
            schedule_code="RST-1",
            schedule_title="Residential Service (Optional Time of Use)",
            fixed_charges=[FixedCharge(label="Customer charge", amount=14.27)],
            energy_charges=[
                EnergyCharge(label="On-Peak", rate=0.11032, period="On-Peak"),
                EnergyCharge(label="Off-Peak", rate=0.08172, period="Off-Peak"),
            ],
        ),
    )
    assert not supports_usage_input(tou_result, UsageInput(monthly_kwh=1200))
    assert supports_usage_input(
        tou_result,
        UsageInput(
            monthly_kwh=1200,
            interval_data=[{"timestamp": "2026-01-01T00:00:00", "kwh": 1.0}],
        ),
    )
