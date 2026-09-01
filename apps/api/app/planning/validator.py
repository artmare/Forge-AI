from collections import Counter, deque

from app.agent_runtime.registry import ModelRegistry
from app.core.config import Settings, get_settings
from app.domain.enums import ToolPermission
from app.domain.exceptions import ModelAliasNotFoundError
from app.planning.contracts import (
    PlanProposal,
    PlanValidationError,
    PlanValidationResult,
)
from app.tool_system.registry import ToolRegistry


class PlanValidator:
    SUPPORTED_ROLES = frozenset({"GENERAL", "RESEARCHER", "DEVELOPER", "QA"})

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model_registry: ModelRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.models = model_registry or ModelRegistry.from_settings(self.settings)
        self.tools = tool_registry or ToolRegistry.from_settings(self.settings)

    @property
    def capability_manifest(self) -> dict[str, list[str]]:
        definitions = self.tools.list(enabled_only=True)
        return {
            "roles": sorted(self.SUPPORTED_ROLES),
            "tools": [definition.name for definition in definitions],
            "permissions": sorted(
                permission.value
                for permission in ToolPermission
                if permission != ToolPermission.SHELL_RUN
            ),
            "model_aliases": sorted(self.settings.model_aliases),
        }

    def validate(self, proposal: PlanProposal) -> PlanValidationResult:
        errors: list[PlanValidationError] = []
        self._validate_limits(proposal, errors)
        agent_keys = [agent.key for agent in proposal.agents]
        task_keys = [task.key for task in proposal.tasks]
        self._duplicates(agent_keys, "agent", errors)
        self._duplicates(task_keys, "task", errors)
        agents = set(agent_keys)
        tasks = set(task_keys)
        allowed_permissions = set(self.capability_manifest["permissions"])

        for index, agent in enumerate(proposal.agents):
            role = agent.role.strip().upper()
            if role not in self.SUPPORTED_ROLES:
                errors.append(
                    PlanValidationError(
                        code="UNSUPPORTED_AGENT_ROLE",
                        message=f"Agent role '{agent.role}' is not supported.",
                        path=f"agents.{index}.role",
                    )
                )
            try:
                self.models.resolve(agent.model_alias)
            except ModelAliasNotFoundError:
                errors.append(
                    PlanValidationError(
                        code="MODEL_ALIAS_NOT_FOUND",
                        message=f"Model alias '{agent.model_alias}' is not configured.",
                        path=f"agents.{index}.model_alias",
                    )
                )
            for permission, requested in agent.requested_permissions.items():
                if permission not in allowed_permissions:
                    errors.append(
                        PlanValidationError(
                            code="UNSUPPORTED_PERMISSION",
                            message=f"Permission '{permission}' is unavailable or disabled.",
                            path=f"agents.{index}.requested_permissions.{permission}",
                        )
                    )
                elif not isinstance(requested, bool):
                    errors.append(
                        PlanValidationError(
                            code="INVALID_PLAN",
                            message="Permission values must be booleans.",
                            path=f"agents.{index}.requested_permissions.{permission}",
                        )
                    )

        for index, task in enumerate(proposal.tasks):
            if task.assigned_agent_key not in agents:
                errors.append(
                    PlanValidationError(
                        code="INVALID_AGENT_REFERENCE",
                        message=(
                            f"Task '{task.key}' references unknown Agent "
                            f"'{task.assigned_agent_key}'."
                        ),
                        path=f"tasks.{index}.assigned_agent_key",
                    )
                )
            if not all(item.strip() for item in task.acceptance_criteria):
                errors.append(
                    PlanValidationError(
                        code="INVALID_PLAN",
                        message="Acceptance criteria cannot contain empty entries.",
                        path=f"tasks.{index}.acceptance_criteria",
                    )
                )

        edge_pairs: list[tuple[str, str]] = []
        for index, dependency in enumerate(proposal.dependencies):
            if dependency.task not in tasks or dependency.depends_on not in tasks:
                errors.append(
                    PlanValidationError(
                        code="INVALID_TASK_REFERENCE",
                        message="Dependency references an unknown Task key.",
                        path=f"dependencies.{index}",
                    )
                )
                continue
            if dependency.task == dependency.depends_on:
                errors.append(
                    PlanValidationError(
                        code="INVALID_PLAN",
                        message="A Task cannot depend on itself.",
                        path=f"dependencies.{index}",
                    )
                )
            edge_pairs.append((dependency.task, dependency.depends_on))

        duplicate_edges = [edge for edge, count in Counter(edge_pairs).items() if count > 1]
        for task_key, dependency_key in sorted(duplicate_edges):
            errors.append(
                PlanValidationError(
                    code="INVALID_PLAN",
                    message=f"Dependency '{task_key}' -> '{dependency_key}' is duplicated.",
                    path="dependencies",
                )
            )
        if self._has_cycle(tasks, set(edge_pairs)):
            errors.append(
                PlanValidationError(
                    code="PLAN_CYCLE_DETECTED",
                    message="Task dependencies must form a directed acyclic graph.",
                    path="dependencies",
                )
            )

        return PlanValidationResult(
            valid=not errors,
            errors=errors,
            agent_count=len(proposal.agents),
            task_count=len(proposal.tasks),
            dependency_count=len(proposal.dependencies),
        )

    def _validate_limits(self, proposal: PlanProposal, errors: list[PlanValidationError]) -> None:
        limits = (
            ("agents", len(proposal.agents), self.settings.mission_max_agents),
            ("tasks", len(proposal.tasks), self.settings.mission_max_tasks),
            (
                "dependencies",
                len(proposal.dependencies),
                self.settings.mission_max_dependencies,
            ),
        )
        for name, actual, maximum in limits:
            if actual > maximum:
                errors.append(
                    PlanValidationError(
                        code="PLAN_LIMIT_EXCEEDED",
                        message=f"Plan has {actual} {name}; maximum is {maximum}.",
                        path=name,
                    )
                )

    @staticmethod
    def _duplicates(values: list[str], kind: str, errors: list[PlanValidationError]) -> None:
        for value, count in sorted(Counter(values).items()):
            if count > 1:
                errors.append(
                    PlanValidationError(
                        code="INVALID_PLAN",
                        message=f"Duplicate {kind} key '{value}'.",
                        path=f"{kind}s",
                    )
                )

    @staticmethod
    def _has_cycle(nodes: set[str], edges: set[tuple[str, str]]) -> bool:
        indegree = dict.fromkeys(nodes, 0)
        outgoing: dict[str, set[str]] = {node: set() for node in nodes}
        for task, depends_on in edges:
            outgoing[depends_on].add(task)
            indegree[task] += 1
        ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
        visited = 0
        while ready:
            node = ready.popleft()
            visited += 1
            for dependent in sorted(outgoing[node]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        return visited != len(nodes)
