# Session 4 resource and cost review — 2026-09-03

Status: **teardown gap corrected and approved cleanup verified; cloud approval outstanding**.
No session-4 infrastructure was created during this review. This is an
estimate in USD, not a bill, a hard spending cap, or an elasticity result.

## Proposed operating envelope

Policy-v4 addendum (2026-09-04): new runs also publish two custom high-resolution
CloudWatch metrics. Each of the two API replicas submits one two-metric request
every ten seconds (about 720 PutMetricData requests/hour combined while both
publishers run). One scale-out alarm now uses ten-second evaluation rather than
standard resolution. Publisher database reads use one additional bounded
connection per API replica, with no additional ECS task. Recheck current regional
custom-metric, API-request and high-resolution-alarm charges before approval;
the historical estimate below does not price this addition. Publishing stops with
task teardown; retained custom metric data follows CloudWatch retention rather
than Terraform resource deletion. No approval or cost ceiling is increased here.

- Region: `ap-southeast-3` (Jakarta), profile `trackrelay-admin`.
- One fixed-versus-elastic experiment on the unchanged asynchronous stack.
  Two API tasks at 1 vCPU / 2 GiB each; one simulator at 0.25 vCPU / 0.5 GiB;
  workers at 0.25 vCPU / 0.5 GiB each, fixed at one and then bounded at eight.
- One private Single-AZ PostgreSQL 17 `db.t4g.micro`, encrypted 20 GiB gp3;
  one ALB, two queues, three ECR repositories, one migration task, private
  service discovery, and the networking/IAM/observability resources listed in
  the [operator runbook](aws-elasticity-runbook.md).
- Original Container Insights (`enabled`, not `enhanced`). Four application
  log groups and the additional performance-log group are Terraform-owned.
- Expected operator window: **60–90 minutes**, an engineering estimate rather
  than an AWS completion guarantee. Candidate-v3 traffic totals 38 minutes; stable
  drain, reset, alignment, publication, deployment, and teardown add time.
- At 90 minutes from foundation apply, stop experiment work and enter cleanup
  if it has not already finished. Reserve another 30 minutes for cleanup: a
  **two-hour planning envelope**, not permission to abandon lingering resources.
  Continue teardown/verification if AWS operations exceed that estimate.
- Rounded planning estimate: **$3 before tax**, with a proposed **$5 session
  ceiling** including contingency. Approval remains outstanding and must bind
  to a fresh session ID, saved plan/revision, `/32`, and unconditional teardown
  after a fresh plan is prepared and reviewed.

The runner validates an approved ceiling against the configured monthly budget
but does not enforce live dollar or wall-clock limits. The operator must watch
elapsed time. Billing can arrive after teardown. This estimate does not rely on
free-tier credits, savings plans, or reserved capacity.

## Public Jakarta unit rates

Retrieved from AWS's public On-Demand price lists on 2026-09-03. Links use the
observed version, not a mutable `current` catalog. Each SKU identifies the
selected price dimension; unrelated engines, architectures, Outposts, and
reserved-capacity products were excluded.

