"""Phase 03 task state-machine integrity.

Revision ID: 20260819_0002
Revises: 20260819_0001
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260819_0002"
down_revision: str | None = "20260819_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Phase 02 did not own TaskRun lifecycle. Close any manually-created active
    # runs before establishing the Phase 03 one-active-run invariant.
    op.execute(
        """
        UPDATE task_runs
        SET status = 'CANCELLED', completed_at = COALESCE(completed_at, now())
        WHERE status = 'STARTED'
        """
    )
    op.execute(
        """
        UPDATE task_runs
        SET completed_at = COALESCE(completed_at, started_at, now())
        WHERE status <> 'STARTED'
        """
    )

    # Renumber any legacy history deterministically so an attempt is unique per
    # task and every Phase 03 execution can append exactly one next iteration.
    op.execute(
        """
        WITH ranked AS (
          SELECT id, row_number() OVER (
            PARTITION BY task_id ORDER BY created_at, id
          ) AS new_iteration
          FROM task_runs
        )
        UPDATE task_runs AS run
        SET iteration = ranked.new_iteration
        FROM ranked
        WHERE run.id = ranked.id
        """
    )
    op.execute(
        """
        UPDATE tasks AS task
        SET iteration = GREATEST(task.iteration, history.max_iteration)
        FROM (
          SELECT task_id, max(iteration) AS max_iteration
          FROM task_runs
          GROUP BY task_id
        ) AS history
        WHERE task.id = history.task_id
        """
    )
    op.execute("UPDATE tasks SET max_iterations = iteration WHERE iteration > max_iterations")

    # A Phase 02 IN_PROGRESS row has no guaranteed active TaskRun. Move it back
    # to QUEUED so the next execution is started atomically by the state machine.
    op.execute(
        """
        INSERT INTO events (
          company_id, project_id, agent_id, task_id, type, message, metadata
        )
        SELECT
          company_id,
          project_id,
          assigned_agent_id,
          id,
          'TASK_STATUS_CHANGED',
          'Legacy IN_PROGRESS task normalized to QUEUED for Phase 03.',
          jsonb_build_object(
            'from_status', 'IN_PROGRESS',
            'to_status', 'QUEUED',
            'reason', 'Phase 03 migration requires TaskRun-backed execution',
            'iteration', iteration
          )
        FROM tasks
        WHERE status = 'IN_PROGRESS'
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET status = 'QUEUED', completed_at = NULL, updated_at = now()
        WHERE status = 'IN_PROGRESS'
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET completed_at = COALESCE(completed_at, now())
        WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET completed_at = NULL
        WHERE status NOT IN ('DONE', 'FAILED', 'CANCELLED')
        """
    )

    op.create_check_constraint(
        "ck_tasks_iteration_within_maximum",
        "tasks",
        "iteration <= max_iterations",
    )
    op.create_check_constraint(
        "ck_tasks_terminal_completion",
        "tasks",
        "(status IN ('DONE', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL) "
        "OR (status NOT IN ('DONE', 'FAILED', 'CANCELLED') AND completed_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_tasks_in_progress_started",
        "tasks",
        "status <> 'IN_PROGRESS' OR started_at IS NOT NULL",
    )

    op.drop_constraint("ck_task_runs_iteration_nonnegative", "task_runs", type_="check")
    op.create_check_constraint("ck_task_runs_iteration_positive", "task_runs", "iteration > 0")
    op.create_check_constraint(
        "ck_task_runs_completion_matches_status",
        "task_runs",
        "(status = 'STARTED' AND completed_at IS NULL) OR "
        "(status <> 'STARTED' AND completed_at IS NOT NULL)",
    )
    op.create_index(
        "uq_task_runs_task_id_iteration",
        "task_runs",
        ["task_id", "iteration"],
        unique=True,
    )
    op.create_index(
        "uq_task_runs_one_started_per_task",
        "task_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'STARTED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_runs_one_started_per_task", table_name="task_runs")
    op.drop_index("uq_task_runs_task_id_iteration", table_name="task_runs")
    op.drop_constraint("ck_task_runs_completion_matches_status", "task_runs", type_="check")
    op.drop_constraint("ck_task_runs_iteration_positive", "task_runs", type_="check")
    op.create_check_constraint("ck_task_runs_iteration_nonnegative", "task_runs", "iteration >= 0")

    op.drop_constraint("ck_tasks_in_progress_started", "tasks", type_="check")
    op.drop_constraint("ck_tasks_terminal_completion", "tasks", type_="check")
    op.drop_constraint("ck_tasks_iteration_within_maximum", "tasks", type_="check")
