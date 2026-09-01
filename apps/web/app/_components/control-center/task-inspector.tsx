"use client";

import { useEffect, useState } from "react";
import { forge } from "./helpers";
import type {
  InspectionAcceptance,
  InspectionCommand,
  InspectionIteration,
  InspectionToolCall,
  ProductQAResult,
  RuntimeEfficiency,
  TaskInspection,
} from "./types";
import { Icon, StatusBadge, formatTime } from "./ui";

interface Props {
  taskId: string | null;
  onClose: () => void;
  onInspectTask: (taskId: string) => void;
}
type InspectorTab = "summary" | "evidence" | "timeline" | "logs";
const inspectorTabs: Array<{ id: InspectorTab; label: string }> = [
  { id: "summary", label: "Summary" },
  { id: "evidence", label: "Evidence" },
  { id: "timeline", label: "Timeline" },
  { id: "logs", label: "Logs" },
];

function value(record: Record<string, unknown>, key: string) {
  const item = record[key];
  return typeof item === "string" || typeof item === "number" ? String(item) : null;
}

function json(valueToRender: unknown) {
  return JSON.stringify(valueToRender, null, 2);
}

function duration(started: string | null, completed: string | null, durationMs?: number | null) {
  if (durationMs !== undefined && durationMs !== null) return `${durationMs.toFixed(0)}ms`;
  if (!started || !completed) return "Duration unavailable";
  return `${Math.max(Date.parse(completed) - Date.parse(started), 0)}ms`;
}

function DevelopmentEnvironment({ inspection }: { inspection: TaskInspection }) {
  const profile = inspection.development_profile;
  if (!profile) return null;
  return <section className="inspection-environment" aria-label="Development environment">
    <div className="inspection-section-heading"><h3>Development environment</h3><StatusBadge status={profile.project_type}/></div>
    <dl>
      <div><dt>Profile</dt><dd>{profile.project_type}</dd></div>
      <div><dt>Package manager</dt><dd>{profile.package_manager}</dd></div>
      <div><dt>Repository</dt><dd>{profile.repository_initialized ? "Initialized" : "Unavailable"}</dd></div>
      <div><dt>Branch</dt><dd>{profile.repository_branch ?? "Not recorded"}</dd></div>
      <div><dt>Initial checkpoint</dt><dd>{profile.initial_checkpoint_created ? "Created" : "Not created"}</dd></div>
      <div><dt>Working tree</dt><dd>{profile.changed_files_count} files changed</dd></div>
      <div><dt>Detection source</dt><dd>{profile.detection_source}</dd></div>
      <div><dt>Last refreshed</dt><dd>{formatTime(profile.last_refreshed_at)}</dd></div>
    </dl>
    <div className="inspection-actions"><strong>Available actions</strong>{profile.available_actions?.length ? profile.available_actions.map((action) => <span key={action} data-available="true"><Icon name="check"/>{action}</span>) : <small>No deterministic actions detected.</small>}{profile.unavailable_actions?.map((action) => <span key={action}><Icon name="close"/>{action} unavailable</span>)}</div>
    {profile.bootstrap_error && <p role="alert">{profile.bootstrap_error.code} — {profile.bootstrap_error.message}</p>}
  </section>;
}

function AcceptanceList({ items }: { items: InspectionAcceptance[] }) {
  return <div className="inspection-criteria">
    {items.map((item) => <article key={`${item.criterion_index}-${item.id ?? "missing"}`} data-status={item.status}>
      <span>{item.criterion_index + 1}</span>
      <div><strong>{item.criterion}</strong><p>{item.evidence_summary}</p>{item.verifier && <small>{item.verifier.replaceAll("_", " ")}</small>}</div>
      <StatusBadge status={item.status}/>
    </article>)}
  </div>;
}

function CommandEvidence({ command }: { command: InspectionCommand }) {
  return <details className="inspection-evidence-item" data-evidence="command">
    <summary><span><strong>{command.action.replaceAll("_", " ")}</strong><small>{duration(command.started_at, command.finished_at, command.duration_ms)} · exit {command.exit_code ?? "—"}</small></span><StatusBadge status={command.status}/></summary>
    <div className="inspection-evidence-body">
      <dl><div><dt>Started</dt><dd>{formatTime(command.started_at)}</dd></div><div><dt>Finished</dt><dd>{formatTime(command.finished_at)}</dd></div><div><dt>Normalized failure</dt><dd>{command.error ? `${command.error.code} — ${command.error.message}` : "None recorded"}</dd></div></dl>
      {Object.keys(command.safe_arguments).length > 0 && <><p>Safe arguments</p><pre>{json(command.safe_arguments)}</pre></>}
      <p>stdout</p><pre>{command.stdout_excerpt || "No stdout was recorded."}</pre>
      <p>stderr</p><pre>{command.stderr_excerpt || "No stderr was recorded."}</pre>
      {command.output_truncated && <small>Output was bounded and truncated by the execution runtime.</small>}
    </div>
  </details>;
}

