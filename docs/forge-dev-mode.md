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
   task completion. A false execution claim becomes a protocol failure. During an approved
   recovery, Forge may request one bounded correction; it never treats model fallback as proof.

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
task. The continuation snapshot contains task/goal/phase, unique successful tool summaries,
modified-file hashes and bounded summaries, authoritative Git status/diff hashes and previews,
test state, unresolved failures, mutation fingerprints, remaining validation, remaining budgets,
progress since the prior rollover, and one concrete next action. It does not copy the transcript or
full file bodies. Agent/task identity, durable observations, tool-step count,
duplicate/fallback/budget state, and permissions remain unchanged. Successful mutation fingerprints
survive the rollover, so an already-executed write/patch/commit cannot be replayed.

Forge distinguishes model context pressure from the cumulative task input-token budget. Before
each provider call it estimates the actual transport payload, including the system prompt, native
tool exchanges, schemas, and a configurable next-turn reserve. Approaching the model context
threshold causes `DEV_CONTEXT_ROLLOVER`; crossing the task budget warning threshold creates one
early checkpoint per task execution so repeated native continuation history cannot consume the
rollover allowance on every subsequent turn. A compacted request that still exceeds the model window fails as
`MODEL_CONTEXT_LIMIT_EXCEEDED`. A compacted request that cannot fit the remaining cumulative task
budget stops as `TASK_INPUT_TOKEN_BUDGET_EXHAUSTED`. Rollover never resets token accounting.
Provider-reported input totals remain authoritative and include cached input where the provider
reports cached tokens as a subset; Forge exposes the cached subset separately and does not subtract
or double-count it.

Raw tool evidence remains in `ToolCall`, while model-visible test output, file content, diffs,
browser output, and other large observations use a bounded head/tail representation with the full
content hash and original size. Forge conservatively caches successful `filesystem.read`,
`filesystem.list`, `git.status`, `git.diff`, and `git.log` observations within a workspace
generation. An identical read can reuse a compact path/hash/size observation without creating a
fake `ToolCall`; the reuse is recorded as `DEV_OBSERVATION_REUSED`. Any successful or attempted
Forge operation that may change filesystem or Git state advances the generation and invalidates
the read cache. Permission checks still run before reuse. Project Brain retrieval bounds every
collection and excludes the current task's checkpoint when the explicit rollover handoff already
contains that state.

The runtime measures context bytes by system/runtime instructions, conversation prompt, native
tool schemas, structured-output schema, tool-call arguments, observations, handoff, source context,
and Project Brain components. These are diagnostics rather than a second token ledger; provider
usage remains authoritative for cumulative budgets. Reused observations, stale invalidations,
malformed-call repairs, stagnation signals, last useful action, rollover reasons, and progress
between rollovers are exposed by the Dev Mode task endpoint.

Malformed native arguments never execute. `TOOL_ARGUMENT_VALIDATION_FAILED` returns the tool name,
required/provided/missing/invalid fields, bounded validation issues, the relevant compact schema,
an invalid-call fingerprint, and a one-call repair instruction. When a model asks
`development.execute` for a Git inspection, Forge points it to the declared dedicated Git tool.
Repair attempts for the same invalid fingerprint are durable and bounded; repeated failure ends as
`TOOL_ARGUMENT_REPAIR_LIMIT_EXHAUSTED`. Alternating unchanged reads also contribute to semantic
stagnation. Forge first serves compact authoritative reuse; if no mutation, validation result,
resolved failure, or phase progress follows within the bound, execution stops as
`DEVELOPMENT_STAGNATION`.

Development bootstrap creates or recognizes a Git baseline before the first mutation. This
includes an empty initial repository through an allow-empty Forge checkpoint. If the real task
budget is exhausted after mutations, Forge creates a typed recovery checkpoint and
`DEV_BUDGET_HANDOFF` containing changed files, bounded diff/status, successful tool references,
failures, usage, remaining budget, mutation fingerprints, and the next action. An explicit human
can resume through `POST /api/v1/tasks/{task_id}/resume-input-budget` with
`additional_input_tokens`; consumed usage is retained, the task receives exactly the approved
addition, and durable mutation fingerprints prevent replay.
For a legacy budget failure created before handoffs existed, explicit resume first checkpoints the
current development tree and reconstructs mutation fingerprints from durable `ToolCall` records.
Resume is refused if that recovery checkpoint cannot be created.
An unrecoverable compacted model-context request uses the same checkpoint path and records
`DEV_CONTEXT_FAILURE_HANDOFF`, but it does not masquerade as cumulative task-budget exhaustion.
If that stop was specifically `MODEL_CONTEXT_ROLLOVER_LIMIT_EXHAUSTED` and the recovery checkpoint
exists, a human may approve one continuation with `POST /api/v1/tasks/{task_id}/resume-context`.
The approval does not add rollovers, calls, tool steps, or tokens, and it does not reset mutation
fingerprints or failure history. A second approval for the same failure is rejected.

