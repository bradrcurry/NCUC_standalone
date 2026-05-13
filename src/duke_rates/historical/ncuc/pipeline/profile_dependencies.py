from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ParserProfileImpactRule:
    """Declarative targeting rule for reparsing after a profile/routing change."""

    parser_profile: str
    family_keys: tuple[str, ...] = ()
    family_prefixes: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    candidate_reason_tokens: tuple[str, ...] = ()
    required_signal_flags: tuple[str, ...] = ()
    gating_signal_flags: tuple[str, ...] = ()
    description: str = ""

    def to_metadata(self) -> dict[str, object]:
        return asdict(self)

    def match_reasons(
        self,
        *,
        family_key: str | None,
        company: str | None,
        latest_parser_profile: str | None,
        candidate_profiles: list[dict[str, object]] | None = None,
        signals: dict[str, object] | None = None,
    ) -> list[str]:
        normalized_family = (family_key or "").lower()
        normalized_company = (company or "").lower()
        normalized_latest = (latest_parser_profile or "").lower()
        candidate_profiles = candidate_profiles or []
        signals = signals or {}

        reasons: list[str] = []
        if normalized_latest == self.parser_profile:
            reasons.append("latest_parser_profile")

        if normalized_family and normalized_family in self.family_keys:
            reasons.append("family_key")

        if normalized_family and any(normalized_family.startswith(prefix) for prefix in self.family_prefixes):
            reasons.append("family_prefix")

        for candidate in candidate_profiles:
            candidate_name = str(candidate.get("name") or "").lower()
            if candidate_name != self.parser_profile:
                continue
            if candidate.get("supported") or float(candidate.get("score") or 0.0) > 0:
                reasons.append("candidate_profile")
            candidate_reasons = {str(reason).lower() for reason in candidate.get("reasons") or []}
            if self.candidate_reason_tokens and candidate_reasons.intersection(self.candidate_reason_tokens):
                reasons.append("candidate_reason")
            break

        if self.gating_signal_flags and not all(bool(signals.get(flag)) for flag in self.gating_signal_flags):
            return []

        if reasons and self.required_signal_flags and all(bool(signals.get(flag)) for flag in self.required_signal_flags):
            reasons.append("signal_match")

        if reasons and self.companies and normalized_company not in self.companies:
            return []

        return list(dict.fromkeys(reasons))


