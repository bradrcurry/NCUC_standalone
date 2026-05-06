from __future__ import annotations

from duke_rates.models.jurisdiction import JurisdictionQuery, JurisdictionSeed

_SEEDS = [
    JurisdictionSeed(
        key="nc-carolinas-res",
        state="NC",
        company="carolinas",
        jurisdiction_code="NC01",
        service_key="01",
        market="residential",
        label="North Carolina residential rates - Duke Energy Carolinas",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC",
        ],
        api_content_path="home/billing/rates/rate-options",
        notes="North Carolina Duke Energy Carolinas public rate pages.",
    ),
    JurisdictionSeed(
        key="nc-progress-res",
        state="NC",
        company="progress",
        jurisdiction_code="NC02",
        service_key="02",
        market="residential",
        label="North Carolina residential rates - Duke Energy Progress",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="nc-carolinas-bus",
        state="NC",
        company="carolinas",
        jurisdiction_code="NC01",
        service_key="01",
        market="business",
        label="North Carolina business rates - Duke Energy Carolinas",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=NC",
            "https://www.duke-energy.com/business/billing/rates/index-of-rate-schedules?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="nc-progress-bus",
        state="NC",
        company="progress",
        jurisdiction_code="NC02",
        service_key="02",
        market="business",
        label="North Carolina business rates - Duke Energy Progress",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=NC",
            "https://www.duke-energy.com/business/billing/rates/index-of-rate-schedules?jur=NC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="sc-carolinas-res",
        state="SC",
        company="carolinas",
        jurisdiction_code="SC01",
        service_key="01",
        market="residential",
        label="South Carolina residential rates - Duke Energy Carolinas",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=SC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="sc-progress-res",
        state="SC",
        company="progress",
        jurisdiction_code="SC02",
        service_key="02",
        market="residential",
        label="South Carolina residential rates - Duke Energy Progress",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=SC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="sc-carolinas-bus",
        state="SC",
        company="carolinas",
        jurisdiction_code="SC01",
        service_key="01",
        market="business",
        label="South Carolina business rates - Duke Energy Carolinas",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=SC",
            "https://www.duke-energy.com/business/billing/rates/index-of-rate-schedules?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=SC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="sc-progress-bus",
        state="SC",
        company="progress",
        jurisdiction_code="SC02",
        service_key="02",
        market="business",
        label="South Carolina business rates - Duke Energy Progress",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=SC",
            "https://www.duke-energy.com/business/billing/rates/index-of-rate-schedules?jur=SC",
            "https://www.duke-energy.com/home/billing/rates/public-notices?jur=SC",
        ],
        api_content_path="home/billing/rates/rate-options",
    ),
    JurisdictionSeed(
        key="fl-res",
        state="FL",
        company="florida",
        jurisdiction_code="FL01",
        service_key="01",
        market="residential",
        label="Florida residential rates",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=FL",
            "https://www.duke-energy.com/home/billing/rates/index-of-rate-schedules?jur=FL",
        ],
        api_content_path="home/billing/rates/index-of-rate-schedules",
    ),
    JurisdictionSeed(
        key="fl-bus",
        state="FL",
        company="florida",
        jurisdiction_code="FL01",
        service_key="01",
        market="business",
        label="Florida business rates",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=FL",
            "https://www.duke-energy.com/business/billing/rates/index-of-rate-schedules?jur=FL",
        ],
        api_content_path="home/billing/rates/index-of-rate-schedules",
    ),
    JurisdictionSeed(
        key="in-res",
        state="IN",
        company="indiana",
        jurisdiction_code="IN01",
        service_key="01",
        market="residential",
        label="Indiana residential rates",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=IN",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=IN",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
    JurisdictionSeed(
        key="in-bus",
        state="IN",
        company="indiana",
        jurisdiction_code="IN01",
        service_key="01",
        market="business",
        label="Indiana business rates",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=IN",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=IN",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
    JurisdictionSeed(
        key="ky-res",
        state="KY",
        company="kentucky",
        jurisdiction_code="KY01",
        service_key="01",
        market="residential",
        label="Kentucky residential rates",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=KY",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=KY",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
    JurisdictionSeed(
        key="ky-bus",
        state="KY",
        company="kentucky",
        jurisdiction_code="KY01",
        service_key="01",
        market="business",
        label="Kentucky business rates",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=KY",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=KY",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
    JurisdictionSeed(
        key="oh-res",
        state="OH",
        company="ohio",
        jurisdiction_code="OH01",
        service_key="01",
        market="residential",
        label="Ohio residential rates",
        seed_urls=[
            "https://www.duke-energy.com/home/billing/rates?jur=OH",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=OH",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
    JurisdictionSeed(
        key="oh-bus",
        state="OH",
        company="ohio",
        jurisdiction_code="OH01",
        service_key="01",
        market="business",
        label="Ohio business rates",
        seed_urls=[
            "https://www.duke-energy.com/business/billing/rates?jur=OH",
            "https://www.duke-energy.com/home/billing/rates/electric-tariff?jur=OH",
        ],
        api_content_path="home/billing/rates/electric-tariff",
    ),
]

_COMPANY_STATE_HINTS = {
    "carolinas": {"NC", "SC"},
    "progress": {"NC", "SC"},
    "florida": {"FL"},
    "indiana": {"IN"},
    "kentucky": {"KY"},
    "ohio": {"OH"},
}


def get_all_jurisdictions() -> list[JurisdictionSeed]:
    return list(_SEEDS)


def select_jurisdictions(query: JurisdictionQuery) -> list[JurisdictionSeed]:
    if query.crawl_all:
        return get_all_jurisdictions()

    selected = _SEEDS
    if query.state:
        selected = [seed for seed in selected if seed.state.lower() == query.state.lower()]
    if query.company:
        hinted_states = _COMPANY_STATE_HINTS.get(query.company.lower())
        selected = [
            seed
            for seed in selected
            if (
                (seed.company and query.company.lower() in seed.company.lower())
                or query.company.lower() in seed.label.lower()
                or (hinted_states and seed.state in hinted_states)
            )
        ]
    return selected
