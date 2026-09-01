# Forge Control Center UI

The Forge web application is an operational control center for user-provided Missions. It presents existing durable backend state; it does not introduce new agent powers, scheduling rules, lifecycle transitions, or autonomous goal creation.

## Information architecture

The persistent desktop shell contains Overview, Missions, Companies, Projects, Agents, Tasks, Activity, and System. The same navigation becomes an accessible drawer on narrow screens. Overview is intentionally mission-first: active Mission, current execution, human checkpoints, task graph, and realtime activity appear before infrastructure telemetry. Detailed workers, jobs, event delivery, model usage, and tool metrics live in System.

Mission workspace tabs separate operational concerns:

- Overview: goal, ownership, progress, and any result awaiting review.
- Plan: proposed team, dependency graph, validation, and activation control.
- Tasks: durable task states and existing review/fix actions.
- Files: safe project-relative paths derived from successful `filesystem.write` ToolCalls.
- Activity: human-readable translations of persistent events.
- Technical: identifiers and structured Mission inputs.

## Progress model

The UI shows a deterministic weighted lifecycle estimate based on the current project graph:

| Task status | Weight |
| --- | ---: |
| DONE | 100% |
| REVIEW | 90% |
| IN_PROGRESS | 50% |
| FIX_REQUIRED | 50% |
| QUEUED | 10% |
| CREATED, FAILED, CANCELLED | 0% |

Mission `COMPLETED` is displayed as 100%. For all other Missions, the UI averages the current Task weights and rounds to the nearest whole percent. This value is labeled “Weighted lifecycle estimate”; it is not persisted and never changes backend state.

## Data and realtime behavior

One client coordinator owns the status poll, Mission selection, Mission detail data, and SSE connection. Polling runs every ten seconds as a recovery path. Incoming events are inserted into the visible feed immediately and coalesced into one follow-up refresh, preventing each panel from opening its own stream or interval.

The Next.js Forge proxy exposes only the read endpoints needed for existing Company, Project, Agent, Task, TaskRun, and graph state. Its POST allowlist remains limited to existing Mission planning/activation/cancellation and Task review/state-machine actions.

## Safety and control semantics

Autonomy pause/resume always calls the persistent orchestrator APIs and refreshes authoritative state afterward. The UI never treats local state as the autonomy source of truth. It does not automatically activate Missions, approve Tasks, request fixes, or resume autonomy.

Artifacts are inferred only from existing successful workspace write records. Host paths, environment variables, secrets, raw provider content, and new filesystem access are not exposed. Internet and browser controls in Mission creation are visibly disabled because those capabilities do not exist.

Motion is limited to status pulses, loading shimmer, and the Mission drawer; `prefers-reduced-motion` disables them. Interactive controls have keyboard focus indicators, semantic labels, and responsive layouts down to 320 pixels.
