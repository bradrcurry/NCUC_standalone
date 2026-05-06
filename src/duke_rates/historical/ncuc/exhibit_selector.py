from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import find_target_by_query
from duke_rates.historical.manual_import import ProgressNCHistoricalImportService
from duke_rates.historical.ncuc.discovery import DUKE_PROGRESS_E2_DOCKETS
from duke_rates.historical.ncuc.family_search_terms import all_profiles
from duke_rates.models.document import DocumentCategory
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFilingClassification
from duke_rates.models.ncuc_exhibit import NcucExhibitCandidate

# ---------------------------------------------------------------------------
# Build FAMILY_ALIASES, FAMILY_EXPECTED_CODES, and FAMILY_TEXT_PROFILES
# from the FamilySearchProfile dictionary, then apply hand-tuned overrides.
# ---------------------------------------------------------------------------

def _build_aliases_from_profiles() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for p in all_profiles():
        if p.family_key.startswith("nc-progress"):
            result[p.leaf] = list(p.aliases)
    return result


def _build_expected_codes_from_profiles() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for p in all_profiles():
        if p.family_key.startswith("nc-progress"):
            codes: set[str] = {p.leaf}
            # Include any short uppercase aliases as expected codes
            for alias in p.aliases:
                if alias.isupper() and len(alias) <= 12 and " " not in alias:
                    codes.add(alias)
            result[p.leaf] = codes
    return result


def _build_text_profiles_from_profiles() -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for p in all_profiles():
        if p.family_key.startswith("nc-progress"):
            result[p.leaf] = {
                "include": list(p.include_terms),
                "exclude": list(p.exclude_terms),
            }
    return result


FAMILY_ALIASES: dict[str, list[str]] = _build_aliases_from_profiles()
FAMILY_EXPECTED_CODES: dict[str, set[str]] = _build_expected_codes_from_profiles()
FAMILY_TEXT_PROFILES: dict[str, dict[str, list[str]]] = _build_text_profiles_from_profiles()

# Hand-tuned overrides for families where the auto-derived profile needs
# extra precision (these were in the original hardcoded version).
FAMILY_TEXT_PROFILES["604"] = {
    "include": ["REPS RIDER", "REPS ADJUSTMENT", "RENEWABLE ENERGY PORTFOLIO STANDARD"],
    "exclude": ["REPS EMF", "EXPERIENCE MODIFICATION FACTOR", "RIDER JAA", "CPRE"],
}
FAMILY_TEXT_PROFILES["605"] = {
    "include": ["REPS EMF", "REPS EMF RIDER", "EXPERIENCE MODIFICATION FACTOR"],
    "exclude": ["RENEWABLE ADVANTAGE", "CPRE"],
}
FAMILY_TEXT_PROFILES["607"] = {
    "include": [
        "STORM COST RECOVERY RIDER",
        "STORM RECOVERY RIDER",
        "STORM SECURITIZATION RIDER",
        "RIDER STS",
        "LEAF NO. 133",
    ],
    "exclude": ["RENEWABLE ADVANTAGE", "REPS EMF", "CPRE", "DSM RIDER", "EE RIDER"],
}
FAMILY_TEXT_PROFILES["609"] = {
    "include": [
        "EARNINGS SHARING MECHANISM", "RIDER ESM", "ESM",
        "JOINT AGENCY ASSET", "RIDER JAA", "JOINT AGENCY",
    ],
    "exclude": ["REPS EMF", "CPRE", "RENEWABLE ADVANTAGE"],
}
FAMILY_TEXT_PROFILES["610"] = {
    "include": ["PERFORMANCE INCENTIVE MECHANISM", "RIDER PIM", "PIM",
                "ENERGY EFFICIENCY RIDER", "EE RIDER", "ENERGY EFFICIENCY"],
    "exclude": ["DEMAND SIDE MANAGEMENT RIDER", "RENEWABLE ADVANTAGE", "CPRE"],
}
FAMILY_TEXT_PROFILES["611"] = {
    "include": ["CUSTOMER ASSISTANCE RECOVERY", "RIDER CAR", "CAR",
                "DEMAND SIDE MANAGEMENT RIDER", "DSM RIDER", "DEMAND SIDE MANAGEMENT"],
    "exclude": ["ENERGY EFFICIENCY RIDER", "RENEWABLE ADVANTAGE", "CPRE"],
}
FAMILY_TEXT_PROFILES["613"] = {
    "include": [
        "STORM SECURITIZATION RIDER",
        "RIDER STS",
        "STORM TRANSITION RIDER",
        "CENTS PER KILOWATT-HOUR",
    ],
    "exclude": ["RENEWABLE ADVANTAGE", "CPRE", "DSM RIDER", "EE RIDER"],
}
FAMILY_TEXT_PROFILES["640"] = {
    "include": ["CPRE", "CLEAN POWER RATE ENHANCEMENT", "PROPOSED RIDER CPRE",
                "ENERGY CONSERVATION DISCOUNT", "RIDER RECD"],
    "exclude": ["RENEWABLE ADVANTAGE"],
}
GENERIC_TITLE_MARKERS = {
    "STATE OF NORTH CAROLINA",
    "INFORMATION SHEET",
    "LAW OFFICE",
    "KENDRICK C. FENTRESS",
    "JACK E. JIRAK",
    "BRIAN L. FRANKLIN",
    "LAWRENCE B. SOMERS",
    "TROUTMAN SANDERS LLP",
}


