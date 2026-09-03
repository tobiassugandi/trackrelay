# Cloud session 4: fixed control and elastic treatment

This is the operator procedure for Stages 9.6–9.8. It provisions the asynchronous
stack once, measures one fixed worker, resets application state, enables only
worker autoscaling, replays the identical workload, destroys the stack, verifies
absence, and builds the comparison from saved evidence with AWS off.

This runbook is not cloud authorization or a measured result. Session 4 still
requires explicit approval of the session ID, region, complete staged resource
list, expected duration, cost ceiling, and unconditional teardown.

**2026-09-03 lifecycle correction:** Container Insights performance logs now
have a separate Terraform-owned group, created before the cluster and deleted
after it. Native verification checks this namespace independently of application
logs, and comparison reports require that new inventory entry. See the
[resource/cost review](aws-elasticity-cost-review.md) for the discovery and cleanup
audit. Three fixed-control attempts have since run and been torn down. The third
qualified, then stopped during reset because of a simulator mode-contract
mismatch. Worker autoscaling and the elastic treatment have not started.
See the [incident record](aws-elasticity-session-4-incident.md).

## Workflow and ownership

```text
local checks → aws-plan → review and explicit approval
                                   ↓
                      aws-elasticity-session
                      ├─ foundation apply
                      ├─ image publication, migration, fixed services
                      ├─ fixed control and qualification
                      ├─ application/queue/simulator reset
                      ├─ worker-only scaling transition
                      ├─ elastic replay and qualification
                      ├─ destroy and native absence verification
                      └─ offline comparison and plots
```

`make aws-elasticity-session` is the spending boundary. It takes ownership
immediately before foundation apply and attempts teardown even if that apply
fails partway. It accepts only a fresh `planned` session with the original
plan hash and clean Git revision. It does not resume partial runs.

Use the combined command **instead of** separately running `aws-up`, deployment,
or treatment commands. Never run a second controller, Terraform operation,
workload, or teardown in parallel. Keep the same machine, network/public IP,
working directory, Git revision, driver, Terraform configuration, and credentials
throughout both treatments. Do not let the machine sleep.

The shared cleanup owner attempts destroy once and native verification once,
including after a destroy error; nested controllers do not repeat those attempts.
A failed attempt requires operator recovery, not an automatic rerun of the
experiment. SIGINT/SIGTERM enter the cleanup path. SIGKILL, machine loss, repeated
interrupts during cleanup, or lost AWS access can prevent cleanup from finishing.
The manual recovery commands below remain essential.

### Terminal progress and evidence

`trackrelay-aws-elasticity-session` (including `make aws-elasticity-session`)
prints low-volume, timestamped progress to stderr by default. Lines flush
immediately, including when redirected. Timestamps include the local UTC offset;
CloudWatch bucket alignment still uses UTC. Stdout remains the final report
conclusion, and errors retain a nonzero exit status.

The progress stream shows:

- Six phase starts/completions and elapsed times, plus deployment substeps.
- The current named operation and elapsed time after roughly a minute of silence
  during blocking Terraform/ECS/image operations. This is elapsed time, not an
  estimated completion time or proof that the AWS operation is healthy.
- Roughly one line per minute during load/drain, using existing observations:
  step, rate, running/desired workers, queue work, and stable-empty duration.
  Failed observations say unavailable, not zero or a stale last-known value.
- CloudWatch attempt counts and publication retries; reset/purge waits.
- Failure phase/reason before cleanup, diagnostic capture, Terraform destroy,
  late-log absence samples, native verification, and the evidence directory.
- Reset HTTP failures name the fixed GET/POST operation and status code (or
  transport exception type). Request URLs, credentials, headers, bodies and
  arbitrary exception text are not printed or saved in the phase journal.
  Failed reset requests are never automatically retried: a timed-out POST may
  have partially changed state and must enter the existing cleanup path.

Example lines (illustrative, not cloud evidence):

```text
[18:55:38+0700] Starting: Phase 3/6: fixed-control treatment
[19:00:00+0700] Fixed workload: 240/660s; step=fall-10 rate=10/s workers=1/1 queue=1842
[19:07:03+0700] Fixed workload ended: k6 exit=0; waiting for drain (limit 1200s, stable-empty target 180s)
[19:11:28+0700] Starting: Cleanup: capturing ECS diagnostics
[19:18:17+0700] Container Insights cleanup: check 6/13; absent sample 5/5
[19:18:31+0700] Native absence confirmed: 30/30 categories zero
```

