# Agent runtime

Phase 09.5 adds durable per-Task budgets, objective model routing/escalation, delta-context prompts,
file-hash context caching, duplicate-loop detection, and read-only efficiency metrics. See
[`efficient-development-runtime.md`](efficient-development-runtime.md). Provider policy and the
default paid-call protection are unchanged.

## Phase 09 specialized execution

`InstructionBuilder` now recognizes `DEVELOPER` and `QA` in addition to `GENERAL` and `RESEARCHER`.
Role selects instructions only; the Permission Engine still authorizes every tool call from current
database state. For `Task.kind=DEVELOPMENT`, AgentRuntime can defer its ordinary final transition so
the deterministic QA workflow evaluates real build/test evidence before `REVIEW`. QA or human
feedback is added to the next bounded context and tagged as revision metadata. Source contents remain
untrusted data and cannot expand runtime authority.

Phase 08 does not merge Mission planning into `AgentRuntime`. `MissionPlanner` reuses the provider,
registry, paid-call gate, structured output, usage, and cost primitives without receiving tools.
Materialized Agents store their validated execution `model_alias`; the worker passes that alias to the
unchanged bounded `AgentRuntime` tool loop.

Phase 05 introduced the explicit model boundary. Phase 06 extends that same manually triggered
path with a bounded, provider-neutral tool loop. It is not an autonomous worker: a caller must
queue an assigned Task and invoke `POST /api/v1/tasks/{task_id}/execute`.

## Safety defaults and provider policy

The application starts with:

```env
MODEL_PROVIDER=mock
ALLOW_PAID_MODEL_CALLS=false
STORE_MODEL_INPUTS=false
MODEL_REQUEST_TIMEOUT_SECONDS=120
AGENT_RUN_STALE_SECONDS=300
```

`MockModelProvider` is deterministic, local, free, and used by all automated execution tests. `OpenAIModelProvider` is optional and uses the Responses API with a Pydantic structured-output schema. It has `max_retries=0`; Forge performs no hidden retry and never falls back to another provider.

A paid OpenAI request is possible only when all of the following are explicit:

```env
MODEL_PROVIDER=openai
ALLOW_PAID_MODEL_CALLS=true
OPENAI_API_KEY=your-secret-in-dot-env-only
```

The key is passed only to the API and autonomous agent-worker containers, where the provider adapter runs. It is not passed to the web, test, publisher, consumer, or orchestrator containers. Automated tests and ordinary `make test` never make a real paid request. Model aliases are configured with `MODEL_DEFAULT`, `MODEL_FAST`, `MODEL_REASONING`, and `MODEL_CODING`; an unknown alias fails before task state changes.

## Execution boundary

`ContextBuilder` loads only the Task's company, optional project, assigned agent identity, Task data, acceptance criteria, and lifecycle metadata. It validates that all records belong to the same company. Agent configuration, permissions, credentials, unrelated tasks, and event history are excluded.

`InstructionBuilder` layers the controlled-tool policy, either the `GENERAL` or `RESEARCHER`
role, scoped task context, currently allowed tool schemas, prior bounded observations, and the
structured turn contract. Unsupported role strings safely use `GENERAL`. Providers receive only
`ModelRequest`; they never receive a database session, repositories, the state machine, Redis,
filesystem handles, shell/browser capabilities, or credentials other than the provider adapter's
API key.

Every turn is validated as either a tool request or final result by a Pydantic discriminated union,
not regex. The final result remains:

```json
{
  "status": "completed",
  "summary": "non-empty summary",
  "output": {},
  "notes": []
}
```

## Transaction and lifecycle sequence

1. Resolve the alias and enforce provider/payment policy before changing the Task.
2. Build scoped context and instructions.
3. Lock the Task row, transition `QUEUED -> IN_PROGRESS`, create its TaskRun and first `RUNNING` AgentRun, and atomically persist start events with outbox rows.
4. Commit and release all database locks.
5. Call the provider with the configured timeout.
6. Persist the model turn and usage. A tool request runs through the registry, permission engine,
   durable ToolCall lifecycle, and controlled executor before a bounded observation is added.
