from typing import Any

from openai.lib._pydantic import to_strict_json_schema

from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.planning.contracts import PlanProposal
from app.planning.validator import PlanValidator
from app.tool_system.contracts import AgentTurnResponse, ToolRequestTurn


def _assert_openai_strict_schema(value: Any, *, root: bool = False) -> None:
    if isinstance(value, dict):
        if root:
            assert value.get("type") == "object"
        assert "oneOf" not in value
        additional = value.get("additionalProperties")
        assert additional is not True
        assert not isinstance(additional, dict)
        for nested in value.values():
            _assert_openai_strict_schema(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_openai_strict_schema(nested)


def test_live_planner_schema_is_supported_by_openai_strict_outputs() -> None:
    _assert_openai_strict_schema(to_strict_json_schema(PlanProposal), root=True)


def test_live_agent_turn_schema_is_supported_by_openai_strict_outputs() -> None:
    _assert_openai_strict_schema(to_strict_json_schema(AgentTurnResponse), root=True)


def test_agent_turn_wrapper_accepts_existing_mock_shape_and_drops_null_arguments() -> None:
    turn = AgentRuntime._parse_turn(
        {
            "type": "tool_call",
            "tool_name": "filesystem.read",
            "arguments": {"path": "research.md", "content": None},
        }
    )

    assert isinstance(turn, ToolRequestTurn)
    assert turn.arguments == {"path": "research.md"}


def test_agent_turn_normalizes_shared_schema_fields_for_selected_tool() -> None:
    write = AgentRuntime._parse_turn(
        {
            "type": "tool_call",
            "tool_name": "filesystem.write",
            "arguments": {
                "path": "src/clip.mjs",
                "content": "export const ready = true;",
                "action": "NODE_TEST",
                "target": ".",
            },
        }
    )
    execute = AgentRuntime._parse_turn(
        {
            "type": "tool_call",
            "tool_name": "development.execute",
            "arguments": {
                "path": "ignored.txt",
                "content": "ignored",
                "action": "NODE_TEST",
                "target": None,
            },
        }
    )

    assert isinstance(write, ToolRequestTurn)
    assert write.arguments == {
        "path": "src/clip.mjs",
        "content": "export const ready = true;",
    }
    assert isinstance(execute, ToolRequestTurn)
    assert execute.arguments == {"action": "NODE_TEST"}


def test_planner_capability_manifest_includes_configured_model_aliases() -> None:
    manifest = PlanValidator(Settings()).capability_manifest

    assert manifest["model_aliases"] == sorted(Settings().model_aliases)
