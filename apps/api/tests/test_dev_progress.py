from uuid import uuid4

import pytest

from app.agent_runtime.context_rollover import ContextRollover
from app.agent_runtime.progress import DevelopmentProgress
from app.development.tools import development_definitions
from app.infrastructure.database import get_session_factory
from app.tool_system.contracts import ToolObservation, ToolRequestTurn
from app.tool_system.errors import ToolSystemError
from app.tool_system.executor import ToolExecutor


def turn(tool: str, **arguments):
    return ToolRequestTurn(type="tool_call", tool_name=tool, arguments=arguments)


def observation(tool: str, result: dict):
    return ToolObservation(tool_call_id=uuid4(), tool=tool, status="success", result=result)


def test_unchanged_read_is_reused_compactly_and_mutation_invalidates_it():
    progress = DevelopmentProgress()
    read = turn("filesystem.read", path="index.html")
    raw = observation(
        "filesystem.read",
        {"path": "index.html", "content": "hello\nworld", "byte_size": 11},
    )
    progress.record(read, raw)

    reused = progress.reusable(read)
    assert reused is not None
    assert reused.result["observation_reused"] is True
    assert reused.result["content_sha256"]
    assert "content" not in reused.result

    write = turn("filesystem.write", path="index.html", content="changed")
    invalidated = progress.record(
        write,
        observation("filesystem.write", {"path": "index.html", "byte_size": 7}),
    )
    assert invalidated == 1
    assert progress.reusable(read) is None


def test_alternating_unchanged_inspection_accumulates_stagnation_signals():
    progress = DevelopmentProgress()
    listed = turn("filesystem.list", path=".")
    read = turn("filesystem.read", path="a.txt")
    progress.record(listed, observation("filesystem.list", {"path": ".", "entries": []}))
    progress.record(
        read,
        observation("filesystem.read", {"path": "a.txt", "content": "a", "byte_size": 1}),
    )
    assert progress.reusable(listed) is not None
    assert progress.reusable(read) is not None
    assert progress.reusable(listed) is not None
    assert progress.stagnation_signals == 3


def test_rollover_snapshot_is_action_oriented_and_omits_raw_file_content():
    state = ContextRollover(0.7, 4)
    observations = [
        observation("filesystem.write", {"path": "index.html", "byte_size": 10_000}),
        observation(
            "filesystem.read",
            {"path": "index.html", "content": "secret-large-content", "byte_size": 20},
        ),
        observation(
            "git.status",
            {"action": "GIT_STATUS", "status": "SUCCEEDED", "stdout_excerpt": "?? index.html"},
        ),
        observation(
            "git.diff",
            {"action": "GIT_DIFF", "status": "SUCCEEDED", "stdout_excerpt": ""},
        ),
    ]
    snapshot = state.checkpoint(
        task_id="task",
        goal="Build page",
        observations=observations,
        tool_steps=4,
        duplicate_signals=0,
        file_states=[
            {
                "path": "index.html",
                "content_hash": "abc",
                "safe_summary": "index.html: 20 bytes",
            }
        ],
        remaining_budget={"input_tokens": 10_000},
    )
    assert snapshot["file_states"][0]["content_hash"] == "abc"
    assert "Return the final structured result" in snapshot["next_action"]
    assert "secret-large-content" not in str(snapshot)
    assert snapshot["git_state"]["git.status"]["output_sha256"]


def test_rollover_snapshot_does_not_reopen_a_repaired_schema_failure():
    state = ContextRollover(0.7, 4)
    invalid = ToolObservation(
        tool_call_id=uuid4(),
        tool="development.execute",
        status="error",
        error={
            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
            "message": "invalid action",
            "details": {"suggested_tool": "git.status"},
        },
    )
    snapshot = state.checkpoint(
        task_id="task",
        goal="Build page",
        observations=[
            invalid,
            observation(
                "git.status",
                {"action": "GIT_STATUS", "status": "SUCCEEDED", "stdout_excerpt": ""},
            ),
            observation(
                "git.diff",
                {"action": "GIT_DIFF", "status": "SUCCEEDED", "stdout_excerpt": ""},
            ),
        ],
        tool_steps=3,
        duplicate_signals=0,
    )
    assert snapshot["failures"] == []
    assert "Return the final structured result" in snapshot["next_action"]


async def test_malformed_development_execute_has_targeted_repair_schema():
    async with get_session_factory()() as session:
        definition = next(
            item for item in development_definitions(session) if item.name == "development.execute"
        )
        with pytest.raises(ToolSystemError) as raised:
            ToolExecutor.validate_arguments(definition, {"action": "GIT_STATUS"})
        details = raised.value.details
        assert details["tool_name"] == "development.execute"
        assert details["suggested_tool"] == "git.status"
        assert details["suggested_arguments"] == {}
        assert details["invalid_call_fingerprint"]
        assert "action" in details["invalid_fields"]
