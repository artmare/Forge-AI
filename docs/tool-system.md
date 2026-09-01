# Tool and permission system

Phase 06 adds a backend-controlled tool loop to the explicit AgentRuntime execution path. The
model can request a structured action, but only Forge can validate, authorize, persist, claim, and
execute it. Tool execution is still manually initiated through
`POST /api/v1/tasks/{task_id}/execute`; there is no background pickup or orchestrator.

## Control boundary

```text
Model -> structured AgentTurn -> ToolRegistry -> input validation
      -> PermissionEngine -> durable ToolCall claim -> ToolExecutor
      -> bounded ToolObservation -> next model turn
```

The model receives safe tool names, descriptions, JSON schemas, risk metadata, and limitations.
It never receives Python handlers, host paths, database sessions, Redis clients, process access,
credentials, or unrestricted filesystem handles. Forge rechecks permission immediately before
each ToolCall; earlier prompt visibility is not authorization.

## Registry and definitions

`ToolRegistry` is the only lookup boundary. A `ToolDefinition` contains:

- stable name and description;
- Pydantic input and output models;
- required permission;
- `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL` risk metadata;
- timeout;
- enabled state;
- an internal handler that is never returned by the API.

The initial registry contains `filesystem.list` and `filesystem.read` at `LOW` risk and
`filesystem.write` at `MEDIUM` risk. No `CRITICAL` tool exists. `shell.run` is intentionally not
registered: a process allowlist is not equivalent to a network- and host-isolated sandbox, and the
three filesystem operations satisfy Phase 06 without exposing subprocess or environment state.

`GET /api/v1/tools` exposes only safe definition metadata and schemas. Unknown names produce
`TOOL_NOT_FOUND`; registered but globally disabled tools produce `TOOL_DISABLED`.

## Permission model and precedence

Known permission keys are strictly validated by the Agent create/update API:

```json
{
  "filesystem.list": false,
  "filesystem.read": false,
  "filesystem.write": false,
  "shell.run": false
}
```

New Agents receive this conservative snapshot. Unknown keys are rejected. A ToolCall is allowed
only when all applicable layers permit it, in this order:

1. `TOOLS_ENABLED` and the tool-specific global switch are enabled.
2. The tool exists, is registered, and has a valid typed argument object.
3. The current Agent identity and role are resolved from PostgreSQL. Role is contextual metadata in
   Phase 06 and never grants permission by itself.
4. The ToolExecutionContext exactly matches the persisted Task's company, project, assigned Agent,
   TaskRun, and AgentRun scope, and a project workspace exists.
5. The Agent's current JSONB permission value for the definition is exactly `true`.

Global policy always wins over Agent data. Permission data is refreshed from PostgreSQL for every
ToolCall, so emergency revocation affects the next request even when a previous model turn saw the
tool. A denial is persisted as `DENIED` and observed as `TOOL_PERMISSION_DENIED`; it performs no
filesystem action.

## Workspace and path security

Workspaces persist per Project at:

```text
/workspaces/{company_id}/{project_id}/
```

Compose mounts the dedicated `forge-workspaces` volume only into `forge-api`. No home directory,
Documents folder, host root, SSH directory, or general host path is mounted for tools. Empty
project directories are created on first use; Forge does not inject templates.

`WorkspaceManager` centralizes every path operation. It rejects empty/NUL paths, POSIX and Windows
absolute paths, drive-qualified paths, `..` traversal, resolved paths outside the workspace, and
any existing symlink component. Reads and writes require regular files; device files, FIFOs,
sockets, directories-as-files, and symlinks are rejected. Listings report symlinks as metadata but
never follow or reveal their target. API and tool output contains workspace-relative paths only.

Writes are UTF-8, bounded, and atomic within one directory using a flushed temporary file plus
`os.replace`. Reads are restricted to bounded UTF-8 text. The dedicated volume and lack of a tool
that creates symlinks form part of the security boundary; like most pathname APIs, checks and the
subsequent operation are not a defense against a separately privileged process racing the same
volume.

## Filesystem contracts and limits

- `filesystem.list({"path":"."})` returns name, relative path, type, and regular-file size.
- `filesystem.read({"path":"src/app.ts"})` returns relative path, UTF-8 content, and byte size.
- `filesystem.write({"path":"src/app.ts","content":"..."})` returns relative path, byte size,
  and whether the file was newly created.

Malformed inputs produce `TOOL_ARGUMENT_VALIDATION_FAILED`. Missing files, invalid types, path
escapes, encoding failures, and configured limits use stable error codes rather than exception
strings. Defaults are:

