import pytest
import json

from datetime import UTC, datetime

import duke_rates.historical.ncuc.pipeline.parser_profiles as parser_profiles_module
from duke_rates.db.sqlite import connect
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.historical.ncuc.pipeline.rate_extractor import ExtractedCharge
from duke_rates.historical.ncuc.pipeline.parser_profiles import (
    CarolinasCustomerAssistanceRecoveryProfile,
    CarolinasCurrentLeafBridgeProfile,
    CarolinasEnergyEfficiencyRiderProfile,
    CarolinasEconomicDevelopmentRiderProfile,
    CarolinasGeneralServiceScheduleProfile,
    CarolinasInterruptibleServiceRiderProfile,
    CarolinasNuclearProductionTaxCreditsProfile,
    CarolinasLightingScheduleProfile,
    CarolinasNetMeteringRiderProfile,
    CarolinasRiderAdjustmentMatrixProfile,
    CarolinasResidentialFlatProfile,
    CarolinasResidentialTouProfile,
    CarolinasScheduleBridgeProfile,
    CarolinasSingleValueRiderProfile,
    CarolinasSmallCustomerGeneratorProfile,
    CarolinasSolarChoiceRiderProfile,
    GreenSourceAdvantageRiderProfile,
    HistoricalRateParserRegistry,
    ProgressBillingAdjustmentsProfile,
    ProgressCustomerAssistanceRecoveryProfile,
    ProgressCurrentLeafBridgeProfile,
    ProgressDemandResponseAutomationProfile,
    ProgressEnergywiseBusinessProfile,
    ProgressGreenPowerProgramProfile,
    ProgressMeterRelatedOptionalProgramsProfile,
    ProgressPowerPairPilotProfile,
    ProgressResidentialLoadControlProfile,
    ProgressResidentialFlatProfile,
    ProgressManagementEnergyEfficiencyCostRecoveryRiderProfile,
    ProgressComplianceReportAndCostRecoveryRiderProfile,
    ProgressRecoveryRiderProfile,
    ProgressRiderAdjustmentMatrixProfile,
    ProgressSpecialtyRiderProfile,
    ProgressStandbyServiceProfile,
    ProgressStormSecuritizationProfile,
    ProgressSingleValueRiderProfile,
    ProgressResidentialTouProfile,
    ProgressSunSenseSolarRebateProfile,
)


TOU_SEASONAL_TEXT = """\
Progress Energy Carolinas
Residential All-Energy Time-of-Use
Schedule R-TOUE

MONTHLY RATE

Service used during May through September:
Basic Customer Charge:
$14.00
29.905¢ per On-Peak kWh
11.321¢ per Off-Peak kWh
7.372¢ per Discount kWh

Service used during October through April:
Basic Customer Charge:
$14.00
21.952¢ per On-Peak kWh
11.000¢ per Off-Peak kWh
8.274¢ per Discount kWh
"""

TOU_DEMAND_TEXT = """\
Progress Energy Carolinas
Residential Service Time-of-Use Schedule R-TOUD

MONTHLY RATE

Service used during May through September:
Basic Customer Charge:
$14.00
21.952¢ per On-Peak kWh
11.000¢ per Off-Peak kWh
8.274¢ per Discount kWh
On-Peak Demand Charge: $4.35 per kW
Base Demand Charge: $1.80 per kW
"""

RIDER_SUMMARY_TEXT = """
Duke Energy Progress, LLC
NC Eighth Revised Leaf No. 600
SUMMARY OF RIDER ADJUSTMENTS
Effective for service rendered on and after January 1, 2026

Residential Service Schedules
cents
/kWh
Effective
Date
Annual Billing Adjustments Rider BA
Fuel and Fuel-Related Adjustment Rate 0.262 12/1/25
Fuel and Fuel-Related Adjustment Experience Modification Factor
(EMF) 0.518 12/1/25
Demand Side Management DSM & EE Rate 0.769 1/1/26
Annual Billing Adjustments Rider BA - Net Adjustment 1.549
EDIT-4 Rider -0.249 10/1/23
Joint Agency Asset Rider JAA 0.464 12/1/25
Competitive Procurement of Renewable Energy Rider CPRE 0.001 12/1/25
Customer Assistance Recovery Rider CAR 0.098 1/1/26
Residential Decoupling Mechanism Rider RDM 0.232 4/1/25
Earnings Sharing Mechanism Rider ESM 0.000 4/1/25
Performance Incentive Mechanism Rider PIM 0.002 4/1/25
TOTAL cents/kWh 2.097
"""

PROGRESS_BA_TEXT = """\
Duke Energy Progress, LLC NC Sixth Revised Leaf No. 601
(North Carolina Only) Superseding NC Fifth Revised Leaf No. 601
ANNUAL BILLING ADJUSTMENTS
RIDER BA
APPLICABILITY – RATES INCLUDED IN TARIFF CHARGES
The rates shown below are not included in the MONTHLY RATE provision in each schedule identified in
the table below:
Billing Adjustment Factors (¢/kWh)*
Fuel and Fuel-
Net
Related
Rate Class DSM and EE Adjustment Adjustment
Adjustment
Rate(1) EMF(2) Rate(3) EMF(4)
Residential .262 0.518 0.663 0.106 1.549
Applicable to Schedules:
RES, R-TOUD, R-TOU, &
R-TOU-CPP
Small General Service (0.123) 0.116 0.417 (EE Only) (0.221) (EE Only) 0.233
Applicable to Schedules: 0.049 (DSM Only) (0.005) (DSM
SGS, SGS-TOUE, SGS- Only)
TOU-CLR, SGS-TOU-CPP,
TSF & TSS
Medium General Service .211 0.240 0.417 (EE Only) (0.221) (EE Only) 0.691
Applicable to Schedules: 0.049 (DSM Only) (0.005) (DSM
MGS, MGS-TOU, SI, CH- Only)
TOUE, GS-TES, APH-TES
Large General Service .361 0.353 0.417 (EE Only) (0.221) (EE Only) 0.954
Applicable to Schedules: 0.049 (DSM Only) (0.005) (DSM
LGS, LGS-TOU, LGS-RTP, Only)
HP, LGS-HLF
Lighting (0.490) 0.693 0.012 (EE Only) (0.001) (EE Only) 0.214
Applicable to Schedules: 0.000 (DSM Only) 0.000 (DSM Only)
ALS, SLS, SLR & SFLS
"""

PROGRESS_BA_NOTICE_TEXT = """\
NOTICE TO CUSTOMERS OF CHANGE IN RATES
Annual Billing Adjustments Rider BA
The rate changes associated with DEP's DSM and EE programs followed Commission review
of DEP's DSM/EE expenses and proposed utility incentives during the test period January 1,
2015, through December 31, 2015, as well as DEP's estimates for the calendar year 2017
rate period. The net changes in the DSM and EE rates from the rates approved in January
2016 were as follows: Residential - an increase of 0.155 cents per kilowatt-hour (kWh);
Small, Medium, and Large General Service (EE component) - an increase of 0.054 cents
per kWh; Small, Medium, and Large General Service (DSM component) - an increase of
0.012 cents per kWh; and Lighting - a decrease of 0.005 cents per kWh.
"""

PROGRESS_BA_NOTICE_2021_TEXT = """\
NOTICE TO CUSTOMERS OF CHANGE IN RATES
Annual Billing Adjustments Rider BA
The rate changes associated with DEP's DSM and EE programs followed Commission review
of DEP's DSM/EE expenses and proposed utility incentives during the test period January 1,
2019 through December 31, 2019, as well as DEP's estimates for the calendar year 2021
rate period. The net changes in the current DSM and EE rates compared to the rates
approved effective January 2021 are as follows: Residential rates will increase 0.100 cents
per kilowatt hour (kWh); Small, Medium, and Large General Service (EE component) will
decrease (0.042) cents per kWh; Small, Medium, and Large General Service (DSM
component) will increase 0.006 cents per kWh; and Lighting will increase .037 cents per kWh.
"""

PROGRESS_ESM_TEXT = """\
Duke Energy Progress, LLC NC Original Leaf No. 609
(North Carolina Only)
EARNINGS SHARING MECHANISM
RIDER ESM
APPLICABILITY
MONTHLY RATE
The approved decremental rate, including revenue-related taxes and regulatory fees,
is 0.002¢ per kilowatt-hour.
NC Original Leaf No. 609
Effective for service rendered on and after April 1, 2025
NCUC Docket No. E-2, Sub 1300, Order dated March 12, 2025
Page 1 of 1
"""

PROGRESS_ESM_TEXT_LINEBREAK_RATE = """\
Duke Energy Progress, LLC NC First Revised Leaf No. 609
(North Carolina Only) Superseding NC Original Leaf No.609
EARNINGS SHARING MECHANISM
RIDER ESM
APPLICABILITY
This Rider is applicable to all service supplied under the Company's rate schedules.
MONTHLY RATE
The approved decremental rate, including revenue-related taxes and regulatory fees, is 0.000¢ per
kilowatt-hour.
NC First Revised Leaf No. 609
Effective for service rendered on and after April 1, 2025
NCUC Docket No. E-2, Sub 1300, Order dated March 12, 2025
Page 1 of 1
"""

PROGRESS_RECOVERY_RIDER_TEXT = """\
Duke Energy Progress, LLC
NC First Revised Leaf No. 79
RECOVERY RIDER
Effective for service rendered on and after January 1, 2025
MONTHLY RATE
The approved recovery rider rate is 0.012 cents per kilowatt-hour.
"""

PROGRESS_MEECR_TEXT = """\
Duke Energy Progress, LLC
NC First Revised Leaf No. 81
MANAGEMENT AND ENERGY EFFICIENCY COST RECOVERY RIDER
Effective for service rendered on and after January 1, 2025
MONTHLY RATE
The approved cost recovery rider rate is 0.014 cents per kilowatt-hour.
"""

PROGRESS_CRCR_TEXT = """\
Duke Energy Progress, LLC
NC First Revised Leaf No. 82
COMPLIANCE REPORT AND COST RECOVERY RIDER
Effective for service rendered on and after January 1, 2025
MONTHLY RATE
The approved compliance cost recovery rider rate is 0.016 cents per kilowatt-hour.
"""

PROGRESS_R_TOUD_TEXT = """\
Duke Energy Progress, LLC
NC Fifth Revised Leaf No. 501
Schedule R-TOUD
Residential Service - Time-of-Use Demand
MONTHLY RATE
Basic Customer Charge:
$14.00
The bill computed for single-phase service plus $9.00
15.638¢ per On-Peak kWh
6.633¢ per Off-Peak kWh
4.347¢ per Discount kWh
"""

PROGRESS_SGS_TEXT = """\
Duke Energy Progress, LLC
NC Second Revised Leaf No. 520
Schedule SGS
Small General Service
MONTHLY RATE
The bill computed for single-phase service plus $9.00
13.188¢ per kWh for the first 300 kWh
"""

PROGRESS_HP_TEXT = """\
Duke Energy Progress, LLC
NC Fourth Revised Leaf No. 535
Schedule HP
High Power Service
MONTHLY RATE
Demand Charge: $4.53 per kW
Demand Charge: $3.33 per kW
"""

PROGRESS_PS_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 674
Rider PS
Partial Requirements Service
MONTHLY RATE
Demand Charge: $0.10 per kW
Demand Charge: $4.00 per kW
Demand Charge: $3.00 per kW
Demand Charge: $5.00 per kW
"""

PROGRESS_NFS_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 654
Rider NFS
Supplementary and Non-Firm Standby Service
MONTHLY BILLING
3. Non-Firm Standby Notification Customer Charge:
$55.00
4. Non-Firm Standby Service Delivery Charge:
Per kWh of Non-Firm Standby Service Usage for Customer served from:
Transmission System (voltage of 69 kV or higher) $0.00641/kWh
Distribution System (voltage below 69 kV) $0.01133/kWh
0.6 cents per kWh of Incremental Load for the Incentive Margin
"""

PROGRESS_LLC_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 655
Rider LLC
Large Load Curtailable Rider
MONTHLY RATE
A. Customer Charge: $55.00
B. Credit = Discount x Demonstrated Curtailable Demand, but not less than zero ($0)
where: discount = $4.90 per kW
For use of premium demand during a Level 1 capacity curtailment, Customer shall pay to Company
$2.40 per kilowatt-hour for all kilowatt-hours attributable to premium demand.
Customer shall pay to Company $45.00 for each kW of premium demand for each and every
Level 2 curtailable period during the billing period.
"""

PROGRESS_NSC_TEXT = """\
Duke Energy Progress, LLC
NC First Revised Leaf No. 668
Rider NSC
Non-Residential Solar Choice
RATE
Monthly Credit for Net Excess Energy, per kWh $0.0390
"""

PROGRESS_NMB_TEXT = """\
Duke Energy Progress, LLC
NC Third Revised Leaf No. 605
Rider NMB
Net Metering Bridge
MONTHLY RATE
Net Excess Energy Credit per month, per kWh 3.94¢
Non-Bypassable Charge per month, per Nameplate Capacity kW $1.13
There shall be a monthly minimum bill of $28
CUSTOMER AND DISTRIBUTION ENERGY CHARGES
All Energy per month, per kWh 4.493¢
"""

PROGRESS_RSC_TEXT = """\
Duke Energy Progress, LLC
NC Third Revised Leaf No. 670
Rider RSC
Residential Solar Choice
MONTHLY RATE
Net Excess Energy Credit per month, per kWh 3.94¢
Non-Bypassable Charge per month, per Nameplate Capacity kW $1.13
Grid Access Fee per month, per Nameplate Capacity kW above 15 kW $1.50
There shall be a monthly minimum bill of $28
Schedule R-TOU-CPP
a. On-Peak Energy per month, per kWh 6.248¢
b. Off-Peak Energy per month, per kWh 4.134¢
c. Discount Energy per month, per kWh 3.736¢
"""

PROGRESS_EWB_TEXT = """\
Duke Energy Progress, LLC
GP-5
ENERGYWISE FOR BUSINESS RIDER EWB-1
CONTROL CREDITS
Summer Control Option:
1. 30% summer cycling level - $50 per load control device
2. 50% summer cycling level - $85 per load control device
3. 75% summer cycling level - $135 per load control device
Winter Control:
additional $25 per thermostat
EnergyWise Business Bring Your Own kW Option:
the compensation will be $30 per kW reduced during the events.
"""

PROGRESS_DRA_TEXT = """\
Duke Energy Progress, LLC
RIDER DRA-7
DEMAND RESPONSE AUTOMATION
MONTHLY RATE
Monthly Availability Credit = $3.25/kW x Summer Contracted Curtailable Demand
Event Performance Credit = $6.00/kW x Sum of Event Demand Reductions in Current Month
PARTICIPATION INCENTIVE
Customer will receive a one-time Participant Incentive, in the amount of $50.00/kW.
"""

PROGRESS_POWERPAIR_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 770
POWERPAIRSM SOLAR AND BATTERY INSTALLATION PROGRAM PPSB (PILOT)
The participating PowerPairSM Customer shall receive a one-time incentive payment of $0.36 per watt
for eligible solar panel installation and $240-$400 per kilowatt hour for eligible battery installation.
"""

PROGRESS_PPS_TEXT = """\
Duke Energy Progress, LLC
NC First Revised Leaf No. 660
PREMIER POWER SERVICE
RIDER PPS
MONTHLY RATE
Monthly Service Payment = Capital Cost + Expenses
Capital Cost equals a carrying cost, if applicable, times the Customer's portion of levelized plant investment.
"""

PROGRESS_CEI_TEXT = """\
Duke Energy Progress, LLC NC Original Leaf No. 672
(North Carolina Only)
CLEAN ENERGY IMPACT
RIDER CEI
AVAILABILITY
This Rider provides eligible Duke Energy Progress, LLC customers the option to purchase
Clean Energy Environmental Attributes ("CEEAs") under the Clean Energy Impact Program.
Each customer may contract with the Company for the purchase of a block of CEEAs to be billed monthly.
Available CEEAs will be based on expected kilowatt-hour production from Renewable Energy Resources.
"""

PROGRESS_SSR_TEXT = """\
Duke Energy Progress, Inc.
RESIDENTIAL SERVICE (EXPERIMENTAL)
SUNSENSE SOLAR REBATE RIDER SSR-3
PARTICIPATION PAYMENTS
Upon receipt of the Certificate of Completion, Customer shall receive a one-time participation payment of
$250 per kilowatt times the alternating current (AC) capacity rating of Customer's solar photo-voltaic electric generating system.
MONTHLY RATE
SSR Credit=
$4.50 per kilowatt times the alternating current (AC) capacity rating of the generating system
Customer shall be required to refund the participation payment with an early termination charge equal to $4.17 per kilowatt based on the AC capacity rating of the generating system.
"""

