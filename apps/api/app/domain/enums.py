from enum import Enum


class CompanyStatus(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class ProjectStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class MissionStatus(str, Enum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    ACTIVE = "ACTIVE"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PlanningRunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    INVALID = "INVALID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentStatus(str, Enum):
    CREATED = "CREATED"
    IDLE = "IDLE"
    BUSY = "BUSY"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    FIX_REQUIRED = "FIX_REQUIRED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TaskKind(str, Enum):
    GENERAL = "GENERAL"
    RESEARCH = "RESEARCH"
    DOCUMENTATION = "DOCUMENTATION"
    DEVELOPMENT = "DEVELOPMENT"
    QA = "QA"


class TaskRunStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskReviewDecision(str, Enum):
    APPROVED = "APPROVED"
    FIX_REQUESTED = "FIX_REQUESTED"


class AgentRunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolCallStatus(str, Enum):
    REQUESTED = "REQUESTED"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


class ToolRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolPermission(str, Enum):
    FILESYSTEM_LIST = "filesystem.list"
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    SHELL_RUN = "shell.run"
    DEVELOPMENT_EXECUTE = "development.execute"
    DEVELOPMENT_INSTALL_DEPENDENCIES = "development.install_dependencies"
    GIT_READ = "git.read"
    GIT_WRITE = "git.write"


class DevelopmentProjectType(str, Enum):
    NODE = "NODE"
    PYTHON = "PYTHON"
    STATIC_WEB = "STATIC_WEB"
    UNKNOWN = "UNKNOWN"


class PackageManager(str, Enum):
    NPM = "NPM"
    PIP = "PIP"
    NONE = "NONE"


class DevelopmentAction(str, Enum):
    NODE_INSTALL = "NODE_INSTALL"
    NODE_TEST = "NODE_TEST"
    NODE_BUILD = "NODE_BUILD"
    NODE_LINT = "NODE_LINT"
    NODE_TYPECHECK = "NODE_TYPECHECK"
    PYTHON_INSTALL = "PYTHON_INSTALL"
    PYTHON_TEST = "PYTHON_TEST"
    PYTHON_LINT = "PYTHON_LINT"
    GIT_INIT = "GIT_INIT"
    GIT_STATUS = "GIT_STATUS"
    GIT_DIFF = "GIT_DIFF"
    GIT_LOG = "GIT_LOG"
    GIT_CHECKPOINT = "GIT_CHECKPOINT"


class DevelopmentExecutionStatus(str, Enum):
    REQUESTED = "REQUESTED"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


class AcceptanceVerificationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class QADecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class QACheckStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_VERIFIED = "NOT_VERIFIED"


class ExecutionJobStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionPhase(str, Enum):
    PREPARING = "PREPARING"
    ENVIRONMENT_SETUP = "ENVIRONMENT_SETUP"
    CHECKPOINTING = "CHECKPOINTING"
    AGENT_START = "AGENT_START"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    QA = "QA"
    FINALIZING = "FINALIZING"


class WorkerStatus(str, Enum):
    ONLINE = "ONLINE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class EventConsumptionStatus(str, Enum):
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
