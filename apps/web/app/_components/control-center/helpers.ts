import type { Agent, ForgeEvent, Graph, GraphNode, Mission, PlannerErrorDetails, StatusPayload, ToolCallGraph } from "./types";

export async function forge<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/forge/${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const payload = (await response.json()) as T & { error?: ForgeErrorDetails };
  if (!response.ok) {
    throw new ForgeRequestError(
      payload.error?.message ?? `Forge returned HTTP ${response.status}`,
      payload.error?.code ?? "forge_request_failed",
      response.status,
      payload.error ?? {},
    );
  }
  return payload;
}

export interface ForgeErrorDetails extends PlannerErrorDetails {
  planning_run_id?: string;
  mission_id?: string;
}

export class ForgeRequestError extends Error {
  constructor(
    message: string,
    public code: string,
    public status: number,
    public details: ForgeErrorDetails = {},
  ) {
    super(message);
    this.name = "ForgeRequestError";
  }
}

const progressWeight: Record<string, number> = { DONE: 1, REVIEW: 0.9, IN_PROGRESS: 0.5, FIX_REQUIRED: 0.5, QUEUED: 0.1, CREATED: 0, FAILED: 0, CANCELLED: 0 };
export function weightedProgress(mission: Mission | null, graph: Graph | null) {
  if (mission?.status === "COMPLETED") return 100;
  const nodes = graph?.nodes ?? [];
  if (!nodes.length) return 0;
  return Math.round(nodes.reduce((sum, node) => sum + (progressWeight[node.status] ?? 0), 0) / nodes.length * 100);
}

const focusOrder = ["IN_PROGRESS", "REVIEW", "FIX_REQUIRED", "QUEUED", "FAILED", "CREATED", "DONE", "CANCELLED"];
export function focusNode(graph: Graph | null) {
  const nodes = graph?.nodes ?? [];
  return [...nodes].sort((a, b) => focusOrder.indexOf(a.status) - focusOrder.indexOf(b.status))[0] ?? null;
}

export function attentionNodes(mission: Mission | null, graph: Graph | null) {
  const nodes = (graph?.nodes ?? []).filter((node) =>
    ["REVIEW", "FIX_REQUIRED", "FAILED"].includes(node.status) ||
    Boolean(node.blocked_reason?.includes("FAILED")),
  );
  return { planReady: mission?.status === "PLAN_READY", nodes, count: nodes.length + (mission?.status === "PLAN_READY" ? 1 : 0) };
}

export function agentForNode(node: GraphNode | null, agents: Agent[]) { return agents.find((agent) => agent.id === node?.assigned_agent_id) ?? null; }

function meta(event: ForgeEvent, key: string) {
  const value = event.payload?.[key] ?? event.metadata?.[key];
  return typeof value === "string" ? value : null;
}

export function eventType(event: ForgeEvent) { return event.event_type ?? event.type ?? "SYSTEM_EVENT"; }
export function eventMessage(event: ForgeEvent) {
  const type = eventType(event);
  const path = meta(event, "path");
  const target = meta(event, "target_status") ?? meta(event, "to_status") ?? meta(event, "status");
  const explicit = typeof event.payload?.message === "string" ? event.payload.message : event.message;
  if (type === "TOOL_CALL_SUCCEEDED") return path ? `Agent completed work on ${path}` : "Agent completed a tool step";
  if (type === "TOOL_CALL_REQUESTED") return "Agent requested a workspace operation";
  if (type === "EXECUTION_JOB_CREATED") return "Ready work entered the execution queue";
  if (type === "EXECUTION_JOB_CLAIMED") return "A worker picked up the next task";
  if (type === "EXECUTION_JOB_STARTED") return "Task execution started";
  if (type === "EXECUTION_JOB_SUCCEEDED") return "Task execution completed successfully";
  if (type === "EXECUTION_JOB_FAILED") return "Task execution needs attention";
  if (type === "AGENT_RUN_STARTED") return "Agent started reasoning about the task";
  if (type === "AGENT_RUN_SUCCEEDED") return "Agent produced a result";
  if (type === "TASK_REVIEW_APPROVED") return "Human reviewer approved the result";
  if (type === "TASK_FIX_REQUESTED") return "Human reviewer requested a revision";
  if (type === "TASK_STATUS_CHANGED") return target === "REVIEW" ? "A task is ready for human review" : target ? `Task moved to ${target.replaceAll("_", " ").toLowerCase()}` : "Task status changed";
  if (type === "MISSION_PLAN_READY") return "Mission plan is ready for approval";
  if (type === "MISSION_ACTIVATED") return "Mission execution was activated";
  if (type === "MISSION_COMPLETED") return "Mission completed";
  if (type === "ORCHESTRATOR_PAUSED") return "Autonomous execution was paused";
  if (type === "ORCHESTRATOR_RESUMED") return "Autonomous execution resumed";
  return explicit || type.replaceAll("_", " ").toLowerCase().replace(/^./, (letter) => letter.toUpperCase());
}

export function waitState(node: GraphNode | null, status: StatusPayload | null) {
  if (!node) return "Waiting for ready work";
  if (node.status === "REVIEW") return "Waiting for human approval";
  if (node.status === "FIX_REQUIRED") return "Waiting for requested fix";
  if (node.status === "QUEUED" && !status?.orchestrator.autonomy_enabled) return "Ready work · autonomy paused";
  if (node.status === "QUEUED") return "Waiting for an available worker";
  if (node.status === "DONE") return "Waiting for the next dependency";
  return node.status.replaceAll("_", " ").toLowerCase();
}

export function toolPath(call: ToolCallGraph) { return typeof call.result?.path === "string" ? call.result.path : null; }