class NcucExhibitSelector:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository

    def list_candidates(
        self,
        *,
        family_query: str,
        limit: int = 20,
        min_score: float = 20.0,
    ) -> list[NcucExhibitCandidate]:
        target = _resolve_target(self.repository, family_query)
        if not target:
            raise ValueError(f"No Progress NC family target found for query: {family_query}")
        family_code = _normalize_family_code(family_query)
        related_dockets = _related_dockets_for_family(family_code)

        candidates: list[NcucExhibitCandidate] = []
        for record in self.repository.list_ncuc_discovery_records():
            if not _record_matches_target(
                record,
                family_key=target.family_key,
                family_code=family_code,
                related_dockets=related_dockets,
            ):
                continue
            if not record.local_path or not str(record.local_path).lower().endswith(".pdf"):
                continue
            candidate = _score_candidate(
                record,
                family_key=target.family_key,
                family_code=family_code,
                related_dockets=related_dockets,
            )
            if candidate.score >= min_score:
                candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                -item.score,
                item.filing_date or "",
                item.record_id,
            )
        )
        return candidates[:limit]

    def import_candidates(
        self,
        *,
        family_query: str,
        top: int = 3,
        min_score: float = 35.0,
    ) -> list[dict[str, object]]:
        target = _resolve_target(self.repository, family_query)
        if not target:
            raise ValueError(f"No Progress NC family target found for query: {family_query}")
        family_code = _normalize_family_code(family_query)

        importer = ProgressNCHistoricalImportService(self.settings, self.repository)
        imported: list[dict[str, object]] = []
        try:
            for candidate in self.list_candidates(
                family_query=family_query,
                limit=top,
                min_score=min_score,
            ):
                record = self.repository.get_ncuc_discovery_record(candidate.record_id)
                if not record or not record.local_path:
                    continue
                title = _choose_import_title(candidate, fallback_title=target.title)
                category = _infer_import_category(candidate, fallback_category=target.category)
                focused_text, focus_metadata = _build_focused_parse_text(
                    record,
                    family_code=family_code,
                )
                stored = importer.import_document(
                    title=title,
                    category=category,
                    source_label="ncuc-edocket",
                    source_authority="regulator",
                    source_type="ncuc",
                    source_url=record.attachment_url or record.viewer_url or record.discovered_url,
                    local_file=Path(record.local_path),
                    docket_number=record.docket_number,
                    family_key_override=target.family_key,
                    parse_text_override=focused_text,
                    parse_text_metadata=focus_metadata,
                )
                imported.append(
                    {
                        "record_id": candidate.record_id,
                        "historical_id": stored.id,
                        "title": stored.title,
                        "docket_number": record.docket_number,
                        "local_path": record.local_path,
                    }
                )
        finally:
            importer.close()
        return imported