function ToolEvidence({ call }: { call: InspectionToolCall }) {
  return <details className="inspection-evidence-item" data-evidence="tool-call">
    <summary><span><strong>{call.tool_name}</strong><small>{duration(call.started_at, call.completed_at, call.duration_ms)}</small></span><StatusBadge status={call.status}/></summary>
    <div className="inspection-evidence-body">
      <p>Safe arguments</p><pre>{json(call.safe_arguments)}</pre>
      <p>Safe observation</p><pre>{call.safe_result ? json(call.safe_result) : "No result was recorded."}</pre>
      <p>Normalized failure</p><pre>{call.error ? `${call.error.code}\n${call.error.message}` : "None recorded"}</pre>
    </div>
  </details>;
}

function IterationCard({ iteration, current }: { iteration: InspectionIteration; current: number }) {
  const qa = iteration.qa_result;
  const changes = iteration.commands.filter((item) => item.change_summary);
  return <details className="inspection-iteration" open={iteration.iteration === current}>
    <summary><span><strong>Iteration {iteration.iteration}</strong><small>{iteration.agent_runs.length} AgentRuns · {iteration.commands.length} commands · {iteration.tool_calls.length} ToolCalls</small></span><StatusBadge status={iteration.task_run.status}/></summary>
    <div className="inspection-iteration-body">
      <section><h4>Developer changes</h4>{changes.length ? changes.map((item) => <pre key={item.id}>{json(item.change_summary)}</pre>) : <p>No durable change summary was recorded for this iteration.</p>}</section>
      <section><h4>AgentRuns</h4><div className="inspection-agent-runs">{iteration.agent_runs.map((run) => <article key={run.id}><span><strong>{run.agent.name}</strong><small>{run.agent.role} · {run.provider} / {run.model_alias} · {run.total_tokens} tokens</small></span><StatusBadge status={run.status}/>{run.error && <p>{run.error.code} — {run.error.message}</p>}{run.recovery_attempts.length > 0 && <details><summary>Provider recovery ({run.recovery_attempts.length})</summary><pre>{json(run.recovery_attempts)}</pre></details>}</article>)}</div></section>
      <section><h4>Command executions</h4>{iteration.commands.length ? iteration.commands.map((command) => <CommandEvidence key={command.id} command={command}/>) : <p>No deterministic command execution was recorded.</p>}</section>
      <section><h4>ToolCalls</h4>{iteration.tool_calls.length ? iteration.tool_calls.map((call) => <ToolEvidence key={call.id} call={call}/>) : <p>No tool calls were recorded.</p>}</section>
      <section className="inspection-qa"><div className="inspection-section-heading"><h4>QA evidence</h4><StatusBadge status={qa?.decision ?? "UNVERIFIED"}/></div>{qa ? <><strong>{qa.summary}</strong>{qa.failure_classification && <p><b>{qa.failure_classification.replaceAll("_", " ")}</b>{qa.failure_code ? ` · ${qa.failure_code}` : ""}</p>}<p>{qa.deterministic_checks_executed ? "Deterministic checks were executed." : "No deterministic checks were executed."}</p>{qa.checks.map((check, index) => <article key={`${value(check,"name")}-${index}`}><StatusBadge status={value(check,"status") ?? "UNVERIFIED"}/><span><strong>{value(check,"name")?.replaceAll("_", " ") ?? "Unnamed check"}</strong><small>{value(check,"evidence") ?? "No evidence was recorded."}</small></span></article>)}{qa.blocking_issues.map((issue, index) => <blockquote key={`${value(issue,"title")}-${index}`}><strong>{value(issue,"title") ?? "Blocking issue"}</strong><p>{value(issue,"description") ?? "No issue description was recorded."}</p></blockquote>)}</> : <p>QA did not record a result for this iteration.</p>}</section>
      <section><h4>Acceptance criteria at this iteration</h4><AcceptanceList items={iteration.acceptance_criteria}/></section>
      <section><h4>Human review history</h4>{iteration.reviews.length ? iteration.reviews.map((review) => <blockquote key={review.id}><strong>{review.decision.replaceAll("_", " ")}</strong><p>{review.feedback ?? "Approved without feedback."}</p><small>{formatTime(review.created_at)}</small></blockquote>) : <p>No human review decision was recorded for this iteration.</p>}</section>
      {iteration.task_run.error !== null && <section><h4>TaskRun error</h4><pre>{json(iteration.task_run.error)}</pre></section>}
    </div>
  </details>;
}

