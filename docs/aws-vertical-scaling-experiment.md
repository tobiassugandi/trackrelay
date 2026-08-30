# Stage 9.3 synchronous hardware-flexibility experiment

## Question

Stage 9.3 asks one deliberately simple question before TrackRelay changes its
application architecture:

> With the synchronous application and RDS held constant, how does the
> healthy-downstream performance envelope change as TrackRelay moves from an
> economical burstable EC2 starting point to compute-optimized and then
> memory-optimized hardware?

This is a test of cloud hardware flexibility under the account's Free plan. It
is not a test of automatic elasticity, and improvement is not assumed in
advance. All three tiers expose two vCPUs. The experiment can show the value of
escaping burst-credit throttling or choosing a better resource profile, but it
cannot claim that adding CPU capacity caused the result.

The 25 events/s result from cloud session 1 is not the small-capacity control.
That workload used PostgreSQL on the EC2 host. All three Stage 9.3 tiers must use
the same private RDS instance so that database placement does not change between
them.

## Controlled hardware ladder

| Property | Economical baseline | Compute-optimized migration | Memory-optimized migration |
| --- | --- | --- | --- |
| Application revision and image digest | Same | Same | Same |
| Application architecture | Synchronous | Synchronous | Synchronous |
| EC2 instance type | `t3.small` | `c7i-flex.large` | `m7i-flex.large` |
| API process and connection-pool settings | Same | Same | Same |
| RDS class, engine, storage, and parameters | Same | Same | Same |
| Downstream mode and resource allowance | Healthy and fixed | Healthy and fixed | Healthy and fixed |
| Workload, driver, route, duration, and SLO | Same | Same | Same |
| Experiment state | Fresh identity namespace | Fresh identity namespace | Fresh identity namespace |

The EC2 instance type is the only deployment input changed between runs, but the
first transition intentionally changes several hardware properties together.
Read-only EC2 and Price List API queries on 2026-08-30 selected this x86_64 ladder:

- economical baseline: `t3.small`, 2 vCPUs, 2 GiB, burstable Intel Skylake,
  USD 0.02640 per hour;
- compute-optimized migration: `c7i-flex.large`, 2 vCPUs, 4 GiB,
  non-burstable Intel Sapphire Rapids, USD 0.09775 per hour;
- memory-optimized migration: `m7i-flex.large`, 2 vCPUs, 8 GiB,
  non-burstable Intel Sapphire Rapids, USD 0.11970 per hour.

The first transition demonstrates the ability to choose a hardware profile that
better fits an observed workload; burstability, processor generation, memory,
network profile, and instance family change together. The second transition
holds processor generation, vCPU count, and burstability constant while doubling
memory from 4 GiB to 8 GiB. Prices are public On-Demand Linux instance time only,
not the complete session estimate.
Catalog facts and timestamps are frozen in
`results/aws-vertical-scaling/ec2-capacity-selection.json`.

Terraform defaults to the `t3.small` baseline and rejects every instance type
outside the three frozen tiers. It configures standard CPU credits for the
burstable baseline and no credit block for either non-burstable tier. The guarded lifecycle
passes the selected type as an explicit Terraform command-line variable, records
it in the session manifest, and rejects later commands that identify a different
tier.

The healthy downstream simulator shares the EC2 host with the API, but Compose
now fixes it at one CPU and 1 GiB in all three tiers. Changing the host
therefore does not silently give the simulated dependency additional compute or
memory. The experiment is valid only while its observed CPU, memory, latency,
and error evidence confirms that it retains headroom. Simulator saturation makes
a rate diagnostic rather than evidence of the API host's capacity.

Every non-hardware control is frozen in the validated
`results/aws-vertical-scaling/experiment-controls.json` artifact. The deployed
API uses one process, disables access logging, and explicitly configures a
five-connection SQLAlchemy pool with ten overflow connections and connection
pre-ping. The simulator also disables access logging. The deployment scripts
write those values into the private runtime environment instead of inheriting
library defaults. RDS remains `db.t4g.micro`, PostgreSQL 17, single-AZ, with
20 GiB of gp3 storage and one unchanged session parameter group.

