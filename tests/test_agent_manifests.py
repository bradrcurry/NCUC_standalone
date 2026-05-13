from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "src" / "duke_rates" / "cli.py"
CLI_COMMANDS_DIR = ROOT / "src" / "duke_rates" / "cli_commands"
TOOL_REGISTRY_PATH = ROOT / "docs" / "agent_tool_registry.json"
WORKFLOW_PATH = ROOT / "docs" / "agent_workflows.json"
ROOT_COMMAND_RE = re.compile(r'@app\.command\("([^"]+)"\)')
SUBAPP_COMMAND_RE = re.compile(r'@(\w+)_app\.command\("([^"]+)"\)')
SUBAPP_REGISTRATION_RE = re.compile(r'app\.add_typer\((\w+)_app,\s*name="([^"]+)"\)')


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cli_commands() -> set[str]:
    """Return the full set of registered CLI command paths.

    Includes both root-level commands (e.g. ``crawl``) and sub-app commands
    (e.g. ``ocr show-queue-nc``).
    """
    cli_text = CLI_PATH.read_text(encoding="utf-8")
    commands: set[str] = set(ROOT_COMMAND_RE.findall(cli_text))

    # Map sub-app variable name (e.g. "ocr_app" → "ocr") from cli.py registration.
    subapp_names: dict[str, str] = {
        match.group(1): match.group(2)
        for match in SUBAPP_REGISTRATION_RE.finditer(cli_text)
    }

    # Scan each sub-app module for its commands.
    for module_path in CLI_COMMANDS_DIR.glob("*.py"):
        if module_path.name.startswith("_"):
            continue
        text = module_path.read_text(encoding="utf-8")
        for match in SUBAPP_COMMAND_RE.finditer(text):
            subapp_var = match.group(1)
            subcommand = match.group(2)
            if subapp_var in subapp_names:
                commands.add(f"{subapp_names[subapp_var]} {subcommand}")

    return commands


def test_agent_tool_registry_is_well_formed_and_matches_cli_surface() -> None:
    registry = _load_json(TOOL_REGISTRY_PATH)
    tools = registry["tools"]
    known_statuses = set(registry["status_definitions"])
    known_categories = set(registry["category_definitions"])
    cli_commands = _cli_commands()

    assert registry["schema_version"] == 1
    assert tools

    tool_ids: set[str] = set()
    for tool in tools:
        tool_id = tool["tool_id"]
        assert tool_id not in tool_ids
        tool_ids.add(tool_id)

        assert tool["status"] in known_statuses
        assert tool["category"] in known_categories
        assert isinstance(tool["mutates_state"], bool)
        assert tool["use_when"]
        assert isinstance(tool["avoid_when"], list)
        assert isinstance(tool["prerequisites"], list)
        assert tool["docs"]

        for doc_path in tool["docs"]:
            assert (ROOT / doc_path).exists(), doc_path

        if tool["surface"] == "cli":
            assert tool_id in cli_commands
            assert tool["command"] == f"python -m duke_rates {tool_id}" or tool["command"].startswith(
                f"python -m duke_rates {tool_id} "
            )
        elif tool["surface"] == "script":
            script_path = ROOT / tool["script_path"]
            assert script_path.exists(), tool["script_path"]
            assert tool["command"].startswith("python ")
        else:
            raise AssertionError(f"Unexpected surface {tool['surface']}")

    for tool in tools:
        for next_tool in tool["next_tools"]:
            assert next_tool in tool_ids, next_tool


def test_agent_workflow_manifest_references_known_non_legacy_tools() -> None:
    registry = _load_json(TOOL_REGISTRY_PATH)
    workflows = _load_json(WORKFLOW_PATH)["workflows"]
    tool_map = {tool["tool_id"]: tool for tool in registry["tools"]}

    assert workflows

    workflow_ids: set[str] = set()
    for workflow in workflows:
        workflow_id = workflow["workflow_id"]
        assert workflow_id not in workflow_ids
        workflow_ids.add(workflow_id)

        for doc_path in workflow["read_first"]:
            assert (ROOT / doc_path).exists(), doc_path

        assert workflow["steps"]
        for step in workflow["steps"]:
            tool = tool_map[step["tool_id"]]
            assert tool["status"] in {"supported", "compatibility_alias"}
            assert step["mode"] in {"required", "optional"}

    for workflow in workflows:
        for branch in workflow["branching"]:
            assert branch["next_workflow"] in workflow_ids, branch["next_workflow"]
        for next_workflow in workflow["default_next_workflows"]:
            assert next_workflow in workflow_ids, next_workflow
