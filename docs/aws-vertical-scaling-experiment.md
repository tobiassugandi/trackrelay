# Stage 9.3 short cloud hardware-flexibility experiment

## Purpose

Stage 9.3 now asks one intentionally small question:

> Can the unchanged synchronous TrackRelay deployment pass a higher short-run
> offered rate after its EC2 instance type is changed from `t3.small` to
> `c7i-flex.large`?

This demonstrates a practical cloud benefit: suitable hardware can be selected
and provisioned without buying, installing, and owning another physical machine.
It is not an autoscaling claim and it is not intended to locate the system's
ultimate bottleneck.

The desired result is deliberately modest:

```text
t3.small highest passing point       X events/s
c7i-flex.large highest passing point Y events/s

Y > X
```

If `Y > X`, changing only the declared EC2 instance type produced an observed
improvement under the short protocol. The result does not claim that CPU count
alone caused it: both machines expose two vCPUs, while processor generation,
burstable behavior, memory, network characteristics, and instance family differ.

## Frozen comparison

| Input | Economical machine | Stronger machine |
| --- | --- | --- |
| EC2 instance type | `t3.small` | `c7i-flex.large` |
| Architecture | x86_64 | x86_64 |
| Application Git revision | Same | Same |
| Container image digest | Same | Same |
| Application architecture | Synchronous | Synchronous |
| API process and database pool | Same | Same |
| PostgreSQL | Same private RDS instance and configuration | Same |
| Downstream simulator | Same healthy mode and resource limits | Same |
| Load generator host and image | Same | Same |
| Rates, duration, seed, route, and pass rules | Same | Same |

The committed capacity catalog still records `m7i-flex.large` as an available
third option. It is not part of the primary experiment. Removing it avoids a
second transition that cannot answer the first, simpler question.

## Short workload

Each machine receives the same increasing ladder:

```text
10, 25, 50, 100, 200 events/s
```

Each point runs once for 10 seconds. A fresh run identity is used, and synthetic
database/downstream state is reset after the point. The machine stops immediately
after its first failed point. The stronger machine always restarts at 10/s; it
does not inherit the weaker machine's starting boundary.

One trial is sufficient for this first demonstration because the aim is to find
an obvious separation quickly, not publish a precise capacity estimate. The
report must call it a short-run result.

The k6 driver preallocates:

```text
max(1, ceil(offered rate / 2))
```

and may grow to at most one VU per offered event/s. The removed 100-VU floor was
counterproductive at 10 events/s: it created unnecessary connection fan-out and
was associated with intermittent multi-second connection setup and dial
timeouts even while successful API latency and server utilization had headroom.

That fix reduces the artificial pressure; it does not redefine success. A real
`dial: i/o timeout` means a request never reached TrackRelay, so that rate still
fails the strict experiment.

## Pass and stop rules

A rate passes only when all existing experiment guardrails pass:

- k6 completes and produces a valid summary;
- p95 response latency is below 500 ms;
- request errors are below 1%;
- no scheduled iterations are dropped or missing;
- every accepted event is accounted for;
- duplicate business effects remain zero; and
- every final shipment state matches the input manifest.

The first failure is preserved and higher rates are skipped. This avoids spending
time collecting overload points that cannot improve the observed boundary.

The terminal may print k6 `level=error` when the first failed point crosses a
threshold. That is an expected measured failure if the controller continues,
collects reconciliation evidence, writes the assessment, and proceeds. A
controller message such as `AWS vertical-scaling session failed` is instead an
experiment-infrastructure failure and invalidates the incomplete run.

## Result rule

The compact report contains only:

- tested rates for each machine;
- highest passing rate for each machine;
- first failing rate for each machine; and
- whether `c7i-flex.large` passed a higher rate than `t3.small`.

The primary result is positive only when the stronger machine's highest passing
rate is strictly higher. Equal boundaries are inconclusive, not evidence of an
improvement.

If the coarse ladder is inconclusive but both boundaries fall within the same
interval, declare a refinement set before running it. Use the same refinement
rates, order, duration, and pass rules on both machines. For example, if both
machines pass 50/s and fail 100/s, a defensible refinement is:

```text
60, 75, 90 events/s
```

Do not choose different rates after seeing one machine's outcomes, and do not
keep searching until a favorable result appears.

## Evidence and safety retained

The short protocol deliberately omits CloudWatch publication waits, 180-second
loads, warm-up rounds, one-minute quiet periods, and three-trial majorities. It
retains the parts needed for an honest and safe cloud run:

- exact k6 process boundaries and summary;
- detached API/downstream runtime sampling with a stop handshake;
- complete database and downstream reconciliation;
- per-rate state reset;
- the immutable image, RDS, and configuration identities;
- a saved Terraform transition plan restricted to the EC2 host;
- post-transition SSM, Docker, image, TLS, and API validation;
- session journaling; and
- unconditional Terraform destroy plus native absence verification.

## Deferred diagnostic experiment

After the short result exists, a separate optional protocol may use 180-second
points, repeated trials, CloudWatch series, CPU credits, process CPU, memory,
pool pressure, RDS metrics, and downstream measurements to estimate a stable
capacity envelope and diagnose why the machines differ.

That later study answers a different question. It must not delay or be silently
merged into this simple demonstration of cloud hardware flexibility.
