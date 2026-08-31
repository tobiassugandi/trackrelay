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

Goal: demonstrate two progressively stronger cloud capabilities:

1. **Rapid infrastructure flexibility:** can TrackRelay gain healthy-downstream
   capacity by changing compute hardware without changing application code?
2. **Automatic elasticity:** can TrackRelay acquire and release processing
   capacity as demand changes while continuing to meet its latency, completion,
   and correctness requirements?

The first supporting result will be a deliberately short two-machine story:

> **With the application and RDS configuration held constant, TrackRelay changed
> from `t3.small` to `c7i-flex.large` and increased its highest passing 10-second
> load point from X to Y events/s without an application redesign.**

This hardware-flexibility experiment answers the reasonable question, "Why
not choose more suitable hardware first?" It tests the practical benefit of
changing to a more suitable cloud machine without acquiring physical hardware.
It does not diagnose which changed hardware property caused the result, and it
is not automatic elasticity.

The primary cloud-specific question remains:

> **Can TrackRelay automatically acquire and release processing capacity as demand changes while continuing to meet its latency, completion, and correctness requirements?**

The intended headline is:

> **AWS-modernized TrackRelay sustained an X× traffic increase by automatically scaling from A to B worker tasks, maintained every latency, completion, and correctness guardrail, and returned to A tasks when demand normalized.**

The primary figure will be one aligned time-series story: offered load rises and falls, running worker tasks follow it, queue depth stays bounded and drains, and p95 latency remains below the SLO. Scale-out alone is insufficient; returning to the minimum worker count after demand falls is required to demonstrate elasticity rather than permanent overprovisioning.

The causal experiment compares the same modernized AWS deployment with **worker autoscaling off** and **worker autoscaling on**. SQS, ECS task definitions, RDS, fixed API capacity, workload, SLO, and minimum worker count must remain the same. Only the worker-capacity policy changes. The asynchronous architecture is a prerequisite that makes delivery independently scalable; access to additional on-demand compute is the cloud capability being tested.

The local legacy baseline remains important as the starting point and benchmark rehearsal, but it is not the denominator for either cloud claim. The `t3.small` economical baseline must be rerun against RDS because the earlier 25 events/s cloud portability result used host-local PostgreSQL. A local asynchronous implementation is optional and is not required for the elasticity experiment.

### Phase 9 operating model

AWS is **off by default**. Application code, container builds, experiment automation, and infrastructure definitions are developed and tested locally. AWS is used only for bounded validation or measurement sessions:

1. **Cloud session 1 — synchronous migration:** validate Stages 9.1 and 9.2, collect evidence, then destroy the stack.
2. **Cloud session 2 — hardware flexibility:** run the same short RDS-backed ladder on `t3.small` and `c7i-flex.large`, collect the simple comparison, then destroy the stack.
3. **Cloud session 3 — asynchronous integration:** validate Stages 9.4 and 9.5 with tiny workloads, collect evidence, then destroy the stack.
4. **Cloud session 4 — headline experiment:** provision once, run the Stage 9.6 fixed control and Stage 9.7 elastic treatment back-to-back, collect both result sets, then destroy the stack.

Infrastructure as code is the reproducible source of truth. The AWS console may be used to learn, inspect, and troubleshoot, but repeatable creation and teardown must come from the repository. Synthetic experiment resources are disposable: each cloud session must remove RDS, load balancers, ECS services and tasks, NAT gateways if used, and every other session-owned billable resource. Retain definitions and evidence, not idle infrastructure.

Stages 9.6 and 9.7 are one controlled experiment session. Between them, reset application data and measurements but do not recreate infrastructure, deploy different code, change task definitions, resize the API or database, or move the load generator. The worker autoscaling policy is the only treatment variable.

### Ownership and user handoffs

Most of Phase 9 remains Codex implementation work. The human owner is needed only where AWS requires account ownership, private authentication, a spending decision, or an explicit decision to start a billable cloud session.