PROGRESS_MROP_TEXT = """\
Duke Energy Progress, LLC
RIDER MROP-13
METER-RELATED OPTIONAL PROGRAMS RIDER MROP-13
The Energy Profiler Online (EPO) program is available to qualifying customers.
Monthly Rate for TotalMeter
Option 1: Customer-supplied suitable telephone communications line
$ 3.00
Option 2: Company-supplied wireless telephone communications circuit
$13.20
Charge for Customer-requested termination of TotalMeter
$50.00
Monthly Rate for EPO
Rate for totalized meter data only (updated monthly)
$20.00 per totalized account
Rate for meter data per individual meter (updated each business day) $20.00 per meter
Set-up fee per meter
$85.00
Set-up fee for totalized meter data only
$85.00
MANUALLY READ METERING (MRM)
Initial Set-up Fee
$170.00
Monthly Rate For MRM
$14.75
Early Termination Charge (Prior to 12 consecutive months of service only)
$50.00
Monthly Rate for non-standard meter with interval data capability
$0.33 per month
"""

PROGRESS_LC_TEXT = """\
Duke Energy Progress, LLC
RESIDENTIAL SERVICE - LOAD CONTROL
RIDER LC-9
PAYMENT OF INCENTIVES
Initial Incentive
For Participants Company-provided HVAC Load Control Device(s) - One Time $25 per residence
For Winter-Focused Participants with Customer-provided eligible Thermostat(s) - One Time $90 per residence through December 31, 2020 and One Time $75 per residence thereafter
For Summer-Only Participants with Customer-provided eligible Thermostat(s) - One Time $75 per residence
Annual Incentive
Qualified Summer-Only Cooling System Controls - $25 per residence
Qualified Winter-Focused System Controls - $25 per residence
"""

PROGRESS_SS_TEXT = """\
Duke Energy Progress, LLC
SUPPLEMENTARY AND FIRM STANDBY SERVICE RIDER SS
MONTHLY BILLING
Generation Reservation Charge
Applicable to customers on non-time-of-use demand rate schedules with less than 60% planning capacity factor - $0.79/kW of Standby Service Contract Demand.
Applicable to customers on time-of-use demand rate schedules with less than 60% planning capacity factor - $0.00/kW of Standby Service Contract Demand.
Applicable to customers with 60% or greater planning capacity factor - $0.79/kW of Standby Service Contract Demand.
Standby Service Delivery Charge
Transmission System (voltage of 69 kV or higher) $2.76/kW
Distribution System (voltage below 69 kV) $5.18/kW
0.6 cents per kWh of Incremental Load for the Incentive Margin
"""

PROGRESS_GP_TEXT = """\
Duke Energy Progress, Inc.
NC GREENPOWER PROGRAM
RIDER GP-1B2
MONTHLY RATE
In addition to all other charges stated in the Monthly Rate of the Schedule with which this Rider is used,
the following charge shall also apply to each block Customer purchases:
$4.00 per block
"""

PROGRESS_REN_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 643
NC GREENPOWER PROGRAM
RENEWABLE RIDER REN
MONTHLY RATE
In addition to all other charges stated in the Monthly Rate of the Schedule with which this Rider is used,
the following charge shall also apply to each block Customer purchases:
$2.50 per block
The minimum monthly charge shall be a charge for 40 blocks of electricity.
"""

PROGRESS_CAR_TEXT = """\
Duke Energy Progress, LLC
NC Second Revised Leaf No. 611
CUSTOMER ASSISTANCE RECOVERY RIDER CAR
APPLICABILITY
MONTHLY RATE
The incremental rate for the appropriate rate class, including revenue-related taxes and regulatory fees, shall be as shown in the following table:
Rate Class
Customer Assistance Program Billing Rate
($/kWh for Residential; $/bill for all General Service)
Residential
$0.00098
Small General Service
$1.12
Medium General Service
$1.12
Large General Service
$1.12
"""

PROGRESS_STS_TEXT = """\
Duke Energy Progress, LLC
NC Original Leaf No. 613
STORM SECURITIZATION
RIDER STS-2
MONTHLY RATE
The incremental rate for the appropriate class, including revenue-related taxes and regulatory fees, shall be shown as in the following table:
Rate Class
Applicable Schedules
Billing Rate
(¢/kWh)
Residential
0.166
Small General Service
0.146
Medium General Service
0.040
Large General Service
0.015
Lighting
0.024
"""

CAROLINAS_RS_TEXT = """
Duke Energy Carolinas, LLC
SCHEDULE RS
Effective for service rendered on and after January 1, 2026
TYPE OF SERVICE
The Company will furnish 60 Hertz service through one meter.
RATE
I.
Basic Customer Charge per month
$ 14.00
II.
Energy Charge per month, per kWh*
12.2603¢
Leaf No. 60
Fuel Cost Adjustment Rider
"""

CAROLINAS_RIDER_SUMMARY_TEXT = """
Duke Energy Carolinas, LLC
NC Sixty-First Revised Leaf No. 99
SUMMARY OF RIDER ADJUSTMENTS
The following is a summary of Rider Adjustments that must be added to the bill.

Residential Schedules RS, RE, ES, RT, RSTC, RETC cents/kWh Effective Date
Fuel Cost Adjustment Rider 1.2682 1/15/24
Energy Efficiency Rider 0.3775 1/1/24
Existing DSM Program Costs Adjustment Rider -0.0027 7/1/24
BPM Prospective Rider -0.0128 7/1/24
BPM True-Up Rider -0.0039 7/1/24
CPRE Rider 0.0143 9/1/23
EDIT-4 Rider -0.5081 1/15/24
Regulatory Asset and Liability Rider -0.0009 1/15/24
Customer Assistance Recovery Rider 0.1246 1/15/24
Residential Decoupling Mechanism Rider 0.0000 1/15/24
Earnings Sharing Mechanism Rider 0.0000 1/15/24
Performance Incentive Mechanism Rider 0.0000 1/15/24
TOTAL cents/kWh 1.2562
"""

CAROLINAS_CAR_TEXT = """\
Duke Energy Carolinas, LLC
NC First Revised Leaf No. 144
RIDER CAR
CUSTOMER ASSISTANCE RECOVERY
Effective for service on and after January 1, 2025
MONTHLY RATE
Rate Class Applicable Schedules Billing Rate
Residential RS, RE, ES, RT, RSTC, RETC $0.000459
General Service SGS, BC, LGS, TS, OPT-V, HP, PG, S, SGSTC, HLF $0.33
Industrial I, OPT-V, HP, PG, HLF $0.33
"""

CAROLINAS_NPTC_TEXT = """\
Duke Energy Carolinas, LLC
NC Original Leaf No. 194
RIDER NPTC
NUCLEAR PRODUCTION TAX CREDITS
Effective for service on and after January 1, 2025
MONTHLY RATE
The current approved decremental rate, including regulatory fees, is (0.0834¢) per kilowatt-hour.
"""

CAROLINAS_EDPR_TEXT = """\
Duke Energy Carolinas, LLC
North Carolina Fourth Revised Leaf No. 64
EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)
Effective for service rendered on and after July 1, 2011
EXISTING DSM PROGRAM COST ADJUSTMENT FACTOR
A rider adjustment will be applied to the energy charges of all NC Retail rate schedules.
Existing DSM Program Costs Rate Adjustment per kilowatt hour
(0.0037) C/kwh
"""

CAROLINAS_BPM_TRUEUP_TEXT = """\
Duke Energy Carolinas, LLC
North Carolina Original Leaf No. 106
BPM TRUE-UP RIDER (NC)
Effective September 25.2013
BPM Net Revenues and Non-Firm Point-to Point Transmission Revenues Rate Adjustment
0.0659 c/kWh
Total Adjustment
0.0682 c/kWh
"""

CAROLINAS_BPM_TRUEUP_OCR_TEXT = """\
Duke Energy Carolinas, LLC
BPM TRUE-UP RIDER (NC)
BPM Net Revenues and Non-Firm Point-to Point Transmission Revenues Rate Adjustment
Gross Receipts Tax and Regulatory Fee Multiplier
Total Adjustment
Effective
September 25.2013
-0.0709 0/kWh
0.0659 (S/kWh
X
1.034554
0.0682 cVkWh
"""

CAROLINAS_HLF_TEXT = """\
Duke Energy Carolinas, LLC
NC Second Revised Leaf No. 31
SCHEDULE HLF
HIGH LOAD FACTOR
Effective for service rendered on and after January 1, 2026
RATE
Basic Customer Charge per month
$34.00
Demand Charge per month, per kW
$1.25
Energy Charge per month, per kWh
2.1775¢
"""

CAROLINAS_PG_TEXT = """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 55
SCHEDULE PG (NC)
PARALLEL GENERATION
RATE
Basic Customer Charge per month
$14.00
On-Peak Demand Charge per month, per kW
$8.21
Energy Charge per month, per kWh
4.9993¢
"""

CAROLINAS_LGS_TEXT = """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 29
SCHEDULE LGS (NC)
LARGE GENERAL SERVICE
RATE
Basic Customer Charge per month
$17.90
Demand Charge per month, per kW
$11.24
Energy Charge per month, per kWh
5.4321¢
"""

CAROLINAS_NMB_TEXT = """\
Duke Energy Carolinas, LLC
NC Fourth Revised Leaf No. 143
RIDER NMB
NET METERING BRIDGE
Effective for service rendered on and after January 1, 2026
RATE
Net Excess Energy Credit per month, per kWh
4.53¢
Non-Bypassable Charge per month, per Nameplate Capacity kW
$0.96
MINIMUM BILL
There shall be a monthly minimum bill of $22
"""

CAROLINAS_NSC_TEXT = """\
Duke Energy Carolinas, LLC
NC Second Revised Leaf No. 139
RIDER NSC
NON-RESIDENTIAL SOLAR CHOICE
Effective for service rendered on and after January 1, 2026
Standby Charge of $1.97 per kW per month shall apply to customers with a generation system larger than 100 kW.
RATE
Monthly Credit for Net Excess Energy, per kWh
$0.0440
"""

CAROLINAS_SCG_TEXT = """\
Duke Energy Carolinas, LLC
NC FifteenthSixteenth Revised Leaf No. 75
RIDER SCG
SMALL CUSTOMER GENERATOR
RATE
Supplemental Basic Customer Charge per month: $3.92
Standby Charge per month, if applicable: $1.92
"""

CAROLINAS_SCG_TERMS_ONLY_TEXT = """\
Rider SCG (NC) continued
DETERMINATION OF ON-PEAK AND OFF-PEAK ENERGY
The On-Peak Period Hours shall be those hours, Monday through Friday, beginning at 1 P.M. and ending at 9 P.M.
DEFINITION OF "NAMEPLATE RATING"
SAFETY, INTERCONNECTION AND INSPECTION REQUIREMENTS
This Rider is only applicable for installed generation systems and equipment that comply with the Interconnection Standard.
"""

CAROLINAS_RT_WITH_SCG_REFERENCE_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE RT (NC)
RESIDENTIAL SERVICE, TIME OF USE
Additionally, power delivered under this schedule shall not be used in parallel with other electric power
except at the option of the Company, or for service in conjunction with Rider SCG or Rider NM.
RATE:
Basic Facilities Charge per month $13.83
"""

CAROLINAS_RT_TOU_TEXT = """\
Duke Energy Carolinas, LLC
NC Twenty-Sixth Revised Leaf No. 15
SCHEDULE RT (NC)
RESIDENTIAL SERVICE, TIME OF USE
RATE
Basic Facilities Charge per month
$13.83
On-Peak Energy per month, per kWh
17.1204¢
Off-Peak Energy per month, per kWh
6.7444¢
"""

CAROLINAS_PL_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE PL (NC)
STREET AND PUBLIC LIGHTING SERVICE
High Pressure Sodium Vapor
Suburban (1)
Urban
Suburban (2) (in suitable mercury fixture)
Urban
Urban
Urban (3) (in suitable mercury fixture)
Urban
Urban (installed on 55-foot wood pole)
Per Month Per Luminaire
Inside
Municipal Limits
$8.40
$9.60
$9.28
$10.45
$12.53
$13.63
$15.52
$31.79
Outside
Municipal Limits
$8.85
$10.07
$9.73
$10.91
$13.00
$14.07
$15.97
$32.23
Metal Halide
Urban
$18.66
$19.12
Mercury Vapor *
Suburban (1)
Suburban (1)
Urban (4)
Urban (4)
Urban (4)
Incandescent(5)
Suburban
Post Top
$5.51
$7.46
$8.57
$12.15
$25.18
$5.40
$2.01
NA
$7.94
$9.03
$12.62
$25.63
NA
NA
"""

CAROLINAS_FL_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE FL (NC)
FLOODLIGHTING SERVICE
High Pressure Sodium Vapor
Floodlight
Floodlight
Floodlight
Metal Halide
Floodlight
Floodlight (2)
Floodlight (2)
Floodlight half night (2)
Floodlight (2)
Floodlight (2)
Existing Pole (1)
New Pole
$14.56
$17.14
$19.28
$21.41
$20.07
NA
$29.22
NA
$49.73
$22.56
$25.14
$27.28
$29.41
$22.39 (5)
$26.41 (6)
$35.57
$50.11 (5)
$56.08
Underground
$27.33
$29.91
$32.05
$34.18
NA
$28.40
$37.56
NA
$58.07
"""

CAROLINAS_OL_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE OL (NC)
OUTDOOR LIGHTING SERVICE
High Pressure Sodium Vapor
Urban $9.89 $11.11 $11.97
Metal Halide
Area $18.32 $19.54 $20.40
"""

CAROLINAS_YL_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE YL (NC)
YARD LIGHTING SERVICE
MONTHLY RATE PER UNIT
250 Watt high pressure sodium vapor attached to existing Company secondary pole $16.54
100 Watt high pressure sodium, (standard traditional luminaire) $8.71
POLES
Special yard lighting pole (30 ft. wood) used only for the support of yard lighting and one span of secondary $2.25
"""

CAROLINAS_GL_TEXT = """\
Duke Energy Carolinas, LLC
SCHEDULE GL (NC)
GOVERNMENTAL LIGHTING SERVICE
High Pressure Sodium Vapor
Urban $9.89 $11.11 $11.97
Metal Halide
Urban $11.33 $12.55 $13.41
"""

CAROLINAS_NM_CURRENT_TEXT = """\
Duke Energy Carolinas, LLC
RIDER NM (NC)
NET METERING
Customer will be assessed a monthly Minimum Bill set at $10 more than the Basic Facilities Charge at that time.
A standby charge of $1.7235 per kW per month will apply to all nonresidential customers where the generator is larger than 100 kW.
"""

CAROLINAS_NM_CONTINUATION_TEXT = """\
Duke Energy Carolinas, LLC
RIDER NM (NC)
NET METERING
MINIMUM BILL
The monthly minimum bill for Customers receiving service under this Rider shall be no less than Basic Facilities Charge plus
the if applicable, any of the following charges: the Demand Charge, the Economy Demand Charge the Standby Charge, and
the Extra Facilities Charge.
METERING REQUIREMENTS
The Company will furnish, install, own and maintain metering to measure the kilowatt demand delivered by the Company.
SAFETY, INTERCONNECTION AND INSPECTION REQUIREMENTS
This Rider is only applicable for installed generation systems and equipment that comply with the Interconnection Procedures.
"""

CAROLINAS_LEGACY_RIDER_SUMMARY_TEXT = """\
Duke Energy Carolinas, LLC
Electricity No. 4
North Carolina Second Revised Leaf No. 99
SUMMARY OF RIDER ADJUSTMENTS
Schedule HP - General Service
Schedule HP - Industrial
cents/kWh
Baseline
-0.3776
0.0428
0.0603
-0.0081
0.0145
0.0205
-0.1220
-0.0549
-0.0900
0.0217
-0.4928
0.1592
-0.3336
cents/kWh
Incremental
0.0000
0.0428
0.0603
-0.0081
0.0145
0.0000
-0.1220
-0.0549
-0.0900
0.0217
-0.1357
0.1592
0.0235
Effective
Date
"""

CAROLINAS_EE_OLD_TEXT = """\
Duke Energy Carolinas, LLC
RIDER EE (NC)
ENERGY EFFICIENCY RIDER
ENERGY EFFICIENCY RIDER ADJUSTMENTS (EEA)
The EEA applicable to the residential and non-residential rate schedules are as follows:
Residential
0.1206 c per kWh
Nonresidential
Vintage 1
Energy Efficiency
0.0226 c per kWh
Demand Side Management
0.0202 c per kWh
Vintage 1 Total
0.0428 c per kWh
"""

CAROLINAS_EE_TOTALS_TEXT = """\
Duke Energy Carolinas, LLC
Rider EE (NC)
ENERGY EFFICIENCY RIDER
ENERGY EFFICIENCY RIDER ADJUSTMENTS (EEA) FOR ALL PROGRAM YEARS
The Rider EE amounts applicable to the residential and nonresidential rate schedules are as follows:
Total Residential Rate
0.4291 c per kWh
Total Nonresidential
0.4822 c per kWh
"""

CAROLINAS_EC_TEXT = """\
Duke Energy Carolinas, LLC
RIDER EC (NC)
ECONOMIC DEVELOPMENT
APPLICATION OF CREDIT
Months 1-12 20%
Months 13-24 15%
Months 25-36 10%
Months 37-48 5%
After Month 48 0%
"""

CAROLINAS_IS_TEXT = """\
Duke Energy Carolinas, LLC
RIDER IS (NC)
INTERRUPTIBLE POWER SERVICE
The amount of credit to be applied to the Customer's account each month will be determined by the formula:
Credit = EID x $3.50 / KWEID
Penalty = EKWP x $10.00
"""

GSA_TEXT = """\
Duke Energy Carolinas, LLC
RIDER GSA
GREEN SOURCE ADVANTAGE (NC)
all customer applications shall be accompanied by the payment of a $2,000 nonrefundable application fee.
GSA Administrative Charge – the applicable monthly administrative charge shall be $375 per Customer Account,
plus an additional $50 charge per additional account billed.
"""

GENERIC_RESIDENTIAL_TEXT = """\
Progress Energy Carolinas
Residential Service
Basic Customer Charge: $14.00 per month
12.623¢ per kWh for all kWh
"""


def _seed_historical_doc_with_version(
    conn,
    *,
    family_key: str,
    company: str,
    title: str,
    local_path: str,
    effective_start: str,
    now: str,
) -> int:
    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            family_key,
            "NC",
            company,
            family_key.split("-")[-1],
            None,
            "rate_schedule",
            title,
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            family_key,
            title,
            "NC",
            company,
            "rate",
            "pdf",
            f"https://example.test/{family_key}.pdf",
            f"https://archive.test/{family_key}",
            "2026-03-26T00:00:00Z",
            local_path,
            f"hash-{family_key}",
            effective_start,
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            family_key,
            historical_id,
            effective_start,
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    return historical_id


def test_registry_selects_progress_residential_tou_profile_for_progress_tou_family() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-502", "effective_start": "2024-01-01"}

    profile = registry.select(doc, TOU_SEASONAL_TEXT)

    assert profile.name == "progress_residential_tou"

    ranked = registry.rank_candidates(doc, TOU_SEASONAL_TEXT)
    assert ranked[0].name == "progress_residential_tou"
    assert ranked[0].score > ranked[-1].score
    assert "progress_tou_family" in ranked[0].reasons
    assert "tou_terms" in ranked[0].reasons


def test_registry_returns_unknown_when_no_profile_supports_document() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-schedule-it", "company": "carolinas", "title": "Schedule IT"}

    profile = registry.select(doc, "Industrial transmission service terms without explicit rate markers.")

    assert profile.name == "unknown"


def test_registry_selects_progress_flat_profile_for_leaf_500() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-500", "company": "progress", "effective_start": "2024-01-01"}

    profile = registry.select(doc, GENERIC_RESIDENTIAL_TEXT)

    assert profile.name == "progress_residential_flat"

    ranked = registry.rank_candidates(doc, GENERIC_RESIDENTIAL_TEXT)
    assert ranked[0].name == "progress_residential_flat"
    assert "progress_family" in ranked[0].reasons
    assert "leaf500" in ranked[0].reasons


def test_registry_selects_progress_billing_adjustments_profile_for_leaf_601() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-601", "company": "progress", "effective_start": "2026-01-01"}

    profile = registry.select(doc, PROGRESS_BA_TEXT)

    assert profile.name == "progress_billing_adjustments"

    ranked = registry.rank_candidates(doc, PROGRESS_BA_TEXT)
    assert ranked[0].name == "progress_billing_adjustments"
    assert "family=leaf601" in ranked[0].reasons
    assert "billing_adjustment_factors" in ranked[0].reasons


def test_registry_selects_progress_billing_adjustments_profile_for_leaf_601_notice_text() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-601", "company": "progress", "effective_start": "2021-01-01"}

    profile = registry.select(doc, PROGRESS_BA_NOTICE_TEXT)

    assert profile.name == "progress_billing_adjustments"

    ranked = registry.rank_candidates(doc, PROGRESS_BA_NOTICE_TEXT)
    assert ranked[0].name == "progress_billing_adjustments"
    assert "family=leaf601" in ranked[0].reasons
    assert "ba_notice_rates" in ranked[0].reasons


def test_registry_selects_progress_residential_load_control_profile_for_leaf_715() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-715", "company": "progress", "effective_start": "2024-01-01"}

    profile = registry.select(doc, PROGRESS_LC_TEXT)

    assert profile.name == "progress_residential_load_control"

    ranked = registry.rank_candidates(doc, PROGRESS_LC_TEXT)
    assert ranked[0].name == "progress_residential_load_control"
    assert "leaf715_lc" in ranked[0].reasons


def test_registry_selects_progress_current_leaf_bridge_for_leaf_501() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-leaf-501",
        "company": "progress",
        "local_path": r"data\raw\nc\progress\rate\leaf-no-501.pdf",
    }

    profile = registry.select(doc, PROGRESS_R_TOUD_TEXT)

    assert isinstance(profile, ProgressCurrentLeafBridgeProfile)
    assert profile.name == "progress_current_leaf_bridge"

    ranked = registry.rank_candidates(doc, PROGRESS_R_TOUD_TEXT)
    assert ranked[0].name == "progress_current_leaf_bridge"
    assert "current_progress_pdf" in ranked[0].reasons
    assert "leaf501_r_toud" in ranked[0].reasons
    assert "tou_terms" in ranked[0].reasons