_PROFILE_IMPACT_RULES: dict[str, ParserProfileImpactRule] = {
    "progress_residential_tou": ParserProfileImpactRule(
        parser_profile="progress_residential_tou",
        family_keys=(
            "nc-progress-leaf-502",
            "nc-progress-leaf-503",
            "nc-progress-leaf-504",
        ),
        companies=("progress",),
        candidate_reason_tokens=("progress_tou_family", "tou_terms", "discount_terms", "demand_terms"),
        required_signal_flags=("has_tou_terms", "has_progress_company_text"),
        description="DEP residential TOU leaves and documents already parsed by the Progress TOU profile.",
    ),
    "progress_residential_flat": ParserProfileImpactRule(
        parser_profile="progress_residential_flat",
        family_keys=(
            "nc-progress-leaf-500",
            "nc-progress-leaf-505",
        ),
        companies=("progress",),
        candidate_reason_tokens=("progress_family", "flat_rate_markers", "leaf500", "all_kwh"),
        description="DEP flat residential sheets already parsed by, or strong candidates for, the Progress flat-residential profile.",
    ),
    "progress_current_leaf_bridge": ParserProfileImpactRule(
        parser_profile="progress_current_leaf_bridge",
        family_keys=(
            "nc-progress-leaf-501",
            "nc-progress-leaf-520",
            "nc-progress-leaf-535",
            "nc-progress-leaf-674",
        ),
        companies=("progress",),
        candidate_reason_tokens=(
            "current_progress_pdf",
            "leaf501_r_toud",
            "leaf520_sgs",
            "leaf535_hp",
            "leaf674_rider_ps",
            "tou_terms",
            "demand_terms",
        ),
        gating_signal_flags=("is_current_progress_pdf",),
        description="Current-style DEP leaf PDFs that can be reparsed through the shared Progress current-leaf bridge profile.",
    ),
    "progress_specialty_rider": ParserProfileImpactRule(
        parser_profile="progress_specialty_rider",
        family_keys=(
            "nc-progress-leaf-654",
            "nc-progress-leaf-655",
            "nc-progress-leaf-668",
            "nc-progress-leaf-670",
        ),
        companies=("progress",),
        candidate_reason_tokens=(
            "current_progress_pdf",
            "leaf654_rider_nfs",
            "leaf655_rider_llc",
            "leaf668_rider_nsc",
            "leaf670_rider_rsc",
            "monthly_rate",
            "credit_terms",
            "demand_terms",
        ),
        gating_signal_flags=("is_current_progress_pdf",),
        description="Current-style DEP specialty riders with explicit fee or net-excess-energy credit terms.",
    ),
    "progress_energywise_business": ParserProfileImpactRule(
        parser_profile="progress_energywise_business",
        family_keys=("nc-progress-leaf-706", "nc-carolinas-rider-eb"),
        companies=("progress", "carolinas"),
        candidate_reason_tokens=("leaf706_ewb", "rider_eb", "control_credits", "summer_cycling", "non_winter_cycling", "bring_your_own_kw"),
        description="DEP and DEC EnergyWise for Business tariff sheets with annual control credits and bring-your-own-kW incentive language.",
    ),
    "progress_solar_rebate_rider": ParserProfileImpactRule(
        parser_profile="progress_solar_rebate_rider",
        family_keys=("nc-progress-leaf-663",),
        companies=("progress",),
        candidate_reason_tokens=("leaf663_srr", "per_watt_terms", "rebate_payment", "ac_nameplate"),
        description="DEP Solar Rebate Rider SRR sheets with one-time $/watt incentive payments.",
    ),
    "progress_sunsense_solar_rebate": ParserProfileImpactRule(
        parser_profile="progress_sunsense_solar_rebate",
        family_keys=("nc-progress-leaf-716",),
        companies=("progress",),
        candidate_reason_tokens=("leaf716_ssr", "ssr_credit", "participation_payment", "termination_charge"),
        description="DEP SunSense Solar Rebate sheets with one-time participation payments and monthly SSR credits.",
    ),
    "progress_meter_related_optional_programs": ParserProfileImpactRule(
        parser_profile="progress_meter_related_optional_programs",
        family_keys=("nc-progress-leaf-661",),
        companies=("progress",),
        candidate_reason_tokens=("leaf661_mrop", "meter_optional_programs", "totalmeter", "energy_profiler_online", "manually_read_metering"),
        description="DEP Rider MROP sheets with TotalMeter, EPO, and manually read metering monthly fees.",
    ),
    "progress_standby_service": ParserProfileImpactRule(
        parser_profile="progress_standby_service",
        family_keys=("nc-progress-leaf-653",),
        companies=("progress",),
        candidate_reason_tokens=("leaf653_standby_service", "generation_reservation_charge", "standby_delivery_charge", "incentive_margin"),
        description="DEP supplementary and firm standby service rider sheets with reservation and standby delivery charges.",
    ),
    "progress_customer_assistance_recovery": ParserProfileImpactRule(
        parser_profile="progress_customer_assistance_recovery",
        family_keys=("nc-progress-leaf-611",),
        companies=("progress",),
        candidate_reason_tokens=("leaf611_car", "billing_table", "rate_class", "general_service"),
        description="DEP Rider CAR sheets with residential and general-service billing adjustments.",
    ),
    "progress_storm_securitization": ParserProfileImpactRule(
        parser_profile="progress_storm_securitization",
        family_keys=("nc-progress-leaf-613", "nc-progress-leaf-607"),
        companies=("progress",),
        candidate_reason_tokens=("leaf613_sts", "leaf607_sts", "billing_rate_table", "applicable_schedules", "rate_class"),
        description="DEP Rider STS sheets with storm securitization per-class billing rates (Leaves 613 and 607).",
    ),
    "progress_greenpower_program": ParserProfileImpactRule(
        parser_profile="progress_greenpower_program",
        family_keys=("nc-progress-leaf-642", "nc-progress-leaf-643"),
        companies=("progress",),
        candidate_reason_tokens=("leaf642_greenpower", "leaf643_renewable_ren", "per_block", "monthly_rate", "renewable_rider"),
        description="DEP NC GreenPower rider sheets with a per-block monthly program charge.",
    ),
    "progress_demand_response_automation": ParserProfileImpactRule(
        parser_profile="progress_demand_response_automation",
        family_keys=("nc-progress-leaf-717",),
        companies=("progress",),
        candidate_reason_tokens=("leaf717_dra", "availability_credit", "event_credit", "participant_incentive"),
        description="DEP Demand Response Automation Rider sheets with monthly availability, event, and participation incentive credits.",
    ),
    "progress_powerpair_pilot": ParserProfileImpactRule(
        parser_profile="progress_powerpair_pilot",
        family_keys=("nc-progress-leaf-770",),
        companies=("progress",),
        candidate_reason_tokens=("leaf770_powerpair", "incentive_terms", "pilot_terms", "rate_terms"),
        description="DEP PowerPair pilot tariff sheets with solar and battery incentive amounts.",
    ),
    "progress_load_control_winter": ParserProfileImpactRule(
        parser_profile="progress_load_control_winter",
        family_keys=("nc-progress-leaf-714",),
        companies=("progress",),
        candidate_reason_tokens=("leaf714_lc_win", "bill_credit", "load_control_winter"),
        description="DEP Residential Load Control (Asheville Area) Rider LC-WIN with fixed bill credit incentives.",
    ),
    "progress_income_qualified_load_control": ParserProfileImpactRule(
        parser_profile="progress_income_qualified_load_control",
        family_keys=("nc-progress-leaf-715", "nc-progress-leaf-725"),
        companies=("progress",),
        candidate_reason_tokens=("leaf715_riqlc", "leaf725_riqlc", "income_qualified_load_control", "initial_incentive"),
        description="DEP Income-Qualified EnergyWise Load Control (Leaf 715) and RIQLC (Leaf 725) incentive amounts.",
    ),
    "progress_billing_adjustments": ParserProfileImpactRule(
        parser_profile="progress_billing_adjustments",
        family_keys=("nc-progress-leaf-601",),
        companies=("progress",),
        candidate_reason_tokens=("family=leaf601", "billing_adjustment_factors", "net_adjustment", "schedule_applicability"),
        description="DEP Rider BA billing-adjustment tables and documents already parsed by the Progress billing-adjustments profile.",
    ),
    "progress_single_value_rider": ParserProfileImpactRule(
        parser_profile="progress_single_value_rider",
        family_keys=(
            "nc-progress-leaf-608",
            "nc-progress-leaf-609",
            "nc-progress-leaf-610",
        ),
        companies=("progress",),
        candidate_reason_tokens=("single_value_rider_family", "monthly_rate", "approved_rate_sentence"),
        description="DEP one-page single-value riders such as RDM, ESM, and PIM.",
    ),
    "progress_recovery_rider": ParserProfileImpactRule(
        parser_profile="progress_recovery_rider",
        family_keys=("nc-progress-rider-RECOVERYRIDER",),
        companies=("progress",),
        candidate_reason_tokens=("recovery_rider", "cost_recovery_rider", "monthly_rate", "applicability"),
        description="DEP Recovery Rider sheets that were previously collapsing into unknown routing.",
    ),
    "progress_rider_adjustment_matrix": ParserProfileImpactRule(
        parser_profile="progress_rider_adjustment_matrix",
        family_keys=("nc-progress-leaf-600",),
        companies=("progress",),
        candidate_reason_tokens=("family=leaf600", "summary_text"),
        required_signal_flags=("has_summary_text", "has_progress_company_text"),
        description="DEP rider summary matrix pages and documents already parsed by the Progress rider-summary profile.",
    ),
    "carolinas_rider_adjustment_matrix": ParserProfileImpactRule(
        parser_profile="carolinas_rider_adjustment_matrix",
        family_keys=(
            "nc-carolinas-rider-summary",
            "nc-carolinas-leaf-99",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("summary_text", "carolinas_company", "leaf99"),
        required_signal_flags=("has_summary_text", "has_carolinas_company_text"),
        description="DEC rider summary matrix pages and documents already parsed by the Carolinas rider-summary profile.",
    ),
    "carolinas_small_customer_generator": ParserProfileImpactRule(
        parser_profile="carolinas_small_customer_generator",
        family_keys=("nc-carolinas-rider-SCG",),
        companies=("carolinas",),
        candidate_reason_tokens=("rider_scg", "small_customer_generator", "supplemental_charge", "standby_charge"),
        description="DEC Rider SCG sheets with explicit supplemental and standby monthly charges.",
    ),
    "carolinas_net_metering_rider": ParserProfileImpactRule(
        parser_profile="carolinas_net_metering_rider",
        family_keys=("nc-carolinas-rider-NM",),
        companies=("carolinas",),
        candidate_reason_tokens=("rider_nm", "net_metering", "standby_charge", "minimum_bill", "non_bypassable_charge"),
        description="DEC Rider NM net-metering sheets with explicit standby-charge or minimum-bill terms.",
    ),
    "carolinas_energy_efficiency_rider": ParserProfileImpactRule(
        parser_profile="carolinas_energy_efficiency_rider",
        family_keys=("nc-carolinas-rider-EE",),
        companies=("carolinas",),
        candidate_reason_tokens=("rider_ee", "energy_efficiency_rider", "ee_adjustments", "explicit_rate_values"),
        description="DEC Rider EE sheets with explicit residential or nonresidential rider-adjustment values.",
    ),
    "carolinas_fuel_cost_adj_rider": ParserProfileImpactRule(
        parser_profile="carolinas_fuel_cost_adj_rider",
        family_keys=("nc-carolinas-rider-fcar",),
        companies=("carolinas",),
        candidate_reason_tokens=("fcar_language", "base_fuel_cost", "fcar_factor_line", "multi_class_structure"),
        description="DEC Fuel Cost Adjustment Rider (FCAR) — per-class ¢/kWh factors for Residential, General Service/Lighting, and Industrial.",
    ),
    "carolinas_flat_fee_rider": ParserProfileImpactRule(
        parser_profile="carolinas_flat_fee_rider",
        family_keys=(
            "nc-carolinas-rider-car",
            "nc-carolinas-rider-ed",
            "nc-carolinas-rider-pm",
            "nc-progress-leaf-644",
            "nc-progress-leaf-666",
            "nc-progress-leaf-718",
        ),
        companies=("carolinas", "progress"),
        candidate_reason_tokens=("per_month_fee", "per_month_per_block", "monthly_charge_language", "monthly_bill_credit"),
        description="Flat per-block or per-month riders for DEC (CAR, ED, PM) and DEP (COP leaf-644, GR leaf-666, CAP leaf-718).",
    ),
    "carolinas_residential_flat": ParserProfileImpactRule(
        parser_profile="carolinas_residential_flat",
        family_keys=(
            "nc-carolinas-schedule-rs",
            "nc-carolinas-leaf-11",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("carolinas_family", "rs_marker", "flat_rate_markers", "specific_rs_family"),
        required_signal_flags=("has_rs_marker", "has_flat_rate_markers", "has_carolinas_company_text"),
        description="DEC RS-style flat residential sheets and documents already parsed by the Carolinas flat-rate profile.",
    ),
    "carolinas_residential_tou": ParserProfileImpactRule(
        parser_profile="carolinas_residential_tou",
        family_keys=(
            "nc-carolinas-schedule-rt",
            "nc-carolinas-schedule-opt-e",
            "nc-carolinas-schedule-optv",
            "nc-carolinas-schedule-opt-v",
            "nc-carolinas-schedule-retc",
            "nc-carolinas-schedule-rstc",
            "nc-carolinas-schedule-sgstc",
            "nc-carolinas-doc-schedulertresidentialservicetimeofuse",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("carolinas_family", "schedule_rt", "residential_tou", "schedule_opt", "retc_schedule", "rstc_schedule", "sgstc_schedule"),
        description="DEC residential TOU schedules (RT, OPT-E, OPTV/OPT-V, RETC, RSTC, SGSTC) parsed by the Carolinas residential TOU profile.",
    ),
    "carolinas_current_leaf_bridge": ParserProfileImpactRule(
        parser_profile="carolinas_current_leaf_bridge",
        family_keys=("nc-carolinas-schedule-hlf",),
        companies=("carolinas",),
        candidate_reason_tokens=("current_carolinas_pdf", "hlf_schedule", "demand_terms", "customer_charge"),
        gating_signal_flags=("is_current_carolinas_pdf",),
        description="Current-style DEC schedule PDFs such as HLF that can be reparsed through the shared Carolinas current-leaf bridge.",
    ),
    "carolinas_general_service_schedule": ParserProfileImpactRule(
        parser_profile="carolinas_general_service_schedule",
        family_keys=(
            "nc-carolinas-schedule-pg",
            "nc-carolinas-schedule-lgs",
            "nc-carolinas-schedule-sgs",
            "nc-carolinas-doc-scheduleoptioptionalpowerservicetimeofuseindustr",
            "nc-carolinas-doc-scheduleiindustrialservice",
            "nc-carolinas-doc-schedulelgslargegeneralservice",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("carolinas_general_service", "pg_schedule", "lgs_schedule", "sgs_schedule", "opti_schedule", "industrial_schedule", "customer_charge", "energy_charge", "demand_terms"),
        description="Historical DEC PG/LGS and related general-service schedule sheets that should parse through the shared Carolinas leaf parser.",
    ),
    "carolinas_schedule_bridge": ParserProfileImpactRule(
        parser_profile="carolinas_schedule_bridge",
        family_keys=(
            "nc-carolinas-schedule-i",
            "nc-carolinas-doc-scheduleiindustrialservice",
            "nc-carolinas-doc-scheduleopte",
            "nc-carolinas-doc-scheduleoptg",
            "nc-carolinas-schedule-ts",
            "nc-carolinas-schedule-opt-e",
            "nc-carolinas-schedule-opt-g",
            "nc-carolinas-schedule-opt-h",
            "nc-carolinas-schedule-opt-i",
            "nc-carolinas-schedule-bc",
            "nc-carolinas-schedule-it",
            "nc-carolinas-schedule-nl",
            "nc-carolinas-schedule-hp",
            "nc-carolinas-doc-schedulewc",
            "nc-carolinas-doc-schedulewcresidentialwaterheatingservice",
            "nc-carolinas-schedule-ppbe",
            "nc-carolinas-schedule-hlf",
            "nc-carolinas-schedule-ret",
            "nc-carolinas-schedule-rst",
            "nc-carolinas-schedule-sgst",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("carolinas_schedule_bridge", "industrial_schedule", "opte_schedule", "optg_schedule", "opth_schedule", "opti_schedule", "bc_schedule", "it_schedule", "nl_schedule", "hp_schedule", "ts_schedule", "wc_schedule", "facilities_charge", "energy_charge", "demand_terms"),
        description="Historical DEC schedules such as I, OPT-E, OPT-G, OPT-H, OPT-I, BC, IT, NL, HP, TS, and WC that should parse through the shared Carolinas leaf bridge.",
    ),
    "carolinas_lighting_schedule": ParserProfileImpactRule(
        parser_profile="carolinas_lighting_schedule",
        family_keys=(
            "nc-carolinas-schedule-ol",
            "nc-carolinas-schedule-pl",
            "nc-carolinas-doc-floodlightingservice",
            "nc-carolinas-doc-scheduleplstreetandpubliclightingservice",
            "nc-carolinas-doc-scheduleflfloodlightingservice",
            "nc-carolinas-doc-scheduleylyardlightingservice",
            "nc-carolinas-doc-governmentallightingservice",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("lighting_schedule", "schedule_ol", "schedule_pl", "schedule_fl", "schedule_yl", "schedule_gl", "luminaire_rates", "per_unit_rates"),
        description="DEC lighting schedules with luminaire or per-unit monthly rate tables such as OL, PL, FL, YL, and GL.",
    ),
    "carolinas_solar_choice_rider": ParserProfileImpactRule(
        parser_profile="carolinas_solar_choice_rider",
        family_keys=(
            "nc-carolinas-rider-nmb",
            "nc-carolinas-rider-nsc",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("current_carolinas_pdf", "rider_nmb", "rider_nsc", "credit_terms", "fixed_charge_terms"),
        gating_signal_flags=("is_current_carolinas_pdf",),
        description="Current-style DEC solar/net-metering riders with explicit monthly credit, standby, or minimum-bill terms.",
    ),
    "generic_residential": ParserProfileImpactRule(
        parser_profile="generic_residential",
        description="Only documents whose latest processing run still relied on the generic fallback profile.",
    ),
    "carolinas_single_value_rider": ParserProfileImpactRule(
        parser_profile="carolinas_single_value_rider",
        family_keys=(
            "nc-carolinas-rider-edpr",
            "nc-carolinas-rider-bpmppttrueup",
            "nc-carolinas-rider-bpmprospectiverider",
            "nc-carolinas-rider-prospectiverider",
            "nc-carolinas-rider-ps",
            "nc-carolinas-rider-riderlc",
        ),
        companies=("carolinas",),
        candidate_reason_tokens=("carolinas_single_value", "kwh_rate"),
        description="Carolinas single-value riders (EDPR, BPM, PS, RIDERLC, PROSPECTIVERIDER) with kWh-based rate structures.",
    ),
}


def get_parser_profile_impact_rule(parser_profile: str) -> ParserProfileImpactRule:
    normalized = parser_profile.strip().lower()
    try:
        return _PROFILE_IMPACT_RULES[normalized]
    except KeyError as exc:
        valid = ", ".join(sorted(_PROFILE_IMPACT_RULES))
        raise ValueError(f"Unknown parser profile {parser_profile!r}. Valid values: {valid}") from exc


def list_parser_profile_impact_rules() -> list[ParserProfileImpactRule]:
    return [rule for _, rule in sorted(_PROFILE_IMPACT_RULES.items())]
