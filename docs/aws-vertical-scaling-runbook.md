# Stage 9.3 AWS vertical-scaling runbook

This is the operator-facing procedure for running TrackRelay's complete
RDS-backed synchronous infrastructure-scaling experiment. It is intentionally
separate from the implementation details in
`docs/aws-vertical-scaling-experiment.md`.

The experiment runs the same six-rate workload on:

```text
t4g.small -> c8g.large -> c8g.4xlarge
```

Only the EC2 instance type changes. The application revision and image, one API
process, connection pool, private RDS database, downstream simulator, load
driver, rates, duration, and pass/fail rules remain fixed.

## Important ownership rule

Before `aws-scaling-session` starts, you are responsible for running setup or
teardown commands. Once `aws-scaling-session` starts, it becomes the only
controller: do not run another scaling, transition, report, destroy, or verify
command in parallel.

The armed runner attempts destroy and native verification after success,
ordinary failure, `Ctrl-C`, or `SIGTERM`. It cannot recover from destruction of
the local machine, loss of Terraform state, or `SIGKILL`; keep the workspace and
session evidence on durable storage.

## 1. Choose the session inputs

Run from the repository root with a clean worktree. Use a new session ID and the
public IPv4 of the machine that will run k6:

```shell
export TRACKRELAY_RUN_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ
export TRACKRELAY_RUN_API_CIDR=YOUR_CURRENT_PUBLIC_IPV4/32
export TRACKRELAY_RUN_COST_CEILING_USD=REVIEWED_APPROVAL_CEILING
export TRACKRELAY_RUN_TIER_ORDER=t4g.small,c8g.large,c8g.4xlarge
```

These task-specific variables deliberately avoid ambient `AWS_PROFILE` and
Terraform variables. The Makefile supplies the project profile
`trackrelay-admin` and region `ap-southeast-3` explicitly.

Confirm the revision and toolchain:

```shell
git status --short
git rev-parse HEAD
make aws-check
make infra-init
make infra-check
make test
```

Expected checkpoints:

- Git status is empty.
- AWS preflight reports `trackrelay-admin`, Jakarta, and no resource changes.
- Terraform validation and all Terraform tests pass.
- The ordinary test suite passes.

## 2. Create—but do not apply—the plan

```shell
make aws-plan \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

This contacts read-only AWS data sources and writes a speculative saved plan; it
does not create resources. Inspect:

```shell
terraform -chdir=infra/terraform show \
  "../../results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/terraform.tfplan"

sed -n '1,240p' \
  "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/proposal.md"
```

Do not apply until the exact session ID, plan digest, resource list, frozen tier
order, cost ceiling, and unconditional teardown have explicit approval. Any Git
commit after planning invalidates the plan automatically; create a new session
and plan instead of trying to repair the old one.

## 3. Apply only the approved saved plan

Record the UTC start time and start the approved duration timer. Then run:

```shell
make aws-up \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Notes:

- The command applies the saved plan file, not a newly generated plan.
- RDS creation is normally the longest quiet provisioning step.
- Detailed provider output is saved privately under the session directory.
- If this or any following setup command fails before the armed runner starts,
  immediately use the manual teardown procedure below.

## 4. Publish and smoke-test the synchronous deployment

Publish the exact committed Linux ARM64 image:

```shell
make aws-rehost-publish \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Deploy the digest-pinned image through SSM and run the small on-host smoke test:

```shell
make aws-rehost-deploy \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Expected checkpoints:

- The session manifest contains a Linux ARM64 image digest.
- The deployment uses that digest, not a mutable tag.
- Migrations, API readiness, downstream readiness, ingestion, and persistence
  pass without SSH access.

## 5. Preserve the portability checkpoint and switch to RDS

Run the frozen short host-local portability workload required by the normal
Stage 9.3 state machine:

```shell
make aws-rehost-workload \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

This is setup evidence, not the RDS-backed Stage 9.3 baseline. It runs six short
rate points and saves each reconciliation result under `rehost-workload/`.

Switch the same deployment to the already provisioned private RDS instance:

```shell
make aws-rds-deploy \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Do not use `aws-rds-deploy-canary` here. That shortcut exists only for a
proposal explicitly limited to the non-publishable sampler canary.

