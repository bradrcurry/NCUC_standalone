from __future__ import annotations

import logging
import re

API_KEY_RE = re.compile(r"(api_key=)([^&\s]+)", re.I)


class _RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = API_KEY_RE.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(_RedactSecretsFilter())
