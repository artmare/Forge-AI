# Forge reliability audit — 2026-10-13–14

## Baseline (before repairs)

- Canonical workspace: `~/Projects/ForgeAI`; branch `feat/forge-dev-openrouter`.
- Commit: `ad3157a47fedd33743a4dcb537b76f1f65971deb`; initial `git status --short` empty.
- Compose project `forge`: PostgreSQL, Redis, API, web, orchestrator and agent worker
  healthy; event publisher/consumer and both development runners running.
- Alembic current and head: `20260929_0020`.
- Host and API container configuration: mock provider; paid calls disabled;
  OpenRouter disabled and no key present. Browser runner and Forge Dev Mode disabled.
  Secrets were checked only for presence. Persisted autonomy was enabled despite the
  bootstrap environment default being false; the database is authoritative.
- `make lint`: Ruff, ESLint, TypeScript passed.
- Frontend `npm test` in the Docker source stage: 6 files, 26 tests passed;
  pre-existing React `act` warnings.
- `docker compose --profile test run --build --rm forge-api-test`: baseline
  **402 passed, 1 failed, 1 skipped** (303.70 seconds). The failing assertion was
  `test_forge_git_acceptance_requires_an_observable_outcome`.
- `git archive HEAD:apps/web | docker build -t forge-web-clean-baseline -`: failed
  because `vite.config.ts` statically imports ignored `.openai/hosting.json`.
- Browser: direct Playwright with Edge, 1440×900, real production Control Center on
  localhost:3000. In-app automation could not initialize; direct Playwright worked.
- Browser-created Mission `770fcd8e-8980-4313-b78e-cce1cced570c`
  (`Reliability audit — Todo`) planned and activated but FAILED. Project
  `df6c8cbf-d3ab-4e42-8359-505cd4494776`; write task
  `92890177-a721-45f4-b277-1242ce40e4de` failed with `DUPLICATE_TOOL_LOOP` after two
  successful writes; dependent verification task was cancelled. The default provider
  is reconstructed on each turn, resetting the mock's call counter. Existing E2E tests
  inject a persistent mock and miss this path.
- Mock planning supplies a fixed brief-writing demonstration regardless of the goal;
  this is not evidence of a generated Todo application.

The original failed Mission and prior user Mission remain as evidence. Raw logs and
screenshots are under `artifacts/` with the `reliability-` prefix, ignored by Git.

## Final verdict

**FORGE END-TO-END READY WITH EXTERNAL LIMITATIONS**, scoped to the local flows
demonstrated below. Six UI-created audit Missions completed with persisted task/run
evidence and human approval. Two rejected plans remain DRAFT with canonical failure
evidence; the original failed audit Mission remains FAILED.

This is not certification of arbitrary AI software generation: no model credential
is present. Every live Mission in this audit used the mock provider. The Todo sample
is explicitly scripted and labelled, and its generated application was independently
opened and exercised. General model reasoning, live provider availability and vision
judgment remain unproven. No paid-call policy or permission gate was relaxed.

## Architecture and runtime ground truth

- FastAPI owns Mission planning/materialization, task transitions and inspection.
  PostgreSQL stores canonical topology, review decisions, execution leases, model
  attempts, tool calls, verification and QA. Alembic runs before API startup.
- Redis Streams carry transactional-outbox events. Publisher/consumer processes
  deliver them; Control Center combines SSE updates with periodic API refresh.
- The orchestrator schedules eligible DAG tasks. A leased worker bootstraps project
  repositories, executes provider/tool turns, then finalizes DEVELOPMENT verification
  and independent QA. Human approval releases dependent tasks and permits completion.
- Development commands run in separate bounded runners with per-project snapshots,
  Landlock, restricted actions, Git metadata protection and no ordinary network.
  Dependency installation has its own runner. Browser capture has a separate optional
  network-disabled container, read-only workspace mount and bounded screenshot queue.
- Node/vinext serves the Control Center and allowlisted API proxy. Artifacts are
  bounded read-only source/text previews; HTML is displayed as source, not executed.
- Current config: mock provider; paid calls false; OpenRouter false; OpenRouter,
  OpenAI and Gemini keys absent; Forge self-development mode and browser tool false.
  The ordinary DEVELOPMENT pipeline still operates; self-development mode is separate.