| Item | USD unit rate | SKU / source |
| --- | ---: | --- |
| Fargate Linux x86 vCPU | 0.05056 / vCPU-hour | `WXV24WRHFC4V9UVZ`, [ECS catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/20260831092155/ap-southeast-3/index.json) |
| Fargate memory | 0.00553 / GB-hour | `6NBA8AWU5J2UTS35`, same ECS catalog |
| PostgreSQL `db.t4g.micro`, Single-AZ | 0.025 / hour | `HVPPAN9R7VGB862X`, [RDS catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/20260903002836/ap-southeast-3/index.json) |
| PostgreSQL gp3 storage | 0.138 / GB-month | `3VQNQXRBT2YN5QJB`, same RDS catalog |
| PostgreSQL T4g surplus CPU credit | 0.075 / vCPU-hour | `XUHMPZ8ZFG47FW7Z`, same RDS catalog |
| Application Load Balancer | 0.0252 / hour | `2ESBTEEMRXNC4MCZ`, [ELB catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSELB/20260831092255/ap-southeast-3/index.json) |
| ALB load capacity | 0.008 / LCU-hour | `425VV7DYSZH6DWGG`, same ELB catalog |
| In-use public IPv4 | 0.005 / address-hour | `Y6TNA9ZFV9Q8X3AG`, [VPC catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonVPC/20260831092232/ap-southeast-3/index.json) |
| Custom metrics, first pricing tier | 0.30 / metric-month | `25WHJB4GX3FRTNER`, [CloudWatch catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/20260831092148/ap-southeast-3/index.json) |
| Standard log ingestion / storage | 0.70 / GB; 0.03 / GB-month | `9S6BM6E4HCB59BFR` / `RAZY9E76R9JUCNQK`, same CloudWatch catalog |
| GetMetricData retrieval / ordinary paid requests | 0.01 / 1,000 metrics; 0.01 / 1,000 requests | `A82PPMQA4UC6EVHB` / `C84XQR3BMRP9KYD6`, same CloudWatch catalog |
| Standard alarm | 0.10 / alarm-month | `HNAU5YRN3PNFRYMT`, same CloudWatch catalog |
| Custom dashboard | 3.00 / dashboard-month | `8AWBXE5MPQY58BK4`, [CloudWatch global catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/20260831092148/index.json) |
| SQS standard requests | 0.40 / million | `3FFU2JU83EB5H7UP`, [SQS catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSQueueService/20250828200713/ap-southeast-3/index.json) |
| ECR private storage | 0.10 / GB-month | `WF7JH74F8HUWXTZB`, [ECR catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECR/20260831092155/ap-southeast-3/index.json) |
| Database secret / secret API requests | 0.40 / secret-month; 0.05 / 10,000 requests | `MKY22FQCFQ3SBRFF` / `EQZPSKJUU2ZDYM5D`, [Secrets Manager catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSSecretsManager/20260831092330/ap-southeast-3/index.json) |
| Cloud Map registered resource / API requests | 0.10 / resource-month; 1.00 / million requests | `657YBNAEZ3UDU9RD` / `NEVKWDGN5YQ3A2Y2`, [Cloud Map catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSCloudMap/20260831092244/ap-southeast-3/index.json) |

