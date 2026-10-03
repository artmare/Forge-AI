"""Capture-gated, advisory visual QA with no mutation tools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.contracts import ModelRequest, ProviderCallError
from app.agent_runtime.dev_models import refresh_profiles
from app.agent_runtime.economics import ModelEconomicsService
from app.agent_runtime.policy import ModelExecutionPolicy
from app.agent_runtime.providers import provider_for_profile
from app.agent_runtime.registry import ModelRegistry
from app.agent_runtime.routing import ModelRouter, RoutingContext, required_capabilities
from app.core.config import Settings
from app.development.verification_contracts import (
    VisualQACaptureReference,
    VisualQAEvidence,
    VisualQAFinding,
    VisualQAResult,
)
from app.domain.enums import ToolCallStatus
from app.domain.models import Event, Task, ToolCall
from app.services.event_factory import EventFactory
from app.tool_system.errors import ToolSystemError

_RUBRIC = (
    "Review only the supplied authoritative captures. Evaluate layout integrity, responsive "
    "behavior, hierarchy, spacing consistency, typography, contrast/readability, overflow or "
    "clipping, broken visible elements, interaction affordances, unfinished/default appearance, "
    "and consistency with the stated criteria. Return actionable bounded findings. Do not claim "
    "execution or fixes. Do not assign a beauty score."
)


class VisualQAService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def review(
        self,
        task_id: UUID,
        task_run_id: UUID,
        *,
        source_generation: str,
        criterion_indices: list[int],
    ) -> VisualQAEvidence:
        task = await self.session.get(Task, task_id)
        if task is None or task.project_id is None:
            raise ToolSystemError("VISUAL_QA_SCOPE", "Visual QA task scope is invalid")
        captures = await self._captures(task_id, task_run_id)
        if not captures:
            raise ToolSystemError(
                "VISUAL_QA_CAPTURE_REQUIRED",
                "Visual QA requires authoritative successful browser capture evidence",
            )
        profiles = self._free_profiles(ModelRegistry.from_settings(self.settings).profiles)
        if self.settings.openrouter_discovery_enabled and any(
            profile.provider == "openrouter" for profile in profiles
        ):
            profiles = await refresh_profiles(
                profiles,
                base_url=self.settings.openrouter_base_url,
                ttl=self.settings.openrouter_catalog_ttl_seconds,
                free_only=True,
            )
            profiles = self._free_profiles(profiles)
        capabilities = required_capabilities(role="CREATIVE_DIRECTOR", has_tools=False, vision=True)
        economics = ModelEconomicsService(self.session, self.settings)
        selection = ModelRouter(profiles).select(
            RoutingContext(
                capabilities,
                paid_budget_remaining=0,
                provider_health=await economics.provider_health(profiles),
            )
        )
        provider = provider_for_profile(self.settings, selection.profile)
        ModelExecutionPolicy(self.settings).authorize(provider)
        call = await economics.begin_call(
            selection,
            capabilities=frozenset(capability.value for capability in capabilities),
            agent_role="CREATIVE_DIRECTOR",
            task=task,
            agent_run_id=None,
        )
        criteria = [
            {"index": index, "criterion": str(task.acceptance_criteria[index])[:500]}
            for index in criterion_indices[:50]
            if 0 <= index < len(task.acceptance_criteria)
        ]
        payload = {
            "viewports": [
                {"width": capture.width, "height": capture.height} for capture in captures
            ],
            "criteria": criteria,
            "rubric": _RUBRIC,
        }
        image_data_urls = self._image_data_urls(captures)
        try:
            async with asyncio.timeout(self.settings.model_request_timeout_seconds):
                response = await provider.generate(
                    ModelRequest(
                        model=selection.profile.model_id,
                        system_prompt=(
                            "You are Forge Visual QA. You have no tools and cannot mutate files. "
                            "Return only the structured visual judgment contract."
                        ),
                        user_prompt=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        response_model=VisualQAResult,
                        image_data_urls=image_data_urls,
                    )
                )
            if response.tool_call is not None:
                raise ValueError("Visual QA requested a tool")
            judgment = VisualQAResult.model_validate(response.output)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, ProviderCallError)
                else ProviderCallError(
                    "INVALID_MODEL_OUTPUT", "Visual QA response failed validation"
                )
            )
            await economics.complete_call(call.id, error=error)
            raise ToolSystemError(error.code, "Visual QA call failed") from None
        await economics.complete_call(call.id, response=response)
        prior = await self.session.scalar(
            select(Event)
            .where(Event.task_id == task.id, Event.type == "DEV_VISUAL_QA_RECORDED")
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        evidence = VisualQAEvidence(
            task_id=task.id,
            task_run_id=task_run_id,
            source_generation=source_generation,
            captures=captures,
            reviewer_provider=selection.profile.provider,
            reviewer_model=selection.profile.model_id,
            decision=judgment.decision,
            findings=judgment.findings,
            criteria_addressed=criterion_indices[:50],
            summary=judgment.summary,
            recorded_at=datetime.now(UTC),
            supersedes_event_id=prior.id if prior else None,
        )
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_VISUAL_QA_RECORDED",
            message=f"Visual QA returned {evidence.decision.value}.",
            payload={"evidence": evidence.model_dump(mode="json")},
        )
        await self.session.commit()
        return evidence

    async def _captures(self, task_id: UUID, task_run_id: UUID) -> list[VisualQACaptureReference]:
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_id == task_id,
                    ToolCall.task_run_id == task_run_id,
                    ToolCall.tool_name == "browser.capture",
                    ToolCall.status == ToolCallStatus.SUCCEEDED,
                )
                .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
                .limit(8)
            )
        )
        output: list[VisualQACaptureReference] = []
        seen: set[tuple[int, int]] = set()
        for call in calls:
            result = call.result if isinstance(call.result, dict) else {}
            viewport = result.get("viewport") if isinstance(result.get("viewport"), dict) else {}
            try:
                item = VisualQACaptureReference(
                    tool_call_id=call.id,
                    artifact=result["artifact"],
                    sha256=result["sha256"],
                    width=int(viewport["width"]),
                    height=int(viewport["height"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            key = (item.width, item.height)
            if key not in seen:
                output.append(item)
                seen.add(key)
        return list(reversed(output[:4]))

    def _image_data_urls(self, captures: list[VisualQACaptureReference]) -> tuple[str, ...]:
        artifact_root = Path(self.settings.development_runner_queue_root) / "browser" / "artifacts"
        output: list[str] = []
        for capture in captures:
            name = Path(capture.artifact).name
            candidate = artifact_root / name
            if candidate.is_symlink() or not candidate.is_file():
                raise ToolSystemError(
                    "VISUAL_QA_CAPTURE_MISSING", "Browser capture artifact is unavailable"
                )
            data = candidate.read_bytes()
            if len(data) > 10_000_000:
                raise ToolSystemError(
                    "VISUAL_QA_CAPTURE_INVALID", "Browser capture exceeds visual QA limit"
                )
            if hashlib.sha256(data).hexdigest() != capture.sha256:
                raise ToolSystemError(
                    "VISUAL_QA_CAPTURE_STALE",
                    "Browser capture no longer matches its authoritative evidence",
                )
            output.append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
        return tuple(output)

    @staticmethod
    def _free_profiles(profiles):
        """Visual judgment can only use explicitly free, known-price profiles."""
        return tuple(
            profile for profile in profiles if not profile.paid and profile.tier.value == "FREE"
        )


def visual_finding_is_blocking(finding: VisualQAFinding) -> bool:
    return finding.severity.value in {"BLOCKING", "MAJOR"}
