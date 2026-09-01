# Missions

A Mission is one explicit user-supplied objective above Project and Task execution. Forge does not
invent Missions. `POST /api/v1/missions` stores a title, goal, optional existing Company, bounded
context, and constraints. Secret-like context keys are rejected and credentials are never part of
planner input.

The lifecycle is `DRAFT → PLANNING → PLAN_READY → ACTIVE → COMPLETED`. Invalid or recoverable
failed planning returns the Mission to `DRAFT` while attempts remain; exhausted planning becomes
`FAILED`. Cancellation preserves every record and cancels non-terminal Tasks through
`TaskStateMachine`. Planning success never activates a Mission: `POST /missions/{id}/activate` is
the human checkpoint.

If `company_id` is supplied, activation adds a new Project and Mission-specific Agents without
changing Company identity. Otherwise the factory creates a Company with a collision-resistant slug.
Activation can occur while autonomy is paused: root Tasks are `QUEUED`, dependent Tasks stay
`CREATED`, and Phase 07 still controls whether execution jobs may be created or claimed.

Mission progress is computed from the Project's durable Task rows: total, done, review, running,
queued, blocked (`CREATED`), failed, and cancelled. All Tasks must be `DONE` before Project and
Mission completion.

## API

- `POST /api/v1/missions`
- `GET /api/v1/missions`
- `GET /api/v1/missions/{id}`
- `POST /api/v1/missions/{id}/plan`
- `GET /api/v1/missions/{id}/plan`
- `POST /api/v1/missions/{id}/activate`
- `POST /api/v1/missions/{id}/cancel`
- `POST /api/v1/missions/{id}/archive`
- `POST /api/v1/missions/{id}/restore`
- `DELETE /api/v1/missions/{id}`

Planning is manually triggered and bounded. There is no background or autonomous paid planning.

## Organizational lifecycle

Archival is independent from the business lifecycle. `archived_at` hides a Mission from default
operational lists while preserving its status, Project, Tasks, Agents, planning runs, executions,
reviews, QA evidence, events, and workspace. `GET /api/v1/missions` defaults to current Missions;
`archive=archived` returns the archive and `archive=all` returns both. Restore only clears
`archived_at` and never recreates topology.

Forge allows archival only for `DRAFT`, `PLAN_READY`, `COMPLETED`, `FAILED`, and `CANCELLED`.
`PLANNING`, `ACTIVE`, and `REVIEW` must first be stopped or cancelled. The service also locks and
checks Task, TaskRun, AgentRun, ToolCall, ExecutionJob, DevelopmentExecution, Agent, and Project
lease state. Archiving never cancels work. Repeated archive and restore calls are idempotent.

Permanent deletion is exceptional. The Mission must first be archived, remain in an organizable
state, have no active durable execution, and include typed confirmation equal to its title or
`DELETE`. Row locks provide one destructive winner under concurrent requests.

Activation persists ownership explicitly. Every activated Mission owns its factory-created
Project. It owns the Company only when CompanyFactory created that Company; Missions attached to an
existing Company never own it. Existing installations conservatively backfill Company ownership as
false. Deletion removes the owned Project, Tasks, dependencies, TaskRuns, AgentRuns, ToolCalls,
ExecutionJobs, development/QA/acceptance evidence, reviews, planning runs, Mission-specific Agents,
and linked event delivery rows. A Company is removed only when ownership is explicit and no other
Mission, Project, Agent, or Task uses it.

Database deletion and filesystem cleanup cannot be one transaction. Forge first validates and
deletes durable topology transactionally while creating a `mission_deletion_records` tombstone.
It then asks `WorkspaceManager` to delete exactly the UUID-scoped Company/Project workspace without
following symlinks. The tombstone records `COMPLETED`, `NOT_APPLICABLE`, or `CLEANUP_FAILED`, so a
filesystem failure is visible and recoverable rather than silently losing cleanup state.

Archived Missions cannot be planned, activated, or cancelled until restored. Historical events
remain queryable according to event retention, while normal sidebar, overview, attention, and live
operational summaries exclude archived Missions.
