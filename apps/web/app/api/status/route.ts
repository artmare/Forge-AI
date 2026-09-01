import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

type HealthValue = "healthy" | "unhealthy";

interface HealthResponse {
  status: HealthValue;
  services: {
    api: HealthValue;
    database: HealthValue;
    redis: HealthValue;
  };
}

interface SystemResponse {
  name: string;
  version: string;
  status: HealthValue;
  companies: number;
  agents: number;
  tasks: number;
  task_states: Record<string, number>;
}

interface EventStatsResponse {
  pending: number;
  processing: number;
  published: number;
  failed: number;
  dlq: number;
  publisher_healthy: boolean;
  publisher_heartbeat_at: string | null;
}

interface EventResponse {
  id: string;
  type: string;
  topic: string;
  message: string;
  created_at: string;
}

interface AgentRunStatsResponse {
  total: number;
  by_status: Record<string, number>;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  estimated_cost: string | null;
}

interface AgentRunResponse {
  id: string;
  status: string;
  provider: string;
  model_alias: string;
  model_id: string;
  total_tokens: number;
  estimated_cost: string | null;
  task_title: string;
  agent_name: string;
  agent_role: string;
  error_code: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

interface ToolCallStatsResponse {
  total: number;
  by_status: Record<string, number>;
  average_duration_ms: number | null;
}

interface ToolCallResponse {
  id: string;
  tool_name: string;
  status: string;
  task_title: string;
  agent_name: string;
  duration_ms: number | null;
  error: { code?: string; message?: string } | null;
  created_at: string;
}

interface OrchestratorResponse {
  autonomy_enabled: boolean;
  orchestrator_online: boolean;
  queued_jobs: number;
  running_jobs: number;
  failed_jobs: number;
  active_workers: number;
  stale_workers: number;
  queued_tasks_without_jobs: number;
  last_reconciliation_time: string | null;
  total_jobs: number;
  job_counts: Record<string, number>;
  average_execution_duration_ms: number | null;
}

interface WorkerResponse {
  id: string;
  worker_key: string;
  status: string;
  concurrency: number;
  active_jobs: number;
  last_heartbeat_at: string;
}

interface ExecutionJobResponse {
  id: string;
  task_id: string;
  agent_id: string;
  worker_id: string | null;
  status: string;
  priority: string;
  attempts: number;
  max_attempts: number;
  last_error: { code?: string; message?: string } | null;
  created_at: string;
}

async function fetchBackend<T>(path: string): Promise<T> {
  const apiUrl = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
  const response = await fetch(`${apiUrl}${path}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(4_000),
  });

  if (!response.ok && response.status !== 503) {
    throw new Error(`Backend returned HTTP ${response.status}`);
  }

  return (await response.json()) as T;
}

export async function GET() {
  try {
    const [
      health,
      system,
      eventStats,
      recentEvents,
      agentRunStats,
      recentAgentRuns,
      toolCallStats,
      recentToolCalls,
      orchestrator,
      workers,
      executionJobs,
    ] =
      await Promise.all([
      fetchBackend<HealthResponse>("/api/v1/health"),
      fetchBackend<SystemResponse>("/api/v1/system"),
      fetchBackend<EventStatsResponse>("/api/v1/events/stats"),
      fetchBackend<EventResponse[]>("/api/v1/events?limit=10"),
      fetchBackend<AgentRunStatsResponse>("/api/v1/agent-runs/stats"),
      fetchBackend<AgentRunResponse[]>("/api/v1/agent-runs?limit=8"),
      fetchBackend<ToolCallStatsResponse>("/api/v1/tool-calls/stats"),
      fetchBackend<ToolCallResponse[]>("/api/v1/tool-calls?limit=8"),
      fetchBackend<OrchestratorResponse>("/api/v1/orchestrator/status"),
      fetchBackend<WorkerResponse[]>("/api/v1/workers?limit=8"),
      fetchBackend<ExecutionJobResponse[]>("/api/v1/execution-jobs?limit=8"),
    ]);

    return NextResponse.json(
      {
        status: health.status,
        services: health.services,
        system,
        eventStats,
        recentEvents,
        agentRunStats,
        recentAgentRuns,
        toolCallStats,
        recentToolCalls,
        orchestrator,
        workers,
        executionJobs,
        checkedAt: new Date().toISOString(),
      },
      { status: health.status === "healthy" ? 200 : 503 },
    );
  } catch {
    return NextResponse.json(
      {
        status: "unhealthy",
        services: {
          api: "unhealthy",
          database: "unhealthy",
          redis: "unhealthy",
        },
        system: {
          name: "Forge",
          version: "0.8.0",
          status: "unhealthy",
          companies: 0,
          agents: 0,
          tasks: 0,
          task_states: {
            CREATED: 0,
            QUEUED: 0,
            IN_PROGRESS: 0,
            REVIEW: 0,
            FIX_REQUIRED: 0,
            DONE: 0,
            FAILED: 0,
            CANCELLED: 0,
          },
        },
        eventStats: {
          pending: 0,
          processing: 0,
          published: 0,
          failed: 0,
          dlq: 0,
          publisher_healthy: false,
          publisher_heartbeat_at: null,
        },
        recentEvents: [],
        agentRunStats: {
          total: 0,
          by_status: { CREATED: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, CANCELLED: 0 },
          input_tokens: 0,
          output_tokens: 0,
          cached_tokens: 0,
          total_tokens: 0,
          estimated_cost: null,
        },
        recentAgentRuns: [],
        toolCallStats: {
          total: 0,
          by_status: {
            REQUESTED: 0,
            AUTHORIZED: 0,
            RUNNING: 0,
            SUCCEEDED: 0,
            FAILED: 0,
            DENIED: 0,
            CANCELLED: 0,
          },
          average_duration_ms: null,
        },
        recentToolCalls: [],
        orchestrator: {
          autonomy_enabled: false,
          orchestrator_online: false,
          queued_jobs: 0,
          running_jobs: 0,
          failed_jobs: 0,
          active_workers: 0,
          stale_workers: 0,
          queued_tasks_without_jobs: 0,
          last_reconciliation_time: null,
          total_jobs: 0,
          job_counts: { PENDING: 0, CLAIMED: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, CANCELLED: 0 },
          average_execution_duration_ms: null,
        },
        workers: [],
        executionJobs: [],
        checkedAt: new Date().toISOString(),
      },
      { status: 503 },
    );
  }
}