Add `--quiet` to the direct `uv run --locked trackrelay-aws-elasticity-session`
command to suppress progress. It does not suppress final errors/conclusions,
change artifact capture, bypass approvals, or change teardown/qualification.
Redirect stderr if you want a separate operator log; raw AWS, Terraform and k6
output remains captured as before and is never forwarded by the progress helper.

Progress is operator convenience only. `session.json` and retained artifacts
remain authoritative; terminal lines cannot substitute for missing metrics or
receipts. Output failures such as a broken pipe disable the progress sink rather
than failing the experiment or preventing cleanup. No extra cloud queries are
made for progress.

Library callers remain silent by default. Tests and supervised Python callers
can inject a fast `Callable[[str], None]` across nested controllers using
`trackrelay.operator_status.progress_output(reporter)`. Use
`repeat_interval_seconds=0` in deterministic tests to disable the background
elapsed-time ticker. The CLI cancels the ticker on success, failure, or interrupt.

## 1. Local preflight, before provisioning

Run from the repository root with Python 3.12, uv, Node.js 22+, Terraform, Docker, AWS CLI,
curl, and jq installed. Start Docker, then run:

```shell
make sync
make lint
make test
make infra-init
make infra-check
make images-smoke
make elasticity-driver-check
make elasticity-session-check
git status --short
git rev-parse HEAD
```

These checks do not provision AWS. Dependency initialization and container builds
may download packages or images. `infra-check` uses mocked-provider Terraform
tests. The driver check executes the actual JavaScript module with mocked k6
APIs and generated inputs, then validates it with the pinned k6 image. It sends
no HTTP requests. Node's VM-module experimental warning is expected; there are
no npm dependencies. The tests force boundary iterations for every step,
including the failed session's baseline/fall-5/recovery pattern, and require
exact, non-overlapping submissions and strict failure thresholds.
The session rehearsal exercises real lifecycle
controllers and saved evidence handoffs using simulated external work, including
phase failures, interrupts, qualification rejection, cleanup failure, and report
failure. Its synthetic outcomes are not cloud measurements.

After changing the driver, also exercise real k6 timing with the complete
11-minute, 3,660-event waveform against a local-only HTTP receiver:

```shell
make elasticity-driver-http-check \
  LOCAL_ELASTICITY_DRIVER_OUTPUT="results/local-elasticity-driver/$(date -u +%Y%m%dT%H%M%SZ)"
```

This uses a loopback receiver and a disposable, uniquely named k6 container
(Docker Desktop on macOS; host networking on Linux). The receiver returns 201
for the first submission and 200 for duplicates. The check requires exactly
3,660 requests and unique events, all per-step counts/checks, and k6 exit zero.
It retains inputs, raw k6 output/summary, and `local-driver-result.json` in a
fresh directory, never contacts AWS, and does not measure application capacity.

Each step now caps HTTP work at its own manifest slice. A single extra closing
iteration may be dispatched by the time-based executor; it sends no request and
is recorded as `driver_boundary_iterations{step:...}` (maximum one per step).
Larger overruns also increment `driver_errors` and fail. The full 60-second
step boundaries and five-minute recovery remain intact; no millisecond-shortened
duration is relied upon for safety. Missing/extra HTTP requests, duplicate
responses, dropped iterations, and driver errors still reject the treatment.

All checks must pass and Git status must be empty before planning. Commit any
intended changes first; never change code between the plan and either treatment.
The rehearsal complements individual reset, metrics, reconciliation, and real
PNG/SVG plotting tests in `make test`; it is not a live AWS integration test.

`make test` also connects the real API and simulator ASGI apps through the reset
controller with an isolated SQLite database and simulated AWS queue/worker
observations. It checks the actual uppercase `SimulatorMode` wire values,
clearing both stores, preserving the partner, rejecting degraded modes and
another run ID without writes, and serializing the stable-empty proof. The
controller's queue purge and autoscaling checks remain required.

A PostgreSQL variant of that contract test exercises the real `TRUNCATE` path.
Use only a fresh, disposable local PostgreSQL 17 database named
`trackrelay_reset_contract`, supplied through `TRACKRELAY_RESET_TEST_DATABASE_URL`
as `postgresql+psycopg://...@127.0.0.1:PORT/trackrelay_reset_contract`, then run:

