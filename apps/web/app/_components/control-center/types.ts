export type View = "overview" | "missions" | "companies" | "projects" | "agents" | "tasks" | "activity" | "system";
export type MissionTab = "overview" | "plan" | "tasks" | "artifacts" | "activity" | "technical";
export type HealthValue = "healthy" | "unhealthy";

export type MissionScope = "active" | "archived";
export interface MissionProgress { total: number; done: number; review: number; running: number; queued: number; blocked: number; failed: number; cancelled?: number }
export interface Mission {
  id: string; company_id: string | null; project_id: string | null; title: string; goal: string;
  constraints: Record<string, unknown>; context?: Record<string, unknown>; status: string;
  planning_attempts: number; max_planning_attempts: number; failure_reason: string | null;
  progress: MissionProgress; project_name?: string | null; archived_at?: string | null;
  owns_company?: boolean; owns_project?: boolean; created_at?: string; updated_at?: string;
}
export interface Validation { valid: boolean; errors: Array<{ code: string; message: string; path: string | null }> }
export interface PlannerValidationEvidence {
  field: string;
  expected: string;
  received?: { type?: string; length?: number };
  error_type?: string;
}
export interface PlannerErrorDetails {
  code?: string; message?: string; category?: string; phase?: string; retryable?: boolean;
  retry_exhausted?: boolean; provider_attempts?: number; max_provider_attempts?: number;
  provider?: string; model?: string; failure_category?: string;
  repair_attempted?: boolean; repair_eligible?: boolean; last_failure_at?: string;
  provider_status?: number; provider_error_code?: string; finish_reason?: string;
  candidate_count?: number; content_exists?: boolean;
  response_shape?: { type?: string; top_level_fields?: string[]; text_length?: number };
  validation_errors?: PlannerValidationEvidence[];
}
export interface Plan {
  planning_run: {
    status: string; provider: string; model_alias: string; resolved_model: string;
    estimated_cost: string | null; completed_at?: string | null;
    validation_result: Validation | null; provider_attempts?: number;
    retry_history?: Array<{ attempt: number; at: string; outcome: string; code?: string; message?: string; category?: string; retryable?: boolean; failure_category?: string; repair_attempted?: boolean }>;
    error?: PlannerErrorDetails | null;
  };
  proposal: {
    project: { name: string; description: string };
    agents: Array<{ key: string; name: string; role: string; model_alias: string; requested_permissions: Record<string, boolean> }>;
    tasks: Array<{ key: string; title: string; description?: string; kind?: "GENERAL"|"RESEARCH"|"DOCUMENTATION"|"DEVELOPMENT"|"QA"; assigned_agent_key: string; priority: string; acceptance_criteria: string[]; max_iterations?: number }>;
    dependencies: Array<{ task: string; depends_on: string }>;
  } | null;
  validation: Validation | null;
}
export interface ToolCallGraph { id: string; tool_name: string; status: string; result: Record<string, unknown> | null; error: Record<string, unknown> | null }
export interface TaskReview {
  id: string; task_id: string; task_run_id: string | null; agent_run_id: string | null;
  iteration: number; decision: "APPROVED" | "FIX_REQUESTED"; feedback: string | null;
  correlation_id: string; created_at: string;
}
export interface DevelopmentExecution {
  id: string; action: string; status: string; exit_code: number | null;
  execution_origin?: "MODEL_REQUESTED" | "FORGE_QA";
  stdout_excerpt: string | null; stderr_excerpt: string | null; output_truncated: boolean;
  duration_ms: number | null; network_enabled: boolean; created_at: string;
  change_summary: { total?: number; added?: number; modified?: number; deleted?: number; files?: Array<{path:string;status:string}> } | null;
}
export interface QAResult {
  id: string; iteration: number; decision: "PASS" | "FAIL"; summary: string; created_at: string;
  failure_classification?: string | null; failure_code?: string | null;
  checks: Array<{ name: string; status: string; evidence: string; execution_id?: string }>;
  blocking_issues: Array<{ title: string; description: string; relatedFiles?: string[] }>;
  non_blocking_issues: Array<{ title: string; description: string; relatedFiles?: string[] }>;
}
export interface AcceptanceVerification {
  id: string; criterion_index: number; criterion: string; iteration: number;
  status: "UNVERIFIED" | "PASSED" | "FAILED" | "NOT_APPLICABLE";
  verifier: string; evidence_summary: string; execution_ids: string[]; created_at: string;
}
export interface GraphNode {
  id: string; title: string; description: string | null; kind: "GENERAL"|"RESEARCH"|"DOCUMENTATION"|"DEVELOPMENT"|"QA"; status: string; state: string; priority?: string;
  assigned_agent_id?: string | null; blocked_reason: string | null; ready?: boolean;
  acceptance_criteria: string[]; iteration: number; max_iterations: number;
  result: Record<string, unknown> | null; tool_calls: ToolCallGraph[]; reviews: TaskReview[];
  development_executions: DevelopmentExecution[]; qa_results: QAResult[];
  acceptance_verifications: AcceptanceVerification[];
}
export interface Graph { project_id: string; nodes: GraphNode[]; edges: Array<{ id: string; task_id: string; depends_on_task_id: string }> }
export interface InspectionAgent { id: string; name: string; role: string }
export interface InspectionError { code: string; message: string }
export interface InspectionTaskLink { id: string; title: string; status: string; blocked_reason: string | null }
export interface InspectionTaskRun { id: string; iteration: number; status: string; error: unknown; started_at: string; completed_at: string | null }
export interface InspectionAgentRun {
  id: string; agent: InspectionAgent; status: string; provider: string; model_alias: string;
  model_id: string; error: InspectionError | null; total_tokens: number;
  recovery_attempts: Array<Record<string, unknown>>;
  started_at: string | null; completed_at: string | null;
}
export interface InspectionCommand {
  id: string; action: string; status: string; safe_arguments: Record<string, unknown>;
  execution_origin?: "MODEL_REQUESTED" | "FORGE_QA";
  started_at: string | null; finished_at: string | null; duration_ms: number | null;
  exit_code: number | null; stdout_excerpt: string | null; stderr_excerpt: string | null;
  output_truncated: boolean; error: InspectionError | null;
  change_summary: Record<string, unknown> | null;
}
export interface InspectionToolCall {
  id: string; tool_name: string; status: string; safe_arguments: Record<string, unknown>;
  safe_result: Record<string, unknown> | null; duration_ms: number | null;
  started_at: string | null; completed_at: string | null; error: InspectionError | null;
}
export interface InspectionQAResult {
  id: string; decision: string; summary: string; checks: Array<Record<string, unknown>>;
  failure_classification?: string | null; failure_code?: string | null;
  blocking_issues: Array<Record<string, unknown>>; non_blocking_issues: Array<Record<string, unknown>>;
  deterministic_checks_executed: boolean; created_at: string;
}
export interface DevelopmentProfile {
  project_id?: string; project_type: string; package_manager: string; detection_source: string;
  install_action?: string | null; test_action?: string | null; build_action?: string | null;
  lint_action?: string | null; typecheck_action?: string | null;
  available_actions?: string[]; unavailable_actions?: string[];
  repository_initialized: boolean; repository_branch: string | null;
  initial_checkpoint_created: boolean; changed_files_count: number; last_refreshed_at: string;
  bootstrap_attempts: number; bootstrap_error_code?: string | null; bootstrap_error_message?: string | null;
  bootstrap_error?: InspectionError | null;
}
export interface InspectionAcceptance {
  id: string | null; criterion_index: number; criterion: string; status: string;
  verifier: string | null; evidence_summary: string; execution_ids: unknown[]; created_at: string | null;
}
export interface InspectionReview { id: string; iteration: number; decision: string; feedback: string | null; created_at: string }
export interface InspectionIteration {
  iteration: number; task_run: InspectionTaskRun; agent_runs: InspectionAgentRun[];
  commands: InspectionCommand[]; tool_calls: InspectionToolCall[]; qa_result: InspectionQAResult | null;
  acceptance_criteria: InspectionAcceptance[]; reviews: InspectionReview[];
}
export interface InspectionExecutionJob {
  id: string; status: string; phase: string; attempts: number; max_attempts: number;
  worker_id: string | null; started_at: string | null; completed_at: string | null;
  last_error: Record<string, unknown> | null; failure_evidence: Record<string, unknown> | null;
  phase_history: Array<Record<string, unknown>>; retry_history: Array<Record<string, unknown>>;
  environment_state: Record<string, unknown>; checkpoint_state: Record<string, unknown>;
  working_tree_state: Record<string, unknown>;
}
export interface TaskFailureSummary {
  category: string; error_code: string; message: string; failing_phase: string | null;
  failing_command: string | null; command_exit_code: number | null; qa_failure_reason: string | null;
  failed_acceptance_criteria: InspectionAcceptance[]; iteration_exhausted: boolean;
  provider: string | null; model: string | null; tool_failure: InspectionError | null;
  failed_at: string | null; attempt: number | null; agent_role: string | null;
  recovery_attempts: Array<Record<string, unknown>>; evidence_source: string;
}
export interface TaskInspection {
  id: string; title: string; description: string | null; kind: GraphNode["kind"]; status: string;
  priority: string; iteration: number; max_iterations: number; acceptance_criteria: unknown[];
  assigned_agent: InspectionAgent | null; dependencies: InspectionTaskLink[]; dependents: InspectionTaskLink[];
  development_profile?: DevelopmentProfile | null;
  iterations: InspectionIteration[]; execution_jobs: InspectionExecutionJob[];
  final_acceptance_criteria: InspectionAcceptance[];
  failure: TaskFailureSummary | null;
}
export interface ModelEscalation {
  from_alias: string; to_alias: string; reason_code: string; reason: string;
  objective_signals: string[]; created_at: string;
}
export interface RuntimeEfficiency {
  task_id: string; model_calls: number; input_tokens: number; output_tokens: number;
  cached_tokens: number; estimated_cost: string; current_model_alias: string | null;
  budget_warning: boolean; stopped_reason: string | null;
  limits: { model_calls: number | null; calls_per_iteration: number | null; input_tokens: number | null; output_tokens: number | null; estimated_cost: string | null };
  context_bytes_sent: number; estimated_unchanged_bytes_avoided: number;
  context_reduction_ratio: number; repeated_reads_avoided: number;
  duplicate_turns_detected: number; deterministic_executions: number;
  escalations: ModelEscalation[];
}
export interface MissionEfficiency {
  mission_id: string; task_count: number; model_calls: number; input_tokens: number;
  output_tokens: number; cached_tokens: number; estimated_cost: string;
  context_bytes_sent: number; estimated_unchanged_bytes_avoided: number;
  context_reduction_ratio: number; escalations: number; tasks: RuntimeEfficiency[];
}
export interface ProductQADimension { dimension: string; status: "PASS" | "FAIL" | "UNVERIFIED"; evidence: string }
export interface ProductQAIssue {
  category: string; dimension: string; severity: string; file: string;
  description: string; suggested_fix: string; evidence: string;
}
export interface ProductQAResult {
  id: string; task_id: string; task_run_id: string; iteration: number; decision: string;
  dimensions: ProductQADimension[]; issues: ProductQAIssue[];
  viewport_contract: Array<{name:string;width:number;height:number}>;
  evidence: Record<string, unknown>; screenshot_references: unknown[]; created_at: string;
}
export interface Company { id: string; name: string; slug?: string; goal?: string; description?: string | null; status?: string; capital_available?: string; revenue_recorded?: string; paid_ai_budget?: string; ai_spend_recorded?: string; created_at?: string }
export interface Project { id: string; company_id?: string; name: string; description?: string | null; goal?: string | null; status?: string; created_at?: string }
export interface ArtifactMetadata {
  name: string; path: string; size_bytes: number; modified_at: string; file_type: string;
  preview_kind: "markdown" | "json" | "text" | "code" | null; previewable: boolean;
  task_id: string | null; task_title: string | null; agent_id: string | null;
  agent_name: string | null; tool_call_id: string | null;
}
export interface ArtifactContent extends ArtifactMetadata { content: string }
export interface Agent { id: string; company_id?: string; name: string; role: string; status: string; model_alias?: string; configuration?: Record<string, unknown>; permissions?: Record<string, unknown> }
export interface TaskDetail { id: string; company_id?: string; project_id?: string | null; assigned_agent_id?: string | null; title: string; description?: string | null; kind?: string; priority?: string; status: string; iteration?: number; max_iterations?: number; started_at?: string | null; completed_at?: string | null }
export interface TaskRun { id: string; status: string; iteration?: number; started_at?: string | null; completed_at?: string | null; failure_reason?: string | null }
export interface ForgeEvent {
  id?: string; event_id?: string; type?: string; event_type?: string; topic?: string; message?: string;
  payload?: Record<string, unknown> & { message?: string }; metadata?: Record<string, unknown>;
  company_id?: string | null; project_id?: string | null; agent_id?: string | null; task_id?: string | null;
  correlation_id?: string | null;
  created_at?: string; occurred_at?: string;
}
export interface AgentRun { id: string; status: string; provider: string; model_alias: string; model_id: string; total_tokens: number; estimated_cost: string | null; task_title: string; agent_name: string; agent_role: string; error_code: string | null; started_at: string | null; completed_at: string | null; created_at: string }
export interface ModelCallRecord {
  id: string; task_id: string | null; provider: string; model_id: string;
  economic_tier: string; paid: boolean; status: string; agent_role: string;
  selection_reason: string; required_capabilities: string[];
  fallback_from_provider: string | null; fallback_from_model: string | null;
  fallback_reason: string | null; estimated_cost: string; input_tokens: number;
  output_tokens: number; cached_tokens: number; error_code: string | null;
  started_at: string; completed_at: string | null;
}
export interface EconomicsSummary {
  scope: string; scope_id: string; paid_ai_budget: string; ai_spend_recorded: string;
  paid_ai_budget_remaining: string; capital_available: string | null;
  revenue_recorded: string | null; reinvestable_capital: string | null;
  total_calls: number; calls_by_tier: Record<string, number>; fallback_count: number;
  escalation_count: number; recent_calls: ModelCallRecord[];
}
export interface ToolCall { id: string; tool_name: string; status: string; task_title: string; agent_name: string; duration_ms: number | null; error: { code?: string; message?: string } | null; created_at: string }
export interface WorkerNode { id: string; worker_key: string; status: string; concurrency: number; active_jobs: number; last_heartbeat_at: string }
export interface ExecutionJob { id: string; task_id: string; agent_id: string; worker_id: string | null; status: string; priority: string; attempts: number; max_attempts: number; last_error: { code?: string; message?: string } | null; created_at: string }
export interface StatusPayload {
  status: HealthValue; services: { api: HealthValue; database: HealthValue; redis: HealthValue };
  system: { name: string; version: string; status: HealthValue; companies: number; agents: number; tasks: number; task_states: Record<string, number> };
  eventStats: { pending: number; processing: number; published: number; failed: number; dlq: number; publisher_healthy: boolean; publisher_heartbeat_at: string | null };
  recentEvents: ForgeEvent[];
  agentRunStats: { total: number; by_status: Record<string, number>; input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number; estimated_cost: string | null };
  recentAgentRuns: AgentRun[];
  toolCallStats: { total: number; by_status: Record<string, number>; average_duration_ms: number | null };
  recentToolCalls: ToolCall[];
  orchestrator: { autonomy_enabled: boolean; orchestrator_online: boolean; queued_jobs: number; running_jobs: number; failed_jobs: number; active_workers: number; stale_workers: number; queued_tasks_without_jobs: number; last_reconciliation_time: string | null; total_jobs: number; job_counts: Record<string, number>; average_execution_duration_ms: number | null };
  workers: WorkerNode[]; executionJobs: ExecutionJob[]; checkedAt: string;
}

export interface ControlData {
  status: StatusPayload | null; missions: Mission[]; archivedMissions: Mission[]; mission: Mission | null; plan: Plan | null; graph: Graph | null;
  company: Company | null; project: Project | null; developmentProfile?: DevelopmentProfile | null; agents: Agent[]; focusTask: TaskDetail | null; taskRuns: TaskRun[];
  artifacts: ArtifactMetadata[] | null; artifactError: string | null;
  companies: Company[] | null; projects: Project[] | null; allAgents: Agent[] | null; allTasks: TaskDetail[] | null; inventoryError: string | null;
}
