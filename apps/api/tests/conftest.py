import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alembic import command
from alembic.config import Config
from app.infrastructure.database import get_engine
from app.infrastructure.redis import close_redis, get_redis_client
from app.main import app

TEST_DATABASE_NAME = "forge_test"


def database_url(database_name: str, *, async_driver: bool = False) -> str:
    admin_url = os.environ["TEST_DATABASE_ADMIN_URL"]
    url = f"{admin_url.rsplit('/', 1)[0]}/{database_name}"
    return url.replace("postgresql://", "postgresql+asyncpg://", 1) if async_driver else url


def recreate_database(name: str) -> None:
    with psycopg.connect(os.environ["TEST_DATABASE_ADMIN_URL"], autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
        connection.execute(f'CREATE DATABASE "{name}"')


def drop_database(name: str) -> None:
    with psycopg.connect(os.environ["TEST_DATABASE_ADMIN_URL"], autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


def alembic_config(database_name: str) -> Config:
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url(database_name, async_driver=True)
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    recreate_database(TEST_DATABASE_NAME)
    command.upgrade(alembic_config(TEST_DATABASE_NAME), "head")
    yield
    drop_database(TEST_DATABASE_NAME)


@pytest.fixture(autouse=True)
async def clean_database(migrated_database: None) -> AsyncIterator[None]:
    del migrated_database
    async with get_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE model_call_records, model_provider_health, product_qa_results, "
                "project_knowledge_indexes, file_context_cache, "
                "model_escalations, task_runtime_metrics, task_runtime_budgets, "
                "event_worker_health, event_dead_letters, event_consumptions, "
                "event_outbox, events, execution_jobs, worker_nodes, tool_calls, agent_runs, "
                "acceptance_verifications, qa_results, development_executions, "
                "project_development_leases, project_development_profiles, "
                "task_reviews, task_runs, task_dependencies, tasks, planning_runs, missions, "
                "mission_deletion_records, "
                "agents, projects, "
                "companies, runtime_controls CASCADE"
            )
        )
        await connection.execute(
            text("INSERT INTO runtime_controls (key, enabled) VALUES ('autonomy', false)")
        )
    await get_redis_client().flushdb()
    await close_redis()
    yield
    await get_redis_client().flushdb()
    await close_redis()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
