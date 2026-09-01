import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskInspector } from "./task-inspector";
import type { InspectionIteration, TaskInspection } from "./types";

const timestamp = "2026-08-24T12:10:58Z";

function iteration(number: number, commandCount: number, toolCount: number): InspectionIteration {
  return {
    iteration: number,
    task_run: { id: `run-${number}`, iteration: number, status: "FAILED", error: {reason:"Deterministic QA found blocking issues."}, started_at: timestamp, completed_at: timestamp },
    agent_runs: [{ id:`agent-${number}`, agent:{id:"developer",name:"Developer 1",role:"DEVELOPER"}, status:"SUCCEEDED", provider:"openai", model_alias:"coding", model_id:"model", error:null, total_tokens:100, started_at:timestamp, completed_at:timestamp, recovery_attempts:[] }],
    commands: Array.from({length:commandCount},(_,index)=>({ id:`command-${number}-${index}`, action:"GIT_STATUS", status:"FAILED", safe_arguments:{}, started_at:timestamp, finished_at:timestamp, duration_ms:12, exit_code:128, stdout_excerpt:null, stderr_excerpt:"fatal: not a git repository", output_truncated:false, error:{code:"DEVELOPMENT_COMMAND_FAILED",message:"Development action exited non-zero."}, change_summary:null })),
    tool_calls: Array.from({length:toolCount},(_,index)=>({ id:`tool-${number}-${index}`, tool_name:"development.execute", status:"FAILED", safe_arguments:{action:"NODE_TEST"}, safe_result:null, duration_ms:4, started_at:timestamp, completed_at:timestamp, error:{code:"DEVELOPMENT_PROFILE_MISMATCH",message:"NODE_TEST is unavailable for UNKNOWN projects"} })),
    qa_result: { id:`qa-${number}`, decision:"FAIL", summary:"QA found 10 blocking issue(s).", failure_classification:"INFRASTRUCTURE_UNVERIFIABLE", failure_code:"DEVELOPMENT_REPOSITORY_UNAVAILABLE", checks:[{name:"GIT_STATUS",status:"FAILED",evidence:"GIT_STATUS exited 128."}], blocking_issues:[{title:"GIT_STATUS failed",description:"Exit 128."}], non_blocking_issues:[], deterministic_checks_executed:true, created_at:timestamp },
    acceptance_criteria: [{ id:`accept-${number}`, criterion_index:0, criterion:"Extension is runnable", status:"UNVERIFIED", verifier:"FORGE_DETERMINISTIC_QA", evidence_summary:"No deterministic evidence mapping was available.", execution_ids:[], created_at:timestamp }],
    reviews: number === 1 ? [{id:"review-1",iteration:1,decision:"FIX_REQUESTED",feedback:"Initialize the repository.",created_at:timestamp}] : [],
  };
}