```shell
uv run --locked pytest -m integration tests/test_experiment_reset_contract.py
```

The test rejects remote URLs, other database names, connection-query overrides,
and databases with existing tables. It is excluded from the default suite and
skips if the dedicated URL is missing. Do not point it at an existing development
database or AWS. Dispose of the dedicated local database after the check.

## 2. Identity, authentication, and a fresh session

The default location is Jakarta (`ap-southeast-3`) with profile
`trackrelay-admin`. Authenticate privately using your existing account workflow;
never paste credentials, tokens, or secret values into chat.

```shell
export TRACKRELAY_AWS_PROFILE=trackrelay-admin
export TRACKRELAY_AWS_REGION=ap-southeast-3
make aws-check

export TRACKRELAY_RUN_SESSION_ID="cloud-session-4-$(date -u +%Y%m%dT%H%M%SZ)"
trackrelay_public_ipv4="$(
  curl --fail --silent --show-error https://checkip.amazonaws.com
)"
trackrelay_public_ipv4="$(
  uv run --locked python -c \
    'import ipaddress, sys; print(ipaddress.IPv4Address(sys.argv[1]))' \
    "$trackrelay_public_ipv4"
)"
export TRACKRELAY_RUN_API_CIDR="${trackrelay_public_ipv4}/32"
export TRACKRELAY_RUN_SESSION_DIR="results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID"
test ! -e "$TRACKRELAY_RUN_SESSION_DIR"
```

Stop on any failed command. Check the preflight identity and region locally.
Ensure no other session owns this Terraform state. Keep the session identity and
evidence directory for recovery; do not generate a different ID when cleaning up.

## 3. Save the foundation plan and review the full session

```shell
make aws-plan \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"

terraform -chdir=infra/terraform show \
  "../../$TRACKRELAY_RUN_SESSION_DIR/terraform.tfplan"

jq '{session_id, region, deployment_mode, api_ingress_cidr,
     git_revision, plan_sha256, status}' \
  "$TRACKRELAY_RUN_SESSION_DIR/session.json"
```

The foundation plan does not yet contain the image-dependent runtime or scaling
intervention. Approval must cover the complete staged topology, not just the
first plan. Review `infra/terraform/async_*.tf`, `rds.tf`, and the
[experiment specification](aws-elasticity-experiment.md) at the saved revision.
Later controllers retain their own exact plans and immutable image digests.

| Component | Fixed control | Elastic treatment |
| --- | --- | --- |
| API | Two Fargate tasks, each 1 vCPU / 2 GiB | Unchanged |
| Worker | One task, 0.25 vCPU / 0.5 GiB | One to eight identical tasks |
| Simulator | One task, 0.25 vCPU / 0.5 GiB | Unchanged |
| Database | Private Single-AZ PostgreSQL 17, `db.t4g.micro`, encrypted 20 GiB gp3 | Unchanged |
| Queue | One SQS source queue and one DLQ | Unchanged |
| Worker scaling | Absent | One scalable target, two policies, two alarms |

Also include the VPC, subnets, routing and security groups, internet-facing ALB
and target group, ECS cluster, three ECR repositories, one-off migration task,
four task definitions, private simulator service discovery, database secret,
IAM resources, four application log groups plus one Container Insights
performance log group, Container Insights, and the seven-widget
CloudWatch dashboard. API ingress remains the reviewed `/32`. Public task IPs
provide outbound access; the design does not use a NAT gateway. The legacy
`REHOST_INSTANCE_TYPE=t3.small` input remains an identity field in async mode;
it does not provision a rehost EC2 instance.

Steady service compute is 2.5 vCPU / 5 GiB at minimum and 4.25 vCPU / 8.5 GiB
at eight workers. Thus the scaling intervention adds up to seven workers, or
1.75 vCPU / 3.5 GiB, plus their public IPv4 and observation costs. These are
configured service capacities, not billed-dollar estimates; deployment/migration
overlap can temporarily add tasks.

The workload is **events per second**, unlike session 3's finite batches:
`1 → 5 → 10 → 25 → 10 → 5 → 1`, with 60 seconds per first six steps and
300 seconds at the final recovery rate. Each treatment sends 3,660 events over
11 minutes; both send 7,320 over 22 minutes of scheduled load. Each also requires
at least 180 seconds of confirmed post-load drain, with an unchanged 20-minute
post-load deadline. Allow additional time for image publication, provisioning,
migration, UTC-minute alignment, metric publication, reset, transition, and
teardown. Agree a full wall-clock operating window with contingency; 22 minutes
is not the expected session duration or a cost bound.

