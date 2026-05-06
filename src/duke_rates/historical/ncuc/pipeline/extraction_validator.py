"""
Validation module for extracted rate charges.

Identifies outliers, validates data consistency, and generates quality metrics
for the Phase 2 extraction results.
"""

import sqlite3
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ValidationIssue:
    """Represents a validation concern."""
    severity: str  # 'error', 'warning', 'info'
    charge_id: int
    family_key: str
    charge_type: str
    issue: str
    rate_value: Optional[float] = None
    confidence: Optional[float] = None


class ExtractionValidator:
    """Validate extracted charges for quality and consistency."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.issues = []

    def validate_all(self) -> Dict[str, any]:
        """Run all validation checks."""
        self.issues = []

        # Get all charges
        cursor = self.conn.execute("""
            SELECT id, family_key, charge_type, rate_value, rate_unit,
                   confidence_score, source_snippet
            FROM tariff_charges
            ORDER BY family_key, charge_type
        """)
        charges = [dict(row) for row in cursor.fetchall()]

        logger.info(f"Validating {len(charges)} charges...")

        # Run checks
        self._check_outliers(charges)
        self._check_confidence_distribution(charges)
        self._check_data_completeness(charges)
        self._check_rate_reasonableness(charges)

        return self._summarize_results(charges)

    def _check_outliers(self, charges: List[dict]):
        """Identify statistical outliers."""
        by_family_type = {}

        for charge in charges:
            key = (charge['family_key'], charge['charge_type'])
            if key not in by_family_type:
                by_family_type[key] = []
            by_family_type[key].append(charge)

        # For each family-type combination, identify outliers
        for (family, ctype), group in by_family_type.items():
            if len(group) < 3:
                continue

            values = [c['rate_value'] for c in group if c['rate_value']]
            if not values:
                continue

            values.sort()
            median = values[len(values) // 2]
            q1 = values[len(values) // 4] if len(values) > 1 else values[0]
            q3 = values[3 * len(values) // 4] if len(values) > 1 else values[0]
            iqr = q3 - q1 if q3 > q1 else 1

            lower_bound = q1 - 3 * iqr
            upper_bound = q3 + 3 * iqr

            for charge in group:
                val = charge['rate_value']
                if val and (val < lower_bound or val > upper_bound):
                    self.issues.append(ValidationIssue(
                        severity='warning' if abs(val - median) < 10 * median else 'error',
                        charge_id=charge['id'],
                        family_key=family,
                        charge_type=ctype,
                        issue=f"Outlier value {val:.4f} (median: {median:.4f}, bounds: {lower_bound:.4f}-{upper_bound:.4f})",
                        rate_value=val,
                        confidence=charge['confidence_score']
                    ))

    def _check_confidence_distribution(self, charges: List[dict]):
        """Check for unexpected confidence patterns."""
        low_confidence = [c for c in charges if c['confidence_score'] < 0.80]

        if low_confidence:
            logger.info(f"Found {len(low_confidence)} charges with confidence < 0.80")
            for charge in low_confidence:
                self.issues.append(ValidationIssue(
                    severity='info',
                    charge_id=charge['id'],
                    family_key=charge['family_key'],
                    charge_type=charge['charge_type'],
                    issue=f"Low confidence: {charge['confidence_score']:.2f}",
                    rate_value=charge['rate_value'],
                    confidence=charge['confidence_score']
                ))

    def _check_data_completeness(self, charges: List[dict]):
        """Check for missing required fields."""
        for charge in charges:
            issues = []

            if not charge['charge_type']:
                issues.append("Missing charge_type")
            if charge['rate_value'] is None:
                issues.append("Missing rate_value")
            if not charge['rate_unit']:
                issues.append("Missing rate_unit")
            if not charge['confidence_score']:
                issues.append("Missing confidence_score")

            for issue in issues:
                self.issues.append(ValidationIssue(
                    severity='error',
                    charge_id=charge['id'],
                    family_key=charge['family_key'],
                    charge_type=charge['charge_type'] or 'unknown',
                    issue=issue,
                    rate_value=charge['rate_value'],
                    confidence=charge['confidence_score']
                ))

    def _check_rate_reasonableness(self, charges: List[dict]):
        """Check if rates fall within reasonable ranges."""
        # Expected ranges by charge type and unit
        reasonable_ranges = {
            ('fixed', '$/month'): (0.01, 500),           # Customer charges
            ('energy_block', '$/kWh'): (0.01, 0.50),     # Energy rates
            ('demand', '$/kW'): (0.01, 100),             # Demand charges
            ('tou_energy', '$/kWh'): (0.01, 0.50),       # TOU energy
            ('adjustment', '%'): (-100, 100),             # Adjustments
            ('adjustment', '$/kWh'): (-0.50, 0.50),      # Fuel adjustments
        }

        for charge in charges:
            ctype = charge['charge_type']
            unit = charge['rate_unit']
            val = charge['rate_value']

            if val is None:
                continue

            # Check for negative values in non-adjustment types
            if ctype not in ['adjustment', 'credit', 'adjustment_total'] and val < 0:
                self.issues.append(ValidationIssue(
                    severity='warning',
                    charge_id=charge['id'],
                    family_key=charge['family_key'],
                    charge_type=ctype,
                    issue=f"Negative rate: {val:.4f} {unit}",
                    rate_value=val,
                    confidence=charge['confidence_score']
                ))

            # Check against expected ranges
            key = (ctype, unit)
            if key in reasonable_ranges:
                low, high = reasonable_ranges[key]
                if val < low or val > high:
                    self.issues.append(ValidationIssue(
                        severity='warning' if abs(val) < 10 * high else 'error',
                        charge_id=charge['id'],
                        family_key=charge['family_key'],
                        charge_type=ctype,
                        issue=f"Out of range: {val:.4f} {unit} (expected {low}-{high})",
                        rate_value=val,
                        confidence=charge['confidence_score']
                    ))

    def _summarize_results(self, charges: List[dict]) -> Dict[str, any]:
        """Generate summary statistics."""
        by_severity = {}
        for issue in self.issues:
            if issue.severity not in by_severity:
                by_severity[issue.severity] = []
            by_severity[issue.severity].append(issue)

        return {
            'total_charges': len(charges),
            'total_issues': len(self.issues),
            'issues_by_severity': {sev: len(issues) for sev, issues in by_severity.items()},
            'issues': self.issues,
            'error_families': list(set(i.family_key for i in by_severity.get('error', []))),
        }

    def get_top_issues(self, limit: int = 20) -> List[ValidationIssue]:
        """Get top N issues, prioritizing errors."""
        errors = [i for i in self.issues if i.severity == 'error']
        warnings = [i for i in self.issues if i.severity == 'warning']

        return (errors + warnings)[:limit]

    def close(self):
        """Close database connection."""
        self.conn.close()


def validate_extraction(db_path: str) -> Dict[str, any]:
    """Main entry point for extraction validation."""
    validator = ExtractionValidator(db_path)
    try:
        results = validator.validate_all()
        results['issues'] = validator.get_top_issues(50)
        return results
    finally:
        validator.close()
