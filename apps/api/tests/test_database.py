from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from alembic.script import ScriptDirectory
from app.infrastructure.database import get_session_factory
from tests.conftest import TEST_DATABASE_NAME, alembic_config, drop_database, recreate_database


async def test_migration_is_at_head() -> None:
    config = alembic_config(TEST_DATABASE_NAME)
    heads = ScriptDirectory.from_config(config).get_heads()
    async with get_session_factory()() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))

    assert heads == ["20260929_0020"]
    assert revision == heads[0]


def test_migration_upgrade_downgrade_lifecycle() -> None:
    name = "forge_migration_test"
    recreate_database(name)
    try:
        config = alembic_config(name)
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        drop_database(name)


async def test_required_and_foreign_key_constraints() -> None:
    async with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO companies (slug, goal) VALUES ('missing-name', 'goal')")
            )
            await session.commit()
        await session.rollback()

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO projects (company_id, name, goal) "
                    "VALUES (:company_id, 'Orphan', 'Should fail')"
                ),
                {"company_id": uuid4()},
            )
            await session.commit()


async def test_company_id_is_immutable() -> None:
    async with get_session_factory()() as session:
        company_id = await session.scalar(
            text(
                "INSERT INTO companies (name, slug, goal) "
                "VALUES ('Immutable', 'immutable', 'Keep identity') RETURNING id"
            )
        )
        await session.commit()

        with pytest.raises(DBAPIError, match="company id is immutable"):
            await session.execute(
                text("UPDATE companies SET id = :new_id WHERE id = :old_id"),
                {"new_id": uuid4(), "old_id": company_id},
            )
            await session.commit()
