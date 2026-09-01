from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints

from app.domain.enums import TaskReviewDecision
from app.schemas.common import ORMResponse

ReviewFeedback = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class TaskFixRequest(ORMResponse):
    model_config = ConfigDict(extra="forbid")

    feedback: ReviewFeedback


class TaskReviewResponse(ORMResponse):
    id: UUID
    task_id: UUID
    task_run_id: UUID | None
    agent_run_id: UUID | None
    iteration: int = Field(gt=0)
    decision: TaskReviewDecision
    feedback: str | None
    correlation_id: UUID
    created_at: datetime
