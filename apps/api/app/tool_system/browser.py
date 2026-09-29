"""Opt-in browser evidence through a separate network-isolated screenshot worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.domain.enums import ToolRiskLevel
from app.tool_system.contracts import ToolDefinition, ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="index.html", max_length=512)
    width: int = Field(default=1280, ge=320, le=1920)
    height: int = Field(default=900, ge=240, le=1440)


class BrowserEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    artifact: str
    sha256: str
    rendered: bool
    title: str
    console_errors: list[str]
    viewport: dict[str, int]


class BrowserTools:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def capture(
        self, arguments: BrowserInput, context: ToolExecutionContext
    ) -> BrowserEvidence:
        if context.project_id is None:
            raise ToolSystemError("WORKSPACE_NOT_AVAILABLE", "Browser requires project workspace")
        _, path = WorkspaceManager(self.settings.tool_workspace_root).resolve(
            context.company_id, context.project_id, arguments.path, must_exist=True
        )
        if path.suffix.lower() not in {".html", ".htm"}:
            raise ToolSystemError(
                "BROWSER_TARGET_INVALID", "Browser target must be built static HTML"
            )
        identifier = str(uuid4())
        queue = Path(self.settings.development_runner_queue_root) / "browser"
        for directory in ("requests", "responses", "artifacts"):
            (queue / directory).mkdir(parents=True, exist_ok=True)
        request = {
            "id": identifier,
            "workspace": f"{context.company_id}/{context.project_id}",
            **arguments.model_dump(),
        }
        temporary = queue / "requests" / f"{identifier}.tmp"
        temporary.write_text(json.dumps(request))
        os.replace(temporary, temporary.with_suffix(".json"))
        response = queue / "responses" / f"{identifier}.json"
        async with asyncio.timeout(60):
            while not response.exists():  # noqa: ASYNC110 - cross-process file queue
                await asyncio.sleep(0.1)
        payload = json.loads(response.read_text())
        response.unlink()
        artifact = queue / "artifacts" / f"{identifier}.png"
        if payload.get("status") != "success" or artifact.is_symlink() or not artifact.is_file():
            raise ToolSystemError("BROWSER_CAPTURE_FAILED", "Isolated browser capture failed")
        if not 8 < artifact.stat().st_size <= 10_000_000:
            raise ToolSystemError("BROWSER_CAPTURE_INVALID", "Screenshot size is invalid")
        data = artifact.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ToolSystemError("BROWSER_CAPTURE_INVALID", "Screenshot is not PNG")
        return BrowserEvidence(
            path=arguments.path,
            artifact=f"browser/{identifier}.png",
            sha256=hashlib.sha256(data).hexdigest(),
            rendered=True,
            title=str(payload.get("title", ""))[:500],
            console_errors=[str(x)[:500] for x in payload.get("console_errors", [])[:20]],
            viewport={"width": arguments.width, "height": arguments.height},
        )


def browser_definition(settings: Settings) -> ToolDefinition:
    return ToolDefinition(
        "browser.capture",
        "Render built static HTML in an isolated browser; "
        "record screenshot evidence. Does not prove visual correctness.",
        BrowserInput,
        BrowserEvidence,
        ToolRiskLevel.MEDIUM,
        "browser.capture",
        65,
        settings.forge_browser_enabled,
        BrowserTools(settings).capture,
    )
