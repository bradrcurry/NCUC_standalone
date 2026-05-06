from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_gap_harvest_targets_use_family_specific_terms() -> None:
    module = load_script_module("harvest_gap_targets", "scripts/ingestion/harvest_gap_targets.py")

    target_terms = {}
    for target in module.TARGETS:
        target_terms.setdefault(target.focus, set()).update(target.terms)

    assert "service regulations" in target_terms["DEP leaf-800"]
    assert "opt-v" not in target_terms["DEP leaf-800"]

    assert "power pair" in target_terms["DEP leaf-770"]
    assert "renewable energy" not in target_terms["DEP leaf-770"]

    assert "standby service" in target_terms["DEP leaf-653"]
    assert "eev" not in target_terms["DEP leaf-653"]


def test_candidate_attachments_requires_target_terms() -> None:
    module = load_script_module("harvest_gap_targets_candidate", "scripts/ingestion/harvest_gap_targets.py")

    row = SimpleNamespace(
        description="E-7 Sub 1032 DEC's Compliance Tariffs NES and REA Programs",
        view_file_labels=["E-7 Sub 1032 DEC's Compliance Tariffs NES and REA Programs"],
        view_file_urls=["https://example.test/ViewFile.aspx?Id=1"],
    )
    target = next(target for target in module.TARGETS if target.focus == "DEP leaf-770")

    assert module.candidate_attachments(row, target) == []


def test_candidate_attachments_keeps_powerpair_tariff_exhibit() -> None:
    module = load_script_module("harvest_gap_targets_positive", "scripts/ingestion/harvest_gap_targets.py")

    row = SimpleNamespace(
        description="Duke Energy NC - DEP NC PowerPair Program Tariff",
        view_file_labels=["Duke Energy NC - DEP NC PowerPair Program Tariff (EXHIBIT 1)"],
        view_file_urls=["https://example.test/ViewFile.aspx?Id=2"],
    )
    target = next(target for target in module.TARGETS if target.focus == "DEP leaf-770")

    matches = module.candidate_attachments(row, target)
    assert len(matches) == 1
    assert "Tariff" in matches[0][0]


def test_register_manifest_accepts_manifest_and_focus_args(tmp_path: Path) -> None:
    module = load_script_module("register_harvest_manifest", "scripts/ingestion/register_harvest_manifest.py")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": []}), encoding="utf-8")

    with patch.object(
        sys,
        "argv",
        [
            "register_harvest_manifest.py",
            "--manifest",
            str(manifest),
            "--focus",
            "DEP leaf-800",
            "--dry-run",
        ],
    ):
        args = module.parse_args()

    assert args.manifest == manifest
    assert args.focus == ["DEP leaf-800"]
    assert args.dry_run is True
    assert args.allow_duplicates is False


def test_register_manifest_infers_family_from_focus_label() -> None:
    module = load_script_module("register_harvest_manifest_focus", "scripts/ingestion/register_harvest_manifest.py")

    assert module.infer_family_keys({"focus": "DEC Rider CEI"}, "Duke Energy Carolinas") == [
        "nc-carolinas-rider-CEI"
    ]
    assert module.infer_family_keys({"focus": "DEP leaf-770"}, "Duke Energy Progress") == [
        "nc-progress-leaf-770"
    ]
