from __future__ import annotations

from pathlib import Path

import pandas as pd

from duke_rates.analytics import dep_leaf503_audit


def test_build_dep_leaf503_audit_reports_missing_rider_links(monkeypatch) -> None:
    version_rows = [
        {
            "version_id": 1,
            "effective_start": "2022-03-16",
            "effective_end": None,
            "revision_label": None,
            "source_type": "regulator",
            "historical_document_id": 10,
            "historical_local_path": "data/historical/leaf503.pdf",
            "charge_count": 5,
        }
    ]

    monkeypatch.setattr(dep_leaf503_audit, "_load_leaf503_versions", lambda conn: version_rows)
    monkeypatch.setattr(dep_leaf503_audit, "_load_rider_links", lambda conn: [])
    monkeypatch.setattr(
        dep_leaf503_audit,
        "load_dep_res_validation_report",
        lambda database_path=None: {
            "summary": {
                "applicable_riders_by_schedule": {
                    "R-TOU-CPP": ["BA-DSM", "CPRE"],
                }
            }
        },
    )
    monkeypatch.setattr(
        dep_leaf503_audit,
        "load_dep_res_canonical_rider_components",
        lambda database_path=None: pd.DataFrame(
            [
                {
                    "effective_date": pd.Timestamp("2022-01-01"),
                    "rider_code": "BA-DSM",
                    "source_kind": "provisional_ingest",
                },
                {
                    "effective_date": pd.Timestamp("2022-01-01"),
                    "rider_code": "CPRE",
                    "source_kind": "provisional_ingest",
                },
            ]
        ),
    )

    report = dep_leaf503_audit.build_dep_leaf503_audit(Path("fake.db"))

    assert report["summary"]["rider_applicability_link_count"] == 0
    assert report["summary"]["missing_rider_applicability_links"] is True
    assert report["summary"]["versions_with_rider_source_coverage"] == 1
    assert report["versions"][0]["rider_coverage_status"] == "carried_forward"
    assert report["versions"][0]["missing_expected_rider_count"] == 0


def test_export_dep_leaf503_audit_writes_markdown_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        dep_leaf503_audit,
        "build_dep_leaf503_audit",
        lambda database_path=None: {
            "summary": {
                "generated_at": "2026-04-07",
                "family_key": "nc-progress-leaf-503",
                "schedule_label": "R-TOU-CPP",
                "version_count": 1,
                "rider_applicability_link_count": 0,
                "expected_rider_code_count": 2,
                "expected_rider_codes": ["BA-DSM", "CPRE"],
                "canonical_rider_effective_dates": ["2022-01-01"],
                "canonical_rider_series_count": 1,
                "versions_with_rider_source_coverage": 1,
                "missing_rider_applicability_links": True,
            },
            "versions": [
                {
                    "version_id": 1,
                    "effective_start": "2022-03-16",
                    "effective_end": None,
                    "revision_label": None,
                    "source_type": "regulator",
                    "historical_document_id": 10,
                    "historical_local_path": "data/historical/leaf503.pdf",
                    "charge_count": 5,
                    "matched_rider_effective_date": "2022-01-01",
                    "rider_coverage_status": "carried_forward",
                    "matched_rider_code_count": 2,
                    "missing_expected_rider_count": 0,
                    "missing_expected_rider_codes": "",
                    "matched_source_kinds": "provisional_ingest",
                }
            ],
            "rider_applicability_links": [],
        },
    )

    output_paths = dep_leaf503_audit.export_dep_leaf503_audit(tmp_path / "out")

    assert output_paths["markdown"].exists()
    assert output_paths["summary_json"].exists()
    assert output_paths["markdown"].read_text(encoding="utf-8").startswith("# DEP Leaf 503 Audit")
