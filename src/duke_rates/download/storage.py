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


def verified_pdf_sha256(path: Path, expected: str | None = None) -> str:
    """Verify original PDF bytes and readability before reuse or registration."""
    import hashlib

    import fitz

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    if expected is not None and sha != expected:
        raise ValueError(f"PDF SHA-256 mismatch: {path}")
    with fitz.open(path) as document:
        if not document.is_pdf or document.is_encrypted or document.page_count < 1:
            raise ValueError(f"PDF is unreadable or has no pages: {path}")
    return sha


def preserve_pdf_blob(source: Path, archive_root: Path) -> tuple[Path, str, bool]:
    """Atomically publish exact bytes under SHA-256; never replace an existing blob.

    The caller owns the incoming file. Invalid incoming files remain available
    for diagnosis. An existing conflicting destination is an integrity error.
    """
    import os
    import shutil
    import tempfile

    sha = verified_pdf_sha256(source)
    target = archive_root.resolve() / "sha256" / sha[:2] / f"{sha}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verified_pdf_sha256(target, sha)
        return target, sha, True
    descriptor, temporary = tempfile.mkstemp(prefix=".blob-", suffix=".part", dir=target.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        verified_pdf_sha256(staged, sha)
        try:
            os.link(staged, target)
            existed = False
        except FileExistsError:
            verified_pdf_sha256(target, sha)
            existed = True
    finally:
        staged.unlink(missing_ok=True)
    return target, sha, existed