Fargate's included 20 GB ephemeral storage is sufficient for these task
definitions; no extra ephemeral-storage allowance is configured. Its billing
starts at image pull, with a one-minute minimum for Linux.
[Fargate pricing](https://aws.amazon.com/fargate/pricing/).

ALB partial hours round up. The model budgets two full hours and one LCU for
each; the LCU assumption is an allowance, not a measured maximum.
[ALB pricing](https://aws.amazon.com/elasticloadbalancing/pricing/).

RDS T4g uses Unlimited mode, so surplus CPU can cost extra even during a short
session. The reserve below deliberately prices four full vCPU-hours at the
credit rate, without subtracting earned baseline credits.
[RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/).

Original Fargate Container Insights publishes metrics by cluster, service, and
task-family name, not one new series set per replica. AWS's published model gives
13 cluster + 15 per service + 10 per task-name metrics: 98 for one cluster,
three services, and four task families, rounded to 100 for this estimate. Custom
metrics, alarms, and the $3/month custom dashboard are prorated by active hours;
performance-log and application-log ingestion are separate.
[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).

## Two-hour sensitivity calculation

For a deliberately conservative capacity scenario, hold **eight workers for
the entire two hours**, even though the fixed control must use only one.
September has 720 hours; monthly storage/metric estimates below use `2 / 720`.
Rounding, additional active billing-hour buckets, unexpected task replacements,
and workload/log volume can change actual charges.

| Component | Assumption | Estimated USD |
| --- | --- | ---: |
| Fargate services | `(4.25 × 0.05056 + 8.5 × 0.00553) × 2` | 0.5238 |
| Public IPv4 | 11 task addresses + 2 ALB addresses, for two hours | 0.1300 |
| ALB and capacity | Two ALB-hours + two LCU-hours | 0.0664 |
| RDS compute and storage | Two hours + 20 GB gp3 | 0.0577 |
| RDS CPU-credit reserve | Four vCPU-hours, without baseline credit offset | 0.3000 |
| CloudWatch metrics | 100 metric series for two hours | 0.0833 |
| Logs | 1 GB ingestion + two-hour storage allowance | 0.7001 |
| CloudWatch requests | 10,000 paid metric retrieval/request units | 0.1000 |
| Dashboard and two alarms | Two-hour allowance without free-tier deduction | 0.0089 |
| SQS | 100,000 standard request units including polling/retries | 0.0400 |
| ECR, secret, Cloud Map | 3 GB images, one secret, one resource; 1,000 secret and 1,000 Cloud Map calls | 0.0082 |
| Private hosted-zone reserve | Full first-zone monthly charge, conservatively retained | 0.5000 |
| Transfer allowance | Cross-AZ/internet traffic contingency, not a quoted unit price | 0.2500 |
| Migration / transient overlap | Additional compute/address contingency | 0.0500 |
| **Total, rounded** | **No tax or credits applied** | **2.82** |

The underlying service compute alone is $0.15405/hour at one worker and
$0.261885/hour at eight. The seven additional workers cost $0.107835/hour in
compute plus $0.035/hour in public IPv4 under this topology. Repeated failures,
large unexpected logs, or resources left running invalidate the two-hour model.

The hosted-zone reserve is intentionally conservative: AWS does not charge a
zone deleted within 12 hours, and private-zone DNS queries are free. Do not
assume that waiver if cleanup fails.
[Route 53 pricing](https://aws.amazon.com/route53/pricing/).
Same-region ECR pulls do not incur transfer charges; other network traffic is
covered by a separate allowance rather than mispriced as image egress.
[ECR pricing](https://aws.amazon.com/ecr/pricing/).

## Account readiness and discovered blocker

Read-only checks on 2026-09-03 passed for the selected non-root identity and
region. Terraform state listed no managed resources. The applied regional
Fargate On-Demand quota was 12 vCPUs, above the 4.25-vCPU steady peak. That quota
is shared with other workloads and still needs available-capacity review at
launch. The monthly cost budget was inspected privately; budget data is delayed
and must not be treated as a real-time spending meter.

The teardown review found two remaining Container Insights performance log
groups belonging to earlier session-3 runs. AWS reported zero stored bytes and
one-day retention on both. Their exact paths and session mapping are retained
in ignored local preflight evidence, not copied into this public pricing note.

The original cause, corrected by the follow-up implementation:

- `async_platform.tf` previously owned only `/trackrelay/<suffix>/...` application
  groups.
- `inventory_rehost_resources` previously counted only that application-log prefix.
- Container Insights automatically creates a different group,
  `/aws/ecs/containerinsights/<cluster-name>/performance`, which those checks
  missed. [AWS performance-log reference](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-reference-performance-logs-ECS.html).

Consequently the earlier `teardown_verified` status did **not** establish
absence of these performance groups. This finding does not invalidate the
recorded integration/correctness measurements, but does qualify the earlier
claim that every session resource had been removed. Historical raw evidence
has not been rewritten.

### Follow-up correction and approved cleanup

The user explicitly approved the fix and deletion of those two groups. The
follow-up implemented a Terraform-owned, one-day performance log group with a
cluster dependency that orders creation before telemetry and deletion after
cluster teardown. Native inventory now reports a separate
`container_insights_log_groups` count. The application-log count retains its
original meaning; old inventories are not backfilled with assumed zeros.
The session-4 report requires the new count and refuses missing or nonzero
values.

Before deletion, the exact group names and creation timestamps were rechecked
against their session hashes. Both ECS clusters were `INACTIVE`, with no active
services or pending/running tasks. The groups reported zero stored bytes and
one-day retention; stream metadata showed only older session events. The two
approved groups were deleted, and their absence verified at 10:30 UTC on
2026-09-03. Any remaining events were deleted irreversibly with the groups.

The strengthened native inventory then reported all **30** resource categories
zero for both sessions at 10:31 UTC, and Terraform state was empty. New audit
files are retained under
`results/aws-elasticity-preflight/20260903T101827Z/`; the original session-3
manifests and measurements remain unchanged. Local verification passed 539
Python tests (15 database integration tests deselected), 17 mocked-provider
Terraform tests, and lint, including lifecycle ordering and fail-closed
inventory/report regression tests.

The next gate is a fresh read-only session-4 plan and explicit approval of its
identity, ingress, revision/hash, resources, $5 proposed ceiling, operating
window, and unconditional teardown. Cleanup authorization did not authorize
provisioning or experiment spend.
