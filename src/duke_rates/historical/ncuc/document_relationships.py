"""Research document relationships; no rate, approval, or human-review writes.

Version supersession remains in lineage_links. D6 adds only relationships that
that table cannot express. A matching docket does not establish approval scope.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

BUILDER_VERSION = "document_relationships@1.0.0"


def build_document_relationships(conn) -> dict:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='document_relationships'"
    ).fetchone():
        return {"schema_not_installed": True}
    now = datetime.now(UTC).isoformat()
    seen = set()

    def put(
        kind,
        sha,
        vid,
        target,
        clue_ids,
        *,
        target_sha=None,
        target_vid=None,
        basis="source_asserted",
    ):
        if not sha:
            return
        uid = hashlib.sha256(
            json.dumps([kind, sha, vid, target, target_sha, target_vid], sort_keys=True).encode()
        ).hexdigest()
        seen.add(uid)
        prior = conn.execute(
            "SELECT supporting_clue_ids_json FROM document_relationships WHERE relationship_uid=?",
            (uid,),
        ).fetchone()
        if prior:
            clue_ids = sorted(set(clue_ids) | set(json.loads(prior[0])))
        conn.execute(
            """
            INSERT INTO document_relationships(relationship_uid,relationship_kind,source_sha256,
                source_version_id,
                target_sha256,target_version_id,target_reference_json,supporting_clue_ids_json,evidence_basis,
                status,checks_json,builder_version,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'candidate',?,?,?,?)
            ON CONFLICT(relationship_uid) DO UPDATE SET active=1,
                supporting_clue_ids_json=excluded.supporting_clue_ids_json,
                checks_json=excluded.checks_json,builder_version=excluded.builder_version,updated_at=excluded.updated_at
        """,
            (
                uid,
                kind,
                sha,
                vid,
                target_sha,
                target_vid,
                json.dumps(target, sort_keys=True),
                json.dumps(sorted(set(clue_ids))),
                basis,
                json.dumps({"authority_scope_established": False, "requires_source_review": True}),
                BUILDER_VERSION,
                now,
                now,
            ),
        )

    for row in conn.execute(
        "SELECT id,file_sha256,clue_kind,normalized_value,normalized_json FROM regulatory_clues "
        "WHERE clue_kind IN ('docket','approval_reference') ORDER BY id"
    ):
        detail = json.loads(row[4])
        target = {"value": row[3], "source_claim": detail}
        kind = detail.get("relationship")
        if kind not in ("approved_by", "implements", "references"):
            kind = "approved_by" if row[2] == "approval_reference" else "references"
        put(kind, row[1], None, target, [row[0]])
    for row in conn.execute(
        "SELECT l.from_version_id,l.to_version_id,l.clue_id,l.expected_json,c.file_sha256 "
        "FROM lineage_links l JOIN regulatory_clues c ON c.id=l.clue_id "
        "WHERE json_extract(l.rule_checks_json,'$.proposed_header') = 1"
    ):
        put("proposed_revision_of", row[4], row[0], json.loads(row[3]), [row[2]], target_vid=row[1])
    for row in conn.execute(
        "SELECT c.file_sha256,c.matched_clue_ids_json,g.anchor_version_id,g.expected_json "
        "FROM gap_candidates c JOIN lineage_gaps_typed g ON g.id=c.gap_id "
        "WHERE g.active=1 AND g.gap_type='missing_official_clean_tariff' "
        "AND c.verification_status <> 'rejected'"
    ):
        put(
            "clean_counterpart_of",
            row[0],
            None,
            json.loads(row[3]),
            json.loads(row[1] or "[]"),
            target_vid=row[2],
            basis="inferred",
        )
    # Retain obsolete evidence and every human decision; only toggle machine activity.
    for row in conn.execute(
        "SELECT id,relationship_uid FROM document_relationships WHERE active=1"
    ):
        if row[1] not in seen:
            conn.execute(
                "UPDATE document_relationships SET active=0,updated_at=? WHERE id=?", (now, row[0])
            )
    conn.commit()
    return {
        "active": len(seen),
        "counts": [
            dict(zip(("kind", "status", "n"), row, strict=True))
            for row in conn.execute(
                "SELECT relationship_kind,status,COUNT(*) FROM document_relationships "
                "WHERE active=1 GROUP BY 1,2"
            )
        ],
    }
