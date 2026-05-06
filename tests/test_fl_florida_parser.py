from __future__ import annotations

from duke_rates.parse.fl_florida import parse_fl_florida_sheet
from duke_rates.parse.nc_carolinas import parse_nc_carolinas_leaf

FL_SERVICE_CHARGES_TEXT = """\
SECTION NO. VI
TWENTY-SECOND REVISED SHEET NO. 6.110
CANCELS TWENTY-FIRST REVISED SHEET NO. 6.110
RATE SCHEDULES SC-1
SERVICE CHARGES
1. A charge of $58.00 will be made for initial establishment of service to a premise.
3. A charge of $4.00 will be made for each subsequent re-establishment of service to said premise.
Late Payment Charge:
Charges are subject to a Late Payment Charge of the greater of $5.00 or 1.5%.
Investigation of Unauthorized Use Charge:
The charge shall be $200.00 for residential customers and $1,000.00 for all other customers.
EFFECTIVE: January 1, 2025
"""

FL_GSLM_TEXT = """\
SECTION NO. VI
FIFTEENTH REVISED SHEET NO. 6.220 CANCELS
FOURTEENTH REVISED SHEET NO. 6.220
RATE SCHEDULE GSLM-1
GENERAL SERVICE - LOAD MANAGEMENT
Rate Per Month:
LOAD MANAGEMENT MONTHLY CREDIT AMOUNT
Electric Space Cooling3 A $ 0.26 Per kW March thru November
Electric Space Cooling3 B $ 0.56 Per kW March thru November
EFFECTIVE: January 1, 2025
"""

NC_CAROLINAS_ORIGINAL_TEXT = """\
Duke Energy Carolinas, LLC NC Original Leaf No. 242
(North Carolina Only) Superseding NC Original Leaf No. 241
RIDER CEI
CLEAN ENERGY IMPACT RIDER
Effective for service rendered on and after January 1, 2026
"""


def test_fl_service_charges_extract_fixed_rows() -> None:
    _, charges, _ = parse_fl_florida_sheet(
        FL_SERVICE_CHARGES_TEXT,
        version_id=0,
        family_key="fl-florida-pe-SC-1",
    )
    assert len(charges) == 5
    assert all(charge.rate_unit == "$/bill" for charge in charges)


def test_fl_gslm_extracts_credit_rows() -> None:
    _, charges, _ = parse_fl_florida_sheet(
        FL_GSLM_TEXT,
        version_id=0,
        family_key="fl-florida-pe-GSLM-1",
    )
    labels = {charge.charge_label: charge.rate_value for charge in charges}
    assert labels["Load Management Credit (Electric Space Cooling3 A)"] == 0.26
    assert labels["Load Management Credit (Electric Space Cooling3 B)"] == 0.56


def test_nc_carolinas_preserves_original_revision_label() -> None:
    version, _, _ = parse_nc_carolinas_leaf(
        NC_CAROLINAS_ORIGINAL_TEXT,
        version_id=0,
        family_key="nc-carolinas-rider-CEI",
    )
    assert version.revision_label == "NC Original Leaf No. 242"
    assert version.supersedes_label == "NC Original Leaf No. 241"
