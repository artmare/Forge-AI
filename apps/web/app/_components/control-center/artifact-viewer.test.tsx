import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ArtifactViewer } from "./artifact-viewer";
import { MissionView } from "./mission-view";
import type { ArtifactContent, ArtifactMetadata, ControlData } from "./types";

const artifact: ArtifactMetadata = {
  name: "research.md",
  path: "research.md",
  size_bytes: 6194,
  modified_at: new Date().toISOString(),
  file_type: "Markdown",
  preview_kind: "markdown",
  previewable: true,
  task_id: "task-1",
  task_title: "Write validated research.md",
  agent_id: "agent-1",
  agent_name: "Researcher",
  tool_call_id: "tool-1",
};

const content: ArtifactContent = {
  ...artifact,
  content: "# Research\n\n| Area | State |\n| --- | --- |\n| Runtime | Ready |\n\n<script>alert('unsafe')</script>\n\n[Unsafe](javascript:alert(1))\n\n![Remote](https://example.invalid/tracker.png)",
};

const data: ControlData = {
  status: null,
      missions: [],
      archivedMissions: [],
  mission: {
    id: "mission-1",
    company_id: "company-1",
    project_id: "project-1",
    title: "Validation Mission",
    goal: "Inspect the generated artifact before review.",
    constraints: {},
    status: "ACTIVE",
    planning_attempts: 1,
    max_planning_attempts: 3,
    failure_reason: null,
    progress: { total: 1, done: 0, review: 1, running: 0, queued: 0, blocked: 0, failed: 0 },
  },
  plan: null,
  graph: {
    project_id: "project-1",
    nodes: [{
      id: "task-1",
      title: "Write validated research.md",
      description: "Create the review artifact.",
      kind: "DOCUMENTATION",
      status: "REVIEW",
      state: "review",
      blocked_reason: null,
      acceptance_criteria: ["research.md exists"],
      iteration: 2,
      max_iterations: 3,
      result: { summary: "Artifact is ready." },
      tool_calls: [],
      development_executions: [],
      qa_results: [],
      acceptance_verifications: [],
      reviews: [{
        id: "review-1",
        task_id: "task-1",
        task_run_id: "task-run-1",
        agent_run_id: "agent-run-1",
        iteration: 1,
        decision: "FIX_REQUESTED",
        feedback: "Keep the validated sections and correct the AI boundary.",
        correlation_id: "task-1",
        created_at: "2026-08-23T17:14:00Z",
      }],
    }],
    edges: [],
  },
  company: { id: "company-1", name: "Validation Company" },
  project: { id: "project-1", name: "Validation Project" },
  agents: [],
  focusTask: null,
  taskRuns: [],
  artifacts: [artifact],
  artifactError: null,
  companies: null,
  projects: null,
  allAgents: null,
  allTasks: null,
  inventoryError: null,
};