| Stage | Owner | What you need to do |
| --- | --- | --- |
| 9.0 | **You + Codex** | You create or secure the AWS account, enable MFA, establish the working identity, receive budget alerts, privately complete authentication, and approve the region and spending ceiling. Codex supplies guidance, repository configuration, checks, and documentation. |
| 9.1 | **Codex** | No routine input after the Stage 9.0 choices; review only if a deployment choice changes scope or expected cost. |
| 9.2 | **Codex, with your cloud-session approval** | Before cloud session 1, explicitly authorize the session and its budget and complete MFA or browser sign-in if AWS requests it. Codex provisions, validates, collects evidence, destroys, and verifies teardown. |
| 9.3 | **Codex, with your cloud-session approval** | Approve cloud session 2 and its hardware-flexibility cost ceiling after reviewing both primary EC2 configurations and complete any private authentication. |
| 9.4 | **Codex** | No routine input; this is local application development and testing. |
| 9.5 | **Codex, with your cloud-session approval** | Provide the same authorization and private authentication handoff for cloud session 3. |
| 9.6–9.7 | **Codex, with your cloud-session approval** | Approve cloud session 4 and its experiment cost ceiling and complete any private authentication. Codex runs both treatments in the same session and tears everything down only after both result sets are secured. |
| 9.8 | **Codex** | No required input unless you want to review or revise the final headline and presentation. |

You never need to send Codex an AWS password, MFA code, root credential, secret access key, or payment information. When interactive authentication is necessary, you enter it directly into AWS or the AWS CLI's browser flow. Before each cloud session, Codex must present the intended resources, region, estimated duration, cost guardrail, and teardown command; your ordinary `continue` is not sufficient authorization to begin incurring AWS charges unless it explicitly refers to that prepared session.

### Stage 9.0 — Prepare safe, reproducible AWS access

- [x] **You:** Create or open the AWS account and create or select the non-root working identity for Phase 9.
- [x] **You:** Enable MFA for the AWS account's root user.
- [x] **You:** Ensure the account has valid billing details and configure a USD 25 monthly AWS budget with alert delivery.
- [x] **Together:** Use Asia Pacific (Jakarta), `ap-southeast-3`; Codex verified current regional support for the planned core services against AWS's regional service documentation.
- [x] **Together:** Agree on cloud session 1's cost ceiling after Codex presents its concrete resource list and estimate; cloud session 1 was explicitly approved at USD 2.00 against USD 0 reported monthly spend and a USD 25 budget.
- [x] **You, privately:** Complete AWS CLI authentication using the non-root `trackrelay-admin` profile; no credentials belong in the repository or chat.
- [x] **Codex:** Verify without printing account identifiers or secrets that `trackrelay-admin` authenticates as a non-root IAM user and selects `ap-southeast-3`.
- [x] **Codex (repository work):** Make AWS commands accept a project profile and region explicitly, using `trackrelay-admin` and `ap-southeast-3` by default without hard-coding credentials. `make aws-check` now proves the configured region and non-root identity without modifying resources or inheriting an unrelated ambient `AWS_PROFILE`.
- [x] **Codex:** Add and locally validate an empty Terraform foundation with pinned tool and AWS-provider requirements, explicit profile and region inputs, standard tags, ignored state and variable files, and no AWS resources.
- [x] **Codex:** Add a saved-plan lifecycle with approval- and budget-gated provision, unconditional destroy, local evidence capture, and generic teardown verification before defining billable resources.
- [x] **Codex:** Write the cloud-session checklist covering approval, provision, validation, evidence collection, failure handling, destroy, and post-destroy verification.
- [x] **Codex:** Define teardown proof as empty Terraform state plus native service-specific absence checks derived from the pre-destroy inventory; save the generic tagging index as supporting evidence because AWS documents that it can return previously tagged resource tombstones.

The USD 25 AWS Budget is a monthly monitoring and alerting guardrail, not an assumed hard spending stop. Each cloud session therefore still needs its own estimate, explicit approval, bounded duration, and verified teardown. Automatic budget actions may be considered separately, but they do not replace teardown automation.

### Stage 9.1 — Prepare the synchronous rehost locally

Do not start cloud session 1 until the local-preparation items in both Stages 9.1 and 9.2 are complete.

