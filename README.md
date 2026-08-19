# TrackRelay

TrackRelay is a learning project about building and modernizing a reliable shipment-event integration gateway.

Fictional courier partners send shipment updates in different formats. TrackRelay authenticates, validates, and normalizes those updates, records them, updates the current shipment state, and delivers a consistent event to an internal order-management system.

The project begins as a deliberately synchronous application. Once that version is working and measured, it will be evolved in controlled stages into an asynchronous, observable, failure-tolerant system on AWS. The goal is not merely to produce a final architecture; it is to understand and demonstrate why each architectural change is useful.

## Core workflow

```text
Courier partner
    -> TrackRelay API
    -> authenticate and validate
    -> normalize partner payload
    -> detect duplicates and stale events
    -> persist event and update shipment state
    -> deliver normalized event downstream
```

The first shipment state machine will be intentionally small:

```text
CREATED -> PICKED_UP -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED
```

Shipment state is monotonic: an event may advance to any later status, allowing for missing intermediate courier updates, but cannot repeat or move backward. Older events are retained for audit with `state_applied = false` and reason `stale_event`, while the shipment remains unchanged. When occurrence timestamps are equal, the later status wins as a deterministic tie-breaker. `DELIVERED` is terminal; if an event is both older and terminal-conflicting, it is classified as stale first.

Inspect a shipment's complete audit history with `GET /api/v1/shipments/{tracking_number}/events`. Applied and rejected events are returned together in ascending `occurred_at` order, followed by `received_at`, database creation time, and event ID as deterministic tie-breakers. An unknown tracking number returns HTTP `404`.

## What the project demonstrates

- Adapting several external payload formats to one internal event model
- Idempotent processing of retried or duplicated events
- Safe handling of late and out-of-order events
- Controlled simulation of slow, failing, or unavailable dependencies
- Reconciliation that accounts for every generated event
- Repeatable load and failure experiments
- Incremental modernization from synchronous delivery to queue-based processing
- Evidence-based comparison of local, rehosted, and cloud-native architectures

## Planned technology stack

### Application

- Python
- FastAPI and Pydantic
- SQLAlchemy and Alembic
- PostgreSQL
- HTTPX

### Testing and experiments

- pytest
- Ruff and static type checking
- A Python scenario generator
- A controllable FastAPI downstream simulator
- k6 load tests
- Reconciliation reports

### Later AWS stages

- EC2
- RDS for PostgreSQL
- SQS and a dead-letter queue
- ECS/Fargate and an Application Load Balancer
- ECR, CloudWatch, and Secrets Manager
- Terraform
- GitHub Actions with OIDC

These are planned tools, not current prerequisites. The local synchronous version comes first.

## Local development

