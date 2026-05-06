from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from duke_rates.discovery.link_extractor import extract_jss_state
from duke_rates.utils.text import normalize_whitespace


def extract_html_text(path: Path) -> str:
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    text = normalize_whitespace(soup.get_text(" ", strip=True))
    state = extract_jss_state(html)
    if state:
        text = f"{text}\n\nJSS_STATE_PRESENT"
    return text