## Workload and acceptance

The experiment reuses the Step 8.6 workload shape, offered-rate ladder,
reconciliation invariants, and initial SLO. Every request represents one unique
shipment in the `CREATED` state. Each rate is evaluated independently; a higher
rate cannot restore the performance envelope after the first failing rate. The
six frozen rates are therefore candidate points, not a requirement to run all
six: each hardware tier preserves its first complete failing point and stops
before offering any higher rate. A tier runs all six only when every candidate
passes.

The ten-second portability rehearsal is too short for aligned AWS resource
metrics. Stage 9.3 freezes each rate at 180 seconds, providing at least three
samples even when the slowest accepted measurement period is one minute. The
same duration, rate ladder, random seed, k6 image, driver placement, workload
shape, and guardrails apply to all three tiers. The control loader also hashes
the committed Step 8.6 benchmark definition and refuses to run against a changed
source artifact.

A rate passes only when all of these remain true:

- p95 response latency is below 500 ms;
- request errors are below 1%;
- no iterations are dropped or missing;
- every accepted event is accounted for;
- duplicate business effects remain zero; and
- every final shipment state matches the manifest.

## Evidence needed to locate the bottleneck

| Evidence source | Required measurements | What it distinguishes |
| --- | --- | --- |
| k6 and reconciliation | Offered and completed rate, p95 latency, errors, dropped work, correctness | The sustainable envelope |
| API process | CPU seconds, maximum RSS, process count | Application compute or memory pressure |
| SQLAlchemy pool | Pool size, checked-out connections, overflow, and checkout pressure | Application-side database concurrency |
| EC2 host | CPU utilization, memory, network, and CPU credits when applicable | Host saturation or burst-credit effects |
| RDS | CPU utilization, connections, freeable memory, read/write latency and I/O | Database resource pressure |
| Delivery attempts and simulator | Downstream request latency, completion rate, and simulator resource use | Time spent waiting for or constraining the dependency |

Measurements must share UTC timestamps and treatment/run identifiers so the
load, latency, and resource evidence can be aligned rather than compared as
unrelated summaries.

The API runtime snapshot records its process ID, Python thread count, GIL state,
available logical CPUs, cumulative Linux scheduler counters for every logical
CPU, host memory, and SQLAlchemy pool capacity and occupancy. Each result derives
the API process's average cores used, the fraction of its available CPU capacity,
per-core host utilization, minimum available memory, and maximum pool pressure.
This is necessary because one fully occupied core appears as 50% aggregate CPU
on these two-vCPU hosts. The current image intentionally remains a one-worker
Uvicorn deployment. Since vCPU count stays fixed, Stage 9.3 does not rely on that
process consuming more cores on later tiers.

Resource consumption is not itself useful throughput. A rate contributes its
observed completed rate to `productive_throughput_per_second` only when execution,
latency, error, completeness, and reconciliation guardrails all pass. A failed
overload point reports zero productive throughput even if native threads consume
several cores while draining abandoned work.

A short, bounded private SSM command starts a named sampler container before each
load point. The command returns only after the detached sampler has read both the
API and downstream runtime endpoints and emitted a timestamped readiness marker.
The load starts only after that completed handshake; it does not depend on
eventually consistent partial SSM output from a command that is still running.
Every five seconds the detached container preserves both processes' CPU, thread,
memory, and host-counter snapshots without publishing the simulator or adding an
API proxy path. When k6 exits, the controller records that exact boundary and
signals the named container. The sampler then attempts one final observation
and exits; a second bounded SSM command returns its logs, checks its exit code,
and removes it. Interrupted loads invoke an idempotent, run-specific cleanup
command.

