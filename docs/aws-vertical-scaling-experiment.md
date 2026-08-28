# Stage 9.3 synchronous vertical-scaling experiment

## Question

Stage 9.3 asks one deliberately simple question before TrackRelay changes its
application architecture:

> With the synchronous application and RDS held constant, how much does the
> healthy-downstream performance envelope change when only EC2 capacity changes?

This is a test of rapid vertical scaling and cloud hardware flexibility. It is
not a test of automatic elasticity, and improvement is not assumed in advance.
If the API host is not the first constrained resource, a larger instance may
produce little or no capacity gain.

The 25 events/s result from cloud session 1 is not the small-capacity control.
That workload used PostgreSQL on the EC2 host. Both Stage 9.3 treatments must use
the same private RDS instance so that database placement does not change between
them.

## Causal comparison

| Property | Small-capacity control | Large-capacity treatment |
| --- | --- | --- |
| Application revision and image digest | Same | Same |
| Application architecture | Synchronous | Synchronous |
| EC2 instance type | Approved small type | Approved larger type |
| API process and connection-pool settings | Same | Same |
| RDS class, engine, storage, and parameters | Same | Same |
| Downstream mode and resource allowance | Healthy and fixed | Healthy and fixed |
| Workload, driver, route, duration, and SLO | Same | Same |
| Experiment state | Fresh identity namespace | Fresh identity namespace |

The EC2 instance type is the only treatment variable. Read-only EC2 and Price
List API queries on 2026-08-28 selected this pair:

- control: `c8g.large`, 2 vCPUs, 4 GiB, USD 0.09163 per hour;
- treatment: `c8g.4xlarge`, 16 vCPUs, 32 GiB, USD 0.73304 per hour.

Both are current Jakarta offerings from the same non-burstable Graviton4
compute-optimized family. The treatment provides eight times the vCPUs and
memory without introducing CPU-credit or processor-generation differences.
The price is public On-Demand Linux instance time only; it is not the complete
session estimate. The catalog facts and their timestamps are frozen in
`results/aws-vertical-scaling/ec2-capacity-selection.json`.

The healthy downstream simulator shares the EC2 host with the API, but Compose
now fixes it at one CPU and 256 MiB in both treatments. Resizing the host
therefore does not silently give the simulated dependency additional compute or
memory. Its behavior, resource use, and observed response latency must still be
recorded so simulator saturation cannot be mistaken for an API limit.

## Workload and acceptance

The experiment reuses the Step 8.6 workload shape, offered-rate ladder,
reconciliation invariants, and initial SLO. Every request represents one unique
shipment in the `CREATED` state. Each rate is evaluated independently; a higher
rate cannot restore the performance envelope after the first failing rate.

The exact tier duration may be longer than the ten-second portability rehearsal.
It must provide at least three samples at the slowest required resource-metric
interval. The same frozen duration is used for both treatments.

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

## Interpretation and stopping rules

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

An evidence-backed RDS-resizing experiment may be proposed later, but it would
need a separate single-variable plan and explicit cloud-session approval.

## Bounded cloud-session procedure

Cloud session 2 will be prepared locally and must receive its own explicit cost
approval before any resource is created:

1. Provision the approved small EC2 capacity and fixed private RDS configuration.
2. Deploy one immutable application image and verify RDS correctness.
3. Run and freeze the small-capacity envelope and aligned resource evidence.
4. Reset synthetic application and simulator state without changing controls.
5. Stop the EC2 instance, change only its approved instance type, and restart it.
6. Verify the same image and controls, then replay the identical envelope.
7. Build the comparison and bottleneck report while the evidence is available.
8. Destroy every session resource and verify empty Terraform state plus empty
   native service inventories.

AWS remains off during ordinary implementation. Read-only availability and price
queries do not authorize provisioning, and an ordinary `continue` does not
authorize cloud session 2. Region-level offering evidence also does not guarantee
that EC2 will have capacity in a particular Availability Zone when the approved
session begins.
