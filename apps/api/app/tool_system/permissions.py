from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Agent
from app.repositories.agent import AgentRepository
from app.tool_system.contracts import ToolDefinition


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    code: str
    message: str
    agent_role: str | None = None


class PermissionEngine:
    def __init__(self, session: AsyncSession) -> None:
        self.agents = AgentRepository(session)

    async def check(
        self,
        *,
        agent_id: UUID,
        company_id: UUID,
        definition: ToolDefinition,
        has_project_workspace: bool,
    ) -> PermissionDecision:
        if not definition.enabled:
            return PermissionDecision(
                False, "TOOL_DISABLED", f"{definition.name} is disabled", None
            )
        agent = await self.agents.get_current(agent_id)
        if agent is None or agent.company_id != company_id:
            return PermissionDecision(
                False, "TOOL_PERMISSION_DENIED", "Agent scope is invalid", None
            )
        role = agent.role.strip().upper()
        if not has_project_workspace:
            return PermissionDecision(
                False,
                "TOOL_PERMISSION_DENIED",
                "Filesystem tools require a project workspace",
                role,
            )
        if agent.permissions.get(definition.permission_required) is not True:
            return PermissionDecision(
                False,
                "TOOL_PERMISSION_DENIED",
                f"Agent is not allowed to use {definition.name}",
                role,
            )
        return PermissionDecision(True, "TOOL_AUTHORIZED", "Tool call is authorized", role)

    async def list_allowed(
        self,
        *,
        agent: Agent,
        definitions: list[ToolDefinition],
        has_project_workspace: bool,
    ) -> list[ToolDefinition]:
        allowed: list[ToolDefinition] = []
        for definition in definitions:
            decision = await self.check(
                agent_id=agent.id,
                company_id=agent.company_id,
                definition=definition,
                has_project_workspace=has_project_workspace,
            )
            if decision.allowed:
                allowed.append(definition)
        return allowed