def _record_matches_target(
    record: NcucDiscoveryRecord,
    *,
    family_key: str,
    family_code: str,
    related_dockets: set[str],
) -> bool:
    metadata = _load_mined_metadata(record)
    mined_schedule_codes = {code.upper() for code in metadata.get("extracted_schedule_codes") or []}
    mined_rider_codes = {code.upper() for code in metadata.get("extracted_rider_codes") or []}
    mined_leaf_nos = {leaf.upper() for leaf in metadata.get("extracted_leaf_nos") or []}
    referenced_schedule_codes = {code.upper() for code in record.referenced_schedule_codes}
    referenced_rider_codes = {code.upper() for code in record.referenced_rider_codes}
    referenced_leaf_nos = {leaf.upper() for leaf in record.referenced_leaf_nos}
    family_keys = {key.lower() for key in record.family_keys}

    return any(
        (
            family_key.lower() in family_keys,
            family_code in referenced_schedule_codes,
            family_code in referenced_rider_codes,
            family_code in mined_schedule_codes,
            family_code in mined_rider_codes,
            family_code in mined_leaf_nos,
            family_code in referenced_leaf_nos,
            bool(record.docket_number and record.docket_number in related_dockets),
        )
    )


def _score_candidate(
    record: NcucDiscoveryRecord,
    *,
    family_key: str,
    family_code: str,
    related_dockets: set[str],
) -> NcucExhibitCandidate:
    mined = _load_mined_metadata(record)
    if record.metadata_json:
        try:
            json.loads(record.metadata_json)
        except json.JSONDecodeError:
            pass

    reasons: list[str] = []
    score = 0.0
    referenced_schedule_codes = {code.upper() for code in record.referenced_schedule_codes}
    referenced_rider_codes = {code.upper() for code in record.referenced_rider_codes}
    schedule_codes = [code.upper() for code in mined.get("extracted_schedule_codes") or []]
    rider_codes = [code.upper() for code in mined.get("extracted_rider_codes") or []]
    leaf_nos = [leaf.upper() for leaf in mined.get("extracted_leaf_nos") or []]

    if family_key in record.family_keys:
        score += 25
        reasons.append("family key matched")
    if family_code in referenced_schedule_codes or family_code in referenced_rider_codes:
        score += 20
        reasons.append("seed/docket references matched family code")
    if record.docket_number and record.docket_number in related_dockets:
        score += 12
        reasons.append("related docket matched")

    contains_tariff_text = bool(mined.get("contains_tariff_text"))
    if contains_tariff_text:
        score += 30
        reasons.append("tariff/rider text detected")

    classification = record.filing_classification.value
    if record.filing_classification == NcucFilingClassification.TARIFF_SHEETS:
        score += 25
        reasons.append("classified as tariff sheets")
    elif record.filing_classification == NcucFilingClassification.EXHIBIT:
        score += 15
        reasons.append("classified as exhibit")
    elif record.filing_classification == NcucFilingClassification.COMPLIANCE_FILING:
        score += 12
        reasons.append("classified as compliance filing")

    if schedule_codes:
        score += 4
        reasons.append("content-derived schedule codes")
    if rider_codes:
        score += 4
        reasons.append("content-derived rider codes")
    if leaf_nos:
        score += 4
        reasons.append("leaf references found")
    if family_code in schedule_codes or family_code in rider_codes or family_code in leaf_nos:
        score += 18
        reasons.append("content matched family code")
    extracted_codes = set(referenced_schedule_codes) | set(referenced_rider_codes)
    extracted_codes |= set(schedule_codes) | set(rider_codes) | set(leaf_nos)
    expected_codes = FAMILY_EXPECTED_CODES.get(family_code, {family_code})
    if extracted_codes & expected_codes:
        score += 10
        reasons.append("codes aligned with target family")
    elif extracted_codes:
        score -= 22
        reasons.append("codes point to a different family")
    if mined.get("effective_date"):
        score += 8
        reasons.append("effective date found")
    title_text = f"{candidate_title(record, mined)}".upper()
    if "SCHEDULE" in title_text or "RIDER" in title_text:
        score += 12
        reasons.append("title looks like tariff/rider exhibit")
    alias_hits = [alias for alias in FAMILY_ALIASES.get(family_code, []) if alias in title_text]
    if alias_hits:
        score += 18
        reasons.append(f"title matched aliases: {', '.join(alias_hits[:2])}")
    text_profile = _score_text_profile(record, family_code=family_code, title_text=title_text)
    score += text_profile["score_delta"]
    reasons.extend(text_profile["reasons"])
    if _is_generic_title(title_text) and not alias_hits and not text_profile["positive_hits"]:
        score -= 18
        reasons.append("generic cover-title penalty")
    if _is_procedural_filing(title_text):
        score -= 25
        reasons.append("procedural/affidavit filing penalty")

    local_path = Path(record.local_path or "")
    if local_path.exists():
        size = local_path.stat().st_size
        if 50_000 <= size <= 25_000_000:
            score += 6
            reasons.append("plausible exhibit PDF size")

    if record.attachment_url:
        score += 6
        reasons.append("attachment URL available")

    if record.docket_number:
        score += 4
        reasons.append("docket linked")

    return NcucExhibitCandidate(
        record_id=record.id or 0,
        family_key=family_key,
        docket_number=record.docket_number,
        filing_date=record.filing_date,
        filing_title=record.filing_title,
        local_path=record.local_path,
        score=round(score, 2),
        reasons=reasons,
        contains_tariff_text=contains_tariff_text,
        filing_classification=classification,
        extracted_schedule_codes=schedule_codes,
        extracted_rider_codes=rider_codes,
        extracted_leaf_nos=leaf_nos,
        effective_date=mined.get("effective_date"),
        derived_title=mined.get("selected_title") or mined.get("derived_title"),
    )