- Final observed API/database/Redis health: healthy. One active worker, no stale
  workers, no queued or running jobs, no queued tasks without jobs. Two historical
  failed jobs are preserved. Migration current=head=`20260929_0020`.

## Defects and repairs

| Symptom | Root cause and component | Repair | Regression evidence |
|---|---|---|---|
| Fresh checkout web image fails | Vite statically imported ignored `.openai/hosting.json` and unused hosting plugins | Use the existing self-hosted Node/vinext build; exclude deployment-local directories from Docker context | Git-aware context build succeeds without ignored files |
| Default UI Mission loops and fails | Model routing constructs a fresh mock adapter each turn; its local counter always restarted | Drive mock scenarios from runtime `turn_number`; retain explicit scripted-test sequencing | Existing autonomous E2E now runs with both injected and ordinary provider routing; live default Missions complete |
| Bare `git.status` passes observable-criterion classification | Filename matcher interpreted dotted Forge Git tool names as artifacts | Strip whole command tokens before filename matching; preserve actual paths such as `reports/git.log` | Planner assertion fixed; artifact-path regression added |
| Valid planning can be cut off by the web proxy | 35-second HTTP deadline was shorter than bounded retries/repair | Planning POST budget 600 seconds; explicit 504 timeout with refresh guidance; ordinary requests remain 35 seconds | Route/helper tests cover budgets, timeout, structured errors and forbidden paths |
| Gateway failures resemble user JSON errors | Frontend parsed every error body as JSON and trusted `error.message` types | Preserve canonical diagnostics and normalize malformed HTTP responses into typed request errors | Six helper tests including HTML, null and malformed error objects |
| Edited creation retry silently uses previous constraints | Pending-Mission reuse compared only title/goal | Compare the complete serialized create payload; changed inputs create a new Mission | Component tests and live max-tasks rejection/recovery |
| Invalid JSON object inputs and stale errors block useful retries | JSON parsing admitted null/arrays/scalars; error state stayed after editing | Require objects; clear stale input error on edits; explain retry semantics | Form tests cover the invalid shapes and real create boundaries |
| Newly created Mission displays previous Mission results | Create callback refreshed the old selected ID; an older list could replace the new selection | Refresh the explicit new ID and preserve deliberate selection against stale list responses | Component regression begins with another selected Mission; repeated real UI creation |
| `.mjs`/`.cjs` sources unavailable | Preview allowlist recognized only `.js`/`.jsx` | Add module/CommonJS source types with unchanged path, size, encoding and execution restrictions | Two API regressions plus actual Todo source preview |
| Mock output looks like goal-directed generation | UI did not distinguish provider behavior | Show a visible mock demonstration notice; document the fixed sample and live-model boundary | Actual Mission screenshots and plan provider records |
| Local UI has unrelated public metadata and missing icon | Hardcoded hosted-site metadata URL; absent favicon | Local metadata default with optional public URL; tracked SVG favicon | Production build and final browser runs without resource errors |

An explicit `context.mock_scenario="todo_demo"` was added to keep the development
pipeline testable without credentials. It writes seven source/test/build files,
runs Node test/build and Git status/diff through normal tools, and submits to the
existing QA/review pipeline. It does not bypass validation, mutate the database
directly, or claim to implement arbitrary user requests.

## Important changed files

- `apps/api/app/agent_runtime/providers.py`, new `mock_development.py`: truthful mock
  progress and explicit development sample.
- `apps/api/app/planning/{acceptance,mission_planner}.py`: observable Git criteria
  and explicit sample metadata.
- `apps/api/app/services/artifact.py`: JavaScript module previews.
- `apps/api/tests/{test_phase08,test_planner_validation,test_artifacts,test_mock_development}.py`:
  normal routing, canonical classification, safe previews, isolated development/QA.
- `apps/web/vite.config.ts`, `.dockerignore`, `app/layout.tsx`, `public/favicon.svg`:
  portable self-hosted build and local metadata.
- `apps/web/app/api/forge/[...path]/route.ts` and new route tests: proxy contracts.
- `apps/web/app/_components/control-center/{control-center,create-mission,helpers,mission-view}`
  and their tests: selection/retry/error handling and visible mock mode.
