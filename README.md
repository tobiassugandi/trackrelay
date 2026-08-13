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

Courier Alpha can submit an event to `POST /api/v1/partners/courier-alpha/events`. TrackRelay validates the configured partner and Alpha payload, normalizes and persists the event with its shipment update, then synchronously posts the normalized event to the downstream simulator. A successful response includes the event ID, processing and delivery statuses, and the downstream HTTP status code.

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

The equivalent application shortcuts are `make sync`, `make test`, `make lint`, and `make run`. Activating `.venv` manually or setting `PYTHONPATH` is not required because TrackRelay is installed as a project package.

## Development approach

TrackRelay will be built in very small, observable steps. Each step should:

1. Introduce one idea or one narrow behavior.
2. Explain why that behavior exists.
3. Add or update a focused test.
4. Leave the repository in a runnable state.
5. Produce a result that can be inspected before moving on.

The first vertical slice supports Courier Alpha events from HTTP request through database persistence, shipment updates, and downstream delivery. It treats retries idempotently and retains out-of-order events without reversing shipment state. Failure modes, more couriers, reconciliation, performance testing, and AWS will be added afterward.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the step-by-step roadmap.

## Current status

The `local-foundation-v1`, `legacy-happy-path-v1`, `legacy-idempotency-v1`, and `legacy-ordering-v1` milestones are complete. PostgreSQL-backed scenarios prove retry idempotency and safe out-of-order handling. Phase 5 now provides deterministic downstream failure modes and durable delivery-attempt records; the next step defines what the ingestion API promises when those attempts fail.
