# Company Factory

`CompanyFactory` is deterministic application code and is the only Phase 08 component that turns an
approved `PlanProposal` into execution records. The LLM never writes to PostgreSQL.

Activation locks the Mission row, requires `PLAN_READY`, selects the latest successful PlanningRun,
and revalidates the stored proposal against the current model/tool capability policy. In one database
transaction it loads or creates the Company, creates the Project, Agents, Tasks, dependency edges,
queues only roots through `TaskStateMachine`, links the Mission, writes correlated events, and commits.

An exception before commit rolls back the entire topology. The Mission row lock and unique Mission
`project_id` make concurrent activation safe. Repeated activation after commit returns the existing
materialized Mission instead of creating duplicates.

Agent configuration records the selected execution model alias plus Mission/Project identifiers.
Workers read that alias before reusing `AgentRuntime`. No Agent is automatically reused across
Projects, no Company identity is overwritten, and no unsupported permission is silently added.
