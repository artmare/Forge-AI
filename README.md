# Forge

Forge Dev Mode architecture and configuration are documented in
[`docs/forge-dev-mode.md`](docs/forge-dev-mode.md).

Forge is an Autonomous Company OS. This repository contains the Phase 08 Mission planning runtime:
domain APIs, a concurrency-safe task state machine, provider-neutral multi-turn model execution,
durable AgentRun and ToolCall history, strict permission enforcement, isolated project workspaces,
deterministic scheduling, leased workers, PostgreSQL transactional outbox, Redis Streams, live
dashboard activity, typed Mission plans, atomic Company/Project factories, Task dependency DAGs,
human review gates, and automated tests.

Users explicitly create and plan Missions, inspect a validated proposal, and activate it before Forge
materializes topology. Queued tasks can be picked up after explicit resume. Autonomy is persistently paused
by default after migration. The default mock provider is deterministic and free. The manual
`POST /api/v1/tasks/{id}/execute` debug path remains available. Phase 08 adds no shell/network/browser
tools, autonomous goal selection, autonomous Mission creation, or human-review bypass.

## Prerequisites

- Docker Desktop (or Docker Engine with Compose)
- GNU Make is optional; every Make target wraps a Docker command

Node.js and Python are only needed when running checks directly on the host.

## Configuration

Copy `.env.example` to `.env` and adjust the local-development values if needed:

```sh
cp .env.example .env
```

Compose also supplies development defaults, so the stack can start without a `.env` file. Never commit `.env` or production credentials.

Model execution defaults are deliberately safe:

```env
MODEL_PROVIDER=mock
ALLOW_PAID_MODEL_CALLS=false
STORE_MODEL_INPUTS=false
```

Keep `OPENAI_API_KEY` only in `.env`, never in `.env.example`. See [docs/agent-runtime.md](docs/agent-runtime.md) before opting into paid calls.

## Start and stop

```sh
make up
# Equivalent: docker compose up --build -d
```

Open the dashboard at <http://localhost:3000>. The API is available at <http://localhost:8000>; interactive API documentation is at <http://localhost:8000/docs>.

```sh
make logs
make down
```

To remove local database and Redis volumes as well, run `docker compose down --volumes` intentionally.

## Validation

```sh
make test   # backend automated tests in a reproducible image
make lint   # backend Ruff plus frontend ESLint and TypeScript
make build  # production container builds
make migrate
make migration message="describe schema change"
make seed   # explicit, idempotent development sample data
make recover-agent-runs  # fail stale RUNNING executions safely
make recover-tool-calls  # reconcile stale RUNNING tool calls safely
```

Useful health checks:

```sh
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/system
curl http://localhost:8000/api/v1/events/stats
curl 'http://localhost:8000/api/v1/events?limit=10'
curl http://localhost:8000/api/v1/agent-runs/stats
curl http://localhost:8000/api/v1/tools
curl http://localhost:8000/api/v1/tool-calls/stats
curl http://localhost:8000/api/v1/orchestrator/status
curl http://localhost:8000/api/v1/workers
curl http://localhost:8000/api/v1/execution-jobs
curl http://localhost:8000/api/v1/missions
```

Task lifecycle example:

```sh
curl -X POST http://localhost:8000/api/v1/tasks/TASK_ID/transition \
  -H 'Content-Type: application/json' \
  -d '{"target_status":"QUEUED","reason":"Ready for execution"}'

curl http://localhost:8000/api/v1/tasks/TASK_ID/runs

curl -X POST http://localhost:8000/api/v1/orchestrator/resume

curl -X POST http://localhost:8000/api/v1/tasks/TASK_ID/execute \
  -H 'Content-Type: application/json' \
  -d '{"model_alias":"default"}'
```

For host-native development:

```sh
cd apps/web && npm ci && npm run dev
cd apps/api && python -m pip install -r requirements-dev.txt && uvicorn app.main:app --reload
```

The API requires `DATABASE_URL` and `REDIS_URL` to point at reachable services. API container startup applies committed Alembic migrations; application code never creates tables implicitly. See `.env.example` and `docker-compose.yml` for the expected variables.

## Repository architecture

- `apps/web` — Next.js App Router dashboard with TypeScript and Tailwind CSS
- `apps/api` — async FastAPI API, domain models, Alembic migrations, repositories, services, schemas, tests, and connectivity adapters
- `services` — reserved project-level boundary; runtime and workers live with the API application
- `packages` — reserved placeholder for future shared packages
- `infra` — infrastructure notes and future configuration boundary
- `tests` — project-level test placeholder
- `docs` — architecture documentation

See [docs/architecture.md](docs/architecture.md) for service boundaries,
[docs/agent-runtime.md](docs/agent-runtime.md) for model execution,
[docs/tool-system.md](docs/tool-system.md) for permission and workspace security,
[docs/event-system.md](docs/event-system.md) for delivery/recovery semantics,
[docs/domain-model.md](docs/domain-model.md) for persistence relationships, and
[docs/task-state-machine.md](docs/task-state-machine.md) for lifecycle rules.

See [docs/orchestrator.md](docs/orchestrator.md) for deterministic scheduling and recovery, and
[docs/worker-runtime.md](docs/worker-runtime.md) for worker claims, leases, health, and shutdown.
See [docs/missions.md](docs/missions.md), [docs/planner.md](docs/planner.md),
[docs/company-factory.md](docs/company-factory.md), and
[docs/task-dependencies.md](docs/task-dependencies.md) for the Phase 08 goal-to-graph workflow.
