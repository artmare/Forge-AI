# Forge Dev Mode

Forge Dev Mode extends the existing agent runtime; it is not a parallel agent framework. The
hosted model proposes a structured result or one native tool request. Forge remains authoritative
for permissions, execution, task state, durable history, budgets, routing, and verification.

## Runtime flow

1. A Lead Engineer (`LEAD_ENGINEER`, normalized to the existing Developer runtime role) receives
   bounded task, Project Brain, code-context, review, and QA context.
2. `ModelRouter` filters enabled models by required capabilities. A Developer turn with tools
   requires TEXT, CODING, STRUCTURED_OUTPUT, and TOOL_CALLING; Lead Engineer also requires
   REASONING.
3. Dev Mode refreshes stale OpenRouter metadata, rejects paid or price-unknown candidates in
   free-only mode, and behaviorally probes bounded candidates. Routing is deterministic among
   candidates whose declared capabilities, probe results, and health state satisfy the turn.
4. The provider converts Forge tools to native function definitions. A native request is mapped
   back to its original Forge name.
5. `PermissionEngine` authorizes the request and `ToolExecutionService` creates a durable
   `ToolCall` before executing it in the UUID-scoped workspace.
6. The provider continuation receives the native call identity and the structured Forge
   observation.
7. `ExecutionTruthValidator` checks the final result against successful Forge observations before
   task completion. A false execution claim becomes a protocol failure and triggers bounded model
   fallback when another compatible candidate exists.

Model prose is never execution evidence. Durable `ToolCall`, development execution, Git,
filesystem, QA, and test state are authoritative.

For Dev Mode selections, `DEV_MODEL_ROUTING` events record required capabilities, the selected
provider/model and reason, rejected candidates, probe latency/status/failure category, cooldown
time, and token use. The task Dev Mode endpoint exposes these events beside model calls, tools,
rollovers, and specialist-call counts.

## OpenRouter

Set `OPENROUTER_ENABLED=true` and provide `OPENROUTER_API_KEY` through the runtime environment.
Never put keys in `MODEL_CATALOG` or tracked files.

`OpenRouterCatalogClient` lazily refreshes `/models` after
`OPENROUTER_CATALOG_TTL_SECONDS`. Refreshes are single-flight and bounded by the HTTP timeout. A
failed refresh retains the last known good catalog and enters a short retry delay. Refresh status,
age, last error code, and entry count are inspectable without exposing credentials.

Pricing metadata is authoritative when present: every advertised price component must be zero for
a model to count as free. The `:free` suffix is accepted only when pricing metadata is absent.
Unknown or paid models are disabled in free-only Dev Mode. OpenRouter requests also include a zero
provider-price ceiling, so a catalog/routing mistake cannot silently buy a paid endpoint.

Catalog metadata establishes only a DECLARED capability. Before a configured OpenRouter candidate
can serve a Dev Mode turn, `CapabilityProber` verifies the required behavior:

- `TOOL_CALLING`: the model must return a native `forge_probe.echo` call with the exact payload.
- `TOOL_CONTINUATION`: Forge supplies a synthetic success observation and requires a valid final
  continuation.
- `STRUCTURED_OUTPUT`: the response must validate against the actual `AgentTurnResponse` Pydantic
  contract.

The synthetic probe never dispatches a machine tool. Prose saying that the tool ran, malformed
arguments, another tool call after continuation, or approximate JSON all fail. Verified results use
`OPENROUTER_PROBE_TTL_SECONDS`; transient timeouts and 429s use a retry/cooldown interval rather
than permanently blacklisting the model. Probe calls consume the existing task call/token budgets.

Per-model `supported_parameters` in `MODEL_CATALOG` prevents the provider from sending unsupported
optional fields such as `response_format`, `tool_choice`, or `parallel_tool_calls`. A Dev Mode
profile must still declare TOOL_CALLING and STRUCTURED_OUTPUT to be eligible for tool turns.

Example profile:

```json
{
  "alias": "free-coder-a",
  "provider": "openrouter",
  "model": "vendor/model:free",
  "tier": "FREE",
  "paid": false,
  "capabilities": ["TEXT", "CODING", "REASONING", "TOOL_CALLING", "STRUCTURED_OUTPUT"],
  "supported_parameters": ["tools", "tool_choice", "response_format"]
}
```

Do not configure `openrouter/free` as the only profile: its selected upstream model may not satisfy
Forge's tool protocol. Use multiple explicit compatible profiles. Failures are tracked per
provider/model as rate limited, temporarily unavailable, timeout, protocol, invalid tool,
structured-output, authentication, quota, or unavailable states. Transient and protocol failures
enter a configurable cooldown; authentication and unavailable configuration fail closed.

