# Development runtime

Phase 09 adds a controlled software-production stage without adding a generic shell. A `DEVELOPMENT`
Task remains an ordinary durable Task: the worker invokes `AgentRuntime`, but the runtime defers the
normal transition to `REVIEW`. Deterministic QA then decides whether the Task enters human review or
is moved through `FIX_REQUIRED` back to `QUEUED` with the findings available to the next Developer
iteration.

The model may request `development.execute` with a typed action identifier. It cannot submit an
executable, command line, environment variable, timeout, working directory, or network setting.
`DevelopmentExecutionService` resolves the project profile and Forge-owned command definition,
re-checks Task state and current permissions, persists `REQUESTED -> AUTHORIZED -> RUNNING`, commits,
and then dispatches to the isolated runner. Results are bounded and persisted as `SUCCEEDED`,
`FAILED`, `TIMED_OUT`, `DENIED`, or `CANCELLED` with events in the transactional outbox.

Conservative bounds are `DEVELOPER_MAX_STEPS=20`, `DEVELOPMENT_MAX_EXECUTIONS=8`, and
`DEVELOPER_MAX_FIX_ATTEMPTS=3`. Each action has a separate configured timeout. Output retains total
byte counts plus bounded head/tail excerpts; it never stores an unlimited build log. A stale
`RUNNING` execution is marked interrupted by orchestration recovery and is not automatically replayed
because its filesystem side effects may be ambiguous.

One `ProjectDevelopmentLease` serializes Developer and QA work for a project. It is acquired with the
ExecutionJob start transaction, renewed with the job lease, released on completion, and cleared when
expired. This avoids concurrent mutation of the persistent project workspace.

Development execution events are correlated to the Task and include only identifiers, action, state,
timing, and normalized errors. They do not include source files, full logs, or secrets.

## Workflow

```text
DEVELOPMENT Task QUEUED
  -> worker / project lease
  -> Developer model + controlled tools
  -> deterministic commands and local Git evidence
  -> deterministic QA
       PASS -> REVIEW -> human approve/request-fix
       FAIL -> FIX_REQUIRED -> QUEUED -> Developer revision
```

Human `FIX_REQUESTED` history remains durable. QA feedback is also loaded by `ContextBuilder`; both
are labeled as revision metadata on subsequent AgentRuns. Source files are explicitly untrusted data
and cannot change tools, permissions, command definitions, or provider policy.

## Deterministic project bootstrap

`ProjectBootstrapService` runs before the first Developer execution and again before deterministic
QA. It is application code, not an Agent or model. Under a per-project database row lock it creates
the project workspace, invokes the fixed `GIT_INIT` action when `.git` is absent, refreshes the
persisted development profile, and verifies `GIT_STATUS`. The runner still accepts only action enum
values resolved by Forge's command registry; bootstrap does not expose a command string or shell.

When a coherent skeleton exists, the service creates one local initial checkpoint through the fixed
`GIT_CHECKPOINT` action. Files that predate bootstrap are retained. The checkpoint stages controlled
workspace content while statically excluding `.env`, private-key/credential-shaped files,
`node_modules`, and Forge's managed Python environment. It uses a Forge-local identity on branch
`main`; it has no remote, push, URL, or credentials capability. Later changes remain visible through
`GIT_STATUS` and `GIT_DIFF`. An empty workspace is initialized but not committed until meaningful
files exist. Worker execution requests this checkpoint before `AgentRuntime`; it is no longer
deferred until QA. Therefore a failure inside the agent tool loop cannot skip the checkpoint for a
workspace that already contained a blueprint or source skeleton.

Autonomous jobs durably progress through `PREPARING`, `ENVIRONMENT_SETUP`, `CHECKPOINTING`,
`AGENT_START`, `EXECUTING`, `VERIFYING`, `QA`, and `FINALIZING`. Phase transactions are short and
do not hold database locks during model, filesystem, or command work. Terminal failures retain a
normalized public evidence record and a separate bounded internal exception record. Bootstrap and
pre-execution lease failures use the existing bounded ExecutionJob retry budget; retry history is
append-only and capped.

Bootstrap is idempotent. The project row lock prevents concurrent workers from performing two
initializations, and repeated calls refresh state without replacing source content or recreating a
checkpoint. Runtime failures are retried only by the bounded infrastructure-job policy
(`DEVELOPMENT_BOOTSTRAP_MAX_ATTEMPTS`, default 3). They do not create a TaskRun and therefore do not
consume a Developer iteration.

## Profile refresh and safe actions

Profile detection is not a one-time cache. It runs before Development execution, after every
successful `filesystem.write`, before QA, and during active-project recovery. `package.json`
transitions a stale or empty `UNKNOWN` profile to `NODE` immediately. `package-lock.json` selects
NPM; a Node project without a lock file also uses NPM by documented fallback policy. pnpm/yarn
markers are detected but their actions remain unavailable until the controlled runner supports those
package managers.

Node scripts are never invented. Only declared `test`, `build`, `lint`, and `typecheck` scripts map
to `NODE_TEST`, `NODE_BUILD`, `NODE_LINT`, and `NODE_TYPECHECK`. A missing required script yields
`DEVELOPMENT_ACTION_UNSUPPORTED`; the Developer may add the script when the Task requires it.

Recovery refreshes active non-terminal development projects. Projects containing terminal FAILED
development evidence are deliberately skipped so incident state remains read-only; a newly created
clean Task is bootstrapped on its own execution path.

## Fresh-project filesystem contract

Developer tool paths are always relative to the assigned Company/Project workspace. The three
filesystem operations intentionally have different path semantics:

- `filesystem.list(".")` lists workspace-root metadata; list accepts directories only.
- `filesystem.read("package.json")` reads one existing regular UTF-8 file; it rejects directories.
- `filesystem.write("package.json", content)` creates or replaces one regular UTF-8 file. The
  target does not need to exist, and missing relative parent directories such as `src/`, `tests/`,
  or `scripts/` are created safely.

`filesystem.write` does not create arbitrary directories, accept absolute paths, traverse outside
the workspace, or follow symlinks. A Developer assigned to create a fresh scaffold must durably
write the required files before returning a final result; repeated unchanged directory inspection
is not implementation progress.

For OpenAI, allowed Forge tools are transported as strict native function definitions with one
function request permitted per model turn. The provider maps the transport-safe function name back
to the registered Forge tool name; ToolRegistry validation, current PermissionEngine policy,
workspace resolution, and durable ToolCall execution still happen inside Forge. File content is
therefore a typed `filesystem.write` argument rather than model-authored tool-protocol text. Stored
AgentRun request metadata records only the allowed Forge tool names unless model-input storage is
explicitly enabled; it never records provider credentials.

## QA preconditions and iteration accounting

Before QA, Forge requires a workspace, usable repository status, a recognized profile when supported
project markers exist, and resolvable deterministic actions required by acceptance criteria. A Forge
runtime defect such as failed Git initialization or profile reconciliation is persisted as
`INFRASTRUCTURE_UNVERIFIABLE`. Acceptance criteria remain `UNVERIFIED` with evidence beginning
`Verification blocked by Forge development runtime:`. This outcome does not requeue the Developer
through `FIX_REQUIRED` and cannot burn all fix iterations.

Missing application manifests/scripts and test/build assertion failures are
`IMPLEMENTATION_FAILURE`: the Developer had a meaningful opportunity to change code, so the normal
bounded revision iteration is consumed. Forge infrastructure retries and Task business iterations
remain separate; exhaustion of the former ends in a visible normalized infrastructure failure rather
than an infinite retry loop.