- `tests/browser/`: real browser workflows, generated-app verification, read-only
  evidence collection, isolated runner diagnostic.
- `README.md`, this report and `.gitignore`: reproducible local operation and evidence hygiene.

## Automated checks

Commands below ran from the canonical repository through WSL/Docker. No test database
was substituted for the running UI database. Tests use their disposable `forge_test`
database and Redis DB 1; live browser evidence comes from the ordinary `forge` stack.

| Command | Result |
|---|---|
| `docker compose --profile test run --build --rm forge-api-test pytest -q -o cache_dir=/tmp/pytest-cache` | **407 passed, 1 skipped**, 274.50 seconds; `artifacts/reliability-final-tests.log` |
| Same command, before module-preview regression additions | 405 passed, 1 skipped; 298.20 seconds |
| Same runner, `tests/test_artifacts.py tests/test_planner_validation.py` | 22 passed, 5.91 seconds |
| Focused normal-routing planner/Mission run | 50 passed, 27.94 seconds |
| `docker build --target source -t forge-web-audit-source apps/web` then `docker run --rm forge-web-audit-source sh -c 'npm test && npm run lint && npx tsc --noEmit'` | 8 files, 41 tests passed; ESLint and TypeScript passed |
| `make lint` | Ruff, ESLint and TypeScript passed |
| `docker compose build` and `docker compose up -d` | Production services built and started |
| `git ls-files -z --cached --others --exclude-standard apps/web \| tar --null -T - --transform=s,apps/web/,, -cf - \| docker build -t forge-web-clean-audit -` | Passed with only Git-visible input; excludes `.openai`, host dependencies and generated output |
| `docker compose --profile browser build forge-browser-runner` | Passed; real Chromium image built |
| `docker compose exec -T forge-api alembic current` / `alembic heads` | Both `20260929_0020` |
| `git diff --check` | Passed |

The sole full-suite skip is the opt-in live OpenRouter test. Separate safe config
inspection proves the credential is absent and the provider disabled; no live-model
success is inferred. Existing React tests print `act` warnings but pass. During
development, new-test type/import issues were corrected before the final checks.
One Docker registry TLS handshake failed transiently; the same build later succeeded.

The full backend suite covers malformed planning and repair, transient/timeout
providers, free-only/capability routing, task DAG/state transitions, concurrent leases,
tool permissions/timeouts, command/install runner boundaries, Git isolation, failed
build/tests, QA rejection/revision, completion manifests, stale-run recovery and
current-vs-historical execution evidence. These are automated fault-injection results,
not claims that each failure was separately triggered in the live browser.

## Browser and generated-product checks

Direct Playwright operated real Edge at 1440×900 against `localhost:3000`. Requests
were not intercepted or mocked. Initial automation integration was unavailable;
the direct browser worked. Evidence is in `artifacts/reliability-audit/`.

1. Clicked Dashboard, Missions, Companies, Projects, Agents, Artifacts, Tasks,
   Activity, System and Settings. Saved all ten page screenshots. Paused/resumed
   autonomy through UI and matched the persisted API state.
2. Used New mission, title/outcome fields, planning limits, Advanced structured
   inputs and Create & start. Plans, topology, scheduling, worker execution and
   review were confirmed from the resulting Mission's own graph, never an older ID.
3. Opened all Mission tabs: Overview, Plan, Tasks, Files, Activity and Technical.
   Opened Inspect execution and Summary, Evidence, Timeline and Logs. Reloaded and
   verified that Mission identity and completed states persisted.
4. Requested a fix on the brief Mission with reviewer instructions. The first task
   advanced to iteration 2 and returned to REVIEW. Approved it and then its dependent
   reader task. Persisted review history and TaskRuns match the UI.
5. Submitted max_tasks=1 against the two-task mock plan. UI showed HTTP 422,
   `INVALID_PLAN`, `CANONICAL_VALIDATION`, `PLAN_LIMIT_EXCEEDED`, the task limit and
   attempted repair. No topology was created. Changed limit to 8 in the same form;
   a new Mission received the new constraints, executed and completed.
6. Created a Mission while autonomy was paused, restarted the real agent-worker
   container, resumed through UI, then approved both tasks. The queued work survived.
   This was a restart with pending work, not a kill in the middle of an external command.
