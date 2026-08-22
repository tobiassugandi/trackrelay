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

Goal: determine and publish the trustworthy fixed-capacity performance envelope of the local synchronous architecture, while proving the benchmark machinery that Phase 9 will reuse.

Phase 8 measurements have different jobs:

- **Local reference measurement:** p95 response latency at each offered request rate.
- **Derived local result:** maximum sustainable throughput, defined as the highest consecutive rate that remains inside the SLO.
- **Acceptance guardrails:** request errors, dropped or missing requests, reconciliation, duplicate effects, and final shipment correctness. A point that violates a guardrail is not a valid capacity result.
- **Supporting diagnostics:** CPU, memory, database connections, and delivery rates explain a result but are not the primary comparison.

The end product is one reproducible legacy curve and capacity number, not a collection of unrelated metrics. It is the project's first measured end product and a benchmark rehearsal, but it is not the causal control for the final cloud-elasticity claim.

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

- [x] Run the healthy-downstream benchmark at 10, 25, 50, 100, 250, and 500 offered events per second using one versioned workload and SLO definition.
- [x] Evaluate every rate independently and accept it only when p95 latency is below 500 ms, request errors are below 1%, no iterations are dropped or missing, and every correctness and reconciliation guardrail passes.
- [x] Define legacy capacity as the last consecutive passing rate before the first failing rate; higher points may remain visible as diagnostics but cannot restore a failed envelope.
- [x] Produce `results/legacy-baseline/benchmark-definition.json`, `summary.json`, `ramp-results.csv`, `latency-vs-load.png`, and a short `README.md` containing the environment, maximum sustainable throughput, and first SLO violation.
- [x] Version the compact legacy-baseline artifact in Git while keeping bulky per-run raw evidence ignored or archived separately.
- [x] Treat the workload definition, rate semantics, SLO, reconciliation guardrails, and core result fields as the foundation for Phase 9; the elasticity experiment will add aligned queue-depth and worker-count time series.

**Measured result:** the recorded local system passes every complete gate through 250 offered events/s. At 500 events/s, observed throughput is about 243 events/s, 2,324 iterations are dropped, p95 latency is 1,506 ms, and the final-state guardrail fails for the missing requests. The frozen workload uses one distinct `CREATED` shipment per request so the envelope measures ingestion and synchronous delivery capacity without artificial same-shipment races.

**Milestone:** `legacy-local-baseline-v1` — the local synchronous implementation is complete and measured.

## Phase 9 — AWS modernization

Goal: answer one cloud-specific question:

> **Can TrackRelay automatically acquire and release processing capacity as demand changes while continuing to meet its latency, completion, and correctness requirements?**

The intended headline is:

> **AWS-modernized TrackRelay sustained an X× traffic increase by automatically scaling from A to B worker tasks, maintained every latency, completion, and correctness guardrail, and returned to A tasks when demand normalized.**

The primary figure will be one aligned time-series story: offered load rises and falls, running worker tasks follow it, queue depth stays bounded and drains, and p95 latency remains below the SLO. Scale-out alone is insufficient; returning to the minimum worker count after demand falls is required to demonstrate elasticity rather than permanent overprovisioning.

The causal experiment compares the same modernized AWS deployment with **worker autoscaling off** and **worker autoscaling on**. SQS, ECS task definitions, RDS, fixed API capacity, workload, SLO, and minimum worker count must remain the same. Only the worker-capacity policy changes. The asynchronous architecture is a prerequisite that makes delivery independently scalable; access to additional on-demand compute is the cloud capability being tested.

The local legacy baseline remains important as the starting point and benchmark rehearsal, but it is not the denominator for the final X× elasticity claim. A local asynchronous implementation is optional and is not required for this experiment.

### Phase 9 operating model

AWS is **off by default**. Application code, container builds, experiment automation, and infrastructure definitions are developed and tested locally. AWS is used only for bounded validation or measurement sessions:

