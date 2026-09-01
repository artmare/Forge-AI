import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ControlCenter } from "./control-center";
import type { Mission } from "./types";

class EventSourceStub {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  addEventListener() { /* transport is idle in this test */ }
  removeEventListener() { /* transport is idle in this test */ }
  close() { /* transport is idle in this test */ }
}

const mission: Mission = {
  id: "11111111-1111-4111-8111-111111111111", company_id: null, project_id: null,
  title: "Runtime verification", goal: "Persist, plan, activate, and expose execution state.",
  constraints: {}, context: {}, status: "DRAFT", planning_attempts: 0, max_planning_attempts: 3,
  failure_reason: null, progress: { total: 0, done: 0, review: 0, running: 0, queued: 0, blocked: 0, failed: 0 },
};
const status = {
  status: "healthy", services: { api: "healthy", database: "healthy", redis: "healthy" },
  system: { name: "Forge", version: "0.9.0", status: "healthy", companies: 0, agents: 0, tasks: 0, task_states: {} },
  eventStats: { pending: 0, processing: 0, published: 0, failed: 0, dlq: 0, publisher_healthy: true, publisher_heartbeat_at: null },
  recentEvents: [], agentRunStats: { total: 0, by_status: {}, input_tokens: 0, output_tokens: 0, cached_tokens: 0, total_tokens: 0, estimated_cost: null },
  recentAgentRuns: [], toolCallStats: { total: 0, by_status: {}, average_duration_ms: null }, recentToolCalls: [],
  orchestrator: { autonomy_enabled: false, orchestrator_online: true, queued_jobs: 0, running_jobs: 0, failed_jobs: 0, active_workers: 1, stale_workers: 0, queued_tasks_without_jobs: 0, last_reconciliation_time: null, total_jobs: 0, job_counts: {}, average_execution_duration_ms: null },
  workers: [], executionJobs: [], checkedAt: "2026-08-28T00:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("ControlCenter mission lifecycle", () => {
  it("persists, plans, activates, and routes to the created mission id", async () => {
    let created = false;
    let planned = false;
    let activated = false;
    const calls: Array<{ path: string; method: string }> = [];
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      calls.push({ path, method });
      const json = (payload: unknown, responseStatus = 200) => new Response(JSON.stringify(payload), { status: responseStatus, headers: { "content-type": "application/json" } });
      if (path === "/api/status") return json(status);
      if (path.includes("missions?archive=active")) return json(created ? [{ ...mission, status: activated ? "ACTIVE" : planned ? "PLAN_READY" : "DRAFT", planning_attempts: planned ? 1 : 0 }] : []);
      if (path.includes("missions?archive=archived")) return json([]);
      if (path === "/api/forge/missions" && method === "POST") { created = true; return json(mission, 201); }
      if (path.endsWith(`/missions/${mission.id}/plan`) && method === "POST") { planned = true; return json({ planning_run: { status: "SUCCEEDED" }, proposal: {}, validation: { valid: true, errors: [] } }); }
      if (path.endsWith(`/missions/${mission.id}/activate`) && method === "POST") { activated = true; return json({ ...mission, status: "ACTIVE", planning_attempts: 1 }); }
      if (path.endsWith(`/missions/${mission.id}`)) return json({ ...mission, status: activated ? "ACTIVE" : planned ? "PLAN_READY" : "DRAFT", planning_attempts: planned ? 1 : 0 });
      if (path.endsWith(`/missions/${mission.id}/plan`)) return json({ planning_run: { status: "SUCCEEDED", provider: "test", model_alias: "planner", estimated_cost: null, validation_result: { valid: true, errors: [] } }, proposal: null, validation: { valid: true, errors: [] } });
      return json({ error: { message: `Unhandled test request: ${method} ${path}` } }, 404);
    }));

    const user = userEvent.setup();
    render(<ControlCenter />);
    await user.click(await screen.findByRole("button", { name: /New mission/ }));
    await user.type(screen.getByLabelText("Mission title"), mission.title);
    await user.type(screen.getByLabelText("Outcome"), mission.goal);
    await user.click(screen.getByRole("button", { name: "Create & start" }));

    await waitFor(() => expect(calls.some((call) => call.path.endsWith(`/missions/${mission.id}/plan`) && call.method === "POST")).toBe(true));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith(`/missions/${mission.id}/activate`) && call.method === "POST")).toBe(true));
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("mission")).toBe(mission.id));
    expect(new URLSearchParams(window.location.search).get("view")).toBe("missions");
  });

  it("retries the saved mission after planning fails without creating a duplicate", async () => {
    let createCount = 0;
    let planCount = 0;
    let planned = false;
    let activated = false;
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method ?? "GET";
      const json = (payload: unknown, responseStatus = 200) => new Response(JSON.stringify(payload), { status: responseStatus, headers: { "content-type": "application/json" } });
      if (path === "/api/status") return json(status);
      if (path.includes("missions?archive=active")) return json(createCount ? [{ ...mission, status: activated ? "ACTIVE" : planned ? "PLAN_READY" : "DRAFT", planning_attempts: planCount }] : []);
      if (path.includes("missions?archive=archived")) return json([]);
      if (path === "/api/forge/missions" && method === "POST") { createCount += 1; return json(mission, 201); }
      if (path.endsWith(`/missions/${mission.id}/plan`) && method === "POST") {
        planCount += 1;
        if (planCount === 1) return json({ error: { code: "planner_unavailable", message: "Planner temporarily unavailable." } }, 503);
        planned = true;
        return json({ planning_run: { status: "SUCCEEDED" } });
      }
      if (path.endsWith(`/missions/${mission.id}/activate`) && method === "POST") { activated = true; return json({ ...mission, status: "ACTIVE", planning_attempts: planCount }); }
      if (path.endsWith(`/missions/${mission.id}`)) return json({ ...mission, status: activated ? "ACTIVE" : planned ? "PLAN_READY" : "DRAFT", planning_attempts: planCount });
      if (path.endsWith(`/missions/${mission.id}/plan`)) return json({ planning_run: { status: "SUCCEEDED", provider: "test", model_alias: "planner", estimated_cost: null, validation_result: { valid: true, errors: [] } }, proposal: null, validation: { valid: true, errors: [] } });
      return json({ error: { message: `Unhandled test request: ${method} ${path}` } }, 404);
    }));

    const user = userEvent.setup();
    render(<ControlCenter />);
    await user.click(await screen.findByRole("button", { name: /New mission/ }));
    await user.type(screen.getByLabelText("Mission title"), mission.title);
    await user.type(screen.getByLabelText("Outcome"), mission.goal);
    await user.click(screen.getByRole("button", { name: "Create & start" }));
    await user.click(await screen.findByRole("button", { name: "Retry saved mission" }));

    await waitFor(() => expect(activated).toBe(true));
    expect(createCount).toBe(1);
    expect(planCount).toBe(2);
  });
});
