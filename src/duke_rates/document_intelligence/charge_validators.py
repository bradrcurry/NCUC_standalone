"""Numerical and structural validators for extracted charges.

Each validator returns :class:`Finding` objects. Levels:

* ``error``   -- the data is wrong as stored (an unreadable date, a total that
  a structured parse proves does not add up).
* ``review``  -- ambiguous; a person must decide. Nothing is chosen silently.
* ``warning`` -- suspicious but plausible.
* ``info``    -- context only.

Validators work on plain charge dicts (``charge_type``, ``charge_label``,
``rate_value``, ``rate_unit``, ``customer_class``, ``tou_period``, ``season``,
``tier_min``, ``tier_max``, ``effective_start``) so they apply equally to
``ExtractedCharge`` output, ``extracted_charges`` rows and ``tariff_charges``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from duke_rates.parse.canonical import Basis, canonicalize_charge, parse_unit

LEVELS = ("error", "review", "warning", "info")
_CHECKSUM_TOLERANCE = Decimal("0.002")


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    detail: str
    refs: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)


def as_charge_dict(charge: Any) -> dict:
    if isinstance(charge, dict):
        return charge
    keys = (
        "charge_type",
        "charge_label",
        "rate_value",
        "rate_unit",
        "customer_class",
        "tou_period",
        "season",
        "tier_min",
        "tier_max",
        "effective_start",
    )
    return {k: getattr(charge, k, None) for k in keys}


def _label_class(label: str | None) -> str:
    return (label or "").split(" - ")[0].replace("Total Rider Adjustments", "").strip().lower()


def _key(c: dict, *, with_class: bool = True) -> tuple:
    """Natural key. The canonical unit is part of it: a section's kWh and kW
    TOTAL rows share a label but are different charges. ``with_class=False``
    relies on the label alone (stored classes may have been backfilled later)."""
    unit = parse_unit(c.get("rate_unit")).canonical
    return (
        (c.get("customer_class") or _label_class(c.get("charge_label"))) if with_class else "",
        c.get("charge_type"),
        c.get("tou_period") or "",
        c.get("season") or "all_year",
        c.get("tier_min"),
        c.get("tier_max"),
        unit.value if unit else c.get("rate_unit"),
        re.sub(r"[^a-z0-9]+", " ", (c.get("charge_label") or "").lower()).strip(),
    )


# ---------------------------------------------------------------------------
# Rider-summary structure
# ---------------------------------------------------------------------------


def validate_total_reconciliation(summary) -> list[Finding]:
    """Printed TOTAL rows vs the sum of their components, recomputed in Decimal."""
    findings: list[Finding] = []
    for block in summary.rate_classes:
        components = [
            li
            for li in block.line_items
            if not li.is_total and not li.is_section_header and not li.indented
        ]
        for total, attr in (
            (block.total_cents_per_kwh, "cents_per_kwh"),
            (block.total_dollars_per_kw, "dollars_per_kw"),
            (block.total_incremental_cents_per_kwh, "incremental_cents_per_kwh"),
        ):
            if total is None:
                continue
            values = [
                Decimal(repr(getattr(li, attr)))
                for li in components
                if getattr(li, attr) is not None
            ]
            diff = abs(sum(values, Decimal(0)) - Decimal(repr(total)))
            if not values:
                findings.append(
                    Finding(
                        "review",
                        "total_without_components",
                        f"{block.rate_class}: printed TOTAL {total} has no {attr} components",
                        (block.rate_class,),
                    )
                )
            elif diff > _CHECKSUM_TOLERANCE:
                findings.append(
                    Finding(
                        "review",
                        "total_mismatch",
                        f"{block.rate_class}: components sum {sum(values)} vs printed "
                        f"TOTAL {total} ({attr})",
                        (block.rate_class,),
                    )
                )
        has_total = any(
            t is not None
            for t in (
                block.total_cents_per_kwh,
                block.total_dollars_per_kw,
                block.total_incremental_cents_per_kwh,
            )
        )
        if not has_total and any(
            li.cents_per_kwh is not None or li.dollars_per_kw is not None for li in block.line_items
        ):
            findings.append(
                Finding(
                    "warning",
                    "no_printed_total",
                    f"{block.rate_class}: values but no printed TOTAL to check them",
                    (block.rate_class,),
                )
            )
    return findings


def validate_column_association(summary) -> list[Finding]:
    """Values that could not be tied to a unit column, or were printed unexpectedly."""
    findings = []
    for block in summary.rate_classes:
        for item in block.line_items:
            if item.unit_ambiguous:
                findings.append(
                    Finding(
                        "review",
                        "value_column_ambiguous",
                        f"{block.rate_class}: {item.label!r} values not tied to a unit column",
                        (block.rate_class, item.label),
                    )
                )
            if (item.evidence or {}).get("disagreement"):
                d = item.evidence["disagreement"]
                findings.append(
                    Finding(
                        "review",
                        "parsers_disagree",
                        f"{block.rate_class}: {item.label!r} text {d['legacy']} vs table "
                        f"{d['structured']}",
                        (block.rate_class, item.label),
                    )
                )
        if block.ambiguous_rate_class:
            findings.append(
                Finding(
                    "review",
                    "section_without_rate_class",
                    f"section {block.rate_class!r} has no recognizable rate class",
                    (block.rate_class,),
                )
            )
    return findings


def validate_cross_class_contamination(summary) -> list[Finding]:
    """A rate-class title appearing as a line item means two sections were merged."""
    findings = []
    title_re = re.compile(
        r"\b(schedules?|service)\b.*\b(cents|/kwh|dollars)\b|^\s*(residential|general service"
        r"|lighting|industrial)\s+schedules?\b",
        re.I,
    )
    for block in summary.rate_classes:
        for item in block.line_items:
            if not item.is_section_header and title_re.search(item.label):
                findings.append(
                    Finding(
                        "review",
                        "cross_class_contamination",
                        f"{block.rate_class}: line {item.label!r} looks like another "
                        "class's header",
                        (block.rate_class, item.label),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Charge lists
# ---------------------------------------------------------------------------


def validate_units(charges: Iterable[Any]) -> list[Finding]:
    findings = []
    by_key: dict[tuple, set] = defaultdict(set)
    for raw in charges:
        c = as_charge_dict(raw)
        canonical = canonicalize_charge(
            c.get("charge_type"),
            c.get("rate_value"),
            c.get("rate_unit"),
            label=c.get("charge_label"),
            tou_period=c.get("tou_period"),
        )
        for warning in canonical.warnings:
            level = (
                "review"
                if warning.startswith(("unit_", "type_unit_conflict", "adjustment_basis"))
                else "warning"
            )
            findings.append(
                Finding(
                    level,
                    warning.split(":")[0],
                    f"{c.get('charge_label')!r}: {warning}",
                    (c.get("charge_label"),),
                )
            )
        if canonical.basis is not None:
            by_key[_key(c)].add(canonical.basis)
    for key, bases in by_key.items():
        if len(bases) > 1 and not ({Basis.ENERGY, Basis.DEMAND} == bases):
            findings.append(
                Finding(
                    "review",
                    "unit_basis_mixed",
                    f"{key[-1]!r}: one charge printed with bases {sorted(b.value for b in bases)}",
                    (key[-1],),
                )
            )
    return findings


def validate_rate_class_consistency(charges: Iterable[Any]) -> list[Finding]:
    """The class in a charge label must agree with its customer_class, when both exist."""
    from duke_rates.billing.tariff_engine import _class_matches
    from duke_rates.billing.tariff_engine import _label_class as engine_label_class

    findings = []
    for raw in charges:
        c = as_charge_dict(raw)
        stored, from_label = c.get("customer_class"), engine_label_class(c.get("charge_label"))
        if (
            stored
            and from_label
            and not _class_matches(from_label, stored)
            and not _class_matches(stored, from_label)
        ):
            findings.append(
                Finding(
                    "review",
                    "rate_class_inconsistent",
                    f"{c.get('charge_label')!r}: label says {from_label}, stored {stored}",
                    (c.get("charge_label"),),
                )
            )
    return findings


def validate_effective_dates(
    charges: Iterable[Any], *, document_effective: str | None = None
) -> list[Finding]:
    from duke_rates.parse.normalization import parse_effective_date

    findings = []
    doc_date = parse_effective_date(document_effective) if document_effective else None
    for raw in charges:
        c = as_charge_dict(raw)
        value = c.get("effective_start")
        if not value:
            continue
        try:
            parsed = parse_effective_date(value)
        except Exception:
            parsed = None
        if parsed is None:
            findings.append(
                Finding(
                    "error",
                    "unreadable_date",
                    f"{c.get('charge_label')!r}: {value!r}",
                    (c.get("charge_label"),),
                )
            )
        elif doc_date and parsed > doc_date:
            findings.append(
                Finding(
                    "review",
                    "line_date_after_document",
                    f"{c.get('charge_label')!r}: {parsed} is after the sheet's {doc_date}",
                    (c.get("charge_label"),),
                )
            )
    return findings


def validate_duplicates(charges: Iterable[Any]) -> list[Finding]:
    findings = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for raw in charges:
        c = as_charge_dict(raw)
        groups[_key(c)].append(c)
    for key, items in groups.items():
        if len(items) < 2:
            continue
        values = {repr(i.get("rate_value")) for i in items}
        if len(values) == 1:
            findings.append(
                Finding(
                    "warning",
                    "duplicate_charge",
                    f"{key[-1]!r} appears {len(items)} times with the same value",
                    (key[-1],),
                )
            )
        else:
            findings.append(
                Finding(
                    "review",
                    "conflicting_duplicates",
                    f"{key[-1]!r} appears {len(items)} times with values {sorted(values)}",
                    (key[-1],),
                )
            )
    return findings


_BASE_RATE_TYPES = {"energy_block", "tou_energy", "demand", "fixed", "minimum"}


def validate_family_contamination(
    charges: Iterable[Any], *, family_type: str | None
) -> list[Finding]:
    """A rider family carrying base-rate charges (e.g. leaf-607 v5723 with RES rates)."""
    if family_type != "rider":
        return []
    base = [
        as_charge_dict(c)
        for c in charges
        if as_charge_dict(c).get("charge_type") in _BASE_RATE_TYPES
    ]
    if len(base) >= 3:
        return [
            Finding(
                "review",
                "cross_family_contamination",
                f"rider version holds {len(base)} base-rate charges "
                f"({sorted({c['charge_type'] for c in base})}); likely another schedule's text",
                (),
            )
        ]
    return []


def validate_revision_changes(
    current: Iterable[Any], previous: Iterable[Any], *, max_ratio: Decimal = Decimal(5)
) -> list[Finding]:
    """Unexpected differences against the previous tariff revision."""
    findings = []
    prev = {_key(as_charge_dict(c), with_class=False): as_charge_dict(c) for c in previous}
    cur = {_key(as_charge_dict(c), with_class=False): as_charge_dict(c) for c in current}
    for key, c in cur.items():
        p = prev.get(key)
        if p is None or c.get("rate_value") is None or p.get("rate_value") is None:
            continue
        now, before = Decimal(repr(float(c["rate_value"]))), Decimal(repr(float(p["rate_value"])))
        if parse_unit(c.get("rate_unit")).canonical != parse_unit(p.get("rate_unit")).canonical:
            findings.append(
                Finding(
                    "review",
                    "revision_unit_changed",
                    f"{key[-1]!r}: {p.get('rate_unit')} -> {c.get('rate_unit')}",
                    (key[-1],),
                )
            )
            continue
        if (
            before != 0
            and now != 0
            and (abs(now / before) > max_ratio or abs(before / now) > max_ratio)
        ):
            findings.append(
                Finding(
                    "review", "revision_value_jump", f"{key[-1]!r}: {before} -> {now}", (key[-1],)
                )
            )
    missing = [k[-1] for k in prev if k not in cur]
    if missing:
        findings.append(
            Finding(
                "info",
                "revision_rows_dropped",
                f"{len(missing)} rows of the previous revision are absent",
                tuple(missing[:10]),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Conflicting evidence (Phase 2C): structured vs text parses, pages, redlines
# ---------------------------------------------------------------------------

_DECIMAL_TOKEN_RE = re.compile(r"(?<![\d/])\d*\.(\d+)(?![\d/])")


def _evidence(charge: Any) -> dict:
    ev = charge.get("evidence") if isinstance(charge, dict) else getattr(charge, "evidence", None)
    return ev if isinstance(ev, dict) else {}


def _printed(ev: dict) -> str | None:
    values = ev.get("values") or ev.get("cells") or []
    return ev.get("printed_value_text") or (values[0].get("text") if values else None)


def _page(ev: dict) -> int | None:
    values = ev.get("values") or ev.get("cells") or []
    page = ev.get("page") or (values[0].get("page") if values else None)
    return int(page) if page is not None else None


def validate_parser_disagreements(charges: Iterable[Any]) -> list[Finding]:
    """The structured and text parses read different values for one row.

    ``merge_with_legacy`` keeps the text value and records both readings; the
    choice is not made here either, so each one needs a person.
    """
    findings = []
    for raw in charges:
        ev = _evidence(raw)
        if "disagreement" in ev:
            c = as_charge_dict(raw)
            findings.append(
                Finding(
                    "review",
                    "parser_disagreement",
                    f"{c.get('charge_label')!r}: text {ev['disagreement'].get('legacy')} vs "
                    f"table {ev['disagreement'].get('structured')}; text value kept",
                    (c.get("charge_label"),),
                )
            )
    return findings


def validate_parse_warnings(parse_warnings: Iterable[str]) -> list[Finding]:
    """Profile warnings that say a person must decide (rows not added, unreconciled sections)."""
    return [
        Finding("review", "parser_conflict", w, ())
        for w in parse_warnings or []
        if "needs review" in w
    ]


def validate_printed_precision(charges: Iterable[Any]) -> list[Finding]:
    """A value printed with more decimals than every other value in its unit.

    Redline copies fuse struck and inserted digits ("0.002" -> "0.0002"). The
    structured parser checks this per column, which needs 3+ values in the
    table; this repeats the check across the whole document per printed unit.
    """
    by_unit: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for raw in charges:
        ev = _evidence(raw)
        printed = _printed(ev)
        match = _DECIMAL_TOKEN_RE.search(printed or "")
        if not match:
            continue
        values = ev.get("values") or ev.get("cells") or []
        unit = (values[0].get("printed_unit") if values else None) or ev.get("printed_unit") or ""
        by_unit[unit.lower()].append((len(match.group(1)), raw))
    findings = []
    for unit, rows in by_unit.items():
        if len(rows) < 5:
            continue
        counts: dict[int, int] = defaultdict(int)
        for places, _ in rows:
            counts[places] += 1
        common, n = max(counts.items(), key=lambda kv: kv[1])
        if n * 4 < len(rows) * 3:  # no clearly dominant precision
            continue
        for places, raw in rows:
            if places > common:
                c = as_charge_dict(raw)
                findings.append(
                    Finding(
                        "review",
                        "printed_precision_outlier",
                        f"{c.get('charge_label')!r} printed {_printed(_evidence(raw))!r}: "
                        f"{places} decimals where {n}/{len(rows)} {unit or 'unitless'} values "
                        f"print {common} (possible fused redline digits)",
                        (c.get("charge_label"),),
                    )
                )
    return findings


def validate_redline_superseded(charges: Iterable[Any], *, is_redline: bool) -> list[Finding]:
    """On a redline copy, a quote printing more numbers than the row has value cells.

    That is how a struck (superseded) value next to its replacement reads in
    extracted text; which one the parser took cannot be told from the text.
    """
    if not is_redline:
        return []
    findings = []
    for raw in charges:
        ev = _evidence(raw)
        quote = ev.get("quote") or ev.get("text")
        if not quote:
            continue
        cells = len(ev.get("values") or ev.get("cells") or []) or 1
        numbers = _DECIMAL_TOKEN_RE.findall(quote)
        if len(numbers) > cells:
            c = as_charge_dict(raw)
            findings.append(
                Finding(
                    "review",
                    "redline_superseded_value",
                    f"{c.get('charge_label')!r}: quote {quote[:120]!r} prints {len(numbers)} "
                    f"numbers for {cells} value cell(s); struck value may have been read",
                    (c.get("charge_label"),),
                )
            )
    return findings


def validate_source_pages(
    charges: Iterable[Any], *, page_span: tuple[int, int] | None = None
) -> list[Finding]:
    """Evidence outside the document's page span, or one charge read from two pages."""
    findings = []
    by_key: dict[tuple, list[tuple[int, Any]]] = defaultdict(list)
    for raw in charges:
        page = _page(_evidence(raw))
        if page is None:
            continue
        c = as_charge_dict(raw)
        if page_span and not (page_span[0] <= page <= page_span[1]):
            findings.append(
                Finding(
                    "review",
                    "page_outside_span",
                    f"{c.get('charge_label')!r} evidence on page {page}, document spans "
                    f"{page_span[0]}-{page_span[1]}",
                    (c.get("charge_label"),),
                )
            )
        by_key[_key(c)].append((page, c.get("rate_value")))
    for key, hits in by_key.items():
        pages = {p for p, _ in hits}
        values = {repr(v) for _, v in hits}
        if len(pages) > 1 and len(values) > 1:
            findings.append(
                Finding(
                    "review",
                    "source_page_disagreement",
                    f"{key[-1]!r} read from pages {sorted(pages)} with values {sorted(values)}",
                    (key[-1],),
                )
            )
    return findings


def run_charge_validators(
    charges: list[Any],
    *,
    family_type: str | None = None,
    document_effective: str | None = None,
    previous: list[Any] | None = None,
    summary=None,
    parse_warnings: list[str] | None = None,
    page_span: tuple[int, int] | None = None,
    is_redline: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    findings += validate_units(charges)
    findings += validate_rate_class_consistency(charges)
    findings += validate_effective_dates(charges, document_effective=document_effective)
    findings += validate_duplicates(charges)
    findings += validate_family_contamination(charges, family_type=family_type)
    findings += validate_parser_disagreements(charges)
    findings += validate_parse_warnings(parse_warnings or [])
    findings += validate_printed_precision(charges)
    findings += validate_redline_superseded(charges, is_redline=is_redline)
    findings += validate_source_pages(charges, page_span=page_span)
    if previous:
        findings += validate_revision_changes(charges, previous)
    if summary is not None:
        findings += validate_total_reconciliation(summary)
        findings += validate_column_association(summary)
        findings += validate_cross_class_contamination(summary)
    return findings