1. **Cloud session 1 — synchronous migration:** validate Stages 9.1 and 9.2, collect evidence, then destroy the stack.
2. **Cloud session 2 — asynchronous integration:** validate Stages 9.3 and 9.4 with tiny workloads, collect evidence, then destroy the stack.
3. **Cloud session 3 — headline experiment:** provision once, run the Stage 9.5 fixed control and Stage 9.6 elastic treatment back-to-back, collect both result sets, then destroy the stack.

Infrastructure as code is the reproducible source of truth. The AWS console may be used to learn, inspect, and troubleshoot, but repeatable creation and teardown must come from the repository. Synthetic experiment resources are disposable: each cloud session must remove RDS, load balancers, ECS services and tasks, NAT gateways if used, and every other session-owned billable resource. Retain definitions and evidence, not idle infrastructure.

Stages 9.5 and 9.6 are one controlled experiment session. Between them, reset application data and measurements but do not recreate infrastructure, deploy different code, change task definitions, resize the API or database, or move the load generator. The worker autoscaling policy is the only treatment variable.

### Ownership and user handoffs

Most of Phase 9 remains Codex implementation work. The human owner is needed only where AWS requires account ownership, private authentication, a spending decision, or an explicit decision to start a billable cloud session.

| Stage | Owner | What you need to do |
| --- | --- | --- |
| 9.0 | **You + Codex** | You create or secure the AWS account, enable MFA, establish the working identity, receive budget alerts, privately complete authentication, and approve the region and spending ceiling. Codex supplies guidance, repository configuration, checks, and documentation. |
| 9.1 | **Codex** | No routine input after the Stage 9.0 choices; review only if a deployment choice changes scope or expected cost. |
| 9.2 | **Codex, with your cloud-session approval** | Before cloud session 1, explicitly authorize the session and its budget and complete MFA or browser sign-in if AWS requests it. Codex provisions, validates, collects evidence, destroys, and verifies teardown. |
| 9.3 | **Codex** | No routine input; this is local application development and testing. |
| 9.4 | **Codex, with your cloud-session approval** | Provide the same authorization and private authentication handoff for cloud session 2. |
| 9.5–9.6 | **Codex, with your cloud-session approval** | Approve cloud session 3 and its experiment cost ceiling and complete any private authentication. Codex runs both treatments in the same session and tears everything down only after both result sets are secured. |
| 9.7 | **Codex** | No required input unless you want to review or revise the final headline and presentation. |

You never need to send Codex an AWS password, MFA code, root credential, secret access key, or payment information. When interactive authentication is necessary, you enter it directly into AWS or the AWS CLI's browser flow. Before each cloud session, Codex must present the intended resources, region, estimated duration, cost guardrail, and teardown command; your ordinary `continue` is not sufficient authorization to begin incurring AWS charges unless it explicitly refers to that prepared session.

### Stage 9.0 — Prepare safe, reproducible AWS access

- [x] **You:** Create or open the AWS account and create or select the non-root working identity for Phase 9.
- [x] **You:** Enable MFA for the AWS account's root user.
- [x] **You:** Ensure the account has valid billing details and configure a USD 25 monthly AWS budget with alert delivery.
- [x] **Together:** Use Asia Pacific (Jakarta), `ap-southeast-3`; Codex verified current regional support for the planned core services against AWS's regional service documentation.
- [ ] **Together:** Agree on cloud session 1's cost ceiling after Codex presents its concrete resource list and estimate; the ceiling must fit within the remaining USD 25 monthly budget.
- [x] **You, privately:** Complete AWS CLI authentication using the non-root `trackrelay-admin` profile; no credentials belong in the repository or chat.
- [x] **Codex:** Verify without printing account identifiers or secrets that `trackrelay-admin` authenticates as a non-root IAM user and selects `ap-southeast-3`.
- [ ] **Codex (repository work):** Make future AWS commands accept a project profile and region explicitly, using `trackrelay-admin` and `ap-southeast-3` for this machine without hard-coding credentials. This prevents an unrelated ambient `AWS_PROFILE` value from selecting the wrong profile.
- [ ] **Codex:** Add the initial infrastructure-as-code structure with explicit provision and destroy workflows.
- [ ] **Codex:** Write a short cloud-session checklist covering provision, validation, evidence collection, destroy, and post-destroy verification.
- [ ] **Codex:** Define how each session will prove that its billable resources have actually been removed.