- [x] Package the unchanged synchronous API as a locally verified, non-root OCI image with locked production dependencies, liveness health check, and an explicit one-off migration command.
- [x] Define the minimal rehost host as locally tested Terraform: one cost-bounded ARM EC2 instance, a disposable public network without a NAT gateway, restricted `/32` API ingress, ECR, SSM access without SSH, and service-native teardown checks. Do not apply it yet.
- [x] Define the host runtime configuration for the API, host-local PostgreSQL, one-off migration, and downstream simulator as a locally validated Compose model. Publish only the API port, require healthy dependencies and successful migrations, pin PostgreSQL by digest, and bound container logs.
- [x] Add one failure-safe local rehost smoke command that builds the application image, creates isolated credentials and storage, starts the complete stack, verifies the migration head and non-root processes, ingests and delivers a real event, proves database persistence across restarts, and removes the stack and volume.
- [x] Add locally tested, guarded automation that publishes only a Linux AMD64 image, records its ECR digest without account identifiers, transfers the committed runtime through SSM without SSH or plaintext secrets, generates the synthetic database password on-host, and performs migrations, health checks, and a unique tiny ingestion smoke test. Do not execute it before cloud-session approval and Terraform apply.
- [x] Automate frozen workload execution and non-secret result collection for cloud session 1. The benchmark driver remains on the approved developer machine; SSM invokes a private in-image helper to prepare and reconcile database and simulator evidence without exposing those services. The saved bundle contains the Step 8.6 workload, k6 and runtime evidence, compact reconciliation, and explicit deployment/driver placement, but not the temporary API endpoint.

### Stage 9.2 — Provision RDS for the AWS deployment

- [x] Define the RDS instance, networking, configuration, and secrets integration as code locally: a private, single-AZ `db.t4g.micro` PostgreSQL 17 instance with fixed encrypted storage, two-AZ subnet-group coverage, security-group-only access, RDS-managed credentials, and native teardown checks. No resource has been provisioned.
- [x] Add a guarded RDS deployment mode that retrieves the managed credential and private endpoint on-host, URL-encodes the credential, requires TLS, switches migrations and the API from host-local PostgreSQL to RDS, verifies migration/readiness/ingestion/persistence, and records only non-secret configuration evidence.
- [x] Automate the four core correctness scenarios and compact reconciliation evidence against the RDS-backed API before cloud session 1.

Use cloud session 1 to validate both Stages 9.1 and 9.2:

- [x] **You:** Explicitly authorize cloud session 1 after reviewing its resource list, region, estimated duration, cost guardrail, and teardown command.
- [x] Provision and run the synchronous rehost in AWS.
- [x] Run the frozen workload and guardrails to verify that the benchmark is portable to the AWS environment; the synchronous rehost sustained 25 events/s and first failed the frozen SLO at 50 events/s.
- [x] Record instance type, process count, database placement, region, and benchmark-driver placement as migration evidence, not as the causal elasticity control.
- [x] Provision PostgreSQL on RDS during cloud session 1.
- [x] Point TrackRelay at RDS and verify connectivity, migrations, and persistence.
- [x] Rerun the core correctness scenarios against RDS; normal, duplicate, out-of-order, and downstream-outage all passed reconciliation.
- [x] Record and freeze the RDS configuration intended for every later fixed-capacity and elastic performance experiment.
- [x] Collect the session evidence, destroy the complete session-1 stack, and verify empty Terraform state plus zero native resource inventories.

RDS provides the stable managed data layer for the target architecture; it is setup for the elasticity experiment, not a separately benchmarked intervention.

### Stage 9.3 — Demonstrate rapid hardware flexibility before redesigning the application

