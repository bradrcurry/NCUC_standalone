from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class HistoryInboxEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    category: str
    source_label: str
    source_authority: str | None = None
    source_type: str | None = None
    file: str | None = None
    url: str | None = None
    docket_number: str | None = None
    notes: list[str] = Field(default_factory=list)
    candidate_dockets: list[str] = Field(default_factory=list)
    family_key: str | None = None
    leaf_no: str | None = None

    def resolve_file(self, *, manifest_dir: Path) -> Path | None:
        if not self.file:
            return None
        path = Path(self.file)
        return path if path.is_absolute() else manifest_dir / path
