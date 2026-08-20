# TrackRelay implementation plan

This plan turns the project into a sequence of small learning steps. We will follow it one step at a time rather than implementing an entire milestone at once.

Every implementation step should end with:

- a short explanation of the new concept;
- the smallest useful code change;
- a focused automated or manual check;
- a summary of what changed and what to inspect;
- a Git commit capturing the verified step.

## Phase 0 — Repository documentation

### Step 0.1 — Initialize the repository

- [x] Initialize Git.
- [x] Use `main` as the initial branch.

**Done when:** `git status` recognizes the folder as a repository.

### Step 0.2 — Document the project

- [x] Add the project summary to `README.md`.
- [x] Add this implementation plan.

**Done when:** a new contributor can explain TrackRelay's purpose and the order in which it will be built.

## Phase 1 — Runnable application foundation

Goal: start a minimal API locally and establish the development feedback loop.

### Step 1.1 — Create the Python project metadata

- [x] Add `pyproject.toml`.
- [x] Declare the supported Python version.
- [x] Add only the dependencies needed for a minimal FastAPI application and tests.

**Learn:** what Python project metadata and dependency groups do.

### Step 1.2 — Create the smallest application package

- [x] Create the TrackRelay package.
- [x] Create a FastAPI application object.
- [x] Add no business logic yet.

**Check:** importing the application succeeds.

### Step 1.3 — Add a liveness endpoint

- [x] Add `GET /health/live`.
- [x] Return a small, stable JSON response.
- [x] Add one test.

**Learn:** liveness answers whether the process is running, not whether its dependencies are healthy.

### Step 1.4 — Add developer commands

- [x] Pin the project-local interpreter to Python 3.12.
- [x] Configure TrackRelay as an installable `src`-layout package.
- [x] Commit a cross-platform `uv.lock` dependency lockfile.
- [x] Add a `Makefile` with narrowly named commands for sync, test, lint, and run.
- [x] Document the raw `uv` commands and Make shortcuts in the README.

**Check:** the application starts locally and the liveness test passes.

### Step 1.5 — Add configuration handling

- [x] Add typed application settings.
- [x] Add `.env.example` without secrets.
- [x] Add an appropriate `.gitignore`.

**Learn:** how runtime configuration differs from source code.

### Step 1.6 — Add PostgreSQL locally

- [x] Add PostgreSQL to Docker Compose.
- [x] Add a local database URL to `.env.example`.
- [x] Verify that the database becomes healthy.

**Check:** PostgreSQL starts without requiring the API to use it yet.

### Step 1.7 — Connect SQLAlchemy

- [x] Add the SQLAlchemy engine and session factory.
- [x] Add a tiny connection check.
- [x] Keep database access out of route handlers where possible.

**Learn:** engine, connection, transaction, and session responsibilities.

### Step 1.8 — Add Alembic

- [x] Initialize migration configuration.
- [x] Make migrations use the same settings as the application.
- [x] Run an empty or metadata-baseline migration.

**Check:** a clean database can be upgraded to the latest revision.

### Step 1.9 — Add readiness

- [x] Add `GET /health/ready`.
- [x] Make readiness check database connectivity.
- [x] Test ready and not-ready behavior.

**Milestone achieved:** `local-foundation-v1` — the API starts, PostgreSQL starts, migrations run, and both health endpoints behave correctly.

## Phase 2 — First complete event path

Goal: move one Courier Alpha event through the entire synchronous system.

### Step 2.1 — Define the internal status model

- [x] Define the five initial shipment statuses.
- [x] Write tests for valid values.

### Step 2.2 — Define the normalized event schema

- [x] Define partner ID, partner event ID, tracking number, normalized status, occurrence time, and raw payload.
- [x] Keep received time separate from occurrence time.

**Learn:** external contracts and internal domain models serve different purposes.

### Step 2.3 — Define Courier Alpha's payload

- [x] Model the Courier Alpha request.
- [x] Add representative valid and invalid examples.

### Step 2.4 — Build the Courier Alpha adapter

- [x] Translate Alpha field names and status codes into the normalized event.
- [x] Test the adapter without HTTP or a database.

### Step 2.5 — Add the partners table

- [x] Store a stable partner ID, name, adapter type, and active flag.
- [x] Create and apply a migration.