Recovery keeps historical execution separate from current execution. Forge does not copy an old
`ToolCall` into the new `AgentRun`. Instead, `RecoveryEvidenceService` loads successful writes from
the same task and approved handoff, preserves their original AgentRun/ToolCall IDs, and revalidates
each current file against both the original write content hash and the task-tagged Git checkpoint.
Missing, changed, unsafe, cross-task, and cross-workspace artifacts are excluded. The compact
handoff labels accepted evidence as `HISTORICAL`; a structured historical claim must include that
scope and the original ToolCall ID. Current-run claims still require current-run observations, and
historical tests never prove tests after later source changes.

If a recovery model returns a premature unsupported final result, Forge stores the exact structured
response, real provider usage, failed claim, and evidence summary, then issues only a bounded claim
correction. Repetition stops as `EXECUTION_TRUTH_REPAIR_LIMIT_EXHAUSTED`. A human may approve one
new attempt for that specific failed AgentRun through
`POST /api/v1/tasks/{task_id}/resume-recovery`; the endpoint requires an earlier approved recovery
chain, refuses reuse for the same failed run, preserves the original iteration/model/token/tool
limits, and never changes the free-only policy.

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
- `FORGE_DEV_CONTEXT_RESERVE_TOKENS=2048`
- `FORGE_DEV_MAX_ROLLOVERS=4`
- `FORGE_DEV_TOOL_REPAIR_LIMIT=2`
- `FORGE_DEV_STAGNATION_LIMIT=4`
- `FORGE_DEV_SPECIALIST_LIMIT=2`
- `FORGE_DEV_SPECIALIST_CONTEXT_CHARS=12000`
- `FORGE_BROWSER_ENABLED=false`
- `ALLOW_PAID_MODEL_CALLS=false`
- `OPENROUTER_LIVE_PROBE_CANDIDATES=12`

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

It discovers and deterministically ranks a bounded pool of up to
`OPENROUTER_LIVE_PROBE_CANDIDATES` current free candidates, runs all three probes, and only after
one passes creates
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

For token failures, inspect the task failure's `budget_diagnostics`: selected model/provider,
configured or observed model context limit, rollover threshold and count, consumed and cached input
tokens, cumulative budget, remaining tokens, model calls, last rollover, and rollover disposition.
The legacy `MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED` code is classified as task budget exhaustion for
existing records; new executions use the explicit task-budget code. Do not fix this condition by
resetting counters. Approve a bounded resume only after reviewing the recovery checkpoint.

For rollover exhaustion, inspect the per-rollover reason, preflight estimate, progress interval,
reused observations, invalidations, malformed repair attempts, last useful action, and unresolved
blocker. A tiny task that repeatedly crosses only the task-budget warning threshold indicates a
runtime regression: that soft threshold is a one-time checkpoint, while model-window pressure may
still cause later rollovers. Use the context-resume endpoint only after confirming the checkpoint;
it is a single bounded continuation, not a retry-limit reset.

For recovery truth failures, inspect `DEV_RECOVERY_EVIDENCE_VERIFIED`,
`DEV_EXECUTION_TRUTH_REJECTED`, and `DEV_EXECUTION_TRUTH_REPAIR_REQUIRED`. The events expose only
bounded claims, provenance IDs, artifact hashes, usage, and invalidation reasons. They do not store
prompts or secrets. Use `resume-recovery` only for the exact failed AgentRun after reviewing those
events and the checkpoint.

## Security and cost boundaries

Models cannot select a repository path, run a probe tool, supply arbitrary shell, merge a branch,
or promote a checkpoint. Workspace and symlink confinement remains in `WorkspaceManager`; tests run
in disposable, network-isolated Landlock sandboxes; typed Git operations alone see worktree Git
metadata. Browser execution is separately network-isolated. Specialist, rollover, tool, retry,
fallback, execution, token, and model-call limits are all bounded. API/event output contains model
and routing diagnostics but never provider credentials or raw secrets.
