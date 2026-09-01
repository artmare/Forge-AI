from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings
from app.development.contracts import RunnerRequest, RunnerResponse
from app.domain.enums import DevelopmentExecutionStatus


class RunnerClient(Protocol):
    async def execute(self, request: RunnerRequest) -> RunnerResponse: ...


class QueueRunnerClient:
    """File-queue bridge to a network-isolated runner container."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = Path(self.settings.development_runner_queue_root)

    async def execute(self, request: RunnerRequest) -> RunnerResponse:
        channel = "install" if request.action.value.endswith("INSTALL") else "offline"
        requests = self.root / channel / "requests"
        responses = self.root / channel / "responses"
        cancellations = self.root / channel / "cancellations"
        requests.mkdir(parents=True, exist_ok=True)
        responses.mkdir(parents=True, exist_ok=True)
        cancellations.mkdir(parents=True, exist_ok=True)
        request_path = requests / f"{request.request_id}.json"
        response_path = responses / f"{request.request_id}.json"
        temporary = request_path.with_suffix(".tmp")
        temporary.write_text(request.model_dump_json(), encoding="utf-8")
        os.replace(temporary, request_path)
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds + 15
        try:
            while asyncio.get_running_loop().time() < deadline:
                if response_path.exists():
                    payload = json.loads(response_path.read_text(encoding="utf-8"))
                    response_path.unlink(missing_ok=True)
                    return RunnerResponse.model_validate(payload)
                await asyncio.sleep(self.settings.development_runner_poll_interval_ms / 1000)
        except asyncio.CancelledError:
            marker = cancellations / f"{request.request_id}.cancel"
            temporary_marker = marker.with_suffix(".tmp")
            temporary_marker.write_text("cancel", encoding="utf-8")
            os.replace(temporary_marker, marker)
            raise
        return RunnerResponse(
            request_id=request.request_id,
            status=DevelopmentExecutionStatus.TIMED_OUT,
            error_code="DEVELOPMENT_RUNNER_UNAVAILABLE",
            error_message="The isolated development runner did not return a durable result.",
            duration_ms=request.timeout_seconds * 1000,
        )


class FakeRunnerClient:
    """Deterministic test adapter; production never selects it implicitly."""

    def __init__(self, responses: list[RunnerResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[RunnerRequest] = []

    async def execute(self, request: RunnerRequest) -> RunnerResponse:
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("No fake development runner response remains")
        response = self.responses.pop(0)
        return response.model_copy(update={"request_id": request.request_id})


def runner_client_from_settings(settings: Settings | None = None) -> RunnerClient:
    resolved = settings or get_settings()
    if resolved.development_runner_mode == "queue":
        return QueueRunnerClient(resolved)
    raise RuntimeError("Only the explicit queue runner is available outside injected tests")