After overload evidence has been collected, the controller does not immediately
assume that the EC2 management plane is ready for reconciliation. It submits a
bounded harmless SSM probe with a 30-second delivery timeout until the agent
actually executes it. The reconciliation command may be resubmitted only when
its terminal invocation is `Undeliverable` or `DeliveryTimedOut` with response
code -1, which proves that the command never ran. A command that started and
then failed, timed out, or returned any other status is never replayed. Every
probe and reconciliation command ID, status, and response code is journaled in
the ignored per-rate experiment evidence.

The normal lifetime is therefore controlled by actual load completion rather
than a predicted tail. An absolute fail-safe timeout still bounds an orphaned
sampler at the configured traffic duration plus 120 seconds for process startup,
10 seconds for k6 graceful stop, one five-second sampling interval, and 15
seconds of scheduling slack.

The resulting timeline is gzip-compressed beneath a strict SSM output-size
limit, decoded locally, and validated against the run identity, timestamped
readiness marker, and exact load boundaries. Both the API and downstream sample
series must contain the whole load window; a merely nonempty or nonoverlapping
timeline is rejected. Independently, persisted delivery attempts are aggregated
into the same five-second UTC intervals with attempt and outcome counts, p95
latency, and maximum latency. This distinguishes simulator pressure from API
work while keeping the measurement interval fixed across all hardware tiers.

The benchmark driver also samples the public API runtime endpoint. Its pre-load
observation remains a strict readiness requirement, while observations during
traffic and immediately after k6 exits use a one-second timeout and are
best-effort. Overload-time timeouts, transport failures, HTTP failures, and
invalid responses are saved as sanitized phase-tagged gaps; they cannot erase a
completed k6 result or prevent private reconciliation. Successful snapshots
remain available for supporting diagnostics, while the private detached
timeline remains the coverage-controlled source for causal reporting.

AWS documents that [EC2 detailed monitoring](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/manage-detailed-monitoring.html)
provides one-minute metrics and incurs metric charges. It is enabled for Stage
9.3 so host CPU and network data can be queried at 60-second resolution. The
same batched CloudWatch query also
collects 60-second RDS CPU, connection, minimum-free-memory, read/write latency,
and read/write IOPS series. Each saved series carries the experiment run ID,
hardware treatment, UTC load boundaries, statistic, unit, native period, and
the number of seconds each AWS bucket actually overlaps the load. EC2 and RDS
resource identifiers are used only as query dimensions and are not persisted in
the portable evidence.