7. Repeat with a new AgentRun per provider invocation until a final result transitions the Task to
   `REVIEW`, or a runtime-fatal condition transitions it to `FAILED`.

The Task row lock serializes concurrent execute requests. Partial unique indexes enforce one active TaskRun per Task and one running AgentRun per TaskRun. Exactly one concurrent request can reach a provider.

## Persistence, privacy, and cost

AgentRun records retain provider/model identifiers, status, timestamps, structured output, normalized errors, token usage, and estimated cost. With the default `STORE_MODEL_INPUTS=false`, raw system and user prompts are not stored; SHA-256 integrity hashes plus non-secret request metadata are stored instead. Enabling input storage improves debugging but increases retention of task content and must be an explicit privacy decision.

`CostEstimator` is centralized. Mock cost is exactly zero. Paid-model cost is calculated only when `MODEL_PRICING` supplies a rate entry; unknown or incomplete pricing is returned as `null`, never guessed. Example configuration uses per-million-token input, cached-input, and output rates:

```env
MODEL_PRICING={"openai:gpt-5.4-mini":{"input":0.75,"cached_input":0.075,"output":4.5}}
```

Pricing changes over time; verify current provider pricing before configuring these values.

## Errors and recovery

Public errors retain the Forge envelope and use these stable codes:

- `MODEL_TIMEOUT`
- `MODEL_AUTH_ERROR`
- `MODEL_RATE_LIMIT`
- `MODEL_PROVIDER_ERROR`
- `INVALID_MODEL_OUTPUT`
- `PAID_MODEL_CALL_DISABLED`
- `MODEL_ALIAS_NOT_FOUND`
- `AGENT_RUNTIME_ERROR`
- `MAX_TOOL_STEPS_EXCEEDED`
- `AGENT_EXECUTION_CANCELLED`

Messages never include API keys, raw provider payloads, or prompt contents. A process crash after the start commit leaves a durable `RUNNING` AgentRun. Recover stale runs with:

```sh
make recover-agent-runs
```

The recovery service claims stale rows with `FOR UPDATE SKIP LOCKED`, marks the AgentRun and TaskRun failed, transitions an in-progress Task through `TaskStateMachine`, and writes the failure event/outbox atomically. The age threshold is `AGENT_RUN_STALE_SECONDS`.

## APIs and operations

- `POST /api/v1/tasks/{task_id}/execute`
- `GET /api/v1/agent-runs/{run_id}`
- `GET /api/v1/tasks/{task_id}/agent-runs?offset=0&limit=100`
- `GET /api/v1/tasks/{task_id}/runs?offset=0&limit=100`
- `GET /api/v1/agent-runs?offset=0&limit=20`
- `GET /api/v1/agent-runs/stats?agent_id={optional_agent_id}`

The dashboard shows recent executions, success/failure counts, tokens, and known estimated cost alongside event delivery. Structured logs include run/task IDs, provider/model metadata, timing, usage, cost, and normalized error codes without raw inputs or secrets.

Tool-loop contracts, permissions, observations, workspace boundaries, and recovery are documented
in [tool-system.md](tool-system.md).

## Phase boundary

Phase 06 still stops at explicit execution. It adds only controlled project-filesystem tools and
does not add Phase 07 behavior: no event-triggered pickup, planner, scheduler, orchestrator,
browser/shell/network tools, memory, delegation, automatic retry, or fallback routing.
# Phase 07 worker integration

Autonomous execution now invokes AgentRuntime from `forge-agent-worker`, outside FastAPI request
handling. The same runtime and TaskStateMachine remain in use for the manual debug endpoint. An
optional worker execution guard blocks future model/tool steps after durable job lease loss; it
does not weaken paid-model protection or Phase 06 permission/workspace checks.
