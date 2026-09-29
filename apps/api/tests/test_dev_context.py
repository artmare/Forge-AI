from uuid import uuid4

import pytest

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
                ModelToolCall("filesystem.read", {"path": "a"}), {"content": "x" * 800}
            ),
        ),
    )
    assert state.needed(request, 1024)
