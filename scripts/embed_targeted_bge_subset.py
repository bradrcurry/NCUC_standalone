"""One-off: embed a targeted subset of sections with bge-m3 for a fast
directional comparison vs the full qwen3 reference pool.

Subset = (964 gold sections) UNION (sections with eval-relevant codes or
leaves). Roughly 2,700 sections.

Note the comparison this enables is biased: bge-m3 has a smaller, cleaner
reference pool than qwen3's full 14k. If bge-m3 cannot move the missed
cases even with this favorable bias, that's evidence the model swap
won't help; if it does move them, a follow-up full embed is warranted.
"""

from __future__ import annotations

import sqlite3
import struct
import time

from duke_rates.config import get_settings
from duke_rates.document_intelligence.ollama_orchestrator import (
    OllamaOrchestrator,
)
from duke_rates.document_intelligence.section_text_extractor import (
    fetch_section_text,
)


SUBSET_SQL = """
    WITH gold_sections AS (
        SELECT ds.id FROM document_sections ds
        JOIN section_type_gold g
          ON g.source_pdf = ds.source_pdf
         AND g.section_index = ds.section_index
        WHERE g.superseded_by IS NULL
    ),
    eval_relevant AS (
        SELECT id FROM document_sections
        WHERE schedule_codes_json LIKE '%"RES%'
           OR schedule_codes_json LIKE '%"EB%'
           OR schedule_codes_json LIKE '%"BA%'
           OR schedule_codes_json LIKE '%"LGS%'
           OR schedule_codes_json LIKE '%"R-TOU%'
           OR schedule_codes_json LIKE '%"STS%'
           OR schedule_codes_json LIKE '%FCAR%'
           OR leaf_numbers_json LIKE '%"607%'
           OR leaf_numbers_json LIKE '%"503%'
           OR source_pdf LIKE '%sub-1305%'
    )
    SELECT ds.id, ds.source_pdf, ds.section_index, ds.start_page, ds.end_page
    FROM document_sections ds
    WHERE ds.id IN (SELECT id FROM gold_sections)
       OR ds.id IN (SELECT id FROM eval_relevant)
    ORDER BY ds.id
"""


def main() -> None:
    settings = get_settings()
    orch = OllamaOrchestrator()
    role = "embedding_secondary"
    model = orch._roles[role].primary
    kind = "section_text"
    print(f"Using role={role} model={model} kind={kind}")

    conn = sqlite3.connect(str(settings.database_path))
    conn.row_factory = sqlite3.Row
    targets = conn.execute(SUBSET_SQL).fetchall()
    print(f"Targeted subset: {len(targets)} sections")

    already = {
        row["source_pdf"] + "::" + str(row["section_index"])
        for row in conn.execute(
            "SELECT source_pdf, section_index FROM section_embeddings "
            "WHERE embedding_kind=? AND embedding_model=?",
            (kind, model),
        ).fetchall()
    }
    print(f"Already embedded: {len(already)}")
    conn.close()

    todo = [
        r for r in targets
        if (r["source_pdf"] + "::" + str(r["section_index"])) not in already
    ]
    print(f"To embed: {len(todo)}")

    t0 = time.perf_counter()
    ok = skip = fail = 0
    for i, sec in enumerate(todo):
        rconn = sqlite3.connect(str(settings.database_path))
        try:
            txt = fetch_section_text(
                rconn,
                sec["source_pdf"],
                int(sec["start_page"]),
                int(sec["end_page"]),
                max_chars=2000,
            ).text
        finally:
            rconn.close()

        if not txt.strip():
            skip += 1
            continue

        try:
            vec = orch.embed(role, txt)
        except Exception as exc:
            print(f"  embed failed for {sec['source_pdf']}#{sec['section_index']}: {exc}")
            fail += 1
            continue

        blob = struct.pack(f"{len(vec)}f", *vec)
        wconn = sqlite3.connect(str(settings.database_path))
        try:
            wconn.execute(
                """
                INSERT OR REPLACE INTO section_embeddings
                  (source_pdf, section_index, start_page, end_page,
                   embedding_kind, embedding_model, embedding_version,
                   vector, text_sample)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    sec["source_pdf"],
                    int(sec["section_index"]),
                    int(sec["start_page"]),
                    int(sec["end_page"]),
                    kind,
                    model,
                    "v1",
                    blob,
                    txt[:200],
                ),
            )
            wconn.commit()
        finally:
            wconn.close()
        ok += 1

        if (i + 1) % 50 == 0:
            elapsed = time.perf_counter() - t0
            rate = (i + 1) / elapsed
            remaining = (len(todo) - i - 1) / rate if rate > 0 else 0
            print(
                f"  {i + 1}/{len(todo)} ok={ok} skip={skip} fail={fail} "
                f"rate={rate:.2f}/s eta={remaining / 60:.1f}min"
            )

    print(f"\nDone: ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()
