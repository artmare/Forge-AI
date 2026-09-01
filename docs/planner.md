# Mission Planner

`MissionPlanner` is separate from `AgentRuntime`. It reuses the provider abstraction, model registry,
paid-call policy, structured response parsing, token usage, and cost estimator, but it has no tools
and cannot mutate domain topology.

The planner receives only Mission fields, a capability manifest derived from enabled `ToolRegistry`
definitions, supported runtime roles (`GENERAL`, `RESEARCHER`), conservative limits, and explicit
unavailable-capability flags. It returns the strongly typed `PlanProposal`: one Project, plan-local
Agents, plan-local Tasks, and dependency edges referencing Task keys. The provider never receives
credentials, environment variables, host paths, unrelated records, or database access.

`MODEL_PLANNER` resolves the `planner` alias. The default `MockModelProvider` emits a deterministic
two-Task filesystem proposal. OpenAI planning goes through the existing `ALLOW_PAID_MODEL_CALLS`
gate; `MODEL_PROVIDER=mock` and `ALLOW_PAID_MODEL_CALLS=false` remain the defaults.

`PlanValidator` deterministically enforces agent/task/dependency limits, unique local keys and edges,
supported roles, configured model aliases, permissions from enabled real tools, valid references,
acceptance criteria, bounded iterations, no self-edges, and DAG acyclicity.

Schema-invalid or semantically invalid output is stored as an `INVALID` PlanningRun and is never
materialized. PlanningRun history is the plan version history; a successful proposal becomes an
immutable activation snapshot.

Planning transactions are short. Forge locks and commits the Mission plus `RUNNING` PlanningRun,
calls the provider outside the transaction, then locks and persists the result. A partial unique index
allows one `CREATED`/`RUNNING` PlanningRun per Mission. `PLANNING_RUN_STALE_SECONDS` recovery fails
abandoned runs and returns the Mission to a replannable state when attempts remain.