def test_registry_selects_progress_specialty_rider_for_leaf_670() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-leaf-670",
        "company": "progress",
        "local_path": r"data\raw\nc\progress\rider\leaf-no-670.pdf",
    }

    profile = registry.select(doc, PROGRESS_RSC_TEXT)

    assert isinstance(profile, ProgressSpecialtyRiderProfile)
    assert profile.name == "progress_specialty_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_RSC_TEXT)
    assert ranked[0].name == "progress_specialty_rider"
    assert "current_progress_pdf" in ranked[0].reasons
    assert "leaf670_rider_rsc" in ranked[0].reasons
    assert "credit_terms" in ranked[0].reasons


def test_registry_selects_progress_specialty_rider_for_leaf_669() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-leaf-669",
        "company": "progress",
        "local_path": r"data\raw\nc\progress\rider\leaf-no-669.pdf",
    }

    profile = registry.select(doc, PROGRESS_NMB_TEXT)

    assert isinstance(profile, ProgressSpecialtyRiderProfile)
    assert profile.name == "progress_specialty_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_NMB_TEXT)
    assert ranked[0].name == "progress_specialty_rider"
    assert "current_progress_pdf" in ranked[0].reasons
    assert "leaf669_rider_nmb" in ranked[0].reasons
    assert "credit_terms" in ranked[0].reasons


def test_registry_selects_progress_energywise_business_for_leaf_706() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-706", "company": "progress"}

    profile = registry.select(doc, PROGRESS_EWB_TEXT)

    assert isinstance(profile, ProgressEnergywiseBusinessProfile)
    assert profile.name == "progress_energywise_business"

    ranked = registry.rank_candidates(doc, PROGRESS_EWB_TEXT)
    assert ranked[0].name == "progress_energywise_business"
    assert "leaf706_ewb" in ranked[0].reasons
    assert "control_credits" in ranked[0].reasons


def test_registry_selects_progress_sunsense_solar_rebate_for_leaf_716() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-716", "company": "progress"}

    profile = registry.select(doc, PROGRESS_SSR_TEXT)

    assert isinstance(profile, ProgressSunSenseSolarRebateProfile)
    assert profile.name == "progress_sunsense_solar_rebate"

    ranked = registry.rank_candidates(doc, PROGRESS_SSR_TEXT)
    assert ranked[0].name == "progress_sunsense_solar_rebate"
    assert "leaf716_ssr" in ranked[0].reasons
    assert "ssr_credit" in ranked[0].reasons


def test_registry_selects_progress_meter_related_optional_programs_for_leaf_661() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-661", "company": "progress"}

    profile = registry.select(doc, PROGRESS_MROP_TEXT)

    assert isinstance(profile, ProgressMeterRelatedOptionalProgramsProfile)
    assert profile.name == "progress_meter_related_optional_programs"

    ranked = registry.rank_candidates(doc, PROGRESS_MROP_TEXT)
    assert ranked[0].name == "progress_meter_related_optional_programs"
    assert "leaf661_mrop" in ranked[0].reasons
    assert "energy_profiler_online" in ranked[0].reasons


def test_registry_selects_progress_standby_service_for_leaf_653() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-653", "company": "progress"}

    profile = registry.select(doc, PROGRESS_SS_TEXT)

    assert isinstance(profile, ProgressStandbyServiceProfile)
    assert profile.name == "progress_standby_service"

    ranked = registry.rank_candidates(doc, PROGRESS_SS_TEXT)
    assert ranked[0].name == "progress_standby_service"
    assert "leaf653_standby_service" in ranked[0].reasons
    assert "standby_delivery_charge" in ranked[0].reasons


def test_registry_selects_progress_greenpower_program_for_leaf_642() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-642", "company": "progress"}

    profile = registry.select(doc, PROGRESS_GP_TEXT)

    assert isinstance(profile, ProgressGreenPowerProgramProfile)
    assert profile.name == "progress_greenpower_program"

    ranked = registry.rank_candidates(doc, PROGRESS_GP_TEXT)
    assert ranked[0].name == "progress_greenpower_program"
    assert "leaf642_greenpower" in ranked[0].reasons
    assert "per_block" in ranked[0].reasons


def test_registry_selects_progress_greenpower_program_for_leaf_643() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-643", "company": "progress"}

    profile = registry.select(doc, PROGRESS_REN_TEXT)

    assert isinstance(profile, ProgressGreenPowerProgramProfile)
    assert profile.name == "progress_greenpower_program"

    ranked = registry.rank_candidates(doc, PROGRESS_REN_TEXT)
    assert ranked[0].name == "progress_greenpower_program"
    assert "leaf643_renewable_ren" in ranked[0].reasons
    assert "per_block" in ranked[0].reasons


def test_registry_selects_progress_customer_assistance_recovery_for_leaf_611() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-611", "company": "progress"}

    profile = registry.select(doc, PROGRESS_CAR_TEXT)

    assert isinstance(profile, ProgressCustomerAssistanceRecoveryProfile)
    assert profile.name == "progress_customer_assistance_recovery"

    ranked = registry.rank_candidates(doc, PROGRESS_CAR_TEXT)
    assert ranked[0].name == "progress_customer_assistance_recovery"
    assert "leaf611_car" in ranked[0].reasons
    assert "billing_table" in ranked[0].reasons


def test_registry_selects_progress_storm_securitization_for_leaf_613() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-613", "company": "progress"}

    profile = registry.select(doc, PROGRESS_STS_TEXT)

    assert isinstance(profile, ProgressStormSecuritizationProfile)
    assert profile.name == "progress_storm_securitization"

    ranked = registry.rank_candidates(doc, PROGRESS_STS_TEXT)
    assert ranked[0].name == "progress_storm_securitization"
    assert "leaf613_sts" in ranked[0].reasons
    assert "billing_rate_table" in ranked[0].reasons


def test_registry_selects_progress_demand_response_automation_for_leaf_717() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-717", "company": "progress"}

    profile = registry.select(doc, PROGRESS_DRA_TEXT)

    assert isinstance(profile, ProgressDemandResponseAutomationProfile)
    assert profile.name == "progress_demand_response_automation"

    ranked = registry.rank_candidates(doc, PROGRESS_DRA_TEXT)
    assert ranked[0].name == "progress_demand_response_automation"
    assert "leaf717_dra" in ranked[0].reasons
    assert "availability_credit" in ranked[0].reasons
    assert "event_credit" in ranked[0].reasons


def test_registry_selects_progress_powerpair_pilot_for_leaf_770() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-770", "company": "progress"}

    profile = registry.select(doc, PROGRESS_POWERPAIR_TEXT)

    assert isinstance(profile, ProgressPowerPairPilotProfile)
    assert profile.name == "progress_powerpair_pilot"

    ranked = registry.rank_candidates(doc, PROGRESS_POWERPAIR_TEXT)
    assert ranked[0].name == "progress_powerpair_pilot"
    assert "leaf770_powerpair" in ranked[0].reasons
    assert "incentive_terms" in ranked[0].reasons


def test_registry_selects_progress_single_value_rider_profile_for_leaf_609() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-609", "company": "progress", "title": "Rider ESM"}

    profile = registry.select(doc, PROGRESS_ESM_TEXT)
    assert isinstance(profile, ProgressSingleValueRiderProfile)
    assert profile.name == "progress_single_value_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_ESM_TEXT)
    assert ranked[0].name == "progress_single_value_rider"
    assert "single_value_rider_family" in ranked[0].reasons


def test_registry_selects_progress_single_value_rider_profile_when_kwh_marker_wraps_line() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-609", "company": "progress", "title": "Rider ESM"}

    profile = registry.select(doc, PROGRESS_ESM_TEXT_LINEBREAK_RATE)
    assert isinstance(profile, ProgressSingleValueRiderProfile)
    assert profile.name == "progress_single_value_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_ESM_TEXT_LINEBREAK_RATE)
    assert ranked[0].name == "progress_single_value_rider"
    assert "single_value_rider_family" in ranked[0].reasons


def test_registry_selects_progress_single_value_rider_for_leaf_602_with_relaxed_family_signal() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-leaf-602",
        "company": "progress",
        "title": "Joint Agency Asset Rider JAA",
    }
    text = """
    Joint Agency Asset Rider JAA
    Rider JAA
    Monthly Rate:
    Leaf No. 602
    """

    profile = registry.select(doc, text)
    assert isinstance(profile, ProgressSingleValueRiderProfile)
    assert profile.name == "progress_single_value_rider"

    ranked = registry.rank_candidates(doc, text)
    assert ranked[0].name == "progress_single_value_rider"
    assert "single_value_rider_family" in ranked[0].reasons
    assert "relaxed_family_selection" in ranked[0].reasons


def test_registry_selects_progress_recovery_rider_for_recovery_rider_family() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-rider-RECOVERYRIDER",
        "company": "progress",
        "title": "Recovery Rider",
    }

    profile = registry.select(doc, PROGRESS_RECOVERY_RIDER_TEXT)

    assert isinstance(profile, ProgressRecoveryRiderProfile)
    assert profile.name == "progress_recovery_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_RECOVERY_RIDER_TEXT)
    assert ranked[0].name == "progress_recovery_rider"
    assert "recovery_rider" in ranked[0].reasons
    assert "monthly_rate" in ranked[0].reasons


def test_registry_selects_progress_management_cost_recovery_rider_family() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER",
        "company": "progress",
        "title": "Management and Energy Efficiency Cost Recovery Rider",
    }

    profile = registry.select(doc, PROGRESS_MEECR_TEXT)

    assert isinstance(profile, ProgressManagementEnergyEfficiencyCostRecoveryRiderProfile)
    assert profile.name == "progress_management_energy_efficiency_cost_recovery_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_MEECR_TEXT)
    assert ranked[0].name == "progress_management_energy_efficiency_cost_recovery_rider"
    assert "management_energy_efficiency_cost_recovery_rider" in ranked[0].reasons
    assert "monthly_rate" in ranked[0].reasons


def test_registry_selects_progress_compliance_report_cost_recovery_rider_family() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER",
        "company": "progress",
        "title": "Compliance Report and Cost Recovery Rider",
    }

    profile = registry.select(doc, PROGRESS_CRCR_TEXT)

    assert isinstance(profile, ProgressComplianceReportAndCostRecoveryRiderProfile)
    assert profile.name == "progress_compliance_report_and_cost_recovery_rider"

    ranked = registry.rank_candidates(doc, PROGRESS_CRCR_TEXT)
    assert ranked[0].name == "progress_compliance_report_and_cost_recovery_rider"
    assert "compliance_report_and_cost_recovery_rider" in ranked[0].reasons
    assert "monthly_rate" in ranked[0].reasons


def test_registry_selects_rider_adjustment_profile_for_leaf_600_summary() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-600", "effective_start": "2026-01-01"}

    profile = registry.select(doc, RIDER_SUMMARY_TEXT)

    assert profile.name == "progress_rider_adjustment_matrix"

    ranked = registry.rank_candidates(doc, RIDER_SUMMARY_TEXT)
    assert ranked[0].name == "progress_rider_adjustment_matrix"
    assert ranked[0].score > ranked[1].score
    assert "family=leaf600" in ranked[0].reasons
    assert "summary_text" in ranked[0].reasons


def test_registry_selects_carolinas_residential_profile_for_rs_sheet() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-schedule-RS", "effective_start": "2026-01-01"}

    profile = registry.select(doc, CAROLINAS_RS_TEXT)

    assert profile.name == "carolinas_residential_flat"

    ranked = registry.rank_candidates(doc, CAROLINAS_RS_TEXT)
    assert ranked[0].name == "carolinas_residential_flat"
    assert "carolinas_family" in ranked[0].reasons
    assert "flat_rate_markers" in ranked[0].reasons


