from __future__ import annotations

import argparse

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc.lineage_gaps import (
    apply_family_link_suggestions,
    suggest_family_links,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit stranded NCUC discovery records that already have span-level family clues."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write recovered family_keys_json and referenced leaf/schedule codes back to ncuc_discovery_records.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of stranded records to print in detail.",
    )
    args = parser.parse_args()

    settings = get_settings()
    repo = Repository(settings.database_path)
    suggestions = suggest_family_links(repo, limit=args.limit)

    print(f"Auto-matchable stranded discovery records: {len(suggestions)}")
    for item in suggestions:
        top_match = item["matches"][0]
        print(
            f"- id={item['discovery_record_id']} docket={item['docket_number']} "
            f"family={top_match['family_key']} score={top_match['score']}"
        )
        print(f"  title={item['filing_title']}")
        print(f"  leafs={item['leaf_nos']}")
        print(f"  codes={item['schedule_codes']}")
        print(f"  reasons={top_match['reasons']}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to persist recovered discovery-family links.")
        return

    updated = apply_family_link_suggestions(repo, suggestions)
    print(f"\nUpdated {updated} ncuc_discovery_records with recovered family clues.")


if __name__ == "__main__":
    main()
