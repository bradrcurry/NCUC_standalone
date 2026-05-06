"""Shared season-matching utilities for the billing engine.

Single source of truth for Duke NC season label → calendar month mapping.

Previously two independent implementations existed:
- ``billing/engine.py`` — ``_ENGINE_SEASON_MONTHS`` + ``_season_matches()``
- ``db/ncuc_loader.py`` — ``_SEASON_MONTHS`` + ``_filter_seasonal_charges()``

Both are now replaced by ``season_matches()`` from this module.

Normalization
-------------
Season labels from PDF parsing are inconsistent.  The canonical normalization
applied here is:

    1. lower-case
    2. replace Unicode en-dash (\\u2013) and em-dash (\\u2014) with ASCII hyphen
    3. strip whitespace around the hyphen (``"may - september"`` → ``"may-september"``)
    4. strip leading/trailing whitespace

This collapses all of the following to ``"may-september"``:
    ``"May - September"``, ``"May-September"``, ``"May–September"`` (en-dash),
    ``"MAY-SEPTEMBER"``, ``" may - september "``
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical season → month set mapping
# ---------------------------------------------------------------------------
# Combined key set covers all variants observed in DEP and DEC tariff parsing.
# Keys are in *normalized* form (see _normalize_season_label below).

SEASON_MONTHS: dict[str, set[int]] = {
    # Duke NC standard residential seasons
    "may-september":       {5, 6, 7, 8, 9},
    "june-september":      {6, 7, 8, 9},
    "october-april":       {10, 11, 12, 1, 2, 3, 4},
    "october-may":         {10, 11, 12, 1, 2, 3, 4, 5},
    # Pre-2023 DEP RES "Bills Rendered During" two-column format
    "july-october":        {7, 8, 9, 10},
    "november-june":       {11, 12, 1, 2, 3, 4, 5, 6},
    # Generic labels sometimes used in tariff text
    "summer":              {6, 7, 8, 9},
    "winter":              {10, 11, 12, 1, 2, 3, 4},
    # DEC RS seasonal variants
    "july-september":      {7, 8, 9},
    "october-june":        {10, 11, 12, 1, 2, 3, 4, 5, 6},
}


def _normalize_season_label(label: str) -> str:
    """Return the normalized form of a season label string.

    Normalization steps:
    1. lower-case
    2. replace en-dash / em-dash with ASCII hyphen
    3. strip whitespace around the hyphen
    4. strip outer whitespace
    """
    s = label.lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    # Collapse " - " → "-" (any whitespace around a hyphen)
    s = re.sub(r"\s*-\s*", "-", s)
    return s.strip()


def season_matches(season_label: str | None, month: int) -> bool:
    """Return True if *month* falls within the named season.

    Parameters
    ----------
    season_label:
        The raw season string from a parsed tariff charge (e.g.
        ``"May - September"``, ``"October-April"``, ``None``).
        ``None`` or empty string means the charge applies year-round.
    month:
        Calendar month number (1 = January … 12 = December).

    Returns
    -------
    bool
        ``True``  if the charge applies to *month*.
        ``False`` if the charge is explicitly limited to a different season.

    Notes
    -----
    - An **unknown** season label (not in ``SEASON_MONTHS``) logs a WARNING
      and returns ``True`` (year-round) as a safe fallback.  This is the
      same behavior as the legacy implementations, but now it is visible.
    - A ``month`` value of ``0`` (sentinel used by ``calculate_bill()`` when
      no billing date is available) returns ``True`` unconditionally.
    """
    if not season_label:
        return True
    if month == 0:
        return True

    normalized = _normalize_season_label(season_label)
    months = SEASON_MONTHS.get(normalized)

    if months is not None:
        return month in months

    # Unknown season label — warn and fall through to year-round
    log.warning(
        "Unknown season label %r (normalized: %r) — treating as year-round. "
        "Add to billing.season_utils.SEASON_MONTHS if this label is valid.",
        season_label,
        normalized,
    )
    return True
