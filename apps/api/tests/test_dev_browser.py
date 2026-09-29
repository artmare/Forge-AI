import asyncio
import json
from pathlib import Path
from uuid import uuid4

from app.agent_runtime.execution_truth import ExecutionTruthValidator
from app.core.config import Settings
from app.tool_system.browser import BrowserInput, BrowserTools
from app.tool_system.contracts import ToolExecutionContext, ToolObservation
from app.tool_system.workspace import WorkspaceManager


async def test_browser_artifact_becomes_execution_evidence(tmp_path: Path):
    settings = Settings(
        tool_workspace_root=str(tmp_path / "workspaces"),
        development_runner_queue_root=str(tmp_path / "queue"),
    )
    context = ToolExecutionContext(*[uuid4() for _ in range(6)])
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        context.company_id, context.project_id
    )
    (workspace / "index.html").write_text("<title>Test</title>")
    task = asyncio.create_task(BrowserTools(settings).capture(BrowserInput(), context))
    requests = tmp_path / "queue/browser/requests"
    for _ in range(100):
        paths = list(requests.glob("*.json"))
        if paths:
            break
        await asyncio.sleep(0.01)
    identifier = paths[0].stem
    (tmp_path / f"queue/browser/artifacts/{identifier}.png").write_bytes(b"\x89PNG\r\n\x1a\nTEST")
    (tmp_path / f"queue/browser/responses/{identifier}.json").write_text(
        json.dumps({"status": "success", "title": "Test", "console_errors": []})
    )
    result = await task
    evidence = ExecutionTruthValidator.collect(
        [
            ToolObservation(
                tool_call_id=uuid4(),
                tool="browser.capture",
                status="success",
                result=result.model_dump(),
            )
        ]
    )
    assert evidence[0].kind == "BROWSER"
    assert evidence[0].reference == result.artifact
    assert not ExecutionTruthValidator.collect(
        [
            ToolObservation(
                tool_call_id=uuid4(),
                tool="browser.capture",
                status="success",
                result={"looks_good": True},
            )
        ]
    )