## Project Brain and context

The existing PostgreSQL `project_knowledge_indexes` table is the Project Brain. Migration
`20260919_0019` adds a project summary, current state, lessons, and bounded context checkpoints to
the existing architecture, modules, contracts, decisions, constraints, and recent changes.

- L0: immutable AgentRun, ToolCall, development execution, event, review, and QA records.
- L1: task summaries and context checkpoints.
- L2: accepted decisions, constraints, lessons, modules, and current project state.
- L3: compact project and architecture summaries.

Compaction never deletes L0. Current source, schema, and real execution output outrank all summaries.
Memory commits occur through `ProjectKnowledgeService` after approved development work. Checkpoints
contain bounded goal, completed work, diff, decisions, tests, failures, questions, and next action.

At the configured fraction of the selected model's context limit (or the conservative configured
fallback), Forge writes a compact checkpoint and creates a fresh transport context for the same
task. The handoff contains task/goal/phase, completed authoritative tool results, inspected and
modified files, diff summary, test state, failures, open questions, counters, and next action. It
does not copy the transcript. Agent/task identity, durable observations, tool-step count,
duplicate/fallback/budget state, and permissions remain unchanged. Successful mutation fingerprints
survive the rollover, so an already-executed write/patch/commit cannot be replayed.

## Lead Engineer and specialists

The Lead Engineer uses the existing bounded Developer loop: inspect, edit, deterministic validation,
Git status/diff, review, and structured completion. It owns implementation decisions. Supported
specialist instruction modes are Architect, Code Reviewer, Product/UX, Creative Director, and
Marketing/Strategy. The Lead Engineer invokes `specialist.advise` only when it has that explicit
permission. The request contains only a role, question, constraints, and bounded relevant context;
the specialist receives no tools. Its findings, risks, recommendations, blocking issues, and
optional suggestions must validate against `SpecialistResult`. Calls are durably reserved before
the provider request, count against task model budgets, default to two per task, and cannot nest.
The Lead Engineer remains responsible for every repository action and completion decision.

## Self-development safety

`SelfDevelopmentWorkspaceManager` is Forge orchestration infrastructure, not a model-accessible
shell tool. When self-development is explicitly enabled, it verifies a Git repository and clean
tracked source, creates `forge-dev/<task>` from an exact base revision, and exposes only the new
worktree as the project's existing UUID-scoped workspace. The repository path must exactly equal
the configured allowlist; repository and worktree roots must be absolute, disjoint, and free of
symlink components. Git subprocesses use bounded timeouts, disabled hooks/signing, and isolated
configuration. It never checks out or commits on the stable branch.

A self-development task must explicitly set `input.self_development=true`. The lifecycle supports
create/prepare, inspect/use, verified checkpoint, complete, abandon-with-retention, and cleanup.
Cleanup refuses dirty work and retains the review branch. Completion requires a successful Lead
Engineer result, current independent QA PASS, mutation evidence for every changed file, a clean
post-checkpoint worktree, and a `forge-dev/<task-id>` commit. The resulting event includes branch,
worktree, base/head, diff summary, changed files, checkpoint commit, and `HUMAN_REQUIRED`
promotion. Forge never merges it.

Both `FORGE_DEV_MODE_ENABLED` and `FORGE_SELF_DEVELOPMENT_ENABLED` default to false. Deployment must
also provide explicitly confined repository and worktree paths before wiring self-development into
a production workflow.

Example deployment configuration (container paths shown):

```dotenv
FORGE_DEV_MODE_ENABLED=true
FORGE_DEV_FREE_ONLY=true
FORGE_SELF_DEVELOPMENT_ENABLED=true
FORGE_DEV_REPOSITORY_PATH=/forge-source
FORGE_DEV_ALLOWED_REPOSITORY=/forge-source
FORGE_DEV_WORKTREE_ROOT=/workspaces
TOOL_WORKSPACE_ROOT=/workspaces
OPENROUTER_ENABLED=true
OPENROUTER_FREE_ONLY=true
OPENROUTER_API_KEY=provided-by-secret-manager
MODEL_CATALOG=[{"alias":"free-coder","provider":"openrouter","model":"vendor/model:free","tier":"FREE","paid":false,"capabilities":["TEXT","CODING","REASONING","TOOL_CALLING","STRUCTURED_OUTPUT"]}]
```

Mount the source repository at `/forge-source` and the worktree volume at `/workspaces`. Do not
point either setting at `/`, a home directory, or the running application directory unless that
mount is the explicitly allowlisted source repository.

The repository includes an explicit Compose override. It refuses to render unless the host path is
set, so ordinary application startup cannot silently enable self-development:

