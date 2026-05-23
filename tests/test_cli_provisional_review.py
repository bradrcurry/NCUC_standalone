from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from duke_rates import cli
from duke_rates.cli_commands import lineage as lineage_module
from duke_rates.db.repository import Repository
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.tariff import TariffChargeRecord, TariffFamilyRecord, TariffVersionRecord


def test_show_provisional_review_candidates_cli_lists_scored_rows(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cli-provisional-review.db"
    repo = Repository(db_path)
    now = datetime(2026, 4, 21, tzinfo=UTC)

    repo.upsert_tariff_family(
        TariffFamilyRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            state="NC",
            company="progress",
            tariff_identifier="doc-TYPEOFSERVICE-LONG-LONG-LONG",
            schedule_code="TYPEOFSERVICE",
            family_type="doc",
            title="Type of Service",
            notes="Provisional historical family created from unmatched NCUC tariff span.",
        )
    )
    historical_id = repo.upsert_historical_document(
        HistoricalDocumentRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            title="Type of Service (Span 1-2)",
            state="NC",
            company="progress",
            category="rate",
            kind="pdf",
            canonical_url="https://example.test/generic.pdf",
            archived_url="https://example.test/generic.pdf",
            snapshot_timestamp=now,
            local_path=tmp_path / "generic.pdf",
            content_hash="hash-generic",
            effective_start="2024-01-01",
            retrieved_at=now,
            start_page=1,
            end_page=2,
        )
    )
    version_id = repo.upsert_tariff_version(
        TariffVersionRecord(
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            historical_document_id=historical_id,
            effective_start="2024-01-01",
            source_type="regulator",
            confidence_score=0.3,
        )
    )
    repo.upsert_tariff_charge(
        TariffChargeRecord(
            version_id=version_id,
            family_key="nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG",
            charge_type="fixed",
            charge_label="Charge",
            rate_value=None,
            rate_unit="$",
            confidence_score=0.2,
        )
    )

    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), Repository(db_path)),
    )
    monkeypatch.setattr(
        lineage_module,
        "_bootstrap",
        lambda: (SimpleNamespace(database_path=str(db_path)), Repository(db_path)),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["lineage", "show-provisional-review-candidates-nc", "--company", "progress", "--limit", "5"],
    )

    assert result.exit_code == 0
    assert "Provisional review candidates: 1" in result.stdout
    assert "nc-progress-doc-TYPEOFSERVICE-LONG-LONG-LONG" in result.stdout
    assert "review_cleanup" in result.stdout
