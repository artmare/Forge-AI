.PHONY: up down logs test lint build migrate migration seed recover-agent-runs recover-tool-calls

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

test:
	docker compose --profile test run --build --rm forge-api-test

lint:
	docker build --target lint -t forge-api-lint ./apps/api
	docker build --target lint -t forge-web-lint ./apps/web

build:
	docker compose build

migrate:
	docker compose run --build --rm forge-api alembic upgrade head

migration:
	docker compose run --build --rm -v "$(CURDIR)/apps/api:/app" forge-api alembic revision --autogenerate -m "$(message)"

seed:
	docker compose exec forge-api python -m app.seed

recover-agent-runs:
	docker compose exec forge-api python -m app.commands.recover_agent_runs

recover-tool-calls:
	docker compose exec forge-api python -m app.commands.recover_tool_calls
