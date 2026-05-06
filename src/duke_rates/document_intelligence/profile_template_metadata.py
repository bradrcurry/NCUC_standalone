"""
Loader for the profile-template metadata catalog (Phase 3A).

Reads ``profile_templates.yaml`` (sibling to this module) and exposes a
typed view over it. Phase 3B's Tier 1 binder consumes this data to
decide whether a profile is safe to auto-bind, and Phase 4 uses the
``scope`` field to route anchor-required cases away from template-level
binding.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.3A.

Usage::

    from duke_rates.document_intelligence.profile_template_metadata import (
        get_template_metadata, all_templates,
    )
    md = get_template_metadata("generic_residential")
    if md and md.scope == "anchor-required":
        ...  # refuse Tier 1 binding

The loader is robust to missing PyYAML — falls back to an empty catalog
with a warning rather than crashing. Profiles not in the catalog return
``None``; Phase 3B treats that as "no metadata available, assume
anchor-required for safety."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The YAML lives next to this module so it's always shipped with the code.
_CATALOG_PATH = Path(__file__).parent / "profile_templates.yaml"

ALLOWED_SCOPES: tuple[str, ...] = (
    "template-level",
    "anchor-required",
    "bundle-aware",
    "redline-aware",
)


@dataclass(frozen=True)
class TemplateMetadata:
    """Typed view over one entry in profile_templates.yaml."""

    profile: str
    description: str = ""
    utility: str = "any"
    state: str = "any"
    scope: str = "template-level"
    intended_schedule_codes: tuple[str, ...] = field(default_factory=tuple)
    intended_rider_codes: tuple[str, ...] = field(default_factory=tuple)
    intended_families: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def is_anchor_required(self) -> bool:
        """True when the binder must refuse Tier 1 binding without anchors."""
        return self.scope == "anchor-required"

    def covers_schedule_code(self, code: str) -> bool:
        return code in self.intended_schedule_codes

    def covers_rider_code(self, code: str) -> bool:
        return code in self.intended_rider_codes


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CACHE: dict[str, TemplateMetadata] | None = None


def _load_catalog() -> dict[str, TemplateMetadata]:
    """Parse the YAML on first access; cache the result.

    Returns an empty dict (not None) on any error so callers don't have
    to special-case the unavailable case.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "PyYAML not installed; profile-template catalog unavailable. "
            "Install with `pip install pyyaml`."
        )
        _CACHE = {}
        return _CACHE

    if not _CATALOG_PATH.exists():
        logger.warning("profile_templates.yaml not found at %s", _CATALOG_PATH)
        _CACHE = {}
        return _CACHE

    try:
        raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to parse profile_templates.yaml", exc_info=True)
        _CACHE = {}
        return _CACHE

    catalog: dict[str, TemplateMetadata] = {}
    for profile, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        scope = str(entry.get("scope") or "template-level")
        if scope not in ALLOWED_SCOPES:
            logger.warning(
                "Profile %s has unrecognized scope %r; defaulting to template-level",
                profile, scope,
            )
            scope = "template-level"
        catalog[profile] = TemplateMetadata(
            profile=profile,
            description=str(entry.get("description") or ""),
            utility=str(entry.get("utility") or "any"),
            state=str(entry.get("state") or "any"),
            scope=scope,
            intended_schedule_codes=tuple(entry.get("intended_schedule_codes") or []),
            intended_rider_codes=tuple(entry.get("intended_rider_codes") or []),
            intended_families=tuple(entry.get("intended_families") or []),
            notes=str(entry.get("notes") or ""),
        )
    _CACHE = catalog
    return _CACHE


def reload_catalog() -> None:
    """Force the next ``get_template_metadata`` call to re-read the YAML.

    Useful when tests modify the catalog at runtime or when the file is
    edited mid-session and the calling process is long-lived.
    """
    global _CACHE
    _CACHE = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_template_metadata(profile: str) -> TemplateMetadata | None:
    """Return the metadata entry for ``profile``, or ``None`` if not in catalog.

    A ``None`` return tells the binder "no information about this profile;
    treat it as anchor-required for safety."
    """
    if not profile:
        return None
    return _load_catalog().get(profile)


def all_templates() -> dict[str, TemplateMetadata]:
    """Return the full catalog (a copy is fine — entries are frozen)."""
    return dict(_load_catalog())


def is_known_template(profile: str) -> bool:
    return get_template_metadata(profile) is not None


def is_safe_for_tier1_binding(profile: str) -> tuple[bool, str]:
    """Decide whether the Tier 1 binder may auto-bind to this profile.

    Returns ``(allowed, reason)``. The binder uses the reason for
    diagnostic logging when it refuses.

    Rules:
      - Profile not in catalog: refuse (no metadata).
      - Profile scope is 'anchor-required': refuse.
      - Otherwise: allow.
    """
    md = get_template_metadata(profile)
    if md is None:
        return False, f"profile {profile!r} not in profile_templates.yaml catalog"
    if md.is_anchor_required():
        return False, f"profile {profile!r} is anchor-required (scope={md.scope})"
    return True, ""
