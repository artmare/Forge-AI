from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from app.agent_runtime.registry import ModelRegistry
from app.core.config import Settings, get_settings
from app.domain.enums import ToolPermission
from app.domain.exceptions import ModelAliasNotFoundError
from app.planning.acceptance import analyze_acceptance_criterion
from app.planning.contracts import (
    PlanProposal,
    PlanValidationError,
    PlanValidationResult,
    ProposedAgent,
    ProposedTask,
)
from app.tool_system.registry import ToolRegistry


@dataclass(frozen=True)
class PlanValidationPolicy:
    max_agents: int
    max_tasks: int
    max_dependencies: int
    browser_access: bool
    browser_requested: bool
    browser_available: bool
    internet_access: bool = False

    def prompt_summary(self) -> dict[str, object]:
        return {
            "limits": {
                "max_agents": self.max_agents,
                "max_tasks": self.max_tasks,
                "max_dependencies": self.max_dependencies,
            },
            "runtime_boundaries": {
                "browser": self.browser_access,
                "internet": self.internet_access,
                "shell": False,
                "email": False,
                "deployment": False,
            },
            "browser": {
                "owner_requested": self.browser_requested,
                "available": self.browser_available,
                "permitted": self.browser_access,
            },
        }


class PlanValidator:
    SUPPORTED_ROLES = frozenset({"GENERAL", "RESEARCHER", "DEVELOPER", "QA"})
    DEVELOPMENT_TOOLS = frozenset(
        {
            "development.execute",
            "development.install_dependencies",
            "git.init",
            "git.status",
            "git.diff",
            "git.log",
            "git.commit",
        }
    )
    DEVELOPMENT_REQUIRED_PERMISSIONS = frozenset(
        {
            ToolPermission.FILESYSTEM_LIST.value,
            ToolPermission.FILESYSTEM_READ.value,
            ToolPermission.FILESYSTEM_WRITE.value,
            ToolPermission.DEVELOPMENT_EXECUTE.value,
            ToolPermission.GIT_READ.value,
        }
    )
    REQUIRED_ROLE_BY_KIND = {
        "DEVELOPMENT": "DEVELOPER",
        "QA": "QA",
        "RESEARCH": "RESEARCHER",
    }
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
    def capability_manifest(self) -> dict[str, object]:
        definitions = self.tools.list(enabled_only=True)
        tools = {definition.name for definition in definitions}
        if self.settings.development_enabled:
            tools.update(self.DEVELOPMENT_TOOLS)
        return {
            "roles": sorted(self.SUPPORTED_ROLES),
            "tools": sorted(tools),
            "permissions": sorted(
                permission.value
                for permission in ToolPermission
                if permission != ToolPermission.SHELL_RUN
            ),
            "model_aliases": sorted(self.settings.model_aliases),
            "development": {
                "enabled": self.settings.development_enabled,
                "required_role": "DEVELOPER",
                "qa_role_required": True,
                "minimum_iterations": max(
                    2, min(self.settings.developer_max_fix_attempts + 1, 20)
                ),
                "required_permissions": sorted(self.DEVELOPMENT_REQUIRED_PERMISSIONS),
                "supported_profiles": ["NODE", "PYTHON", "STATIC_WEB"],
            },
        }

    def policy_for(self, constraints: dict[str, Any] | None) -> PlanValidationPolicy:
        values = constraints if isinstance(constraints, dict) else {}
        tools = set(self.capability_manifest["tools"])
        browser_requested = values.get("browser_access") is True
        browser_available = "browser.capture" in tools

        def bounded_limit(key: str, configured: int) -> int:
            value = values.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return min(value, configured)
            return configured

        return PlanValidationPolicy(
            max_agents=bounded_limit("max_agents", self.settings.mission_max_agents),
            max_tasks=bounded_limit("max_tasks", self.settings.mission_max_tasks),
            max_dependencies=bounded_limit(
                "max_dependencies", self.settings.mission_max_dependencies
            ),
            browser_access=browser_requested and browser_available,
            browser_requested=browser_requested,
            browser_available=browser_available,
        )

    def validate(
        self, proposal: PlanProposal, *, policy: PlanValidationPolicy | None = None
    ) -> PlanValidationResult:
        resolved_policy = policy or self.policy_for({})
        errors: list[PlanValidationError] = []
        self._validate_limits(proposal, resolved_policy, errors)
        agent_keys = [agent.key for agent in proposal.agents]
        task_keys = [task.key for task in proposal.tasks]
        self._duplicates(agent_keys, "agent", errors)
        self._duplicates(task_keys, "task", errors)
        agents = set(agent_keys)
        agents_by_key = {agent.key: agent for agent in proposal.agents}
        agent_indices = {agent.key: index for index, agent in enumerate(proposal.agents)}
        roles = {agent.role.strip().upper() for agent in proposal.agents}
        browser_qa_available = any(
            agent.role.strip().upper() == "QA"
            and agent.requested_permissions.get(ToolPermission.BROWSER_CAPTURE.value) is True
            for agent in proposal.agents
        )
        tasks = set(task_keys)
        permission_manifest = self.capability_manifest["permissions"]
        assert isinstance(permission_manifest, list)
        allowed_permissions = set(permission_manifest)

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
                elif (
                    permission == ToolPermission.BROWSER_CAPTURE.value
                    and requested
                    and not resolved_policy.browser_access
                ):
                    errors.append(
                        PlanValidationError(
                            code="CAPABILITY_POLICY_CONFLICT",
                            message=(
                                "Agent requested browser.capture, but effective Mission policy "
                                "does not permit browser access."
                            ),
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
            for criterion_index, criterion in enumerate(task.acceptance_criteria):
                analysis = analyze_acceptance_criterion(criterion)
                path = f"tasks.{index}.acceptance_criteria.{criterion_index}"
                if not analysis.observable:
                    errors.append(
                        PlanValidationError(
                            code="UNVERIFIABLE_ACCEPTANCE_CRITERION",
                            message=(
                                "Acceptance criteria must name observable file, command, "
                                "behavior, or verification evidence."
                            ),
                            path=path,
                        )
                    )
                elif analysis.browser_required and not resolved_policy.browser_access:
                    reason = (
                        "Mission policy forbids browser access."
                        if not resolved_policy.browser_requested
                        else "Forge browser capture is unavailable in this environment."
                    )
                    errors.append(
                        PlanValidationError(
                            code="CAPABILITY_POLICY_CONFLICT",
                            message=(
                                "Acceptance criterion requires browser or rendered viewport "
                                f"evidence, but {reason} Use a permitted verification mechanism "
                                "or leave the judgment to human review."
                            ),
                            path=path,
                        )
                    )
                elif analysis.browser_required and not browser_qa_available:
                    errors.append(
                        PlanValidationError(
                            code="BROWSER_PERMISSION_REQUIRED",
                            message=(
                                "Browser-verifiable acceptance criteria require a QA Agent with "
                                "browser.capture explicitly enabled."
                            ),
                            path=path,
                        )
                    )
            assigned = agents_by_key.get(task.assigned_agent_key)
            assigned_role = assigned.role.strip().upper() if assigned is not None else None
            required_role = self.REQUIRED_ROLE_BY_KIND.get(task.kind.value)
            if required_role is not None and assigned_role != required_role:
                errors.append(
                    PlanValidationError(
                        code="TASK_ROLE_MISMATCH",
                        message=(
                            f"{task.kind.value} Task '{task.key}' must be assigned to "
                            f"a {required_role} Agent."
                        ),
                        path=f"tasks.{index}.assigned_agent_key",
                    )
                )
            if task.kind.value == "DEVELOPMENT":
                self._validate_development_task(
                    task_index=index,
                    task=task,
                    assigned=assigned,
                    assigned_index=agent_indices.get(task.assigned_agent_key),
                    roles=roles,
                    errors=errors,
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

    def _validate_development_task(
        self,
        *,
        task_index: int,
        task: ProposedTask,
        assigned: ProposedAgent | None,
        assigned_index: int | None,
        roles: set[str],
        errors: list[PlanValidationError],
    ) -> None:
        if not self.settings.development_enabled:
            errors.append(
                PlanValidationError(
                    code="DEVELOPMENT_RUNTIME_UNAVAILABLE",
                    message="Forge development execution is disabled.",
                    path=f"tasks.{task_index}.kind",
                )
            )
        if "QA" not in roles:
            errors.append(
                PlanValidationError(
                    code="DEVELOPMENT_QA_PATH_REQUIRED",
                    message="A DEVELOPMENT plan requires an independent QA Agent.",
                    path="agents",
                )
            )
        minimum_iterations = max(2, min(self.settings.developer_max_fix_attempts + 1, 20))
        if task.max_iterations < minimum_iterations:
            errors.append(
                PlanValidationError(
                    code="DEVELOPMENT_ITERATION_BUDGET_INVALID",
                    message=(
                        "DEVELOPMENT Tasks require at least "
                        f"{minimum_iterations} iterations for the configured fix loop."
                    ),
                    path=f"tasks.{task_index}.max_iterations",
                )
            )
        if assigned is None:
            return
        permissions = assigned.requested_permissions
        missing = sorted(
            permission
            for permission in self.DEVELOPMENT_REQUIRED_PERMISSIONS
            if permissions.get(permission) is not True
        )
        for permission in missing:
            errors.append(
                PlanValidationError(
                    code="DEVELOPMENT_PERMISSION_REQUIRED",
                    message=f"Developer permission '{permission}' must be explicitly enabled.",
                    path=(
                        f"agents.{assigned_index}.requested_permissions.{permission}"
                    ),
                )
            )

    def _validate_limits(
        self,
        proposal: PlanProposal,
        policy: PlanValidationPolicy,
        errors: list[PlanValidationError],
    ) -> None:
        limits = (
            ("agents", len(proposal.agents), policy.max_agents),
            ("tasks", len(proposal.tasks), policy.max_tasks),
            (
                "dependencies",
                len(proposal.dependencies),
                policy.max_dependencies,
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
