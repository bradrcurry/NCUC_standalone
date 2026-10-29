"""Recut the manifest's original archived pages; never modify the source PDFs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main() -> None:
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    hashes = {}
    for item in manifest["pages"]:
        source = (ROOT / item["source_path"]).resolve()
        if not source.is_relative_to(ROOT):
            raise ValueError(f"Source outside repository archive: {source}")
        if source not in hashes:
            hashes[source] = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashes[source] != item["source_sha256"]:
            raise ValueError(f"Source hash changed: {item['id']}")
    for item in manifest["pages"]:
        source = (ROOT / item["source_path"]).resolve()
        target = (ROOT / item["fixture"]).resolve()
        if not target.is_relative_to(HERE):
            # Existing D2 excerpts have their own generator and provenance.
            if hashlib.sha256(target.read_bytes()).hexdigest() != item["fixture_sha256"]:
                raise ValueError(f"Existing D2 fixture hash changed: {item['id']}")
            continue
        temporary = target.with_suffix(".regenerate.pdf")
        with fitz.open(source) as doc, fitz.open() as cut:
            page = item["original_page"] - 1
            cut.insert_pdf(doc, from_page=page, to_page=page)
            cut.save(temporary, garbage=3, deflate=True)
        temporary.replace(target)
        item["fixture_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
