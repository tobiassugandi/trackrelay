UV := uv
COMPOSE := docker compose
DOCKER := docker
TERRAFORM := terraform
TERRAFORM_DIR := infra/terraform
API_IMAGE ?= trackrelay-api:local
API_INGRESS_CIDR ?= 127.0.0.1/32
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
BASELINE_RATES ?= 10,25,50,100,250,500
BASELINE_TIER_DURATION_SECONDS ?= 10
BASELINE_OUTPUT ?= results/legacy-baseline
BASELINE_RAW_OUTPUT ?= results/raw/legacy-baseline
TRACKRELAY_AWS_PROFILE ?= trackrelay-admin
TRACKRELAY_AWS_REGION ?= ap-southeast-3
AWS_MONTHLY_BUDGET_USD ?= 25
AWS_SESSION_RESULTS ?= results/aws-sessions
SESSION_ID ?=
APPROVED_SESSION_ID ?=
APPROVED_COST_CEILING_USD ?=
REHOST_COMPOSE_FILE ?= deploy/rehost/compose.yaml
REHOST_INSTALLER ?= deploy/rehost/install.sh
REHOST_RDS_INSTALLER ?= deploy/rehost/install-rds.sh
REHOST_INSTANCE_TYPE ?= t4g.small
TARGET_INSTANCE_TYPE ?=
APPROVED_TARGET_INSTANCE_TYPE ?=

.PHONY: sync test test-integration lint run run-downstream image-api image-api-smoke rehost-config rehost-smoke generate reconcile summary scenario-normal scenario-duplicate scenario-out-of-order scenario-downstream-outage load-smoke load-prepare load-ramp load-slow load-outage load-baseline aws-check aws-plan aws-up aws-rehost-publish aws-rehost-deploy aws-rehost-workload aws-rds-deploy aws-rds-correctness aws-scaling-prepare aws-scaling-run-tier aws-scaling-transition aws-scaling-report aws-down aws-verify-down infra-init infra-check db-up db-status db-check db-down migrate migration-status

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

image-api:
	$(DOCKER) build --file Dockerfile --tag "$(API_IMAGE)" .

image-api-smoke: image-api
	DOCKER="$(DOCKER)" ./scripts/smoke-api-image.sh "$(API_IMAGE)"

rehost-config:
	$(COMPOSE) \
		--env-file deploy/rehost/.env.example \
		--file deploy/rehost/compose.yaml \
		config --quiet

rehost-smoke: rehost-config
	DOCKER="$(DOCKER)" UV="$(UV)" \
		./scripts/smoke-rehost.sh "$(API_IMAGE)"

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

load-baseline:
	$(UV) run --locked trackrelay-baseline \
		--rates $(BASELINE_RATES) \
		--tier-duration-seconds $(BASELINE_TIER_DURATION_SECONDS) \
		--api-url $(API_URL) \
		--container-api-url $(K6_API_URL) \
		--k6-image $(K6_IMAGE) \
		--output-root $(BASELINE_OUTPUT) \
		--raw-output-root $(BASELINE_RAW_OUTPUT)

aws-check:
	$(UV) run --locked trackrelay-aws-check \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)"

infra-init:
	$(TERRAFORM) -chdir=$(TERRAFORM_DIR) init -backend=false -input=false

infra-check:
	$(TERRAFORM) -chdir=$(TERRAFORM_DIR) fmt -check -recursive
	$(TERRAFORM) -chdir=$(TERRAFORM_DIR) validate
	$(TERRAFORM) -chdir=$(TERRAFORM_DIR) test

aws-plan:
	$(UV) run --locked trackrelay-aws-session plan \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-up:
	$(UV) run --locked trackrelay-aws-session apply \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)" \
		--approved-session-id "$(APPROVED_SESSION_ID)" \
		--approved-cost-ceiling-usd "$(APPROVED_COST_CEILING_USD)" \
		--monthly-budget-usd "$(AWS_MONTHLY_BUDGET_USD)"

aws-rehost-publish:
	$(UV) run --locked trackrelay-aws-rehost publish \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-rehost-deploy:
	$(UV) run --locked trackrelay-aws-rehost deploy \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)" \
		--compose-file "$(REHOST_COMPOSE_FILE)" \
		--installer "$(REHOST_INSTALLER)"

aws-rehost-workload:
	$(UV) run --locked trackrelay-aws-rehost workload \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-rds-deploy:
	$(UV) run --locked trackrelay-aws-rehost deploy-rds \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)" \
		--compose-file "$(REHOST_COMPOSE_FILE)" \
		--installer "$(REHOST_RDS_INSTALLER)"

aws-rds-correctness:
	$(UV) run --locked trackrelay-aws-rehost correctness-rds \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-scaling-prepare:
	$(UV) run --locked trackrelay-aws-vertical-scaling prepare \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-scaling-run-tier:
	$(UV) run --locked trackrelay-aws-vertical-scaling run-tier \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-scaling-transition:
	$(UV) run --locked trackrelay-aws-vertical-scaling transition \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--target-instance-type "$(TARGET_INSTANCE_TYPE)" \
		--approved-session-id "$(APPROVED_SESSION_ID)" \
		--approved-target-instance-type "$(APPROVED_TARGET_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-scaling-report:
	$(UV) run --locked trackrelay-aws-vertical-scaling-report \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-down:
	$(UV) run --locked trackrelay-aws-session destroy \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

aws-verify-down:
	$(UV) run --locked trackrelay-aws-session verify \
		--session-id "$(SESSION_ID)" \
		--profile "$(TRACKRELAY_AWS_PROFILE)" \
		--region "$(TRACKRELAY_AWS_REGION)" \
		--api-ingress-cidr "$(API_INGRESS_CIDR)" \
		--rehost-instance-type "$(REHOST_INSTANCE_TYPE)" \
		--terraform-dir "$(TERRAFORM_DIR)" \
		--evidence-root "$(AWS_SESSION_RESULTS)"

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
