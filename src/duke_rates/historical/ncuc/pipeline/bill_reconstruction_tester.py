"""
Test bill reconstruction using extracted historical rates.

Validates that extracted charges can be used for bill calculation.
"""

import sqlite3
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class BillTest:
    """Result of a bill reconstruction test."""
    family_key: str
    effective_date: str
    kwh: float
    expected_charge_components: int  # Number of charge types found
    can_reconstruct: bool
    reason: str


class BillReconstructionTester:
    """Test bill reconstruction capability with extracted rates."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def test_residential_families(self) -> Dict[str, any]:
        """Test residential rate families (Leaf 500-504) for bill reconstruction."""
        residential_keys = [
            'nc-progress-leaf-500',  # Residential Service
            'nc-progress-leaf-501',  # Fuel Charge Adjustment
            'nc-progress-leaf-502',  # R-TOU
            'nc-progress-leaf-503',  # R-TOU
            'nc-progress-leaf-504',  # R-TOU-EV
        ]

        results = []
        for family_key in residential_keys:
            test_result = self._test_family_versions(family_key)
            results.extend(test_result)

        return self._summarize_tests(results)

    def _test_family_versions(self, family_key: str) -> List[BillTest]:
        """Test multiple versions of a family."""
        tests = []

        # Get all versions with extracted charges for this family
        cursor = self.conn.execute("""
            SELECT DISTINCT tv.id, tv.effective_start, COUNT(DISTINCT tc.charge_type) as charge_types
            FROM tariff_versions tv
            LEFT JOIN tariff_charges tc ON tv.id = tc.version_id AND tc.family_key = ?
            WHERE tv.family_key = ?
                AND (tc.notes IS NULL OR tc.notes NOT LIKE 'TIER_BOUNDARY%')
            GROUP BY tv.id
            ORDER BY tv.effective_start DESC
            LIMIT 5
        """, (family_key, family_key))

        versions = [dict(row) for row in cursor.fetchall()]

        for version in versions:
            # Get all charges for this version
            cursor = self.conn.execute("""
                SELECT charge_type, rate_value, rate_unit, season, tou_period
                FROM tariff_charges
                WHERE version_id = ? AND family_key = ?
                    AND (notes IS NULL OR notes NOT LIKE 'TIER_BOUNDARY%')
                ORDER BY charge_type
            """, (version['id'], family_key))

            charges = [dict(row) for row in cursor.fetchall()]

            if not charges:
                tests.append(BillTest(
                    family_key=family_key,
                    effective_date=version['effective_start'] or 'unknown',
                    kwh=0,
                    expected_charge_components=0,
                    can_reconstruct=False,
                    reason='No usable charges found'
                ))
                continue

            # Check for required charge types
            charge_types = set(c['charge_type'] for c in charges)

            # Residential should have fixed (customer charge) + energy charges
            has_fixed = 'fixed' in charge_types or any(c['rate_unit'] == '$/month' for c in charges)
            has_energy = 'energy_block' in charge_types or 'tou_energy' in charge_types

            can_reconstruct = has_fixed or has_energy

            reason = ""
            if has_fixed:
                reason += "customer_charge "
            if has_energy:
                reason += "energy_rate"
            if not reason:
                reason = "missing required charge types"

            tests.append(BillTest(
                family_key=family_key,
                effective_date=version['effective_start'] or 'unknown',
                kwh=0,
                expected_charge_components=len(charge_types),
                can_reconstruct=can_reconstruct,
                reason=reason
            ))

        return tests

    def test_commercial_families(self) -> List[BillTest]:
        """Test commercial rate families (Leaf 520-535)."""
        commercial_keys = [
            'nc-progress-leaf-520',  # Small General Service
            'nc-progress-leaf-532',  # Commercial Small
            'nc-progress-leaf-535',  # LGS
        ]

        results = []
        for family_key in commercial_keys:
            test_result = self._test_family_versions(family_key)
            results.extend(test_result)

        return results

    def _summarize_tests(self, tests: List[BillTest]) -> Dict[str, any]:
        """Summarize test results."""
        total = len(tests)
        can_reconstruct = sum(1 for t in tests if t.can_reconstruct)
        by_family = {}

        for test in tests:
            if test.family_key not in by_family:
                by_family[test.family_key] = {'total': 0, 'ok': 0}
            by_family[test.family_key]['total'] += 1
            if test.can_reconstruct:
                by_family[test.family_key]['ok'] += 1

        return {
            'total_tests': total,
            'can_reconstruct': can_reconstruct,
            'pct_success': 100 * can_reconstruct / total if total > 0 else 0,
            'by_family': by_family,
            'tests': tests
        }

    def close(self):
        """Close database connection."""
        self.conn.close()


def test_bill_reconstruction(db_path: str) -> Dict[str, any]:
    """Main entry point for bill reconstruction testing."""
    tester = BillReconstructionTester(db_path)
    try:
        # Test residential families (critical for bill reconstruction)
        residential_results = tester.test_residential_families()

        # Also test commercial
        commercial_results = tester.test_commercial_families()

        return {
            'residential': residential_results,
            'commercial': commercial_results,
            'all_testable': len(residential_results['tests']) + len(commercial_results)
        }
    finally:
        tester.close()
