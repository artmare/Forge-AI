# Task execution inspection

Forge exposes a read-only execution inspection endpoint at:

`GET /api/v1/tasks/{task_id}/inspection`

The endpoint assembles durable Task, TaskRun, AgentRun, ExecutionJob, DevelopmentExecution,
QAResult, AcceptanceVerification, ToolCall, TaskReview, and TaskDependency records. ExecutionJob
diagnostics include the current phase, bounded phase history, safe environment/checkpoint/working
tree state, normalized failure evidence, and bounded retry history. Inspection is read-only and
does not mutate or retry the Task.

## Failure normalization

For a failed Task, Forge selects the final persisted iteration and reports the most specific
supported failure category. A persisted `ExecutionJob.failure_evidence` record wins; otherwise the
service falls back through QA, command, ToolCall, AgentRun, `TaskRun.error`, and
`ExecutionJob.last_error`. Development iteration exhaustion is reported only when the Task is
FAILED, its current iteration reached `max_iterations`, and the final persisted QA decision is
FAIL. Command, tool, or provider failures are reported only when corresponding durable records
exist. If none of those sources classifies the incident, the API explicitly returns
`UNCLASSIFIED_RUNTIME_FAILURE` rather than `UNKNOWN` or an invented cause. Raw unexpected worker
exception type/message/traceback is retained in a bounded internal-only ExecutionJob field and is
deliberately omitted from public API schemas.

Acceptance evidence is presented per criterion. Missing final-iteration verification is represented
as UNVERIFIED with an explicit explanation; it is never converted to PASS or FAIL by the UI.

## Security boundary

Inspection responses omit model requests, model responses, provider response identifiers, raw Task
inputs, and unrestricted environment details. Tool arguments/results are recursively redacted for
credential-shaped keys. File contents are replaced with bounded metadata, common token and URL
credential patterns are removed, and command stdout/stderr are bounded again before serialization.
Existing ToolRegistry permissions, command allowlists, workspace isolation, and AgentRuntime
boundaries remain unchanged.

## User interface

Every Mission Task has an **Inspect execution** action. The inspection drawer contains the Task
metadata, dependency links, a durable failure summary, final acceptance evidence, an ExecutionJob
phase/recovery timeline, and one expandable section per execution iteration. Failed dependencies
link directly to their upstream failure.
Mission Overview also surfaces failed development work as an attention card. On narrow screens the
drawer occupies the viewport and retains keyboard-accessible close and evidence controls.