7. Opened source artifacts in the actual viewer, including `.mjs`; text and Markdown
   preview safety is covered by component/API tests. No ZIP/project download control
   exists; this is unsupported functionality, not a tested download success.
8. Retrieved the Todo's unchanged `dist/` artifacts through the public artifact API,
   served only those files on a temporary loopback server, and opened the application.
   Empty state, add, check/complete and delete passed. At 390×844 there was no horizontal
   overflow. Desktop/mobile screenshots were visually inspected; no JavaScript errors.
9. Started the optional browser profile and ran `tests/browser/runner_smoke.py`
   inside API against the completed Todo project. The real isolated Chromium worker
   returned title `Forge Todo demo`, `rendered=true`, no console errors and a real PNG:
   `browser/25ba68d6-fc80-401f-828e-2e0f8ca1a140.png`, SHA-256
   `3f60297ca0b466f764dc69807af6e863fc379904fe7ad80ed9eb2d50a3f53317`.
   Copied evidence to `isolated-browser.png` and stopped the optional runner afterward.
   This diagnostic did not insert browser evidence into the Mission or enable its tool.

Final successful navigation/review/generated-app runs have zero page errors and zero
failed requests. The invalid-plan scenario has the expected single 422 and associated
browser resource-error entry. Historical initial favicon failure is fixed. Logs show
intentional constraint violations from database tests and transient SSE connection
errors during API recreation; final browser navigation reconnected successfully.
No persistent worker crash/retry storm was observed.

## Verification limits and remaining scope

- **External credential limitation:** OpenRouter cannot execute without a supplied key
  and explicit configuration. No OpenAI/Gemini key is present either. Live free-model
  availability, fallback quality, structured-output compatibility and visual model
  judgment cannot be certified from mock or mocked HTTP tests.
- **Supported scope:** Todo DEVELOPMENT runs have actual successful Node test/build,
  Git evidence, three PASSED acceptance checks, independent QA PASS and human approval.
  Brief tasks use GENERAL workflow and human review; they do not have a DEVELOPMENT
  QA result or deterministic acceptance-verification rows. Their file writes/reads
  are persisted, and the reviewer saw the resulting file.
- **Unsupported functionality:** no built-in ZIP download or hosted app preview.
  Browser capture supports built static HTML; it does not host arbitrary server apps.
  The Todo sample is in-memory and deliberately has no persistence requirement.
- **Visual limits:** a screenshot proves rendering, not aesthetic or feature correctness.
  Product QA's unverified visual dimensions remain unverified. Manual browser actions
  establish the sample's three requested behaviors only.
- **Coverage limits:** the dependency-free sample does not install packages. Install,
  command timeout, denied tool, QA rejection and mid-run lease recovery are covered by
  automated integration tests, not live paid-model Missions. Self-development worktree
  mode stays disabled; its dedicated automated tests passed.
- **Environment scope:** clean web build used a Git-visible snapshot; fresh schema
  behavior is exercised by test setup/migration tests. Existing user volumes were not
  destroyed or replaced with a second independent production stack.
- **Repository defects:** no known blocker remains in the demonstrated local flows.
  This audit does not prove every arbitrary goal, external provider or deployment.

## Git and evidence handling

Branch remains `feat/forge-dev-openrouter`; HEAD remains
`ad3157a47fedd33743a4dcb537b76f1f65971deb`. No commit, push or PR was created.
No `.env`, credential, original user Mission or existing failure evidence was removed.
Raw screenshots/logs/generated sample exports are ignored; reproducible checks and
the report are source files. All work occurred inside the canonical Forge checkout.
The final status and diff summary follow the Mission appendix.

## Persisted Mission appendix

The following identifiers are read from the running API. Full graph/inspection JSON
is saved in `artifacts/reliability-audit/persisted-missions.json`. Every planning run
uses provider `mock`, alias `planner`, resolved model label `gpt-5.4-mini`; that label
does **not** mean an OpenAI request was sent.

### Reliability queued 2026-10-14T04:10:26.491Z

