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
K6_IMAGE ?= grafana/k6:2.1.0
K6_API_URL ?= http://host.docker.internal:8000
K6_OUTPUT ?= results/k6/smoke-summary.json
RAMP_PARTNER_ID ?= load-alpha
RAMP_RATES ?= 10,25,50,100,250,500
RAMP_TIER_DURATION_SECONDS ?= 10
RAMP_RUN_ID ?=
K6_RAMP_OUTPUT ?= results/k6/ramp-summary.json
LOAD_RATE ?= 5
LOAD_DURATION_SECONDS ?= 5
PERFORMANCE_OUTPUT ?= results/performance

.PHONY: sync test test-integration lint run run-downstream generate reconcile summary scenario-normal scenario-duplicate scenario-out-of-order scenario-downstream-outage load-smoke load-prepare load-ramp load-slow load-outage db-up db-status db-check db-down migrate migration-status

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

load-smoke:
	mkdir -p "$(dir $(K6_OUTPUT))"
	docker run --rm \
		--add-host host.docker.internal:host-gateway \
		--env TRACKRELAY_API_URL="$(K6_API_URL)" \
		--env K6_SUMMARY_PATH="/results/$(notdir $(K6_OUTPUT))" \
		--volume "$(CURDIR)/load:/scripts:ro" \
		--volume "$(abspath $(dir $(K6_OUTPUT))):/results" \
		$(K6_IMAGE) run /scripts/smoke.js

load-prepare:
	$(UV) run --locked trackrelay-prepare-load --partner-id $(RAMP_PARTNER_ID)

load-ramp: load-prepare
	mkdir -p "$(dir $(K6_RAMP_OUTPUT))"
	ramp_run_id="$(RAMP_RUN_ID)"; \
	if [ -z "$$ramp_run_id" ]; then \
		ramp_run_id="$$( $(UV) run --locked python -c 'from uuid import uuid4; print(uuid4())' )"; \
	fi; \
	docker run --rm \
		--add-host host.docker.internal:host-gateway \
		--env TRACKRELAY_API_URL="$(K6_API_URL)" \
		--env RAMP_PARTNER_ID="$(RAMP_PARTNER_ID)" \
		--env RAMP_RATES="$(RAMP_RATES)" \
		--env RAMP_TIER_DURATION_SECONDS="$(RAMP_TIER_DURATION_SECONDS)" \
		--env RAMP_RUN_ID="$$ramp_run_id" \
		--env K6_SUMMARY_PATH="/results/$(notdir $(K6_RAMP_OUTPUT))" \
		--volume "$(CURDIR)/load:/scripts:ro" \
		--volume "$(abspath $(dir $(K6_RAMP_OUTPUT))):/results" \
		$(K6_IMAGE) run /scripts/ramp.js

load-slow:
	$(UV) run --locked trackrelay-load-experiment slow --rate $(LOAD_RATE) --duration-seconds $(LOAD_DURATION_SECONDS) --api-url $(API_URL) --k6-image $(K6_IMAGE) --output-root $(PERFORMANCE_OUTPUT)

load-outage:
	$(UV) run --locked trackrelay-load-experiment outage --rate $(LOAD_RATE) --duration-seconds $(LOAD_DURATION_SECONDS) --api-url $(API_URL) --k6-image $(K6_IMAGE) --output-root $(PERFORMANCE_OUTPUT)

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
