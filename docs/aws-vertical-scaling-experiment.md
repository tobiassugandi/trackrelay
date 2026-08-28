# Stage 9.3 synchronous infrastructure-scaling experiment

## Question

Stage 9.3 asks one deliberately simple question before TrackRelay changes its
application architecture:

> With the synchronous application and RDS held constant, how does the
> healthy-downstream performance envelope change as TrackRelay moves from an
> economical EC2 starting point to workload-fit and then larger hardware?

This is a test of cloud hardware flexibility and vertical scaling. It is not a
test of automatic elasticity, and improvement is not assumed in advance. If the
API host is not the first constrained resource, a different or larger instance
may produce little or no capacity gain.

The 25 events/s result from cloud session 1 is not the small-capacity control.
That workload used PostgreSQL on the EC2 host. All three Stage 9.3 tiers must use
the same private RDS instance so that database placement does not change between
them.

## Controlled hardware ladder

| Property | Economical baseline | Workload-fit migration | Within-family scale-up |
| --- | --- | --- | --- |
| Application revision and image digest | Same | Same | Same |
| Application architecture | Synchronous | Synchronous | Synchronous |
| EC2 instance type | `t4g.small` | `c8g.large` | `c8g.4xlarge` |
| API process and connection-pool settings | Same | Same | Same |
| RDS class, engine, storage, and parameters | Same | Same | Same |
| Downstream mode and resource allowance | Healthy and fixed | Healthy and fixed | Healthy and fixed |
| Workload, driver, route, duration, and SLO | Same | Same | Same |
| Experiment state | Fresh identity namespace | Fresh identity namespace | Fresh identity namespace |

The EC2 instance type is the only deployment input changed between runs, but the
first transition intentionally changes several hardware properties together.
Read-only EC2 and Price List API queries on 2026-08-28 selected this ladder:

- economical baseline: `t4g.small`, 2 vCPUs, 2 GiB, burstable Graviton2,
  USD 0.02120 per hour;
- workload-fit migration: `c8g.large`, 2 vCPUs, 4 GiB, non-burstable
  Graviton4, USD 0.09163 per hour;
- within-family scale-up: `c8g.4xlarge`, 16 vCPUs, 32 GiB, non-burstable
  Graviton4, USD 0.73304 per hour.

The first transition demonstrates the ability to choose a hardware profile that
better fits an observed workload; it must not be attributed to CPU count alone.
The second transition is the cleaner vertical-scaling comparison: the same
non-burstable Graviton4 family gains eight times the vCPUs and memory. Prices are
public On-Demand Linux instance time only, not the complete session estimate.
Catalog facts and timestamps are frozen in
`results/aws-vertical-scaling/ec2-capacity-selection.json`.

Terraform defaults to the `t4g.small` baseline and rejects every instance type
outside the three frozen tiers. It configures standard CPU credits for the
burstable baseline and no credit block for either C8g tier. The guarded lifecycle
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
rate cannot restore the performance envelope after the first failing rate.

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
This is necessary because one fully occupied core appears as only 6.25% aggregate
CPU on a 16-vCPU host.

The current image intentionally remains a one-worker Uvicorn deployment. A local
qualification probe showed that it can benefit from more than one CPU because
synchronous FastAPI handlers overlap native database and HTTP work in a thread
pool, even though Python bytecode remains GIL-constrained. It did not demonstrate
efficient use of 16 CPUs: an unrestricted 50 events/s point passed while a
one-CPU-capped replay dropped work, but both two- and eight-CPU treatments failed
at 100 events/s. These local results are process-model diagnostics, not AWS
performance claims. Stage 9.3 therefore measures the current deployment without
promising a large scale-up gain.

Resource consumption is not itself useful throughput. A rate contributes its
observed completed rate to `productive_throughput_per_second` only when execution,
latency, error, completeness, and reconciliation guardrails all pass. A failed
overload point reports zero productive throughput even if native threads consume
several cores while draining abandoned work.

A private SSM command now starts before each load point and runs a sampler in a
one-off container on the existing Compose network. Every five seconds it reads
the API and downstream runtime endpoints, preserving both processes' CPU, thread,
memory, and host-counter snapshots without publishing the simulator or adding an
API proxy path. The resulting timeline is gzip-compressed beneath a strict SSM
output-size limit, decoded locally, validated against the run identity, and
saved with the other raw rate evidence. Independently, persisted delivery
attempts are aggregated into the same five-second UTC intervals with attempt and
outcome counts, p95 latency, and maximum latency. This distinguishes simulator
pressure from API work while keeping the measurement interval fixed across all
hardware tiers.

## Interpretation and stopping rules

- The `t4g.small` to `c8g.large` result may be described only as a workload-fit
  hardware migration. Family, processor generation, memory, network profile,
  and burstability change together, so CPU alone cannot receive causal credit.
- The `c8g.large` to `c8g.4xlarge` result is the cleaner within-family vertical
  comparison because processor generation and burstability remain unchanged.
- High EC2 pressure with healthy RDS, pool, and downstream evidence supports an
  EC2-compute bottleneck. A substantial envelope increase then demonstrates
  useful vertical scaling without application changes.
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

1. Provision the `t4g.small` baseline and fixed private RDS configuration.
2. Deploy one immutable application image and verify RDS correctness.
3. Run and freeze the economical baseline envelope and aligned evidence,
   including CPU-credit behavior.
4. Reset synthetic application and simulator state without changing controls.
5. Stop the EC2 instance, switch to `c8g.large`, verify the unchanged image and
   controls, and replay the identical envelope.
6. Preserve that result, reset state, switch to `c8g.4xlarge`, verify the same
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