- [x] Define the causal comparison, fixed controls, evidence requirements, bottleneck interpretations, and honest stopping rules in `docs/aws-vertical-scaling-experiment.md`.
- [x] Revise the frozen ladder for the AWS Free account plan: use one x86_64 image and host identity across `t3.small`, `c7i-flex.large`, and `m7i-flex.large`; record their current Jakarta availability, public On-Demand Linux prices, two-vCPU count, memory profiles, burstability, processors, and query timestamps in a versioned artifact.
- [x] Fix the co-located healthy downstream simulator at one CPU and 1 GiB in the shared Compose definition so it cannot inherit additional capacity from changing host tiers; require observed headroom for a valid rate point.
- [x] Restrict Terraform to the three frozen tiers, default to `t3.small` with standard CPU credits, omit credit configuration from both non-burstable Flex tiers, select the x86_64 Amazon Linux image and AMD64 container/bootstrap artifacts, and reject unrelated instance types locally.
- [x] Pass the selected EC2 tier explicitly through every guarded cloud-session command, give it command-line precedence over ambient Terraform variables, and bind it into session evidence.
- [x] Hold the application image and revision, RDS instance and configuration, API process and connection-pool settings, benchmark driver, workload, and guardrails constant through a validated control artifact and explicit runtime configuration. Only the declared EC2 instance type changes; do not misstate the first cross-family transition as a CPU-only causal result.
- [x] Add aligned evidence for API process CPU and memory, EC2 CPU and any burst credits, RDS CPU, connections, memory and I/O latency, database-pool pressure, and downstream latency. Use measurement intervals long enough to identify the first constrained resource rather than relying on a short latency curve alone.
  - [x] Capture process identity, Python threads, GIL state, available CPUs, cumulative per-core Linux CPU time, host memory, and configured database-pool capacity; derive actual average cores used, per-core utilization, memory headroom, and pool pressure.
  - [x] Define productive throughput as zero for every rate that fails execution, SLO, or reconciliation guardrails, even when the failed run consumed substantial CPU.
  - [x] Collect aligned downstream-process resource measurements every five seconds throughout each rate and aggregate persisted delivery outcomes and p95 latency into matching UTC intervals.
  - [x] Collect aligned EC2 and RDS CloudWatch measurements, including resolution-honest burst-credit evidence for `t3.small`.
