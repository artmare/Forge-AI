from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from app.domain.enums import TaskKind, TaskPriority

RequestedPermissions = Annotated[
    dict[str, bool],
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "filesystem.list": {"type": "boolean"},
                "filesystem.read": {"type": "boolean"},
                "filesystem.write": {"type": "boolean"},
                "development.execute": {"type": "boolean"},
                "development.install_dependencies": {"type": "boolean"},
                "git.read": {"type": "boolean"},
                "git.write": {"type": "boolean"},
                "browser.capture": {"type": "boolean"},
            },
            "required": [
                "filesystem.list",
                "filesystem.read",
                "filesystem.write",
                "development.execute",
                "development.install_dependencies",
                "git.read",
                "git.write",
                "browser.capture",
            ],
            "additionalProperties": False,
        }
    ),
]

ProposedTaskInput = Annotated[
    dict[str, Any],
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "deliverables": {"type": "array", "items": {"type": "string"}},
                "source_files": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["objective", "deliverables", "source_files", "constraints"],
            "additionalProperties": False,
        }
    ),
]


class ProposedProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class ProposedAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    model_alias: str = Field(default="default", min_length=1, max_length=80)
    requested_permissions: RequestedPermissions = Field(default_factory=dict)


class ProposedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4000)
    assigned_agent_key: str = Field(min_length=1, max_length=80)
    priority: TaskPriority = TaskPriority.NORMAL
    kind: TaskKind = TaskKind.GENERAL
    input: ProposedTaskInput = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    max_iterations: int = Field(default=1, ge=1, le=20)


class ProposedDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=80)
    depends_on: str = Field(min_length=1, max_length=80)


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProposedProject
    agents: list[ProposedAgent] = Field(min_length=1)
    tasks: list[ProposedTask] = Field(min_length=1)
    dependencies: list[ProposedDependency] = Field(default_factory=list)


class PlanValidationError(BaseModel):
    code: str
    message: str
    path: str | None = None


class PlanValidationResult(BaseModel):
    valid: bool
    errors: list[PlanValidationError] = Field(default_factory=list)
    agent_count: int
    task_count: int
    dependency_count: int