const inspection: TaskInspection = {
  id:"failed-task", title:"Implement ClipMind MVP extension vertical slice", description:"Build the production vertical slice.", kind:"DEVELOPMENT", status:"FAILED", priority:"CRITICAL", iteration:4, max_iterations:4,
  acceptance_criteria:Array.from({length:9},(_,index)=>`Criterion ${index+1}`), assigned_agent:{id:"developer",name:"Developer 1",role:"DEVELOPER"}, dependencies:[],
  dependents:[{id:"blocked-task",title:"Add deterministic tests for core behavior",status:"CREATED",blocked_reason:"BLOCKED_BY_FAILED_DEPENDENCY"}],
  development_profile:{project_type:"NODE",package_manager:"NPM",detection_source:"package.json",test_action:"NODE_TEST",build_action:"NODE_BUILD",lint_action:null,typecheck_action:null,available_actions:["NODE_TEST","NODE_BUILD"],unavailable_actions:["NODE_LINT","NODE_TYPECHECK"],repository_initialized:true,repository_branch:"main",initial_checkpoint_created:true,changed_files_count:3,last_refreshed_at:timestamp,bootstrap_attempts:1,bootstrap_error:null},
  iterations:[iteration(1,1,4),iteration(2,1,3),iteration(3,1,4),iteration(4,2,5)],
  execution_jobs:[{id:"job-1",status:"FAILED",phase:"QA",attempts:2,max_attempts:3,worker_id:"worker-1",started_at:timestamp,completed_at:timestamp,last_error:{code:"DEVELOPMENT_ITERATIONS_EXHAUSTED"},failure_evidence:{category:"ITERATION_EXHAUSTION",code:"DEVELOPMENT_ITERATIONS_EXHAUSTED",phase:"QA"},phase_history:[{phase:"PREPARING",at:timestamp},{phase:"QA",at:timestamp}],retry_history:[{attempt:1,outcome:"RETRY_SCHEDULED"}],environment_state:{project_type:"NODE"},checkpoint_state:{initial_checkpoint_created:true},working_tree_state:{changed_files_count:3}}],
  final_acceptance_criteria:Array.from({length:9},(_,index)=>({id:`final-${index}`,criterion_index:index,criterion:`Criterion ${index+1}`,status:"UNVERIFIED",verifier:"FORGE_DETERMINISTIC_QA",evidence_summary:"No deterministic evidence mapping was available.",execution_ids:[],created_at:timestamp})),
  failure:{category:"ITERATION_EXHAUSTION",error_code:"DEVELOPMENT_ITERATIONS_EXHAUSTED",message:"Maximum development iterations reached (4/4). The Task remains failed because 9 acceptance criteria were not satisfied and deterministic QA failed.",failing_phase:"QA",failing_command:"GIT_STATUS",command_exit_code:128,qa_failure_reason:"QA found 10 blocking issue(s).",failed_acceptance_criteria:[],iteration_exhausted:true,provider:null,model:null,tool_failure:{code:"DEVELOPMENT_PROFILE_MISMATCH",message:"NODE_TEST is unavailable for UNKNOWN projects"},failed_at:timestamp,attempt:2,agent_role:"DEVELOPER",recovery_attempts:[{attempt:1,outcome:"RETRY_SCHEDULED"}],evidence_source:"qa_result"},
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("TaskInspector", () => {
  it("renders complete failed execution evidence and all four iterations", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(inspection), {status:200,headers:{"content-type":"application/json"}})));
    const { container } = render(<TaskInspector taskId="failed-task" onClose={vi.fn()} onInspectTask={vi.fn()}/>);

    expect(await screen.findByRole("heading", {name:inspection.title})).toBeTruthy();
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");
    expect(screen.getByRole("heading", {name:"Why this failed"})).toBeTruthy();
    expect(screen.getByText(/Maximum development iterations reached \(4\/4\)/)).toBeTruthy();
    expect(screen.getByText("DEVELOPMENT ITERATIONS EXHAUSTED")).toBeTruthy();
    expect(screen.getByRole("heading", {name:"Development environment"})).toBeTruthy();
    expect(screen.getByText("NPM")).toBeTruthy();
    expect(screen.getByText("Initialized")).toBeTruthy();
    await userEvent.click(screen.getByRole("tab", {name:"Timeline"}));
    expect(screen.getAllByText("INFRASTRUCTURE UNVERIFIABLE")).toHaveLength(4);
    expect(screen.getAllByText(/DEVELOPMENT_REPOSITORY_UNAVAILABLE/)).toHaveLength(4);
    for (const number of [1,2,3,4]) expect(screen.getByText(`Iteration ${number}`)).toBeTruthy();
    expect(screen.getAllByText("Deterministic checks were executed.")).toHaveLength(4);
    expect(screen.getByText("Initialize the repository.")).toBeTruthy();
    await userEvent.click(screen.getByRole("tab", {name:"Logs"}));
    expect(container.querySelectorAll('[data-evidence="command"]')).toHaveLength(5);
    expect(container.querySelectorAll('[data-evidence="tool-call"]')).toHaveLength(16);
    await userEvent.click(screen.getByRole("tab", {name:"Evidence"}));
    expect(screen.getAllByText("UNVERIFIED").length).toBeGreaterThanOrEqual(9);
  });

  it("navigates directly to an affected Task and exposes a mobile-usable close control", async () => {
    const inspect = vi.fn();
    const close = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(inspection), {status:200,headers:{"content-type":"application/json"}})));
    render(<TaskInspector taskId="failed-task" onClose={close} onInspectTask={inspect}/>);
    await screen.findByRole("heading", {name:inspection.title});
    await userEvent.click(screen.getByRole("tab", {name:"Evidence"}));
    await userEvent.click(screen.getByRole("button", {name:/Add deterministic tests/}));
    expect(inspect).toHaveBeenCalledWith("blocked-task");
    await userEvent.click(screen.getAllByRole("button", {name:"Close task inspection"})[1]);
    await waitFor(() => expect(close).toHaveBeenCalled());
  });
});