- Mission: `b523d78b-5270-47b1-93fa-0cd430f8a051`; final status: **COMPLETED**.
- PlanningRun: `7bea7386-a576-4476-b841-98eeabee3478`; SUCCEEDED.
- Project: `86720623-d679-4ae4-b913-a20617edd285`.
- Task **Create project brief**: `d6e470b6-862e-4ef5-b77c-f65e784502f4`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `b76a291c-b853-4ac1-b6c7-a62c43c4e993`; AgentRuns: `f8c0d124-2cd7-40d2-9419-828a308ed059`, `75d99909-cf82-497f-8099-1c4984e0e647`; 1 tool calls, 0 command records.
- Task **Verify project brief**: `d96625e9-feae-4955-b4b4-c1aba846e631`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `dd4fff6e-47b1-4958-b8b6-348963687640`; AgentRuns: `d366701f-1408-4a03-a967-dfe8ca9b09a9`, `edb1e3ac-c1da-4323-995d-12d8fee9d4a6`; 1 tool calls, 0 command records.

### Reliability invalid 2026-10-14T04:08:30.489Z

- Mission: `ca19e324-c9d8-472c-922f-f9af16c66581`; final status: **COMPLETED**.
- PlanningRun: `22709e09-7646-4154-88f7-25a4852218c1`; SUCCEEDED.
- Project: `42ca18aa-42eb-4487-972e-cfc6b39dbfc8`.
- Task **Create project brief**: `e0967e1f-6473-471d-a40f-4fb1e0de9200`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `4be82358-ec42-4ef7-be74-5cbc3af6da0b`; AgentRuns: `30edc7cd-5ae8-43d0-95a9-0c339f936b4a`, `e492de29-7f17-44a5-b6cf-c5a102c3cf5f`; 1 tool calls, 0 command records.
- Task **Verify project brief**: `8aaff3a6-8299-46f5-995e-ef70541c5747`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `ed047b85-634c-4d1d-bdd8-f072548bed7f`; AgentRuns: `253ee8fe-184e-4c16-942d-78e6481c7ba6`, `211a7002-d9b8-4c68-9b2f-25084bfd0554`; 1 tool calls, 0 command records.

### Reliability invalid 2026-10-14T04:08:30.489Z

- Mission: `5d06d6ba-df88-440a-8166-6a8d5dc3fb93`; final status: **DRAFT**.
- PlanningRun: `a3c6462d-3fc0-4ab2-a2df-df058f0bc24c`; INVALID.
- No project/tasks/runs; canonical rejection preserved for inspection.

### Reliability invalid 2026-10-14T04:07:52.481Z

- Mission: `24219fd5-772a-405b-8536-6fecfbfb195a`; final status: **DRAFT**.
- PlanningRun: `f0e3b54c-5cf8-4ea0-9966-ed63c77f3d7a`; INVALID.
- No project/tasks/runs; canonical rejection preserved for inspection.

### Reliability brief 2026-10-14T04:06:16.653Z

- Mission: `1cf1a2a7-3359-4be8-8dbd-9331e309f57f`; final status: **COMPLETED**.
- PlanningRun: `6ef69d1e-aa83-413a-a654-51446c77e574`; SUCCEEDED.
- Project: `b486b5ae-2969-4411-8e28-eb166e26dfb1`.
- Task **Create project brief**: `7dca583c-5467-4b3e-a409-049ac4bb7333`; DONE; iteration 2; QA: not applicable to GENERAL workflow.
  TaskRun `6fad056b-66ef-456b-9082-9654be7bc3ec`; AgentRuns: `92693128-6008-4a92-871f-6f6eefdbb9ef`, `f509736f-483a-4a09-bb9a-3877562e17e5`; 1 tool calls, 0 command records.
  TaskRun `084d25f4-aa0b-4ee0-841e-37ca1f71f772`; AgentRuns: `8c61a545-1d3d-4589-851c-dffde2872276`, `865c95da-be60-4662-8988-c4b755c34f68`; 1 tool calls, 0 command records.
- Task **Verify project brief**: `35779a68-374a-4d24-8c8a-26c1cf323d6c`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `df701f59-0cd8-4b55-a6f7-8b8401f9456b`; AgentRuns: `ef586ea6-7452-45d2-8d89-3ae83014e61c`, `fd8329fb-b72a-4c3c-8f22-b8e71caaee83`; 1 tool calls, 0 command records.

### Reliability todo 2026-10-14T03:56:42.248Z

