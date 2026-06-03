# Phase 3 — Forecast-accuracy backtest (plan)

**Goal:** answer "how accurate have Duke's load forecasts historically been?" by
grading past forecast *vintages* against realized actuals, by horizon (1/3/5-yr
ahead), per utility and class.

This builds on Phase 1 (shipped): `proposed_forecast.load_proposed_load_forecast`
harvests the current **Spring 2025** vintage (Customer Growth, Retail Sales by
class, Gross-to-Net drivers, Peak) from the two proposed filings.

## Phase 3a — SHIPPED: true-up riders as a forecast-error track record

Duke's own PBR/MYRP true-up riders *are* actual-vs-forecast reconciliations, so
their historical values give a forecast-error series with **no external data**:

- **Fuel** (DEC `FCA`, DEP `BA-EMF`) — actual fuel cost vs the projection in base
  rates. DEC peaked **+2.30 ¢/kWh (2024-01)**, DEP **+1.19** — a large fuel
  under-forecast in 2023–24 later recovered from customers.
- **Decoupling** (`RDM`) — actual residential revenue-per-customer vs the case
  target. DEP **+0.232** = residential sales came in below forecast.
- **Earnings** (`ESM`) — actual vs authorized ROE. **0** = no over-earning.

Implemented in `analytics/forecast_trueup.py`
(`load_forecast_trueup_series`, `summarize_trueup`) over the accepted canonical
rider-component timelines, surfaced as the dashboard "Track record" panel.
This is the most faithful answer to "what did Duke later verify," and it is the
recommended primary lens. The classic external backtest below remains a
complementary cross-check.

## What we already have

- **Current-vintage forecast** (2025 → 2040) for DEC and DEP, reconciled
  (gross+drivers = net; class components = total). Too new to grade on its own.
- **True-up rider history** (Phase 3a) — fuel/decoupling/earnings reconciliations
  back to ~2016 (fuel) / 2023 (decoupling).
- **Actuals:** `eia_retail_sales` — NC (and other states) annual sales
  (`sales_million_kwh`), `customers`, `revenue`, `price` by sector (RES/COM/IND/
  ALL) back to 2001. This is the actuals spine.

## What's missing (the Phase-3 work)

1. **Prior forecast vintages.** Need the same Customer Growth / Retail Sales
   tables from earlier filings so there are forecasts old enough to have a
   realized target year:
   - 2022 rate cases: **E-7 Sub 1276** (DEC), **E-2 Sub 1300** (DEP).
   - Duke biennial **IRPs** (richest source of load forecasts; multiple vintages).
   - Annual MYRP true-up filings (RY1/RY2) which restate near-term forecasts.
   Harvest reuses `proposed_forecast.extract_forecast_from_pdf`; expect the same
   font-subset garble handled by `_degarble`, plus possibly image-only pages
   needing the Docling/Tesseract OCR path.
   - **Schema add:** persist harvested points to a `load_forecast_history`
     table keyed by `(utility, forecast_vintage, table_type, year, segment)` so
     vintages accumulate instead of being re-parsed on the fly.

2. **Actuals granularity.** Forecasts are **utility-territory** (DEC vs DEP);
   `eia_retail_sales` is currently **state-level** (NC). Two options:
   - Ingest **EIA-861** *utility-level* "Sales to Ultimate Customers" for DEC and
     DEP (preferred — exact match). Extend `eia_analytics` ingestion.
   - Interim: compare **combined DEC+DEP** forecast vs **NC state** actual (rough;
     contaminated by Dominion NC + co-ops/munis — document the caveat).

## Method

For each `(utility, vintage v, target_year t, class c)`:

```
error_pct(v, t, c) = forecast(v, t, c) / actual(t, c) − 1
horizon            = t − vintage_year(v)
```

Aggregate by horizon: mean/median error and bias (signed) per 1/3/5-yr ahead.
Headline view: forecast-vs-actual fan chart (each vintage as a ray) overlaid on
the realized line; a horizon-vs-error scatter/bar showing systematic over- or
under-forecast.

## Hypotheses to test

- Utilities historically **over-forecast** load (post-2010 flattening). Does
  Duke's record show that bias?
- Is the **current data-center / Economic-Development surge** (the wedge driving
  this ask) consistent with prior forecasts, or a regime change from the
  historically flat commercial/residential trend?

## Deliverables

- `load_forecast_history` table + harvest CLI/loader for prior vintages.
- EIA-861 utility-level actuals ingestion (or documented state-level proxy).
- `forecast_accuracy.py` analytics (error by horizon) + a dashboard
  "Track record" panel under Section 3.

## Dependencies / order

1. Locate + harvest prior-vintage forecasts (PDF discovery + extractor reuse).
2. Add utility-level EIA actuals.
3. Build the accuracy analytics + dashboard panel.