### Step 2.6 — Add the shipments table

- [x] Store tracking number, current status, status occurrence time, and timestamps.
- [x] Create and apply a migration.

### Step 2.7 — Add the events table

- [x] Store normalized fields, raw payload, processing state, and whether the state transition was applied.
- [x] Add `UNIQUE(partner_id, partner_event_id)`.
- [x] Create and apply a migration.

### Step 2.8 — Persist one normalized event

- [x] Add a small repository/service operation.
- [x] Persist an event and create or update its shipment in one transaction.
- [x] Test against PostgreSQL.

### Step 2.9 — Create the downstream simulator

- [x] Create a separate minimal FastAPI application.
- [x] Add `POST /events`.
- [x] Record received normalized events in the simplest inspectable form.

### Step 2.10 — Add the ingestion endpoint

- [x] Add `POST /api/v1/partners/{partner_id}/events`.
- [x] Validate the partner and payload.
- [x] Call the Alpha adapter and application service.

### Step 2.11 — Deliver synchronously downstream

- [x] Send the normalized event to the simulator using HTTPX.
- [x] Record a basic delivery result.
- [x] Return a clear API response.

### Step 2.12 — Verify the vertical slice

- [x] Send one Courier Alpha `PICKUP` event.
- [x] Verify one event row, one shipment with `picked_up`, and one downstream receipt.
- [x] Add an end-to-end test for this path.

**Milestone achieved:** `legacy-happy-path-v1` — one courier event travels through every layer.

## Phase 3 — Idempotency

Goal: retries must not repeat business effects.

### Step 3.1 — Define duplicate behavior

- [x] Specify the response returned for an already-seen partner event.
- [x] Distinguish transport deliveries from logical events.

### Step 3.2 — Handle the uniqueness conflict

- [x] Make concurrent or repeated requests resolve to the original event.
- [x] Do not apply the shipment transition again.

### Step 3.3 — Prevent duplicate downstream effects

- [x] Ensure a duplicate request does not create another downstream delivery.
- [x] Add database and service tests.

### Step 3.4 — Add a duplicate scenario

- [x] Send the same event ten times.
- [x] Verify ten requests, one event, one state transition, one downstream effect, and nine duplicates.

**Milestone achieved:** `legacy-idempotency-v1`.

## Phase 4 — Event ordering

Goal: late events remain auditable but cannot move a shipment backward.

### Step 4.1 — Define transition rules

- [x] Define the allowed forward transitions.
- [x] Decide how equal timestamps and terminal states behave.
- [x] Cover the rules with unit tests.

### Step 4.2 — Detect stale events

- [x] Compare `occurred_at` with the shipment's current status time.
- [x] Store stale events with `state_applied = false` and reason `stale_event`.

### Step 4.3 — Add shipment history

- [x] Add `GET /api/v1/shipments/{tracking_number}/events`.
- [x] Return applied and rejected events in a documented order.

### Step 4.4 — Add an out-of-order scenario

- [x] Receive `DELIVERED` at 10:03, followed by `OUT_FOR_DELIVERY` at 10:01.
- [x] Verify both events are retained and the shipment remains delivered.

**Milestone achieved:** `legacy-ordering-v1`.

## Phase 5 — Controlled downstream failures

Goal: reproduce and measure the weaknesses of synchronous coupling.

### Step 5.1 — Add simulator control state

- [x] Add `PUT /control/mode` and `GET /control/status`.
- [x] Begin with `HEALTHY` and `RETURN_500`.

### Step 5.2 — Add slow and unavailable modes

- [x] Add `SLOW`, `TIMEOUT`, and `UNAVAILABLE` behavior.
- [x] Keep every mode deterministic and resettable.

### Step 5.3 — Add delivery attempts

- [x] Create a delivery-attempts table.
- [x] Record attempt number, result, response code, latency, error, and timestamps.

### Step 5.4 — Define transaction boundaries

- [x] Make it explicit what remains stored when downstream delivery fails.
- [x] Test timeout and server-error behavior.

### Step 5.5 — Add a small outage scenario

- [x] Send a few requests while the simulator is unavailable.
- [x] Verify API failures, persisted data, recorded attempts, and safe duplicate retries.

**Milestone achieved:** `legacy-failure-behavior-v1`.

## Phase 6 — Inspection APIs and multiple couriers

