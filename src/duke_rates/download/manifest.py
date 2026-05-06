from __future__ import annotations

import json
from pathlib import Path

from duke_rates.models.document import DiscoveryRecord
from duke_rates.utils.files import ensure_parent


class ManifestWriter:
    def __init__(self, path: Path):
        self.path = ensure_parent(path)

    def append(self, record: DiscoveryRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")
