# Forge architecture

Phase 09.5 runtime efficiency and Product QA are documented in
[`efficient-development-runtime.md`](efficient-development-runtime.md) and
[`product-qa.md`](product-qa.md). They extend Phase 09 without changing deterministic command,
workspace-isolation, paid-model, or tool-permission boundaries.

## Phase 09 development path

```text
Orchestrator -> ExecutionJob -> agent worker -> Developer AgentRuntime
                                         |          |
                                         |          +-> filesystem tools
                                         |          +-> typed development/Git intent
                                         |                       |
                                         |                isolated dev runner
                                         |                       |
                                         +-> deterministic QA <- build/test evidence
                                                     |
                                               REVIEW or FIX_REQUIRED
```

PostgreSQL remains authoritative for Tasks, AgentRuns, ToolCalls, DevelopmentExecutions, QA results,
acceptance verification, project leases, events, and review history. The runner's file queue is a
bounded local transport only; a missing response is never treated as success. See
[development-runtime.md](development-runtime.md) and [dev-runner.md](dev-runner.md).

## Phase 08 goal-to-execution path

```text
User Goal → Mission → MissionPlanner → typed PlanProposal → deterministic PlanValidator
          → Human ACTIVATE → transactional CompanyFactory → Project/Agents/Task DAG
          → DependencyResolver → ready Tasks → Orchestrator → Workers → AgentRuntime
```

Planning and execution are separate. The provider proposes plan-local data only; deterministic
backend code validates and materializes it. PostgreSQL remains the source of truth for planning,
graph, review, and execution state. Redis remains event transport and is not required for dependency
reconciliation correctness.

Forge Phase 08 adds user-triggered Mission planning and dependency-graph execution while retaining
the controlled AgentRuntime, task lifecycle, and transactional event architecture.

```text
Browser -> forge-web -> forge-api -> AgentRuntime <-> ModelProvider
              ^            |              |                 +-> mock (default)
              | SSE        |              |                 `-> OpenAI (gated)
              |            |              |
              |            |              +-> ToolRegistry -> PermissionEngine
              |            |                                  -> ToolExecutor
              |            |                                      -> Project workspace
              |            v
              +------- Redis Stream <- publisher <- PostgreSQL outbox
                                           +-> forge-audit consumer group
                                           `-> forge-dashboard consumer group
```

The browser polls the same-origin `/api/status` route for persistent state and connects to `/api/events/stream` for live activity. Next.js queries and streams from FastAPI over the private Compose network; the browser never receives an internal container hostname.

## Domain and persistence boundaries

Pydantic request/response schemas are API contracts and do not expose ORM models. Route handlers validate HTTP input and delegate to focused services. Services validate cross-entity ownership and coordinate repositories.

`TaskStateMachine` exclusively owns lifecycle fields. `AgentRuntime` coordinates it but cannot
bypass it. Each provider call has its own AgentRun. Tool requests pass through a separate registry,
permission, workspace, executor, and ToolCall service boundary. Provider and filesystem work runs
after commit with no database lock held. `EventFactory` remains the single
event/envelope/outbox construction boundary.

Application database access is async through SQLAlchemy and `asyncpg`. PostgreSQL JSONB stores structured domain data, run request metadata, validated result/error data, and events. With `STORE_MODEL_INPUTS=false`, prompt hashes are stored instead of raw prompts.

Provider-neutral, free-first model selection and durable budget reservations are described in
[`model-router.md`](model-router.md). The deterministic router sits above provider adapters and
below AgentRuntime/Planner; it does not broaden tool authority or agent capabilities.

## Model boundary

`ModelProvider` exposes provider-neutral `ModelRequest`, `ModelResponse`, and `ModelUsage` contracts. The default deterministic mock is free. The optional OpenAI adapter uses the Responses API structured-output parser, has zero SDK retries, and is blocked unless paid calls are explicitly enabled. There is no provider fallback.

Providers receive immutable prompt/context values only. They never receive an ORM session, repository, state machine, Redis client, shell, filesystem, browser, or Forge credentials. The OpenAI key is passed only to the API container and is held as a secret setting.

## Migrations and concurrency

Alembic is the only schema creation path. Phase 05 revisions `0005`/`0006` add AgentRun history
and cached-token accounting. Phase 06 revision `0007` adds ToolCall history and
`tool_call_status` without altering applied migrations.

Transitions lock their Task row with PostgreSQL `SELECT FOR UPDATE`. A partial unique index permits at most one `STARTED` TaskRun per Task, and another permits at most one `RUNNING` AgentRun per TaskRun. These database constraints backstop service-level checks. Stale recovery uses a partial `RUNNING`/`started_at` index and `FOR UPDATE SKIP LOCKED`.

The API container runs `alembic upgrade head` before Uvicorn. The test suite validates upgrade → downgrade → upgrade against a disposable database.

## Deletion, indexes, and events

There are no hard-delete APIs. Domain and run foreign keys use `ON DELETE RESTRICT`, so dependent history cannot be silently discarded. Lifecycle statuses are the retention mechanism.

Every foreign key used for joins or restriction checks is indexed. ToolCall lists use task/time,
agent/time, status/time, AgentRun, and TaskRun indexes. A partial index supports stale `RUNNING`
recovery. JSONB fields are not indexed because no containment query exists yet.

AgentRun and ToolCall events use the same Task-based correlation ID as Task and TaskRun events.
Domain state and outbox publication intent commit atomically. Redis remains transport, never the
source of truth.

## Phase boundary

Phase 06 supports explicit single-task execution with bounded filesystem tools only. It does not
include Phase 07 autonomous pickup or orchestration. See [agent-runtime.md](agent-runtime.md),
[tool-system.md](tool-system.md), [event-system.md](event-system.md), and
[task-state-machine.md](task-state-machine.md).
# Phase 07 runtime topology

Phase 07 adds two process boundaries: `forge-orchestrator` performs deterministic event wakeups,
database reconciliation, and recovery; `forge-agent-worker` claims leased PostgreSQL jobs and
invokes the existing AgentRuntime. PostgreSQL remains authoritative. Redis accelerates wakeups
and realtime delivery but is not the execution queue source of truth. See
[orchestrator.md](orchestrator.md) and [worker-runtime.md](worker-runtime.md).