```text
FORGE_DEV_REPOSITORY_HOST_PATH=/absolute/path/to/ForgeAI \
docker compose -f docker-compose.yml -f docker-compose.self-dev.yml up -d --build
```

## Browser verification

`browser.capture` is an opt-in Forge tool for built static HTML. A separate browser-runner
container has no network, capabilities, credentials, or write access to project workspaces. It
copies a bounded secret-filtered snapshot into a disposable Landlock sandbox, blocks service
workers and all requests except files under `forge.local`, renders with Chromium, and returns a PNG
artifact hash, title, console errors, and viewport. The API serves an artifact only when a
successful ToolCall owns the matching reference. A visual correctness claim without this evidence
is rejected by `ExecutionTruthValidator`; successful capture proves rendering occurred, not that a
design is good. Product QA remains separate from browser and Developer evidence.

## Important configuration

- `MODEL_FALLBACK_MAX_CANDIDATES=3`
- `MODEL_TRANSIENT_MAX_ATTEMPTS=3`
- `MODEL_PROVIDER_HEALTH_COOLDOWN_SECONDS=300`
- `OPENROUTER_CATALOG_TTL_SECONDS=3600`
- `OPENROUTER_PROBE_TTL_SECONDS=3600`
- `OPENROUTER_PROBE_TIMEOUT_SECONDS=30`
- `OPENROUTER_FREE_ONLY=true`
- `FORGE_DEV_FREE_ONLY=true`
- `FORGE_DEV_CONTEXT_CHECKPOINT_RATIO=0.7`
- `FORGE_DEV_CONTEXT_LIMIT=32768`
- `FORGE_DEV_MAX_ROLLOVERS=4`
- `FORGE_DEV_SPECIALIST_LIMIT=2`
- `FORGE_DEV_SPECIALIST_CONTEXT_CHARS=12000`
- `FORGE_BROWSER_ENABLED=false`
- `ALLOW_PAID_MODEL_CALLS=false`

Budgets count free calls and tokens even when estimated monetary cost is zero.

## Tests and troubleshooting

Run the API suite with:

```text
docker compose --profile test run --build --rm forge-api-test
```

On hosts requiring privileged Docker, use the locally approved `sudo docker-compose` equivalent.
Provider-only tests are in `test_openrouter_provider.py`; routing/health tests are in
`test_model_router.py`; execution-truth, discovery, Project Brain, and self-development tests use
their correspondingly named files.

The live harness is opt-in and never runs in the normal suite:

```text
FORGE_RUN_LIVE_OPENROUTER_TESTS=true \
docker compose --profile test run --build --rm \
  -e OPENROUTER_API_KEY -e FORGE_RUN_LIVE_OPENROUTER_TESTS \
  forge-api-test pytest -q -s -rs tests/test_live_openrouter.py
```

It discovers free candidates, runs all three probes, and only after one passes creates
`forge_probe.txt` in a disposable UUID workspace through the real Forge tool loop. Set
`FORGE_LIVE_OPENROUTER_CODE_CHANGE=true` to enable Stage 4 after Stage 3 succeeds; Stage 4 creates
a temporary sample Git repository, repairs its deliberately failing test through the isolated
self-development workflow, runs QA, and checkpoints the review branch. It never targets Forge's
source repository.

If a model says it changed a file but Forge reports `UNVERIFIED_EXECUTION_CLAIM`, inspect ToolCall
history. The correct fix is a successful native tool request—not relaxing validation. For repeated
429s, inspect per-model health and add another explicit free, capability-compatible profile. Never
enable paid escalation merely to hide a free-pool reliability problem.

If catalog refresh fails, inspect its last error and age; the previous catalog remains active. If
all probes fail, distinguish `rate_limited`/`temporarily_unavailable` from protocol failures and
wait for the recorded retry time or add another explicit free candidate. If a context rollover is
rejected, inspect `DEV_CONTEXT_ROLLOVER` and task-wide call/tool limits. If self-development refuses
completion, inspect current QA, Developer ToolCalls, unexpected changed files, and the worktree
status before retrying. Browser capture requires the `browser` Compose profile and remains disabled
unless `FORGE_BROWSER_ENABLED=true`.

## Security and cost boundaries

Models cannot select a repository path, run a probe tool, supply arbitrary shell, merge a branch,
or promote a checkpoint. Workspace and symlink confinement remains in `WorkspaceManager`; tests run
in disposable, network-isolated Landlock sandboxes; typed Git operations alone see worktree Git
metadata. Browser execution is separately network-isolated. Specialist, rollover, tool, retry,
fallback, execution, token, and model-call limits are all bounded. API/event output contains model
and routing diagnostics but never provider credentials or raw secrets.
