"""Tests for the NC Progress leaf PDF parser (src/duke_rates/parse/nc_progress.py)."""
from __future__ import annotations

import pytest

from duke_rates.parse.nc_progress import parse_nc_progress_leaf

# ---------------------------------------------------------------------------
# Fixtures — synthetic PDF text that mirrors real NC Progress leaf layouts
# ---------------------------------------------------------------------------

RES_TEXT = """\
Duke Energy Progress, LLC NC Second Revised Leaf No. 500
(North Carolina Only) Superseding NC First Revised Leaf No. 500
RESIDENTIAL SERVICE
SCHEDULE RES

MONTHLY RATE

I. For Single-Phase Service:

Service used during May - September Service used during October - April
A. Basic Customer Charge: Basic Customer Charge:
$14.00 per month $14.00 per month
B. Kilowatt-Hour Charge: Kilowatt-Hour Charge:
12.623\xa2 per kWh for all kWh 12.623\xa2 per kWh for the first 800 kWh
11.623\xa2 per kWh for the additional kWh

II. For Three-Phase Service:
The bill computed for single-phase service plus $9.00.

III. Riders
Leaf No. 601 Rider BA
Leaf No. 602 Rider JAA
Leaf No. 607 Rider STS

IV. Storm Securitization Charge:
A Storm Securitization charge (Leaf No. 607 Rider STS).

NC Second Revised Leaf No. 500
Effective for service rendered on and after October 1, 2025
NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023
"""

TOU_TEXT = """\
Duke Energy Progress, LLC NC Second Revised Leaf No. 502
(North Carolina Only) Superseding NC First Revised Leaf No. 502
RESIDENTIAL SERVICE TIME-OF-USE
SCHEDULE R-TOU

MONTHLY RATE

I. For Single-Phase Service:

A. Service used during May through September:
1. Basic Customer Charge:
$14.00
2. kWh Energy Charge:
29.905\xa2 per On-Peak kWh 29.905\xa2 per On-Peak kWh
11.321\xa2 per Off-Peak kWh 11.321\xa2 per Off-Peak kWh
7.372\xa2 per Discount kWh 7.372\xa2 per Discount kWh

II. For Three-Phase Service:
The bill computed for single-phase service plus $9.00.

III. Riders
Leaf No. 601 Rider BA
Leaf No. 602 Rider JAA

NC Second Revised Leaf No. 502
Effective for service rendered on and after October 1, 2025
"""

RTOUD_QUALIFIED_DEMAND_TEXT = """\
Duke Energy Progress, LLC NC Original Leaf No. 501
(North Carolina Only) Superseding NC Prior Leaf No. 501
RESIDENTIAL SERVICE TIME-OF-USE
SCHEDULE R-TOUD

MONTHLY RATE

I. For Single-Phase Service:
A. Basic Customer Charge:
$14.00
B. kWh Energy Charge:
21.952¢ per On-Peak kWh
11.000¢ per Off-Peak kWh
8.274¢ per Discount kWh
C. Demand Charges:
$1.95 per On-Peak kW
$3.82 per Max kW

NC Original Leaf No. 501
Effective for service rendered on and after October 1, 2024
"""

RTOUD_LEGACY_TOU_NO_CENT_TEXT = """\
Duke Energy Progress, Inc. R-2 (North Carolina Only)
RESIDENTIAL SERVICE TIME-OF-USE
SCHEDULE R-TOUD-24A

MONTHLY RATE

I. For Single-Phase Service:
A. Service used during calendar months of June through September:
1. Basic Customer Charge:
$14.60
2. On-Peak kW Demand Charge:
$5.14 per kW for all on-peak Billing Demand
3. kWh Energy Charge:
6.9480 per On-Peak kWh
5.5411 per Off-Peak kWh

B. Service used during calendar months of October through May:
1. Basic Customer Charge:
$14.60
2. On-Peak kW Demand Charge:
$3.81 per kW for all on-peak Billing Demand
3. kWh Energy Charge:
6.9480 per On-Peak kWh
5.5410 per Off-Peak kWh

II. For Three-Phase Service:
The bill computed for single-phase service plus $9.00.

Supersedes Schedule R-TOUD-24
Effective for service rendered on and after June 1, 2013
NCUC Docket No. E-2, Sub 1023
"""