Goal: inspect the system through its API and prove the adapter design works.

### Step 6.1 — Retrieve a shipment

- [x] Add `GET /api/v1/shipments/{tracking_number}`.
- [x] Test found and not-found responses.

### Step 6.2 — Retrieve an event

- [x] Add `GET /api/v1/events/{event_id}`.
- [x] Include processing and delivery information useful for diagnosis.

### Step 6.3 — Define the adapter contract

- [x] Create one interface or protocol for all partner adapters.
- [x] Create a shared contract test suite.

### Step 6.4 — Add Courier Beta

- [x] Accept integer status codes, Unix timestamps, and Beta field names.
- [x] Reuse the shared contract tests.

### Step 6.5 — Add Courier Gamma

- [x] Accept a nested payload and UTC timestamp.
- [x] Reuse the shared contract tests.

**Milestone achieved:** `legacy-multipartner-v1`.

## Phase 7 — Experiment tracking and reconciliation

Goal: explicitly account for every generated event.

### Step 7.1 — Add test runs

- [x] Create a test-runs table.
- [x] Attach `test_run_id` to synthetic events.

### Step 7.2 — Build a deterministic generator

- [x] Generate the same dataset from the same seed.
- [x] Produce an input manifest containing its events and declared final shipment states.

### Step 7.3 — Build basic reconciliation

- [x] Compare manifest events with TrackRelay's database events.
- [x] Report accepted, rejected, unique, processed, failed, pending, and unaccounted events.

### Step 7.4 — Reconcile downstream effects

- [x] Compare database delivery attempts with simulator receipts and detect duplicate business effects.
- [x] Enforce these invariants:

```text
unique accepted events = processed + explicitly failed + pending
unaccounted events = 0
unexpected duplicate business effects = 0
incorrect final shipment states = 0
```

### Step 7.5 — Add a test-run summary endpoint

- [x] Add `GET /api/v1/test-runs/{test_run_id}/summary`.
- [x] Make the machine-readable report easy to save.

**Milestone:** `legacy-reconciliation-v1`.

## Phase 8 — Repeatable correctness and performance experiments

Goal: determine and publish the trustworthy performance envelope of the local synchronous architecture.

Phase 8 measurements have different jobs:

- **Headline measurement:** p95 response latency at each offered request rate.
- **Derived headline:** maximum sustainable throughput, defined as the highest consecutive rate that remains inside the SLO.
- **Acceptance guardrails:** request errors, dropped or missing requests, reconciliation, duplicate effects, and final shipment correctness. A point that violates a guardrail is not a valid capacity result.
- **Supporting diagnostics:** CPU, memory, database connections, and delivery rates explain a result but are not the primary comparison.

The end product is one comparison-ready legacy curve and capacity number, not a collection of unrelated metrics.

### Step 8.1 — Turn correctness scenarios into commands

- [x] Add normal, duplicate, out-of-order, and downstream-outage commands.
- [x] Reconcile every run.

### Step 8.2 — Add a k6 smoke test

- [x] Send a tiny amount of traffic.
- [x] Confirm the load script and result capture work.

### Step 8.3 — Define the baseline SLO

- [x] Start with p95 response latency below 500 ms, request errors below 1%, and zero unaccounted accepted events.
- [x] Treat these as an initial experiment definition, not a guaranteed production target.

### Step 8.4 — Add a gradual ramp test

- [x] Try 10, 25, 50, 100, 250, and 500 requests per second as the machine permits.
- [x] Stop treating higher throughput as success once the SLO is crossed.

### Step 8.5 — Run slow-downstream and outage-under-load tests

- [x] Capture latency, errors, throughput, CPU, memory, database connections, delivery rate, and reconciliation output.
- [x] Store raw results with the test configuration.

### Step 8.6 — Freeze the legacy performance envelope