def test_registry_selects_carolinas_residential_tou_profile_for_rt_sheet() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-schedule-RT",
        "company": "carolinas",
        "local_path": r"data\raw\historical\ncuc\e-7\rt.pdf",
    }

    profile = registry.select(doc, CAROLINAS_RT_TOU_TEXT)

    assert isinstance(profile, CarolinasResidentialTouProfile)
    assert profile.name == "carolinas_residential_tou"

    ranked = registry.rank_candidates(doc, CAROLINAS_RT_TOU_TEXT)
    assert ranked[0].name == "carolinas_residential_tou"
    assert "carolinas_tou_schedule" in ranked[0].reasons
    assert "tou_terms" in ranked[0].reasons


def test_registry_selects_carolinas_rider_summary_profile() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-rider-SUMMARY",
        "company": "carolinas",
        "effective_start": "2024-01-15",
    }

    profile = registry.select(doc, CAROLINAS_RIDER_SUMMARY_TEXT)

    assert profile.name == "carolinas_rider_adjustment_matrix"

    ranked = registry.rank_candidates(doc, CAROLINAS_RIDER_SUMMARY_TEXT)
    assert ranked[0].name == "carolinas_rider_adjustment_matrix"
    assert ranked[0].score > ranked[1].score


def test_registry_selects_carolinas_small_customer_generator_profile() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-rider-SCG",
        "company": "carolinas",
    }

    profile = registry.select(doc, CAROLINAS_SCG_TEXT)

    assert isinstance(profile, CarolinasSmallCustomerGeneratorProfile)
    assert profile.name == "carolinas_small_customer_generator"

    ranked = registry.rank_candidates(doc, CAROLINAS_SCG_TEXT)
    assert ranked[0].name == "carolinas_small_customer_generator"
    assert "rider_scg" in ranked[0].reasons
    assert "supplemental_charge" in ranked[0].reasons


def test_registry_selects_carolinas_current_leaf_bridge_for_hlf() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-schedule-HLF",
        "company": "carolinas",
        "local_path": r"data\raw\nc\carolinas\rate\hlf.pdf",
    }

    profile = registry.select(doc, CAROLINAS_HLF_TEXT)

    # Either bridge profile is acceptable — both call parse_nc_carolinas_leaf
    # and produce identical extraction results.
    assert profile.name in {"carolinas_current_leaf_bridge", "carolinas_schedule_bridge"}

    ranked = registry.rank_candidates(doc, CAROLINAS_HLF_TEXT)
    assert ranked[0].name in {"carolinas_current_leaf_bridge", "carolinas_schedule_bridge"}
    assert "hlf_schedule" in ranked[0].reasons


@pytest.mark.parametrize(
    ("family_key", "text", "reason"),
    [
        ("nc-carolinas-schedule-PG", CAROLINAS_PG_TEXT, "pg_schedule"),
        ("nc-carolinas-schedule-LGS", CAROLINAS_LGS_TEXT, "lgs_schedule"),
        (
            "nc-carolinas-schedule-SGS",
            "Duke Energy Carolinas, LLC\nNC Revised Leaf No. 21\nSCHEDULE SGS (NC)\nSMALL GENERAL SERVICE\nRATE\nBasic Facilities Charge per month\n$12.00\nEnergy Charge\nFor all kWh used per month, per kWh 7.5000¢\n",
            "sgs_schedule",
        ),
        (
            "nc-carolinas-doc-SCHEDULEOPTIOPTIONALPOWERSERVICETIMEOFUSEINDUSTR",
            "Duke Energy Carolinas, LLC\nNC Revised Leaf No. 47\nSCHEDULE OPT-I (NC)\nOPTIONAL POWER SERVICE\nRATE\nBasic Customer Charge per month\n$15.00\nEnergy Charge per month, per kWh\n5.1100¢\n",
            "opti_schedule",
        ),
    ],
)
def test_registry_selects_carolinas_general_service_schedule(
    family_key: str,
    text: str,
    reason: str,
) -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": family_key,
        "company": "carolinas",
        "local_path": r"data\raw\historical\ncuc\e-7\example.pdf",
    }

    profile = registry.select(doc, text)

    assert isinstance(profile, CarolinasGeneralServiceScheduleProfile)
    assert profile.name == "carolinas_general_service_schedule"

    ranked = registry.rank_candidates(doc, text)
    assert ranked[0].name == "carolinas_general_service_schedule"
    assert "carolinas_general_service" in ranked[0].reasons
    assert reason in ranked[0].reasons


@pytest.mark.parametrize(
    ("family_key", "text", "reason"),
    [
        (
            "nc-carolinas-schedule-I",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 41
SCHEDULE I (NC)
INDUSTRIAL SERVICE
RATE:
Basic Facilities Charge per month
$19.91
Demand Charge
For all over 30 kW of Billing Demand per month, per kW $ 4.6197
Energy Charge
For all kWh per month, per kWh 5.3653¢
""",
            "industrial_schedule",
        ),
        (
            "nc-carolinas-doc-SCHEDULEOPTE",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 45
SCHEDULE OPT-E (NC)
OPTIONAL POWER SERVICE TIME-OF-USE ENERGY-ONLY (PILOT)
RATE:
Basic Facilities Charge per month
$34.31
Energy Charge
On-Peak Energy per month, per kWh 18.9376¢
All Off-Peak Energy per month, per kWh 3.4993¢
""",
            "opte_schedule",
        ),
        (
            "nc-carolinas-schedule-TS",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 38
SCHEDULE TS (NC)
TRAFFIC SIGNAL SERVICE
RATE:
Basic Facilities Charge per month
$6.92
Energy Charge
For the first 50 kWh used per month, per kWh 19.4905¢
For all over 50 kWh used per month, per kWh 7.6859¢
""",
            "ts_schedule",
        ),
        (
            "nc-carolinas-doc-SCHEDULEWC",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 17
SCHEDULE WC (NC)
RESIDENTIAL WATER HEATING SERVICE
RATE:
Basic Facilities Charge per month
$1.77
Energy Charge
All kWh per month, per kWh 4.8238¢
""",
            "wc_schedule",
        ),
    ],
)
def test_registry_selects_carolinas_schedule_bridge(
    family_key: str,
    text: str,
    reason: str,
) -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": family_key, "company": "carolinas"}

    profile = registry.select(doc, text)

    assert isinstance(profile, CarolinasScheduleBridgeProfile)
    assert profile.name == "carolinas_schedule_bridge"

    ranked = registry.rank_candidates(doc, text)
    assert ranked[0].name == "carolinas_schedule_bridge"
    assert "carolinas_schedule_bridge" in ranked[0].reasons
    assert reason in ranked[0].reasons


def test_registry_selects_carolinas_solar_choice_rider_for_nmb() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-rider-NMB",
        "company": "carolinas",
        "local_path": r"data\raw\nc\carolinas\rider\nmb.pdf",
    }

    profile = registry.select(doc, CAROLINAS_NMB_TEXT)

    assert isinstance(profile, CarolinasSolarChoiceRiderProfile)
    assert profile.name == "carolinas_solar_choice_rider"

    ranked = registry.rank_candidates(doc, CAROLINAS_NMB_TEXT)
    assert ranked[0].name == "carolinas_solar_choice_rider"
    assert "current_carolinas_pdf" in ranked[0].reasons
    assert "rider_nmb" in ranked[0].reasons
    assert "credit_terms" in ranked[0].reasons


def test_registry_selects_carolinas_lighting_schedule_for_pl() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-doc-SCHEDULEPLSTREETANDPUBLICLIGHTINGSERVICE",
        "company": "carolinas",
    }

    profile = registry.select(doc, CAROLINAS_PL_TEXT)

    assert isinstance(profile, CarolinasLightingScheduleProfile)
    assert profile.name == "carolinas_lighting_schedule"

    ranked = registry.rank_candidates(doc, CAROLINAS_PL_TEXT)
    assert ranked[0].name == "carolinas_lighting_schedule"
    assert "lighting_schedule" in ranked[0].reasons
    assert "schedule_pl" in ranked[0].reasons


def test_registry_selects_carolinas_lighting_schedule_for_ol() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-schedule-OL",
        "company": "carolinas",
    }

    profile = registry.select(doc, CAROLINAS_OL_TEXT)

    assert isinstance(profile, CarolinasLightingScheduleProfile)
    assert profile.name == "carolinas_lighting_schedule"

    ranked = registry.rank_candidates(doc, CAROLINAS_OL_TEXT)
    assert ranked[0].name == "carolinas_lighting_schedule"
    assert "lighting_schedule" in ranked[0].reasons
    assert "schedule_ol" in ranked[0].reasons


def test_registry_selects_carolinas_lighting_schedule_for_pl_alias_family() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-schedule-PL",
        "company": "carolinas",
    }

    ranked = registry.rank_candidates(doc, CAROLINAS_PL_TEXT)

    assert ranked[0].name == "carolinas_lighting_schedule"
    assert "schedule_pl" in ranked[0].reasons


def test_registry_selects_carolinas_net_metering_rider() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-rider-NM",
        "company": "carolinas",
    }

    profile = registry.select(doc, CAROLINAS_NM_CURRENT_TEXT)

    assert isinstance(profile, CarolinasNetMeteringRiderProfile)
    assert profile.name == "carolinas_net_metering_rider"

    ranked = registry.rank_candidates(doc, CAROLINAS_NM_CURRENT_TEXT)
    assert ranked[0].name == "carolinas_net_metering_rider"
    assert "rider_nm" in ranked[0].reasons
    assert "standby_charge" in ranked[0].reasons


def test_registry_selects_carolinas_energy_efficiency_rider() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-carolinas-rider-EE",
        "company": "carolinas",
    }

    profile = registry.select(doc, CAROLINAS_EE_TOTALS_TEXT)

    assert isinstance(profile, CarolinasEnergyEfficiencyRiderProfile)
    assert profile.name == "carolinas_energy_efficiency_rider"

    ranked = registry.rank_candidates(doc, CAROLINAS_EE_TOTALS_TEXT)
    assert ranked[0].name == "carolinas_energy_efficiency_rider"
    assert "rider_ee" in ranked[0].reasons
    assert "explicit_rate_values" in ranked[0].reasons


@pytest.mark.parametrize(
    ("family_key", "text", "expected_type"),
    [
        ("nc-carolinas-rider-EC", CAROLINAS_EC_TEXT, CarolinasEconomicDevelopmentRiderProfile),
        ("nc-carolinas-rider-IS", CAROLINAS_IS_TEXT, CarolinasInterruptibleServiceRiderProfile),
        ("nc-carolinas-rider-GSA", GSA_TEXT, GreenSourceAdvantageRiderProfile),
        ("nc-progress-leaf-665", GSA_TEXT, GreenSourceAdvantageRiderProfile),
    ],
)
def test_registry_selects_new_rider_profiles(family_key: str, text: str, expected_type: type) -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": family_key, "company": "carolinas"}
    if family_key == "nc-progress-leaf-665":
        doc["company"] = "progress"

    profile = registry.select(doc, text)

    assert isinstance(profile, expected_type)


def test_registry_recommends_fallback_sequence_after_selected_profile() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-progress-leaf-502", "effective_start": "2024-01-01"}

    ranked = registry.rank_candidates(doc, TOU_SEASONAL_TEXT)
    recommendations = registry.recommend_fallback_sequence(
        doc,
        TOU_SEASONAL_TEXT,
        ranked_candidates=ranked,
        selected_name="progress_residential_tou",
    )

    assert recommendations
    assert recommendations[0].name == "generic_residential"
    assert all(candidate.name != "progress_residential_tou" for candidate in recommendations)


def test_progress_residential_tou_profile_extracts_seasonal_tou_rates_and_discount() -> None:
    profile = ProgressResidentialTouProfile()
    doc = {"family_key": "nc-progress-leaf-502", "effective_start": "2024-01-01"}

    charges = profile.extract(doc, TOU_SEASONAL_TEXT)

    fixed = [c for c in charges if c.charge_type == "fixed"]
    assert len(fixed) == 1
    assert fixed[0].rate_value == 14.0

    tou = [c for c in charges if c.charge_type == "tou_energy"]
    assert len(tou) == 6

    by_season_period = {(c.season, c.tou_period): c.rate_value for c in tou}
    assert by_season_period[("summer", "on_peak")] == pytest.approx(0.29905)
    assert by_season_period[("summer", "off_peak")] == pytest.approx(0.11321)
    assert by_season_period[("summer", "discount")] == pytest.approx(0.07372)
    assert by_season_period[("winter", "on_peak")] == pytest.approx(0.21952)
    assert by_season_period[("winter", "off_peak")] == pytest.approx(0.11)
    assert by_season_period[("winter", "discount")] == pytest.approx(0.08274)

    assert all(c.rate_unit == "$/kWh" for c in tou)


def test_progress_residential_tou_profile_extracts_r_toud_demand_charges() -> None:
    profile = ProgressResidentialTouProfile()
    doc = {"family_key": "nc-progress-leaf-503", "effective_start": "2024-05-01"}

    charges = profile.extract(doc, TOU_DEMAND_TEXT)

    demand = [c for c in charges if c.charge_type == "demand"]
    assert len(demand) == 2

    by_label = {c.charge_label: c for c in demand}
    assert by_label["On-Peak Demand Charge"].rate_value == pytest.approx(4.35)
    assert by_label["On-Peak Demand Charge"].rate_unit == "$/kW"
    assert by_label["On-Peak Demand Charge"].tou_period == "on_peak"
    assert by_label["On-Peak Demand Charge"].season == "summer"

    assert by_label["Base Demand Charge"].rate_value == pytest.approx(1.80)
    assert by_label["Base Demand Charge"].rate_unit == "$/kW"
    assert by_label["Base Demand Charge"].tou_period == "base"
    assert by_label["Base Demand Charge"].season == "summer"


def test_progress_flat_profile_extracts_fixed_and_energy_rates() -> None:
    profile = ProgressResidentialFlatProfile()
    doc = {"family_key": "nc-progress-leaf-500", "company": "progress"}

    charges = profile.extract(doc, GENERIC_RESIDENTIAL_TEXT)

    fixed = [c for c in charges if c.charge_type == "fixed"]
    energy = [c for c in charges if c.charge_type == "energy_block"]

    assert len(fixed) == 1
    assert fixed[0].rate_value == pytest.approx(14.0)
    assert fixed[0].rate_unit == "$/month"

    assert len(energy) == 1
    assert energy[0].rate_value == pytest.approx(0.12623)
    assert energy[0].rate_unit == "$/kWh"


def test_progress_rider_adjustment_matrix_profile_extracts_component_and_total_adjustments() -> None:
    profile = ProgressRiderAdjustmentMatrixProfile()
    doc = {
        "family_key": "nc-progress-leaf-600",
        "effective_start": "2026-01-01",
        "local_path": "dep-leaf600.pdf",
    }

    charges = profile.extract(doc, RIDER_SUMMARY_TEXT)

    adjustments = [c for c in charges if c.charge_type == "adjustment"]
    totals = [c for c in charges if c.charge_type == "adjustment_total"]

    assert len(adjustments) >= 10
    assert len(totals) == 1

    by_label = {c.charge_label: c for c in adjustments}
    assert by_label["Residential Service Schedules - BA-Fuel"].rate_value == pytest.approx(0.00262)
    assert by_label["Residential Service Schedules - BA"].rate_value == pytest.approx(0.01549)
    assert by_label["Residential Service Schedules - EDIT-4"].rate_value == pytest.approx(-0.00249)
    assert by_label["Residential Service Schedules - JAA"].rate_value == pytest.approx(0.00464)

    assert totals[0].charge_label == "Residential Service Schedules Total Rider Adjustments"
    assert totals[0].rate_value == pytest.approx(0.02097)
    assert totals[0].rate_unit == "$/kWh"


def test_progress_billing_adjustments_profile_extracts_rate_class_adjustments() -> None:
    profile = ProgressBillingAdjustmentsProfile()
    doc = {"family_key": "nc-progress-leaf-601", "company": "progress", "local_path": ""}

    charges = profile.extract(doc, PROGRESS_BA_TEXT)

    assert len(charges) == 5
    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Billing Adjustment - Residential"].rate_value == pytest.approx(0.01549)
    assert by_label["Billing Adjustment - Small General Service"].rate_value == pytest.approx(0.00233)
    assert by_label["Billing Adjustment - Medium General Service"].rate_value == pytest.approx(0.00691)
    assert by_label["Billing Adjustment - Large General Service"].rate_value == pytest.approx(0.00954)
    assert by_label["Billing Adjustment - Lighting"].rate_value == pytest.approx(0.00214)
    assert all(charge.charge_type == "adjustment" for charge in charges)
    assert all(charge.rate_unit == "$/kWh" for charge in charges)


def test_progress_billing_adjustments_profile_extracts_notice_style_adjustments() -> None:
    profile = ProgressBillingAdjustmentsProfile()
    doc = {"family_key": "nc-progress-leaf-601", "company": "progress", "local_path": ""}

    charges = profile.extract(doc, PROGRESS_BA_NOTICE_TEXT)

    assert len(charges) == 4
    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Billing Adjustment Notice - Residential"].rate_value == pytest.approx(0.00155)
    assert by_label["Billing Adjustment Notice - General Service EE"].rate_value == pytest.approx(0.00054)
    assert by_label["Billing Adjustment Notice - General Service DSM"].rate_value == pytest.approx(0.00012)
    assert by_label["Billing Adjustment Notice - Lighting"].rate_value == pytest.approx(-0.00005)


