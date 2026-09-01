from fastapi import APIRouter

from app.api.routes import (
    agent_runs,
    agents,
    companies,
    development,
    efficient_runtime,
    events,
    execution_jobs,
    health,
    missions,
    model_economics,
    orchestrator,
    projects,
    system,
    tasks,
    tool_calls,
    workers,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(system.router)
api_router.include_router(missions.router)
api_router.include_router(model_economics.router)
api_router.include_router(companies.router)
api_router.include_router(development.router)
api_router.include_router(efficient_runtime.router)
api_router.include_router(projects.router)
api_router.include_router(agents.router)
api_router.include_router(tasks.router)
api_router.include_router(agent_runs.router)
api_router.include_router(tool_calls.router)
api_router.include_router(events.router)
api_router.include_router(execution_jobs.router)
api_router.include_router(workers.router)
api_router.include_router(orchestrator.router)