The USD 25 AWS Budget is a monthly monitoring and alerting guardrail, not an assumed hard spending stop. Each cloud session therefore still needs its own estimate, explicit approval, bounded duration, and verified teardown. Automatic budget actions may be considered separately, but they do not replace teardown automation.

### Stage 9.1 — Prepare the synchronous rehost locally

Do not start cloud session 1 until the local-preparation items in both Stages 9.1 and 9.2 are complete.

- [ ] Package the synchronous application for AWS with minimal architectural change.
- [ ] Define the rehost infrastructure and deployment configuration as code.
- [ ] Automate deployment, migrations, health checks, smoke tests, workload execution, and result collection.

### Stage 9.2 — Provision RDS for the AWS deployment

- [ ] Define the RDS instance, networking, configuration, and secrets integration as code locally.

Use cloud session 1 to validate both Stages 9.1 and 9.2:

- [ ] **You:** Explicitly authorize cloud session 1 after reviewing its resource list, region, estimated duration, cost guardrail, and teardown command.
- [ ] Provision and run the synchronous rehost in AWS.
- [ ] Run the frozen workload and guardrails to verify that the benchmark is portable to the AWS environment.
- [ ] Record instance type, process count, database placement, region, and benchmark-driver placement as migration evidence, not as the causal elasticity control.
- [ ] Provision PostgreSQL on RDS during cloud session 1.
- [ ] Point TrackRelay at RDS and verify connectivity, migrations, and persistence.
- [ ] Rerun the core correctness scenarios against RDS.
- [ ] Record and freeze the RDS configuration intended for every later fixed-capacity and elastic performance experiment.
- [ ] Collect the session evidence, destroy the complete session-1 stack, and verify the teardown.

RDS provides the stable managed data layer for the target architecture; it is setup for the elasticity experiment, not a separately benchmarked intervention.

### Stage 9.3 — Decouple downstream delivery with SQS and a worker

- [ ] Define a narrow queue interface and develop ingestion and worker behavior locally with deterministic fakes; do not require a local AWS emulator or substitute message broker.
- [ ] Make ingestion persist and enqueue work through that interface.
- [ ] Move downstream delivery into a separate worker and implement the real SQS adapter behind the same interface.
- [ ] Add retries and a dead-letter queue.
- [ ] Define durable acceptance precisely and confirm that a fast API response cannot hide lost work.
- [ ] Add processing guardrails: every accepted event is accounted for, duplicate business effects remain zero, final shipment states are correct, and the queue drains by a documented deadline after offered load falls.

### Stage 9.4 — Containerize on ECS/Fargate

- [ ] Build and test separate API, worker, and simulator images locally.
- [ ] Define ECS/Fargate, ECR, SQS, dead-letter queue, RDS, networking, load balancing, secrets, and observability as code.
- [ ] Define CloudWatch metrics for offered load, API p95 latency, request errors, running worker tasks, queue depth, and message age or processing lag.
- [ ] Configure the API at a fixed, documented capacity with enough headroom that worker delivery capacity is the variable under test.

Use cloud session 2 as a small integration checkpoint:

