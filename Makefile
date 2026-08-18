UV := uv
COMPOSE := docker compose
SEED ?= 20260806
SHIPMENTS ?= 3
OUTPUT ?= results/input-manifest.json
MANIFEST ?= results/input-manifest.json
REPORT ?= results/reconciliation.json
API_URL ?= http://127.0.0.1:8000
TEST_RUN_ID ?=
SUMMARY ?= results/test-run-summary.json
CORRECTNESS_OUTPUT ?= results/correctness

.PHONY: sync test test-integration lint run run-downstream generate reconcile summary scenario-normal scenario-duplicate scenario-out-of-order scenario-downstream-outage db-up db-status db-check db-down migrate migration-status

sync:
	$(UV) sync --locked --python 3.12

test:
	$(UV) run --locked pytest

test-integration:
	$(UV) run --locked pytest -o addopts='' -m integration

lint:
	$(UV) run --locked ruff check .

run:
	$(UV) run --locked uvicorn trackrelay.main:app --reload --host 127.0.0.1 --port 8000

run-downstream:
	$(UV) run --locked uvicorn trackrelay.downstream.main:app --reload --host 127.0.0.1 --port 8001

generate:
	$(UV) run --locked trackrelay-generate --seed $(SEED) --shipments $(SHIPMENTS) --output $(OUTPUT)

reconcile:
	$(UV) run --locked trackrelay-reconcile --manifest $(MANIFEST) --output $(REPORT)

summary:
	test -n "$(TEST_RUN_ID)"
	mkdir -p "$(dir $(SUMMARY))"
	curl --fail --silent --show-error "$(API_URL)/api/v1/test-runs/$(TEST_RUN_ID)/summary" --output "$(SUMMARY)"

scenario-normal:
	$(UV) run --locked trackrelay-scenario normal --seed $(SEED) --shipments $(SHIPMENTS) --api-url $(API_URL) --output-root $(CORRECTNESS_OUTPUT)

scenario-duplicate:
	$(UV) run --locked trackrelay-scenario duplicate --seed $(SEED) --shipments $(SHIPMENTS) --api-url $(API_URL) --output-root $(CORRECTNESS_OUTPUT)

scenario-out-of-order:
	$(UV) run --locked trackrelay-scenario out-of-order --seed $(SEED) --shipments $(SHIPMENTS) --api-url $(API_URL) --output-root $(CORRECTNESS_OUTPUT)

scenario-downstream-outage:
	$(UV) run --locked trackrelay-scenario downstream-outage --seed $(SEED) --shipments $(SHIPMENTS) --api-url $(API_URL) --output-root $(CORRECTNESS_OUTPUT)

db-up:
	$(COMPOSE) up -d --wait postgres

db-status:
	$(COMPOSE) ps postgres

db-check:
	$(UV) run --locked python -m trackrelay.database

db-down:
	$(COMPOSE) down

migrate:
	$(UV) run --locked alembic upgrade head

migration-status:
	$(UV) run --locked alembic current
