from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class EntityNotFoundError(DomainError):
    def __init__(self, entity: str) -> None:
        super().__init__(404, "not_found", f"{entity} not found.")


class DuplicateSlugError(DomainError):
    def __init__(self) -> None:
        super().__init__(409, "duplicate_company_slug", "A company with this slug already exists.")


class InvalidRelationshipError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "invalid_relationship", message)


class InvalidTaskTransitionError(DomainError):
    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(
            409,
            "INVALID_TASK_TRANSITION",
            f"Transition from {from_status} to {to_status} is not allowed",
        )


class TaskUnassignedError(DomainError):
    def __init__(self) -> None:
        super().__init__(409, "TASK_UNASSIGNED", "Task must be assigned before execution")


class TaskRunStateConflictError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "TASK_RUN_STATE_CONFLICT", message)


class TaskReviewConflictError(DomainError):
    def __init__(self, message: str = "This task iteration already has a review decision") -> None:
        super().__init__(409, "TASK_REVIEW_CONFLICT", message)


class InvalidTaskInputError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "INVALID_TASK_INPUT", message)


class AgentRuntimeDomainError(DomainError):
    def __init__(self, code: str, message: str, status_code: int = 500) -> None:
        super().__init__(status_code, code, message)


class DevelopmentInfrastructureError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(503, code, message)


class ModelAliasNotFoundError(AgentRuntimeDomainError):
    def __init__(self, alias: str) -> None:
        super().__init__("MODEL_ALIAS_NOT_FOUND", f"Model alias '{alias}' is not configured", 400)


class PaidModelCallDisabledError(AgentRuntimeDomainError):
    def __init__(self) -> None:
        super().__init__(
            "PAID_MODEL_CALL_DISABLED",
            "Paid model calls are disabled. Set ALLOW_PAID_MODEL_CALLS=true to opt in",
            403,
        )


class ExecutionConflictError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "EXECUTION_CONFLICT", message)


class MissionConflictError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class MissionPlanningError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status_code, code, message, details=details)
