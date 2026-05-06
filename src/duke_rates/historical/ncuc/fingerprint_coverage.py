from __future__ import annotations

from typing import Any

from duke_rates.db.repository import Repository


def _valid_text_sql(expr: str) -> str:
    return (
        f"({expr} IS NOT NULL AND TRIM(CAST({expr} AS TEXT)) <> '' "
        f"AND LOWER(TRIM(CAST({expr} AS TEXT))) NOT IN ('none', 'null'))"
    )


def build_fingerprint_coverage_report(
    repo: Repository,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    valid_hd_hash = _valid_text_sql("hd.content_hash")
    valid_hd_local_path = _valid_text_sql("hd.local_path")
    valid_dr_hash = _valid_text_sql("dr.content_hash")

    with repo._connect() as conn:
        summary = {
            "historical_nc_total_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM historical_documents WHERE state = 'NC'"
                ).fetchone()[0]
            ),
            "historical_nc_hash_backed_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_hash}
                    """
                ).fetchone()[0]
            ),
            "historical_nc_path_only_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_local_path}
                      AND NOT {valid_hd_hash}
                    """
                ).fetchone()[0]
            ),
            "historical_nc_with_fingerprint_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND EXISTS (
                        SELECT 1 FROM document_fingerprints df
                        WHERE df.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_without_fingerprint_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND NOT EXISTS (
                        SELECT 1 FROM document_fingerprints df
                        WHERE df.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_hash_backed_with_fingerprint_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_hash}
                      AND EXISTS (
                        SELECT 1 FROM document_fingerprints df
                        WHERE df.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_with_page_artifacts_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND EXISTS (
                        SELECT 1 FROM ncuc_page_artifacts pa
                        WHERE pa.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_with_span_artifacts_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND EXISTS (
                        SELECT 1 FROM ncuc_span_artifacts sa
                        WHERE sa.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_with_docling_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND EXISTS (
                        SELECT 1 FROM docling_artifacts da
                        WHERE da.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "historical_nc_with_ocr_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT hd.id)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND EXISTS (
                        SELECT 1 FROM ocr_artifacts oa
                        WHERE oa.source_pdf = hd.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_total_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM ncuc_discovery_records
                    WHERE fetch_status IN ('success', 'downloaded')
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_with_hash_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM ncuc_discovery_records dr
                    WHERE dr.fetch_status IN ('success', 'downloaded')
                      AND {valid_dr_hash}
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_with_page_artifacts_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT dr.id)
                    FROM ncuc_discovery_records dr
                    WHERE dr.fetch_status IN ('success', 'downloaded')
                      AND EXISTS (
                        SELECT 1 FROM ncuc_page_artifacts pa
                        WHERE pa.source_pdf = dr.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_with_span_artifacts_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT dr.id)
                    FROM ncuc_discovery_records dr
                    WHERE dr.fetch_status IN ('success', 'downloaded')
                      AND EXISTS (
                        SELECT 1 FROM ncuc_span_artifacts sa
                        WHERE sa.source_pdf = dr.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_with_docling_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT dr.id)
                    FROM ncuc_discovery_records dr
                    WHERE dr.fetch_status IN ('success', 'downloaded')
                      AND EXISTS (
                        SELECT 1 FROM docling_artifacts da
                        WHERE da.source_pdf = dr.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_with_ocr_count": int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT dr.id)
                    FROM ncuc_discovery_records dr
                    WHERE dr.fetch_status IN ('success', 'downloaded')
                      AND EXISTS (
                        SELECT 1 FROM ocr_artifacts oa
                        WHERE oa.source_pdf = dr.local_path
                      )
                    """
                ).fetchone()[0]
            ),
            "document_fingerprint_row_count": int(
                conn.execute("SELECT COUNT(*) FROM document_fingerprints").fetchone()[0]
            ),
            "fingerprint_rows_with_family_key_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_fingerprints
                    WHERE json_extract(metadata_json, '$.family_key') IS NOT NULL
                      AND TRIM(CAST(json_extract(metadata_json, '$.family_key') AS TEXT)) <> ''
                    """
                ).fetchone()[0]
            ),
            "fingerprint_rows_with_parser_profile_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_fingerprints
                    WHERE json_extract(metadata_json, '$.parser_profile') IS NOT NULL
                      AND TRIM(CAST(json_extract(metadata_json, '$.parser_profile') AS TEXT)) <> ''
                    """
                ).fetchone()[0]
            ),
            "fingerprint_rows_with_outcome_quality_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_fingerprints
                    WHERE json_extract(metadata_json, '$.outcome_quality') IS NOT NULL
                      AND TRIM(CAST(json_extract(metadata_json, '$.outcome_quality') AS TEXT)) <> ''
                    """
                ).fetchone()[0]
            ),
        }

        by_company = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    hd.company,
                    COUNT(*) AS historical_document_count,
                    SUM(CASE WHEN {valid_hd_hash} THEN 1 ELSE 0 END) AS hash_backed_count,
                    SUM(
                        CASE WHEN EXISTS (
                            SELECT 1 FROM document_fingerprints df
                            WHERE df.source_pdf = hd.local_path
                        ) THEN 1 ELSE 0 END
                    ) AS with_fingerprint_count,
                    SUM(
                        CASE WHEN EXISTS (
                            SELECT 1 FROM ncuc_span_artifacts sa
                            WHERE sa.source_pdf = hd.local_path
                        ) THEN 1 ELSE 0 END
                    ) AS with_span_artifacts_count
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                GROUP BY hd.company
                ORDER BY historical_document_count DESC, hd.company
                """
            ).fetchall()
        ]

        fingerprint_quality_breakdown = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    COALESCE(json_extract(metadata_json, '$.outcome_quality'), '(null)') AS outcome_quality,
                    COUNT(*) AS row_count
                FROM document_fingerprints
                GROUP BY COALESCE(json_extract(metadata_json, '$.outcome_quality'), '(null)')
                ORDER BY row_count DESC, outcome_quality
                """
            ).fetchall()
        ]

        historical_without_fingerprint = [
            dict(row)
            for row in conn.execute(
                """
                SELECT hd.id, hd.family_key, hd.company, hd.title, hd.effective_start, hd.local_path
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND NOT EXISTS (
                    SELECT 1 FROM document_fingerprints df
                    WHERE df.source_pdf = hd.local_path
                  )
                ORDER BY hd.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]

        hash_backed_without_fingerprint = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT hd.id, hd.family_key, hd.company, hd.title, hd.effective_start, hd.local_path, hd.content_hash
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND {valid_hd_hash}
                  AND NOT EXISTS (
                    SELECT 1 FROM document_fingerprints df
                    WHERE df.source_pdf = hd.local_path
                  )
                ORDER BY hd.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]

    return {
        "summary": summary,
        "historical_by_company": by_company,
        "fingerprint_quality_breakdown": fingerprint_quality_breakdown,
        "historical_documents_without_fingerprint": historical_without_fingerprint,
        "hash_backed_historical_documents_without_fingerprint": hash_backed_without_fingerprint,
    }