AWS publishes [T-family CPU-credit metrics](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html#ec2-cloudwatch-metrics)
in five-minute buckets, even when EC2 detailed monitoring is enabled. The
`t3.small` treatment therefore collects
`CPUCreditUsage` and `CPUCreditBalance` at their native 300-second period and
records their exact overlap with the 180-second load window. It does not
mislabel those buckets as per-minute observations or apportion a whole-bucket
credit value to only its overlapping seconds. Both non-burstable treatments omit credit
queries because they are non-burstable. The collector polls for a bounded ten
minutes after a load so the final five-minute bucket can close and be published;
each poll must provide continuous native-bucket coverage from the exact load
start through the exact load end for every required series. A missing leading,
interior, or trailing bucket keeps the collector polling. If the bound expires,
the rate is retained but rejected as incomplete evidence with the affected
metric names and uncovered UTC ranges recorded in the error.

## Interpretation and stopping rules

- The `t3.small` to `c7i-flex.large` result is a burstable-to-compute-optimized
  migration. Family, processor generation, memory, network profile, and
  burstability change together, so no single hardware attribute receives causal
  credit.
- The `c7i-flex.large` to `m7i-flex.large` result is a compute-to-memory-optimized
  migration. Processor generation, vCPU count, and burstability remain unchanged;
  memory doubles. Little or no gain is a useful workload-fit result rather than
  a failed elasticity claim.
- High EC2 pressure with healthy RDS, pool, and downstream evidence supports an
  EC2-compute bottleneck. A substantial envelope increase then demonstrates
  useful hardware-profile flexibility without application changes.
- High RDS pressure with spare EC2 capacity identifies the database as the first
  scaling domain. The controlled EC2 comparison stops; it does not resize RDS
  and pretend that EC2 caused the combined result.
- Persistent pool saturation with spare EC2 and RDS capacity identifies
  application concurrency as the immediate constraint.
- Low compute and database pressure with rising delivery latency identifies
  synchronous downstream waiting as the architectural constraint.
- Ambiguous or missing resource evidence makes the run diagnostic, not a
  publishable causal result.
- Downstream simulator saturation invalidates attribution at that rate; adding
  EC2 capacity to TrackRelay cannot be credited for a constrained dependency.

An evidence-backed RDS-resizing experiment may be proposed later, but it would
need a separate single-variable plan and explicit cloud-session approval.

## Bounded cloud-session procedure

Cloud session 2 will be prepared locally and must receive its own explicit cost
approval before any resource is created:

### Detached-sampler cloud canary

Before another full Stage 9.3 attempt, use a separately planned and approved
canary session on `t3.small` with the same private RDS deployment. The canary
is intentionally not a performance result: it sends only 10 events/s for 30
seconds, omits CloudWatch collection and EC2 transitions, and cannot contribute
to the final capacity comparison. Its only purpose is to prove the repaired
cloud-specific lifecycle before committing to all three hardware treatments.
To keep that purpose narrow, its setup uses `make aws-rds-deploy-canary`
directly after the deployment smoke. It does not run the unrelated six-point
host-local portability ladder; normal Stage 9.3 sessions still require that
checkpoint and use `make aws-rds-deploy`.

After RDS correctness passes, arm the canary with:

```shell
make aws-scaling-canary \
  SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  APPROVED_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  APPROVED_COST_CEILING_USD=REVIEWED_CANARY_CEILING \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR=YOUR_CURRENT_PUBLIC_IP/32
```

The gate passes only when all 300 events execute and reconcile, the timestamped
API and downstream timeline contains the exact driver-side load window, and a
separate private check proves the detached sampler container no longer exists.
Success or failure then leads directly to full Terraform destroy and native
empty-inventory verification. A passing canary is reviewed before a new full
Stage 9.3 proposal is created; it never rolls directly into that experiment.

The approved canary session `cloud-session-2-20260829T120045Z` passed on
2026-08-29. It completed all 300 requests with zero errors or dropped
iterations, reconciled 300 generated, accepted, processed, and downstream
receipt records with nothing unaccounted, and observed 87.65 ms p95 response
latency. Ten paired runtime samples began 4.34 seconds before the driver window
and ended 8.67 seconds after it. The separate absence check found no sampler
container. The runner then verified empty Terraform state and zero resources in
all native inventories. This validates the repaired measurement lifecycle; it
is not a Stage 9.3 performance treatment.

A later full-session setup attempt exposed a different observer condition at
the final 500 events/s host-local portability point: TrackRelay's own
runtime-metrics endpoint timed out under overload, so the observer process
exited even though overload is a legitimate experiment outcome. Runtime
timeline schema 3 therefore records each API and downstream sampling outcome
independently. Timeouts, transport errors, HTTP statuses, and invalid responses
become sanitized timestamped gaps while later sampling continues. The sampler
still requires one complete paired read before it signals readiness, uses a
one-second request timeout so observation cannot materially extend the load,
and preserves any downstream snapshot when only the API is starved. Coverage
means that observation attempts span the driver window; bottleneck calculations
use only the successful snapshots and still fail closed if they cannot span the
window required for a claimed diagnosis.

### Full Stage 9.3 procedure

After the approved baseline deployment has passed RDS correctness,
`make aws-scaling-prepare` performs no AWS API operation. It reads the guarded
session record and clean Git revision, validates the deployed image and RDS
facts against the committed controls, and writes a self-contained experiment
definition. That definition embeds the full hardware selection and control
models plus hashes of their source artifacts. Later tier runners must also save
the exact driver-side start and end of each k6 execution; setup, reconciliation,
CloudWatch publication waits, and other evidence collection are outside that
load window.

`make aws-scaling-run-tier` is the stateful counterpart and must be used only
inside the separately approved cloud session. It runs the frozen candidate
ladder for the session's current EC2 treatment until the first complete failed
point. Every executed rate gets its input manifest, k6
summary, exact k6 process window, local and private process timelines, persisted
downstream outcomes, reconciliation report, complete CloudWatch series, full
derived performance result, and compact pass/fail result. The session advances
to `vertical_scaling_tier_collected` only after the first failed rate has been
fully preserved, or all six candidates have passed, and the tier summary has
been written. Incomplete CloudWatch or reconciliation evidence aborts the tier
instead of being mistaken for a classified failure or a publishable result.

After a tier has been collected, `make aws-scaling-transition` is the only
supported way to move to the next frozen treatment. The command requires the
session ID and target instance type twice: once as the requested change and
once as an explicit approval. It rejects skipped or repeated tiers.

For example, the first transition in an approved cloud session is:

```shell
make aws-scaling-transition \
  SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  APPROVED_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  REHOST_INSTANCE_TYPE=t3.small \
  TARGET_INSTANCE_TYPE=c7i-flex.large \
  APPROVED_TARGET_INSTANCE_TYPE=c7i-flex.large \
  API_INGRESS_CIDR=YOUR_CURRENT_PUBLIC_IP/32
```

Before resizing, the controller deletes only synthetic `delivery_attempts`,
`events`, `shipments`, and `test_runs` rows, returns the downstream simulator to
healthy mode, clears its receipts, and verifies both stores are empty. Partner
configuration is deliberately preserved. It saves row and receipt counts as
portable reset evidence.

The controller then saves a new Terraform plan and inspects its JSON before
apply. Exactly one meaningful resource change is accepted: an in-place update
of `aws_instance.rehost` from the completed tier to the next tier. A database,
network, image, or unrelated EC2 change aborts before apply. Because an EC2
resize stops and restarts a host with an automatically assigned public address,
the same in-place plan may mark `public_ip` and its derived `public_dns` as
unknown until apply. The guard accepts those fields only as explicit
`after_unknown` computed consequences; it still rejects concrete address
assignments or changes to the ENI, private IP, subnet, or security groups. The
session record is journaled before and after apply so an interrupted transition
is visible.
After the restart, the controller requires the same EC2 instance identity, the
same RDS identifier, the requested instance type, the same digest-pinned image,
an RDS TLS connection string, and a ready API. Only then does the next tier
become runnable.

This transition command is stateful and can incur AWS charges. Its existence is
not authorization to run it: cloud session 2 still needs the separately
reviewed resource list, duration, cost ceiling, and explicit user approval.

After the final `m7i-flex.large` tier is collected,
`make aws-scaling-report REHOST_INSTANCE_TYPE=m7i-flex.large` is a local-only
command. It makes no AWS request. It refuses to report until the manifest names
all three completed tiers and both validated transitions in their frozen order.
For each tier, it re-derives the compact capacity boundary and loads the API,
downstream, reconciliation, load-window, and CloudWatch evidence belonging to
the first failing rate. If the highest rate passed, that rate is used as a
censored lower-bound diagnostic instead.

The report writes portable JSON and Markdown under
`vertical-scaling/report/` in the ignored session evidence directory. It names
the two comparisons differently: `t3.small` to `c7i-flex.large` is a
burstable-to-compute-optimized migration, while `c7i-flex.large` to
`m7i-flex.large` is a compute-to-memory-optimized migration. Capacity ratios are
marked as censored whenever either tier did not reach a failing rate.

Bottleneck labels use explicit thresholds embedded in the JSON report: 85% for
high EC2 or RDS CPU and for simulator CPU or memory allowance; 90% for
database-pool pressure; and 70% as the spare-resource boundary. An application
process-concurrency signal requires at least 0.85 average API cores while EC2
and RDS remain below the spare boundary. Synchronous downstream waiting requires
downstream p95 latency of at least 100 ms and at least 75% of API p95. The label
becomes `ambiguous` when zero or multiple explanations match. If the fixed
simulator reaches its CPU or memory allowance, attribution is invalidated rather
than credited to the EC2 treatment. These labels summarize evidence; they do
not replace the recorded measurements.

## Failure-safe session runner

After an approved cloud session has reached `rds_correctness_collected` on
`t3.small`, the preferred Stage 9.3 entry point is the explicitly armed session
runner:

```shell
make aws-scaling-session \
  SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  APPROVED_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  APPROVED_COST_CEILING_USD=REVIEWED_CEILING \
  APPROVED_TIER_ORDER='t3.small,c7i-flex.large,m7i-flex.large' \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR=YOUR_CURRENT_PUBLIC_IP/32
```

The repeated session ID, recorded cost ceiling, exact tier order, and
unconditional-teardown session ID are mechanical arming controls. All must
match the approved apply and frozen experiment. A generic request to continue
does not supply them and cannot start this command.

After the approval gate passes, the runner prepares the control artifact, runs
the three bounded rate ladders and two guarded transitions, generates the
comparison report, and then attempts full-stack destroy and native absence
verification.
Destroy and verification run after success, an experiment failure, `SIGINT`, or
`SIGTERM`; verification is still attempted if destroy itself raises an error.
Cleanup derives the current or pending EC2 tier from the manifest journal rather
than from an earlier caller assumption and journals that cleanup choice before
destroy. A cleanup error retains the original workflow error for diagnosis.

No in-process cleanup can survive `SIGKILL`, a local power loss, destruction of
the benchmark host, or loss of local Terraform state. Keep the Terraform state
and ignored session evidence on durable storage. After an abrupt interruption,
read `rehost_instance_type` from the session's `session.json`, then recover with
the same journaled tier:

```shell
make aws-down \
  SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR=YOUR_CURRENT_PUBLIC_IP/32
make aws-verify-down \
  SESSION_ID=cloud-session-2-YYYYMMDDTHHMMSSZ \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR=YOUR_CURRENT_PUBLIC_IP/32
```

The private five-second process sampler starts before k6 and stops only after a
controller signal triggered by k6's actual exit. It attempts one final observation
after that signal, so coverage does not depend on predicting startup or graceful
drain time. A named 150-second overhead budget remains solely as an absolute
orphan-safety timeout. This does not expand the CloudWatch load window: AWS
metrics remain aligned to callbacks taken immediately after the k6 process
starts and when it exits. Setup, the final sampler observation, database
settling, and metric-publication polling are therefore visible evidence but not
counted as offered-load time.

1. Provision the `t3.small` baseline and fixed private RDS configuration.
2. Deploy one immutable application image and verify RDS correctness.
3. Run and freeze the economical baseline envelope and aligned evidence,
   including CPU-credit behavior.
4. Reset synthetic application and simulator state without changing controls.
5. Stop the EC2 instance, switch to `c7i-flex.large`, verify the unchanged image and
   controls, and replay the identical envelope.
6. Preserve that result, reset state, switch to `m7i-flex.large`, verify the same
   controls, and replay the envelope again.
7. Build both transition comparisons and the bottleneck report while the
   evidence is available.
8. Destroy every session resource and verify empty Terraform state plus empty
   native service inventories.

AWS remains off during ordinary implementation. Read-only availability and price
queries do not authorize provisioning, and an ordinary `continue` does not
authorize cloud session 2. Region-level offering evidence also does not guarantee
that EC2 will have capacity in a particular Availability Zone when the approved
session begins.