function FailureSummary({ inspection }: { inspection: TaskInspection }) {
  const failure = inspection.failure;
  if (!failure) return null;
  return <section className="failure-summary" aria-labelledby="why-task-failed">
    <div><Icon name="alert"/><span><p>Failure analysis</p><h3 id="why-task-failed">Why this failed</h3></span><StatusBadge status={failure.error_code}/></div>
    <strong>{failure.message}</strong>
    <dl>
      <div><dt>Category</dt><dd>{failure.category.replaceAll("_", " ")}</dd></div>
      <div><dt>Failing phase</dt><dd>{failure.failing_phase ?? "Not durably identified"}</dd></div>
      <div><dt>Final command</dt><dd>{failure.failing_command ? `${failure.failing_command} · exit ${failure.command_exit_code ?? "—"}` : "No failing command recorded"}</dd></div>
      <div><dt>QA reason</dt><dd>{failure.qa_failure_reason ?? "No QA failure reason recorded"}</dd></div>
      <div><dt>Failed at</dt><dd>{failure.failed_at ? formatTime(failure.failed_at) : "Timestamp unavailable"}</dd></div>
      <div><dt>Agent / attempt</dt><dd>{failure.agent_role ?? inspection.assigned_agent?.role ?? "Unknown role"} · attempt {failure.attempt ?? "—"}</dd></div>
      <div><dt>Evidence source</dt><dd>{failure.evidence_source.replaceAll("_", " ")}</dd></div>
    </dl>
    {failure.tool_failure && <p><b>Relevant tool failure:</b> {failure.tool_failure.code} — {failure.tool_failure.message}</p>}
    {failure.recovery_attempts.length > 0 && <details><summary>Recovery attempts ({failure.recovery_attempts.length})</summary><pre>{json(failure.recovery_attempts)}</pre></details>}
  </section>;
}

function ExecutionLifecycle({ inspection }: { inspection: TaskInspection }) {
  if (!inspection.execution_jobs.length) return <section><h3>Execution lifecycle</h3><p>No autonomous ExecutionJob was recorded; this may have been a manual execution.</p></section>;
  return <section className="inspection-lifecycle"><h3>Execution lifecycle</h3>{inspection.execution_jobs.map((job) => <details key={job.id} open={job.status === "FAILED"}>
    <summary><span><strong>{job.status} · {job.phase.replaceAll("_", " ")}</strong><small>attempts {job.attempts}/{job.max_attempts}</small></span><StatusBadge status={job.status}/></summary>
    <div className="inspection-evidence-body">
      <div className="inspection-phase-list">{job.phase_history.length ? job.phase_history.map((entry, index) => <article key={`${value(entry,"phase")}-${index}`}><StatusBadge status={value(entry,"phase") ?? "UNKNOWN"}/><span><strong>{value(entry,"phase")?.replaceAll("_", " ")}</strong><small>{value(entry,"at") ? formatTime(value(entry,"at")) : "Timestamp unavailable"}</small></span></article>) : <p>No phase history was recorded for this legacy execution.</p>}</div>
      {job.failure_evidence && <><h4>Normalized durable failure</h4><pre>{json(job.failure_evidence)}</pre></>}
      {job.retry_history.length > 0 && <><h4>Bounded recovery history</h4><pre>{json(job.retry_history)}</pre></>}
      <h4>Environment / checkpoint / working tree</h4><pre>{json({environment:job.environment_state,checkpoint:job.checkpoint_state,working_tree:job.working_tree_state})}</pre>
    </div>
  </details>)}</section>;
}