SGS_TEXT = """\
Duke Energy Progress, LLC NC Second Revised Leaf No. 520
(North Carolina Only) Superseding NC First Revised Leaf No. 520
SMALL GENERAL SERVICE
SCHEDULE SGS

MONTHLY RATE

A. Basic Customer Charge: $22.00 per month
B. Demand Charge: $10.50 per kW
C. kWh Energy Charge: 6.543\xa2 per kWh for all kWh

Riders
Leaf No. 601 Rider BA
Leaf No. 602 Rider JAA

NC Second Revised Leaf No. 520
Effective for service rendered on and after October 1, 2025
"""

RIDER_TEXT = """\
Duke Energy Progress, LLC NC Sixth Revised Leaf No. 601
(North Carolina Only) Superseding NC Fifth Revised Leaf No. 601
ANNUAL BILLING ADJUSTMENTS
RIDER BA

This Rider applies to all base rate schedules.
The currently approved cents/kWh rider increment or decrement
is added to the base energy rate.

NC Sixth Revised Leaf No. 601
Effective for service rendered on and after January 1, 2026
"""

JAA_TEXT = """\
Duke Energy Progress, LLC NC Third Revised Leaf No. 602
(North Carolina Only) Superseding NC Second Revised Leaf No. 602
JOINT AGENCY ASSET RIDER JAA
MONTHLY RATE
The incremental rider for each rate class as follows:
Rate Class Applicable Schedule(s) Incremental Rate*
Non-Demand Rate Class (dollars per kilowatt-hour)
Residential RES, R-TOUD, R-TOU, 0.00464
R-TOU-CPP
Small General Service SGS, SGS-TOUE, SGS-TOU-CPP 0.00223
Outdoor Lighting Service ALS, SLS, SLR, SFLS 0.01389
Demand Rate Classes (dollars per kilowatt)
Medium General Service MGS, GS-TES, APH-TES, MGS- 0.92
TOU**
Large General Service LGS, LGS-TOU**, LGS-HLF 3.03
NC Third Revised Leaf No. 602
Effective for bills rendered on and after December 1, 2025
"""