- [ ] Run the healthy-downstream benchmark at 10, 25, 50, 100, 250, and 500 offered events per second using one versioned workload and SLO definition.
- [ ] Evaluate every rate independently and accept it only when p95 latency is below 500 ms, request errors are below 1%, no iterations are dropped or missing, and every correctness and reconciliation guardrail passes.
- [ ] Define legacy capacity as the last consecutive passing rate before the first failing rate; higher points may remain visible as diagnostics but cannot restore a failed envelope.
- [ ] Produce `results/legacy-baseline/benchmark-definition.json`, `summary.json`, `ramp-results.csv`, `latency-vs-load.png`, and a short `README.md` containing the environment, maximum sustainable throughput, and first SLO violation.
- [ ] Version the compact legacy-baseline artifact in Git while keeping bulky per-run raw evidence ignored or archived separately.
- [ ] Treat these files and their schema as the comparison contract reused by every Phase 9 architecture.

**Milestone:** `legacy-local-baseline-v1` — the local synchronous implementation is complete and measured.

## Phase 9 — AWS modernization

Goal: move the performance envelope and publish one defensible headline:

> **TrackRelay sustains X× more events per second after modernization while preserving the same SLO and correctness guardrails.**

The primary figure is one p95-latency-versus-offered-load plot. Its most important annotations are the sustainable-throughput boundary for the synchronous control, the boundary for the selected modernized architecture, and the improvement multiplier.

Correctness and reconciliation remain mandatory acceptance gates, not competing headline metrics. Failure recovery, resource efficiency, cloud cost, and broader operational comparisons remain useful evidence but are deferred until after the throughput/latency headline is complete.

Phase 8 proves the benchmark locally. Stage 9.1 must also establish a synchronous AWS control on comparable infrastructure, because the final modernization multiplier must not confuse an architectural improvement with a local-versus-cloud hardware difference.

### Stage 9.1 — Establish the synchronous AWS control

- [ ] Package and run the synchronous application with minimal architectural change.
- [ ] Run the frozen benchmark and guardrails unchanged to produce the authoritative synchronous AWS curve.
- [ ] Record instance type, process count, database placement, region, and benchmark-driver placement so later curves are comparable.

### Stage 9.2 — Move PostgreSQL to RDS without changing delivery

- [ ] Replace the locally managed database with RDS.
- [ ] Rerun the same headline benchmark and record whether the sustainable-throughput boundary moves.
- [ ] Keep the synchronous downstream call unchanged so the database effect remains distinguishable from the later queue effect.

### Stage 9.3 — Decouple downstream delivery with SQS and a worker

- [ ] Make ingestion persist and enqueue work.
- [ ] Move downstream delivery into a separate worker.
- [ ] Add retries and a dead-letter queue.
- [ ] Rerun the frozen benchmark with the same offered loads, SLO, and accepted-event guardrails.
- [ ] Confirm the measured API success boundary still represents durable acceptance, not merely a faster response that loses work later.

### Stage 9.4 — Containerize on ECS/Fargate

- [ ] Build separate API, worker, and simulator images.
- [ ] Deploy behind an Application Load Balancer where appropriate.
- [ ] Rerun the headline benchmark with a fixed, documented task count and resource allocation.

### Stage 9.5 — Scale and freeze the modernized envelope

- [ ] Add only the observability needed to validate load, latency, errors, durable acceptance, queue depth, and benchmark health.
- [ ] Apply a documented scaling policy, rerun the standard load points, and select the final modernized curve.
- [ ] Freeze `results/modernized-baseline/` using the same artifact schema as the legacy baseline.
- [ ] Version the compact modernized artifact so both headline inputs are durable and independently inspectable.

### Stage 9.6 — Publish the headline comparison

- [ ] Overlay the synchronous AWS control and final modernized p95-latency curves on one large plot with the 500 ms SLO line.
- [ ] Report both maximum sustainable throughputs and calculate the improvement multiplier.
- [ ] Put the plot and one-sentence result near the top of the repository README.
- [ ] Keep intermediate architectures and detailed guardrail evidence in the benchmark report rather than competing with the main result.

## Deferred follow-up results

Only after the headline throughput/latency comparison is published:

- [ ] Measure automatic delivery recovery and backlog drain after a downstream outage.
- [ ] Compare CPU, memory, database utilization, AWS cost, and operational complexity.
- [ ] Publish secondary resilience and efficiency figures without diluting the primary result.

## Explicitly out of scope at the beginning

To protect the learning sequence, do not initially add a React UI, mobile application, real courier APIs, Kafka, Kubernetes, multi-region deployment, machine learning, real notifications, or an elaborate operations dashboard.

The important early work is event semantics, correctness, failure behavior, and measurable evidence.
