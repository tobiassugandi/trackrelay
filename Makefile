UV := uv
COMPOSE := docker compose

.PHONY: sync test lint run db-up db-status db-down

sync:
	$(UV) sync --locked --python 3.12

test:
	$(UV) run --locked pytest

lint:
	$(UV) run --locked ruff check .

run:
	$(UV) run --locked uvicorn trackrelay.main:app --reload --host 127.0.0.1 --port 8000

db-up:
	$(COMPOSE) up -d --wait postgres

db-status:
	$(COMPOSE) ps postgres

db-down:
	$(COMPOSE) down