- [ ] **You:** Explicitly authorize cloud session 2 after reviewing its resource list, region, estimated duration, cost guardrail, and teardown command.
- [ ] Provision the complete asynchronous stack and deploy the locally tested artifacts.
- [ ] Exercise 1-, 10-, and 100-event workloads before attempting a performance experiment.
- [ ] Verify the full path through the load balancer, API, RDS, SQS, worker, simulator, dead-letter queue, and CloudWatch.
- [ ] Reconcile every accepted event and confirm that the new processing and drain guardrails work.
- [ ] Collect integration evidence, destroy the complete session-2 stack, and verify the teardown.

### Stage 9.5 — Establish the fixed-capacity modernized control

- [ ] Make provision, fixed experiment, application-state reset, elastic experiment, result collection, and teardown reproducible through `make aws-up`, `make experiment-fixed`, `make experiment-reset`, `make experiment-elastic`, `make collect-results`, and `make aws-down` (or clearly documented equivalents).
- [ ] Test the workload driver, reset procedure, metrics collection, reconciliation, and plot generation locally before starting cloud session 3.
- [ ] **You:** Explicitly authorize cloud session 3 after reviewing its resource list, region, expected experiment duration, cost ceiling, and teardown command.
- [ ] Provision the final experiment environment once and record its immutable application and infrastructure versions.
- [ ] Disable worker autoscaling and fix the worker tier at its documented minimum task count.
- [ ] Run a stepped workload that rises beyond fixed worker capacity and later returns to the starting rate.
- [ ] Define a sustainable end-to-end load using ingestion SLOs plus bounded backlog, completion, drain-deadline, and correctness guardrails; API latency alone is insufficient.
- [ ] Freeze the fixed-control configuration and aligned time series in `results/aws-fixed-control/`.
- [ ] Leave the deployment unchanged and continue directly into Stage 9.6; do not tear it down or redeploy it between treatments.

### Stage 9.6 — Enable and measure worker elasticity

- [ ] Reset application data, queues, simulator state, and measurements without recreating or resizing the infrastructure.
- [ ] Enable a documented worker scaling policy with the same minimum task count and a bounded maximum; use queue backlog or backlog per task as the demand signal.
- [ ] Replay the fixed-control workload without changing the application, task definition, API capacity, database, simulator, benchmark driver, SLO, or guardrails.
- [ ] Verify that workers scale from A to B as load rises, backlog remains bounded and drains, and workers return to A after demand falls.
- [ ] Freeze the scaling policy, environment, raw aligned time series, reconciliation evidence, and summary in `results/aws-elastic-treatment/`.
- [ ] Collect both treatments' evidence, destroy the complete session-3 stack, and verify the teardown.

### Stage 9.7 — Publish the elasticity headline

- [ ] Analyze the frozen results and build the report locally with AWS off.
- [ ] Produce one large, aligned time-series figure comparing fixed and elastic runs across offered load, running worker tasks, queue depth or message age, and p95 latency with its 500 ms SLO line.
- [ ] Report the highest demand step that satisfies every end-to-end guardrail in each run, the load multiplier, worker expansion A→B, time to scale out, backlog drain time, and return to A.
- [ ] Put the figure and one-sentence elasticity result near the top of the repository README.
- [ ] Explain the causal chain plainly: SQS exposes pending demand, autoscaling responds, ECS changes the worker count, and AWS supplies and releases compute without TrackRelay owning spare hardware.
- [ ] Keep migration-stage measurements, fixed-resource architecture effects, and detailed guardrail evidence in the benchmark report rather than competing with the main result.

## Deferred follow-up results

Only after the headline elasticity result is published:

- [ ] Measure automatic delivery recovery and backlog drain after a downstream outage.
- [ ] Compare fixed-resource throughput effects, CPU, memory, database utilization, AWS cost, and operational complexity.
- [ ] Publish secondary resilience and efficiency figures without diluting the primary result.

## Explicitly out of scope at the beginning

To protect the learning sequence, do not initially add a React UI, mobile application, real courier APIs, Kafka, Kubernetes, multi-region deployment, machine learning, real notifications, or an elaborate operations dashboard.

The important early work is event semantics, correctness, failure behavior, and measurable evidence.