def candidate_title(record: NcucDiscoveryRecord, mined: dict[str, object]) -> str:
    return str(
        mined.get("selected_title")
        or mined.get("derived_title")
        or record.filing_title
        or ""
    )


def _load_mined_metadata(record: NcucDiscoveryRecord) -> dict[str, object]:
    if not record.metadata_json:
        return {}
    try:
        metadata = json.loads(record.metadata_json)
    except json.JSONDecodeError:
        return {}
    return metadata.get("pdf_content_mining", {})


def _normalize_family_code(family_query: str) -> str:
    return family_query.strip().upper().replace("LEAF ", "").replace("SCHEDULE ", "")


def _related_dockets_for_family(family_code: str) -> set[str]:
    related: set[str] = set()
    for seed in DUKE_PROGRESS_E2_DOCKETS:
        referenced_codes = {code.upper() for code in seed.referenced_schedule_codes}
        referenced_riders = {code.upper() for code in seed.referenced_rider_codes}
        if family_code in referenced_codes or family_code in referenced_riders:
            related.add(seed.docket_number)
    return related


def _infer_import_category(candidate: NcucExhibitCandidate, *, fallback_category: str) -> str:
    title = (candidate.derived_title or candidate.filing_title or "").upper()
    if any(
        keyword in title for keyword in ("TESTIMONY", "WORKPAPER", "EXHIBIT", "ORDER", "NOTICE")
    ):
        return DocumentCategory.OTHER.value
    if " SCHEDULE " in f" {title} ":
        return DocumentCategory.RATE.value
    if " RIDER " in f" {title} ":
        return DocumentCategory.RIDER.value
    if candidate.extracted_schedule_codes and not candidate.extracted_rider_codes:
        return DocumentCategory.RATE.value
    if candidate.extracted_rider_codes and not candidate.extracted_schedule_codes:
        return DocumentCategory.RIDER.value
    if candidate.filing_classification == NcucFilingClassification.TARIFF_SHEETS.value:
        return (
            DocumentCategory.RATE.value
            if fallback_category == DocumentCategory.RATE.value
            else DocumentCategory.RIDER.value
        )
    return DocumentCategory.OTHER.value


def _choose_import_title(candidate: NcucExhibitCandidate, *, fallback_title: str) -> str:
    title = (candidate.derived_title or candidate.filing_title or "").strip()
    title_upper = title.upper()
    fallback_upper = fallback_title.upper()
    if not title:
        return fallback_title
    if _is_generic_title(title_upper):
        return fallback_title
    if len(title) > 120:
        return fallback_title
    if (
        title_upper.startswith("ON ")
        or title_upper.startswith("PURSUANT TO ")
        or "ORDER APPROVING" in title_upper
        or "PUBLIC NOTICE" in title_upper
        or "COMPLIANCE REPORT" in title_upper
    ):
        return fallback_title
    family_aliases = FAMILY_ALIASES.get(_family_code_from_family_key(candidate.family_key), [])
    has_alias_match = any(alias in title_upper for alias in family_aliases)
    if not has_alias_match and not ("RIDER" in title_upper or "SCHEDULE" in title_upper):
        return fallback_title
    if fallback_upper in title_upper:
        return title
    return title


