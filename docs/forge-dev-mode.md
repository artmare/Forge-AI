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
3. Routing is deterministic: compatible healthy FREE profiles first, then explicitly budgeted
   CHEAP profiles, then justified PREMIUM escalation. Paid use remains disabled by default.
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

## OpenRouter

Set `OPENROUTER_ENABLED=true` and provide `OPENROUTER_API_KEY` through the runtime environment.
Never put keys in `MODEL_CATALOG` or tracked files.

`OpenRouterCatalogClient` reads `/models`, classifies zero prompt/completion pricing as FREE, maps
advertised tool/structured/vision parameters, and caches results using
`OPENROUTER_CATALOG_TTL_SECONDS`. It deliberately does not guess coding or reasoning capability;
those capabilities must come from trusted configuration or probe results. Discovered profiles can
be merged into `ModelRegistry` without overriding operator-defined aliases. `OPENROUTER_FREE_ONLY`
defaults to true.

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

## Lead Engineer and specialists

The Lead Engineer uses the existing bounded Developer loop: inspect, edit, deterministic validation,
Git status/diff, review, and structured completion. It owns implementation decisions. Supported
specialist instruction modes are Architect, Code Reviewer, Product/UX, Creative Director, and
Marketing/Strategy. Specialist tasks should contain only the question, constraints, relevant files,
and current diff; the configured specialist limit defaults to two.

## Self-development safety

`SelfDevelopmentWorkspaceManager` is Forge orchestration infrastructure, not a model-accessible
shell tool. When self-development is explicitly enabled, it verifies a Git repository and clean
tracked source, creates `forge-dev/<task>` from an exact base revision, and exposes only the new
worktree as the project workspace. It never checks out or commits on the stable branch. Promotion is
a separate human-controlled operation.

Both `FORGE_DEV_MODE_ENABLED` and `FORGE_SELF_DEVELOPMENT_ENABLED` default to false. Deployment must
also provide explicitly confined repository and worktree paths before wiring self-development into
a production workflow.

## Important configuration

- `MODEL_FALLBACK_MAX_CANDIDATES=3`
- `MODEL_TRANSIENT_MAX_ATTEMPTS=3`
- `MODEL_PROVIDER_HEALTH_COOLDOWN_SECONDS=300`
- `OPENROUTER_CATALOG_TTL_SECONDS=3600`
- `OPENROUTER_FREE_ONLY=true`
- `FORGE_DEV_CONTEXT_CHECKPOINT_RATIO=0.7`
- `FORGE_DEV_SPECIALIST_LIMIT=2`
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

If a model says it changed a file but Forge reports `UNVERIFIED_EXECUTION_CLAIM`, inspect ToolCall
history. The correct fix is a successful native tool request—not relaxing validation. For repeated
429s, inspect per-model health and add another explicit free, capability-compatible profile. Never
enable paid escalation merely to hide a free-pool reliability problem.
