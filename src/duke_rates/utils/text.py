from __future__ import annotations

import re
import unicodedata

WHITESPACE_RE = re.compile(r"\s+")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def slugify(text: str, *, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = SLUG_RE.sub("-", normalized.lower()).strip("-")
    return slug[:max_length] or "document"