- [x] Reframe and automate the primary result as the same 10-second `10, 25, 50, 100, 200` events/s ladder on `t3.small` and `c7i-flex.large`, one strict trial per point, stopping each machine at its first failure, followed by one compact comparison and unconditional teardown. Keep the former 180-second CloudWatch/bottleneck protocol as an optional later diagnostic study.
  - [x] Bind the applied image, verified RDS deployment, hardware catalog, frozen controls, and starting tier into one self-contained experiment definition before traffic begins; record exact driver-side load windows separately from setup and metric polling.
  - [x] Run and preserve one RDS-backed candidate rate ladder for the current hardware tier, including exact load windows and complete process, downstream, reconciliation, and frozen starting-condition evidence at every executed rate. Keep the longer CloudWatch study optional rather than making it a gate for the short protocol.
  - [x] Stop each hardware tier after fully preserving its first failed rate; do not spend time or cloud budget on higher rates outside the measured performance envelope. Start every new hardware tier again at the lowest frozen candidate, and run all five only if they all pass.
  - [x] Add a bounded post-overload SSM recovery gate before reconciliation. Prove the agent can execute a harmless command, retry collection only when `Undeliverable` or `DeliveryTimedOut` with response code -1 proves it never ran, never replay an executed failure, and preserve every attempted command ID and delivery result as per-rate evidence.
  - [x] Correct the EC2 transition-plan guard after a real `t4g.small` to `c8g.large` plan exposed `public_ip` and `public_dns` as computed stop/start consequences. Permit them only when Terraform marks them `after_unknown`; continue rejecting concrete public-address assignments and every private-network or identity change.
  - [x] Make driver-side runtime sampling survive overload just like the detached observer. Keep the pre-load sample strict, bound later reads to one second, preserve sanitized during-load and post-load gaps, and never discard a completed k6 result merely because the overloaded API metrics endpoint is unavailable.
  - [x] Implement and locally test evidence-preserving synthetic-state reset plus the guarded, exact-next-tier EC2 transition workflow. Require a saved plan that changes only the EC2 host in place, then revalidate the instance identity, RDS identity, image digest, TLS configuration, and API readiness.
  - [x] Implement and locally test the fail-closed hardware comparison and boundary reporter. Re-derive each envelope, align raw evidence by explicit tier/rate/run identity, expose its classification thresholds, invalidate constrained-downstream attribution, and mark ambiguous or censored results honestly.
  - [x] Implement and locally test an explicitly armed session runner that enforces the frozen tier order, journals the current or pending cleanup tier, and attempts destroy plus native verification after success, failure, `SIGINT`, or `SIGTERM`. Document manual journal-based recovery for uncatchable process or host loss.
  - [x] Exercise the failure-safe path in the first cloud-session-2 attempt. The first `t4g.small` rate exposed a flawed sampler lifecycle: SSM does not reliably expose partial standard output while a command is running, and the earlier short run's sampler had actually completed before load began. The runner still destroyed the complete stack and verification found empty Terraform state plus zero resources in every native inventory. Replace polling with a completed start handshake, a detached run-specific sampler, a separate bounded collection command, idempotent cleanup, and fail-closed proof that both process timelines contain the exact load window.
  - [x] Add a separate, non-publishable AWS canary that ran one RDS-backed `t4g.small` point at 10 events/s for 30 seconds, proved sampler overlap and removal, required complete reconciliation, omitted CloudWatch and hardware transitions, and always destroyed and natively verified its stack. The current revision uses `t3.small` if this diagnostic is ever repeated.
  - [x] Execute the separately approved sampler canary and review its compact evidence before proposing another full Stage 9.3 run. Session `cloud-session-2-20260829T120045Z` accepted, processed, and delivered all 300 events with zero errors or dropped iterations; both process timelines contained the complete load window, the run-specific sampler was absent afterward, and teardown verification found empty Terraform state plus zero resources in all native inventories.
  - [x] Make the detached observer survive expected overload. A full-session setup attempt reached the 500 events/s portability point but its API metrics read timed out; schema 3 now records sanitized per-target gaps, preserves the independently available downstream snapshot, continues sampling after failures, and requires a complete paired read before signalling readiness. The aborted session was destroyed and Terraform, tagged, and native absence checks all passed.
  - [x] Extend the sampler's bounded post-load tail from 15 to 30 seconds after the next 500 events/s gate proved that gap recording worked but k6 VU initialization and graceful draining outlasted the original timing allowance by roughly two seconds. Keep the exact driver window unchanged and retain fail-closed coverage validation.
  - [x] Measure sampler coverage against the exact k6 process callbacks in the rehost checkpoint. The 500 events/s overload proved that a post-k6 runtime-metrics request can remain queued for roughly 30 seconds; its eventual timestamp describes observation delay, not load duration, and must not cause a false coverage failure.
  - [x] Replace the predicted post-load sampling tail with an explicit stop handshake. Signal the live run-specific container from the exact k6 exit callback, require one final observation attempt before it exits, and retain a named duration-plus-150-second absolute timeout only as an orphan-safety bound.
  - [x] Preserve failed Terraform transition stdout and stderr before raising, so AWS account-plan, capacity, or instance-compatibility errors remain diagnosable in `terraform-apply.log`.
  - [x] Make tier classification robust against one-off jitter exposed by the first three-tier run. Warm every tier identically, preallocate load-driver concurrency headroom, reset and wait between trials, classify each candidate by three independent trials with a two-of-three majority, validate every trial and reset in the report, and stop only after a majority-confirmed failure.
  - [x] Correct protocol v3's initial load-driver over-allocation after the 500 events/s portability checkpoint attempted to initialize 1,000 VUs and exited before traffic or summary generation. Keep the 100-VU low-rate floor, preallocate half the offered rate above it, cap growth at one VU per event/s, and report the k6 exit code when no summary exists. A local 500 events/s initialization test produced all 5,000 scheduled requests with zero driver drops; the failed AWS session was completely destroyed and passed every native absence check.
  - [x] Remove the transition-validation startup race exposed after the successful `c7i-flex.large` treatment. SSM became executable four seconds after the `m7i-flex.large` resize, before Docker had restored the API, so a one-shot inspection failed and triggered correct teardown. Follow SSM readiness with a bounded on-host Docker/API readiness gate, fail immediately on immutable-image or RDS-TLS drift, and retain sanitized timeout diagnostics.
  - [x] Remove protocol v3's 100-VU low-rate floor after the next run showed that it manufactured TCP connection fan-out: successful API p95 remained 53–163 ms and host/RDS utilization retained headroom, while connection setup intermittently reached 2–20 seconds and made every 10 events/s trial fail. This intermediate revision used SLO-derived half-rate preallocation before the final protocol-v4 simplification below.
  - [x] Make protocol v4 mechanically match its one-short-trial design: enforce one required trial and trial number in the evidence models, preinitialize exactly one bounded k6 VU per offered event/s, and update the frozen control identity.
  - [x] Collapse Stage 9.3 preparation into the armed failure-safe runner. Start immediately after approved Terraform apply; publish the image, deploy directly to private RDS on the fresh host, run correctness, reset and prove empty synthetic state, require a recent Standard-mode T3 balance of at least one CPU credit, and only then begin the comparison. Remove the host-local rehost workload from the Stage 9.3 operator path.
  - [x] Execute the revised single EC2 transition in a newly approved cloud session while preserving the image, RDS, controls, and prior evidence.
  - [x] Generate the short comparison from the completed cloud evidence, then guarantee teardown and native empty-inventory verification.