SECTION_SIGNAL_PATTERNS = (
    "LEAF NO.",
    "REVISED LEAF",
    "SCHEDULE",
    "RIDER",
    "EFFECTIVE",
    "PER KWH",
    "CENTS PER KWH",
    "PER CUSTOMER ACCOUNT",
    "PER MONTH",
    "RESIDENTIAL",
    "GENERAL SERVICE",
    "LIGHTING",
)


def _build_focused_parse_text(
    record: NcucDiscoveryRecord,
    *,
    family_code: str,
) -> tuple[str | None, dict[str, object] | None]:
    text = _load_candidate_text(record)
    if not text:
        return (None, None)
    profile = FAMILY_TEXT_PROFILES.get(family_code, {"include": [], "exclude": []})
    alias_terms = FAMILY_ALIASES.get(family_code, [])
    include_terms = list(dict.fromkeys([*profile.get("include", []), *alias_terms, family_code]))
    segments = [
        segment.strip()
        for segment in re.split(r"\n\s*\n+", text)
        if segment and segment.strip()
    ]
    scored: list[tuple[float, int, list[str], str]] = []
    for index, segment in enumerate(segments):
        segment_upper = segment.upper()
        score = 0.0
        matched_terms: list[str] = []
        for term in include_terms:
            if term and term.upper() in segment_upper:
                score += 4.0
                matched_terms.append(term)
        for term in profile.get("exclude", []):
            if term and term.upper() in segment_upper:
                score -= 2.5
        for signal in SECTION_SIGNAL_PATTERNS:
            if signal in segment_upper:
                score += 1.0
        if len(segment) > 2400:
            score -= 1.0
        if score > 0:
            scored.append((score, index, matched_terms, segment))

    if not scored:
        return (None, None)

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected_indexes = {index for _, index, _, _ in scored[:3]}
    expanded_indexes = {
        neighbor
        for index in selected_indexes
        for neighbor in range(max(0, index - 1), min(len(segments), index + 2))
    }
    excerpt_segments = [segments[index] for index in sorted(expanded_indexes)]
    excerpt = "\n\n".join(excerpt_segments).strip()
    line_excerpt, line_metadata = _build_line_window_excerpt(
        text,
        family_code=family_code,
        include_terms=include_terms,
    )
    if line_excerpt:
        excerpt = "\n\n".join(part for part in (line_excerpt, excerpt) if part).strip()
    if not excerpt:
        return (None, None)
    if len(excerpt) > 8000:
        excerpt = excerpt[:8000].rstrip()
    matched_terms = sorted(
        {
            term
            for _, index, terms, _ in scored[:3]
            if index in selected_indexes
            for term in terms
        }
    )
    return (
        excerpt,
        {
            "strategy": "ncuc_focused_excerpt",
            "family_code": family_code,
            "matched_terms": matched_terms,
            "segment_count": len(excerpt_segments),
            "line_window_count": line_metadata["window_count"] if line_metadata else 0,
        },
    )


def _build_line_window_excerpt(
    text: str,
    *,
    family_code: str,
    include_terms: list[str],
) -> tuple[str | None, dict[str, object] | None]:
    lines = [line.rstrip() for line in text.splitlines()]
    windows: list[tuple[int, int]] = []
    uppercase_terms = [term.upper() for term in include_terms if term]
    signal_terms = (
        "PER CUSTOMER ACCOUNT",
        "PER MONTH",
        "CENTS PER KWH",
        "RIDER",
        "SCHEDULE",
        "LEAF NO.",
        "EFFECTIVE",
    )
    for index, line in enumerate(lines):
        upper = line.upper()
        if any(term in upper for term in uppercase_terms):
            windows.append((max(0, index - 3), min(len(lines), index + 12)))
            continue
        if family_code in upper and any(signal in upper for signal in signal_terms):
            windows.append((max(0, index - 3), min(len(lines), index + 10)))
    if not windows:
        return (None, None)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 2:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    excerpt_lines: list[str] = []
    for start, end in merged[:6]:
        excerpt_lines.extend(lines[start:end])
        excerpt_lines.append("")
    excerpt = "\n".join(excerpt_lines).strip()
    return (
        excerpt or None,
        {
            "window_count": len(merged[:6]),
        },
    )


