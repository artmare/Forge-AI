# Agent worker runtime

`forge-agent-worker` is the dedicated Phase 07 execution process. FastAPI continues serving
management and explicit debug execution, while autonomous AgentRuntime work happens outside the
HTTP request lifecycle.

## Process lifecycle

At startup a worker generates a non-sensitive random key, inserts a `WorkerNode`, emits
`WORKER_ONLINE`, and writes its database ID to an internal container health file. Registration
exposes only the random key, status, configured concurrency, timestamps, and safe runtime
metadata. Hostnames, network addresses, environment variables, provider keys, and model/tool
content are not registered.

Workers heartbeat every `WORKER_HEARTBEAT_INTERVAL_SECONDS`. Container health requires the
process health file, a reachable database, an `ONLINE` row, and a fresh heartbeat. On shutdown the
worker changes `ONLINE → DRAINING`, stops new claims, allows active slots to finish within
`WORKER_SHUTDOWN_GRACE_SECONDS`, then becomes `OFFLINE`. If the grace period expires, leases are
left for deterministic recovery rather than being reassigned immediately.

## Claim and execution flow

Each bounded slot polls at `WORKER_POLL_INTERVAL_MS`:

1. lock the persistent autonomy-control row;
2. reject claims while paused or at the global cap;
3. select the highest-priority eligible pending job using `FOR UPDATE SKIP LOCKED`;
4. set `CLAIMED`, worker/lease fields, and increment the infrastructure attempt;
5. commit;
6. recheck autonomy, task, and agent in a second short transaction;
7. set the job `RUNNING` and agent `BUSY`, then commit;
8. execute the existing AgentRuntime with lease guards;
9. persist job success/failure and return the agent to `IDLE`.

AgentRuntime remains responsible for `QUEUED → IN_PROGRESS → REVIEW/FAILED`, TaskRun creation and
closure, AgentRuns, structured model output, tool permission refresh, cancellation checks, usage,
cost, and events. The worker does not duplicate or bypass it.

## Leases and fail-closed behavior

The worker renews each running lease every `JOB_LEASE_RENEW_INTERVAL_SECONDS`, before the
`JOB_LEASE_SECONDS` expiry. Renewal is a short conditional row update. If renewal fails or
ownership changes, an in-memory guard prevents the runtime from starting another model turn or
tool call. The current bounded call cannot be rolled back; the orchestrator later applies the
documented lease-recovery rules.

Logs carry worker, job, task, task-run, agent, slot, status, and duration identifiers where
available. They do not include secrets or full model/tool payloads.

## Agent and cancellation behavior

Only `CREATED` or `IDLE` agents are claimable. A claim becomes `RUNNING` only after the agent is
locked and changed to `BUSY`. Completion or task failure normally returns it to `IDLE`; one task
failure does not mark the agent `FAILED`. A `STOPPED`, `PAUSED`, or `FAILED` agent gets no new
work. A status change before start fails/cancels the job safely. Current bounded work is allowed
to finish unless task cancellation or lease loss is observed.

Task cancellation is checked by AgentRuntime between model/tool steps. Pending jobs whose tasks
are cancelled become `CANCELLED` through reconciliation. A claimed job is rechecked before start.

## Manual execution compatibility

`POST /api/v1/tasks/{id}/execute` remains an explicit debug path. A database guard serializes it
against worker claims, cancels a pending autonomous job, temporarily marks the agent busy, and
rejects the request if a worker already owns the task. The task state-machine lock remains the
final race defense, so only one TaskRun and one side-effecting runtime can win.

## Configuration

The main Phase 07 settings are:

```env
AUTONOMY_ENABLED=false
ORCHESTRATOR_RECONCILE_INTERVAL_SECONDS=5
ORCHESTRATOR_RECOVERY_INTERVAL_SECONDS=10
WORKER_POLL_INTERVAL_MS=500
WORKER_HEARTBEAT_INTERVAL_SECONDS=10
WORKER_STALE_SECONDS=30
AGENT_WORKER_CONCURRENCY=2
FORGE_MAX_CONCURRENT_TASKS=4
JOB_LEASE_SECONDS=60
JOB_LEASE_RENEW_INTERVAL_SECONDS=20
JOB_RETRY_BASE_SECONDS=5
EXECUTION_JOB_MAX_ATTEMPTS=3
WORKER_SHUTDOWN_GRACE_SECONDS=30
```

The worker retains `MODEL_PROVIDER=mock` and `ALLOW_PAID_MODEL_CALLS=false` by default. It mounts
only the named project-workspace volume required by Phase 06 tools. No shell, network, browser,
database, email, deployment, or external-API tool is added.

For integration validation only, the deterministic mock provider recognizes task input
`mock_scenario: filesystem_roundtrip` and emits one existing filesystem write, one existing read,
then a final result. This exercises the worker/tool boundary without adding tools or making a
paid/network model call.
