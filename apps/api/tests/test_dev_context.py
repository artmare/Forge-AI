from uuid import uuid4

import pytest

from app.agent_runtime.builders import ContextRecord, InstructionBuilder
from app.agent_runtime.context_rollover import ContextRollover, mutation_key
from app.agent_runtime.contracts import ModelRequest, ModelToolCall, ModelToolExchange
from app.tool_system.contracts import ToolErrorPayload, ToolObservation


def test_rollover_keeps_failures_bounds_and_files():
    state = ContextRollover(0.7, 2)
    observations = [
        ToolObservation(
            tool_call_id=uuid4(), tool="filesystem.write", status="success", result={"path": "a.py"}
        ),
        ToolObservation(
            tool_call_id=uuid4(),
            tool="development.execute",
            status="success",
            result={"action": "PYTHON_TEST", "status": "FAILED"},
        ),
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.write",
            status="error",
            error=ToolErrorPayload(code="DENIED", message="Permission denied"),
        ),
    ]
    key = mutation_key("filesystem.write", {"path": "a.py", "content": "x"})
    state.executed_mutations.add(key)
    handoff = state.checkpoint(
        task_id="same", goal="Fix", observations=observations, tool_steps=3, duplicate_signals=1
    )
    assert handoff["task_id"] == "same"
    assert handoff["tool_steps"] == 3 and handoff["duplicate_signals"] == 1
    assert handoff["files_modified"] == ["a.py"]
    assert len(handoff["failures"]) == 2
    assert key in state.executed_mutations
    state.checkpoint(
        task_id="same", goal="Fix", observations=observations, tool_steps=4, duplicate_signals=1
    )
    with pytest.raises(ValueError, match="limit"):
        state.checkpoint(
            task_id="same", goal="Fix", observations=observations, tool_steps=5, duplicate_signals=1
        )


def test_native_exchanges_count_toward_rollover_threshold():
    state = ContextRollover(0.7, 2)
    request = ModelRequest(model="test", system_prompt="s", user_prompt="u")
    assert not state.needed(request, 1024)
    request = ModelRequest(
        model="test",
        system_prompt="s",
        user_prompt="u",
        tool_exchanges=(
            ModelToolExchange(
                ModelToolCall("filesystem.read", {"path": "a"}), {"content": "x" * 2400}
            ),
        ),
    )
    assert state.needed(request, 1024)


def test_continuation_estimate_matches_provider_transport_and_reserves_next_turn():
    state = ContextRollover(0.7, 2)
    request = ModelRequest(
        model="test",
        system_prompt="system",
        user_prompt="unused-current-delta" * 100,
        conversation_start_prompt="start",
        tool_exchanges=(
            ModelToolExchange(ModelToolCall("filesystem.read", {"path": "a"}), {"ok": True}),
        ),
    )
    without_reserve = state.estimated_input_tokens(request)
    assert without_reserve < 100
    assert state.estimated_input_tokens(request, reserve_tokens=512) == without_reserve + 512


def test_prompt_bounds_tool_output_and_deduplicates_unchanged_file_reads():
    content = "x" * 20_000
    observations = [
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.read",
            status="success",
            result={"path": "index.html", "content": content},
        ),
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.read",
            status="success",
            result={"path": "index.html", "content": content},
        ),
    ]
    context = ContextRecord(
        company={"id": str(uuid4()), "name": "Forge", "goal": "Ship"},
        project={"id": str(uuid4()), "name": "Site", "goal": "Launch"},
        task={"id": str(uuid4()), "title": "Build", "input": {}, "acceptance_criteria": []},
        agent={"id": str(uuid4()), "name": "Developer", "role": "DEVELOPER"},
    )
    prompt = InstructionBuilder().build(
        context, observations=observations, turn_number=3, recent_observation_limit=5
    ).user_prompt
    assert prompt.count("unchanged_duplicate") == 1
    assert "content_sha256" in prompt
    assert "raw evidence is durable" in prompt
    assert len(prompt) < 16_000