def test_progress_billing_adjustments_profile_extracts_2021_notice_style_adjustments() -> None:
    profile = ProgressBillingAdjustmentsProfile()
    doc = {"family_key": "nc-progress-leaf-601", "company": "progress", "local_path": ""}

    charges = profile.extract(doc, PROGRESS_BA_NOTICE_2021_TEXT)

    assert len(charges) == 4
    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Billing Adjustment Notice - Residential"].rate_value == pytest.approx(0.001)
    assert by_label["Billing Adjustment Notice - General Service EE"].rate_value == pytest.approx(-0.00042)
    assert by_label["Billing Adjustment Notice - General Service DSM"].rate_value == pytest.approx(0.00006)
    assert by_label["Billing Adjustment Notice - Lighting"].rate_value == pytest.approx(0.00037)


@pytest.mark.parametrize(
    ("family_key", "text", "expected_count", "expected_types"),
    [
        ("nc-progress-leaf-501", PROGRESS_R_TOUD_TEXT, 5, {"fixed", "tou_energy"}),
        ("nc-progress-leaf-520", PROGRESS_SGS_TEXT, 2, {"fixed", "energy_block"}),
        ("nc-progress-leaf-535", PROGRESS_HP_TEXT, 2, {"demand"}),
        ("nc-progress-leaf-674", PROGRESS_PS_TEXT, 4, {"demand"}),
    ],
)
def test_progress_current_leaf_bridge_profile_extracts_current_pdf_charge_shapes(
    family_key: str,
    text: str,
    expected_count: int,
    expected_types: set[str],
) -> None:
    profile = ProgressCurrentLeafBridgeProfile()
    doc = {
        "family_key": family_key,
        "company": "progress",
        "local_path": r"data\raw\nc\progress\rate\placeholder.pdf",
    }

    charges = profile.extract(doc, text)

    assert len(charges) == expected_count
    assert {charge.charge_type for charge in charges} == expected_types


@pytest.mark.parametrize(
    ("family_key", "text", "expected_count"),
    [
        ("nc-progress-leaf-654", PROGRESS_NFS_TEXT, 4),
        ("nc-progress-leaf-655", PROGRESS_LLC_TEXT, 4),
        ("nc-progress-leaf-668", PROGRESS_NSC_TEXT, 1),
        ("nc-progress-leaf-669", PROGRESS_NMB_TEXT, 4),
        ("nc-progress-leaf-670", PROGRESS_RSC_TEXT, 7),
    ],
)
def test_progress_specialty_rider_profile_extracts_targeted_current_rider_values(
    family_key: str,
    text: str,
    expected_count: int,
) -> None:
    profile = ProgressSpecialtyRiderProfile()
    doc = {
        "family_key": family_key,
        "company": "progress",
        "local_path": r"data\raw\nc\progress\rider\placeholder.pdf",
    }

    charges = profile.extract(doc, text)

    assert len(charges) == expected_count
    assert all(charge.rate_value is not None for charge in charges)


def test_progress_energywise_business_profile_extracts_control_credits() -> None:
    profile = ProgressEnergywiseBusinessProfile()
    doc = {"family_key": "nc-progress-leaf-706", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_EWB_TEXT)

    assert len(charges) == 5
    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Summer Control Credit - 30% Cycling"].rate_value == pytest.approx(50.0)
    assert by_label["Summer Control Credit - 50% Cycling"].rate_value == pytest.approx(85.0)
    assert by_label["Summer Control Credit - 75% Cycling"].rate_value == pytest.approx(135.0)
    assert by_label["Winter Control Credit"].rate_value == pytest.approx(25.0)
    assert by_label["Bring Your Own kW Incentive"].rate_value == pytest.approx(30.0)


def test_progress_energywise_business_profile_extracts_non_winter_current_credit_terms() -> None:
    profile = ProgressEnergywiseBusinessProfile()
    doc = {"family_key": "nc-progress-leaf-706", "company": "progress"}
    text = """\
    ENERGYWISE FOR BUSINESS RIDER EWB
    CONTROL CREDITS
    50% Non-Winter Cycling Level - $100 per load control device
    75% Non-Winter Cycling Level - $150 per load control device
    additional $25 per thermostat
    the compensation will be $50 per kW reduced during the events.
    """

    charges = profile.extract(doc, text)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Non-Winter Control Credit - 50% Cycling"].rate_value == pytest.approx(100.0)
    assert by_label["Non-Winter Control Credit - 75% Cycling"].rate_value == pytest.approx(150.0)
    assert by_label["Winter Control Credit"].rate_value == pytest.approx(25.0)
    assert by_label["Bring Your Own kW Incentive"].rate_value == pytest.approx(50.0)