def _family_code_from_family_key(family_key: str) -> str:
    return family_key.split("-")[-1].upper()


def _score_text_profile(
    record: NcucDiscoveryRecord,
    *,
    family_code: str,
    title_text: str,
) -> dict[str, object]:
    profile = FAMILY_TEXT_PROFILES.get(family_code)
    if not profile:
        return {"score_delta": 0.0, "reasons": [], "positive_hits": []}
    text = _load_candidate_text(record).upper()
    searchable = f"{title_text}\n{text}" if text else title_text
    positive_hits = [term for term in profile["include"] if term in searchable]
    # For STS families (607/613), discount weak "STS" hits that appear only as a cost
    # abbreviation in DSM/EE context rather than as an explicit rider name.
    if family_code in {"607", "613"}:
        positive_hits = _filter_sts_false_positives(positive_hits, searchable)
    negative_hits = [term for term in profile["exclude"] if term in searchable]
    reasons: list[str] = []
    score_delta = 0.0
    if positive_hits:
        score_delta += 22
        reasons.append(f"text matched family terms: {', '.join(positive_hits[:2])}")
    if negative_hits and not positive_hits:
        score_delta -= 18
        reasons.append(f"text matched competing terms: {', '.join(negative_hits[:2])}")
    elif negative_hits:
        score_delta -= 6
        reasons.append(f"mixed-family text detected: {', '.join(negative_hits[:2])}")
    return {
        "score_delta": score_delta,
        "reasons": reasons,
        "positive_hits": positive_hits,
    }


# Patterns that indicate "STS" is an abbreviation for costs/items, not the rider name.
_STS_COST_CONTEXT_RE = re.compile(
    r"\b(?:costs?\s+incurred|sts\s+costs?|non-sts\s+costs?|sts\s+balance|"
    r"sts\s+amortization|sts\s+carrying|recoverable\s+sts|deferral)\b",
    re.I,
)
# Patterns that confirm STS appears as an explicit rider name.
_STS_RIDER_NAME_RE = re.compile(
    r"\b(?:rider\s+sts|sts\s+rider|storm\s+securitization\s+rider|"
    r"storm\s+transition\s+rider|storm\s+cost\s+recovery\s+rider)\b",
    re.I,
)


def _filter_sts_false_positives(positive_hits: list[str], searchable: str) -> list[str]:
    """Remove generic STS/STORM hits when text only references STS as a cost abbreviation."""
    weak_sts_terms = {"STS", "STORM TRANSITION", "STORM SECURITIZATION"}
    strong_sts_terms = {
        "STORM SECURITIZATION RIDER",
        "RIDER STS",
        "STORM TRANSITION RIDER",
        "STORM COST RECOVERY RIDER",
        "CENTS PER KILOWATT-HOUR",
        "LEAF NO. 133",
    }
    has_strong = any(term in positive_hits for term in strong_sts_terms)
    if has_strong:
        return positive_hits
    has_weak_only = all(term in weak_sts_terms for term in positive_hits)
    if has_weak_only and positive_hits:
        # Keep if text contains explicit rider name reference; drop if only cost context.
        if _STS_RIDER_NAME_RE.search(searchable):
            return positive_hits
        if _STS_COST_CONTEXT_RE.search(searchable):
            return []
    return positive_hits


def _load_candidate_text(record: NcucDiscoveryRecord) -> str:
    mined = _load_mined_metadata(record)
    text_path = mined.get("text_path")
    candidate_paths: list[Path] = []
    if text_path:
        candidate_paths.append(Path(str(text_path)))
    if record.local_path:
        local_path = Path(str(record.local_path))
        candidate_paths.append(local_path.with_suffix(".pdf.txt"))
        candidate_paths.append(local_path.with_suffix(local_path.suffix + ".txt"))
    for path in candidate_paths:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    return ""