Completed cloud-session evidence:

- [x] **You:** Authorized `cloud-session-2-20260831T050531Z` with the reviewed `t3.small,c7i-flex.large` tier order, Jakarta region, USD 4.00 ceiling, and unconditional teardown.
- [x] Provision the synchronous API against fixed RDS on `t3.small`, pass all four RDS correctness scenarios, clear their state, and record a Standard-mode CPU-credit balance of `1.2908152` before the ladder.
- [x] Run the short healthy-downstream ladder on `t3.small`, preserve and reset experiment state, switch only the EC2 instance type to `c7i-flex.large`, verify unchanged controls, and replay the identical envelope.
- [x] Establish the headline boundary: `t3.small` passed 10 events/s and failed 25 events/s, while `c7i-flex.large` passed 25 events/s and failed 50 events/s—a 2.5x increase in highest passing short-run rate.
- [x] Preserve the useful same-rate comparison at 25 events/s: p95 request latency fell from 1,118 ms to 134 ms (about 8.3x lower), and dropped iterations fell from 8 to 0.
- [x] Generate the saved-evidence comparison report, destroy the stack, and verify empty Terraform state plus zero resources in every authoritative native AWS inventory.

The completed result is recorded under `results/aws-sessions/cloud-session-2-20260831T050531Z`. Every executed trial produced complete runtime observations, and every post-trial reset left zero experiment database rows and downstream receipts. The per-rate process and host figures remain supporting diagnostics only: each point is a single 10-second trial, so the headline is the pass boundary and same-rate user-visible behavior, not a long-duration capacity or bottleneck claim.

This result demonstrates rapid cloud hardware flexibility, not automatic elasticity. Both tiers have two vCPUs, so any gain must not be described as the effect of adding CPU count. The later fixed-worker-versus-autoscaled-worker experiment remains the elasticity proof. A separate evidence-backed database-resizing experiment would require its own single-variable plan and approval if RDS proves to be the constraint.

### Stage 9.4 — Decouple downstream delivery with SQS and a worker

- [x] Define a small, versioned downstream-delivery job containing only its persisted event ID, a narrow queue publishing interface, and a deterministic recording fake.
- [x] Make ingestion persist and enqueue work through that interface. Return the
  persisted event as queued, skip duplicate queue publications, expose publish
  failure after persistence, and label the temporary local recording queue as
  non-durable until the durable-acceptance step closes the database-to-queue gap.
- [x] Develop worker delivery and acknowledgement behavior locally with
  deterministic fakes; load the authoritative persisted event by ID, deliver
  and record the attempt, acknowledge only after success, and leave loading,
  delivery, recording, and acknowledgement failures visible for retry. Do not
  require a local AWS emulator or substitute message broker.
- [x] Move downstream delivery into a separately runnable worker and implement
  real boto3-backed SQS publish, long-poll receive, and delete-on-acknowledgement
  adapters behind the existing application boundaries. Keep the recording
  transport as the local default and verify every AWS operation through fakes.
- [ ] Add retries and a dead-letter queue.
- [ ] Define durable acceptance precisely and confirm that a fast API response cannot hide lost work.
- [ ] Add processing guardrails: every accepted event is accounted for, duplicate business effects remain zero, final shipment states are correct, and the queue drains by a documented deadline after offered load falls.

### Stage 9.5 — Containerize on ECS/Fargate

- [ ] Build and test separate API, worker, and simulator images locally.
- [ ] Define ECS/Fargate, ECR, SQS, dead-letter queue, RDS, networking, load balancing, secrets, and observability as code.
- [ ] Define CloudWatch metrics for offered load, API p95 latency, request errors, running worker tasks, queue depth, and message age or processing lag.
- [ ] Configure the API at a fixed, documented capacity with enough headroom that worker delivery capacity is the variable under test.

Use cloud session 3 as a small integration checkpoint:

