import hashlib
from uuid import uuid4

from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.completion_safety import (
    DeliverableCompletionStatus,
    DevelopmentCompletionSafety,
)
from app.tool_system.contracts import ToolErrorPayload, ToolObservation
from app.tool_system.workspace import WorkspaceManager


async def test_all_required_deliverables_with_current_evidence_are_ready(tmp_path) -> None:
    company_id, project_id = uuid4(), uuid4()
    settings = Settings(tool_workspace_root=str(tmp_path))
    workspace = WorkspaceManager(tmp_path).project_workspace(company_id, project_id)
    required = ["index.html", "styles.css", "app.js"]
    for path in required:
        (workspace / path).write_text(path, encoding="utf-8")
    snapshot = await DevelopmentCompletionSafety(settings).evaluate(
        company_id=company_id,
        project_id=project_id,
        task_input={"deliverables": required},
        acceptance_criteria=[],
        current_mutations=set(required),
        recovery_hashes={},
    )
    assert snapshot.ready
    assert snapshot.incomplete_deliverables == []
    assert all(
        item.status == DeliverableCompletionStatus.VERIFIED_CURRENT
        for item in snapshot.deliverable_states
    )


async def test_missing_or_failed_write_cannot_satisfy_completion(tmp_path) -> None:
    company_id, project_id = uuid4(), uuid4()
    settings = Settings(tool_workspace_root=str(tmp_path))
    workspace = WorkspaceManager(tmp_path).project_workspace(company_id, project_id)
    (workspace / "index.html").write_text("index", encoding="utf-8")
    (workspace / "styles.css").write_text("styles", encoding="utf-8")
    snapshot = await DevelopmentCompletionSafety(settings).evaluate(
        company_id=company_id,
        project_id=project_id,
        task_input={"deliverables": ["index.html", "styles.css", "app.js"]},
        acceptance_criteria=[],
        # A failed app.js ToolCall is deliberately absent from accepted mutation evidence.
        current_mutations={"index.html", "styles.css"},
        recovery_hashes={},
    )
    by_path = {item.path: item for item in snapshot.deliverable_states}
    assert not snapshot.ready
    assert snapshot.incomplete_deliverables == ["app.js"]
    assert by_path["app.js"].status == DeliverableCompletionStatus.MISSING


async def test_existing_file_without_evidence_and_stale_recovery_are_incomplete(tmp_path) -> None:
    company_id, project_id = uuid4(), uuid4()
    settings = Settings(tool_workspace_root=str(tmp_path))
    workspace = WorkspaceManager(tmp_path).project_workspace(company_id, project_id)
    (workspace / "foreign.js").write_text("foreign", encoding="utf-8")
    (workspace / "recovered.js").write_text("changed", encoding="utf-8")
    snapshot = await DevelopmentCompletionSafety(settings).evaluate(
        company_id=company_id,
        project_id=project_id,
        task_input={"deliverables": ["foreign.js", "recovered.js"]},
        acceptance_criteria=[],
        current_mutations=set(),
        recovery_hashes={
            "recovered.js": hashlib.sha256(b"checkpoint-content").hexdigest()
        },
    )
    by_path = {item.path: item for item in snapshot.deliverable_states}
    assert by_path["foreign.js"].status == DeliverableCompletionStatus.UNSUPPORTED
    assert by_path["recovered.js"].status == DeliverableCompletionStatus.INVALIDATED
    assert not snapshot.ready


def test_failed_or_foreign_write_observation_is_not_current_mutation_evidence() -> None:
    task_id = uuid4()
    observations = [
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.write",
            status="success",
            result={"path": "index.html"},
            task_id=task_id,
        ),
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.write",
            status="error",
            error=ToolErrorPayload(code="TOOL_EXECUTION_FAILED", message="write failed"),
            task_id=task_id,
        ),
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.write",
            status="success",
            result={"path": "app.js"},
            task_id=uuid4(),
        ),
    ]

    assert AgentRuntime._current_mutation_paths(task_id, observations) == {"index.html"}
