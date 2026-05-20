"""Tests for the generic_residential cross-attribution guard in
BulkExtractor._should_apply_fallback.

Regression history:
  - 2026-05-16: hd_id=14 (JAA compliance book, no span) and hd_id=1847 (RDM
    compliance book, no span) had family-specific initial profiles return 0
    charges and the fallback unconditionally accepted generic_residential,
    which harvested 55/64 charges from a multi-schedule rate matrix and
    attributed them to the wrong family. The initial guard refused
    generic_residential fallback only when the doc had no page bounds.
  - 2026-05-20: hd_id=179 (leaf-602 JAA proposed order, bounded span 1-6)
    still produced 9 garbage charges via generic_residential fallback —
    narrative proposed-order text like "Duke Energy Progress filed an
    application... on June" parsed as "$0.49/kWh". The guard was broadened
    to refuse generic_residential fallback whenever the initial profile is
    family-specific, regardless of span boundedness.
"""

from __future__ import annotations

import pytest

from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor


METRICS_EMPTY = {
    "unique_charge_types": 0,
    "tou_period_count": 0,
    "season_count": 0,
    "completeness_score": 0,
}
METRICS_GENERIC_RES = {
    "unique_charge_types": 3,
    "tou_period_count": 2,
    "season_count": 1,
    "completeness_score": 5,
}


@pytest.fixture
def extractor(tmp_path):
    db_path = tmp_path / "stub.db"
    return BulkExtractor(db_path=str(db_path))


def test_unbounded_fallback_to_generic_residential_is_blocked(extractor):
    """When start_page is None and initial profile is family-specific,
    fallback to generic_residential must be refused even if it would
    produce charges. Regression: hd_id=14 / hd_id=1847."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=55,
        candidate_outcome_quality="weak",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is False
    assert reason is None


def test_bounded_fallback_to_generic_residential_is_also_blocked(extractor):
    """2026-05-20 broadening: even with bounded spans, generic_residential's
    broad rate-shaped-text regex will harvest narrative mentions and attribute
    them to a non-residential family. Regression: hd_id=179 leaf-602 JAA
    proposed-order doc (span 1-6) produced 9 garbage charges via this path."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=9,
        candidate_outcome_quality="weak",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=True,
    )
    assert should is False
    assert reason is None


def test_unbounded_fallback_to_specific_profile_still_allowed(extractor):
    """The guard targets only generic_residential. Family-specific fallbacks
    (e.g. progress_single_value_rider) are still allowed even without span
    boundaries — they have their own family-key gates that prevent
    cross-schedule pollution."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="progress_single_value_rider",
        candidate_charge_count=3,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_unbounded_generic_to_generic_not_blocked(extractor):
    """If the initial profile was also generic_residential, the guard does
    not apply — there's no cross-family attribution to worry about."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="generic_residential",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
        has_page_bounds=False,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_unknown_initial_profile_still_allows_generic_residential_fallback(extractor):
    """When the initial profile is `unknown` (no family-specific match), the
    guard does not fire — generic_residential is the intended salvage path
    for unclassified docs that happen to contain residential rate patterns."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="unknown",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
    )
    assert should is True
    assert reason == "empty_initial_parse"


def test_insert_charges_force_clear_deletes_stale_when_no_new_charges(tmp_path):
    """2026-05-20 force_clear: when re-extraction now produces 0 charges
    (because the broadened guard refuses a previously-polluting fallback),
    the old polluted charges must be deletable in the same run. Without
    force_clear, insert_charges returns early on empty input and leaves
    stale rows in tariff_charges. With force_clear=True, the DELETE runs
    even when charges is empty."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "force_clear.db"
    # Minimal schema for the test — just the table insert_charges writes to.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER, family_key TEXT, charge_type TEXT, charge_label TEXT,
            rate_value REAL, rate_unit TEXT, tier_min REAL, tier_max REAL,
            tou_period TEXT, season TEXT, source_snippet TEXT,
            confidence_score REAL, created_at TEXT
        );
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'nc-progress-leaf-602', 'Polluted Energy Block', 0.005);
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'nc-progress-leaf-602', 'Polluted Energy Block', 0.007);
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))

    # Without force_clear: 0 new charges → no DELETE, old rows survive
    inserted = extractor.insert_charges(version_id=1, family_key="nc-progress-leaf-602", charges=[])
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=1").fetchone()[0]
    check.close()
    assert count == 2, f"expected stale charges to survive without force_clear; got {count}"

    # With force_clear=True: 0 new charges → DELETE still runs, old rows cleared
    inserted = extractor.insert_charges(
        version_id=1, family_key="nc-progress-leaf-602", charges=[], force_clear=True
    )
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id=1").fetchone()[0]
    check.close()
    assert count == 0, f"expected stale charges cleared with force_clear; got {count}"


def test_insert_charges_force_clear_no_op_when_version_id_unset(tmp_path):
    """force_clear must not bypass the version_id guard. Without a
    version_id we don't know which rows to clear and a wildcard DELETE
    would be catastrophic — must return 0 unchanged."""
    import sqlite3
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor

    db_path = tmp_path / "force_clear_noversion.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tariff_charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER, family_key TEXT, charge_type TEXT, charge_label TEXT,
            rate_value REAL, rate_unit TEXT, tier_min REAL, tier_max REAL,
            tou_period TEXT, season TEXT, source_snippet TEXT,
            confidence_score REAL, created_at TEXT
        );
        INSERT INTO tariff_charges (version_id, family_key, charge_label, rate_value)
        VALUES (1, 'some-family', 'Existing', 0.05);
        """
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(db_path=str(db_path))
    inserted = extractor.insert_charges(
        version_id=0, family_key="some-family", charges=[], force_clear=True
    )
    assert inserted == 0
    check = sqlite3.connect(db_path)
    count = check.execute("SELECT COUNT(*) FROM tariff_charges").fetchone()[0]
    check.close()
    assert count == 1, "force_clear with version_id=0 must not delete anything"


def test_default_has_page_bounds_preserves_caller_compatibility(extractor):
    """has_page_bounds still defaults to True so existing call sites that
    don't pass the kwarg keep working; the kwarg is no longer load-bearing
    for the guard logic but is retained for caller compatibility."""
    should, reason = extractor._should_apply_fallback(
        current_profile_name="progress_jaa_rider",
        current_charge_count=0,
        current_outcome_quality="empty",
        current_metrics=METRICS_EMPTY,
        candidate_name="generic_residential",
        candidate_charge_count=5,
        candidate_outcome_quality="strong",
        candidate_metrics=METRICS_GENERIC_RES,
    )
    # Now refused regardless of has_page_bounds — this is the 2026-05-20 fix.
    assert should is False
    assert reason is None
