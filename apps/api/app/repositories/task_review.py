from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskReviewDecision
from app.domain.models import TaskReview


class TaskReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, review: TaskReview) -> TaskReview:
        self.session.add(review)
        await self.session.flush()
        return review

    async def get_for_iteration(self, task_id: UUID, iteration: int) -> TaskReview | None:
        return await self.session.scalar(
            select(TaskReview).where(
                TaskReview.task_id == task_id,
                TaskReview.iteration == iteration,
            )
        )

    async def latest_fix_for_task(self, task_id: UUID) -> TaskReview | None:
        return await self.session.scalar(
            select(TaskReview)
            .where(
                TaskReview.task_id == task_id,
                TaskReview.decision == TaskReviewDecision.FIX_REQUESTED,
            )
            .order_by(TaskReview.iteration.desc(), TaskReview.created_at.desc())
            .limit(1)
        )

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[TaskReview]:
        result = await self.session.scalars(
            select(TaskReview)
            .where(TaskReview.task_id == task_id)
            .order_by(TaskReview.iteration, TaskReview.created_at, TaskReview.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result)
