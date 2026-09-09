# TrackRelay implementation plan 2 — Post-MVP

This is the active plan for work after the first published autoscaling MVP.
The [original implementation plan](implementation-plan.md) preserves the
implementation and experiment history. Detailed protocols and results belong
in their linked contracts and reports, rather than accumulating in this plan.

## Purpose and starting point

Study cloud elasticity and scalability through a simple API relay while
maintaining timely delivery, exact reconciliation, and correct business effects.
Compare capacity strategies without assuming the asynchronous architecture wins.

The [current MVP](../README.md) demonstrates one successful AWS run: demand rose
from 1 to 25 events/s, workers automatically expanded 1→8→1, and all 5,730 events
were delivered. The [measurement notes](autoscaling-report.md) explain its
guardrails and limitations. It establishes bounded elasticity, not maximum
sustainable capacity, an architecture multiplier, or measured cost savings.

## Working approach

- Develop on `codex/post-mvp-evidence`; keep `main` at the presentable MVP until
  verified results and their explanation justify a merge and README update.
- Work one small step at a time: explain the change, make it, run a focused
  check, and commit the verified step.
- Develop and analyze locally with AWS off. A new billable experiment requires
  a concrete session plan, explicit budget approval, and verified teardown.
- Freeze workloads, measurement windows, acceptance rules, and comparison
  conditions before running treatments. Preserve failures and missing evidence.
- Keep API acceptance latency separate from downstream delivery latency.
  Post-load drain alone does not establish sustainable completed throughput.
- Keep this plan concise. Record detailed decisions in the experiment contract
  and outcomes in a report; link them here when a milestone is completed.

## Milestone 1 — Fixed versus elastic worker capacity

**Question:** With the same asynchronous deployment and workload, what does
enabling worker autoscaling change about supported demand and delivery behavior?

**Priority:** First. This supersedes the earlier sequencing decision in the
[baseline review](aws-async-capacity-baseline-review.md) to defer the paired
comparison until after the synchronous comparison. Its evidence limitations
still apply. Historical controls and the successful standalone diagnostic do
not constitute a matching qualified pair.

**Evidence needed:** Fixed and elastic treatments with matching application,
infrastructure, workload, initial state, and observation rules. Only the worker
capacity policy changes. Retain offered and completed rates, running workers,
unfinished work/queue age, latency, reconciliation, and teardown evidence.

Next steps:

- [x] Audit the existing paired controller and reporter against the current
  [experiment definition](aws-elasticity-experiment.md),
  [v6 contract](aws-elasticity-v6-contract.md), and
  [paired runbook](aws-elasticity-runbook.md). Identify local gaps before
  proposing another run; preserve historical qualification rules. See the
  [readiness audit](post-mvp-paired-audit.md) for findings and the next local
  implementation step: version the consecutive-rate reporting rule.
- [ ] Freeze the paired protocol, including identical per-step completion,
  backlog, latency, and correctness gates. Define timing origins for scale-out,
  backlog drain, and return to minimum; distinguish valid fixed-worker overload
  from a broken experiment or incomplete evidence. Keep any short-waveform
  capacity claim explicitly limited to the tested demand steps.
- [ ] Complete necessary local fixes and focused checks for collection,
  qualification, reporting, failure handling, and unconditional cleanup.
- [ ] Prepare the concrete cloud session and cost review, then obtain approval
  before provisioning. Collect both treatments under the frozen protocol and
  verify complete teardown.
- [ ] Analyze frozen evidence and build the report locally with AWS off. Produce
  one large aligned figure comparing offered load, running workers, unfinished
  work or queue age, and ingestion p95 with its 500 ms SLO line. Show completion
  evidence alongside it; label latency windows and preserve transient failures.
- [ ] Report each treatment's highest consecutive passing tested demand step,
  the ratio only when supported, worker expansion A→B, observed scale-out time,
  backlog drain time, and return to A. Retain unavailable values and sampling
  limits instead of inventing precision or a multiplier.
- [ ] Prepare a readable report and README change for review and merge.

**Success evidence:** A valid pair explains whether autoscaling changes which
demand steps meet every shared end-to-end guardrail, and whether added capacity
is released after demand falls. If there is no improvement, report that result;
do not tune or relabel completed runs to manufacture one.

**README update threshold:** A reproducible comparison figure, a supported
one-sentence finding, and clear measurement limits improve on the standalone
demo. A measured passing-step ratio is not an async-versus-sync multiplier or
an unrestricted maximum-throughput claim.

## Milestone 2 — Synchronous versus asynchronous scalability

**Question:** How do larger synchronous compute and independently scaled async
workers compare when delivering the same business outcome?

- [ ] Design a matched sustained-load protocol using the
  [baseline review](aws-async-capacity-baseline-review.md) as the starting point.
  Define shared delivery deadlines, completed-throughput/backlog gates,
  correctness rules, resource accounting, and repetition rules before execution.
- [ ] Compare synchronous hardware sizes and declared async capacities under
  that protocol; distinguish fixed-capacity scalability from automatic response
  to changing demand. Document remaining topology and resource differences.
- [ ] Publish the supported rates, delivery latency, resource use, variability,
  and bottlenecks. Report a ratio only for valid matched measurements.

**Success evidence:** Both architectures face the same delivery requirements;
additional compute and its limits are visible. The historical 10-second hardware
ladder cannot supply the denominator for the current async waveform.

**README update threshold:** A fair comparison adds a useful finding about
capacity strategy. Neither an async win nor an equal-cost claim is assumed.
Expand this milestone into small implementation steps after Milestone 1.

## Milestone 3 — Downstream failure and recovery

**Question:** How do the architectures preserve correctness and recover timely
delivery when the downstream system slows or becomes unavailable?

- [ ] Freeze controlled slowdown/outage scenarios, caller and worker retry
  behavior, and explicit delivery and recovery deadlines for both architectures.
- [ ] Measure acceptance, completed delivery, unfinished work, recovery/drain
  time, missing events, duplicate effects, and final shipment correctness.
- [ ] Publish the observed recovery behavior and tradeoffs with retained evidence.

**Success evidence:** Every event is accounted for and recovery is measured
against declared deadlines; eventual drain does not conceal missed service goals.

**README update threshold:** A concise resilience finding adds to the capacity
story. Keep detailed diagnostics in a separate report. Expand this milestone
when the shared comparison measurements are ready.
