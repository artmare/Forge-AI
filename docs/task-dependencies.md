# Task Dependencies

`TaskDependency(task_id, depends_on_task_id)` is the durable Project execution graph. PostgreSQL
enforces non-self edges and unique pairs; both foreign keys are indexed. `CompanyFactory` additionally
enforces same-Project and same-Company ownership, and `PlanValidator` rejects cycles before any row
exists.

A dependency is satisfied only when the upstream Task is `DONE`. `REVIEW` is intentionally not
sufficient:

```text
IN_PROGRESS → REVIEW → explicit APPROVE → DONE → downstream QUEUED
```

`POST /api/v1/tasks/{id}/approve` uses `TaskStateMachine`, then `DependencyResolver` locks each
dependent Task and queues it only when every upstream row is `DONE`. `request-fix` moves
`REVIEW → FIX_REQUIRED`; the existing explicit transition endpoint handles `FIX_REQUIRED → QUEUED`.

The Orchestrator consumer reacts to `TASK_STATUS_CHANGED → DONE` for low-latency wakeup. Periodic
database reconciliation independently finds `CREATED` Tasks whose dependencies are all `DONE`, so a
Redis outage or missed event cannot permanently strand work. Repeated events are safe because the
Task row lock and state machine allow only one valid queue transition.

Failed or cancelled upstream Tasks keep dependents in `CREATED` and expose deterministic blocked
reasons. Project failure is not inferred from one failed Task. Completion reconciliation marks a
Project and linked Mission complete only when the Project has Tasks and every one is `DONE`.

Read APIs are `GET /tasks/{id}/dependencies`, `GET /tasks/{id}/dependents`, and
`GET /projects/{id}/graph`. The graph returns safe Task state, acceptance criteria, latest result, and
safe ToolCall summaries for review.
