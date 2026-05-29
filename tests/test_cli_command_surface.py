"""Snapshot test guarding the CLI command surface.

This test exists to make the cli.py split refactor safe: moving 282 commands
from a single 20k-line file into per-area sub-app modules must not drop,
rename, or duplicate any command. The snapshot is the source of truth for
the command surface — update it deliberately, not accidentally.

Update procedure when adding/removing/renaming a command:
    pytest tests/test_cli_command_surface.py --snapshot-update

(or just edit tests/cli_command_surface.snapshot.txt by hand and commit it
alongside the cli change.)
"""

from __future__ import annotations

from pathlib import Path

import typer.main

from duke_rates.cli import app


SNAPSHOT_PATH = Path(__file__).parent / "cli_command_surface.snapshot.txt"


def _current_command_names() -> list[str]:
    click_app = typer.main.get_command(app)
    names: list[str] = []
    for name, cmd in click_app.commands.items():  # type: ignore[attr-defined]
        if isinstance(cmd, type(click_app)) and hasattr(cmd, "commands"):
            for sub_name in cmd.commands:  # type: ignore[attr-defined]
                names.append(f"{name} {sub_name}")
        else:
            names.append(name)
    return sorted(names)


def test_cli_command_surface_matches_snapshot() -> None:
    """Fail loudly if the command set drifts during the cli.py split.

    The intent is *not* to forbid changes — it's to force them to be
    deliberate. If this fails after a legitimate add/rename, update the
    snapshot file in the same commit as the cli change.
    """
    current = _current_command_names()
    assert SNAPSHOT_PATH.exists(), (
        f"Snapshot missing. Create it by writing the current command list to "
        f"{SNAPSHOT_PATH}:\n" + "\n".join(current)
    )
    expected = [
        line.strip()
        for line in SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    missing = sorted(set(expected) - set(current))
    added = sorted(set(current) - set(expected))
    assert not missing and not added, (
        "CLI command surface drift detected.\n"
        f"  Missing (in snapshot but not registered): {missing}\n"
        f"  Added   (registered but not in snapshot): {added}\n"
        f"If intentional, update {SNAPSHOT_PATH.name} in this commit."
    )


def test_cli_command_count_is_stable() -> None:
    """Belt-and-suspenders check that the total count hasn't shifted.

    Independent of the snapshot — catches the case where someone updates
    the snapshot file in one direction but a count drift signals an
    unintended change in another.
    """
    expected_count = 335
    actual = len(_current_command_names())
    assert actual == expected_count, (
        f"Expected {expected_count} CLI commands, got {actual}. "
        "If intentional, update expected_count and the snapshot file."
    )
