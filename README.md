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

## Development approach

TrackRelay will be built in very small, observable steps. Each step should:

1. Introduce one idea or one narrow behavior.
2. Explain why that behavior exists.
3. Add or update a focused test.
4. Leave the repository in a runnable state.
5. Produce a result that can be inspected before moving on.

The first meaningful vertical slice will support one Courier Alpha event from HTTP request through database persistence, shipment update, and downstream delivery. Idempotency, ordering rules, failure modes, more couriers, reconciliation, performance testing, and AWS will be added afterward.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the step-by-step roadmap.

## Current status

The repository has been initialized and the project has been documented. No application code has been implemented yet.