- Mission: `617150c8-65ff-442c-b55c-42b43ab7fa0d`; final status: **COMPLETED**.
- PlanningRun: `399b90e1-e7ee-4963-a248-018a51472560`; SUCCEEDED.
- Project: `fcccc9df-ac61-4d0c-ad0c-309768ee06b5`.
- Task **Build and verify the mock Todo sample**: `5bbb4df8-2cd7-4c1a-99bd-91c257ba5572`; DONE; iteration 1; QA: PASS (`cfe11112-f2cb-4ff4-bc60-b16aaeb1f99d`).
  Acceptance results: PASSED, PASSED, PASSED.
  TaskRun `d3d9f574-6ad5-480f-a294-1338ddd285bd`; AgentRuns: `91d061cc-5057-4aa1-a802-4ee3c1ca1796`, `4ff10ed0-ef99-4abb-979c-b2dcd3cdaca9`, `429857b8-dcb7-4765-bed3-9c21c60b8854`, `b821102b-c5a7-4ca7-bd4b-0eda8ae40a37`, `1fc334bc-2281-4b26-84fd-f29e16118e41`, `87416b2f-73d2-48ff-a288-ae5cbf37e400`, `3b6eec01-455a-43fd-a496-cbc52aa24b95`, `f6faa1f8-2ba3-41a9-ac2a-3d16c0b46f6a`, `09cdcf30-4fd4-4194-8df8-a8a758f04aed`, `6c6ffada-f674-4a6a-b8fe-fb939f26345a`, `fc860961-a2ad-4bc2-ae72-b02e49238ff5`, `64acbf66-7fca-45ce-b7a9-637d1ddf73af`, `37057b77-4107-473c-892c-7a22653395ff`; 11 tool calls, 7 command records.

### Reliability todo 2026-10-13T21:09:50.792Z

- Mission: `de108038-beec-434a-b964-c7526b3d9b4c`; final status: **COMPLETED**.
- PlanningRun: `751bc61f-0292-4acd-be33-f6aab4f52348`; SUCCEEDED.
- Project: `9a41a3ba-83da-4f96-aff2-f5ab38a5b142`.
- Task **Build and verify the mock Todo sample**: `8316ee6c-796c-42e6-b73c-d4c6ef1fce70`; DONE; iteration 1; QA: PASS (`2c938695-4b80-49ce-8ca0-60f577968140`).
  Acceptance results: PASSED, PASSED, PASSED.
  TaskRun `ca4855ac-578d-4e82-8caf-1dd639517825`; AgentRuns: `f5fa01ba-9230-407f-97d8-be65f548161c`, `a561e066-1cd7-4ca3-b1ab-85eb225bc3e2`, `36a46031-f333-49ef-8462-c9baf1f6f072`, `788f6095-2f08-491d-a04d-46bfaa714189`, `e725ff48-5f67-4e28-9c2a-0a5c02861636`, `5e1e1860-fa2c-4de3-85b5-8f064292bb16`, `0c36db2e-3251-41a8-8ba5-ae009d72dd2b`, `cf0d0c32-f66d-41d9-93c8-dbbd099f435b`, `9f2fdfe6-589b-4e79-9a11-7d01b4e008c4`, `8321dd9f-105b-4826-8052-47d780e51511`, `d0753cbb-16f2-4397-8826-710103332c49`, `055e8542-3aa3-457f-a1e4-34e714458899`, `f7bff557-5494-45ce-b626-dad2ddaf9a49`; 11 tool calls, 7 command records.

### Reliability brief 2026-10-13T21:08:31.742Z

- Mission: `8d0fda6f-6a4d-483e-81a1-8835cf3ccc5c`; final status: **COMPLETED**.
- PlanningRun: `222dc899-e684-48d8-99f2-a747b4ba2866`; SUCCEEDED.
- Project: `a5a59279-eb88-4f56-8745-9d39695922b4`.
- Task **Create project brief**: `e3a6dc42-e9bf-4f44-a8d3-5584904ed0d4`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `63d5b5fb-cabb-4f12-801f-e24ad8a8b6b5`; AgentRuns: `fe0e181b-c547-4ea2-99d1-81d9eda20d2a`, `a26deb81-dc5a-4a56-ae3a-25bdc740d0d5`; 1 tool calls, 0 command records.
- Task **Verify project brief**: `12673630-d28f-47ce-9fd9-dd99fb0f7f5f`; DONE; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `70726c1e-8049-41cb-807e-51636997e544`; AgentRuns: `79207112-48e0-4c32-a591-e7335cbcb431`, `538ff88d-6646-4ebd-9a0c-94097df96865`; 1 tool calls, 0 command records.