function response(payload: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ArtifactViewer", () => {
  it("renders sanitized Markdown and switches between Rendered and Source", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(content)));
    const user = userEvent.setup();
    const { container } = render(<ArtifactViewer projectId="project-1" artifact={artifact} onClose={() => undefined}/>);

    expect(await screen.findByRole("heading", { name: "Research" })).toBeTruthy();
    expect(container.querySelector("table")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("[Image: Remote]")).toBeTruthy();
    expect(container.querySelector("a")?.getAttribute("href") ?? "").not.toContain("javascript:");

    await user.click(screen.getByRole("tab", { name: "Source" }));
    expect(screen.getByTestId("artifact-source").textContent).toContain("# Research");
    await user.click(screen.getByRole("tab", { name: "Rendered" }));
    expect(screen.getByTestId("rendered-markdown")).toBeTruthy();
  });

  it("shows loading and safe read errors", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const view = render(<ArtifactViewer projectId="project-1" artifact={artifact} onClose={() => undefined}/>);
    expect(screen.getByLabelText("Loading artifact")).toBeTruthy();
    view.unmount();

    vi.stubGlobal("fetch", vi.fn(() => response({ error: { code: "ARTIFACT_TOO_LARGE", message: "Internal detail" } }, 413)));
    render(<ArtifactViewer projectId="project-1" artifact={artifact} onClose={() => undefined}/>);
    expect(await screen.findByText("This file is too large to preview.")).toBeTruthy();
    expect(screen.queryByText("Internal detail")).toBeNull();
  });

  it("does not fetch or execute unsupported files", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ArtifactViewer projectId="project-1" artifact={{ ...artifact, name: "payload.bin", path: "payload.bin", file_type: "Unsupported file", preview_kind: null, previewable: false }} onClose={() => undefined}/>);
    expect(screen.getByText("Preview is not available for this file type.")).toBeTruthy();
    await waitFor(() => expect(fetchMock).not.toHaveBeenCalled());
  });

  it("formats valid JSON as readable source", async () => {
    const jsonArtifact: ArtifactMetadata = {
      ...artifact,
      name: "result.json",
      path: "result.json",
      file_type: "JSON",
      preview_kind: "json",
    };
    vi.stubGlobal("fetch", vi.fn(() => response({ ...jsonArtifact, content: '{"ready":true}' })));
    render(<ArtifactViewer projectId="project-1" artifact={jsonArtifact} onClose={() => undefined}/>);

    expect((await screen.findByTestId("artifact-source")).textContent).toContain('{\n  "ready": true\n}');
  });
});