function EfficiencyPanel({ efficiency }: { efficiency: RuntimeEfficiency }) {
  const limit = efficiency.limits.model_calls;
  return <section className="inspection-efficiency" aria-label="Runtime efficiency">
    <div className="inspection-section-heading"><h3>Runtime efficiency</h3>{efficiency.budget_warning && <StatusBadge status="WARNING"/>}</div>
    <div className="efficiency-stats">
      <span><small>Model calls</small><strong>{efficiency.model_calls}{limit ? ` / ${limit}` : ""}</strong></span>
      <span><small>Tokens</small><strong>{efficiency.input_tokens + efficiency.output_tokens}</strong></span>
      <span><small>Cached input</small><strong>{efficiency.cached_tokens}</strong></span>
      <span><small>Estimated cost</small><strong>${Number(efficiency.estimated_cost).toFixed(4)}</strong></span>
      <span><small>Context avoided</small><strong>{Math.round(efficiency.context_reduction_ratio * 100)}%</strong></span>
      <span><small>Deterministic runs</small><strong>{efficiency.deterministic_executions}</strong></span>
    </div>
    <p>Route: <b>{efficiency.current_model_alias ?? "not selected"}</b> · repeated reads avoided {efficiency.repeated_reads_avoided} · duplicate turns detected {efficiency.duplicate_turns_detected}</p>
    {efficiency.stopped_reason && <p role="alert">Stopped for human intervention: {efficiency.stopped_reason}</p>}
    {efficiency.escalations.map((item) => <blockquote key={`${item.created_at}-${item.to_alias}`}><strong>{item.from_alias} → {item.to_alias}</strong><p>{item.reason}</p><small>{item.objective_signals.join(" · ")}</small></blockquote>)}
  </section>;
}

function ProductQAPanel({ results }: { results: ProductQAResult[] }) {
  const result = results.at(-1);
  if (!result) return null;
  return <section className="inspection-product-qa" aria-label="Product and visual QA">
    <div className="inspection-section-heading"><h3>Product / Visual QA · iteration {result.iteration}</h3><StatusBadge status={result.decision}/></div>
    <div className="product-dimensions">{result.dimensions.map((item) => <article key={item.dimension}><span><strong>{item.dimension}</strong><small>{item.evidence}</small></span><StatusBadge status={item.status}/></article>)}</div>
    <div className="viewport-contract">{result.viewport_contract.map((viewport) => <code key={viewport.name}>{viewport.name} · {viewport.width}×{viewport.height}</code>)}</div>
    {result.issues.map((issue, index) => <blockquote key={`${issue.category}-${issue.file}-${index}`}><div><StatusBadge status={issue.severity}/><strong>{issue.category.replaceAll("_", " ")}</strong></div><p>{issue.description}</p><small>{issue.file} · {issue.evidence}</small><b>Suggested fix: {issue.suggested_fix}</b></blockquote>)}
    {result.screenshot_references.length === 0 && <p>Rendered screenshot evidence was not available; render-dependent dimensions remain UNVERIFIED.</p>}
  </section>;
}

