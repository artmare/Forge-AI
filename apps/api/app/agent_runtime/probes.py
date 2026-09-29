"""Behavioral checks using synthetic observations only; never dispatch machine tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent_runtime.contracts import (
    ModelProvider,
    ModelRequest,
    ModelTool,
    ModelToolExchange,
    ProviderCallError,
)
from app.tool_system.contracts import AgentTurnResponse, FinalTurn


class ProbeCapability(StrEnum):
    TOOL_CALLING = "TOOL_CALLING"
    TOOL_CONTINUATION = "TOOL_CONTINUATION"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["FORGE_PROBE_OK"]


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    model: str
    capability: ProbeCapability
    status: str
    timestamp: datetime
    retry_at: datetime
    latency_ms: int
    failure_category: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class CapabilityProber:
    def __init__(
        self, *, ttl_seconds: int = 3600, retry_seconds: int = 300, timeout_seconds: float = 30
    ) -> None:
        self.ttl = timedelta(seconds=max(1, ttl_seconds))
        self.retry = timedelta(seconds=max(1, retry_seconds))
        self.timeout = timeout_seconds
        self.results: dict[tuple[str, str, ProbeCapability], ProbeResult] = {}
        self._lock = asyncio.Lock()

    async def probe(
        self,
        provider: ModelProvider,
        model: str,
        capability: ProbeCapability,
        *,
        force: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> ProbeResult:
        # Single flight also bounds simultaneous external probe traffic per worker.
        async with self._lock:
            key = (provider.name, model, capability)
            now = datetime.now(UTC)
            cached = self.results.get(key)
            if not force and cached and now < cached.retry_at:
                return cached
            started = perf_counter()
            status, failure = "verified", None
            input_tokens = output_tokens = 0
            tool = ModelTool("forge_probe.echo", "Synthetic echo; no machine execution", EchoInput)
            request = ModelRequest(
                model=model,
                system_prompt="Follow the probe protocol exactly.",
                user_prompt=(
                    "Return a final completed Forge turn with summary FORGE_PROBE_OK and "
                    'output {"artifacts":[],"details":[],"execution_claims":[]}.'
                    if capability == ProbeCapability.STRUCTURED_OUTPUT
                    else 'Call forge_probe.echo exactly once with {"value":"FORGE_PROBE_OK"}. '
                    "After its observation return a final completed Forge turn with summary "
                    "FORGE_PROBE_OK and output "
                    '{"artifacts":[],"details":[],"execution_claims":[]}.'
                ),
                tools=() if capability == ProbeCapability.STRUCTURED_OUTPUT else (tool,),
                response_model=AgentTurnResponse,
                metadata=metadata or {},
            )
            try:
                async with asyncio.timeout(self.timeout):
                    response = await provider.generate(request)
                    input_tokens += response.usage.input_tokens
                    output_tokens += response.usage.output_tokens
                    if capability != ProbeCapability.STRUCTURED_OUTPUT:
                        call = response.tool_call
                        if call is None or call.name != tool.name or not call.call_id:
                            raise ValueError("native tool call missing")
                        EchoInput.model_validate(call.arguments)
                        if capability == ProbeCapability.TOOL_CONTINUATION:
                            response = await provider.generate(
                                ModelRequest(
                                    model=model,
                                    system_prompt=request.system_prompt,
                                    user_prompt=request.user_prompt,
                                    conversation_start_prompt=request.user_prompt,
                                    tools=(tool,),
                                    response_model=AgentTurnResponse,
                                    metadata=request.metadata,
                                    tool_exchanges=(
                                        ModelToolExchange(
                                            call,
                                            {
                                                "status": "success",
                                                "result": {"value": "FORGE_PROBE_OK"},
                                            },
                                        ),
                                    ),
                                )
                            )
                            input_tokens += response.usage.input_tokens
                            output_tokens += response.usage.output_tokens
                    if capability != ProbeCapability.TOOL_CALLING:
                        turn = AgentTurnResponse.model_validate(response.output).turn
                        if (
                            response.tool_call is not None
                            or not isinstance(turn, FinalTurn)
                            or turn.result.summary != "FORGE_PROBE_OK"
                        ):
                            raise ValueError("invalid final continuation")
            except ProviderCallError as exc:
                failure = exc.code
                status = (
                    "rate_limited"
                    if exc.http_status == 429 or exc.code == "MODEL_RATE_LIMIT"
                    else "temporarily_unavailable"
                    if exc.retryable
                    else "protocol_failure"
                )
            except TimeoutError:
                status, failure = "temporarily_unavailable", "TIMEOUT"
            except (ValueError, ValidationError):
                status, failure = "protocol_failure", "INVALID_PROBE_RESPONSE"
            result = ProbeResult(
                provider.name,
                model,
                capability,
                status,
                now,
                now + (self.ttl if status == "verified" else self.retry),
                int((perf_counter() - started) * 1000),
                failure,
                input_tokens,
                output_tokens,
            )
            if len(self.results) >= 1024:
                self.results.pop(next(iter(self.results)))
            self.results[key] = result
            return result
