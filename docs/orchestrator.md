# Deterministic orchestrator

Phase 08 adds two deterministic duties to the existing Orchestrator process: dependency/project
completion reconciliation and stale PlanningRun recovery. It never performs Mission planning or any
model call. A `DONE` Task event triggers a short dependency resolution transaction; polling provides
the Redis-independent fallback. Newly queued downstream Tasks enter the unchanged Phase 07 job path
and remain subject to pause/resume, capacity, claim, lease, and recovery controls.

Forge Phase 07 adds autonomous pickup without giving an LLM control over scheduling. The
orchestrator is deterministic application code. It detects queued work, evaluates fixed
eligibility rules, persists `ExecutionJob` rows, reconciles missed wakeups, applies runtime
controls, and invokes recovery. It never invents goals, creates companies or tasks, selects
markets, or performs model/tool execution.

## Architecture

```text
                      ┌───────────────┐
                      │   FastAPI     │
                      └───────┬───────┘
                              │
                              ▼
                         PostgreSQL
                              │
                ┌─────────────┴─────────────┐
                │                           │
                ▼                           ▼
         Event System                 Orchestrator
                │                           │
                ▼                           ▼
             Redis                   ExecutionJobs
                                            │
                                      claim / lease
                                            │
                                            ▼
                                   forge-agent-worker
                                            │
                                            ▼
                                       AgentRuntime
                                            │
                                      ┌─────┴─────┐
                                      ▼           ▼
                                    Model       Tools
```

PostgreSQL is authoritative for tasks, jobs, workers, controls, runs, and events. Redis Streams
carry event wakeups and dashboard delivery only. A Redis outage cannot delete or hide durable
work from reconciliation or database-polling workers.

## ExecutionJob

`execution_jobs` separates business state from infrastructure dispatch. A `Task` is the work
unit; an `ExecutionJob` is an instruction to attempt that task. Jobs contain task, optional task
run, agent and worker references; status; inherited priority; infrastructure attempts; delayed
availability; lease ownership/expiry; lifecycle timestamps; normalized error data; and the
correlation ID.

Statuses are `PENDING`, `CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED`, and `CANCELLED`. A partial
unique index permits at most one `PENDING`/`CLAIMED`/`RUNNING` job per task. Another partial
unique index permits at most one `CLAIMED`/`RUNNING` job per agent. Foreign-key columns and the
pending/lease query columns are indexed.

`Task.iteration` counts business executions and is changed only by `TaskStateMachine`.
`ExecutionJob.attempts` counts infrastructure claims. These counters are never substituted for
one another.

## Scheduling and eligibility

A task is scheduled only when persistent autonomy is enabled and all of these conditions hold:

- status is `QUEUED`;
- an assigned agent exists in the same company;
- the agent is not `PAUSED`, `STOPPED`, or `FAILED`;
- task iteration is below the maximum;
- no active job exists for the task;
- no `STARTED` task run conflicts with scheduling.

The event path consumes `TASK_STATUS_CHANGED` events whose target is `QUEUED` in the
`forge-orchestrator` consumer group. The handler only attempts idempotent job creation; it never
runs AgentRuntime. Every five seconds by default, reconciliation scans the same durable
eligibility conditions and creates anything missed by event delivery. The database constraint is
the final duplicate defense when event and reconciliation paths race.

Priority order is `CRITICAL`, `HIGH`, `NORMAL`, then `LOW`; oldest available job wins within a
priority. Phase 07 does not implement priority aging, so sustained higher-priority load can delay
low-priority work. This limitation is explicit rather than hidden behind a complex scheduler.

## Runtime controls

The `runtime_controls` table contains the singleton `autonomy` row. Migration `20260820_0008`
inserts it as disabled regardless of environment, so upgrading a database with queued tasks does
not unexpectedly execute them. The environment default is also false and is only used to repair
a missing row.

`POST /api/v1/orchestrator/pause` persists disabled autonomy. Scheduling stops, workers stop
claiming, and a job claimed but not started is returned to pending. Already-running bounded work
may finish. `POST /api/v1/orchestrator/resume` persists enabled autonomy and immediately runs
reconciliation. Browser state is never authoritative.

Phase 07 intentionally implements pause/resume, not an emergency stop with irreversible-action
guarantees. Lease loss blocks future model turns and tool calls as soon as the runtime reaches a
guard point, but a model call or filesystem operation already in progress cannot be undone.

## Concurrency and claiming

Workers lock the autonomy row before claim-capacity decisions. That short row lock serializes the
global `FORGE_MAX_CONCURRENT_TASKS` count across processes without advisory locks. The selected
pending job is locked with `FOR UPDATE SKIP LOCKED`, changed to `CLAIMED`, assigned a worker and
lease, and committed. Task, agent, and control locks are never held during model calls, tool
execution, or filesystem work.

Each worker independently runs at most `AGENT_WORKER_CONCURRENCY` slots. Database state, not an
in-memory counter, enforces the global cap and one-task-per-agent rule. Company-specific capacity
is intentionally deferred.

## Recovery

Claimed and running jobs use configurable renewable leases. Recovery treats states differently:

- If a lease expires while the task remains queued and no task run began, the same job returns to
  `PENDING` after bounded backoff when attempts remain. This is a clearly infrastructure-only
  retry.
- If attempts are exhausted before execution begins, the job and task fail with
  `EXECUTION_RETRY_EXHAUSTED` / infrastructure-failure context.
- If a lease expires after a task run started, recovery does not redispatch. Active AgentRuns and
  ToolCalls are failed, the task is failed through `TaskStateMachine`, the job becomes `FAILED`
  with `WORKER_LOST`, and the agent returns to `IDLE`. Side-effect state is explicitly ambiguous.
- If the task already reached `REVIEW` with a successful task run, recovery infers completion and
  marks the job succeeded instead of rerunning it.
- Cancelled tasks cancel their active infrastructure job. Pending jobs for tasks no longer queued
  are cancelled during reconciliation.

Stale worker heartbeats become `OFFLINE`. `BUSY` agents with no claimed/running job are returned
to `IDLE`. Started task runs with no active job are failed through the state machine. Repairs are
row-locked, bounded, and idempotent.

## Failure and outage semantics

Business/runtime errors such as invalid model output, permission denial, tool-step exhaustion, or
provider authentication failure are visible task/job failures and are not automatically retried.
Only a claim lost before durable business execution can be redispatched.

During Redis failure, the event consumer retries while database reconciliation and worker polling
continue. When PostgreSQL is unavailable, no new claim can be committed. Lease renewal failure
clears the AgentRuntime execution guard, which blocks subsequent model/tool steps; recovery
reconciles the durable state when PostgreSQL returns. An already-running bounded side effect may
finish before the guard is observed.

Forge targets an at-most-one active database claim under normal operation and an at-least-once
recoverable infrastructure workflow. It does not claim exactly-once external side effects.
