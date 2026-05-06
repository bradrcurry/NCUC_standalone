from __future__ import annotations

import csv
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.manual_import import ProgressNCHistoricalImportService
from duke_rates.historical.regulator_gaps import ProgressNCRegulatorGapService
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.history_inbox import HistoryInboxEntry
from duke_rates.utils.files import ensure_parent


class ProgressNCHistoricalInboxService:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository

    def load_manifest(self, manifest_path: Path) -> list[HistoryInboxEntry]:
        entries: list[HistoryInboxEntry] = []
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            entries.append(HistoryInboxEntry.model_validate_json(stripped))
        return entries

    def import_manifest(self, manifest_path: Path) -> list[HistoricalDocumentRecord]:
        manifest_dir = manifest_path.parent
        imported: list[HistoricalDocumentRecord] = []
        importer = ProgressNCHistoricalImportService(self.settings, self.repository)
        try:
            for entry in self.load_manifest(manifest_path):
                local_file = entry.resolve_file(manifest_dir=manifest_dir)
                record = importer.import_document(
                    title=entry.title,
                    category=entry.category,
                    source_label=entry.source_label,
                    source_authority=entry.source_authority,
                    source_type=entry.source_type,
                    source_url=entry.url,
                    local_file=local_file,
                    docket_number=entry.docket_number,
                )
                imported.append(record)
        finally:
            importer.close()
        return imported

    def generate_regulator_manifest(
        self,
        *,
        output_path: Path,
        query: str | None = None,
    ) -> int:
        gaps = ProgressNCRegulatorGapService(self.repository).build_gaps(query=query)
        rows = []
        for gap in gaps:
            rows.append(
                HistoryInboxEntry(
                    title=gap.title,
                    category=gap.category,
                    source_label="ncuc-manual",
                    source_authority="regulator",
                    source_type="ncuc",
                    file="",
                    docket_number=gap.suggested_dockets[0] if gap.suggested_dockets else None,
                    notes=[
                        f"gap_priority={gap.gap_priority}",
                        gap.reason,
                    ],
                    candidate_dockets=gap.suggested_dockets,
                    family_key=gap.family_key,
                    leaf_no=gap.leaf_no,
                )
            )
        ensure_parent(output_path).write_text(
            "\n".join(row.model_dump_json() for row in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        return len(rows)

    def export_manifest_csv(self, *, manifest_path: Path, output_path: Path) -> int:
        entries = self.load_manifest(manifest_path)
        ensure_parent(output_path)
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "title",
                    "category",
                    "leaf_no",
                    "docket_number",
                    "candidate_dockets",
                    "family_key",
                    "source_label",
                    "source_authority",
                    "source_type",
                    "file",
                    "url",
                    "notes",
                ],
            )
            writer.writeheader()
            for entry in entries:
                writer.writerow(
                    {
                        "title": entry.title,
                        "category": entry.category,
                        "leaf_no": entry.leaf_no or "",
                        "docket_number": entry.docket_number or "",
                        "candidate_dockets": " | ".join(entry.candidate_dockets),
                        "family_key": entry.family_key or "",
                        "source_label": entry.source_label,
                        "source_authority": entry.source_authority or "",
                        "source_type": entry.source_type or "",
                        "file": entry.file or "",
                        "url": entry.url or "",
                        "notes": " | ".join(entry.notes),
                    }
                )
        return len(entries)

    def export_manifest_markdown(self, *, manifest_path: Path, output_path: Path) -> int:
        entries = self.load_manifest(manifest_path)
        ensure_parent(output_path)
        lines = [
            "# Progress NC Regulator Inbox",
            "",
            "| Title | Category | Leaf | Primary Docket | Candidate Dockets | File | Notes |",
            "|---|---|---:|---|---|---|---|",
        ]
        row = (
            "| {title} | {category} | {leaf_no} | {docket_number} | "
            "{candidate_dockets} | {file} | {notes} |"
        )
        for entry in entries:
            lines.append(
                row.format(
                    title=_md_cell(entry.title),
                    category=_md_cell(entry.category),
                    leaf_no=_md_cell(entry.leaf_no or ""),
                    docket_number=_md_cell(entry.docket_number or ""),
                    candidate_dockets=_md_cell(", ".join(entry.candidate_dockets)),
                    file=_md_cell(entry.file or ""),
                    notes=_md_cell("; ".join(entry.notes)),
                )
            )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(entries)


def parse_history_inbox_manifest(manifest_path: Path) -> list[HistoryInboxEntry]:
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    return [HistoryInboxEntry.model_validate_json(line) for line in lines if line.strip()]


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()
