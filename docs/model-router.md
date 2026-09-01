# Multi-provider model routing and economics

Forge routes model work through deterministic application code. `AgentRuntime` and
`MissionPlanner` declare required capabilities; they do not select a provider directly. The
central `ModelRouter` filters the configured catalog by provider enablement, health, capability,
economic tier, and durable budget before a provider request is constructed.

## Provider-neutral catalog

`MODEL_CATALOG` is a JSON array. Each entry contains `alias`, `provider`, `model`, `tier`,
`capabilities`, `enabled`, `paid`, and (for paid entries) a conservative `max_call_cost` reservation.
Supported providers are `mock`, `openai`, `gemini`, and `openrouter`. OpenRouter uses the existing
OpenAI-compatible adapter boundary; Gemini uses its native `generateContent` HTTP contract. Both
adapters translate Forge's provider-neutral structured-output and tool schemas. Tool execution
still occurs only through `ToolRegistry`, `PermissionEngine`, and the bounded AgentRuntime loop.

Provider credentials stay in process environment variables. They are never placed in the catalog,
database, events, API responses, model prompts, or Control Center. Providers are disabled unless
their `*_ENABLED` flag is true. The global defaults remain:

```text
MODEL_PROVIDER=mock
ALLOW_PAID_MODEL_CALLS=false
GEMINI_ENABLED=false
OPENROUTER_ENABLED=false
```

An empty catalog preserves the legacy `MODEL_*` aliases under `MODEL_PROVIDER`. A production
catalog can contain several entries for the same role/capability so provider failover is possible.

## Free-first policy and escalation

Candidates are ordered `FREE`, `CHEAP`, then `PREMIUM`; stable alias ordering makes the decision
reproducible. Disabled, incompatible, unhealthy, quota-exhausted, unauthenticated, unavailable, or
over-budget entries are excluded. PREMIUM is refused unless durable runtime context supplies a
bounded justification such as capability unavailability, repeated deterministic failure, invalid
structured output, bounded implementation failure, architecture ambiguity, or provider failover.
The chosen tier, capabilities, reason, and fallback history are persisted on each AgentRun or
PlanningRun and on each individual ModelCallRecord.

Deterministic commands and cached Forge evidence do not consume a model-call record. The existing
efficient runtime still handles delta context, unchanged observation caching, duplicate-action
detection, and bounded iteration routing before the economic router runs.

## Budget hierarchy and reservations

Companies store capital, recorded revenue, a paid-AI cap, and recorded AI spend. Missions require
an explicit paid-AI budget; the default is zero. Tasks may add an optional narrower cap. A paid call
must fit every applicable non-zero company cap, the Mission cap, the optional Task cap, the global
paid-call switch, `MAX_AI_SPEND_PER_TASK`, `MAX_AI_SPEND_PER_MISSION`, per-Task paid-call count,
and per-Mission total-call count. Empty global spend limits mean “no additional operator cap”; they
never override the persistent Company/Mission/Task budget hierarchy.

Before provider I/O, Forge locks the budget owner rows briefly and persists a `STARTED`
ModelCallRecord with a conservative reservation. It commits before the network request. Completion
releases the reservation and records normalized usage and estimated cost. Concurrent calls count
active reservations, so overspend fails closed. Unknown paid pricing uses the catalog reservation;
Forge never invents a lower price. Calls refused before provider I/O create no usage charge.

New Missions and Companies start with zero paid budget. Users can set a Mission paid limit in the
advanced Create Mission controls, but paid use still requires an operator to enable paid calls.
This permits zero-capital operation with mock/free models and deterministic tools.

Hard call limits are configured with `MAX_MODEL_CALLS_PER_TASK`,
`MAX_MODEL_CALLS_PER_MISSION`, `MAX_FREE_MODEL_CALLS_PER_TASK`, and
`MAX_PAID_MODEL_CALLS_PER_TASK`. Forge stops before provider I/O when any limit is exhausted; it
does not silently raise a limit.

## Health, fallback, and failure semantics

Provider outcomes update a small durable health record. Authentication, exhausted quota, and model
unavailability prevent future selection until configuration/reconciliation changes the state;
temporary errors mark a target degraded and keep bounded retry behavior. Runtime retries count as
real model calls. If one candidate exhausts its bounded retry policy, Forge may move to the next
pre-authorized compatible candidate and persists why.

Normalized refusal/failure codes include `MODEL_PROVIDER_DISABLED`, `MODEL_PROVIDER_UNAVAILABLE`,
`MODEL_UNAVAILABLE`, `MODEL_CAPABILITY_UNAVAILABLE`, `MODEL_ESCALATION_REQUIRED`,
`MODEL_BUDGET_UNAVAILABLE`, `MISSION_MODEL_CALL_LIMIT_EXHAUSTED`,
`MODEL_CALL_BUDGET_EXHAUSTED`, `FREE_MODEL_CALL_LIMIT_EXHAUSTED`, and
`PAID_MODEL_CALL_LIMIT_EXHAUSTED`.

## Operational view

Read-only endpoints `/api/v1/economics/companies/{id}` and
`/api/v1/economics/missions/{id}` expose safe budgets, spend, tier counts, fallback/escalation
counts, and recent selection explanations. They intentionally exclude credentials, prompts,
provider payloads, authorization metadata, and provider response bodies. Mission Technical view
renders this as a compact Economics / AI Usage panel.

Future accounting may add realized business revenue and reinvestment policies. Phase 10 product
capabilities, autonomous spending decisions, payment rails, and provider credential management are
not part of this upgrade.
