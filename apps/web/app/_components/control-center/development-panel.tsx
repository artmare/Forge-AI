import type { AcceptanceVerification, GraphNode } from "./types";
import { Icon, StatusBadge } from "./ui";

function latestVerifications(node: GraphNode) {
  const latest = new Map<number, AcceptanceVerification>();
  for (const item of node.acceptance_verifications) {
    const current = latest.get(item.criterion_index);
    if (!current || item.iteration >= current.iteration) latest.set(item.criterion_index, item);
  }
  return latest;
}

export function AcceptanceCriteria({ node }: { node: GraphNode }) {
  const verifications = latestVerifications(node);
  return <div className="acceptance-evidence"><p>Acceptance criteria</p><ul>{node.acceptance_criteria.map((criterion, index) => {
    const verification = verifications.get(index);
    const status = verification?.status ?? "UNVERIFIED";
    return <li key={`${index}-${criterion}`} data-status={status}>
      <span className="criterion-icon"><Icon name={status === "PASSED" ? "check" : status === "FAILED" ? "close" : "activity"}/></span>
      <span><strong>{criterion}</strong><small>{verification?.evidence_summary ?? "No verification evidence recorded yet."}</small></span>
      <StatusBadge status={status}/>
    </li>;
  })}</ul></div>;
}

function currentStage(node: GraphNode) {
  if (node.status === "REVIEW") return "Human review";
  if (node.status === "FIX_REQUIRED" || node.status === "QUEUED" && node.qa_results.at(-1)?.decision === "FAIL") return "Fixing";
  if (node.status === "IN_PROGRESS") return node.qa_results.length ? "QA" : "Coding / testing";
  if (node.status === "DONE") return "Approved";
  return node.status.replaceAll("_", " ");
}

export function DevelopmentPanel({ node }: { node: GraphNode }) {
  if (node.kind !== "DEVELOPMENT") return null;
  const latestQA = node.qa_results.at(-1);
  const latestChange = [...node.development_executions]
    .reverse()
    .find((item) => (item.change_summary?.total ?? 0) > 0)?.change_summary;
  return <section className="development-panel" aria-label={`Development evidence for ${node.title}`}>
    <div className="development-heading"><div><p>Development</p><strong>{currentStage(node)}</strong></div><span>Iteration {node.iteration} / {node.max_iterations}</span></div>
    {latestChange && <div className="change-summary"><strong>{latestChange.total ?? 0} files changed</strong><span>{latestChange.added ?? 0} added · {latestChange.modified ?? 0} modified · {latestChange.deleted ?? 0} deleted</span><div>{latestChange.files?.map((file) => <code key={file.path}>{file.path}</code>)}</div></div>}
    <div className="execution-history"><p>Command history</p>{node.development_executions.length ? <div>{node.development_executions.map((execution) => <article key={execution.id}><span><strong>{execution.action.replaceAll("_", " ")}</strong><small>{execution.duration_ms === null ? "Pending" : `${(execution.duration_ms / 1000).toFixed(1)}s`} · network {execution.network_enabled ? "enabled" : "off"}</small></span><StatusBadge status={execution.status}/></article>)}</div> : <small>No deterministic development execution recorded.</small>}</div>
    <div className="qa-result"><div><p>QA result</p>{latestQA ? <StatusBadge status={latestQA.decision}/> : <StatusBadge status="UNVERIFIED"/>}</div><strong>{latestQA?.summary ?? "QA has not evaluated this iteration."}</strong>{latestQA?.checks.map((check) => <article key={`${check.name}-${check.execution_id ?? check.evidence}`}><Icon name={check.status === "PASSED" ? "check" : "close"}/><span><strong>{check.name.replaceAll("_", " ")}</strong><small>{check.evidence}</small></span></article>)}{latestQA?.blocking_issues.map((issue) => <blockquote key={issue.title}><strong>{issue.title}</strong><span>{issue.description}</span></blockquote>)}</div>
    <AcceptanceCriteria node={node}/>
  </section>;
}
