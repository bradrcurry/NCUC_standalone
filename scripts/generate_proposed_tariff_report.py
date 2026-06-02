"""Generate a consolidated evaluation report for proposed NCUC tariff filings.

Reads the read-only ``proposed_tariff_*`` tables and writes a markdown summary
covering, per docket: the proposed schedule inventory, the proposed rider
inventory, and the per-rate-year Summary of Rider Adjustments stacks (each
rider's cents/kWh increment with its effective date, plus the group total).

Usage:
    python scripts/generate_proposed_tariff_report.py \
        --db data/db/duke_rates.db \
        --out reports/proposed_tariff_evaluation.md
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from datetime import date


def _conn(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _schedules(c: sqlite3.Connection, doc_id: int) -> list[str]:
    rows = c.execute(
        "SELECT DISTINCT schedule_code FROM proposed_tariff_blocks "
        "WHERE proposed_document_id=? AND tariff_kind='schedule' "
        "AND schedule_code IS NOT NULL ORDER BY schedule_code",
        (doc_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _riders(c: sqlite3.Connection, doc_id: int) -> list[str]:
    rows = c.execute(
        "SELECT DISTINCT tariff_name FROM proposed_tariff_blocks "
        "WHERE proposed_document_id=? AND tariff_kind='rider' ORDER BY tariff_name",
        (doc_id,),
    ).fetchall()
    return [r[0] for r in rows]


_GROUP_RE = re.compile(r"\[(?P<group>.+?)\]\s*$")


def _summary_stacks(c: sqlite3.Connection, doc_id: int) -> dict[tuple[str, str], list[sqlite3.Row]]:
    """Return charges keyed by (exhibit_key + effective_start, schedule_group)."""
    rows = c.execute(
        """
        SELECT b.exhibit_key, b.start_page, b.effective_start,
               cc.charge_label, cc.rate_value, cc.raw_line
        FROM proposed_tariff_blocks b
        JOIN proposed_tariff_charge_candidates cc ON cc.proposed_block_id = b.id
        WHERE b.proposed_document_id=? AND b.tariff_kind='rider_summary'
        ORDER BY b.start_page, cc.id
        """,
        (doc_id,),
    ).fetchall()
    stacks: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        m = _GROUP_RE.search(r["charge_label"])
        group = m.group("group") if m else "(ungrouped)"
        page_key = f"{r['exhibit_key']} p{r['start_page']}" + (
            f" (eff {r['effective_start']})" if r["effective_start"] else ""
        )
        stacks[(page_key, group)].append(r)
    return stacks


def _eff_from_raw(raw: str) -> str:
    m = re.search(r"eff\s+([0-9/]+)", raw or "")
    return m.group(1) if m else ""


def _rider_name(label: str) -> str:
    return _GROUP_RE.sub("", label).strip()


def generate(db: str, out: str) -> None:
    c = _conn(db)
    docs = c.execute(
        "SELECT id, docket_number, utility, source_pdf FROM proposed_tariff_documents "
        "ORDER BY docket_number"
    ).fetchall()

    lines: list[str] = []
    lines.append("# Proposed Tariff Evaluation — Projected Rate Years")
    lines.append("")
    lines.append(f"_Generated {date.today().isoformat()} from `{db}` (read-only proposed lane)._")
    lines.append("")

    for d in docs:
        doc_id = d["id"]
        scheds = _schedules(c, doc_id)
        riders = _riders(c, doc_id)
        stacks = _summary_stacks(c, doc_id)

        lines.append(f"## {d['docket_number']} — {d['utility']}")
        lines.append("")
        lines.append(f"Source: `{d['source_pdf']}`")
        lines.append("")
        lines.append(f"### Rate schedules ({len(scheds)})")
        lines.append("")
        lines.append(", ".join(scheds) if scheds else "_none_")
        lines.append("")
        lines.append(f"### Riders ({len(riders)})")
        lines.append("")
        for r in riders:
            lines.append(f"- {r}")
        lines.append("")
        lines.append("### Rider adjustment stacks (Summary of Rider Adjustments)")
        lines.append("")
        if not stacks:
            lines.append("_No summary stacks captured._")
            lines.append("")
        if stacks:
            lines.append(
                "> Values are transcribed verbatim from each filing's summary "
                "sheet. Read the authoritative class total from the filing's "
                "own `TOTAL cents/kWh` line — do **not** sum the rows here: DEP "
                "lists a `Rider BA - Net Adjustment` subtotal that already rolls "
                "up the Fuel/DSM/EE sub-lines printed above it."
            )
            lines.append("")
        for (page_key, group), charges in stacks.items():
            lines.append(f"#### {page_key} — {group}")
            lines.append("")
            lines.append("| Rider | ¢/kWh | $/kWh | Effective |")
            lines.append("|---|--:|--:|---|")
            for ch in charges:
                cents = ch["rate_value"] * 100.0
                lines.append(
                    f"| {_rider_name(ch['charge_label'])} | {cents:.4f} | "
                    f"{ch['rate_value']:.8f} | {_eff_from_raw(ch['raw_line'])} |"
                )
            lines.append("")

    c.close()
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out} ({len(lines)} lines)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/db/duke_rates.db")
    ap.add_argument("--out", default="reports/proposed_tariff_evaluation.md")
    args = ap.parse_args()
    generate(args.db, args.out)


if __name__ == "__main__":
    main()
