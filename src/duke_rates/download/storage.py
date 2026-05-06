from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from duke_rates.models.document import DiscoveryRecord, DocumentKind
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.text import slugify


def build_archive_path(base_dir: Path, record: DiscoveryRecord, suffix: str | None = None) -> Path:
    state = (record.state or "unknown").lower()
    company = (record.company or "unknown").lower()
    category = record.category.value
    parsed = urlparse(str(record.document_url))
    url_hint = f"{parsed.path}-{parsed.query}".strip("-")
    stem = slugify(f"{record.title}-{url_hint}")
    extension = suffix or (".pdf" if record.kind == DocumentKind.PDF else ".html")
    return ensure_parent(base_dir / state / company / category / f"{stem}{extension}")