Run the four RDS correctness scenarios:

```shell
make aws-rds-correctness \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Do not continue unless all four scenarios pass and the session status is
`rds_correctness_collected`.

## 6. Hand ownership to the failure-safe experiment runner

This is the long-running experiment command:

```shell
make aws-scaling-session \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_TIER_ORDER="$TRACKRELAY_RUN_TIER_ORDER" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t4g.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

The runner performs, in order:

1. Freeze the exact image, RDS, workload, hardware, and guardrail controls.
2. Run the six rates for 180 seconds each on `t4g.small`.
3. Preserve evidence, reset synthetic state, and validate the in-place move to
   `c8g.large`.
4. Replay the same six rates on `c8g.large`.
5. Preserve evidence, reset state, and validate the in-place move to
   `c8g.4xlarge`.
6. Replay the same six rates on `c8g.4xlarge`.
7. Generate the two-transition comparison and bottleneck report.
8. Destroy the complete Terraform stack.
9. Verify empty Terraform state, the generic tag inventory, and all native
   service inventories.

For every rate, it requires:

- Exact k6 process boundaries and the complete k6 summary.
- A detached private sampler ready before load begins.
- Timestamped API and downstream observation attempts containing the whole
  load window. Individual overload-time gaps are retained explicitly instead
  of crashing the observer; diagnostic calculations use successful snapshots
  and remain fail-closed when those snapshots are insufficient.
- Proof that the run-specific sampler container was removed.
- Complete database and downstream reconciliation.
- EC2 and RDS CloudWatch evidence aligned to the load window.
- A valid pass/fail interpretation; failed overload is never counted as useful
  throughput merely because it consumed CPU.

Long quiet periods are expected while CloudWatch metrics publish, EC2 changes
state, SSM comes back online, or RDS is being destroyed. Do not start a second
controller during those waits.

## 7. Verify the terminal result

After the command returns, inspect compact, non-secret evidence:

```shell
uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

root = Path("results/aws-sessions") / os.environ["TRACKRELAY_RUN_SESSION_ID"]
manifest = json.loads((root / "session.json").read_text())
print("session status:", manifest["status"])
print(
    "report:",
    manifest.get("vertical_scaling", {}).get("report"),
)
native = json.loads(
    (root / "aws-native-inventory-after-destroy.json").read_text()
)
print("nonzero native inventory:", {k: v for k, v in native.items() if v})
PY
```

Required terminal state:

- `session status: teardown_verified`
- A saved Stage 9.3 comparison report
- Empty Terraform state
- No nonzero native inventory count

Raw experiment evidence lives under:

```text
results/aws-sessions/<session ID>/vertical-scaling/
```

This directory is intentionally ignored by Git because it can contain private
operational evidence. The final reviewed, portable result should be copied into
the repository only through a separate, deliberate reporting step.

## Manual teardown and recovery

Use this immediately if setup fails before `aws-scaling-session` starts, or if
the runner is lost because the local process or machine disappears:

```shell
make aws-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"

make aws-verify-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Read `rehost_instance_type` and any pending transition target from the session's
`session.json`; do not guess the cleanup tier. If destroy fails, still run
verification, preserve both errors, diagnose the exact remaining resource, and
retry complete destroy. Do not use targeted destroy as the normal recovery.

## What not to do

- Do not reuse a prior session ID or saved plan.
- Do not run with a dirty worktree or a different Git revision.
- Do not broaden API ingress beyond the approved `/32`.
- Do not use ambient Codex-specific environment configuration.
- Do not resize RDS, change worker counts, tune the pool, or alter the downstream
  limit during the experiment.
- Do not interpret host-local portability evidence as the RDS-backed baseline.
- Do not turn a diagnostic canary into a publishable rate point.
- Do not leave the stack alive for interactive analysis after the runner ends.

See also:

- `docs/aws-cloud-session-checklist.md`
- `docs/aws-vertical-scaling-experiment.md`
- `docs/aws-cost-controls.md`