CAR_TEXT = """\
Duke Energy Progress, LLC NC Second Revised Leaf No. 611
(North Carolina Only) Superseding NC First Revised Leaf No. 611
CUSTOMER ASSISTANCE RECOVERY RIDER CAR
MONTHLY RATE
Customer Assistance Program Billing Rate
($/kWh for Residential;
Rate Class $/bill for all General Service)
Residential $0.00098
Applicable to Schedules:
RES, R-TOUD, R-TOU, &
R-TOU-CPP
Small General Service $1.12
Applicable to Schedules:
SGS, SGS-TOUE, SGS-TOU-
CLR, SGS-TOU-CPP, TFS, &
TSS
Medium General Service $1.12
Applicable to Schedules:
MGS, MGS-TOU, SI, CH-
TOUE, GS-TES, APH-TES,
& CSE
Large General Service $1.12
Applicable to Schedules:
LGS, LGS-TOU, LGS-RTP,
HP, & LGS-HLF
NC Second Revised Leaf No. 611
Effective for services rendered on and after January 1, 2026
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _parse(text: str, family_key: str = "nc-progress-leaf-500"):
    return parse_nc_progress_leaf(text, version_id=0, family_key=family_key, document_id=None)


# ---------------------------------------------------------------------------
# Version record tests
# ---------------------------------------------------------------------------

class TestVersionRecord:
    def test_res_revision_label(self):
        version, _, _ = _parse(RES_TEXT)
        assert version.revision_label == "NC Second Revised Leaf No. 500"

    def test_res_supersedes_label(self):
        version, _, _ = _parse(RES_TEXT)
        assert version.supersedes_label == "NC First Revised Leaf No. 500"

    def test_res_effective_start(self):
        version, _, _ = _parse(RES_TEXT)
        assert version.effective_start == "2025-10-01"

    def test_res_source_type(self):
        version, _, _ = _parse(RES_TEXT)
        assert version.source_type == "utility_current"

    def test_rider_effective_start(self):
        version, _, _ = _parse(RIDER_TEXT, "nc-progress-leaf-601")
        assert version.effective_start == "2026-01-01"

    def test_rider_revision_label(self):
        version, _, _ = _parse(RIDER_TEXT, "nc-progress-leaf-601")
        assert version.revision_label == "NC Sixth Revised Leaf No. 601"


# ---------------------------------------------------------------------------
# RES charge tests (seasonal block rates)
# ---------------------------------------------------------------------------

class TestResCharges:
    def setup_method(self):
        _, self.charges, _ = _parse(RES_TEXT)

    def _by_label(self, label: str):
        return [c for c in self.charges if c.charge_label == label]

    def _fixed_customer_charges(self):
        return [
            c
            for c in self.charges
            if c.charge_type == "fixed" and "Customer" in (c.charge_label or "")
        ]

    def test_customer_charge_present(self):
        fixed = self._fixed_customer_charges()
        assert len(fixed) == 1
        assert fixed[0].rate_value == 14.0
        assert fixed[0].rate_unit == "$/month"

    def test_three_phase_surcharge(self):
        three_phase = [c for c in self.charges if "Three-Phase" in (c.charge_label or "")]
        assert len(three_phase) == 1
        assert three_phase[0].rate_value == 9.0

    def test_summer_energy_charge(self):
        summer = [
            c
            for c in self.charges
            if c.season == "summer" and c.charge_type == "energy_block"
        ]
        assert len(summer) == 1
        assert summer[0].rate_value == pytest.approx(0.12623)
        assert summer[0].tier_min == 0.0
        assert summer[0].tier_max is None

    def test_winter_first_block(self):
        winter_first = [
            c for c in self.charges
            if c.season == "winter" and c.charge_type == "energy_block" and c.tier_max == 800.0
        ]
        assert len(winter_first) == 1
        assert winter_first[0].rate_value == pytest.approx(0.12623)

    def test_winter_additional_block(self):
        winter_add = [
            c for c in self.charges
            if c.season == "winter" and c.charge_type == "energy_block" and c.tier_min == 800.0
        ]
        assert len(winter_add) == 1
        assert winter_add[0].rate_value == pytest.approx(0.11623)
        assert winter_add[0].tier_max is None

    def test_rate_unit_is_dollars_per_kwh(self):
        energy_charges = [c for c in self.charges if c.charge_type == "energy_block"]
        for c in energy_charges:
            assert c.rate_unit == "$/kWh", f"Bad unit: {c.charge_label}"


# ---------------------------------------------------------------------------
# TOU charge tests
# ---------------------------------------------------------------------------

class TestTouCharges:
    def setup_method(self):
        _, self.charges, _ = _parse(TOU_TEXT, "nc-progress-leaf-502")

    def test_on_peak_charge(self):
        on_peak = [c for c in self.charges if c.tou_period == "on_peak"]
        assert len(on_peak) == 1
        assert on_peak[0].rate_value == pytest.approx(0.29905)
        assert on_peak[0].charge_type == "tou_energy"

    def test_off_peak_charge(self):
        off_peak = [c for c in self.charges if c.tou_period == "off_peak"]
        assert len(off_peak) == 1
        assert off_peak[0].rate_value == pytest.approx(0.11321)

    def test_discount_charge(self):
        discount = [c for c in self.charges if c.tou_period == "discount"]
        assert len(discount) == 1
        assert discount[0].rate_value == pytest.approx(0.07372)

    def test_no_duplicate_tou_charges(self):
        # Two-column layout merges to single line; dedup should prevent doubles
        on_peak = [c for c in self.charges if c.tou_period == "on_peak"]
        assert len(on_peak) == 1

    def test_customer_charge_from_multiline(self):
        fixed = [
            c
            for c in self.charges
            if c.charge_type == "fixed" and "Customer" in (c.charge_label or "")
        ]
        assert len(fixed) == 1
        assert fixed[0].rate_value == 14.0

    def test_qualified_demand_labels_are_preserved(self):
        _, charges, _ = _parse(RTOUD_QUALIFIED_DEMAND_TEXT, "nc-progress-leaf-501")

        by_label = {c.charge_label: c for c in charges if c.charge_type == "demand"}

        assert by_label["Demand Charge - On-Peak"].rate_value == pytest.approx(1.95)
        assert by_label["Demand Charge - On-Peak"].rate_unit == "$/kW"
        assert by_label["Demand Charge - Maximum"].rate_value == pytest.approx(3.82)
        assert by_label["Demand Charge - Maximum"].rate_unit == "$/kW"

    def test_legacy_tou_rates_without_cent_symbol_are_extracted(self):
        _, charges, _ = _parse(RTOUD_LEGACY_TOU_NO_CENT_TEXT, "nc-progress-leaf-501")

        by_period = {c.tou_period: c for c in charges if c.charge_type == "tou_energy"}

        assert by_period["on_peak"].rate_value == pytest.approx(0.06948)
        assert by_period["off_peak"].rate_value == pytest.approx(0.05541)


# ---------------------------------------------------------------------------
# SGS demand charge tests
# ---------------------------------------------------------------------------

class TestSgsCharges:
    def setup_method(self):
        _, self.charges, _ = _parse(SGS_TEXT, "nc-progress-leaf-520")

    def test_demand_charge(self):
        demand = [c for c in self.charges if c.charge_type == "demand"]
        assert len(demand) == 1
        assert demand[0].rate_value == 10.50
        assert demand[0].rate_unit == "$/kW"

    def test_energy_charge(self):
        energy = [c for c in self.charges if c.charge_type == "energy_block"]
        assert len(energy) == 1
        assert energy[0].rate_value == pytest.approx(0.06543)


# ---------------------------------------------------------------------------
# Rider applicability tests
# ---------------------------------------------------------------------------

class TestRiderApplicability:
    def setup_method(self):
        _, _, self.riders = _parse(RES_TEXT)

    def test_leaf_601_linked(self):
        keys = [r.rider_family_key for r in self.riders]
        assert "nc-progress-leaf-601" in keys

    def test_leaf_602_linked(self):
        keys = [r.rider_family_key for r in self.riders]
        assert "nc-progress-leaf-602" in keys

    def test_storm_leaf_607_linked(self):
        keys = [r.rider_family_key for r in self.riders]
        assert "nc-progress-leaf-607" in keys

    def test_applies_to_family_key(self):
        for rider in self.riders:
            assert rider.applies_to_family_key == "nc-progress-leaf-500"

    def test_no_duplicate_riders(self):
        # Leaf 607 is referenced twice (rider list + storm section) — should appear once
        count_607 = sum(1 for r in self.riders if r.rider_family_key == "nc-progress-leaf-607")
        assert count_607 == 1

    def test_source_type(self):
        for rider in self.riders:
            assert rider.source_type == "tariff_text"

    def test_mandatory_true(self):
        for rider in self.riders:
            assert rider.mandatory is True


class TestRiderParsing:
    def test_jaa_extracts_kwh_kw_and_lighting_rows(self):
        _, charges, _ = _parse(JAA_TEXT, "nc-progress-leaf-602")
        by_class_and_unit = {(c.customer_class, c.rate_unit): c.rate_value for c in charges}
        assert by_class_and_unit[("residential", "$/kWh")] == pytest.approx(0.00464)
        assert by_class_and_unit[("lighting", "$/kWh")] == pytest.approx(0.01389)
        assert by_class_and_unit[("commercial_medium", "$/kW")] == pytest.approx(0.92)
        assert by_class_and_unit[("commercial_large", "$/kW")] == pytest.approx(3.03)

    def test_car_extracts_residential_kwh_and_general_service_bill_rows(self):
        _, charges, _ = _parse(CAR_TEXT, "nc-progress-leaf-611")
        rows = {(c.customer_class, c.rate_unit): c.rate_value for c in charges}
        assert rows[("residential", "$/kWh")] == pytest.approx(0.00098)
        assert rows[("commercial_small", "$/bill")] == pytest.approx(1.12)
        assert rows[("commercial_medium", "$/bill")] == pytest.approx(1.12)
        assert rows[("commercial_large", "$/bill")] == pytest.approx(1.12)