### Reliability audit — Todo

- Mission: `770fcd8e-8980-4313-b78e-cce1cced570c`; final status: **FAILED**.
- PlanningRun: `a6160544-7b3e-400c-a933-7b5cfb3ae556`; SUCCEEDED.
- Project: `df6c8cbf-d3ab-4e42-8359-505cd4494776`.
- Task **Create project brief**: `92890177-a721-45f4-b277-1242ce40e4de`; FAILED; iteration 1; QA: not applicable to GENERAL workflow.
  TaskRun `65a1dab5-19d1-4394-b1e2-925b2d87920d`; AgentRuns: `7efc83e9-6934-4c48-a82f-5441aa4870bc`, `30e56f11-198e-4178-a1d1-01e2c41502d4`, `24194138-b609-4653-a606-7677541ec9e7`; 2 tool calls, 0 command records.
- Task **Verify project brief**: `e28007dc-8b84-4aa5-aa67-d58003f594c2`; CANCELLED; iteration 0; QA: not applicable to GENERAL workflow.

## Final working tree

```text
 M .gitignore
 M README.md
 M apps/api/app/agent_runtime/providers.py
 M apps/api/app/planning/acceptance.py
 M apps/api/app/planning/mission_planner.py
 M apps/api/app/services/artifact.py
 M apps/api/tests/test_artifacts.py
 M apps/api/tests/test_phase08.py
 M apps/api/tests/test_planner_validation.py
 M apps/web/.dockerignore
 M apps/web/app/_components/control-center/control-center.test.tsx
 M apps/web/app/_components/control-center/control-center.tsx
 M apps/web/app/_components/control-center/create-mission.test.tsx
 M apps/web/app/_components/control-center/create-mission.tsx
 M apps/web/app/_components/control-center/helpers.ts
 M apps/web/app/_components/control-center/mission-view.tsx
 M apps/web/app/api/forge/[...path]/route.ts
 M apps/web/app/layout.tsx
 M apps/web/vite.config.ts
?? apps/api/app/agent_runtime/mock_development.py
?? apps/api/tests/test_mock_development.py
?? apps/web/app/_components/control-center/helpers.test.ts
?? apps/web/app/api/forge/[...path]/route.test.ts
?? apps/web/public/favicon.svg
?? docs/reliability-audit.md
?? tests/browser/
```

Tracked diff summary (new files above are additional):

```text
 .gitignore                                         |  1 +
 README.md                                          | 37 ++++++++++++++++++++--
 apps/api/app/agent_runtime/providers.py            | 20 +++++++++---
 apps/api/app/planning/acceptance.py                |  7 +++-
 apps/api/app/planning/mission_planner.py           |  1 +
 apps/api/app/services/artifact.py                  |  2 ++
 apps/api/tests/test_artifacts.py                   | 14 ++++++++
 apps/api/tests/test_phase08.py                     |  7 ++--
 apps/api/tests/test_planner_validation.py          |  4 +++
 apps/web/.dockerignore                             |  2 ++
 .../control-center/control-center.test.tsx         | 14 ++++++--
 .../_components/control-center/control-center.tsx  | 21 +++++++-----
 .../control-center/create-mission.test.tsx         | 11 +++++++
 .../_components/control-center/create-mission.tsx  | 16 +++++++---
 apps/web/app/_components/control-center/helpers.ts | 18 ++++++++---
 .../_components/control-center/mission-view.tsx    |  2 +-
 apps/web/app/api/forge/[...path]/route.ts          | 12 +++++--
 apps/web/app/layout.tsx                            |  3 +-
 apps/web/vite.config.ts                            | 36 ++++-----------------
 19 files changed, 165 insertions(+), 63 deletions(-)
```

Final review: no whitespace errors; 29 changed/new source and documentation files checked for credential/private-key patterns with no matches. This pattern check supplements the explicit secret-presence-only configuration inspection.

