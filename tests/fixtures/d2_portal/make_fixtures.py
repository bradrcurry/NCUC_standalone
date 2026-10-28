"""Regenerate the D2 portal fixtures from the pages captured on 2026-03-18.

The two captured pages (``data/doc_param_form.html`` and
``data/doc_param_results.html``, gitignored) are copied with the hidden ASP.NET
state blanked (``__VIEWSTATE`` / ``__EVENTVALIDATION``/``__VIEWSTATEGENERATOR``).
Nothing else is changed, and the searcher never reads those fields.

Derived pages (later pages, a small 3-page result set, the empty and blocked
pages) are built at test time from ``results_captured.html`` by
``tests/test_d2_portal_search.py`` helpers, so every derivation is visible in
the test that relies on it.

    python tests/fixtures/d2_portal/make_fixtures.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
_HIDDEN = re.compile(r'(name="(?:__VIEWSTATE|__EVENTVALIDATION|__VIEWSTATEGENERATOR)"[^>]*?value=")[^"]*(")')


def main() -> None:
    for src, dst in (("doc_param_form.html", "form_captured.html"), ("doc_param_results.html", "results_captured.html")):
        html = (ROOT / "data" / src).read_text(encoding="utf-8", errors="replace")
        (HERE / dst).write_text(_HIDDEN.sub(r"\1\2", html), encoding="utf-8")


if __name__ == "__main__":
    main()