def test_progress_sunsense_solar_rebate_profile_extracts_payment_credit_and_termination_terms() -> None:
    profile = ProgressSunSenseSolarRebateProfile()
    doc = {"family_key": "nc-progress-leaf-716", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_SSR_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Participation Payment"].rate_value == pytest.approx(250.0)
    assert by_label["Participation Payment"].rate_unit == "$/kW"
    assert by_label["SSR Credit"].rate_value == pytest.approx(4.50)
    assert by_label["SSR Credit"].rate_unit == "$/kW-month"
    assert by_label["Early Termination Charge"].rate_value == pytest.approx(4.17)


def test_progress_residential_load_control_profile_extracts_incentives() -> None:
    profile = ProgressResidentialLoadControlProfile()
    doc = {"family_key": "nc-progress-leaf-715", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_LC_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["HVAC Load Control Device - Initial Incentive"].rate_value == pytest.approx(25.0)
    assert by_label["Thermostat Winter - Initial Incentive"].rate_value == pytest.approx(90.0)
    assert by_label["Thermostat Summer - Initial Incentive"].rate_value == pytest.approx(75.0)
    assert by_label["Summer Control - Annual Incentive"].rate_value == pytest.approx(25.0)
    assert by_label["Winter Control - Annual Incentive"].rate_value == pytest.approx(25.0)


def test_progress_meter_related_optional_programs_profile_extracts_named_monthly_and_setup_charges() -> None:
    profile = ProgressMeterRelatedOptionalProgramsProfile()
    doc = {"family_key": "nc-progress-leaf-661", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_MROP_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["TotalMeter Monthly Rate - Option 1"].rate_value == pytest.approx(3.00)
    assert by_label["TotalMeter Monthly Rate - Option 2"].rate_value == pytest.approx(13.20)
    assert by_label["EPO Monthly Rate - Totalized Account"].rate_value == pytest.approx(20.00)
    assert by_label["MRM Monthly Rate"].rate_value == pytest.approx(14.75)
    assert by_label["Non-Standard Meter Monthly Rate"].rate_value == pytest.approx(0.33)
    assert len(charges) >= 9


def test_progress_standby_service_profile_extracts_reservation_delivery_and_margin_charges() -> None:
    profile = ProgressStandbyServiceProfile()
    doc = {"family_key": "nc-progress-leaf-653", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_SS_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Generation Reservation Charge - Non-TOU <60% Capacity Factor"].rate_value == pytest.approx(0.79)
    assert by_label["Generation Reservation Charge - TOU <60% Capacity Factor"].rate_value == pytest.approx(0.00)
    assert by_label["Generation Reservation Charge - >=60% Capacity Factor"].rate_value == pytest.approx(0.79)
    assert by_label["Standby Service Delivery Charge - Transmission System"].rate_value == pytest.approx(2.76)
    assert by_label["Standby Service Delivery Charge - Distribution System"].rate_value == pytest.approx(5.18)
    assert by_label["Incentive Margin Adder"].rate_value == pytest.approx(0.006)


def test_progress_greenpower_program_profile_extracts_per_block_charge() -> None:
    profile = ProgressGreenPowerProgramProfile()
    doc = {"family_key": "nc-progress-leaf-642", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_GP_TEXT)

    assert len(charges) == 1
    assert charges[0].charge_label == "GreenPower Block Charge"
    assert charges[0].rate_value == pytest.approx(4.00)
    assert charges[0].rate_unit == "$/block"


def test_progress_greenpower_program_profile_extracts_renewable_rider_block_charge() -> None:
    profile = ProgressGreenPowerProgramProfile()
    doc = {"family_key": "nc-progress-leaf-643", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_REN_TEXT)

    assert len(charges) == 1
    assert charges[0].charge_label == "Renewable Rider REN Block Charge"
    assert charges[0].rate_value == pytest.approx(2.50)
    assert charges[0].rate_unit == "$/block"


def test_progress_customer_assistance_recovery_profile_extracts_residential_and_general_service_rates() -> None:
    profile = ProgressCustomerAssistanceRecoveryProfile()
    doc = {"family_key": "nc-progress-leaf-611", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_CAR_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Rider Adjustment - residential"].rate_value == pytest.approx(0.00098)
    assert by_label["Rider Adjustment - residential"].rate_unit == "$/kWh"
    assert by_label["Rider Adjustment - commercial_small"].rate_value == pytest.approx(1.12)
    assert by_label["Rider Adjustment - commercial_small"].rate_unit == "$/bill"
    assert by_label["Rider Adjustment - commercial_medium"].rate_value == pytest.approx(1.12)
    assert by_label["Rider Adjustment - commercial_large"].rate_value == pytest.approx(1.12)


def test_progress_recovery_rider_profile_extracts_delegated_charges(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ProgressRecoveryRiderProfile()
    doc = {
        "family_key": "nc-progress-rider-RECOVERYRIDER",
        "company": "progress",
        "effective_start": "2025-01-01",
    }

    def _fake_parse_text(text: str, *, version_id: int, family_key: str, document_id=None):
        return None, [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label="Recovery Rider Monthly Rate",
                rate_value=0.012,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Recovery Rider",
                confidence_score=0.91,
            )
        ], []

    monkeypatch.setattr(parser_profiles_module, "parse_nc_progress_leaf", _fake_parse_text)

    charges = profile.extract(doc, PROGRESS_RECOVERY_RIDER_TEXT)

    assert len(charges) == 1
    assert charges[0].charge_label == "Recovery Rider Monthly Rate"
    assert charges[0].rate_value == pytest.approx(0.012)
    assert charges[0].rate_unit == "$/kWh"


def test_progress_management_cost_recovery_rider_profile_extracts_delegated_charges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProgressManagementEnergyEfficiencyCostRecoveryRiderProfile()
    doc = {
        "family_key": "nc-progress-rider-MANAGEMENTANDENERGYEFFICIENCYCOSTRECOVERYRIDER",
        "company": "progress",
        "effective_start": "2025-01-01",
    }

    def _fake_parse_text(text: str, *, version_id: int, family_key: str, document_id=None):
        return None, [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label="Management and Energy Efficiency Cost Recovery Rider Rate",
                rate_value=0.014,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Management and Energy Efficiency Cost Recovery Rider",
                confidence_score=0.91,
            )
        ], []

    monkeypatch.setattr(parser_profiles_module, "parse_nc_progress_leaf", _fake_parse_text)

    charges = profile.extract(doc, PROGRESS_MEECR_TEXT)

    assert len(charges) == 1
    assert charges[0].charge_label == "Management and Energy Efficiency Cost Recovery Rider Rate"
    assert charges[0].rate_value == pytest.approx(0.014)
    assert charges[0].rate_unit == "$/kWh"


def test_progress_compliance_report_cost_recovery_rider_profile_extracts_delegated_charges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProgressComplianceReportAndCostRecoveryRiderProfile()
    doc = {
        "family_key": "nc-progress-rider-COMPLIANCEREPORTANDCOSTRECOVERYRIDER",
        "company": "progress",
        "effective_start": "2025-01-01",
    }

    def _fake_parse_text(text: str, *, version_id: int, family_key: str, document_id=None):
        return None, [
            ExtractedCharge(
                charge_type="adjustment",
                charge_label="Compliance Report and Cost Recovery Rider Rate",
                rate_value=0.016,
                rate_unit="$/kWh",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Compliance Report and Cost Recovery Rider",
                confidence_score=0.91,
            )
        ], []

    monkeypatch.setattr(parser_profiles_module, "parse_nc_progress_leaf", _fake_parse_text)

    charges = profile.extract(doc, PROGRESS_CRCR_TEXT)

    assert len(charges) == 1
    assert charges[0].charge_label == "Compliance Report and Cost Recovery Rider Rate"
    assert charges[0].rate_value == pytest.approx(0.016)
    assert charges[0].rate_unit == "$/kWh"


def test_progress_customer_assistance_recovery_profile_uses_bounded_text_over_full_pdf(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ProgressCustomerAssistanceRecoveryProfile()
    pdf_path = tmp_path / "leaf611.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%stub\n")
    doc = {
        "family_key": "nc-progress-leaf-611",
        "company": "progress",
        "local_path": str(pdf_path),
        "start_page": 3,
        "end_page": 3,
    }

    calls: list[str] = []

    def _fake_parse_text(text: str, *, version_id: int, family_key: str, document_id=None):
        calls.append("text")
        return None, [], []

    def _fake_parse_file(path, *, version_id: int, family_key: str, document_id=None):
        calls.append("file")
        raise AssertionError("bounded CAR extraction should not reparse the full PDF")

    monkeypatch.setattr(parser_profiles_module, "parse_nc_progress_leaf", _fake_parse_text)
    monkeypatch.setattr(parser_profiles_module, "parse_nc_progress_leaf_file", _fake_parse_file)

    charges = profile.extract(doc, "Customer Assistance Recovery Rider CAR\nMONTHLY RATE\n$0.25")

    assert charges == []
    assert calls == ["text"]


def test_progress_storm_securitization_profile_extracts_per_class_rates() -> None:
    profile = ProgressStormSecuritizationProfile()
    doc = {"family_key": "nc-progress-leaf-613", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_STS_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Rider Adjustment - residential"].rate_value == pytest.approx(0.00166)
    assert by_label["Rider Adjustment - commercial_small"].rate_value == pytest.approx(0.00146)
    assert by_label["Rider Adjustment - commercial_medium"].rate_value == pytest.approx(0.00040)
    assert by_label["Rider Adjustment - commercial_large"].rate_value == pytest.approx(0.00015)
    assert by_label["Rider Adjustment - lighting"].rate_value == pytest.approx(0.00024)


def test_progress_demand_response_automation_profile_extracts_credit_rates() -> None:
    profile = ProgressDemandResponseAutomationProfile()
    doc = {"family_key": "nc-progress-leaf-717", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_DRA_TEXT)

    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["Monthly Availability Credit"].rate_value == pytest.approx(3.25)
    assert by_label["Monthly Availability Credit"].rate_unit == "$/kW"
    assert by_label["Event Performance Credit"].rate_value == pytest.approx(6.00)
    assert by_label["Participant Incentive"].rate_value == pytest.approx(50.00)


def test_progress_powerpair_pilot_profile_extracts_incentive_rates() -> None:
    profile = ProgressPowerPairPilotProfile()
    doc = {"family_key": "nc-progress-leaf-770", "company": "progress"}

    charges = profile.extract(doc, PROGRESS_POWERPAIR_TEXT)

    assert len(charges) == 3
    by_label = {charge.charge_label: charge for charge in charges}
    assert by_label["PowerPair Solar Incentive"].rate_value == pytest.approx(0.36)
    assert by_label["PowerPair Solar Incentive"].rate_unit == "$/W"
    assert by_label["PowerPair Battery Incentive Minimum"].rate_value == pytest.approx(240.0)
    assert by_label["PowerPair Battery Incentive Maximum"].rate_value == pytest.approx(400.0)


def test_bulk_extractor_skips_formula_only_premier_power_service(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    is_formula_only = extractor._is_formula_only_document(
        {"family_key": "nc-progress-leaf-660"},
        PROGRESS_PPS_TEXT,
    )

    assert is_formula_only is True


def test_bulk_extractor_skips_formula_only_clean_energy_impact(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    is_formula_only = extractor._is_formula_only_document(
        {"family_key": "nc-progress-leaf-672"},
        """\
RIDER CEI
CLEAN ENERGY IMPACT
MONTHLY RATE
The cost of CEEAs will be set annually.
The market price per block of the CEEA will be based upon the then-current market rate.
An administrative fee not to exceed 20% of the cost of the Clean Energy Environmental Attributes shall apply.
""",
    )

    assert is_formula_only is True
    assert extractor._is_formula_only_document(
        {"family_key": "nc-progress-leaf-672"},
        PROGRESS_CEI_TEXT,
    ) is True


@pytest.mark.parametrize(
    ("family_key", "text"),
    [
        (
            "nc-progress-leaf-712",
            """\
LOW-INCOME WEATHERIZATION PAY FOR PERFORMANCE PROGRAM (PILOT)
PROGRAM
Participants will receive eligible measures.
PAYMENT
Payments will be made to the administering agency based upon estimated savings.
Current kWh based payment levels for installed measures will be posted on the Company's website.
""",
        ),
        (
            "nc-progress-leaf-721",
            """\
RESIDENTIAL SERVICE – TARIFFED ON-BILL PROGRAM
PARTICIPANT CO-PAYMENT
PARTICIPANT REPAYMENT COSTS
Monthly Service Charge = (Total Amount Paid for Measures minus the incentive payment and minus
the Participant Co-Payment) divided by the repayment period.
""",
        ),
        (
            "nc-progress-leaf-723",
            """\
RESIDENTIAL – SMART $AVER® ENERGY EFFICIENCY PROGRAM – EARLY REPLACEMENT AND RETROFIT TOBR
PROGRAM
The current amount of the incentive payment for eligible equipment, products, and services will be posted
to the Company's website at www.duke-energy.com.
""",
        ),
        (
            "nc-progress-leaf-640",
            """\
RESIDENTIAL SERVICE
ENERGY CONSERVATION DISCOUNT
MONTHLY RATE
RECD Credit = (1) 5% times the stated kilowatt and kilowatt-hour charges minus
(2) the kilowatt-hour usage times the Incremental Adjustment Rate
Incremental Adjustment Rate is 0.032 cents per kilowatt-hour.
""",
        ),
        (
            "nc-progress-leaf-663",
            """\
SOLAR REBATE RIDER SRR
For years 2021 and 2022, applications will be accepted within six-month application periods.
An early termination charge shall equal to one minus the number of months since initial participation
divided by one hundred and twenty (120) multiplied by the rebate payment amount.
""",
        ),
    ],
)
def test_bulk_extractor_skips_formula_only_program_schedules(tmp_path, family_key: str, text: str) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    assert extractor._is_formula_only_document({"family_key": family_key}, text) is True


def test_bulk_extractor_skips_formula_only_program_by_title_when_text_missing(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    assert extractor._is_formula_only_document(
        {
            "family_key": "nc-progress-leaf-720",
            "title": "Prepaid Advantage Program PPA (Span 302-303)",
        },
        "",
    ) is True


def test_bulk_extractor_treats_scg_terms_only_continuation_as_reference(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    assert extractor._is_reference_only_document(
        {"family_key": "nc-carolinas-rider-SCG", "title": "SCG continued"},
        CAROLINAS_SCG_TERMS_ONLY_TEXT,
    ) is True


def test_bulk_extractor_treats_incidental_scg_reference_inside_rt_schedule_as_reference(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    assert extractor._is_reference_only_document(
        {"family_key": "nc-carolinas-rider-SCG", "title": "SCG (Span 64-64)"},
        CAROLINAS_RT_WITH_SCG_REFERENCE_TEXT,
    ) is True


def test_bulk_extractor_treats_order_style_title_without_rate_markers_as_reference(tmp_path) -> None:
    extractor = BulkExtractor(tmp_path / "test.db")

    assert extractor._is_reference_only_document(
        {"family_key": "nc-progress-leaf-602", "title": "Approval of Joint Agency Asset Rider JAA"},
        "Order approving rider recovery and procedural findings with no tariff price table or numeric rate values.",
    ) is True


@pytest.mark.parametrize(
    ("family_key", "text", "expected_fixed", "expected_rate"),
    [
        ("nc-carolinas-schedule-RS", CAROLINAS_RS_TEXT, 14.0, 0.122603),
        (
            "nc-carolinas-schedule-ES",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 14
SCHEDULE ES (NC)
RESIDENTIAL SERVICE, ENERGY STAR
RATE:
Basic Facilities Charge per month $12.19
Energy Charges
For the billing months of July - October
For all kWh used per month, per kWh 9.6701¢
""",
            12.19,
            0.096701,
        ),
    ],
)
def test_carolinas_residential_profile_extracts_fixed_and_energy_rates(
    family_key: str,
    text: str,
    expected_fixed: float,
    expected_rate: float,
) -> None:
    profile = CarolinasResidentialFlatProfile()
    doc = {"family_key": family_key, "effective_start": "2026-01-01"}

    charges = profile.extract(doc, text)

    fixed = [c for c in charges if c.charge_type == "fixed"]
    energy = [c for c in charges if c.charge_type == "energy_block"]

    assert len(fixed) == 1
    assert fixed[0].rate_value == pytest.approx(expected_fixed)
    assert fixed[0].rate_unit == "$/month"

    assert len(energy) == 1
    assert energy[0].rate_value == pytest.approx(expected_rate)
    assert energy[0].rate_unit == "$/kWh"


def test_carolinas_rider_adjustment_matrix_profile_extracts_dec_adjustments_and_total() -> None:
    profile = CarolinasRiderAdjustmentMatrixProfile()
    doc = {
        "family_key": "nc-carolinas-rider-SUMMARY",
        "company": "carolinas",
        "effective_start": "2024-01-15",
        "local_path": "dec-leaf99.pdf",
    }

    charges = profile.extract(doc, CAROLINAS_RIDER_SUMMARY_TEXT)

    adjustments = [c for c in charges if c.charge_type == "adjustment"]
    totals = [c for c in charges if c.charge_type == "adjustment_total"]

    assert len(adjustments) >= 12
    assert len(totals) == 1

    by_label = {c.charge_label: c for c in adjustments}
    assert by_label["Residential Schedules - FCA"].rate_value == pytest.approx(0.012682)
    assert by_label["Residential Schedules - EE"].rate_value == pytest.approx(0.003775)
    assert by_label["Residential Schedules - BPM-T"].rate_value == pytest.approx(-0.000039)
    assert by_label["Residential Schedules - EDIT-4"].rate_value == pytest.approx(-0.005081)
    assert by_label["Residential Schedules - CAR"].rate_value == pytest.approx(0.001246)

    assert totals[0].charge_label == "Residential Schedules Total Rider Adjustments"
    assert totals[0].rate_value == pytest.approx(0.012562)
    assert totals[0].rate_unit == "$/kWh"


def test_registry_selects_carolinas_customer_assistance_recovery_profile() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-rider-CAR", "title": "CAR (Span 1-1)", "local_path": "x.pdf"}

    profile = registry.select(doc, CAROLINAS_CAR_TEXT)

    assert isinstance(profile, CarolinasCustomerAssistanceRecoveryProfile)


def test_carolinas_customer_assistance_recovery_extracts_rates() -> None:
    profile = CarolinasCustomerAssistanceRecoveryProfile()
    doc = {"family_key": "nc-carolinas-rider-CAR", "local_path": None}

    charges = profile.extract(doc, CAROLINAS_CAR_TEXT)

    assert len(charges) == 3
    assert sorted(charge.rate_value for charge in charges) == pytest.approx([0.000459, 0.33, 0.33])


def test_registry_selects_carolinas_nptc_profile() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-rider-RIDERNPTC", "title": "RIDER NPTC (Span 1-1)", "local_path": "x.pdf"}

    profile = registry.select(doc, CAROLINAS_NPTC_TEXT)

    assert isinstance(profile, CarolinasNuclearProductionTaxCreditsProfile)


def test_carolinas_nptc_extracts_decremental_rate() -> None:
    profile = CarolinasNuclearProductionTaxCreditsProfile()
    doc = {"family_key": "nc-carolinas-rider-RIDERNPTC", "local_path": None}

    charges = profile.extract(doc, CAROLINAS_NPTC_TEXT)

    assert len(charges) == 1
    assert charges[0].rate_value == pytest.approx(-0.000834)
    assert charges[0].rate_unit == "$/kWh"


def test_registry_selects_carolinas_single_value_rider_profile_for_edpr() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {"family_key": "nc-carolinas-rider-EDPR", "title": "EDPR (Span 3-7)", "local_path": "x.pdf"}

    profile = registry.select(doc, CAROLINAS_EDPR_TEXT)

    assert isinstance(profile, CarolinasSingleValueRiderProfile)


def test_carolinas_single_value_rider_extracts_edpr_rate() -> None:
    profile = CarolinasSingleValueRiderProfile()
    doc = {"family_key": "nc-carolinas-rider-EDPR", "local_path": None}

    charges = profile.extract(doc, CAROLINAS_EDPR_TEXT)

    assert len(charges) == 1
    assert charges[0].rate_value == pytest.approx(-0.000037)
    assert charges[0].rate_unit == "$/kWh"


def test_carolinas_single_value_rider_extracts_bpm_trueup_rate() -> None:
    profile = CarolinasSingleValueRiderProfile()
    doc = {"family_key": "nc-carolinas-rider-BPMPPTTRUEUP", "local_path": None}

    charges = profile.extract(doc, CAROLINAS_BPM_TRUEUP_TEXT)

    assert len(charges) == 1
    assert charges[0].rate_value == pytest.approx(0.000659)
    assert charges[0].rate_unit == "$/kWh"


def test_carolinas_single_value_rider_extracts_bpm_trueup_rate_from_ocr_text() -> None:
    profile = CarolinasSingleValueRiderProfile()
    doc = {"family_key": "nc-carolinas-rider-BPMPPTTRUEUP", "local_path": None}

    charges = profile.extract(doc, CAROLINAS_BPM_TRUEUP_OCR_TEXT)

    assert len(charges) == 1
    assert charges[0].rate_value == pytest.approx(0.000659)


def test_carolinas_rider_adjustment_matrix_profile_falls_back_to_legacy_totals() -> None:
    profile = CarolinasRiderAdjustmentMatrixProfile()
    doc = {
        "family_key": "nc-carolinas-rider-SUMMARY",
        "company": "carolinas",
        "local_path": "dec-leaf99-legacy.pdf",
    }

    charges = profile.extract(doc, CAROLINAS_LEGACY_RIDER_SUMMARY_TEXT)

    totals = {charge.charge_label: charge for charge in charges if charge.charge_type == "adjustment_total"}
    assert totals["Schedule HP - General Service Baseline Total Rider Adjustments"].rate_value == pytest.approx(-0.003336)
    assert totals["Schedule HP - Industrial Incremental Total Rider Adjustments"].rate_value == pytest.approx(0.000235)


def test_carolinas_current_leaf_bridge_profile_extracts_hlf_rates() -> None:
    profile = CarolinasCurrentLeafBridgeProfile()
    doc = {
        "family_key": "nc-carolinas-schedule-HLF",
        "company": "carolinas",
        "local_path": r"data\raw\nc\carolinas\rate\placeholder.pdf",
    }

    charges = profile.extract(doc, CAROLINAS_HLF_TEXT)

    assert len(charges) == 3
    assert {charge.charge_type for charge in charges} == {"fixed", "demand", "energy_block"}


@pytest.mark.parametrize(
    ("family_key", "text", "expected_types"),
    [
        ("nc-carolinas-schedule-PG", CAROLINAS_PG_TEXT, {"fixed", "demand", "energy_block"}),
        ("nc-carolinas-schedule-LGS", CAROLINAS_LGS_TEXT, {"fixed", "demand", "energy_block"}),
        (
            "nc-carolinas-schedule-SGS",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 21
SCHEDULE SGS (NC)
SMALL GENERAL SERVICE
RATE:
Basic Facilities Charge per month $12.00
Energy Charge
For all kWh used per month, per kWh 7.5000¢
""",
            {"fixed", "energy_block"},
        ),
    ],
)
def test_carolinas_general_service_schedule_profile_extracts_core_rates(
    family_key: str,
    text: str,
    expected_types: set[str],
) -> None:
    profile = CarolinasGeneralServiceScheduleProfile()
    doc = {"family_key": family_key, "company": "carolinas"}

    charges = profile.extract(doc, text)

    assert charges
    assert {charge.charge_type for charge in charges}.issuperset(expected_types)


@pytest.mark.parametrize(
    ("family_key", "text", "expected_types"),
    [
        (
            "nc-carolinas-schedule-I",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 41
SCHEDULE I (NC)
INDUSTRIAL SERVICE
RATE:
Basic Facilities Charge per month $19.91
Demand Charge
For all over 30 kW of Billing Demand per month, per kW $ 4.6197
Energy Charge
For all kWh per month, per kWh 5.3653¢
""",
            {"fixed", "demand", "energy_block"},
        ),
        (
            "nc-carolinas-doc-SCHEDULEOPTE",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 45
SCHEDULE OPT-E (NC)
OPTIONAL POWER SERVICE TIME-OF-USE ENERGY-ONLY (PILOT)
RATE:
Basic Facilities Charge per month $34.31
On-Peak Energy per month, per kWh 18.9376¢
All Off-Peak Energy per month, per kWh 3.4993¢
""",
            {"fixed", "tou_energy"},
        ),
        (
            "nc-carolinas-schedule-TS",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 38
SCHEDULE TS (NC)
TRAFFIC SIGNAL SERVICE
RATE:
Basic Facilities Charge per month $6.92
For the first 50 kWh used per month, per kWh 19.4905¢
For all over 50 kWh used per month, per kWh 7.6859¢
""",
            {"fixed", "energy_block"},
        ),
        (
            "nc-carolinas-doc-SCHEDULEWC",
            """\
Duke Energy Carolinas, LLC
NC Revised Leaf No. 17
SCHEDULE WC (NC)
RESIDENTIAL WATER HEATING SERVICE
RATE:
Basic Facilities Charge per month $1.77
All kWh per month, per kWh 4.8238¢
""",
            {"fixed", "energy_block"},
        ),
    ],
)
def test_carolinas_schedule_bridge_profile_extracts_core_rates(
    family_key: str,
    text: str,
    expected_types: set[str],
) -> None:
    profile = CarolinasScheduleBridgeProfile()
    doc = {"family_key": family_key, "company": "carolinas"}

    charges = profile.extract(doc, text)

    assert charges
    assert {charge.charge_type for charge in charges}.issuperset(expected_types)


def test_carolinas_small_customer_generator_profile_extracts_monthly_charges() -> None:
    profile = CarolinasSmallCustomerGeneratorProfile()
    doc = {"family_key": "nc-carolinas-rider-SCG", "company": "carolinas"}

    charges = profile.extract(doc, CAROLINAS_SCG_TEXT)

    assert len(charges) == 2
    labels = {charge.charge_label: charge for charge in charges}
    assert labels["Supplemental Basic Customer Charge"].rate_value == pytest.approx(3.92)
    assert labels["Standby Charge"].rate_value == pytest.approx(1.92)


def test_carolinas_lighting_schedule_profile_extracts_pl_and_fl_rates() -> None:
    profile = CarolinasLightingScheduleProfile()

    pl_doc = {
        "family_key": "nc-carolinas-doc-SCHEDULEPLSTREETANDPUBLICLIGHTINGSERVICE",
        "company": "carolinas",
    }
    pl_charges = profile.extract(pl_doc, CAROLINAS_PL_TEXT)
    assert len(pl_charges) >= 10
    pl_labels = {charge.charge_label: charge for charge in pl_charges}
    assert pl_labels["High Pressure Sodium Vapor - Suburban (1) - Inside Municipal Limits"].rate_value == pytest.approx(8.40)
    assert pl_labels["High Pressure Sodium Vapor - Suburban (1) - Outside Municipal Limits"].rate_value == pytest.approx(8.85)
    assert pl_labels["Metal Halide - Urban - Inside Municipal Limits"].rate_value == pytest.approx(18.66)

    fl_doc = {
        "family_key": "nc-carolinas-doc-SCHEDULEFLFLOODLIGHTINGSERVICE",
        "company": "carolinas",
    }
    fl_charges = profile.extract(fl_doc, CAROLINAS_FL_TEXT)
    assert len(fl_charges) >= 10
    fl_labels = {charge.charge_label: charge for charge in fl_charges}
    assert fl_labels["Floodlighting - High Pressure Sodium Vapor - Floodlight - Existing Pole"].rate_value == pytest.approx(14.56)
    assert fl_labels["Floodlighting - High Pressure Sodium Vapor - Floodlight - New Pole"].rate_value == pytest.approx(22.56)
    assert fl_labels["Floodlighting - High Pressure Sodium Vapor - Floodlight - Underground"].rate_value == pytest.approx(27.33)


def test_carolinas_lighting_schedule_profile_extracts_ol_yl_and_gl_rates() -> None:
    profile = CarolinasLightingScheduleProfile()

    ol_doc = {
        "family_key": "nc-carolinas-schedule-OL",
        "company": "carolinas",
    }
    ol_charges = profile.extract(ol_doc, CAROLINAS_OL_TEXT)
    ol_labels = {charge.charge_label: charge for charge in ol_charges}
    assert ol_labels["High Pressure Sodium Vapor - Urban - Existing Pole"].rate_value == pytest.approx(9.89)
    assert ol_labels["High Pressure Sodium Vapor - Urban - Underground"].rate_value == pytest.approx(11.97)
    assert ol_labels["Metal Halide - Area - New Pole"].rate_value == pytest.approx(19.54)

    yl_doc = {
        "family_key": "nc-carolinas-doc-SCHEDULEYLYARDLIGHTINGSERVICE",
        "company": "carolinas",
    }
    yl_charges = profile.extract(yl_doc, CAROLINAS_YL_TEXT)
    yl_labels = {charge.charge_label: charge for charge in yl_charges}
    assert yl_labels["Yard Lighting - 250 Watt high pressure sodium vapor attached to existing Company secondary pole"].rate_value == pytest.approx(16.54)
    assert yl_labels["Yard Lighting - Special yard lighting pole (30 ft. wood) used only for the support of yard lighting and one span of secondary"].rate_value == pytest.approx(2.25)

    gl_doc = {
        "family_key": "nc-carolinas-doc-GOVERNMENTALLIGHTINGSERVICE",
        "company": "carolinas",
    }
    gl_charges = profile.extract(gl_doc, CAROLINAS_GL_TEXT)
    gl_labels = {charge.charge_label: charge for charge in gl_charges}
    assert gl_labels["High Pressure Sodium Vapor - Urban - Existing Pole"].rate_value == pytest.approx(9.89)
    assert gl_labels["Metal Halide - Urban - Underground"].rate_value == pytest.approx(13.41)

    pl_alias_doc = {
        "family_key": "nc-carolinas-schedule-PL",
        "company": "carolinas",
    }
    assert profile.extract(pl_alias_doc, CAROLINAS_PL_TEXT)

    fl_alias_doc = {
        "family_key": "nc-carolinas-doc-FLOODLIGHTINGSERVICE",
        "company": "carolinas",
    }
    assert profile.extract(fl_alias_doc, CAROLINAS_FL_TEXT)


def test_carolinas_net_metering_rider_profile_extracts_targeted_terms() -> None:
    profile = CarolinasNetMeteringRiderProfile()
    doc = {
        "family_key": "nc-carolinas-rider-NM",
        "company": "carolinas",
    }

    charges = profile.extract(doc, CAROLINAS_NM_CURRENT_TEXT)

    labels = {charge.charge_label: charge for charge in charges}
    assert labels["Standby Charge"].rate_value == pytest.approx(1.7235)
    assert labels["Standby Charge"].rate_unit == "$/kW-month"
    assert labels["Minimum Bill Adder"].rate_value == pytest.approx(10.0)


def test_carolinas_energy_efficiency_rider_profile_extracts_old_and_total_rates() -> None:
    profile = CarolinasEnergyEfficiencyRiderProfile()
    doc = {
        "family_key": "nc-carolinas-rider-EE",
        "company": "carolinas",
    }

    old_charges = profile.extract(doc, CAROLINAS_EE_OLD_TEXT)
    old_labels = {charge.charge_label: charge for charge in old_charges}
    assert old_labels["Residential Rider EE"].rate_value == pytest.approx(0.001206)
    assert old_labels["Nonresidential Rider EE"].rate_value == pytest.approx(0.000428)

    total_charges = profile.extract(doc, CAROLINAS_EE_TOTALS_TEXT)
    total_labels = {charge.charge_label: charge for charge in total_charges}
    assert total_labels["Residential Total Rider EE"].rate_value == pytest.approx(0.004291)
    assert total_labels["Nonresidential Total Rider EE"].rate_value == pytest.approx(0.004822)


def test_carolinas_economic_development_rider_profile_extracts_percentage_schedule() -> None:
    profile = CarolinasEconomicDevelopmentRiderProfile()
    doc = {
        "family_key": "nc-carolinas-rider-EC",
        "company": "carolinas",
    }

    charges = profile.extract(doc, CAROLINAS_EC_TEXT)

    labels = {charge.charge_label: charge for charge in charges}
    assert labels["Months 1-12 Rider Credit"].rate_value == pytest.approx(0.20)
    assert labels["Months 13-24 Rider Credit"].rate_value == pytest.approx(0.15)
    assert labels["After Month 48 Rider Credit"].rate_value == pytest.approx(0.0)


def test_carolinas_interruptible_service_rider_profile_extracts_credit_and_penalty() -> None:
    profile = CarolinasInterruptibleServiceRiderProfile()
    doc = {
        "family_key": "nc-carolinas-rider-IS",
        "company": "carolinas",
    }

    charges = profile.extract(doc, CAROLINAS_IS_TEXT)

    labels = {charge.charge_label: charge for charge in charges}
    assert labels["Interruptible Credit"].rate_value == pytest.approx(3.5)
    assert labels["Penalty Charge"].rate_value == pytest.approx(10.0)


def test_green_source_advantage_rider_profile_extracts_admin_charges() -> None:
    profile = GreenSourceAdvantageRiderProfile()
    doc = {
        "family_key": "nc-carolinas-rider-GSA",
        "company": "carolinas",
    }

    charges = profile.extract(doc, GSA_TEXT)

    labels = {charge.charge_label: charge for charge in charges}
    assert labels["Application Fee"].rate_value == pytest.approx(2000.0)
    assert labels["GSA Administrative Charge"].rate_value == pytest.approx(375.0)
    assert labels["Additional Account Charge"].rate_value == pytest.approx(50.0)


def test_bulk_extractor_skips_nm_continuation_reference_page(tmp_path) -> None:
    db_path = tmp_path / "duke_rates.db"
    extractor = BulkExtractor(str(db_path))

    should_skip = extractor._is_reference_only_document(
        {"family_key": "nc-carolinas-rider-NM", "title": "NM continuation"},
        CAROLINAS_NM_CONTINUATION_TEXT,
    )

    assert should_skip is True


def test_bulk_extractor_skips_formula_only_rider_ee_page(tmp_path) -> None:
    db_path = tmp_path / "duke_rates.db"
    extractor = BulkExtractor(str(db_path))

    should_skip = extractor._is_formula_only_document(
        {"family_key": "nc-carolinas-rider-EE", "title": "EE formula page"},
        """Duke Energy Carolinas, LLC
        RIDER EE (NC)
        ENERGY EFFICIENCY RIDER
        EEA Residential (expressed as cents per kWh) =
        DETERMINATION OF ENERGY EFFICIENCY RIDER ADJUSTMENT
        """,
    )

    assert should_skip is True


@pytest.mark.parametrize(
    ("family_key", "text", "expected_count", "expected_labels"),
    [
        (
            "nc-carolinas-rider-NMB",
            CAROLINAS_NMB_TEXT,
            3,
            {"Net Excess Energy Credit", "Non-Bypassable Charge", "Minimum Bill"},
        ),
        (
            "nc-carolinas-rider-NSC",
            CAROLINAS_NSC_TEXT,
            2,
            {"Monthly Credit for Net Excess Energy", "Standby Charge"},
        ),
    ],
)
def test_carolinas_solar_choice_rider_profile_extracts_targeted_values(
    family_key: str,
    text: str,
    expected_count: int,
    expected_labels: set[str],
) -> None:
    profile = CarolinasSolarChoiceRiderProfile()
    doc = {
        "family_key": family_key,
        "company": "carolinas",
        "local_path": r"data\raw\nc\carolinas\rider\placeholder.pdf",
    }

    charges = profile.extract(doc, text)

    assert len(charges) == expected_count
    assert {charge.charge_label for charge in charges} == expected_labels
    assert all(charge.rate_value is not None for charge in charges)


def test_bulk_extractor_lists_progress_and_carolinas_documents(tmp_path) -> None:
    conn = connect(tmp_path / "historical.db")
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    rows = []
    for family_key, title, company, local_name, content_hash in [
        ("nc-progress-leaf-500", "Progress Residential", "progress", "progress-500.pdf", "hash-progress"),
        ("nc-carolinas-schedule-RS", "Carolinas Residential", "carolinas", "carolinas-rs.pdf", "hash-carolinas"),
    ]:
        local_path = str(tmp_path / local_name)
        historical_id = conn.execute(
            """
            INSERT INTO historical_documents (
                family_key, title, state, company, category, kind,
                canonical_url, archived_url, snapshot_timestamp,
                local_path, content_hash, effective_start, retrieved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                family_key,
                title,
                "NC",
                company,
                "rate",
                "pdf",
                f"https://example.test/{local_name}",
                f"https://archive.test/{local_name}",
                "2026-03-26T00:00:00Z",
                local_path,
                content_hash,
                "2025-01-01",
                now,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO tariff_versions (
                family_key, historical_document_id, effective_start, source_type,
                confidence_score, created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                family_key,
                historical_id,
                "2025-01-01",
                "historical_ncuc",
                0.9,
                now,
            ),
        )
        rows.append((family_key, title))
    conn.commit()
    conn.close()

    docs = BulkExtractor(str(tmp_path / "historical.db")).get_documents_needing_extraction()

    assert {doc["company"] for doc in docs} == {"progress", "carolinas"}
    assert {doc["family_key"] for doc in docs} == {
        "nc-progress-leaf-500",
        "nc-carolinas-schedule-RS",
    }


def test_bulk_extractor_persists_profile_selection_diagnostics(tmp_path) -> None:
    db_path = tmp_path / "historical.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "tou-history.txt.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-502",
            "NC",
            "progress",
            "leaf-502",
            "R_TOU",
            "rate_schedule",
            "Progress R-TOU",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-502",
            "Progress R-TOU",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/progress-502.pdf",
            "https://archive.test/progress-502",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-progress-502",
            "2024-01-01",
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-502",
            historical_id,
            "2024-01-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (TOU_SEASONAL_TEXT, "test")

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-502"
    assert inserted >= 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT id, parser_profile, status, confidence, utility, charge_count, metadata_json, review_flags_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_residential_tou"
    assert row["status"] == "parsed"
    assert row["utility"] == "DEP"
    assert row["confidence"] > 0.8
    assert row["charge_count"] >= 1
    assert json.loads(row["review_flags_json"]) == []

    metadata = json.loads(row["metadata_json"])
    assert metadata["family_key"] == "nc-progress-leaf-502"
    assert metadata["outcome_quality"] == "strong"
    assert metadata["candidate_profiles"][0]["name"] == "progress_residential_tou"
    assert "progress_tou_family" in metadata["candidate_profiles"][0]["reasons"]

    review = check.execute(
        """
        SELECT parse_attempt_id, review_source, outcome, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["parse_attempt_id"] == row["id"]
    assert review["review_source"] == "rule"
    assert review["outcome"] == "accepted"
    review_notes = json.loads(review["notes_json"])
    assert review_notes["outcome_quality"] == "strong"
    assert review_notes["family_key"] == "nc-progress-leaf-502"

    fingerprint = check.execute(
        """
        SELECT text_length, line_count, numeric_line_count, metadata_json
        FROM document_fingerprints
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert fingerprint is not None
    assert fingerprint["text_length"] > 0
    assert fingerprint["line_count"] > 0
    fp_metadata = json.loads(fingerprint["metadata_json"])
    assert fp_metadata["family_key"] == "nc-progress-leaf-502"
    assert fp_metadata["parser_profile"] == "progress_residential_tou"
    assert fp_metadata["outcome_quality"] == "strong"
    assert fp_metadata["signals"]["has_tou_terms"] is True
    assert fp_metadata["signals"]["has_progress_company_text"] is True
    check.close()


def test_bulk_extractor_uses_progress_billing_adjustments_profile_for_leaf_601(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "historical-ba.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "leaf-601-ba.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-601",
            "NC",
            "progress",
            "leaf-601",
            "RIDER_BA_RY1",
            "rider",
            "Annual Billing Adjustments Rider BA",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-601",
            "Annual Billing Adjustments Rider BA",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/progress-601.pdf",
            "https://archive.test/progress-601",
            "2026-03-26T00:00:00Z",
            str(pdf_path),
            "hash-progress-601",
            "2026-01-01",
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-601",
            historical_id,
            "2026-01-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (PROGRESS_BA_TEXT, "test")
    monkeypatch.setattr(
        parser_profiles_module,
        "parse_nc_progress_leaf_file",
        lambda path, *, version_id, family_key, document_id=None: parser_profiles_module.parse_nc_progress_leaf(
            PROGRESS_BA_TEXT,
            version_id=version_id,
            family_key=family_key,
            document_id=document_id,
        ),
    )

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-601"
    assert inserted == 5

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_billing_adjustments"
    assert row["status"] == "parsed"
    assert row["charge_count"] == 5
    metadata = json.loads(row["metadata_json"])
    assert metadata["outcome_quality"] == "strong"
    assert metadata["candidate_profiles"][0]["name"] == "progress_billing_adjustments"
    assert "family=leaf601" in metadata["candidate_profiles"][0]["reasons"]

    charges = check.execute(
        """
        SELECT charge_type, charge_label, rate_value, rate_unit
        FROM tariff_charges
        WHERE version_id = (SELECT id FROM tariff_versions WHERE historical_document_id = ?)
        ORDER BY id
        """,
        (historical_id,),
    ).fetchall()
    assert len(charges) == 5
    assert {row["charge_label"] for row in charges} == {
        "Billing Adjustment - Residential",
        "Billing Adjustment - Small General Service",
        "Billing Adjustment - Medium General Service",
        "Billing Adjustment - Large General Service",
        "Billing Adjustment - Lighting",
    }
    assert all(row["charge_type"] == "adjustment" for row in charges)
    assert all(row["rate_unit"] == "$/kWh" for row in charges)
    check.close()


def test_bulk_extractor_marks_generic_fallback_parse_as_weak(tmp_path) -> None:
    db_path = tmp_path / "historical.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "generic-history.txt.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
            (
                "nc-progress-leaf-590",
                "NC",
                "progress",
                "leaf-590",
                "RES",
                "rate_schedule",
                "Progress Residential",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                "nc-progress-leaf-590",
                "Progress Residential",
                "NC",
                "progress",
            "rate",
            "pdf",
            "https://example.test/progress-500.pdf",
            "https://archive.test/progress-500",
            "2026-03-26T00:00:00Z",
                str(pdf_path),
                "hash-progress-590",
                "2024-01-01",
                now,
            ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
            (
                "nc-progress-leaf-590",
                historical_id,
                "2024-01-01",
                "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (GENERIC_RESIDENTIAL_TEXT, "test")

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-590"
    assert inserted >= 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT id, parser_profile, status, metadata_json, review_flags_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "generic_residential"
    assert row["status"] == "parsed"
    assert "generic_fallback_selected" in json.loads(row["review_flags_json"])
    metadata = json.loads(row["metadata_json"])
    assert metadata["outcome_quality"] == "weak"

    review = check.execute(
        """
        SELECT parse_attempt_id, review_source, outcome, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["parse_attempt_id"] == row["id"]
    assert review["review_source"] == "rule"
    assert review["outcome"] == "needs_review"
    assert json.loads(review["notes_json"])["outcome_quality"] == "weak"

    fingerprint = check.execute(
        """
        SELECT metadata_json, review_flags_json
        FROM document_fingerprints
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert fingerprint is not None
    fp_metadata = json.loads(fingerprint["metadata_json"])
    assert fp_metadata["parser_profile"] == "generic_residential"
    assert fp_metadata["outcome_quality"] == "weak"
    assert "generic_fallback_selected" in json.loads(fingerprint["review_flags_json"])
    check.close()


def test_bulk_extractor_forces_bounded_leaf_609_tariff_despite_order_markers(tmp_path) -> None:
    db_path = tmp_path / "historical-esm.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 27, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "leaf-609-esm.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-609",
            "NC",
            "progress",
            "leaf-609",
            "RIDER_ESM_RY1",
            "rider",
            "Rider ESM",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, start_page, end_page, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-609",
            "Rider ESM (Earnings Sharing Mechanism) (Span 15-16)",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/progress-609.pdf",
            "https://archive.test/progress-609",
            "2026-03-27T00:00:00Z",
            str(pdf_path),
            "hash-progress-609",
            "2025-04-01",
            15,
            16,
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-609",
            historical_id,
            "2025-04-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (PROGRESS_ESM_TEXT, "test")

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-609"
    assert inserted >= 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_single_value_rider"
    assert row["status"] == "parsed"
    assert row["charge_count"] >= 1
    metadata = json.loads(row["metadata_json"])
    assert metadata["outcome_quality"] == "strong"

    review = check.execute(
        """
        SELECT outcome, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["outcome"] == "accepted"
    assert json.loads(review["notes_json"])["outcome_quality"] == "strong"

    charge = check.execute(
        """
        SELECT charge_type, rate_value, rate_unit
        FROM tariff_charges
        WHERE version_id = (SELECT id FROM tariff_versions WHERE historical_document_id = ?)
        ORDER BY id
        LIMIT 1
        """,
        (historical_id,),
    ).fetchone()
    assert charge is not None
    assert charge["charge_type"] == "adjustment"
    assert charge["rate_unit"] == "$/kWh"

    fingerprint = check.execute(
        """
        SELECT metadata_json, review_flags_json
        FROM document_fingerprints
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert fingerprint is not None
    assert json.loads(fingerprint["metadata_json"])["outcome_quality"] == "strong"
    assert json.loads(fingerprint["review_flags_json"]) == []
    check.close()


def test_bulk_extractor_forces_tariff_for_authenticated_portal_progress_rider(tmp_path) -> None:
    jaa_text = """\
Duke Energy Progress, LLC NC Third Revised Leaf No. 602
JOINT AGENCY ASSET RIDER JAA
MONTHLY RATE
The incremental rider for each rate class as follows:
Rate Class Applicable Schedule(s) Incremental Rate*
Non-Demand Rate Class (dollars per kilowatt-hour)
Residential RES, R-TOUD, R-TOU, R-TOU-CPP 0.00464
Small General Service SGS, SGS-TOUE, SGS-TOU-CPP 0.00223
Outdoor Lighting Service ALS, SLS, SLR, SFLS 0.01389
Demand Rate Classes (dollars per kilowatt)
Medium General Service MGS, GS-TES, APH-TES, MGS-TOU 0.92
Large General Service LGS, LGS-TOU, LGS-HLF 3.03
Effective for bills rendered on and after December 1, 2025
"""
    db_path = tmp_path / "authenticated-rider.db"
    conn = connect(db_path)
    now = datetime(2026, 4, 7, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "11-24-2025_auth_portal_dep_nc_rider_jaa_rev_3.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-602",
        company="progress",
        title="DEP NC Rider JAA Rev. 3 Eff. 12.01.2025 (Current)",
        local_path=str(pdf_path),
        effective_start="2025-12-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (jaa_text, "test")
    extractor.classifier.classify = lambda *args, **kwargs: "other"

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-602"
    assert inserted >= 4

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_single_value_rider"
    assert row["status"] == "parsed"
    assert row["charge_count"] >= 4
    check.close()


def test_bulk_extractor_uses_progress_current_leaf_bridge_for_unbounded_current_leaf_501(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "historical-current-501.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 27, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "data" / "raw" / "nc" / "progress" / "rate" / "leaf-no-501.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            "NC",
            "progress",
            "leaf-501",
            "R_TOUD",
            "rate_schedule",
            "Schedule R-TOUD",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            "Schedule R-TOUD",
            "NC",
            "progress",
            "rate",
            "pdf",
            "https://example.test/progress-501.pdf",
            "https://archive.test/progress-501",
            "2026-03-27T00:00:00Z",
            str(pdf_path),
            "hash-progress-501",
            "2025-04-01",
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-501",
            historical_id,
            "2025-04-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (PROGRESS_R_TOUD_TEXT, "test")
    monkeypatch.setattr(
        parser_profiles_module,
        "parse_nc_progress_leaf_file",
        lambda path, version_id, family_key: parser_profiles_module.parse_nc_progress_leaf(
            PROGRESS_R_TOUD_TEXT,
            version_id=version_id,
            family_key=family_key,
        ),
    )

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-501"
    assert inserted >= 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count, review_flags_json, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_current_leaf_bridge"
    assert row["status"] == "parsed"
    assert row["charge_count"] == 5
    assert json.loads(row["review_flags_json"]) == []
    metadata = json.loads(row["metadata_json"])
    assert metadata["outcome_quality"] == "strong"
    assert metadata["selection"]["final_parser_profile"] == "progress_current_leaf_bridge"

    review = check.execute(
        """
        SELECT outcome, review_source, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["outcome"] == "accepted"
    assert review["review_source"] == "rule"
    assert json.loads(review["notes_json"])["outcome_quality"] == "strong"
    check.close()


def test_bulk_extractor_uses_progress_specialty_rider_for_unbounded_current_leaf_668(tmp_path) -> None:
    db_path = tmp_path / "historical-current-668.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 27, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "data" / "raw" / "nc" / "progress" / "rider" / "leaf-no-668.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_text("placeholder", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO tariff_families (
            family_key, state, company, tariff_identifier, schedule_code,
            family_type, title, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-668",
            "NC",
            "progress",
            "leaf-668",
            "RIDER_NSC_RY1",
            "rider",
            "Rider NSC",
            now,
            now,
        ),
    )
    cur = conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp,
            local_path, content_hash, effective_start, retrieved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-668",
            "Rider NSC",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/progress-668.pdf",
            "https://archive.test/progress-668",
            "2026-03-27T00:00:00Z",
            str(pdf_path),
            "hash-progress-668",
            "2025-01-01",
            now,
        ),
    )
    historical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tariff_versions (
            family_key, historical_document_id, effective_start, source_type,
            confidence_score, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "nc-progress-leaf-668",
            historical_id,
            "2025-01-01",
            "historical_ncuc",
            0.9,
            now,
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (PROGRESS_NSC_TEXT, "test")

    doc = extractor.get_documents_needing_extraction()[0]
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-668"
    assert inserted >= 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count, review_flags_json, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_specialty_rider"
    assert row["status"] == "parsed"
    assert row["charge_count"] == 1
    assert json.loads(row["review_flags_json"]) == []
    metadata = json.loads(row["metadata_json"])
    assert metadata["outcome_quality"] == "strong"

    review = check.execute(
        """
        SELECT outcome, review_source, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["outcome"] == "accepted"
    assert review["review_source"] == "rule"
    assert json.loads(review["notes_json"])["outcome_quality"] == "strong"
    check.close()


def test_bulk_extractor_applies_fallback_when_initial_profile_extracts_nothing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fallback.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "tou-fallback.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-502",
        company="progress",
        title="Progress R-TOU",
        local_path=str(pdf_path),
        effective_start="2024-01-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (TOU_SEASONAL_TEXT, "test")

    progress_profile = extractor.parser_registry.get_profile("progress_residential_tou")
    generic_profile = extractor.parser_registry.get_profile("generic_residential")
    assert progress_profile is not None
    assert generic_profile is not None

    monkeypatch.setattr(progress_profile, "extract", lambda doc, text: [])
    monkeypatch.setattr(
        generic_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed_charge",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.75,
            )
        ],
    )

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    doc_id, family_key, inserted, _, _ = extractor.process_document(doc)

    assert doc_id == historical_id
    assert family_key == "nc-progress-leaf-502"
    assert inserted == 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, status, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "generic_residential"
    assert row["status"] == "parsed"
    assert row["charge_count"] == 1

    metadata = json.loads(row["metadata_json"])
    selection = metadata["selection"]
    assert selection["initial_parser_profile"] == "progress_residential_tou"
    assert selection["final_parser_profile"] == "generic_residential"
    assert selection["fallback_applied"] is True
    assert selection["fallback_triggered_by"] == "empty"
    assert selection["fallback_reason"] == "empty_initial_parse"
    assert selection["fallback_candidates"][0]["name"] == "generic_residential"
    assert selection["fallback_attempts"][0]["name"] == "generic_residential"


def test_bulk_extractor_filters_to_versioned_documents_and_reports_missing_versions(tmp_path) -> None:
    db_path = tmp_path / "version_filter.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()

    versioned_pdf = tmp_path / "versioned.pdf"
    versioned_pdf.write_text("placeholder", encoding="utf-8")
    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-500",
        company="progress",
        title="Residential Service",
        local_path=str(versioned_pdf),
        effective_start="2024-01-01",
        now=now,
    )

    unversioned_pdf = tmp_path / "unversioned.pdf"
    unversioned_pdf.write_text("placeholder", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO historical_documents (
            family_key, title, state, company, category, kind,
            canonical_url, archived_url, snapshot_timestamp, local_path,
            content_hash, content_type, direct_status_code, direct_downloadable,
            effective_start, retrieved_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "nc-progress-leaf-501",
            "Fuel Charge Adjustment",
            "NC",
            "progress",
            "rider",
            "pdf",
            "https://example.test/unversioned",
            "https://archive.example.test/unversioned",
            now,
            str(unversioned_pdf),
            "hash-unversioned",
            "application/pdf",
            200,
            1,
            "2024-02-01",
            now,
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (GENERIC_RESIDENTIAL_TEXT, "test")

    results = extractor.run_extraction(max_workers=1)

    assert results["total_documents"] == 1
    assert results["documents_missing_versions"] == 1
    assert results["documents_processed"] == 1
    assert results["total_charges_inserted"] >= 1

    check = connect(db_path)
    rows = check.execute(
        """
        SELECT DISTINCT historical_document_id
        FROM tariff_versions
        WHERE historical_document_id IS NOT NULL
        """
    ).fetchall()
    assert [row[0] for row in rows] == [historical_id]
    check.close()


def test_bulk_extractor_skips_reference_only_program_families_as_accepted(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "reference-only.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "leaf-740-program.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-740",
        company="progress",
        title="Electric Vehicle School Bus Charging Station Program EVSB (NC Pilot)",
        local_path=str(pdf_path),
        effective_start="2024-01-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (
        (
            "Electric Vehicle School Bus Charging Station Program EVSB (NC Pilot)\n"
            "Program Eligibility\n"
            "Pilot terms and participation requirements.\n"
        ),
        "test",
    )
    monkeypatch.setattr(extractor, "classify_document", lambda title, text_sample: "tariff")

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    _, _, inserted, _, _ = extractor.process_document(doc)
    assert inserted == 0

    check = connect(db_path)
    attempt = check.execute(
        """
        SELECT parser_profile, status, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert attempt is not None
    assert attempt["parser_profile"] is None
    assert attempt["status"] == "skipped_reference"
    assert attempt["charge_count"] == 0
    selection = json.loads(attempt["metadata_json"])["selection"]
    assert selection["skip_reason"] == "reference_only_family"

    review = check.execute(
        """
        SELECT outcome, notes_json
        FROM parse_review_outcomes
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert review is not None
    assert review["outcome"] == "accepted"
    notes = json.loads(review["notes_json"])
    assert notes["status"] == "skipped_reference"
    assert notes["outcome_quality"] == "skipped"
    check.close()


def test_registry_selects_zero_charge_program_for_program_only_families() -> None:
    registry = HistoricalRateParserRegistry()
    doc = {
        "family_key": "nc-progress-program-SCHOOLSPROGRAM",
        "title": "Schools Program",
    }
    selected = registry.select(doc, "Schools Program Terms and Conditions")
    assert selected.name == "zero_charge_program"
    assert selected.extract(doc, "Schools Program Terms and Conditions") == []


def test_bulk_extractor_applies_fallback_on_weak_parse_only_when_materially_better(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "weak-fallback.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "tou-weak-fallback.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-502",
        company="progress",
        title="Progress R-TOU",
        local_path=str(pdf_path),
        effective_start="2024-01-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (TOU_SEASONAL_TEXT, "test")

    progress_profile = extractor.parser_registry.get_profile("progress_residential_tou")
    generic_profile = extractor.parser_registry.get_profile("generic_residential")
    assert progress_profile is not None
    assert generic_profile is not None

    monkeypatch.setattr(
        progress_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed_charge",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.75,
            )
        ],
    )
    monkeypatch.setattr(
        generic_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed_charge",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.75,
            ),
            ExtractedCharge(
                charge_type="tou_energy",
                charge_label="On Peak Energy Charge",
                rate_value=0.29905,
                rate_unit="$/kWh",
                season="summer",
                tou_period="on_peak",
                tier_min=None,
                tier_max=None,
                source_snippet="On-Peak",
                confidence_score=0.80,
            ),
            ExtractedCharge(
                charge_type="tou_energy",
                charge_label="Off Peak Energy Charge",
                rate_value=0.11321,
                rate_unit="$/kWh",
                season="summer",
                tou_period="off_peak",
                tier_min=None,
                tier_max=None,
                source_snippet="Off-Peak",
                confidence_score=0.80,
            ),
        ],
    )

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    _, _, inserted, _, _ = extractor.process_document(doc)
    assert inserted == 3

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "generic_residential"
    assert row["charge_count"] == 3
    selection = json.loads(row["metadata_json"])["selection"]
    assert selection["fallback_applied"] is True
    assert selection["fallback_triggered_by"] == "weak"
    assert selection["fallback_reason"] == "material_charge_gain"
    assert selection["initial_outcome_quality"] == "weak"
    assert selection["final_outcome_quality"] == "weak"
    assert selection["fallback_attempts"][0]["applied"] is True
    check.close()


def test_bulk_extractor_does_not_apply_fallback_for_marginal_weak_parse_gain(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "weak-no-fallback.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "tou-weak-no-fallback.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-502",
        company="progress",
        title="Progress R-TOU",
        local_path=str(pdf_path),
        effective_start="2024-01-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (TOU_SEASONAL_TEXT, "test")

    progress_profile = extractor.parser_registry.get_profile("progress_residential_tou")
    generic_profile = extractor.parser_registry.get_profile("generic_residential")
    assert progress_profile is not None
    assert generic_profile is not None

    monkeypatch.setattr(
        progress_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed_charge",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.75,
            )
        ],
    )
    monkeypatch.setattr(
        generic_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed_charge",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.75,
            ),
            ExtractedCharge(
                charge_type="tou_energy",
                charge_label="On Peak Energy Charge",
                rate_value=0.29905,
                rate_unit="$/kWh",
                season="summer",
                tou_period="on_peak",
                tier_min=None,
                tier_max=None,
                source_snippet="On-Peak",
                confidence_score=0.80,
            ),
        ],
    )

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    _, _, inserted, _, _ = extractor.process_document(doc)
    assert inserted == 1

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_residential_tou"
    assert row["charge_count"] == 1
    selection = json.loads(row["metadata_json"])["selection"]
    assert selection["fallback_applied"] is False
    assert selection["fallback_triggered_by"] == "weak"
    assert selection["fallback_reason"] is None
    assert selection["fallback_attempts"][0]["applied"] is False
    assert selection["fallback_attempts"][0]["apply_reason"] is None
    check.close()


def test_bulk_extractor_applies_fallback_for_same_count_but_better_charge_type_coverage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "weak-coverage-fallback.db"
    conn = connect(db_path)
    now = datetime(2026, 3, 26, tzinfo=UTC).isoformat()
    pdf_path = tmp_path / "weak-coverage-fallback.pdf"
    pdf_path.write_text("placeholder", encoding="utf-8")

    historical_id = _seed_historical_doc_with_version(
        conn,
        family_key="nc-progress-leaf-502",
        company="progress",
        title="Progress R-TOU",
        local_path=str(pdf_path),
        effective_start="2024-01-01",
        now=now,
    )
    conn.commit()
    conn.close()

    extractor = BulkExtractor(str(db_path))
    extractor.extract_text_from_pdf = lambda *args, **kwargs: (TOU_SEASONAL_TEXT, "test")

    progress_profile = extractor.parser_registry.get_profile("progress_residential_tou")
    generic_profile = extractor.parser_registry.get_profile("generic_residential")
    assert progress_profile is not None
    assert generic_profile is not None
    monkeypatch.setattr(extractor.parser_registry, "select", lambda doc, text: generic_profile)

    monkeypatch.setattr(
        progress_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="fixed",
                charge_label="Basic Customer Charge",
                rate_value=14.0,
                rate_unit="$/month",
                season="all_year",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Basic Customer Charge",
                confidence_score=0.80,
            ),
            ExtractedCharge(
                charge_type="tou_energy",
                charge_label="On Peak Energy Charge",
                rate_value=0.29905,
                rate_unit="$/kWh",
                season="summer",
                tou_period="on_peak",
                tier_min=None,
                tier_max=None,
                source_snippet="On-Peak",
                confidence_score=0.82,
            ),
        ],
    )
    monkeypatch.setattr(
        generic_profile,
        "extract",
        lambda doc, text: [
            ExtractedCharge(
                charge_type="energy_block",
                charge_label="Energy Charge",
                rate_value=0.11321,
                rate_unit="$/kWh",
                season="summer",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Energy Charge",
                confidence_score=0.72,
            ),
            ExtractedCharge(
                charge_type="energy_block",
                charge_label="Energy Charge",
                rate_value=0.08274,
                rate_unit="$/kWh",
                season="winter",
                tou_period=None,
                tier_min=None,
                tier_max=None,
                source_snippet="Energy Charge",
                confidence_score=0.72,
            ),
        ],
    )

    doc = extractor.get_document_for_extraction(historical_id)
    assert doc is not None
    _, _, inserted, _, _ = extractor.process_document(doc)
    assert inserted == 2

    check = connect(db_path)
    row = check.execute(
        """
        SELECT parser_profile, charge_count, metadata_json
        FROM parse_attempt_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    assert row["parser_profile"] == "progress_residential_tou"
    assert row["charge_count"] == 2
    selection = json.loads(row["metadata_json"])["selection"]
    assert selection["fallback_applied"] is True
    assert selection["fallback_triggered_by"] == "weak"
    assert selection["fallback_reason"] == "charge_type_coverage_gain"
    assert selection["initial_outcome_quality"] == "weak"
    assert selection["final_outcome_quality"] == "strong"
    assert selection["initial_metrics"]["unique_charge_types"] == 1
    assert selection["final_metrics"]["unique_charge_types"] == 2
    assert selection["fallback_attempts"][0]["metrics"]["unique_charge_types"] == 2
    check.close()


def test_bulk_extractor_run_extraction_respects_limit(tmp_path) -> None:
    extractor = BulkExtractor(str(tmp_path / "limit.db"))
    docs = [
        {"id": 1, "family_key": "family-a"},
        {"id": 2, "family_key": "family-b"},
        {"id": 3, "family_key": "family-c"},
    ]

    extractor.get_documents_needing_extraction = lambda: list(docs)
    extractor.count_documents_missing_versions = lambda: 0
    extractor.process_document = lambda doc: (doc["id"], doc["family_key"], 1, "ok", "test_profile")

    results = extractor.run_extraction(max_workers=1, limit=1)

    assert results["total_documents"] == 1
    assert results["documents_processed"] == 1
    assert results["total_charges_inserted"] == 1
    assert results["by_family"] == {"family-a": 1}
    assert results["status_counts"] == {"ok": 1}