Before approving, price the complete topology in the selected region, including
maximum worker time, ALB/LCU, public IPv4, RDS/storage, SQS, ECR, Secrets Manager,
Cloud Map, CloudWatch/Container Insights, logs, and data transfer. Check current
monthly-budget headroom. Do not reuse the earlier integration ceiling blindly.
The command validates a positive ceiling against the configured monthly budget;
it does **not** meter billing, deduct prior spend, or enforce a dollar/wall-clock
kill switch. Monitor the agreed operating window and abort into cleanup if it
is exceeded.

Record explicit approval only after this review:

```shell
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
export AWS_MONTHLY_BUDGET_USD='REVIEWED_MONTHLY_BUDGET'
```

The placeholders deliberately fail numeric validation. Replace them with the
approved numeric USD amounts, not an inferred or default authorization.

## 4. Run the approved session once

```shell
make aws-elasticity-session \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_MONTHLY_BUDGET_USD="$AWS_MONTHLY_BUDGET_USD"
```

Do not run `aws-up` first. A successful fixed control deliberately keeps the
deployment alive for the in-process reset and elastic replay. Fixed rejection
stops the session, retains evidence, and tears down without enabling autoscaling.
Do not adjust the workload mid-session to force a passing elastic result.

During execution, inspect only the local journal from another terminal:

```shell
jq '{status, elasticity_session, fixed_control, experiment_reset,
     worker_autoscaling_transition, elastic_treatment}' \
  "$TRACKRELAY_RUN_SESSION_DIR/session.json"
```

Command output is mostly captured in local evidence logs. The journal's phase
identifies the active handoff, not a continuously updated progress percentage.
An AWS command still running may not yet have a completed log. Check the agreed
time budget rather than interpreting quiet stdout as either success or failure.

## 5. Verify teardown and inspect the result

```shell
jq -e '.status == "teardown_verified"' \
  "$TRACKRELAY_RUN_SESSION_DIR/session.json"
jq -e 'length > 0 and all(.[]; . == 0)' \
  "$TRACKRELAY_RUN_SESSION_DIR/aws-native-inventory-after-destroy.json"
jq '{elasticity_demonstrated, observed_step_rate_multiplier, conclusion}' \
  "$TRACKRELAY_RUN_SESSION_DIR/elasticity/report/comparison-report.json"
```

Read `elasticity/report/comparison-report.md`, `comparison.png`, and
`comparison.svg`. The JSON retains the comparison method, qualification, and
source hashes. The report requires a complete native absence inventory, empty
Terraform state evidence, matching revisions, reset/transition evidence, and
correct chronological handoffs; all-zero entries alone are not a substitute for
the controller's full verification.

Important distinctions:

- Teardown success means resources were verified absent, not that elasticity
  passed.
- Fixed rejection produces no two-treatment comparison. Inspect
  `elasticity/fixed/summary.json` and its evidence before proposing a new,
  separately approved candidate.
- Elastic rejection may produce a valid negative comparison after teardown,
  but the session command exits unsuccessfully. Missing or inconsistent evidence
  prevents reporting instead of being filled with invented values.
- Passing elasticity and establishing a supported-rate multiplier are separate
  claims. A missing multiplier is not zero and must not be replaced with an
  API-latency ratio. These ordered short steps are not maximum steady-state
  production-capacity measurements.

Keep the complete session evidence privately. Plans, Terraform state, logs,
account identifiers, and endpoints are not automatically safe to publish. The
generated comparison is not automatically committed or published as a headline.

## 6. Failure recovery: cloud off first

If the controller fails, first inspect `session.json` and local phase/teardown
logs. Do not rerun the combined command. If cleanup did not finish, recover the
original identity from its evidence (replace the path placeholder):