PROCEDURAL_FILING_MARKERS = {
    "TESTIMONY OF",
    "AFFIDAVIT OF",
    "REBUTTAL TESTIMONY",
    "SURREBUTTAL TESTIMONY",
    "PREFILED TESTIMONY",
    "VERIFIED COMPLAINT",
    "MOTION TO",
    "BRIEF OF",
    "COMMENTS OF",
    "PETITION OF",
    "APPLICATION OF",
    "NOTICE OF FILING",
    "CERTIFICATE OF SERVICE",
    "SUPPLEMENTAL RESPONSE",
    "RESPONSES TO",
    "ANNUAL REPORT",
    "COMPLIANCE REPORT",
}


def _is_generic_title(title_text: str) -> bool:
    if any(marker in title_text for marker in GENERIC_TITLE_MARKERS):
        return True
    return len(title_text) > 140 and "RIDER" not in title_text and "SCHEDULE" not in title_text


def _is_procedural_filing(title_text: str) -> bool:
    return any(marker in title_text for marker in PROCEDURAL_FILING_MARKERS)


# Build FALLBACK_FAMILY_TITLES and RATE_FAMILIES from profiles
FALLBACK_FAMILY_TITLES: dict[str, str] = {
    p.leaf: p.title
    for p in all_profiles()
    if p.family_key.startswith("nc-progress")
}

# Rate (non-rider) families: schedule codes that don't start with RIDER_ and
# aren't DSM programs (leaf 500–575 range are rate schedules).
RATE_FAMILIES: set[str] = {
    p.leaf
    for p in all_profiles()
    if p.family_key.startswith("nc-progress")
    and not p.schedule_code.startswith("RIDER_")
    and not p.schedule_code.startswith("PROGRAM_")
    and not p.schedule_code.startswith("YOUR_")
    and not p.schedule_code.startswith("EVSE")
    and not p.schedule_code.startswith("POWER_PAIR")
    and not p.schedule_code.startswith("SERVICE_")
    and not p.schedule_code.startswith("OUTDOOR_")
    and not p.schedule_code.startswith("LINE_")
    and not p.schedule_code.startswith("STANDARD_")
    and not p.schedule_code.startswith("SUMMARY_")
    and p.leaf not in {"600", "601"}  # rider summaries
}


def _resolve_target(repository: Repository, family_query: str):
    target = find_target_by_query(repository, family_query, missing_only=False)
    # Only use the documents-based target if it has a proper nc-progress family key.
    # The documents table uses URL paths as family keys; prefer tariff_families lookup.
    if target and target.family_key.startswith("nc-progress"):
        return target
    normalized = family_query.strip().upper()

    # Try to look up the leaf in the tariff_families table directly
    # This covers leaves that exist in tariff_families (nc-progress-leaf-NNN) but
    # don't yet have parsed documents (missing from build_progress_nc_family_targets)
    try:
        conn = repository._connect()
        row = conn.execute(
            "SELECT family_key, title, family_type FROM tariff_families WHERE family_key = ? OR schedule_code = ?",
            (f"nc-progress-leaf-{normalized}", normalized),
        ).fetchone()
        if not row:
            # Try matching by leaf number suffix
            row = conn.execute(
                "SELECT family_key, title, family_type FROM tariff_families "
                "WHERE family_key LIKE ? AND family_key LIKE 'nc-progress%'",
                (f"%-{normalized.lstrip('0')}",),
            ).fetchone()
        if row:
            fk, title, ftype = row
            return SimpleNamespace(
                family_key=fk,
                category=(
                    DocumentCategory.RATE.value if ftype in ("base_schedule", "optional_service")
                    else DocumentCategory.RIDER.value
                ),
                title=title or FALLBACK_FAMILY_TITLES.get(normalized, normalized),
            )
    except Exception:
        pass

    if normalized in FALLBACK_FAMILY_TITLES:
        return SimpleNamespace(
            family_key=f"ncuc-dep-{normalized}",
            category=(
                DocumentCategory.RATE.value
                if normalized in RATE_FAMILIES
                else DocumentCategory.RIDER.value
            ),
            title=FALLBACK_FAMILY_TITLES[normalized],
        )
    return None