- [ ] **You:** Explicitly authorize cloud session 3 after reviewing its resource list, region, estimated duration, cost guardrail, and teardown command.
- [ ] Provision the complete asynchronous stack and deploy the locally tested artifacts.
- [ ] Exercise 1-, 10-, and 100-event workloads before attempting a performance experiment.
- [ ] Verify the full path through the load balancer, API, RDS, SQS, worker, simulator, dead-letter queue, and CloudWatch.
- [ ] Reconcile every accepted event and confirm that the new processing and drain guardrails work.
- [ ] Collect integration evidence, destroy the complete session-3 stack, and verify the teardown.

### Stage 9.6 — Establish the fixed-capacity modernized control

- [ ] Make provision, fixed experiment, application-state reset, elastic experiment, result collection, and teardown reproducible through `make aws-up`, `make experiment-fixed`, `make experiment-reset`, `make experiment-elastic`, `make collect-results`, and `make aws-down` (or clearly documented equivalents).
- [ ] Test the workload driver, reset procedure, metrics collection, reconciliation, and plot generation locally before starting cloud session 4.
- [ ] **You:** Explicitly authorize cloud session 4 after reviewing its resource list, region, expected experiment duration, cost ceiling, and teardown command.
- [ ] Provision the final experiment environment once and record its immutable application and infrastructure versions.
- [ ] Disable worker autoscaling and fix the worker tier at its documented minimum task count.
- [ ] Run a stepped workload that rises beyond fixed worker capacity and later returns to the starting rate.
- [ ] Define a sustainable end-to-end load using ingestion SLOs plus bounded backlog, completion, drain-deadline, and correctness guardrails; API latency alone is insufficient.
- [ ] Freeze the fixed-control configuration and aligned time series in `results/aws-fixed-control/`.
- [ ] Leave the deployment unchanged and continue directly into Stage 9.7; do not tear it down or redeploy it between treatments.

### Stage 9.7 — Enable and measure worker elasticity

- [ ] Reset application data, queues, simulator state, and measurements without recreating or resizing the infrastructure.
- [ ] Enable a documented worker scaling policy with the same minimum task count and a bounded maximum; use queue backlog or backlog per task as the demand signal.
- [ ] Replay the fixed-control workload without changing the application, task definition, API capacity, database, simulator, benchmark driver, SLO, or guardrails.
- [ ] Verify that workers scale from A to B as load rises, backlog remains bounded and drains, and workers return to A after demand falls.
- [ ] Freeze the scaling policy, environment, raw aligned time series, reconciliation evidence, and summary in `results/aws-elastic-treatment/`.
- [ ] Collect both treatments' evidence, destroy the complete session-4 stack, and verify the teardown.

### Stage 9.8 — Publish the elasticity headline

- [ ] Analyze the frozen results and build the report locally with AWS off.
- [ ] Produce one large, aligned time-series figure comparing fixed and elastic runs across offered load, running worker tasks, queue depth or message age, and p95 latency with its 500 ms SLO line.
- [ ] Report the highest demand step that satisfies every end-to-end guardrail in each run, the load multiplier, worker expansion A→B, time to scale out, backlog drain time, and return to A.
- [ ] Put the figure and one-sentence elasticity result near the top of the repository README.
- [ ] Explain the causal chain plainly: SQS exposes pending demand, autoscaling responds, ECS changes the worker count, and AWS supplies and releases compute without TrackRelay owning spare hardware.
- [ ] Present the Stage 9.3 hardware-flexibility result as the clear first answer to "why not choose more suitable cloud hardware?", then keep migration details and extensive guardrail evidence from competing with the primary elasticity result.

## Deferred follow-up results

Only after the headline elasticity result is published:

- [ ] Measure automatic delivery recovery and backlog drain after a downstream outage.
- [ ] Compare deeper cost efficiency, database scaling, resource utilization, and operational complexity after the controlled vertical and elastic results are established.
- [ ] Publish secondary resilience and efficiency figures without diluting the primary result.

## Explicitly out of scope at the beginning

To protect the learning sequence, do not initially add a React UI, mobile application, real courier APIs, Kafka, Kubernetes, multi-region deployment, machine learning, real notifications, or an elaborate operations dashboard.

The important early work is event semantics, correctness, failure behavior, and measurable evidence.
