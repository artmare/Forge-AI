# Efficient Development Runtime (Phase 09.5)

Phase 09.5 keeps the Phase 09 execution and security boundaries while reducing repeated model
work. PostgreSQL remains authoritative for budgets, routing decisions, context metadata, and
efficiency reporting. Deterministic commands are not model calls and do not consume model-call
budgets.

## Model routing and escalation

Model identifiers are configured once through aliases. Developer work starts on
`developer_coding`; deterministic QA uses no LLM; optional QA routes are `qa_fast` and `qa_visual`.
`developer_reasoning` is selected only after an objective signal, currently a repeated unchanged
tool action or multiple failed development iterations. Every escalation records the from/to
aliases, reason code, human-readable reason, objective signals, Task, and TaskRun. Forge does not
silently escalate merely because more capability is available.

## Durable Task budgets

`task_runtime_budgets` stores hard per-Task and per-iteration model-call limits, input/output token
limits, an optional estimated-cost ceiling, current route, warning state, and stop reason. Actual
usage is recomputed from durable `AgentRun` rows before every provider call. At the warning ratio
the next prompt asks for a bounded, evidence-first completion. At a hard limit Forge fails closed
with `MODEL_BUDGET_EXHAUSTED` and requests human intervention; it does not call the provider.

Defaults are 24 calls per Task, 24 per iteration, 240,000 input tokens and 48,000 output tokens.
`MODEL_PROVIDER=mock` and `ALLOW_PAID_MODEL_CALLS=false` remain unchanged.

## Delta context and file cache

The first turn receives the complete Task-scoped context. Later turns receive the Task, latest
review/QA delta, development profile, compact project knowledge, relevant file hashes/summaries,
and only the latest configured observations. Older observations remain durable but are represented
by tool-call ID, tool, and status instead of retransmitting their content.

`file_context_cache` records project-relative paths, SHA-256 content hashes, byte sizes and safe
structural summaries after controlled reads/writes. It never expands filesystem scope. Duplicate
detection is consecutive: an intervening tool action resets the loop signal, so reading a file
again after `filesystem.write` is treated as verification of changed state. Three consecutive
identical requests reach the default bounded threshold and stop with `DUPLICATE_TOOL_LOOP`.

Transient provider timeout, rate-limit, and provider-availability failures retry inside the same
AgentRun/TaskRun using a bounded exponential schedule (`MODEL_TRANSIENT_MAX_ATTEMPTS`, default 3).
Every retry is persisted on the AgentRun and emitted as `AGENT_RUN_RETRY_SCHEDULED`. Authentication,
invalid output, tool/permission, budget, and implementation failures are not automatically retried.

## Project knowledge compaction

After a human approves a DEVELOPMENT Task, Forge deterministically updates one bounded
`project_knowledge_indexes` record. It indexes approved file paths, acceptance contracts, approval
decisions, and a short result/change summary. Raw AgentRuns, ToolCalls, command evidence, QA and
review history remain durable; compaction is an index, not deletion or replacement of evidence.

## Metrics and benchmark

Read-only APIs expose Task and Mission calls, token classes, estimated cost, deterministic command
count, escalation history, duplicate/repeated-read counts, context bytes sent, and estimated bytes
avoided. `GET /tasks/{id}/efficiency-benchmark` compares actual delta prompt bytes with the
equivalent full-context retransmission baseline without making a model call.

## Security boundaries

No shell, browser, network, Git remote, package-install policy, workspace path, provider secret, or
tool permission was added or widened. The model still receives only registered tool schemas;
filesystem operations remain project-scoped and symlink/traversal protected.