[uv](https://docs.astral.sh/uv/) manages TrackRelay's Python interpreter, project environment, dependencies, and lockfile. The repository pins the local interpreter to Python 3.12 in `.python-version`. `uv` will use or install a matching interpreter when needed.

Synchronize the project-local `.venv` from the committed lockfile:

```bash
uv sync --locked --python 3.12
```

TrackRelay has safe defaults and runs without local configuration. To override them, copy the example environment file:

```bash
cp .env.example .env
```

The `TRACKRELAY_APP_NAME`, `TRACKRELAY_ENVIRONMENT`, and `TRACKRELAY_DEBUG` variables configure the application name, runtime environment, and debug mode. `TRACKRELAY_DOWNSTREAM_URL` and `TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS` configure synchronous delivery. The local `.env` file is ignored by Git; `.env.example` documents non-secret example values.

### Local PostgreSQL

Docker Compose runs PostgreSQL independently of the API, so database startup can be learned and verified before application persistence is introduced. Start it with:

```bash
make db-up
```

Inspect its state:

```bash
make db-status
```

Check the application-configured SQLAlchemy connection:

```bash
make db-check
```

Apply every pending database migration:

```bash
make migrate
```

Inspect the database's current migration revision:

```bash
make migration-status
```

Alembic reads the same `TRACKRELAY_DATABASE_URL` setting as the application. The initial `0001_baseline` migration is deliberately empty because no domain tables exist yet; it establishes the starting point for later schema changes.

Run tests that exercise transaction behavior against PostgreSQL:

```bash
make test-integration
```

The default `make test` suite excludes tests marked `integration`, so it remains fast and does not require Docker. Run `make db-up` and `make migrate` before the integration suite.

Open an interactive PostgreSQL shell when you want to inspect it directly:

```bash
docker compose exec postgres psql -U trackrelay -d trackrelay
```

Stop the service without deleting its persistent data volume:

```bash
make db-down
```

Compose reads `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_PORT` from `.env`, with development defaults when the file is absent. TrackRelay defaults to host port `5433` to avoid conflicting with a separately installed PostgreSQL server; the container itself still listens on the standard port `5432`. SQLAlchemy reads `TRACKRELAY_DATABASE_URL`. Database infrastructure remains separate from the API routes; readiness will use it in Step 1.9.

Run the tests:

```bash
uv run --locked pytest
```

Run the linter:

```bash
uv run --locked ruff check .
```

Start the local API server:

```bash
uv run --locked uvicorn trackrelay.main:app --reload --host 127.0.0.1 --port 8000
```

Then visit `http://127.0.0.1:8000/docs` or check liveness with:

```bash
curl http://127.0.0.1:8000/health/live
```

Liveness reports whether the API process is responding. Readiness additionally checks whether PostgreSQL can execute a query:

```bash
curl --include http://127.0.0.1:8000/health/ready
```

Readiness returns HTTP `200` with `{"status":"ready"}` when PostgreSQL is available and HTTP `503` when it is unavailable.

A configured business partner submits an event to `POST /api/v1/partners/{partner_id}/events`; for example, an `alpha-indonesia` partner using the `courier-alpha` adapter posts to `/api/v1/partners/alpha-indonesia/events`. TrackRelay loads that partner, selects its configured payload adapter, normalizes and persists the event with its shipment update, then synchronously posts the normalized event to the downstream simulator. A successful response includes the event ID, processing and delivery statuses, and the downstream HTTP status code.

Synthetic experiment traffic can include an `X-Test-Run-ID` request header containing a UUID. TrackRelay keeps this laboratory metadata outside the courier-specific JSON, attaches it to the normalized event, stores it as an optional foreign key, and forwards it downstream. Ordinary courier traffic omits the header and retains a null `test_run_id`.

Each `test_runs` row records the scenario name, random seed, JSON configuration, expected event count, start time, and optional completion time. A scenario executor can populate this reproducibility boundary directly from the deterministic input manifest.

### Deterministic input manifests

Generate a small normal Courier Alpha dataset without sending any traffic:

```bash
make generate SEED=20260806 SHIPMENTS=3 OUTPUT=results/input-manifest.json
```

The same seed, partner, shipment count, and start time produce the same test-run UUID and byte-identical manifest. Pass `--test-run-id` directly to `uv run trackrelay-generate` when a fresh experiment needs a distinct namespace while retaining the same history shape.

The manifest contains every sendable partner payload together with its `test_run_id`, sequence number, partner event ID, tracking number, expected normalized status, and occurrence time. It also records the expected unique-event count and final state of every shipment. Generated partner event and tracking identifiers include the run UUID, so experiments given distinct run IDs use separate identity namespaces.

Generation is intentionally offline in this step: it writes the experiment inputs and expectations but does not alter PostgreSQL or call TrackRelay. Later scenario execution will persist the matching `test_runs` definition and send each payload with the `X-Test-Run-ID` header.

### Basic reconciliation

After every request in a manifest has been attempted, the matching `test_runs` definition exists, and the same downstream simulator process still holds its receipts, compare the manifest with TrackRelay's configured database and simulator:

```bash
make reconcile \
  MANIFEST=results/input-manifest.json \
  REPORT=results/reconciliation.json
```

The JSON report counts generated and accepted requests, rejected requests without a matching persisted identity, unique persisted events, and unique events in the `processed`, `failed`, or pending `received` states. It also reports simulator receipts, unique downstream events, duplicate business effects, and incorrect final shipment states. The command assumes every manifest request was attempted before reconciliation.

The reconciliation code names evidence after its source. A `manifest_event` is the declared input, a `database_event` is the persisted record, a `database_delivery_attempt` records an attempted downstream call, a `simulator_receipt` is the downstream system's evidence, and a `database_shipment` holds TrackRelay's final materialized state. It compares those sources in three stages:

1. Manifest events against database events.
2. Database delivery attempts against simulator receipts.
3. Manifest final shipment states against database shipments.

The `test_run_id` selects one experiment. Inside that experiment, `(partner_id, partner_event_id)` matches the same business event across the manifest, database, and simulator; `Event.id` joins a database event to its delivery attempts; and `tracking_number` groups event history into a shipment. These names and identities are intentionally explicit because “expected” and “actual” are ambiguous when every evidence source can contain either.

`unaccounted` identifies persisted run events that have no manifest identity, whose stored or received content disagrees with the manifest, or whose durable delivery attempts do not agree with simulator receipts. A failed delivery attempt with no receipt remains explicitly accounted for; a successful attempt without a receipt, or a receipt without a successful attempt, does not.

`invariants_passed` is true only when all four reconciliation invariants hold:

```text
unique = processed + failed + pending
unaccounted = 0
duplicate business effects = 0
incorrect final shipment states = 0
```

### Test-run database summaries

With the TrackRelay API running, save its persisted view of one experiment as versioned JSON:

```bash
make summary \
  TEST_RUN_ID=00000000-0000-0000-0000-000000000705 \
  SUMMARY=results/test-run-summary.json
```

This calls `GET /api/v1/test-runs/{test_run_id}/summary`. The response contains the run definition and timestamps, declared event count, database event counts by processing state, and database delivery-attempt counts by result. A known run with no events returns zero counts; an unknown run returns HTTP `404`.

The endpoint deliberately summarizes only evidence persisted by TrackRelay. Use the reconciliation command when the manifest, simulator receipts, duplicate effects, and final shipment correctness must also be compared.

### Repeatable correctness scenarios

Start PostgreSQL, apply migrations, and run the TrackRelay API and downstream simulator in separate terminals. Then execute any correctness scenario as a command:

```bash
make scenario-normal
make scenario-duplicate
make scenario-out-of-order
make scenario-downstream-outage
```

Each command creates a fresh test-run UUID, ensures the configured Courier Alpha partner exists, sends deterministic traffic, and stores its evidence under `results/correctness/<scenario>/<test_run_id>/`:

```text
input-manifest.json
request-observations.json
database-summary.json
reconciliation.json
```

The duplicate scenario sends each business identity twice and expects only the first request to create a database event or downstream effect. The out-of-order scenario sends every shipment history in reverse while expecting the final shipment state to remain delivered. The outage scenario switches the simulator to `UNAVAILABLE`, expects persisted events with HTTP-error delivery attempts, and restores the simulator to `HEALTHY` afterward.

Every command runs reconciliation before it succeeds. It exits unsuccessfully after saving the evidence if the observed HTTP statuses disagree with the scenario or any reconciliation invariant fails. Override `SEED`, `SHIPMENTS`, `API_URL`, or `CORRECTNESS_OUTPUT` through Make variables when needed.

### k6 smoke test

With the TrackRelay API running, send five liveness requests from one virtual user and save k6's machine-readable end-of-test summary:

```bash
make load-smoke
```

The command runs the pinned `grafana/k6:2.1.0` container, so a host installation of k6 is not required. It writes the summary to `results/k6/smoke-summary.json`; override `K6_OUTPUT` to choose another local path. The container reaches the API through `http://host.docker.internal:8000`, which can be changed with `K6_API_URL`.

This deliberately small test checks only that k6 can reach TrackRelay, all liveness checks pass, and result capture works. It does not yet define a latency or request-error SLO. The script uses k6's [`handleSummary()`](https://grafana.com/docs/k6/latest/results-output/end-of-test/custom-summary/) hook so the raw aggregate metrics remain available for later experiment steps.

### Initial local baseline SLO

The first performance baseline uses three deliberately simple pass conditions:

| Signal | Evidence | Pass condition |
| --- | --- | --- |
| Response latency | k6 `http_req_duration` | `p(95) < 500 ms` |
| Request errors | k6 `http_req_failed` | `rate < 0.01` (below 1%) |
| Accepted-event accounting | Reconciliation report | `unaccounted == 0` |

The comparisons are strict: exactly 500 ms or exactly 1% fails. These k6 metric expressions follow its standard [threshold syntax](https://grafana.com/docs/k6/latest/using-k6/thresholds/). The accounting target comes from the matching test run's reconciliation report because k6 cannot determine whether an accepted request produced consistent database and downstream evidence.

`INITIAL_BASELINE_SLO` in `trackrelay.experiments.slo` is the machine-readable definition. `evaluate_baseline_slo()` distinguishes `slo_passed`, meaning the three targets above passed, from `experiment_passed`, which also requires `reconciliation.invariants_passed`. Therefore duplicate business effects or incorrect final shipment states still make the complete experiment fail even when its latency, error rate, and unaccounted count meet the SLO.

This is an initial local experiment definition for finding the synchronous architecture's limits. It is not a promised production target; later evidence and business requirements may justify revising it. The gradual ramp below applies it while increasing ingestion traffic.

### Gradual ingestion ramp

With PostgreSQL migrated and both TrackRelay and the downstream simulator running, execute the standard arrival-rate ramp:

```bash
make load-ramp
```

The command first creates or validates an active `load-alpha` partner, then sends unique Courier Alpha events at 10, 25, 50, 100, 250, and 500 requests per second. Each tier lasts 10 seconds by default. The open-model k6 [arrival-rate executor](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/) starts requests independently of response completion, so the configured rate remains the input while TrackRelay's response time is the observation.

Each tier has its own p95-latency and request-error thresholds from the initial SLO. A tier failure aborts the remaining higher rates; dropped iterations and non-`201` responses also fail the run. The machine-readable k6 summary is saved to `results/k6/ramp-summary.json`.

Use shorter tiers for a wiring check, or override the rates when diagnosing a constrained machine:

```bash
make load-ramp RAMP_TIER_DURATION_SECONDS=1 RAMP_RATES=1,2
```

`RAMP_PARTNER_ID`, `RAMP_RUN_ID`, `K6_API_URL`, and `K6_RAMP_OUTPUT` are also configurable. When `RAMP_RUN_ID` is omitted, the command generates a UUID used only to keep synthetic event and tracking identities unique.

This step evaluates the k6-observable latency and error targets. It does not call the ramp a complete successful experiment yet: Step 8.5 will add a run manifest, reconciliation, resource measurements, and the `experiment_passed` evaluation needed to prove the accounting and correctness side of the SLO.

### Duplicate-event contract

A logical event is identified by `(partner_id, partner_event_id)`. Multiple HTTP requests carrying that identity are transport retries of the same logical event, not additional events. Likewise, a downstream HTTP delivery is a side effect of the logical event; retrying ingestion must not create another downstream delivery.

The first successful request returns HTTP `201` with `duplicate: false`. An already-seen event returns HTTP `200` with the original `event_id`, `duplicate: true`, `delivery_status: "skipped_duplicate"`, and `downstream_status_code: null`. Sequential and concurrent uniqueness conflicts resolve to the original event without repeating the shipment update, and duplicate requests skip downstream delivery.

Start the downstream order-system simulator in a separate terminal:

```bash
make run-downstream
```

It listens on `http://127.0.0.1:8001`. `POST /events` validates and records a normalized event in memory, while `GET /events` makes the received events inspectable. The store is intentionally process-local and resets whenever the simulator restarts.

The simulator starts in `HEALTHY` mode. Change behavior with `PUT /control/mode`; `GET /control/status` reports the active mode and fixed delay. `RETURN_500` returns HTTP `500`, `SLOW` waits 1 second and accepts, `TIMEOUT` waits 6 seconds and accepts, and `UNAVAILABLE` returns HTTP `503`. Failed or unavailable requests are not recorded. Use `{"mode":"HEALTHY"}` to restore normal acceptance. Control state is process-local and resets to `HEALTHY` when the simulator restarts. The 6-second timeout delay intentionally exceeds TrackRelay's default 5-second downstream timeout; the server can still finish and record after the client gives up, a synchronous ambiguity explored in later steps.

TrackRelay stores every actual downstream call in `delivery_attempts`, separate from the logical `events` row. Each attempt records its per-event attempt number, result (`delivered`, `http_error`, or `transport_error`), optional HTTP response code, latency in milliseconds, optional error text, and start/completion timestamps. Duplicate ingestion requests do not make downstream calls and therefore do not create delivery attempts.

### Downstream-failure transaction boundary

Event persistence and downstream delivery do not share a transaction. Once normalization succeeds, TrackRelay commits the logical event and any shipment-state change before attempting delivery. The delivery-attempt record then commits independently, so both records remain inspectable even when the HTTP request reports a downstream failure. TrackRelay does not automatically retry delivery inside the ingestion request.

A downstream HTTP error or non-timeout transport error returns HTTP `502` with `{"detail":"Downstream delivery failed; event remains persisted"}`. A downstream timeout returns HTTP `504` with `{"detail":"Downstream delivery timed out; event remains persisted"}`. These responses describe delivery failure, not persistence failure: the event, shipment state, and failed attempt remain stored.

The Phase 5 outage scenario sends three distinct events while the simulator is `UNAVAILABLE`. All three API calls return `502`, yet PostgreSQL retains three processed events, three shipment updates, and three HTTP-error attempts with downstream status `503`; the simulator receives no events. Retrying the same payloads returns the existing event IDs with HTTP `200` and `skipped_duplicate`, without creating more attempts. This prevents duplicate side effects but does not recover the missed deliveries—a deliberate measurement of the legacy synchronous design.

Inspect a shipment's latest accepted state with `GET /api/v1/shipments/{tracking_number}`. A found response includes its tracking number, current normalized status, the business timestamp of that status, and creation/update timestamps. An unknown tracking number returns HTTP `404` with `{"detail":"Shipment not found"}`. Append `/events` to retrieve the shipment's complete applied and rejected event history.

Inspect one logical event with `GET /api/v1/events/{event_id}`. The response combines its normalized payload, processing status, shipment-state application decision, and every delivery attempt ordered by attempt number. Attempt diagnostics include result, downstream response code, latency, error text, and start/completion timestamps. An unknown UUID returns HTTP `404` with `{"detail":"Event not found"}`.

Every courier adapter implements the shared `PartnerAdapter` protocol: it declares a stable `adapter_type`, exposes its Pydantic `payload_model`, and normalizes a validated payload plus the configured business-partner ID and receipt time into a `NormalizedEvent` without I/O. The registry is keyed by adapter type, and ingestion selects it through `Partner.adapter_type`; `Partner.id` remains the event's business identity. This allows records such as `beta-indonesia` and `beta-singapore` to share the `courier-beta` payload adapter while retaining separate idempotency namespaces. A reusable contract-test suite verifies this separation, shared identifiers and timestamps, raw-payload preservation, and all normalized shipment-status mappings.

Courier Beta uses its external field names `messageId`, `awb`, `statusCode`, and `timestamp`. Status codes `10`, `20`, `30`, `60`, and `72` map respectively to created, picked up, in transit, out for delivery, and delivered; `72` and the canonical sample come from the original project design, while the preceding monotonic codes are TrackRelay's documented local simulator contract. `timestamp` is a positive Unix timestamp in seconds. Both numeric fields are strict integers, and the adapter preserves the original external field names in `raw_payload`.

Courier Gamma nests its external contract under `notification`: `reference`, `trackingNumber`, `status`, and `occurredAt`. The timestamp must be an aware ISO 8601 value with a zero UTC offset, such as `2026-08-06T07:21:00Z`; naive or non-UTC offsets are rejected. Gamma uses the five normalized status names as its external codes and preserves the complete nested object in `raw_payload`.

The equivalent application shortcuts are `make sync`, `make test`, `make lint`, and `make run`. Activating `.venv` manually or setting `PYTHONPATH` is not required because TrackRelay is installed as a project package.

## Development approach

TrackRelay will be built in very small, observable steps. Each step should:

1. Introduce one idea or one narrow behavior.
2. Explain why that behavior exists.
3. Add or update a focused test.
4. Leave the repository in a runnable state.
5. Produce a result that can be inspected before moving on.

The first vertical slice supports Courier Alpha events from HTTP request through database persistence, shipment updates, and downstream delivery. It treats retries idempotently and retains out-of-order events without reversing shipment state. Performance testing and AWS will be added afterward.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the step-by-step roadmap.

## Current status

The `local-foundation-v1`, `legacy-happy-path-v1`, `legacy-idempotency-v1`, `legacy-ordering-v1`, `legacy-failure-behavior-v1`, `legacy-multipartner-v1`, and `legacy-reconciliation-v1` milestones are complete. TrackRelay exposes shipment, event, and test-run diagnostics; accepts Alpha, Beta, and Gamma formats; runs reconciled correctness scenarios; and has a machine-checkable local SLO with a gradual k6 ingestion ramp. The next step captures failure-under-load evidence and completes the local baseline.