describe("Mission artifact integration", () => {
  it("lists and opens research.md from Files", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(content)));
    const user = userEvent.setup();
    render(<MissionView data={data} tab="artifacts" busy={false} onTab={() => undefined} onAction={async () => true}/>);

    expect(screen.getByPlaceholderText("Search files…")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Open research.md" }));
    expect(await screen.findByRole("heading", { name: "Research" })).toBeTruthy();
  });

  it("opens the task artifact before approval from REVIEW", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(content)));
    const user = userEvent.setup();
    render(<MissionView data={data} tab="overview" busy={false} onTab={() => undefined} onAction={async () => true}/>);

    expect(screen.getByRole("button", { name: "Approve result" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /Open artifact/i }));
    expect(await screen.findByRole("heading", { name: "Research" })).toBeTruthy();
  });

  it("shows Files loading and error states", () => {
    const loading = { ...data, artifacts: null };
    const view = render(<MissionView data={loading} tab="artifacts" busy={false} onTab={() => undefined} onAction={async () => true}/>);
    expect(screen.getByLabelText("Loading files")).toBeTruthy();
    view.unmount();

    const failed = { ...data, artifacts: [], artifactError: "failed" };
    render(<MissionView data={failed} tab="artifacts" busy={false} onTab={() => undefined} onAction={async () => true}/>);
    expect(screen.getByText("Could not load Mission artifacts")).toBeTruthy();
  });

  it("requires feedback, submits structured review instructions, and keeps history", async () => {
    const action = vi.fn(async () => true);
    const user = userEvent.setup();
    render(<MissionView data={data} tab="overview" busy={false} onTab={() => undefined} onAction={action}/>);

    expect(screen.getByText("Review history")).toBeTruthy();
    expect(screen.getByText("Iteration 1")).toBeTruthy();
    expect(screen.getByText("Keep the validated sections and correct the AI boundary.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Request a fix" }));
    expect(screen.getByRole("dialog", { name: "Request changes" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    expect(screen.getByText("Describe what the agent should change.")).toBeTruthy();

    const feedback = "Move structured AI generation into the MVP backend boundary.";
    await user.type(screen.getByLabelText("Reviewer instructions"), feedback);
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith(
      "tasks/task-1/request-fix",
      { feedback },
    ));
    expect(screen.queryByRole("dialog", { name: "Request changes" })).toBeNull();
  });

  it("shows the active revision feedback while the task is queued", () => {
    const revisionData: ControlData = {
      ...data,
      graph: data.graph ? {
        ...data.graph,
        nodes: data.graph.nodes.map((node) => ({ ...node, status: "QUEUED" })),
      } : null,
    };
    render(<MissionView data={revisionData} tab="tasks" busy={false} onTab={() => undefined} onAction={async () => true}/>);

    expect(screen.getByText("Revision requested")).toBeTruthy();
    expect(screen.getByText("Keep the validated sections and correct the AI boundary.")).toBeTruthy();
  });

  it("shows durable development, QA, and acceptance evidence", () => {
    const developmentData: ControlData = {
      ...data,
      graph: data.graph ? {
        ...data.graph,
        nodes: data.graph.nodes.map((node) => ({
          ...node,
          kind: "DEVELOPMENT" as const,
          development_executions: [{
            id: "execution-1",
            action: "NODE_TEST",
            status: "SUCCEEDED",
            exit_code: 0,
            stdout_excerpt: "1 test passed",
            stderr_excerpt: "",
            output_truncated: false,
            duration_ms: 4200,
            network_enabled: false,
            created_at: "2026-08-24T10:00:00Z",
            change_summary: {
              total: 2,
              added: 2,
              modified: 0,
              deleted: 0,
              files: [{ path: "manifest.json", status: "added" }],
            },
          }],
          qa_results: [{
            id: "qa-1",
            iteration: 2,
            decision: "PASS" as const,
            summary: "Build and tests passed independently.",
            created_at: "2026-08-24T10:01:00Z",
            checks: [{
              name: "NODE_TEST",
              status: "PASSED",
              evidence: "NODE_TEST exited 0",
              execution_id: "execution-1",
            }],
            blocking_issues: [],
            non_blocking_issues: [],
          }],
          acceptance_verifications: [{
            id: "verification-1",
            criterion_index: 0,
            criterion: "research.md exists",
            iteration: 2,
            status: "PASSED" as const,
            verifier: "QA",
            evidence_summary: "File exists in the project workspace.",
            execution_ids: ["execution-1"],
            created_at: "2026-08-24T10:01:00Z",
          }],
        })),
      } : null,
    };

    render(<MissionView data={developmentData} tab="overview" busy={false} onTab={() => undefined} onAction={async () => true}/>);

    expect(screen.getByText("Development")).toBeTruthy();
    expect(screen.getByText("2 files changed")).toBeTruthy();
    expect(screen.getAllByText("NODE TEST")).toHaveLength(2);
    expect(screen.getByText("Build and tests passed independently.")).toBeTruthy();
    expect(screen.getByText("File exists in the project workspace.")).toBeTruthy();
  });

  it("shows the persisted development profile and repository state in Technical", () => {
    const technicalData: ControlData = {
      ...data,
      developmentProfile: {
        project_type: "NODE",
        package_manager: "NPM",
        detection_source: "package.json",
        test_action: "NODE_TEST",
        build_action: "NODE_BUILD",
        lint_action: null,
        typecheck_action: null,
        available_actions: ["NODE_TEST", "NODE_BUILD"],
        unavailable_actions: ["NODE_LINT", "NODE_TYPECHECK"],
        repository_initialized: true,
        repository_branch: "main",
        initial_checkpoint_created: true,
        changed_files_count: 2,
        last_refreshed_at: "2026-08-24T10:01:00Z",
        bootstrap_attempts: 1,
        bootstrap_error: null,
      },
    };

    render(<MissionView data={technicalData} tab="technical" busy={false} onTab={() => undefined} onAction={async () => true}/>);

    expect(screen.getByText("NODE · NPM")).toBeTruthy();
    expect(screen.getByText("Initialized")).toBeTruthy();
    expect(screen.getByText("main · created")).toBeTruthy();
    expect(screen.getByText("2 files changed")).toBeTruthy();
    expect(screen.getByText("NODE_TEST").closest("span")?.dataset.available).toBe("true");
    expect(screen.getByText("NODE_LINT unavailable").closest("span")?.dataset.available).toBe("false");
  });
});
