"""Tests for NCUC docket status detection (PR #34 follow-up)."""
from __future__ import annotations

import sqlite3

import pytest

from duke_rates.historical.ncuc.status_detection import (
    compute_docket_status,
    detect_docket_status_from_db,
    docket_has_order,
    docket_has_proposal_filing,
)


class TestComputeDocketStatus:
    def test_order_wins_even_with_application(self):
        # Application + Order in same docket → approved (the typical pattern
        # for a docket that's already been decided)
        assert compute_docket_status(
            ["application", "order", "exhibit"],
            ["Filing", "Order", "Filing"],
        ) == "approved"

    def test_pure_application_is_proposed(self):
        # An open docket: application + testimony, no order yet
        assert compute_docket_status(
            ["application", "testimony"],
            ["Filing", "Filing"],
        ) == "proposed"

    def test_only_exhibits_is_approved(self):
        # Compliance bundle docket: exhibits + tariff_sheets, no proposal-type
        # filings — not a rate-application proceeding at all
        assert compute_docket_status(
            ["exhibit", "tariff_sheets", "other"],
            ["Filing", "Filing", None],
        ) == "approved"

    def test_proceeding_type_order_also_counts(self):
        # Some records have classification='other' but proceeding_type='Order'
        assert compute_docket_status(
            ["application", "other"],
            ["Filing", "Order"],
        ) == "approved"

    def test_empty_inputs_default_to_approved(self):
        assert compute_docket_status([], []) == "approved"

    def test_none_values_are_safe(self):
        assert compute_docket_status([None, None], [None, None]) == "approved"


class TestPredicates:
    def test_docket_has_order_classification(self):
        assert docket_has_order(["order"], [None])
        assert docket_has_order(["ORDER"], [None])  # case-insensitive

    def test_docket_has_order_proceeding_type(self):
        assert docket_has_order(["other"], ["Order"])

    def test_docket_has_no_order(self):
        assert not docket_has_order(["application"], ["Filing"])

    def test_proposal_filing_detection(self):
        assert docket_has_proposal_filing(["application"])
        assert docket_has_proposal_filing(["testimony"])
        assert docket_has_proposal_filing(["settlement"])
        assert docket_has_proposal_filing(["compliance_filing"])
        assert not docket_has_proposal_filing(["exhibit", "order"])


class TestDetectFromDb:
    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.execute(
            """CREATE TABLE ncuc_discovery_records (
                id INTEGER PRIMARY KEY,
                docket_number TEXT,
                filing_classification TEXT,
                proceeding_type TEXT
            )"""
        )
        return c

    def test_open_docket_is_proposed(self, conn):
        conn.executemany(
            "INSERT INTO ncuc_discovery_records (docket_number, filing_classification, proceeding_type) VALUES (?, ?, ?)",
            [
                ("E-7 Sub 9999", "application", "Filing"),
                ("E-7 Sub 9999", "testimony", "Filing"),
            ],
        )
        assert detect_docket_status_from_db(conn, "E-7 Sub 9999") == "proposed"

    def test_decided_docket_is_approved(self, conn):
        conn.executemany(
            "INSERT INTO ncuc_discovery_records (docket_number, filing_classification, proceeding_type) VALUES (?, ?, ?)",
            [
                ("E-7 Sub 9998", "application", "Filing"),
                ("E-7 Sub 9998", "order", "Order"),
            ],
        )
        assert detect_docket_status_from_db(conn, "E-7 Sub 9998") == "approved"

    def test_unknown_docket_defaults_to_approved(self, conn):
        assert detect_docket_status_from_db(conn, "E-7 Sub 0") == "approved"