```shell
export TRACKRELAY_RUN_SESSION_DIR='results/aws-sessions/ORIGINAL_SESSION_ID'
export TRACKRELAY_RUN_SESSION_ID="$(jq -er '.session_id' "$TRACKRELAY_RUN_SESSION_DIR/session.json")"
export TRACKRELAY_RUN_API_CIDR="$(jq -er '.api_ingress_cidr' "$TRACKRELAY_RUN_SESSION_DIR/session.json")"
export TRACKRELAY_AWS_PROFILE="$(jq -er '.profile' "$TRACKRELAY_RUN_SESSION_DIR/session.json")"
export TRACKRELAY_AWS_REGION="$(jq -er '.region' "$TRACKRELAY_RUN_SESSION_DIR/session.json")"
```

After confirming no controller is still running, use the original Terraform
state and restore private authentication if needed:

```shell
make aws-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Then run verification **even if destroy returned an error**. Run these as
separate commands; do not join them with `&&` or put them in a fail-fast script
that skips verification:

```shell
make aws-verify-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

If verification fails, inspect the saved native inventory and address the exact
remaining session resources. Do not delete the Terraform state or evidence to
make the check appear clean. Keep working on cleanup until absence is verified.
These commands remove the disposable session database, queues, images, and
other session resources; the local evidence remains.

Once teardown is verified, a report-only failure can be retried without AWS:

```shell
make aws-elasticity-report \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  ELASTICITY_REPORT_OUTPUT="$TRACKRELAY_RUN_SESSION_DIR/elasticity/report-retry-1"
```

Choose a new output directory; the reporter refuses to overwrite. Do not modify
source evidence after report generation: its hashes bind to the saved files,
including `session.json`. If cleanup was re-verified afterward, generate a new
report from the updated evidence. Incomplete treatment evidence requires a new
approved experiment, not fabricated repair data or live reporting queries.

## Individual phase commands

### Diagnosing a failed combined session

The combined runner records `elasticity_session.failed_phase`, safe nested
`workflow_error` details, and `cleanup_errors` even if teardown verification
fails. `diagnostics/ecs-*.json` captures task stop reasons before destruction;
`diagnostics/cloudwatch/<run-id>/<collection-time>/` retains each metric response
and collection status, including incomplete windows. These diagnostics do not
replace the qualified fixed/elastic summaries.

`aws-down` includes a bounded cleanup for late Container Insights recreation:
only after Terraform state is empty and native checks find no other resources
may it remove the exact session performance group. It requires five absent
samples 15 seconds apart, with at most 13 checks. `aws-verify-down` is still
read-only. Never interpret successful Terraform destruction alone as verified
AWS absence, and do not disable the inventory gate when cleanup fails.

A simulator replacement loses its in-memory receipt evidence and invalidates
the treatment. Do not fill missing CPU/memory buckets, reconstruct receipts,
or generate a success report from a partial run. See the
[first session-4 incident](aws-elasticity-session-4-incident.md).

Before a new approved attempt, exercise the simulator at its frozen resource
limit (use a fresh output filename each time):

```sh
make image-simulator
uv run --locked python scripts/check-simulator-health.py \
  --image trackrelay-simulator:local \
  --duration-seconds 180 --rate 10 \
  --output results/simulator-health-soak.json
```

This uses disposable local Docker containers, not AWS. It is a probe/receipt
check, not a cloud SLO result; record the image architecture when comparing
native local runs with emulated x86_64 images.

### Manual phase sequence

The combined command is recommended. The lower-level equivalents remain for an
explicitly supervised manual workflow; they must not run alongside it:

| Command | Required checkpoint | Successful handoff |
| --- | --- | --- |
| `make aws-up` | Reviewed saved plan and spending approval | `applied` |
| `make aws-async-deploy` | `applied` | `async_deployed` |
| `make aws-fixed-control` | `async_deployed` | `fixed_control_qualified` |
| `make aws-experiment-reset` | `fixed_control_qualified` | `experiment_reset_verified` |
| `make aws-elasticity-transition` | `experiment_reset_verified` | `worker_autoscaling_verified` |
| `make aws-elastic-treatment` | `worker_autoscaling_verified` | `teardown_verified` |
| `make aws-elasticity-report` | Complete evidence and verified teardown | Offline report |

All cloud phases use the same session, `/32`, profile, and region; all guarded
phase commands require the explicit session/cost/teardown approvals shown in
the [experiment specification](aws-elasticity-experiment.md). `aws-up` also needs
`AWS_DEPLOYMENT_MODE=async` and the monthly budget. The manual operator owns the
gaps between successful phases and any partial foundation failure. Do not leave
a successful intermediate phase unattended, and never recreate infrastructure
between treatments.
