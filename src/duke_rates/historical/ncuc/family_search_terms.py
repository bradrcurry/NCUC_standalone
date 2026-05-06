"""
Per-family NCUC search term dictionary for Duke Energy Progress NC.

Each entry maps a leaf number (as string) to a FamilySearchProfile containing:
  - family_key:     canonical DB family key (nc-progress-leaf-NNN)
  - schedule_code:  DB schedule_code value
  - title:          canonical tariff title
  - search_terms:   NCUC filing language to use in queries (most distinctive first)
  - aliases:        alternate names/abbreviations found in older filings
  - include_terms:  text profile positive matches (for exhibit_selector scoring)
  - exclude_terms:  text profile negative matches (competing family signals)
  - ncuc_queries:   pre-built query strings ready for search_run
  - docket_hints:   known E-2 sub-docket numbers where this family appears

Naming conventions in NCUC filings:
  - Pre-2012: "Progress Energy Carolinas" (PEC)
  - Post-2012: "Duke Energy Progress" (DEP)
  - Leaf numbers appear as "Leaf No. NNN" or "Revised Leaf No. NNN"
  - Riders appear as "Rider XYZ" or "Rider No. XYZ"
  - Rate schedules appear as "Schedule NNN" or "Rate Schedule NNN"
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FamilySearchProfile:
    """Search profile for a single tariff family."""
    leaf: str                           # Leaf number as string (e.g. "501")
    family_key: str                     # DB key (e.g. "nc-progress-leaf-501")
    schedule_code: str                  # DB schedule_code
    title: str                          # Canonical title
    search_terms: list[str]             # NCUC filing language, most distinctive first
    aliases: list[str]                  # Alternate names / abbreviations
    include_terms: list[str]            # Positive text profile signals
    exclude_terms: list[str]            # Negative / competing signals
    ncuc_queries: list[str]             # Ready-to-use query strings
    docket_hints: list[str] = field(default_factory=list)  # Known E-2 sub dockets


# ---------------------------------------------------------------------------
# The master dictionary: leaf number → FamilySearchProfile
# ---------------------------------------------------------------------------

FAMILY_PROFILES: dict[str, FamilySearchProfile] = {

    # ------------------------------------------------------------------
    # Rate Schedules — Residential
    # ------------------------------------------------------------------
    "500": FamilySearchProfile(
        leaf="500",
        family_key="nc-progress-leaf-500",
        schedule_code="RES",
        title="Residential Service Schedule RES",
        search_terms=[
            "residential service schedule",
            "schedule RES",
            "residential rate",
            "leaf no 500",
        ],
        aliases=["RES", "SCHEDULE RES", "RESIDENTIAL SERVICE"],
        include_terms=[
            "RESIDENTIAL SERVICE", "SCHEDULE RES", "LEAF NO. 500",
            "PER KILOWATT-HOUR", "CUSTOMER CHARGE",
        ],
        exclude_terms=["TIME-OF-USE", "DEMAND CHARGE", "RIDER", "FUEL CHARGE"],
        ncuc_queries=[
            "Duke Energy Progress residential service schedule RES",
            "Progress Energy Carolinas residential service schedule",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142", "E-2 Sub 1100"],
    ),

    "501": FamilySearchProfile(
        leaf="501",
        family_key="nc-progress-doc-FUELCHARGEADJUSTMENT",
        schedule_code="FUEL",
        title="Fuel Charge Adjustment",
        search_terms=[
            "fuel charge adjustment",
            "fuel cost recovery",
            "fuel clause",
            "leaf no 501",
            "fuel factor",
        ],
        aliases=["FUEL", "FUEL CHARGE", "FUEL COST RECOVERY", "FCA"],
        include_terms=[
            "FUEL CHARGE ADJUSTMENT", "FUEL RATES", "FUEL FACTOR",
            "FUEL COST RECOVERY", "CENTS PER KILOWATT-HOUR", "LEAF NO. 501",
        ],
        exclude_terms=["REPS EMF", "CPRE", "RENEWABLE ADVANTAGE", "DSM"],
        ncuc_queries=[
            "Duke Energy Progress fuel charge adjustment",
            "Progress Energy Carolinas fuel cost recovery",
            "fuel clause adjustment schedule",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1100"],
    ),

    "502": FamilySearchProfile(
        leaf="502",
        family_key="nc-progress-leaf-502",
        schedule_code="R_TOU",
        title="Residential Service Time-of-Use Schedule R-TOU",
        search_terms=[
            "residential time of use",
            "schedule R-TOU",
            "smart usage plan",
            "leaf no 502",
            "time of use residential",
        ],
        aliases=["R-TOU", "SCHEDULE R-TOU", "SMART USAGE", "RTOU"],
        include_terms=[
            "R-TOU", "RESIDENTIAL TIME-OF-USE", "SMART USAGE PLAN",
            "ON-PEAK", "OFF-PEAK", "LEAF NO. 502",
        ],
        exclude_terms=["DEMAND CHARGE", "CRITICAL PEAK", "R-TOUD", "R-TOU-EV"],
        ncuc_queries=[
            "Duke Energy Progress residential time of use R-TOU",
            "Progress Energy Carolinas schedule R-TOU",
            "residential time of use schedule",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "503": FamilySearchProfile(
        leaf="503",
        family_key="nc-progress-leaf-503",
        schedule_code="R_TOU_CPP",
        title="Residential Service Time-of-Use with Critical Peak Pricing",
        search_terms=[
            "critical peak pricing",
            "R-TOU-CPP",
            "time of use demand",
            "R-TOUD",
            "leaf no 503",
        ],
        aliases=["R-TOUD", "R-TOU-CPP", "CPP", "CRITICAL PEAK"],
        include_terms=[
            "R-TOUD", "R-TOU-CPP", "CRITICAL PEAK PRICING",
            "TIME-OF-USE DEMAND", "LEAF NO. 503",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "REPS RIDER", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress critical peak pricing residential",
            "Progress Energy Carolinas R-TOUD schedule",
            "residential time of use demand schedule",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "504": FamilySearchProfile(
        leaf="504",
        family_key="nc-progress-leaf-504",
        schedule_code="R_TOU_EV",
        title="Residential Service Pilot Time of Use with Discount Charging Period Schedule R-TOU-EV",
        search_terms=[
            "R-TOU-EV",
            "residential service pilot time of use",
            "discount charging period",
            "residential time of use energy",
            "all energy time of use",
            "leaf no 504",
        ],
        aliases=["R-TOU-EV", "R-TOU ENERGY", "ALL-ENERGY TIME-OF-USE"],
        include_terms=[
            "R-TOU", "R-TOU-EV", "TIME-OF-USE", "ALL-ENERGY TIME-OF-USE",
            "LEAF NO. 504",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "REPS RIDER", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress residential time of use energy R-TOU",
            "Progress Energy Carolinas all energy time of use",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Rate Schedules — General Service
    # ------------------------------------------------------------------
    "520": FamilySearchProfile(
        leaf="520",
        family_key="nc-progress-leaf-520",
        schedule_code="SGS",
        title="Small General Service Schedule SGS",
        search_terms=[
            "small general service",
            "schedule SGS",
            "leaf no 520",
            "SGS rate",
        ],
        aliases=["SGS", "SCHEDULE SGS", "SMALL GENERAL SERVICE"],
        include_terms=[
            "SMALL GENERAL SERVICE", "SCHEDULE SGS", "LEAF NO. 520",
            "PER KILOWATT-HOUR", "CUSTOMER CHARGE",
        ],
        exclude_terms=["TIME-OF-USE", "DEMAND CHARGE", "RIDER"],
        ncuc_queries=[
            "Duke Energy Progress small general service SGS",
            "Progress Energy Carolinas schedule SGS",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "521": FamilySearchProfile(
        leaf="521",
        family_key="nc-progress-leaf-521",
        schedule_code="SGS_TOUE",
        title="Small General Service All-Energy Time-of-Use Schedule SGS-TOUE",
        search_terms=[
            "SGS-TOUE",
            "small general service time of use",
            "all energy time of use SGS",
            "leaf no 521",
        ],
        aliases=["SGS-TOUE", "SGS-TOU", "SMALL GENERAL SERVICE TIME-OF-USE"],
        include_terms=[
            "SGS-TOUE", "SGS-TOU", "SMALL GENERAL SERVICE TIME-OF-USE",
            "ALL-ENERGY", "LEAF NO. 521",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "LGS"],
        ncuc_queries=[
            "Duke Energy Progress SGS-TOUE small general service time of use",
            "Progress Energy Carolinas small general service all energy TOU",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "522": FamilySearchProfile(
        leaf="522",
        family_key="nc-progress-leaf-522",
        schedule_code="SGS_TOU_CLR",
        title="Small General Service (Constant Load) SGS-TOU-CLR",
        search_terms=[
            "SGS-TOU-CLR",
            "constant load rate",
            "small general service constant load",
            "leaf no 522",
        ],
        aliases=["SGS-TOU-CLR", "SGS-CLR", "CONSTANT LOAD"],
        include_terms=[
            "SGS-TOU-CLR", "CONSTANT LOAD", "SMALL GENERAL SERVICE",
            "LEAF NO. 522",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "LGS"],
        ncuc_queries=[
            "Duke Energy Progress SGS-TOU-CLR constant load",
            "Progress Energy Carolinas small general service constant load",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "523": FamilySearchProfile(
        leaf="523",
        family_key="nc-progress-leaf-523",
        schedule_code="SGS_TOU_CPP",
        title="Small General Service Time-of-Use with Critical Peak Pricing",
        search_terms=[
            "SGS-TOU-CPP",
            "small general service critical peak",
            "leaf no 523",
        ],
        aliases=["SGS-TOU-CPP", "SGS CPP", "SMALL GENERAL SERVICE CRITICAL PEAK"],
        include_terms=[
            "SGS-TOU-CPP", "CRITICAL PEAK", "SMALL GENERAL SERVICE",
            "LEAF NO. 523",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "LGS", "R-TOU-CPP"],
        ncuc_queries=[
            "Duke Energy Progress SGS-TOU-CPP critical peak",
            "Progress Energy Carolinas small general service critical peak pricing",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "524": FamilySearchProfile(
        leaf="524",
        family_key="nc-progress-leaf-524",
        schedule_code="MGS",
        title="Medium General Service MGS",
        search_terms=[
            "medium general service",
            "schedule MGS",
            "leaf no 524",
        ],
        aliases=["MGS", "SCHEDULE MGS", "MEDIUM GENERAL SERVICE"],
        include_terms=[
            "MEDIUM GENERAL SERVICE", "SCHEDULE MGS", "LEAF NO. 524",
            "DEMAND CHARGE", "CUSTOMER CHARGE",
        ],
        exclude_terms=["SMALL GENERAL SERVICE", "LARGE GENERAL SERVICE", "RIDER"],
        ncuc_queries=[
            "Duke Energy Progress medium general service MGS",
            "Progress Energy Carolinas schedule MGS",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "525": FamilySearchProfile(
        leaf="525",
        family_key="nc-progress-leaf-525",
        schedule_code="MGS_TOU",
        title="Medium General Service Time-of-Use MGS-TOU",
        search_terms=[
            "MGS-TOU",
            "medium general service time of use",
            "leaf no 525",
        ],
        aliases=["MGS-TOU", "MEDIUM GENERAL SERVICE TIME-OF-USE"],
        include_terms=[
            "MGS-TOU", "MEDIUM GENERAL SERVICE TIME-OF-USE",
            "ON-PEAK", "OFF-PEAK", "LEAF NO. 525",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "LGS-TOU"],
        ncuc_queries=[
            "Duke Energy Progress MGS-TOU medium general service time of use",
            "Progress Energy Carolinas medium general service TOU",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "526": FamilySearchProfile(
        leaf="526",
        family_key="nc-progress-leaf-526",
        schedule_code="SI",
        title="Seasonal or Intermittent Service SI",
        search_terms=[
            "seasonal or intermittent service",
            "schedule SI",
            "intermittent load",
            "leaf no 526",
        ],
        aliases=["SI", "SCHEDULE SI", "SEASONAL SERVICE", "INTERMITTENT SERVICE"],
        include_terms=[
            "SEASONAL OR INTERMITTENT", "SCHEDULE SI", "LEAF NO. 526",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL"],
        ncuc_queries=[
            "Duke Energy Progress seasonal intermittent service SI",
            "Progress Energy Carolinas schedule SI intermittent",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "527": FamilySearchProfile(
        leaf="527",
        family_key="nc-progress-leaf-527",
        schedule_code="CH_TOUE",
        title="Church Service (Time-of-Use) CH-TOUE",
        search_terms=[
            "church service time of use",
            "CH-TOUE",
            "church rate schedule",
            "leaf no 527",
        ],
        aliases=["CH-TOUE", "CHURCH SERVICE", "CHURCH TIME-OF-USE"],
        include_terms=[
            "CHURCH SERVICE", "CH-TOUE", "TIME-OF-USE", "LEAF NO. 527",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "SCHOOL"],
        ncuc_queries=[
            "Duke Energy Progress church service CH-TOUE",
            "Progress Energy Carolinas church time of use schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "528": FamilySearchProfile(
        leaf="528",
        family_key="nc-progress-leaf-528",
        schedule_code="GS_TES",
        title="General Service (Thermal Energy Storage) Schedule GS-TES",
        search_terms=[
            "thermal energy storage",
            "GS-TES",
            "thermal storage schedule",
            "leaf no 528",
        ],
        aliases=["GS-TES", "THERMAL ENERGY STORAGE", "TES"],
        include_terms=[
            "THERMAL ENERGY STORAGE", "GS-TES", "LEAF NO. 528",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "APH-TES"],
        ncuc_queries=[
            "Duke Energy Progress thermal energy storage GS-TES",
            "Progress Energy Carolinas thermal energy storage schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "529": FamilySearchProfile(
        leaf="529",
        family_key="nc-progress-leaf-529",
        schedule_code="APH_TES",
        title="Agricultural Post-Harvest Processing (Experimental Thermal Energy Storage)",
        search_terms=[
            "agricultural post harvest",
            "APH-TES",
            "post harvest thermal storage",
            "leaf no 529",
        ],
        aliases=["APH-TES", "AGRICULTURAL POST-HARVEST", "POST-HARVEST PROCESSING"],
        include_terms=[
            "AGRICULTURAL POST-HARVEST", "APH-TES", "THERMAL ENERGY STORAGE",
            "LEAF NO. 529",
        ],
        exclude_terms=["RIDER", "GS-TES"],
        ncuc_queries=[
            "Duke Energy Progress agricultural post harvest APH-TES",
            "Progress Energy Carolinas agricultural thermal storage",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "532": FamilySearchProfile(
        leaf="532",
        family_key="nc-progress-leaf-532",
        schedule_code="LGS",
        title="Large General Service LGS",
        search_terms=[
            "large general service",
            "schedule LGS",
            "leaf no 532",
        ],
        aliases=["LGS", "SCHEDULE LGS", "LARGE GENERAL SERVICE"],
        include_terms=[
            "LARGE GENERAL SERVICE", "SCHEDULE LGS", "LEAF NO. 532",
            "DEMAND CHARGE", "CUSTOMER CHARGE",
        ],
        exclude_terms=["MEDIUM GENERAL SERVICE", "RIDER", "LGS-TOU", "LGS-HLF"],
        ncuc_queries=[
            "Duke Energy Progress large general service LGS",
            "Progress Energy Carolinas schedule LGS",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "533": FamilySearchProfile(
        leaf="533",
        family_key="nc-progress-leaf-533",
        schedule_code="LGS_TOU",
        title="Large General Service Time-of-Use LGS-TOU",
        search_terms=[
            "LGS-TOU",
            "large general service time of use",
            "leaf no 533",
        ],
        aliases=["LGS-TOU", "LARGE GENERAL SERVICE TIME-OF-USE"],
        include_terms=[
            "LGS-TOU", "LARGE GENERAL SERVICE TIME-OF-USE",
            "ON-PEAK", "OFF-PEAK", "LEAF NO. 533",
        ],
        exclude_terms=["RIDER", "LGS-HLF", "LGS-RTP", "MGS-TOU"],
        ncuc_queries=[
            "Duke Energy Progress LGS-TOU large general service time of use",
            "Progress Energy Carolinas large general service TOU",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "534": FamilySearchProfile(
        leaf="534",
        family_key="nc-progress-leaf-534",
        schedule_code="LGS_RTP",
        title="Large General Service Real Time Pricing LGS-RTP",
        search_terms=[
            "real time pricing",
            "LGS-RTP",
            "large general service real time",
            "leaf no 534",
        ],
        aliases=["LGS-RTP", "REAL TIME PRICING", "RTP"],
        include_terms=[
            "LGS-RTP", "REAL TIME PRICING", "LARGE GENERAL SERVICE",
            "LEAF NO. 534",
        ],
        exclude_terms=["RIDER", "LGS-TOU", "LGS-HLF"],
        ncuc_queries=[
            "Duke Energy Progress large general service real time pricing LGS-RTP",
            "Progress Energy Carolinas real time pricing schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "535": FamilySearchProfile(
        leaf="535",
        family_key="nc-progress-leaf-535",
        schedule_code="HP",
        title="Large General Service Hourly Pricing HP",
        search_terms=[
            "hourly pricing",
            "schedule HP",
            "large general service hourly",
            "leaf no 535",
        ],
        aliases=["HP", "SCHEDULE HP", "HOURLY PRICING"],
        include_terms=[
            "HOURLY PRICING", "SCHEDULE HP", "LARGE GENERAL SERVICE",
            "LEAF NO. 535",
        ],
        exclude_terms=["RIDER", "LGS-TOU", "REAL TIME PRICING"],
        ncuc_queries=[
            "Duke Energy Progress hourly pricing schedule HP",
            "Progress Energy Carolinas large general service hourly pricing",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "536": FamilySearchProfile(
        leaf="536",
        family_key="nc-progress-leaf-536",
        schedule_code="LGS_HLF",
        title="Large General Service (High Load Factor) Schedule LGS-HLF",
        search_terms=[
            "high load factor",
            "LGS-HLF",
            "large general service high load",
            "leaf no 536",
        ],
        aliases=["LGS-HLF", "HIGH LOAD FACTOR", "HLF"],
        include_terms=[
            "LGS-HLF", "HIGH LOAD FACTOR", "LARGE GENERAL SERVICE",
            "LEAF NO. 536",
        ],
        exclude_terms=["RIDER", "LGS-TOU", "REAL TIME PRICING"],
        ncuc_queries=[
            "Duke Energy Progress LGS-HLF high load factor",
            "Progress Energy Carolinas large general service high load factor",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Rate Schedules — Lighting
    # ------------------------------------------------------------------
    "570": FamilySearchProfile(
        leaf="570",
        family_key="nc-progress-leaf-570",
        schedule_code="ALS",
        title="Area Lighting Service ALS",
        search_terms=[
            "area lighting service",
            "schedule ALS",
            "leaf no 570",
        ],
        aliases=["ALS", "SCHEDULE ALS", "AREA LIGHTING"],
        include_terms=[
            "AREA LIGHTING SERVICE", "SCHEDULE ALS", "LEAF NO. 570",
            "PER LAMP", "LUMINAIRE",
        ],
        exclude_terms=["STREET LIGHTING", "RIDER"],
        ncuc_queries=[
            "Duke Energy Progress area lighting service ALS",
            "Progress Energy Carolinas area lighting schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "571": FamilySearchProfile(
        leaf="571",
        family_key="nc-progress-leaf-571",
        schedule_code="SLS",
        title="Street Lighting Service SLS",
        search_terms=[
            "street lighting service",
            "schedule SLS",
            "leaf no 571",
        ],
        aliases=["SLS", "SCHEDULE SLS", "STREET LIGHTING SERVICE"],
        include_terms=[
            "STREET LIGHTING SERVICE", "SCHEDULE SLS", "LEAF NO. 571",
            "PER LAMP",
        ],
        exclude_terms=["RESIDENTIAL SUBDIVISIONS", "RENEWABLE ADVANTAGE", "RIDER"],
        ncuc_queries=[
            "Duke Energy Progress street lighting service SLS",
            "Progress Energy Carolinas schedule SLS street lighting",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "572": FamilySearchProfile(
        leaf="572",
        family_key="nc-progress-leaf-572",
        schedule_code="SLR",
        title="Street Lighting Service - Residential Subdivisions SLR",
        search_terms=[
            "street lighting residential subdivisions",
            "schedule SLR",
            "residential subdivision lighting",
            "leaf no 572",
        ],
        aliases=["SLR", "SCHEDULE SLR", "RESIDENTIAL SUBDIVISIONS"],
        include_terms=[
            "RESIDENTIAL SUBDIVISIONS", "SCHEDULE SLR", "SLR",
            "LEAF NO. 572", "STREET LIGHTING",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "RIDER"],
        ncuc_queries=[
            "Duke Energy Progress street lighting residential subdivisions SLR",
            "Progress Energy Carolinas schedule SLR residential subdivision",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "573": FamilySearchProfile(
        leaf="573",
        family_key="nc-progress-leaf-573",
        schedule_code="SFLS",
        title="Sports Field Lighting Service SFLS",
        search_terms=[
            "sports field lighting",
            "SFLS",
            "leaf no 573",
        ],
        aliases=["SFLS", "SPORTS FIELD LIGHTING", "SPORTS LIGHTING"],
        include_terms=[
            "SPORTS FIELD LIGHTING", "SFLS", "LEAF NO. 573",
        ],
        exclude_terms=["RIDER", "STREET LIGHTING", "AREA LIGHTING"],
        ncuc_queries=[
            "Duke Energy Progress sports field lighting SFLS",
            "Progress Energy Carolinas sports field lighting service",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "574": FamilySearchProfile(
        leaf="574",
        family_key="nc-progress-leaf-574",
        schedule_code="TSS",
        title="Traffic Signal Service TSS",
        search_terms=[
            "traffic signal service",
            "schedule TSS",
            "leaf no 574",
        ],
        aliases=["TSS", "SCHEDULE TSS", "TRAFFIC SIGNAL"],
        include_terms=[
            "TRAFFIC SIGNAL SERVICE", "SCHEDULE TSS", "LEAF NO. 574",
        ],
        exclude_terms=["RIDER", "STREET LIGHTING"],
        ncuc_queries=[
            "Duke Energy Progress traffic signal service TSS",
            "Progress Energy Carolinas traffic signal schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "575": FamilySearchProfile(
        leaf="575",
        family_key="nc-progress-leaf-575",
        schedule_code="TFS",
        title="Traffic Signal Service (Metered) TFS",
        search_terms=[
            "traffic signal metered",
            "TFS",
            "metered traffic signal",
            "leaf no 575",
        ],
        aliases=["TFS", "TRAFFIC SIGNAL METERED", "METERED TRAFFIC SIGNAL"],
        include_terms=[
            "TRAFFIC SIGNAL", "TFS", "METERED", "LEAF NO. 575",
        ],
        exclude_terms=["RIDER", "TSS"],
        ncuc_queries=[
            "Duke Energy Progress traffic signal metered TFS",
            "Progress Energy Carolinas metered traffic signal service",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Rate Schedules — Purchased Power
    # ------------------------------------------------------------------
    "590": FamilySearchProfile(
        leaf="590",
        family_key="nc-progress-leaf-590",
        schedule_code="PP_RY_1",
        title="Purchased Power Schedule PP",
        search_terms=[
            "purchased power schedule",
            "schedule PP",
            "qualifying facility",
            "leaf no 590",
        ],
        aliases=["PP", "SCHEDULE PP", "PURCHASED POWER", "QF"],
        include_terms=[
            "PURCHASED POWER", "SCHEDULE PP", "QUALIFYING FACILITY",
            "LEAF NO. 590",
        ],
        exclude_terms=["RIDER", "RESIDENTIAL", "PPBE"],
        ncuc_queries=[
            "Duke Energy Progress purchased power schedule PP",
            "Progress Energy Carolinas purchased power qualifying facility",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "591": FamilySearchProfile(
        leaf="591",
        family_key="nc-progress-leaf-591",
        schedule_code="TANDC_FOR_PURCHASE_POWER_RY1",
        title="Terms and Conditions for the Purchase of Electric Power",
        search_terms=[
            "terms and conditions purchase electric power",
            "terms conditions cogeneration",
            "small power producer",
            "leaf no 591",
        ],
        aliases=["TERMS AND CONDITIONS", "PURCHASE POWER TERMS", "QF TERMS"],
        include_terms=[
            "TERMS AND CONDITIONS", "PURCHASE OF ELECTRIC POWER",
            "COGENERATION", "SMALL POWER PRODUCER", "LEAF NO. 591",
        ],
        exclude_terms=["RIDER", "SCHEDULE PP"],
        ncuc_queries=[
            "Duke Energy Progress terms conditions purchase electric power",
            "Progress Energy Carolinas cogeneration small power producer terms",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "592": FamilySearchProfile(
        leaf="592",
        family_key="nc-progress-leaf-592",
        schedule_code="PPBE_RY_1",
        title="Purchased Power Blend and Extend Schedule PPBE",
        search_terms=[
            "blend and extend",
            "PPBE",
            "purchased power blend",
            "leaf no 592",
        ],
        aliases=["PPBE", "BLEND AND EXTEND", "PURCHASED POWER BLEND"],
        include_terms=[
            "PPBE", "BLEND AND EXTEND", "PURCHASED POWER", "LEAF NO. 592",
        ],
        exclude_terms=["RIDER", "SCHEDULE PP"],
        ncuc_queries=[
            "Duke Energy Progress purchased power blend extend PPBE",
            "Progress Energy Carolinas PPBE schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Riders — Billing Adjustments and Base Riders
    # ------------------------------------------------------------------
    "600": FamilySearchProfile(
        leaf="600",
        family_key="nc-progress-leaf-600",
        schedule_code="SUMMARY_OF_RIDERS",
        title="Summary of Rider Adjustments",
        search_terms=[
            "summary of rider adjustments",
            "rider adjustment summary",
            "leaf no 600",
        ],
        aliases=["SUMMARY OF RIDERS", "RIDER SUMMARY", "SUMMARY OF ADJUSTMENTS"],
        include_terms=[
            "SUMMARY OF RIDER ADJUSTMENTS", "LEAF NO. 600",
            "RIDER ADJUSTMENTS",
        ],
        exclude_terms=[],
        ncuc_queries=[
            "Duke Energy Progress summary of rider adjustments",
            "Progress Energy Carolinas rider adjustment summary",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "601": FamilySearchProfile(
        leaf="601",
        family_key="nc-progress-leaf-601",
        schedule_code="RIDER_BA_RY1",
        title="Annual Billing Adjustments Rider BA",
        search_terms=[
            "annual billing adjustment",
            "rider BA",
            "billing adjustment rider",
            "leaf no 601",
        ],
        aliases=["RIDER BA", "BA", "ANNUAL BILLING ADJUSTMENT"],
        include_terms=[
            "ANNUAL BILLING ADJUSTMENT", "RIDER BA", "LEAF NO. 601",
            "BILLING ADJUSTMENT",
        ],
        exclude_terms=["FUEL CHARGE", "DSM", "EE RIDER"],
        ncuc_queries=[
            "Duke Energy Progress annual billing adjustment rider BA",
            "Progress Energy Carolinas rider BA billing adjustment",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "602": FamilySearchProfile(
        leaf="602",
        family_key="nc-progress-leaf-602",
        schedule_code="RIDER_JAA_RY1",
        title="Joint Agency Asset Rider JAA",
        search_terms=[
            "joint agency asset",
            "rider JAA",
            "joint agency adjustment",
            "leaf no 602",
        ],
        aliases=["JAA", "RIDER JAA", "JOINT AGENCY ASSET", "JOINT AGENCY"],
        include_terms=[
            "JOINT AGENCY ASSET", "RIDER JAA", "JOINT AGENCY",
            "LEAF NO. 602",
        ],
        exclude_terms=["REPS EMF", "CPRE", "RENEWABLE ADVANTAGE"],
        ncuc_queries=[
            "Duke Energy Progress joint agency asset rider JAA",
            "Progress Energy Carolinas rider JAA",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "604": FamilySearchProfile(
        leaf="604",
        family_key="nc-progress-leaf-604",
        schedule_code="RIDER_EDIT_4_RY1",
        title="Excess Deferred Income Tax Rider EDIT-4",
        search_terms=[
            "excess deferred income tax",
            "EDIT-4",
            "rider EDIT",
            "deferred tax rider",
            "leaf no 604",
        ],
        aliases=["EDIT-4", "RIDER EDIT", "EDIT", "EXCESS DEFERRED INCOME TAX"],
        include_terms=[
            "EXCESS DEFERRED INCOME TAX", "RIDER EDIT", "EDIT-4",
            "LEAF NO. 604", "DEFERRED TAX",
        ],
        exclude_terms=["REPS EMF", "CPRE", "RENEWABLE ADVANTAGE", "RIDER JAA"],
        ncuc_queries=[
            "Duke Energy Progress excess deferred income tax EDIT rider",
            "Progress Energy Carolinas rider EDIT deferred income tax",
            "Duke Energy Progress EDIT-4",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "605": FamilySearchProfile(
        leaf="605",
        family_key="nc-progress-leaf-605",
        schedule_code="RIDER_CPRE_RY1",
        title="Competitive Procurement of Renewable Energy Rider CPRE",
        search_terms=[
            "competitive procurement renewable energy",
            "rider CPRE",
            "CPRE rider",
            "leaf no 605",
        ],
        aliases=["CPRE", "RIDER CPRE", "COMPETITIVE PROCUREMENT RENEWABLE"],
        include_terms=[
            "CPRE", "COMPETITIVE PROCUREMENT OF RENEWABLE ENERGY",
            "RIDER CPRE", "LEAF NO. 605",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "REPS EMF"],
        ncuc_queries=[
            "Duke Energy Progress competitive procurement renewable energy CPRE",
            "Progress Energy Carolinas rider CPRE",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "607": FamilySearchProfile(
        leaf="607",
        family_key="nc-progress-leaf-607",
        schedule_code="RIDER_STS_RY1",
        title="Storm Securitization Rider STS",
        search_terms=[
            "rider STS",
            "storm securitization",
            "storm recovery rider",
            "storm cost recovery rider",
            "leaf no 607",
        ],
        aliases=["RIDER STS", "STS", "STORM RECOVERY RIDER", "STORM COST RECOVERY"],
        include_terms=[
            "STORM COST RECOVERY RIDER", "STORM RECOVERY RIDER",
            "RIDER STS", "LEAF NO. 607",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "REPS EMF", "CPRE", "DSM RIDER", "EE RIDER"],
        ncuc_queries=[
            "Duke Energy Progress storm recovery rider STS",
            "Progress Energy Carolinas storm cost recovery rider",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "608": FamilySearchProfile(
        leaf="608",
        family_key="nc-progress-leaf-608",
        schedule_code="RIDER_RDM_RY1",
        title="Residential Decoupling Mechanism Rider RDM",
        search_terms=[
            "residential decoupling mechanism",
            "rider RDM",
            "decoupling mechanism",
            "leaf no 608",
        ],
        aliases=["RIDER RDM", "RDM", "RESIDENTIAL DECOUPLING", "DECOUPLING MECHANISM"],
        include_terms=[
            "RESIDENTIAL DECOUPLING MECHANISM", "RIDER RDM", "RDM",
            "DECOUPLING", "LEAF NO. 608",
        ],
        exclude_terms=["FUEL CHARGE", "DSM", "STORM RECOVERY"],
        ncuc_queries=[
            "Duke Energy Progress residential decoupling mechanism rider RDM",
            "Progress Energy Carolinas rider RDM decoupling",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "609": FamilySearchProfile(
        leaf="609",
        family_key="nc-progress-leaf-609",
        schedule_code="RIDER_ESM_RY1",
        title="Earnings Sharing Mechanism Rider ESM",
        search_terms=[
            "earnings sharing mechanism",
            "rider ESM",
            "ESM rider",
            "joint agency asset rider",
            "leaf no 609",
        ],
        aliases=["ESM", "RIDER ESM", "EARNINGS SHARING MECHANISM", "JAA"],
        include_terms=[
            "EARNINGS SHARING MECHANISM", "RIDER ESM", "ESM",
            "JOINT AGENCY ASSET", "RIDER JAA", "LEAF NO. 609",
        ],
        exclude_terms=["REPS EMF", "CPRE", "RENEWABLE ADVANTAGE"],
        ncuc_queries=[
            "Duke Energy Progress earnings sharing mechanism ESM",
            "Progress Energy Carolinas rider ESM earnings sharing",
            "Duke Energy Progress rider JAA joint agency",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "610": FamilySearchProfile(
        leaf="610",
        family_key="nc-progress-leaf-610",
        schedule_code="RIDER_PIM_RY1",
        title="Performance Incentive Mechanism Rider PIM",
        search_terms=[
            "performance incentive mechanism",
            "rider PIM",
            "energy efficiency rider",
            "EE rider",
            "leaf no 610",
        ],
        aliases=["RIDER PIM", "PIM", "PERFORMANCE INCENTIVE", "EE RIDER"],
        include_terms=[
            "PERFORMANCE INCENTIVE MECHANISM", "RIDER PIM", "PIM",
            "ENERGY EFFICIENCY RIDER", "EE RIDER", "LEAF NO. 610",
        ],
        exclude_terms=["DEMAND SIDE MANAGEMENT RIDER", "RENEWABLE ADVANTAGE", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress performance incentive mechanism rider PIM",
            "Progress Energy Carolinas energy efficiency rider EE",
            "Duke Energy Progress rider PIM",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "611": FamilySearchProfile(
        leaf="611",
        family_key="nc-progress-leaf-611",
        schedule_code="RIDER_CAR_RY1",
        title="Customer Assistance Recovery Rider CAR",
        search_terms=[
            "customer assistance recovery rider",
            "rider CAR",
            "demand side management rider",
            "DSM rider",
            "leaf no 611",
        ],
        aliases=["RIDER CAR", "CAR", "CUSTOMER ASSISTANCE RECOVERY", "DSM RIDER"],
        include_terms=[
            "CUSTOMER ASSISTANCE RECOVERY", "RIDER CAR", "CAR",
            "DEMAND SIDE MANAGEMENT RIDER", "DSM RIDER", "LEAF NO. 611",
        ],
        exclude_terms=["ENERGY EFFICIENCY RIDER", "RENEWABLE ADVANTAGE", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress customer assistance recovery rider CAR",
            "Progress Energy Carolinas demand side management rider DSM",
            "Duke Energy Progress rider CAR",
        ],
        docket_hints=["E-2 Sub 1190", "E-2 Sub 1142"],
    ),

    "613": FamilySearchProfile(
        leaf="613",
        family_key="nc-progress-leaf-613",
        schedule_code="RIDER_STS",
        title="Storm Securitization Rider STS",
        search_terms=[
            "storm securitization rider",
            "rider STS",
            "storm transition rider",
            "storm securitization",
            "leaf no 613",
        ],
        aliases=["RIDER STS", "STS", "STORM SECURITIZATION RIDER", "STORM TRANSITION RIDER"],
        include_terms=[
            "STORM SECURITIZATION RIDER", "RIDER STS",
            "STORM TRANSITION RIDER", "LEAF NO. 613",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "CPRE", "DSM RIDER", "EE RIDER"],
        ncuc_queries=[
            "Duke Energy Progress storm securitization rider STS",
            "Progress Energy Carolinas storm transition rider",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "640": FamilySearchProfile(
        leaf="640",
        family_key="nc-progress-leaf-640",
        schedule_code="RIDER_RECD",
        title="Residential Service Energy Conservation Discount Rider RECD",
        search_terms=[
            "energy conservation discount",
            "rider RECD",
            "clean power rate enhancement",
            "CPRE rider",
            "leaf no 640",
        ],
        aliases=["RECD", "RIDER RECD", "CPRE", "CLEAN POWER RATE ENHANCEMENT", "ENERGY CONSERVATION DISCOUNT"],
        include_terms=[
            "CPRE", "CLEAN POWER RATE ENHANCEMENT", "RIDER RECD",
            "ENERGY CONSERVATION DISCOUNT", "LEAF NO. 640",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE"],
        ncuc_queries=[
            "Duke Energy Progress energy conservation discount rider RECD",
            "Progress Energy Carolinas clean power rate enhancement CPRE",
            "Duke Energy Progress rider CPRE",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "641": FamilySearchProfile(
        leaf="641",
        family_key="nc-progress-leaf-641",
        schedule_code="RIDER_NM_RY1",
        title="Net Metering for Renewable Energy Facilities Rider NM",
        search_terms=[
            "net metering rider",
            "rider NM",
            "net metering renewable",
            "leaf no 641",
        ],
        aliases=["RIDER NM", "NM", "NET METERING RIDER", "NET METERING"],
        include_terms=[
            "NET METERING", "RIDER NM", "RENEWABLE ENERGY FACILITIES",
            "LEAF NO. 641",
        ],
        exclude_terms=["RIDER NMB", "SOLAR CHOICE", "RIDER SRR"],
        ncuc_queries=[
            "Duke Energy Progress net metering rider NM renewable",
            "Progress Energy Carolinas net metering renewable energy facilities",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "642": FamilySearchProfile(
        leaf="642",
        family_key="nc-progress-leaf-642",
        schedule_code="RIDER_GP_RY1",
        title="GreenPower Program Rider GP",
        search_terms=[
            "greenpower program rider",
            "rider GP",
            "green power program",
            "leaf no 642",
        ],
        aliases=["RIDER GP", "GP", "GREENPOWER PROGRAM", "GREEN POWER"],
        include_terms=[
            "GREENPOWER PROGRAM", "RIDER GP", "GREEN POWER", "LEAF NO. 642",
        ],
        exclude_terms=["RIDER REN", "SOLAR CHOICE", "GO RENEWABLE"],
        ncuc_queries=[
            "Duke Energy Progress GreenPower program rider GP",
            "Progress Energy Carolinas green power rider",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "643": FamilySearchProfile(
        leaf="643",
        family_key="nc-progress-leaf-643",
        schedule_code="RIDER_REN_RY1",
        title="GreenPower Program Renewable Rider REN",
        search_terms=[
            "GreenPower renewable rider",
            "rider REN",
            "renewable rider",
            "leaf no 643",
        ],
        aliases=["RIDER REN", "REN", "GREENPOWER RENEWABLE", "RENEWABLE ADVANTAGE"],
        include_terms=[
            "RIDER REN", "GREENPOWER", "RENEWABLE ADVANTAGE", "LEAF NO. 643",
        ],
        exclude_terms=["RIDER GP", "SOLAR CHOICE"],
        ncuc_queries=[
            "Duke Energy Progress GreenPower renewable rider REN",
            "Progress Energy Carolinas renewable advantage rider",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "644": FamilySearchProfile(
        leaf="644",
        family_key="nc-progress-leaf-644",
        schedule_code="RIDER_COP_RY1",
        title="Carbon Offset Program Rider COP",
        search_terms=[
            "carbon offset program",
            "rider COP",
            "carbon offset rider",
            "leaf no 644",
        ],
        aliases=["RIDER COP", "COP", "CARBON OFFSET PROGRAM"],
        include_terms=[
            "CARBON OFFSET PROGRAM", "RIDER COP", "CARBON OFFSET",
            "LEAF NO. 644",
        ],
        exclude_terms=["GREENPOWER", "SOLAR CHOICE"],
        ncuc_queries=[
            "Duke Energy Progress carbon offset program rider COP",
            "Progress Energy Carolinas carbon offset rider",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "645": FamilySearchProfile(
        leaf="645",
        family_key="nc-progress-leaf-645",
        schedule_code="RIDER_18_RY1",
        title="Public Housing Project Service Rider 18",
        search_terms=[
            "public housing project service",
            "rider 18",
            "public housing rider",
            "leaf no 645",
        ],
        aliases=["RIDER 18", "PUBLIC HOUSING RIDER", "PUBLIC HOUSING PROJECT SERVICE"],
        include_terms=[
            "PUBLIC HOUSING PROJECT SERVICE", "RIDER 18", "LEAF NO. 645",
        ],
        exclude_terms=["RIDER 7", "RIDER 28", "MILITARY"],
        ncuc_queries=[
            "Duke Energy Progress public housing project service rider 18",
            "Progress Energy Carolinas rider 18 public housing",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "646": FamilySearchProfile(
        leaf="646",
        family_key="nc-progress-leaf-646",
        schedule_code="RIDER_CM_RY1",
        title="Campground and Marina Rider CM",
        search_terms=[
            "campground and marina rider",
            "rider CM",
            "campground marina",
            "leaf no 646",
        ],
        aliases=["RIDER CM", "CM", "CAMPGROUND AND MARINA"],
        include_terms=[
            "CAMPGROUND AND MARINA", "RIDER CM", "LEAF NO. 646",
        ],
        exclude_terms=["MILITARY", "PUBLIC HOUSING"],
        ncuc_queries=[
            "Duke Energy Progress campground marina rider CM",
            "Progress Energy Carolinas campground and marina service",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "647": FamilySearchProfile(
        leaf="647",
        family_key="nc-progress-leaf-647",
        schedule_code="RIDER_28_RY1",
        title="Military Service Rider 28",
        search_terms=[
            "military service rider",
            "rider 28",
            "military installation service",
            "leaf no 647",
        ],
        aliases=["RIDER 28", "MILITARY RIDER", "MILITARY SERVICE RIDER"],
        include_terms=[
            "MILITARY SERVICE", "RIDER 28", "LEAF NO. 647",
        ],
        exclude_terms=["RIDER 7", "RIDER 18", "PUBLIC HOUSING"],
        ncuc_queries=[
            "Duke Energy Progress military service rider 28",
            "Progress Energy Carolinas rider 28 military",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "648": FamilySearchProfile(
        leaf="648",
        family_key="nc-progress-leaf-648",
        schedule_code="RIDER_TR_RY1",
        title="Transition Rider TR",
        search_terms=[
            "transition rider",
            "rider TR",
            "transition adjustment rider",
            "leaf no 648",
        ],
        aliases=["RIDER TR", "TR", "TRANSITION RIDER"],
        include_terms=[
            "TRANSITION RIDER", "RIDER TR", "LEAF NO. 648",
        ],
        exclude_terms=["STORM RECOVERY", "DECOUPLING"],
        ncuc_queries=[
            "Duke Energy Progress transition rider TR",
            "Progress Energy Carolinas rider TR transition",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "649": FamilySearchProfile(
        leaf="649",
        family_key="nc-progress-leaf-649",
        schedule_code="RIDER_US_RY1",
        title="Unmetered Service Rider US",
        search_terms=[
            "unmetered service rider",
            "rider US",
            "unmetered service",
            "leaf no 649",
        ],
        aliases=["RIDER US", "US", "UNMETERED SERVICE"],
        include_terms=[
            "UNMETERED SERVICE", "RIDER US", "LEAF NO. 649",
        ],
        exclude_terms=["METERED", "STREET LIGHTING"],
        ncuc_queries=[
            "Duke Energy Progress unmetered service rider US",
            "Progress Energy Carolinas unmetered service",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "650": FamilySearchProfile(
        leaf="650",
        family_key="nc-progress-leaf-650",
        schedule_code="RIDER_9_RY1",
        title="Highly Fluctuating or Intermittent Load Rider 9",
        search_terms=[
            "highly fluctuating load",
            "intermittent load rider",
            "rider 9",
            "leaf no 650",
        ],
        aliases=["RIDER 9", "FLUCTUATING LOAD", "INTERMITTENT LOAD RIDER"],
        include_terms=[
            "HIGHLY FLUCTUATING", "INTERMITTENT LOAD", "RIDER 9", "LEAF NO. 650",
        ],
        exclude_terms=["SEASONAL", "RIDER 7"],
        ncuc_queries=[
            "Duke Energy Progress highly fluctuating intermittent load rider 9",
            "Progress Energy Carolinas rider 9 fluctuating load",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "651": FamilySearchProfile(
        leaf="651",
        family_key="nc-progress-leaf-651",
        schedule_code="RIDER_7",
        title="Standby and Supplementary Service Rider 7",
        search_terms=[
            "standby and supplementary service",
            "rider 7",
            "standby service rider",
            "leaf no 651",
        ],
        aliases=["RIDER 7", "STANDBY SERVICE", "SUPPLEMENTARY SERVICE RIDER"],
        include_terms=[
            "STANDBY AND SUPPLEMENTARY SERVICE", "RIDER 7",
            "STANDBY SERVICE", "LEAF NO. 651",
        ],
        exclude_terms=["RIDER 57", "RIDER SS", "STORM RECOVERY"],
        ncuc_queries=[
            "Duke Energy Progress standby supplementary service rider 7",
            "Progress Energy Carolinas rider 7 standby service",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "652": FamilySearchProfile(
        leaf="652",
        family_key="nc-progress-leaf-652",
        schedule_code="RIDER_57_RY1",
        title="Supplementary and Interruptible Standby Service Rider 57",
        search_terms=[
            "supplementary interruptible standby",
            "rider 57",
            "interruptible standby service",
            "leaf no 652",
        ],
        aliases=["RIDER 57", "INTERRUPTIBLE STANDBY", "SUPPLEMENTARY INTERRUPTIBLE"],
        include_terms=[
            "SUPPLEMENTARY AND INTERRUPTIBLE STANDBY", "RIDER 57",
            "LEAF NO. 652",
        ],
        exclude_terms=["RIDER 7", "RIDER SS", "STORM RECOVERY"],
        ncuc_queries=[
            "Duke Energy Progress supplementary interruptible standby rider 57",
            "Progress Energy Carolinas rider 57 interruptible standby",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "653": FamilySearchProfile(
        leaf="653",
        family_key="nc-progress-leaf-653",
        schedule_code="RIDER_SS",
        title="Supplemental and Firm Standby Service SS",
        search_terms=[
            "supplemental firm standby",
            "rider SS",
            "firm standby service",
            "leaf no 653",
        ],
        aliases=["RIDER SS", "SS", "FIRM STANDBY", "SUPPLEMENTAL STANDBY"],
        include_terms=[
            "SUPPLEMENTAL AND FIRM STANDBY", "RIDER SS", "FIRM STANDBY",
            "LEAF NO. 653",
        ],
        exclude_terms=["RIDER 7", "RIDER 57", "NON-FIRM"],
        ncuc_queries=[
            "Duke Energy Progress supplemental firm standby service SS",
            "Progress Energy Carolinas rider SS firm standby",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "654": FamilySearchProfile(
        leaf="654",
        family_key="nc-progress-leaf-654",
        schedule_code="RIDER_NFS",
        title="Supplementary and Non-Firm Standby Service NFS",
        search_terms=[
            "non-firm standby service",
            "NFS",
            "non firm standby",
            "leaf no 654",
        ],
        aliases=["NFS", "RIDER NFS", "NON-FIRM STANDBY", "NON-FIRM SERVICE"],
        include_terms=[
            "NON-FIRM STANDBY", "NFS", "SUPPLEMENTARY AND NON-FIRM",
            "LEAF NO. 654",
        ],
        exclude_terms=["RIDER SS", "FIRM STANDBY", "RIDER 7"],
        ncuc_queries=[
            "Duke Energy Progress non-firm standby service NFS",
            "Progress Energy Carolinas supplementary non-firm standby",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "655": FamilySearchProfile(
        leaf="655",
        family_key="nc-progress-leaf-655",
        schedule_code="RIDER_LLC_RY1",
        title="Large Load Curtailable Rider LLC",
        search_terms=[
            "large load curtailable",
            "rider LLC",
            "curtailable load rider",
            "leaf no 655",
        ],
        aliases=["RIDER LLC", "LLC", "LARGE LOAD CURTAILABLE"],
        include_terms=[
            "LARGE LOAD CURTAILABLE", "RIDER LLC", "CURTAILABLE",
            "LEAF NO. 655",
        ],
        exclude_terms=["INTERRUPTIBLE", "STANDBY"],
        ncuc_queries=[
            "Duke Energy Progress large load curtailable rider LLC",
            "Progress Energy Carolinas rider LLC curtailable load",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "656": FamilySearchProfile(
        leaf="656",
        family_key="nc-progress-leaf-656",
        schedule_code="RIDER_68_RY1",
        title="Dispatched Power (Experimental) Rider 68",
        search_terms=[
            "dispatched power experimental",
            "rider 68",
            "dispatched power rider",
            "leaf no 656",
        ],
        aliases=["RIDER 68", "DISPATCHED POWER", "DISPATCHED POWER EXPERIMENTAL"],
        include_terms=[
            "DISPATCHED POWER", "RIDER 68", "LEAF NO. 656",
        ],
        exclude_terms=["INTERRUPTIBLE", "CURTAILABLE"],
        ncuc_queries=[
            "Duke Energy Progress dispatched power rider 68",
            "Progress Energy Carolinas dispatched power experimental",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "657": FamilySearchProfile(
        leaf="657",
        family_key="nc-progress-leaf-657",
        schedule_code="RIDER_IPS_RY1",
        title="Incremental Power Service IPS",
        search_terms=[
            "incremental power service",
            "rider IPS",
            "IPS schedule",
            "leaf no 657",
        ],
        aliases=["RIDER IPS", "IPS", "INCREMENTAL POWER SERVICE"],
        include_terms=[
            "INCREMENTAL POWER SERVICE", "RIDER IPS", "IPS",
            "LEAF NO. 657",
        ],
        exclude_terms=["CURTAILABLE", "DISPATCHED POWER"],
        ncuc_queries=[
            "Duke Energy Progress incremental power service IPS",
            "Progress Energy Carolinas rider IPS incremental power",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "658": FamilySearchProfile(
        leaf="658",
        family_key="nc-progress-leaf-658",
        schedule_code="RIDER_ED_RY1",
        title="Economic Development Rider ED",
        search_terms=[
            "economic development rider",
            "rider ED",
            "economic development rate",
            "leaf no 658",
        ],
        aliases=["RIDER ED", "ED", "ECONOMIC DEVELOPMENT RIDER"],
        include_terms=[
            "ECONOMIC DEVELOPMENT", "RIDER ED", "LEAF NO. 658",
        ],
        exclude_terms=["ECONOMIC REDEVELOPMENT", "RIDER ERD"],
        ncuc_queries=[
            "Duke Energy Progress economic development rider ED",
            "Progress Energy Carolinas rider ED economic development",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "659": FamilySearchProfile(
        leaf="659",
        family_key="nc-progress-leaf-659",
        schedule_code="RIDER_ERD_RY1",
        title="Economic Redevelopment Rider ERD",
        search_terms=[
            "economic redevelopment rider",
            "rider ERD",
            "leaf no 659",
        ],
        aliases=["RIDER ERD", "ERD", "ECONOMIC REDEVELOPMENT"],
        include_terms=[
            "ECONOMIC REDEVELOPMENT", "RIDER ERD", "LEAF NO. 659",
        ],
        exclude_terms=["ECONOMIC DEVELOPMENT RIDER", "RIDER ED"],
        ncuc_queries=[
            "Duke Energy Progress economic redevelopment rider ERD",
            "Progress Energy Carolinas rider ERD economic redevelopment",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "660": FamilySearchProfile(
        leaf="660",
        family_key="nc-progress-leaf-660",
        schedule_code="RIDER_PPS_RY1",
        title="Premier Power Service Rider PPS",
        search_terms=[
            "premier power service rider",
            "rider PPS",
            "premier power rider",
            "leaf no 660",
        ],
        aliases=["RIDER PPS", "PPS", "PREMIER POWER SERVICE"],
        include_terms=[
            "PREMIER POWER SERVICE", "RIDER PPS", "LEAF NO. 660",
        ],
        exclude_terms=["INTERRUPTIBLE", "CURTAILABLE"],
        ncuc_queries=[
            "Duke Energy Progress premier power service rider PPS",
            "Progress Energy Carolinas rider PPS premier power",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "661": FamilySearchProfile(
        leaf="661",
        family_key="nc-progress-leaf-661",
        schedule_code="RIDER_MROP_RY1",
        title="Meter-Related Optional Programs Rider MROP",
        search_terms=[
            "meter related optional programs",
            "rider MROP",
            "MROP rider",
            "leaf no 661",
        ],
        aliases=["RIDER MROP", "MROP", "METER-RELATED OPTIONAL PROGRAMS"],
        include_terms=[
            "METER-RELATED OPTIONAL PROGRAMS", "RIDER MROP", "MROP",
            "LEAF NO. 661",
        ],
        exclude_terms=["PREPAY", "SOLAR"],
        ncuc_queries=[
            "Duke Energy Progress meter related optional programs rider MROP",
            "Progress Energy Carolinas rider MROP optional programs",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "662": FamilySearchProfile(
        leaf="662",
        family_key="nc-progress-leaf-662",
        schedule_code="RIDER_EPPWP_RY1",
        title="Residential Service Equal Payment Plan (WeatherProtect) Pilot EPPWP",
        search_terms=[
            "weatherprotect pilot",
            "equal payment plan",
            "EPPWP",
            "prepay service rider",
            "leaf no 662",
        ],
        aliases=["EPPWP", "WEATHERPROTECT", "EQUAL PAYMENT PLAN", "PREPAY", "PREPAY SERVICE RIDER"],
        include_terms=[
            "EPPWP", "WEATHERPROTECT", "EQUAL PAYMENT PLAN", "LEAF NO. 662",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress WeatherProtect pilot rider EPPWP",
            "Progress Energy Carolinas equal payment plan weatherprotect",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "663": FamilySearchProfile(
        leaf="663",
        family_key="nc-progress-leaf-663",
        schedule_code="RIDER_SRR_RY1",
        title="Solar Rebate Rider SRR",
        search_terms=[
            "solar rebate rider",
            "rider SRR",
            "SRR rider",
            "leaf no 663",
        ],
        aliases=["RIDER SRR", "SRR", "SOLAR REBATE RIDER", "SUNSENSE REBATE"],
        include_terms=[
            "SOLAR REBATE", "RIDER SRR", "SRR", "SUNSENSE", "LEAF NO. 663",
        ],
        exclude_terms=["SHARED SOLAR", "SOLAR CHOICE", "NET METERING"],
        ncuc_queries=[
            "Duke Energy Progress solar rebate rider SRR",
            "Progress Energy Carolinas rider SRR solar rebate SunSense",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "664": FamilySearchProfile(
        leaf="664",
        family_key="nc-progress-leaf-664",
        schedule_code="RIDER_SSR_RY1",
        title="Shared Solar Rider SSR",
        search_terms=[
            "shared solar rider",
            "rider SSR",
            "community solar",
            "shared solar program",
            "leaf no 664",
        ],
        aliases=["RIDER SSR", "SSR", "SHARED SOLAR RIDER", "COMMUNITY SOLAR"],
        include_terms=[
            "SHARED SOLAR", "RIDER SSR", "SSR", "LEAF NO. 664",
        ],
        exclude_terms=["SOLAR REBATE", "RIDER SRR", "SOLAR CHOICE", "NET METERING"],
        ncuc_queries=[
            "Duke Energy Progress shared solar rider SSR",
            "Progress Energy Carolinas shared solar community solar rider",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "665": FamilySearchProfile(
        leaf="665",
        family_key="nc-progress-leaf-665",
        schedule_code="RIDER_GSA_RY1",
        title="Green Source Advantage Rider GSA",
        search_terms=[
            "green source advantage rider",
            "rider GSA",
            "green source advantage",
            "leaf no 665",
        ],
        aliases=["RIDER GSA", "GSA", "GREEN SOURCE ADVANTAGE"],
        include_terms=[
            "GREEN SOURCE ADVANTAGE", "RIDER GSA", "GSA", "LEAF NO. 665",
        ],
        exclude_terms=["SOLAR CHOICE", "GREENPOWER", "GO RENEWABLE"],
        ncuc_queries=[
            "Duke Energy Progress green source advantage rider GSA",
            "Progress Energy Carolinas rider GSA green source advantage",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "666": FamilySearchProfile(
        leaf="666",
        family_key="nc-progress-leaf-666",
        schedule_code="RIDER_GR",
        title="Go Renewable Rider GR",
        search_terms=[
            "go renewable rider",
            "rider GR",
            "go renewable program",
            "leaf no 666",
        ],
        aliases=["RIDER GR", "GR", "GO RENEWABLE RIDER"],
        include_terms=[
            "GO RENEWABLE", "RIDER GR", "LEAF NO. 666",
        ],
        exclude_terms=["GREENPOWER", "GREEN SOURCE ADVANTAGE", "SOLAR CHOICE"],
        ncuc_queries=[
            "Duke Energy Progress go renewable rider GR",
            "Progress Energy Carolinas rider GR go renewable",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "667": FamilySearchProfile(
        leaf="667",
        family_key="nc-progress-leaf-667",
        schedule_code="RIDER_EC_RY1",
        title="Economic Development Rider EC",
        search_terms=[
            "economic development rider EC",
            "rider EC",
            "leaf no 667",
        ],
        aliases=["RIDER EC", "EC", "ECONOMIC DEVELOPMENT RIDER EC"],
        include_terms=[
            "ECONOMIC DEVELOPMENT", "RIDER EC", "LEAF NO. 667",
        ],
        exclude_terms=["RIDER ED", "ECONOMIC REDEVELOPMENT"],
        ncuc_queries=[
            "Duke Energy Progress economic development rider EC",
            "Progress Energy Carolinas rider EC economic development",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "668": FamilySearchProfile(
        leaf="668",
        family_key="nc-progress-leaf-668",
        schedule_code="RIDER_NSC_RY1",
        title="Non-Residential Solar Choice Rider NSC",
        search_terms=[
            "non-residential solar choice",
            "rider NSC",
            "commercial solar choice",
            "leaf no 668",
        ],
        aliases=["RIDER NSC", "NSC", "NON-RESIDENTIAL SOLAR CHOICE"],
        include_terms=[
            "NON-RESIDENTIAL SOLAR CHOICE", "RIDER NSC", "NSC", "LEAF NO. 668",
        ],
        exclude_terms=["RESIDENTIAL SOLAR CHOICE", "RIDER SOLAR", "SHARED SOLAR"],
        ncuc_queries=[
            "Duke Energy Progress non-residential solar choice rider NSC",
            "Progress Energy Carolinas rider NSC solar choice non-residential",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "669": FamilySearchProfile(
        leaf="669",
        family_key="nc-progress-leaf-669",
        schedule_code="RIDER_NMB_RY1",
        title="Net Metering Bridge Rider NMB",
        search_terms=[
            "net metering bridge rider",
            "rider NMB",
            "net metering bridge",
            "leaf no 669",
        ],
        aliases=["RIDER NMB", "NMB", "NET METERING BRIDGE"],
        include_terms=[
            "NET METERING BRIDGE", "RIDER NMB", "NMB", "LEAF NO. 669",
        ],
        exclude_terms=["NET METERING RIDER NM", "RIDER NM", "SHARED SOLAR"],
        ncuc_queries=[
            "Duke Energy Progress net metering bridge rider NMB",
            "Progress Energy Carolinas rider NMB net metering bridge",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "670": FamilySearchProfile(
        leaf="670",
        family_key="nc-progress-leaf-670",
        schedule_code="RIDER_RSC_RY1",
        title="Residential Solar Choice Rider RSC",
        search_terms=[
            "residential solar choice rider",
            "solar choice rider",
            "solar choice program",
            "leaf no 670",
        ],
        aliases=["RIDER RSC", "RSC", "RIDER SOLAR", "SOLAR CHOICE", "RESIDENTIAL SOLAR CHOICE"],
        include_terms=[
            "RIDER RSC", "RSC", "SOLAR CHOICE", "RESIDENTIAL SOLAR", "SOLAR CHOICE RIDER",
            "LEAF NO. 670",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "CPRE"],
        ncuc_queries=[
            "Duke Energy Progress residential solar choice rider",
            "Progress Energy Carolinas solar choice rider residential",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "671": FamilySearchProfile(
        leaf="671",
        family_key="nc-progress-leaf-671",
        schedule_code="RIDER_GSAC",
        title="Green Source Advantage Confirmation Rider GSAC",
        search_terms=[
            "green source advantage confirmation",
            "rider GSAC",
            "GSAC rider",
            "leaf no 671",
        ],
        aliases=["RIDER GSAC", "GSAC", "GREEN SOURCE ADVANTAGE CONFIRMATION"],
        include_terms=[
            "GREEN SOURCE ADVANTAGE CONFIRMATION", "RIDER GSAC", "GSAC",
            "LEAF NO. 671",
        ],
        exclude_terms=["GSA", "GO RENEWABLE", "SOLAR CHOICE"],
        ncuc_queries=[
            "Duke Energy Progress green source advantage confirmation rider GSAC",
            "Progress Energy Carolinas rider GSAC green source",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "672": FamilySearchProfile(
        leaf="672",
        family_key="nc-progress-leaf-672",
        schedule_code="RIDER_CEI",
        title="Clean Energy Impact Rider CEI",
        search_terms=[
            "clean energy impact rider",
            "rider CEI",
            "CEI rider",
            "clean energy impact",
            "leaf no 672",
        ],
        aliases=["RIDER CEI", "CEI", "CLEAN ENERGY IMPACT RIDER"],
        include_terms=[
            "CLEAN ENERGY IMPACT RIDER", "RIDER CEI", "CLEAN ENERGY IMPACT",
            "PER MONTH", "PER CUSTOMER", "LEAF NO. 672",
        ],
        exclude_terms=["RENEWABLE ADVANTAGE", "CPRE", "DSM RIDER", "EE RIDER"],
        ncuc_queries=[
            "Duke Energy Progress clean energy impact rider CEI",
            "Progress Energy Carolinas rider CEI clean energy impact",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "674": FamilySearchProfile(
        leaf="674",
        family_key="nc-progress-leaf-674",
        schedule_code="RIDER_PS",
        title="Powershare Nonresidential Load Curtailment Rider PS",
        search_terms=[
            "powershare nonresidential load curtailment",
            "rider PS",
            "powershare curtailment",
            "leaf no 674",
        ],
        aliases=["RIDER PS", "PS", "POWERSHARE", "NONRESIDENTIAL CURTAILMENT"],
        include_terms=[
            "POWERSHARE", "RIDER PS", "NONRESIDENTIAL LOAD CURTAILMENT",
            "LEAF NO. 674",
        ],
        exclude_terms=["RESIDENTIAL", "RIDER LLC"],
        ncuc_queries=[
            "Duke Energy Progress powershare nonresidential load curtailment rider PS",
            "Progress Energy Carolinas rider PS powershare curtailment",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # DSM / EE / Programs — Conservation and Efficiency
    # ------------------------------------------------------------------
    "700": FamilySearchProfile(
        leaf="700",
        family_key="nc-progress-leaf-700",
        schedule_code="PROGRAM_NSSEE",
        title="Non-Residential Smart Saver EE Products and Assessment Program",
        search_terms=[
            "non-residential smart saver",
            "NSSEE",
            "smart saver energy efficiency",
            "leaf no 700",
        ],
        aliases=["NSSEE", "NON-RESIDENTIAL SMART SAVER", "SMART SAVER EE"],
        include_terms=[
            "NON-RESIDENTIAL SMART SAVER", "NSSEE", "ENERGY EFFICIENCY",
            "LEAF NO. 700",
        ],
        exclude_terms=["RESIDENTIAL", "DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress non-residential smart saver energy efficiency NSSEE",
            "Progress Energy Carolinas non-residential energy efficiency program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "701": FamilySearchProfile(
        leaf="701",
        family_key="nc-progress-leaf-701",
        schedule_code="PROGRAM_SBES",
        title="Business Energy Saver Program SBES",
        search_terms=[
            "business energy saver program",
            "SBES",
            "leaf no 701",
        ],
        aliases=["SBES", "BUSINESS ENERGY SAVER"],
        include_terms=["BUSINESS ENERGY SAVER", "SBES", "LEAF NO. 701"],
        exclude_terms=["RESIDENTIAL", "SMART SAVER EE"],
        ncuc_queries=[
            "Duke Energy Progress business energy saver SBES",
            "Progress Energy Carolinas business energy saver program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "702": FamilySearchProfile(
        leaf="702",
        family_key="nc-progress-leaf-702",
        schedule_code="PROGRAM_SSP",
        title="Non-Residential Smart Saver Performance Incentive Program SSP",
        search_terms=[
            "smart saver performance incentive",
            "SSP program",
            "leaf no 702",
        ],
        aliases=["SSP", "SMART SAVER PERFORMANCE INCENTIVE"],
        include_terms=["SMART SAVER PERFORMANCE INCENTIVE", "SSP", "LEAF NO. 702"],
        exclude_terms=["RESIDENTIAL", "DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress smart saver performance incentive SSP",
            "Progress Energy Carolinas non-residential smart saver performance",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "703": FamilySearchProfile(
        leaf="703",
        family_key="nc-progress-leaf-703",
        schedule_code="PROGRAM_RSNES",
        title="Residential Service Neighborhood Energy Saver Program RSNES",
        search_terms=[
            "neighborhood energy saver",
            "RSNES",
            "residential neighborhood energy",
            "leaf no 703",
        ],
        aliases=["RSNES", "NEIGHBORHOOD ENERGY SAVER"],
        include_terms=["NEIGHBORHOOD ENERGY SAVER", "RSNES", "LEAF NO. 703"],
        exclude_terms=["NON-RESIDENTIAL", "DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress neighborhood energy saver RSNES",
            "Progress Energy Carolinas residential neighborhood energy saver",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "704": FamilySearchProfile(
        leaf="704",
        family_key="nc-progress-leaf-704",
        schedule_code="PROGRAM_RSSEE",
        title="Residential Service Smart Saver Energy Efficiency Program RSSEE",
        search_terms=[
            "residential smart saver energy efficiency",
            "RSSEE",
            "leaf no 704",
        ],
        aliases=["RSSEE", "RESIDENTIAL SMART SAVER EE"],
        include_terms=["RSSEE", "RESIDENTIAL SMART SAVER", "LEAF NO. 704"],
        exclude_terms=["NON-RESIDENTIAL", "DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress residential smart saver energy efficiency RSSEE",
            "Progress Energy Carolinas residential smart saver program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "705": FamilySearchProfile(
        leaf="705",
        family_key="nc-progress-leaf-705",
        schedule_code="PROGRAM_EEL",
        title="Energy Efficient Lighting Program EEL",
        search_terms=[
            "energy efficient lighting program",
            "EEL program",
            "leaf no 705",
        ],
        aliases=["EEL", "ENERGY EFFICIENT LIGHTING"],
        include_terms=["ENERGY EFFICIENT LIGHTING", "EEL", "LEAF NO. 705"],
        exclude_terms=["DSM RIDER", "STREET LIGHTING"],
        ncuc_queries=[
            "Duke Energy Progress energy efficient lighting program EEL",
            "Progress Energy Carolinas energy efficient lighting",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "706": FamilySearchProfile(
        leaf="706",
        family_key="nc-progress-leaf-706",
        schedule_code="",
        title="EnergyWise for Business Program EWB",
        search_terms=[
            "EnergyWise for Business",
            "EWB program",
            "energywise business",
            "leaf no 706",
        ],
        aliases=["EWB", "ENERGYWISE FOR BUSINESS", "ENERGYWISE BUSINESS"],
        include_terms=["ENERGYWISE FOR BUSINESS", "EWB", "LEAF NO. 706"],
        exclude_terms=["RESIDENTIAL", "DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress EnergyWise for Business EWB",
            "Progress Energy Carolinas EnergyWise business program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "707": FamilySearchProfile(
        leaf="707",
        family_key="nc-progress-leaf-707",
        schedule_code="PROGRAM_RS_HERP",
        title="Residential Service My Home Energy Report Program RS-HERP",
        search_terms=[
            "home energy report program",
            "RS-HERP",
            "my home energy report",
            "leaf no 707",
        ],
        aliases=["RS-HERP", "HOME ENERGY REPORT", "MY HOME ENERGY REPORT"],
        include_terms=["HOME ENERGY REPORT", "RS-HERP", "LEAF NO. 707"],
        exclude_terms=["DSM RIDER", "WEATHERIZATION"],
        ncuc_queries=[
            "Duke Energy Progress home energy report RS-HERP",
            "Progress Energy Carolinas my home energy report program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "708": FamilySearchProfile(
        leaf="708",
        family_key="nc-progress-leaf-708",
        schedule_code="PROGRAM_RNC",
        title="Residential Service Residential New Construction Program RNC",
        search_terms=[
            "residential new construction program",
            "RNC program",
            "new construction energy efficiency",
            "leaf no 708",
        ],
        aliases=["RNC", "RESIDENTIAL NEW CONSTRUCTION", "NEW CONSTRUCTION PROGRAM"],
        include_terms=["RESIDENTIAL NEW CONSTRUCTION", "RNC", "LEAF NO. 708"],
        exclude_terms=["DSM RIDER", "WEATHERIZATION"],
        ncuc_queries=[
            "Duke Energy Progress residential new construction RNC",
            "Progress Energy Carolinas residential new construction program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "709": FamilySearchProfile(
        leaf="709",
        family_key="nc-progress-leaf-709",
        schedule_code="PROGRAM_EEE",
        title="Residential Service Energy Efficiency Education Program EEE",
        search_terms=[
            "energy efficiency education program",
            "EEE program",
            "residential energy education",
            "leaf no 709",
        ],
        aliases=["EEE", "ENERGY EFFICIENCY EDUCATION"],
        include_terms=["ENERGY EFFICIENCY EDUCATION", "EEE", "LEAF NO. 709"],
        exclude_terms=["DSM RIDER", "WEATHERIZATION"],
        ncuc_queries=[
            "Duke Energy Progress energy efficiency education program EEE",
            "Progress Energy Carolinas residential energy efficiency education",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "710": FamilySearchProfile(
        leaf="710",
        family_key="nc-progress-leaf-710",
        schedule_code="PROGRAM_MEE",
        title="Residential Service Multi-Family Energy Efficiency Program MEE",
        search_terms=[
            "multi-family energy efficiency",
            "MEE program",
            "multifamily energy efficiency",
            "leaf no 710",
        ],
        aliases=["MEE", "MULTI-FAMILY ENERGY EFFICIENCY"],
        include_terms=["MULTI-FAMILY ENERGY EFFICIENCY", "MEE", "LEAF NO. 710"],
        exclude_terms=["DSM RIDER", "SINGLE FAMILY"],
        ncuc_queries=[
            "Duke Energy Progress multi-family energy efficiency program MEE",
            "Progress Energy Carolinas multi-family energy efficiency",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "711": FamilySearchProfile(
        leaf="711",
        family_key="nc-progress-leaf-711",
        schedule_code="PROGRAM_REA",
        title="Residential Energy Assessment Program REA",
        search_terms=[
            "residential energy assessment",
            "REA program",
            "home energy assessment",
            "leaf no 711",
        ],
        aliases=["REA", "RESIDENTIAL ENERGY ASSESSMENT", "HOME ENERGY ASSESSMENT"],
        include_terms=["RESIDENTIAL ENERGY ASSESSMENT", "REA", "LEAF NO. 711"],
        exclude_terms=["DSM RIDER"],
        ncuc_queries=[
            "Duke Energy Progress residential energy assessment REA",
            "Progress Energy Carolinas residential energy assessment program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "712": FamilySearchProfile(
        leaf="712",
        family_key="nc-progress-leaf-712",
        schedule_code="PROGRAM_LWP",
        title="Low-Income Weatherization Pay For Performance Program LWP",
        search_terms=[
            "low income weatherization",
            "LWP program",
            "weatherization pay for performance",
            "leaf no 712",
        ],
        aliases=["LWP", "LOW-INCOME WEATHERIZATION", "WEATHERIZATION PAY FOR PERFORMANCE"],
        include_terms=["LOW-INCOME WEATHERIZATION", "LWP", "PAY FOR PERFORMANCE", "LEAF NO. 712"],
        exclude_terms=["DSM RIDER", "MULTI-FAMILY"],
        ncuc_queries=[
            "Duke Energy Progress low income weatherization LWP",
            "Progress Energy Carolinas weatherization pay for performance",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "713": FamilySearchProfile(
        leaf="713",
        family_key="nc-progress-leaf-713",
        schedule_code="PROGRAM_REEAD",
        title="Residential Energy Efficient Appliances and Devices Program REEAD",
        search_terms=[
            "residential energy efficient appliances",
            "REEAD program",
            "energy efficient appliances devices",
            "leaf no 713",
        ],
        aliases=["REEAD", "ENERGY EFFICIENT APPLIANCES", "APPLIANCES AND DEVICES"],
        include_terms=["RESIDENTIAL ENERGY EFFICIENT APPLIANCES", "REEAD", "LEAF NO. 713"],
        exclude_terms=["DSM RIDER", "WEATHERIZATION"],
        ncuc_queries=[
            "Duke Energy Progress residential energy efficient appliances REEAD",
            "Progress Energy Carolinas energy efficient appliances devices",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "714": FamilySearchProfile(
        leaf="714",
        family_key="nc-progress-leaf-714",
        schedule_code="PROGRAM_LC_WIN",
        title="Residential Service Load Control (Asheville Area) LC-WIN",
        search_terms=[
            "load control Asheville",
            "LC-WIN",
            "residential load control Asheville",
            "leaf no 714",
        ],
        aliases=["LC-WIN", "LOAD CONTROL ASHEVILLE", "ASHEVILLE LOAD CONTROL"],
        include_terms=["LOAD CONTROL", "LC-WIN", "ASHEVILLE", "LEAF NO. 714"],
        exclude_terms=["DSM RIDER", "DEMAND RESPONSE"],
        ncuc_queries=[
            "Duke Energy Progress load control Asheville LC-WIN",
            "Progress Energy Carolinas residential load control Asheville area",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "715": FamilySearchProfile(
        leaf="715",
        family_key="nc-progress-leaf-715",
        schedule_code="PROGRAM_LC",
        title="Residential Service Load Control LC",
        search_terms=[
            "residential load control",
            "load control program",
            "LC program",
            "leaf no 715",
        ],
        aliases=["LC", "LOAD CONTROL PROGRAM", "RESIDENTIAL LOAD CONTROL"],
        include_terms=["RESIDENTIAL LOAD CONTROL", "LOAD CONTROL", "LC", "LEAF NO. 715"],
        exclude_terms=["DSM RIDER", "DEMAND RESPONSE AUTOMATION"],
        ncuc_queries=[
            "Duke Energy Progress residential load control LC",
            "Progress Energy Carolinas residential load control program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "716": FamilySearchProfile(
        leaf="716",
        family_key="nc-progress-leaf-716",
        schedule_code="PROGRAM_SSR",
        title="Residential Service SunSense Solar Rebate Program SSR",
        search_terms=[
            "SunSense solar rebate",
            "SSR program",
            "solar rebate program",
            "leaf no 716",
        ],
        aliases=["SSR", "SUNSENSE SOLAR REBATE", "SOLAR REBATE PROGRAM"],
        include_terms=["SUNSENSE SOLAR REBATE", "SSR", "SOLAR REBATE", "LEAF NO. 716"],
        exclude_terms=["NET METERING", "SHARED SOLAR", "RIDER SRR"],
        ncuc_queries=[
            "Duke Energy Progress SunSense solar rebate program SSR",
            "Progress Energy Carolinas solar rebate program SunSense",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "717": FamilySearchProfile(
        leaf="717",
        family_key="nc-progress-leaf-717",
        schedule_code="PROGRAM_DRA",
        title="Demand Response Automation Rider DRA",
        search_terms=[
            "demand response automation rider",
            "rider DRA",
            "DRA rider",
            "demand response automation",
            "leaf no 717",
        ],
        aliases=["RIDER DRA", "DRA", "DEMAND RESPONSE AUTOMATION"],
        include_terms=["DEMAND RESPONSE AUTOMATION", "RIDER DRA", "DRA", "LEAF NO. 717"],
        exclude_terms=["DSM RIDER", "LOAD CONTROL"],
        ncuc_queries=[
            "Duke Energy Progress demand response automation rider DRA",
            "Progress Energy Carolinas rider DRA demand response automation",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "718": FamilySearchProfile(
        leaf="718",
        family_key="nc-progress-leaf-718",
        schedule_code="PROGRAM_CAP",
        title="Customer Assistance Program Credit CAP",
        search_terms=[
            "customer assistance program credit",
            "CAP credit",
            "low income customer assistance",
            "leaf no 718",
        ],
        aliases=["CAP", "CUSTOMER ASSISTANCE PROGRAM", "CAP CREDIT"],
        include_terms=["CUSTOMER ASSISTANCE PROGRAM", "CAP", "LEAF NO. 718"],
        exclude_terms=["RIDER CAR", "STORM RECOVERY"],
        ncuc_queries=[
            "Duke Energy Progress customer assistance program credit CAP",
            "Progress Energy Carolinas customer assistance program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "719": FamilySearchProfile(
        leaf="719",
        family_key="nc-progress-leaf-719",
        schedule_code="PROGRAM_IWZ",
        title="Residential Income-Qualified Energy Efficiency and Weatherization Program IWZ",
        search_terms=[
            "income-qualified weatherization",
            "IWZ program",
            "income qualified energy efficiency weatherization",
            "leaf no 719",
        ],
        aliases=["IWZ", "INCOME-QUALIFIED WEATHERIZATION", "INCOME QUALIFIED EE"],
        include_terms=["INCOME-QUALIFIED", "IWZ", "WEATHERIZATION", "LEAF NO. 719"],
        exclude_terms=["LOW INCOME WEATHERIZATION LWP", "MULTI-FAMILY"],
        ncuc_queries=[
            "Duke Energy Progress income-qualified weatherization IWZ",
            "Progress Energy Carolinas income qualified energy efficiency weatherization",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "720": FamilySearchProfile(
        leaf="720",
        family_key="nc-progress-leaf-720",
        schedule_code="PROGRAM_PPA",
        title="Prepaid Advantage Program PPA",
        search_terms=[
            "prepaid advantage program",
            "PPA program",
            "prepaid service program",
            "leaf no 720",
        ],
        aliases=["PPA", "PREPAID ADVANTAGE PROGRAM", "PREPAID SERVICE"],
        include_terms=["PREPAID ADVANTAGE", "PPA", "LEAF NO. 720"],
        exclude_terms=["PREPAY RIDER", "PURCHASED POWER"],
        ncuc_queries=[
            "Duke Energy Progress prepaid advantage program PPA",
            "Progress Energy Carolinas prepaid advantage",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "721": FamilySearchProfile(
        leaf="721",
        family_key="nc-progress-leaf-721",
        schedule_code="PROGRAM_TOB",
        title="Residential Service Tariffed On-Bill Program TOB",
        search_terms=[
            "tariffed on-bill program",
            "TOB program",
            "on-bill financing",
            "leaf no 721",
        ],
        aliases=["TOB", "TARIFFED ON-BILL", "ON-BILL FINANCING"],
        include_terms=["TARIFFED ON-BILL", "TOB", "ON-BILL FINANCING", "LEAF NO. 721"],
        exclude_terms=["MULTI-FAMILY", "PREPAID"],
        ncuc_queries=[
            "Duke Energy Progress tariffed on-bill program TOB",
            "Progress Energy Carolinas on-bill financing program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "722": FamilySearchProfile(
        leaf="722",
        family_key="nc-progress-leaf-722",
        schedule_code="PROGRAM_TOBM",
        title="Residential Multi-Family New Construction Tariffed On-Bill Program TOBM",
        search_terms=[
            "multi-family new construction on-bill",
            "TOBM program",
            "leaf no 722",
        ],
        aliases=["TOBM", "MULTI-FAMILY ON-BILL", "MULTI-FAMILY NEW CONSTRUCTION ON-BILL"],
        include_terms=["MULTI-FAMILY NEW CONSTRUCTION", "TOBM", "ON-BILL", "LEAF NO. 722"],
        exclude_terms=["RESIDENTIAL TOB", "SINGLE FAMILY"],
        ncuc_queries=[
            "Duke Energy Progress multi-family new construction on-bill TOBM",
            "Progress Energy Carolinas multi-family on-bill program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "723": FamilySearchProfile(
        leaf="723",
        family_key="nc-progress-leaf-723",
        schedule_code="PROGRAM_TOBR",
        title="Residential Smart Saver Energy Efficiency Program Early Retirement TOBR",
        search_terms=[
            "smart saver early retirement",
            "TOBR program",
            "leaf no 723",
        ],
        aliases=["TOBR", "SMART SAVER EARLY RETIREMENT"],
        include_terms=["SMART SAVER EARLY RETIREMENT", "TOBR", "LEAF NO. 723"],
        exclude_terms=["TOB", "TOBM"],
        ncuc_queries=[
            "Duke Energy Progress smart saver energy efficiency early retirement TOBR",
            "Progress Energy Carolinas smart saver early retirement program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "724": FamilySearchProfile(
        leaf="724",
        family_key="nc-progress-leaf-724",
        schedule_code="YOUR_FIXEDBILL_YFB",
        title="Residential Your FixedBill Program YFB",
        search_terms=[
            "your fixedbill program",
            "YFB",
            "fixed bill program",
            "leaf no 724",
        ],
        aliases=["YFB", "YOUR FIXEDBILL", "FIXED BILL PROGRAM"],
        include_terms=["YOUR FIXEDBILL", "YFB", "FIXED BILL", "LEAF NO. 724"],
        exclude_terms=["PREPAID ADVANTAGE", "PREPAY RIDER"],
        ncuc_queries=[
            "Duke Energy Progress Your FixedBill program YFB",
            "Progress Energy Carolinas fixed bill program residential",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "725": FamilySearchProfile(
        leaf="725",
        family_key="nc-progress-leaf-725",
        schedule_code="PROGRAM_RIQLC",
        title="Residential Income-Qualified Load Control Program RIQLC",
        search_terms=[
            "income-qualified load control",
            "RIQLC",
            "residential income qualified load control",
            "leaf no 725",
        ],
        aliases=["RIQLC", "INCOME-QUALIFIED LOAD CONTROL"],
        include_terms=["INCOME-QUALIFIED LOAD CONTROL", "RIQLC", "LEAF NO. 725"],
        exclude_terms=["LC PROGRAM", "DEMAND RESPONSE"],
        ncuc_queries=[
            "Duke Energy Progress income-qualified load control RIQLC",
            "Progress Energy Carolinas residential income qualified load control",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Programs — EV / Transportation
    # ------------------------------------------------------------------
    "740": FamilySearchProfile(
        leaf="740",
        family_key="nc-progress-leaf-740",
        schedule_code="PROGRAM_EVSB",
        title="Electric Vehicle School Bus Charging Station Program EVSB",
        search_terms=[
            "electric vehicle school bus",
            "EVSB program",
            "school bus charging station",
            "leaf no 740",
        ],
        aliases=["EVSB", "SCHOOL BUS CHARGING", "EV SCHOOL BUS"],
        include_terms=["ELECTRIC VEHICLE SCHOOL BUS", "EVSB", "LEAF NO. 740"],
        exclude_terms=["PUBLIC FAST CHARGING", "LEVEL 2", "MAKE READY"],
        ncuc_queries=[
            "Duke Energy Progress electric vehicle school bus charging EVSB",
            "Progress Energy Carolinas school bus EV charging program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "741": FamilySearchProfile(
        leaf="741",
        family_key="nc-progress-leaf-741",
        schedule_code="PROGRAM_FCS",
        title="Public Fast Charging Station Program FCS",
        search_terms=[
            "public fast charging station",
            "FCS program",
            "fast charging EV",
            "leaf no 741",
        ],
        aliases=["FCS", "PUBLIC FAST CHARGING", "FAST CHARGING STATION"],
        include_terms=["PUBLIC FAST CHARGING", "FCS", "DCFC", "LEAF NO. 741"],
        exclude_terms=["SCHOOL BUS", "LEVEL 2", "MAKE READY"],
        ncuc_queries=[
            "Duke Energy Progress public fast charging station FCS",
            "Progress Energy Carolinas fast charging station program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "742": FamilySearchProfile(
        leaf="742",
        family_key="nc-progress-leaf-742",
        schedule_code="PROGRAM_L2EV",
        title="Public Level 2 Charging Station Program L2EV",
        search_terms=[
            "level 2 charging station",
            "L2EV program",
            "public level 2 EV charging",
            "leaf no 742",
        ],
        aliases=["L2EV", "LEVEL 2 CHARGING", "PUBLIC LEVEL 2 EV"],
        include_terms=["PUBLIC LEVEL 2 CHARGING", "L2EV", "LEVEL 2", "LEAF NO. 742"],
        exclude_terms=["FAST CHARGING", "SCHOOL BUS", "MAKE READY"],
        ncuc_queries=[
            "Duke Energy Progress public level 2 charging station L2EV",
            "Progress Energy Carolinas level 2 EV charging program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "743": FamilySearchProfile(
        leaf="743",
        family_key="nc-progress-leaf-743",
        schedule_code="PROGRAM_MFEV",
        title="Multi-Family Dwelling Charging Station Program MFEV",
        search_terms=[
            "multi-family EV charging",
            "MFEV program",
            "multi-family dwelling charging",
            "leaf no 743",
        ],
        aliases=["MFEV", "MULTI-FAMILY EV CHARGING", "MULTI-FAMILY CHARGING STATION"],
        include_terms=["MULTI-FAMILY", "MFEV", "EV CHARGING", "LEAF NO. 743"],
        exclude_terms=["PUBLIC FAST CHARGING", "SCHOOL BUS", "LEVEL 2 L2EV"],
        ncuc_queries=[
            "Duke Energy Progress multi-family dwelling charging station MFEV",
            "Progress Energy Carolinas multi-family EV charging program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "744": FamilySearchProfile(
        leaf="744",
        family_key="nc-progress-leaf-744",
        schedule_code="PROGRAM_MREV",
        title="Electric Vehicle Make Ready Infrastructure Program MREV",
        search_terms=[
            "make ready infrastructure",
            "MREV program",
            "EV make ready",
            "leaf no 744",
        ],
        aliases=["MREV", "MAKE READY INFRASTRUCTURE", "EV MAKE READY"],
        include_terms=["MAKE READY", "MREV", "EV INFRASTRUCTURE", "LEAF NO. 744"],
        exclude_terms=["FAST CHARGING FCS", "SCHOOL BUS EVSB"],
        ncuc_queries=[
            "Duke Energy Progress EV make ready infrastructure MREV",
            "Progress Energy Carolinas make ready infrastructure program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "745": FamilySearchProfile(
        leaf="745",
        family_key="nc-progress-leaf-745",
        schedule_code="EVSE_RY1",
        title="Electric Vehicle Service Equipment Schedule EVSE",
        search_terms=[
            "electric vehicle service equipment",
            "EVSE schedule",
            "EV service equipment rate",
            "leaf no 745",
        ],
        aliases=["EVSE", "SCHEDULE EVSE", "ELECTRIC VEHICLE SERVICE EQUIPMENT"],
        include_terms=["ELECTRIC VEHICLE SERVICE EQUIPMENT", "EVSE", "LEAF NO. 745"],
        exclude_terms=["MAKE READY", "FAST CHARGING"],
        ncuc_queries=[
            "Duke Energy Progress electric vehicle service equipment EVSE schedule",
            "Progress Energy Carolinas EVSE schedule",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Programs — Other / Miscellaneous
    # ------------------------------------------------------------------
    "770": FamilySearchProfile(
        leaf="770",
        family_key="nc-progress-leaf-770",
        schedule_code="POWER_PAIR_PROGRAM_INSTALLATION_PPSB",
        title="Power Pair Program Installation PPSB",
        search_terms=[
            "power pair program",
            "PPSB",
            "battery storage installation",
            "leaf no 770",
        ],
        aliases=["PPSB", "POWER PAIR PROGRAM", "POWER PAIR BATTERY STORAGE"],
        include_terms=["POWER PAIR", "PPSB", "BATTERY STORAGE", "LEAF NO. 770"],
        exclude_terms=["SOLAR CHOICE", "NET METERING"],
        ncuc_queries=[
            "Duke Energy Progress power pair program installation PPSB",
            "Progress Energy Carolinas power pair battery storage program",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    # ------------------------------------------------------------------
    # Service Regulations / Administrative Schedules
    # ------------------------------------------------------------------
    "800": FamilySearchProfile(
        leaf="800",
        family_key="nc-progress-leaf-800",
        schedule_code="SERVICE_REGULATIONS",
        title="Service Regulations",
        search_terms=[
            "service regulations",
            "rules and regulations",
            "electric service regulations",
            "leaf no 800",
        ],
        aliases=["SERVICE REGULATIONS", "RULES AND REGULATIONS", "ELECTRIC SERVICE REGULATIONS"],
        include_terms=["SERVICE REGULATIONS", "RULES AND REGULATIONS", "LEAF NO. 800"],
        exclude_terms=["RIDER", "RATE SCHEDULE"],
        ncuc_queries=[
            "Duke Energy Progress service regulations",
            "Progress Energy Carolinas electric service regulations rules",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "801": FamilySearchProfile(
        leaf="801",
        family_key="nc-progress-leaf-801",
        schedule_code="OUTDOOR_LIGHTING_SERVICE_REGULATIONS",
        title="Outdoor Lighting Service Regulations",
        search_terms=[
            "outdoor lighting service regulations",
            "outdoor lighting regulations",
            "leaf no 801",
        ],
        aliases=["OUTDOOR LIGHTING SERVICE REGULATIONS", "OUTDOOR LIGHTING REGULATIONS"],
        include_terms=["OUTDOOR LIGHTING SERVICE REGULATIONS", "LEAF NO. 801"],
        exclude_terms=["RIDER", "RATE SCHEDULE", "STREET LIGHTING"],
        ncuc_queries=[
            "Duke Energy Progress outdoor lighting service regulations",
            "Progress Energy Carolinas outdoor lighting regulations",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "802": FamilySearchProfile(
        leaf="802",
        family_key="nc-progress-leaf-802",
        schedule_code="LINE_EXTENSION_PLAN",
        title="Line Extension Plan LEP",
        search_terms=[
            "line extension plan",
            "LEP",
            "line extension policy",
            "leaf no 802",
        ],
        aliases=["LEP", "LINE EXTENSION PLAN", "LINE EXTENSION POLICY"],
        include_terms=["LINE EXTENSION PLAN", "LEP", "LINE EXTENSION", "LEAF NO. 802"],
        exclude_terms=["RIDER", "RATE SCHEDULE"],
        ncuc_queries=[
            "Duke Energy Progress line extension plan LEP",
            "Progress Energy Carolinas line extension plan",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),

    "803": FamilySearchProfile(
        leaf="803",
        family_key="nc-progress-leaf-803",
        schedule_code="STANDARD_SERVICE_VOLTAGES",
        title="Standard Service Voltages",
        search_terms=[
            "standard service voltages",
            "service voltage standards",
            "leaf no 803",
        ],
        aliases=["STANDARD SERVICE VOLTAGES", "SERVICE VOLTAGES"],
        include_terms=["STANDARD SERVICE VOLTAGES", "LEAF NO. 803"],
        exclude_terms=["RIDER", "RATE SCHEDULE"],
        ncuc_queries=[
            "Duke Energy Progress standard service voltages",
            "Progress Energy Carolinas standard service voltages",
        ],
        docket_hints=["E-2 Sub 1190"],
    ),
}


def get_profile(leaf: str) -> FamilySearchProfile | None:
    """Return the search profile for a given leaf number string."""
    return FAMILY_PROFILES.get(str(leaf))


def all_profiles() -> list[FamilySearchProfile]:
    """Return all profiles sorted by leaf number."""
    return sorted(FAMILY_PROFILES.values(), key=lambda p: int(p.leaf))


def profiles_by_family_key(family_key: str) -> FamilySearchProfile | None:
    """Look up a profile by DB family_key."""
    for p in FAMILY_PROFILES.values():
        if p.family_key == family_key:
            return p
    return None


def all_ncuc_queries() -> list[tuple[str, str]]:
    """Return all (query_text, leaf) pairs for use in search pipeline."""
    pairs: list[tuple[str, str]] = []
    for p in all_profiles():
        for q in p.ncuc_queries:
            pairs.append((q, p.leaf))
    return pairs
