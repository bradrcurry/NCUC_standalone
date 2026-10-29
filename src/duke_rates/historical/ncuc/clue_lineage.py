"""Supersession links and typed gaps derived from the clue ledger (Phase 2E D1).

Everything here is rebuildable from ``regulatory_clues`` + ``clue_associations``
and the (read-only) version tables. A rebuild never touches review columns
(``human_review``) or a gap's lifecycle ``status`` once it has left ``open``.

Link status:

* ``verified``: exactly one version in the same family carries a source clue
  for the printed predecessor revision, both sides name the same utility (or
  neither names one), the predecessor's effective date precedes the successor's,
  and the successor's own revision is later than the one it supersedes. The
  rule and every check are stored. A rule match is not a human review; that is
  ``human_review = 'confirmed'``.
* ``candidate``: a predecessor with the printed revision exists, but a check
  could not be evaluated (no effective date), or several versions share that
  revision.
* ``conflict``: the evidence contradicts itself (the "predecessor" is dated after
  the successor; one sheet prints two different supersedes targets; it
  supersedes a different leaf).
* ``explicit_unresolved``: the reference is printed but no version in the
  family carries that revision (or the printed reference is a redline glue).

Chronological adjacency alone never creates a link.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from duke_rates.historical.ncuc.regulatory_clues import revision_display

BUILDER_VERSION = "clue_lineage@1.1.0"
VERIFICATION_RULE = "same_family+printed_revision_match+utility+effective_order+ordinal_order@1"
_LEAF_FAMILY_RE = re.compile(r"-leaf-([0-9]+[a-z]?(?:-[0-9]+)?)$", re.I)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uid(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()


def family_leaf(family_key: str | None) -> str | None:
    m = _LEAF_FAMILY_RE.search(family_key or "")
    return m.group(1).upper() if m else None


def family_utility(family_key: str | None) -> str | None:
    key = family_key or ""
    if key.startswith(("nc-progress", "sc-progress")):
        return "DEP"
    if key.startswith(("nc-carolinas", "sc-carolinas")):
        return "DEC"
    return None


# ---------------------------------------------------------------------------
# Version evidence
# ---------------------------------------------------------------------------


@dataclass
class Clue:
    id: int
    kind: str
    role: str | None
    file_sha256: str
    page: int
    printed_text: str
    value: str | None
    normalization: str
    detail: dict


@dataclass
class VersionEvidence:
    id: int
    family_key: str
    historical_document_id: int | None
    effective_start: str | None
    status: str | None
    clues: list[Clue] = field(default_factory=list)
    pages: set = field(default_factory=set)
    index_pages: set = field(default_factory=set)
    leaf_hint: str | None = None  # from tariff_families / historical_documents when the key has no leaf
    schedule_base: str | None = None  # printed form of tariff_families.schedule_code ("SGS-TOU-CLR", "JAA")

    # Derived
    identities: dict = field(default_factory=dict)  # token -> [clue ids]
    identity_status: str = "none"
    redline_markers: list = field(default_factory=list)
    utilities: set = field(default_factory=set)

    @property
    def leaf(self) -> str | None:
        return family_leaf(self.family_key) or self.leaf_hint

    def leaf_ordinals(self) -> set[int]:
        return {t[3] for t in self.identities if t[0] == "leaf"}

    def effective(self) -> tuple[str | None, str]:
        if self.effective_start:
            return self.effective_start[:10], "tariff_versions.effective_start"
        dates = sorted(
            c.value.split("/")[0] for c in self.family_clues("effective_date") if c.value
        )
        return (dates[0], "effective_date_clue") if dates else (None, "none")

    def page_leaves(self) -> dict[tuple[str, int], set[str]]:
        leaves: dict[tuple[str, int], set[str]] = defaultdict(set)
        for c in self.clues:
            if c.kind == "revision" and c.role != "index":
                leaves[(c.file_sha256, c.page)].add(c.detail.get("leaf"))
        return leaves

    def family_pages(self) -> set[tuple[str, int]]:
        """Span pages that belong to this family.

        A page qualifies when it prints the family's leaf (or no leaf at all) and its
        header does not name the other utility (joint DEC/DEP filings share spans).
        """
        leaves = self.page_leaves()
        own = self.leaf
        utility = family_utility(self.family_key)
        header_utilities: dict[tuple[str, int], set] = defaultdict(set)
        for c in self.clues:
            if c.kind == "utility" and c.role == "header":
                header_utilities[(c.file_sha256, c.page)].add(c.value)
        return {
            p for p in self.pages
            if p not in self.index_pages
            and (own is None or not leaves.get(p) or own in leaves[p])
            and (utility is None or not header_utilities.get(p) or utility in header_utilities[p])
        }

    def page_has_own_revision(self, c: Clue) -> bool:
        return self.leaf in self.page_leaves().get((c.file_sha256, c.page), set())

    def owns_supersedes(self, c: Clue) -> bool:
        """Whether a supersedes clue on a span page speaks about this family's sheet.

        Bundled spans hold other sheets: "Supersedes Schedule SGS-56" on a page of a
        leaf-571 (SLS) span belongs to the SGS sheet, not to this version.
        """
        target = c.detail.get("target")
        if target == "schedule":
            return bool(self.schedule_base) and c.detail.get("base") == self.schedule_base
        if self.leaf is None:
            # Without a family leaf, nothing ties a leaf reference to this family.
            return False
        if target == "leaf" and c.detail.get("leaf") == self.leaf:
            return True
        return self.page_has_own_revision(c)

    def family_clues(self, kind: str) -> list[Clue]:
        pages = self.family_pages()
        return [c for c in self.clues if c.kind == kind and c.role != "index"
                and (c.file_sha256, c.page) in pages]


def load_version_evidence(conn: sqlite3.Connection, family_prefixes: tuple[str, ...] = ("nc-",)) -> dict[int, VersionEvidence]:
    versions: dict[int, VersionEvidence] = {}
    leaf_hints = family_leaf_hints(conn)
    schedule_bases = {
        key: printed_schedule_base(code)
        for key, code in conn.execute("SELECT family_key, schedule_code FROM tariff_families")
    }
    for row in conn.execute(
        "SELECT id, family_key, historical_document_id, effective_start, status FROM tariff_versions"
    ):
        if row[1] and row[1].startswith(family_prefixes):
            versions[row[0]] = VersionEvidence(row[0], row[1], row[2], row[3], row[4],
                                               leaf_hint=leaf_hints.get(row[1]),
                                               schedule_base=schedule_bases.get(row[1]))
    for row in conn.execute(
        """
        SELECT a.tariff_version_id, c.id, c.clue_kind, c.role, c.file_sha256, c.page, c.printed_text,
               c.normalized_value, c.normalization, c.normalized_json
        FROM clue_associations a JOIN regulatory_clues c ON c.id = a.clue_id
        WHERE a.tariff_version_id IS NOT NULL
        ORDER BY c.page, c.char_start
        """
    ):
        ev = versions.get(row[0])
        if ev is None:
            continue
        ev.clues.append(Clue(row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
                             json.loads(row[9] or "{}")))
    # Pages scanned per version (including pages that print nothing).
    for row in conn.execute(
        """
        SELECT DISTINCT tv.id, s.file_sha256, s.page, s.page_artifact_id
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        JOIN clue_scan_pages s ON s.source_pdf = hd.local_path
        WHERE s.page BETWEEN COALESCE(hd.start_page, 1) AND COALESCE(hd.end_page, hd.start_page, 100000)
        """
    ):
        ev = versions.get(row[0])
        if ev is not None:
            ev.pages.add((row[1], row[2]))
    index_pages = {
        (r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT file_sha256, page FROM clue_scan_pages WHERE is_index_page = 1")
    }
    for ev in versions.values():
        ev.index_pages = ev.pages & index_pages
        _derive_identity(ev)
    return versions


def printed_schedule_base(code: str | None) -> str | None:
    """``RIDER_JAA_RY1`` -> ``JAA``; ``SGS_TOU_CLR`` -> ``SGS-TOU-CLR``."""
    if not code:
        return None
    base = re.sub(r"^RIDER_|_RY_?\d+$", "", code.upper())
    return base.replace("_", "-") or None


def family_leaf_hints(conn: sqlite3.Connection) -> dict[str, str]:
    """Leaf of families whose key does not name one: the family's identifier, else its documents' leaf."""
    hints: dict[str, str] = {}
    for key, ident in conn.execute("SELECT family_key, tariff_identifier FROM tariff_families"):
        m = re.fullmatch(r"leaf-([0-9]+[a-z]?(?:-[0-9]+)?)", ident or "", re.I)
        if m and not family_leaf(key):
            hints[key] = m.group(1).upper()
    for key, leaf, _ in conn.execute(
        "SELECT family_key, leaf_no, COUNT(*) FROM historical_documents "
        "WHERE leaf_no IS NOT NULL AND family_key IS NOT NULL GROUP BY 1, 2 ORDER BY 3"
    ):
        if not family_leaf(key) and key not in hints and re.fullmatch(r"[0-9]+[A-Z]?(?:-[0-9]+)?", leaf or ""):
            hints[key] = leaf
    return hints


def _derive_identity(ev: VersionEvidence) -> None:
    own_leaf = ev.leaf
    ambiguous = []
    for c in ev.family_clues("revision"):
        if own_leaf and c.detail.get("leaf") != own_leaf:
            continue
        if c.value is None:
            ambiguous.append(c)
            continue
        token = ("leaf", c.detail["state"], c.detail["leaf"], c.detail["ordinal"])
        ev.identities.setdefault(token, []).append(c.id)
        if c.detail.get("proposed_marker"):
            ev.redline_markers.append({"marker": "proposed_header", "clue_id": c.id})
    for c in ev.family_clues("schedule_code"):
        code = (c.value or "").removeprefix("schedule:")
        m = re.fullmatch(r"(.+)-\d+[A-Z]?", code)
        if m and ev.schedule_base and m.group(1) == ev.schedule_base:
            ev.identities.setdefault(("schedule", code), []).append(c.id)
    for c in ambiguous:
        ev.redline_markers.append({"marker": "glued_revision_header", "clue_id": c.id,
                                   "ordinals_printed": c.detail.get("ordinals_printed")})
    for c in ev.family_clues("approval_reference"):
        if c.normalization == "blank":
            ev.redline_markers.append({"marker": "blank_order_date", "clue_id": c.id})
    ev.utilities = {c.value for c in ev.family_clues("utility") if c.role == "header"}
    leaf_ordinals = ev.leaf_ordinals()
    schedules = {t for t in ev.identities if t[0] == "schedule"}
    if len(leaf_ordinals) > 1 or len(schedules) > 1:
        ev.identity_status = "conflict"
    elif ev.identities:
        ev.identity_status = "single"
    elif ambiguous:
        ev.identity_status = "redline_only"
    else:
        ev.identity_status = "none"


def _target_token(c: Clue):
    d = c.detail
    if d.get("target") == "leaf" and d.get("ordinal") is not None:
        return ("leaf", d["state"], d["leaf"], d["ordinal"])
    if d.get("target") == "schedule":
        return ("schedule", d["code"])
    return None


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def build_lineage_links(conn: sqlite3.Connection, versions: dict[int, VersionEvidence]) -> dict:
    by_family: dict[str, list[VersionEvidence]] = defaultdict(list)
    by_token: dict[tuple, list[VersionEvidence]] = defaultdict(list)
    for ev in versions.values():
        by_family[ev.family_key].append(ev)
        for token in ev.identities:
            by_token[token].append(ev)
    now = _now()
    seen_uids: set[str] = set()
    counts: dict[str, int] = defaultdict(int)
    versions_with_supersedes = 0
    unattributed = 0  # versions whose span prints supersedes text that belongs to other sheets
    for ev in versions.values():
        printed = ev.family_clues("supersedes")
        sup = [c for c in printed if ev.owns_supersedes(c)]
        if printed and not sup:
            unattributed += 1
        if not sup:
            continue
        versions_with_supersedes += 1
        targets: dict[object, list[Clue]] = defaultdict(list)
        for c in sup:
            token = _target_token(c)
            key = token if token else ("unresolvable", c.printed_text.strip())
            targets[key].append(c)
        resolvable = [k for k in targets if k[0] in ("leaf", "schedule")]
        same_leaf = [k for k in resolvable if k[0] != "leaf" or ev.leaf is None or k[2] == ev.leaf]
        multiple_targets = len(same_leaf) > 1
        for key, clues in targets.items():
            first = clues[0]
            status, to_version, pred_clue, checks = _evaluate_target(
                ev, key, by_family[ev.family_key], by_token, multiple_targets)
            uid = _uid("supersedes", ev.id, key)
            seen_uids.add(uid)
            expected = {"token": list(key), "printed": first.printed_text,
                        "supporting_clue_ids": [c.id for c in clues]}
            conn.execute(
                """
                INSERT INTO lineage_links (link_uid, link_kind, family_key, from_version_id, to_version_id,
                    clue_id, predecessor_clue_id, expected_json, status, verification_rule, rule_checks_json,
                    builder_version, created_at, updated_at)
                VALUES (?, 'supersedes', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(link_uid) DO UPDATE SET to_version_id = excluded.to_version_id,
                    clue_id = excluded.clue_id, predecessor_clue_id = excluded.predecessor_clue_id,
                    expected_json = excluded.expected_json, status = excluded.status,
                    verification_rule = excluded.verification_rule, rule_checks_json = excluded.rule_checks_json,
                    builder_version = excluded.builder_version, updated_at = excluded.updated_at
                """,
                (uid, ev.family_key, ev.id, to_version, first.id, pred_clue, json.dumps(expected),
                 status, VERIFICATION_RULE, json.dumps(checks, default=str), BUILDER_VERSION, now, now),
            )
            counts[status] += 1
    stale = [r[0] for r in conn.execute("SELECT link_uid FROM lineage_links") if r[0] not in seen_uids]
    for uid in stale:  # derived rows no longer supported by evidence (reviewed rows are kept)
        conn.execute("DELETE FROM lineage_links WHERE link_uid = ? AND human_review = 'unreviewed'", (uid,))
    conn.commit()
    return {"versions_printing_supersedes": versions_with_supersedes,
            "versions_with_only_other_sheets_supersedes": unattributed, "links_by_status": dict(counts),
            "stale_links_removed": len(stale)}


def _evaluate_target(ev, key, family, by_token, multiple_targets):
    checks: dict = {"multiple_supersedes_targets_on_sheet": multiple_targets}
    if any(m.get("marker") == "proposed_header" for m in ev.redline_markers):
        checks.update(
            {"proposed_header": True, "reason": "proposed revision is not actual supersession"}
        )
        return "candidate", None, None, checks
    if key[0] == "unresolvable":
        checks["reason"] = "printed reference is a redline glue or not a leaf/schedule identifier"
        return "explicit_unresolved", None, None, checks
    if key[0] == "leaf" and ev.leaf and key[2] != ev.leaf:
        checks["reason"] = f"a leaf-{ev.leaf} sheet supersedes leaf {key[2]}"
        return "conflict", None, None, checks
    own_ordinals = ev.leaf_ordinals()
    if key[0] == "leaf" and own_ordinals:
        checks["successor_ordinal_after_target"] = all(o > key[3] for o in own_ordinals)
    successor_date, successor_date_source = ev.effective()
    checks["successor_effective"] = [successor_date, successor_date_source]
    in_family = [p for p in family if p.id != ev.id and key in p.identities]
    checks["predecessors_in_family"] = [p.id for p in in_family]
    other_families = sorted(
        {p.family_key for p in by_token.get(key, []) if p.family_key != ev.family_key}
    )
    if other_families:
        checks["revision_found_in_other_families"] = other_families
    if multiple_targets:
        checks["reason"] = "the sheet prints more than one supersedes target"
        return "conflict", None, None, checks
    if not in_family:
        checks["reason"] = "no version in the family carries the printed predecessor revision"
        return "explicit_unresolved", None, None, checks
    evaluated = []
    for p in in_family:
        pred_date, pred_source = p.effective()
        date_order = None if not (pred_date and successor_date) else pred_date < successor_date
        utility_ok = not (p.utilities and ev.utilities) or bool(p.utilities & ev.utilities)
        evaluated.append(
            {
                "version_id": p.id,
                "effective": [pred_date, pred_source],
                "date_order": date_order,
                "utility_match": utility_ok,
                "revision_clue_ids": p.identities[key],
                "proposed_header": any(
                    m.get("marker") == "proposed_header" for m in p.redline_markers
                ),
            }
        )
    checks["evaluated"] = evaluated
    full = [
        e
        for e in evaluated
        if e["date_order"] is True
        and e["utility_match"]
        and checks.get("successor_ordinal_after_target", True)
        and not e["proposed_header"]
    ]
    if any(e["date_order"] is False for e in evaluated) and not full:
        checks["reason"] = (
            "the version carrying the printed predecessor revision is not dated earlier"
        )
        return "conflict", None, None, checks
    if len(full) == 1:
        return "verified", full[0]["version_id"], full[0]["revision_clue_ids"][0], checks
    if len(full) > 1:
        dates = {e["effective"][0] for e in full}
        checks["reason"] = "several versions carry the printed predecessor revision" + (
            "" if len(dates) == 1 else " with different effective dates"
        )
        return "candidate", None, None, checks
    checks["reason"] = (
        "predecessor revision present but an order/utility check could not be satisfied"
    )
    return "candidate", None, None, checks


# ---------------------------------------------------------------------------
# Typed gaps
# ---------------------------------------------------------------------------


@dataclass
class GapDraft:
    gap_type: str
    evidence_basis: str
    family_key: str
    identity: tuple
    anchor_version_id: int | None = None
    known_predecessor_version_id: int | None = None
    known_successor_version_id: int | None = None
    expected_revision: str | None = None
    expected: dict = field(default_factory=dict)
    dockets: list = field(default_factory=list)
    window_start: str | None = None
    window_end: str | None = None
    clue_ids: list = field(default_factory=list)
    link_id: int | None = None
    reason: str | None = None


def _docket_clue_values(ev: VersionEvidence, *, footer_only: bool = False) -> list[str]:
    return sorted({c.value for c in ev.family_clues("docket")
                   if c.value and (c.role == "footer" or not footer_only)})


def build_typed_gaps(conn: sqlite3.Connection, versions: dict[int, VersionEvidence],
                     *, today: date | None = None) -> dict:
    today = today or date.today()
    drafts: dict[str, GapDraft] = {}

    def add(d: GapDraft) -> None:
        uid = _uid(d.gap_type, d.family_key, d.identity)
        if uid in drafts:
            existing = drafts[uid]
            existing.clue_ids = sorted(set(existing.clue_ids) | set(d.clue_ids))
            existing.dockets = sorted(set(existing.dockets) | set(d.dockets))
            if d.window_end and (existing.window_end is None or d.window_end < existing.window_end):
                existing.window_end = d.window_end
                existing.known_successor_version_id = d.known_successor_version_id
            return
        drafts[uid] = d

    by_family: dict[str, list[VersionEvidence]] = defaultdict(list)
    for ev in versions.values():
        if ev.pages:
            by_family[ev.family_key].append(ev)
    links = conn.execute(
        "SELECT id, from_version_id, to_version_id, status, expected_json, clue_id FROM lineage_links"
    ).fetchall()
    links_by_version: dict[int, list] = defaultdict(list)
    for link in links:
        links_by_version[link[1]].append(link)

    for family, members in by_family.items():
        utility = family_utility(family)
        dated = sorted((m.effective()[0] or "9999", m.id, m) for m in members)
        known_leaf: dict[int, list[VersionEvidence]] = defaultdict(list)
        for m in members:
            for o in m.leaf_ordinals():
                known_leaf[o].append(m)
        explicit_missing: dict[int, str | None] = {}  # ordinal -> successor's effective date

        for ev in members:
            eff, _ = ev.effective()
            dockets = _docket_clue_values(ev)
            # 1. Printed predecessor absent, and 8. conflicting supersession.
            for link_id, _, to_vid, status, expected_json, clue_id in links_by_version.get(ev.id, []):
                expected = json.loads(expected_json)
                token = tuple(expected["token"])
                if status == "conflict":
                    add(GapDraft("conflicting_supersession", "explicit", family, ("link", link_id),
                                 anchor_version_id=ev.id, expected={"printed": expected["printed"]},
                                 dockets=dockets, clue_ids=expected["supporting_clue_ids"], link_id=link_id,
                                 reason="supersession evidence contradicts itself"))
                    continue
                if status != "explicit_unresolved" or token[0] not in ("leaf", "schedule"):
                    if status == "explicit_unresolved":
                        add(GapDraft("unresolved_version_identity", "explicit", family,
                                     ("unresolvable_supersedes", ev.id),
                                     anchor_version_id=ev.id, expected={"printed": expected["printed"]},
                                     clue_ids=expected["supporting_clue_ids"], link_id=link_id,
                                     reason="printed supersedes reference cannot be read unambiguously"))
                    continue
                if token[0] == "leaf":
                    if token[3] not in explicit_missing or (eff and eff < (explicit_missing[token[3]] or "9999")):
                        explicit_missing[token[3]] = eff
                    older = [m for m in members if m.leaf_ordinals() and max(m.leaf_ordinals()) < token[3]]
                    expected_rev = revision_display(token[1], token[2], token[3])
                else:
                    older = []
                    expected_rev = f"Schedule {token[1]}"
                prev = max(older, key=lambda m: m.effective()[0] or "", default=None)
                add(GapDraft(
                    "missing_predecessor_revision", "explicit", family, token,
                    anchor_version_id=ev.id, known_successor_version_id=ev.id,
                    known_predecessor_version_id=prev.id if prev else None,
                    expected_revision=expected_rev, expected={"token": list(token), "utility": utility},
                    dockets=dockets, window_start=prev.effective()[0] if prev else None,
                    window_end=eff, clue_ids=expected["supporting_clue_ids"], link_id=link_id,
                    reason=f"{expected['printed']!r} is printed but no version in the family carries it"))

            # 4. Redline / proposed sheet with no clean counterpart.
            if ev.redline_markers:
                candidates = set()
                for marker in ev.redline_markers:
                    candidates.update(marker.get("ordinals_printed") or [])
                candidates |= ev.leaf_ordinals()
                likely = max(candidates) if candidates else None
                clean = [m for m in known_leaf.get(likely, []) if m.id != ev.id and not m.redline_markers]
                if not clean:
                    add(GapDraft(
                        "missing_official_clean_tariff", "explicit", family, ("clean", ev.id),
                        anchor_version_id=ev.id,
                        expected_revision=revision_display("NC", ev.leaf, likely) if (ev.leaf and likely is not None) else None,
                        expected={"likely_ordinal": likely, "likely_ordinal_basis": "machine:max_printed_ordinal",
                                  "markers": ev.redline_markers, "utility": utility},
                        dockets=dockets, window_start=eff, clue_ids=[m["clue_id"] for m in ev.redline_markers],
                        reason="sheet prints redline/proposed markers and no clean sheet with that revision exists"))

            # 5. Docket printed, order date never printed (or blank).
            # Only the rate-sheet footer ("NCUC Docket No. ..., Order dated ...") promises an order date.
            footer_dockets = _docket_clue_values(ev, footer_only=True)
            orders = [c for c in ev.family_clues("order_date") if c.role == "footer"]
            if footer_dockets and not any(c.value for c in orders):
                blank = [c.id for c in orders if c.normalization == "blank"]
                docket_ids = [c.id for c in ev.family_clues("docket") if c.value and c.role == "footer"]
                for docket in footer_dockets:
                    if _local_order_exists(conn, docket):
                        continue
                    add(GapDraft("missing_regulatory_order", "explicit" if blank else "inferred", family,
                                 ("order", docket), anchor_version_id=ev.id, expected={"docket": docket},
                                 dockets=[docket], clue_ids=blank + docket_ids,
                                 reason="docket printed without an order date" + (" (blank)" if blank else "")))

            # 6. No readable effective date on the sheet.
            eff_clues = ev.family_clues("effective_date")
            if ev.family_pages() and not any(c.value for c in eff_clues) and _sheet_text_present(ev):
                blank = [c.id for c in eff_clues if c.normalization in ("blank", "redline_ambiguous")]
                add(GapDraft("missing_effective_date_evidence", "explicit" if blank else "inferred", family,
                             ("effective", ev.id), anchor_version_id=ev.id, dockets=dockets, clue_ids=blank,
                             expected={"recorded_effective_start": ev.effective_start},
                             reason="no unambiguous printed effective date on the bound pages"))

            # 7. Identity not established from the sheet.
            if ev.identity_status != "single":
                reason = {
                    "none": "no revision/schedule header for this family on the bound pages",
                    "conflict": "bound pages print more than one revision of this leaf",
                    "redline_only": "only a redline-glued revision header is printed",
                }[ev.identity_status]
                if ev.pages and ev.index_pages == ev.pages:
                    reason = "every bound page is a tariff index page (binding points at the index)"
                add(GapDraft("unresolved_version_identity", "explicit" if ev.identity_status != "none" else "inferred",
                             family, ("identity", ev.id), anchor_version_id=ev.id, dockets=dockets,
                             expected={"identity_status": ev.identity_status,
                                       "ordinals": sorted(ev.leaf_ordinals())},
                             clue_ids=[c for ids in ev.identities.values() for c in ids], reason=reason))

            # 3. Printed effective period has ended and nothing later exists.
            for c in eff_clues:
                if c.value and "/" in c.value:
                    end = c.value.split("/")[1]
                    later = [m for m in members if (m.effective()[0] or "") > (eff or "")]
                    if end < today.isoformat() and not later:
                        ordinal = max(ev.leaf_ordinals(), default=None)
                        add(GapDraft(
                            "missing_successor_document", "explicit", family, ("successor", ev.id),
                            anchor_version_id=ev.id, known_predecessor_version_id=ev.id,
                            expected_revision=(revision_display("NC", ev.leaf, ordinal + 1)
                                               if ev.leaf and ordinal is not None else None),
                            expected={"after_ordinal": ordinal, "utility": utility},
                            dockets=dockets,
                            window_start=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
                            clue_ids=[c.id], reason=f"printed effective period ends {end}; no later version"))
                        break

        # 2. Ordinal holes between known revisions (inferred).
        known = sorted(set(known_leaf) | set(explicit_missing))
        leaf = members[0].leaf
        for lo, hi in zip(known, known[1:]):
            if hi - lo <= 1 or not leaf:
                continue
            lo_v = known_leaf.get(lo, [None])[0]
            hi_v = known_leaf.get(hi, [None])[0]
            for missing in range(lo + 1, hi):
                add(GapDraft(
                    "missing_intermediate_revision", "inferred", family, ("leaf", "NC", leaf, missing),
                    known_predecessor_version_id=lo_v.id if lo_v else None,
                    known_successor_version_id=hi_v.id if hi_v else None,
                    expected_revision=revision_display("NC", leaf, missing),
                    expected={"token": ["leaf", "NC", leaf, missing], "utility": utility,
                              "between_ordinals": [lo, hi]},
                    window_start=lo_v.effective()[0] if lo_v else None,
                    window_end=hi_v.effective()[0] if hi_v else explicit_missing.get(hi),
                    clue_ids=sorted({i for v in (lo_v, hi_v) if v for ids in v.identities.values() for i in ids}),
                    reason=f"revisions {lo} and {hi} are known; {missing} is neither present nor printed"))
        del dated

    return _write_gaps(conn, drafts, versions)


def _sheet_text_present(ev: VersionEvidence) -> bool:
    return any(c.kind in ("revision", "leaf", "schedule_code", "docket", "utility") for c in ev.clues
               if c.role != "index")


def _local_order_exists(conn: sqlite3.Connection, docket: str) -> bool:
    m = re.match(r"([A-Z]+-\d+) Sub (\d+)", docket)
    found = False
    if m:
        found = (
            conn.execute(
                """
            SELECT 1 FROM ncuc_discovery_records
            WHERE REPLACE(REPLACE(UPPER(COALESCE(docket_number, '')), ' ', ''), ',', '') IN (?, ?)
              AND (COALESCE(sub_number, '') = ? OR UPPER(COALESCE(docket_number, '')) LIKE ?)
              AND (LOWER(COALESCE(filing_classification, '')) LIKE '%order%'
                   OR LOWER(COALESCE(filing_title, '')) LIKE '%order%')
              AND fetch_status IN ('fetched', 'downloaded', 'success', 'ok')
            LIMIT 1
            """,
                (m.group(1), f"{m.group(1)}SUB{m.group(2)}", m.group(2), f"%SUB%{m.group(2)}"),
            ).fetchone()
            is not None
        )
    return found


def _write_gaps(conn: sqlite3.Connection, drafts: dict[str, GapDraft], versions) -> dict:
    now = _now()
    conn.execute("UPDATE lineage_gaps_typed SET active = 0")
    counts: dict[tuple, int] = defaultdict(int)
    for uid, d in drafts.items():
        conn.execute(
            """
            INSERT INTO lineage_gaps_typed (gap_uid, gap_type, evidence_basis, family_key, utility,
                anchor_version_id, known_predecessor_version_id, known_successor_version_id, expected_revision,
                expected_json, docket_candidates_json, window_start, window_end, supporting_clue_ids_json,
                link_id, status, status_reason, active, builder_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 1, ?, ?, ?)
            ON CONFLICT(gap_uid) DO UPDATE SET evidence_basis = excluded.evidence_basis,
                anchor_version_id = excluded.anchor_version_id,
                known_predecessor_version_id = excluded.known_predecessor_version_id,
                known_successor_version_id = excluded.known_successor_version_id,
                expected_revision = excluded.expected_revision, expected_json = excluded.expected_json,
                docket_candidates_json = excluded.docket_candidates_json, window_start = excluded.window_start,
                window_end = excluded.window_end, supporting_clue_ids_json = excluded.supporting_clue_ids_json,
                link_id = excluded.link_id, active = 1, builder_version = excluded.builder_version,
                updated_at = excluded.updated_at,
                status_reason = CASE WHEN lineage_gaps_typed.status = 'open'
                                     THEN excluded.status_reason ELSE lineage_gaps_typed.status_reason END
            """,
            (uid, d.gap_type, d.evidence_basis, d.family_key, family_utility(d.family_key), d.anchor_version_id,
             d.known_predecessor_version_id, d.known_successor_version_id, d.expected_revision,
             json.dumps(d.expected, default=str), json.dumps(d.dockets), d.window_start, d.window_end,
             json.dumps(d.clue_ids), d.link_id, d.reason, BUILDER_VERSION, now, now),
        )
        counts[(d.gap_type, d.evidence_basis)] += 1
    conn.commit()
    return {
        "gaps": len(drafts),
        "by_type": [{"gap_type": k[0], "evidence_basis": k[1], "n": n} for k, n in sorted(counts.items())],
        "inactive_retained": conn.execute(
            "SELECT COUNT(*) FROM lineage_gaps_typed WHERE active = 0").fetchone()[0],
    }


def rebuild_lineage(conn: sqlite3.Connection) -> dict:
    versions = load_version_evidence(conn)
    with_pages = [v for v in versions.values() if v.pages]
    identity = defaultdict(int)
    for v in with_pages:
        identity[v.identity_status] += 1
    links = build_lineage_links(conn, versions)
    versions = load_version_evidence(conn)
    gaps = build_typed_gaps(conn, versions)
    from duke_rates.historical.ncuc.document_relationships import build_document_relationships

    relationships = build_document_relationships(conn)
    return {
        "document_relationships": relationships,
        "versions_with_scanned_pages": len(with_pages),
        "identity_status": dict(identity),
        "links": links,
        "gaps": gaps,
    }


def all_family_leaves(conn: sqlite3.Connection) -> dict[str, str]:
    """Leaf of every family that has one: from its key, else its identifier or documents."""
    leaves = family_leaf_hints(conn)
    for (key,) in conn.execute("SELECT family_key FROM tariff_families"):
        if family_leaf(key):
            leaves[key] = family_leaf(key)
    return leaves