```env
TOOLS_ENABLED=true
FILESYSTEM_LIST_ENABLED=true
FILESYSTEM_READ_ENABLED=true
FILESYSTEM_WRITE_ENABLED=true
SHELL_RUN_ENABLED=false
FILESYSTEM_TOOL_TIMEOUT_SECONDS=10
TOOL_FILE_READ_MAX_BYTES=262144
TOOL_FILE_WRITE_MAX_BYTES=262144
TOOL_OBSERVATION_MAX_CHARS=50000
AGENT_MAX_TOOL_STEPS=10
TOOL_CALL_STALE_SECONDS=300
```

Every tool has an `asyncio` timeout. Timeout is returned as `TOOL_TIMEOUT`. Persisted results are
already tool-bounded; the observation sent to the next model turn has a separate character limit.
Truncation is explicit through `truncated=true` and `original_chars` metadata.

## Agent reasoning loop

Each provider invocation is one AgentRun. A single TaskRun can therefore contain:

```text
AgentRun (tool request) -> ToolCall -> AgentRun (tool request) -> ToolCall -> AgentRun (final)
```

The provider-neutral `AgentTurnResponse` is a Pydantic discriminated union between
`type="tool_call"` and `type="final"`; Forge does not parse free-form text. The mock provider
supports deterministic scripted sequences. Every AgentRun retains its own model usage and cost,
so existing aggregates remain correct.

Normal tool failures become structured observations and do not immediately fail the Task. The
model may choose another tool or return a final result. A provider failure, corrupted runtime
state, cancellation, or a further tool request after `AGENT_MAX_TOOL_STEPS` fails or terminates the
execution. Final success still transitions the Task through `TaskStateMachine` to `REVIEW`.

Before every model turn and every tool claim, Forge checks Task state. Cancellation prevents any
future claim or model turn as soon as the current bounded external operation returns. An already
running filesystem operation cannot be rolled back, but no later tool action begins.

## Persistence, concurrency, and recovery

ToolCall status is one of `REQUESTED`, `AUTHORIZED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DENIED`, or
`CANCELLED`. It references its AgentRun, TaskRun, Task, and Agent and stores typed arguments,
structured result/error, required permission, and timestamps in PostgreSQL.

Transactions are deliberately short:

1. persist request and event, then commit;
2. validate and authorize current policy, then commit;
3. lock the ToolCall, claim `AUTHORIZED -> RUNNING`, then commit;
4. execute outside the database transaction;
5. lock and persist the terminal result/event, then commit.

The row-lock claim forces a post-lock refresh from PostgreSQL. Competing executors cannot both
perform the same ToolCall ID, and a terminal retry returns the persisted result. This is
at-most-one active executor per durable ID, not a claim of exactly-once filesystem side effects.
A crash after an atomic write but before result persistence is intrinsically ambiguous.

`make recover-tool-calls` claims stale `RUNNING` calls through the partial stale index and
`FOR UPDATE SKIP LOCKED`, marks them `FAILED` with `TOOL_EXECUTION_INTERRUPTED`, and reconciles an
in-progress Task/TaskRun to failed. Recovery explicitly reports that side-effect state is unknown.

## Events, APIs, and observability

State changes use `EventFactory` and the existing transactional outbox:

- `TOOL_CALL_REQUESTED`
- `TOOL_CALL_AUTHORIZED`
- `TOOL_CALL_DENIED`
- `TOOL_CALL_STARTED`
- `TOOL_CALL_SUCCEEDED`
- `TOOL_CALL_FAILED`

Events share Task correlation and contain IDs, tool name, status, safe relative path metadata,
byte count, and normalized error code. File content is not included. Structured logs contain the
same identities plus duration without arguments, output content, environment, or credentials.

Read APIs are paginated where applicable:

- `GET /api/v1/tools`
- `GET /api/v1/tool-calls/{tool_call_id}`
- `GET /api/v1/tool-calls?offset=0&limit=20`
- `GET /api/v1/tool-calls/stats`
- `GET /api/v1/tasks/{task_id}/tool-calls?offset=0&limit=100`
- `GET /api/v1/agent-runs/{agent_run_id}/tool-calls?offset=0&limit=100`

The dashboard shows totals, successes, failures, denials, running calls, average duration, and
recent Agent/Task/Tool activity.

## Phase boundary

Phase 06 has no shell, browser, web/network, external API, email, social, payment, wallet,
deployment, GitHub-write, memory, vector, scheduler, automatic pickup, orchestrator, or autonomous
worker tool. Those capabilities require later explicit security designs.