export function TaskInspector({ taskId, onClose, onInspectTask }: Props) {
  const [tabState, setTabState] = useState<{ taskId: string | null; tab: InspectorTab }>({ taskId: null, tab: "summary" });
  const tab = tabState.taskId === taskId ? tabState.tab : "summary";
  const [result, setResult] = useState<{
    taskId: string;
    inspection: TaskInspection | null;
    efficiency: RuntimeEfficiency | null;
    productQA: ProductQAResult[];
    error: string | null;
  } | null>(null);

  useEffect(() => {
    if (!taskId) return;
    let active = true;
    void Promise.all([
      forge<TaskInspection>(`tasks/${taskId}/inspection`),
      forge<RuntimeEfficiency>(`tasks/${taskId}/runtime-efficiency`),
      forge<ProductQAResult[]>(`tasks/${taskId}/product-qa-results`),
    ]).then(([inspection, efficiencyPayload, productQAPayload]) => {
      const efficiency = typeof efficiencyPayload?.model_calls === "number" ? efficiencyPayload : null;
      const productQA = Array.isArray(productQAPayload) ? productQAPayload : [];
      if (active) setResult({taskId, inspection, efficiency, productQA, error: null});
    }).catch((cause) => {
      if (active) setResult({taskId, inspection: null, efficiency: null, productQA: [], error: cause instanceof Error ? cause.message : "Task inspection is unavailable."});
    });
    return () => { active = false; };
  }, [taskId]);

  useEffect(() => {
    if (!taskId) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose, taskId]);

  if (!taskId) return null;
  const inspection = result?.taskId === taskId ? result.inspection : null;
  const efficiency = result?.taskId === taskId ? result.efficiency : null;
  const productQA = result?.taskId === taskId ? result.productQA : [];
  const error = result?.taskId === taskId ? result.error : null;
  return <div className="task-inspector-layer">
    <button className="task-inspector-backdrop" onClick={onClose} aria-label="Close task inspection"/>
    <section className="task-inspector" role="dialog" aria-modal="true" aria-labelledby="task-inspector-title">
      <header><div><p>Execution inspection</p><h2 id="task-inspector-title">{inspection?.title ?? "Loading Task…"}</h2>{inspection && <span>{inspection.kind} · {inspection.priority} priority · Iteration {inspection.iteration}/{inspection.max_iterations}</span>}</div><button onClick={onClose} className="icon-button" aria-label="Close task inspection"><Icon name="close"/></button></header>
      {inspection && <nav className="inspector-tabs" role="tablist" aria-label="Task inspection sections">{inspectorTabs.map((item) => <button key={item.id} role="tab" aria-selected={tab === item.id} onClick={() => setTabState({ taskId, tab: item.id })}>{item.label}</button>)}</nav>}
      <div className="task-inspector-body">
        {error && <div className="global-error" role="alert"><Icon name="alert"/><span>{error}</span></div>}
        {!inspection && !error && <div className="inspection-loading" aria-label="Loading task execution"><div/><div/><div/></div>}
        {inspection && tab === "summary" && <>
          <div className="inspection-task-meta"><StatusBadge status={inspection.status}/><span>{inspection.assigned_agent ? `${inspection.assigned_agent.name} · ${inspection.assigned_agent.role}` : "Unassigned"}</span></div>
          <p className="inspection-description">{inspection.description ?? "No Task description was recorded."}</p>
          <FailureSummary inspection={inspection}/>
          <DevelopmentEnvironment inspection={inspection}/>
          {efficiency && <EfficiencyPanel efficiency={efficiency}/>}
          <ProductQAPanel results={productQA}/>
        </>}
        {inspection && tab === "evidence" && <>
          {inspection.dependencies.length > 0 && <section className="inspection-links"><h3>Dependencies</h3>{inspection.dependencies.map((dependency) => <button key={dependency.id} onClick={() => onInspectTask(dependency.id)}><span><strong>{dependency.title}</strong><small>{dependency.blocked_reason ?? dependency.status}</small></span><StatusBadge status={dependency.status}/><Icon name="arrow"/></button>)}</section>}
          {inspection.dependents.length > 0 && <section className="inspection-links"><h3>Affected downstream Tasks</h3>{inspection.dependents.map((dependent) => <button key={dependent.id} onClick={() => onInspectTask(dependent.id)}><span><strong>{dependent.title}</strong><small>{dependent.blocked_reason ?? dependent.status}</small></span><StatusBadge status={dependent.status}/><Icon name="arrow"/></button>)}</section>}
          <section className="inspection-final-criteria"><h3>Final acceptance evidence</h3><p>{inspection.final_acceptance_criteria.filter((item) => item.status === "PASSED").length} of {inspection.final_acceptance_criteria.length} criteria passed in the final iteration.</p><AcceptanceList items={inspection.final_acceptance_criteria}/></section>
        </>}
        {inspection && tab === "timeline" && <>
          <ExecutionLifecycle inspection={inspection}/>
          <section className="inspection-timeline"><h3>Iteration timeline</h3>{inspection.iterations.length ? inspection.iterations.map((iteration) => <IterationCard key={iteration.task_run.id} iteration={iteration} current={inspection.iteration}/>) : <p>No TaskRuns were recorded.</p>}</section>
        </>}
        {inspection && tab === "logs" && <section className="inspection-logs"><h3>Commands and tool calls</h3>{inspection.iterations.map((iteration) => <div key={iteration.task_run.id} className="inspection-log-iteration"><h4>Iteration {iteration.iteration}</h4>{iteration.commands.map((command) => <CommandEvidence key={command.id} command={command}/>)}{iteration.tool_calls.map((call) => <ToolEvidence key={call.id} call={call}/>)}{!iteration.commands.length && !iteration.tool_calls.length && <p>No command or tool-call evidence was recorded.</p>}</div>)}{!inspection.iterations.length && <p>No execution logs were recorded.</p>}</section>}
      </div>
    </section>
  </div>;
}
