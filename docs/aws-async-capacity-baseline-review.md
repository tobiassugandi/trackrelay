# Successful elasticity demo and capacity-baseline review

Reviewed 2026-09-04 with AWS off. This is an evidence review and next-work plan,
not a frozen capacity protocol, operator runbook or authorization to spend.

## What has now passed

Session `cloud-session-4-20260904T121328Z` used workload v6, policy v6 and
application revision `d2ca9d465d19e3c7642a95111a58cce213da5ec0`.
Its [summary](../results/aws-sessions/cloud-session-4-20260904T121328Z/elasticity/diagnostic/elastic/summary.json)
passes every diagnostic qualification gate. The
[measurement](../results/aws-sessions/cloud-session-4-20260904T121328Z/elasticity/diagnostic/elastic/result.json)
records:

| Measurement | Result |
| --- | --- |
| Scheduled workload | 5,730 events over 630 seconds |
| Peak offered traffic | 25 events/s for 180 seconds |
| Accepted / processed / unique receipts | 5,730 / 5,730 / 5,730 |
| Dropped iterations / request errors | 0 / 0% in every phase |
| Unaccounted / duplicate business effects | 0 / 0 |
| Successful delivery retry attempts | 2, without duplicate effects |
| Peak-phase ingestion p95 | 79.459 ms |
| Worst phase ingestion p95 | 124.266 ms, baseline |
| First observed eight running workers | 119.110 seconds after load start |
| First observed return to desired=running=1, pending=0 | 510.136 seconds |
| Observed minimum-worker recovery suffix | 117.659 seconds before load end |
| Maximum sampled outstanding events | 479 |
| Maximum native source-queue work / oldest-message age | 245 messages / 20 seconds |

These are different measurement streams: sampled database outstanding work and
native queue work need not have identical maxima. First-observed service counts
are not exact task-transition timestamps. The 120-second quiet qualification
threshold produced an observed return about 180 seconds after the recovery
phase began; it is not a 120-second total shutdown deadline or a billing claim.
The run proves eventual exact delivery and bounded backlog/age, not a measured
per-event end-to-end delivery-latency percentile.

Stable drain was confirmed. The
[session journal](../results/aws-sessions/cloud-session-4-20260904T121328Z/session.json)
records all four phases complete, no workflow or cleanup errors, and teardown
verified at **2026-09-04 12:48:29 UTC**. The
[native inventory](../results/aws-sessions/cloud-session-4-20260904T121328Z/aws-native-inventory-after-destroy.json)
contains zero in all 30 tracked categories.

Supported wording:

> In a 10½-minute AWS experiment, async TrackRelay handled traffic rising from
> 1 to 25 events/s, automatically expanded from one to eight workers and returned
> to one, delivered every event, and passed all configured diagnostic guardrails.

Keep `headline_eligible=false` and `comparison_multiplier=null` in the original
diagnostic evidence. They prohibit a comparative headline; they do not negate
this observed standalone elasticity result. Do not relabel the run as paired,
rewrite earlier failed evidence, or infer a 25× architecture improvement from
the 1-to-25 demand swing.

## Can we reuse the synchronous denominator?

**Not for a matched sustainable-throughput claim.** The RDS-backed synchronous
[Stage 9.3 report](../results/aws-sessions/cloud-session-2-20260831T050531Z/vertical-scaling/report/comparison-report.md)
established these short-run boundaries:

| Synchronous machine | Highest passing point | First failing point |
| --- | ---: | ---: |
| `t3.small` | 10/s | 25/s |
| `c7i-flex.large` | 25/s | 50/s |

The [saved definition](../results/aws-sessions/cloud-session-2-20260831T050531Z/vertical-scaling/experiment-definition.json)
shows one 10-second trial per rate, seed 20260806, one preallocated VU per
offered event/s, and a synchronous application image/revision. Its two hardware
treatments shared RDS, application, simulator and driver. The measured 2.5×
hardware-flexibility result remains valid **within that protocol**.

The current async experiment instead uses an ordered rise/fall waveform, a
180-second peak, seed 20260901, five VUs per offered event/s, a different
application revision, two API tasks, separate simulator and one to eight worker
tasks. The synchronous simulator's frozen resource limit was 1 vCPU / 1 GiB;
the current async simulator task is 0.25 vCPU / 0.5 GiB. RDS being present in
both runs does not make the whole environment matched.

Most importantly, a synchronous response includes downstream delivery, whereas
an async acceptance response does not. A shared HTTP p95 <500 ms limit alone
does not match delivery service quality. Nor does successful post-load drain
alone prove that completion kept up while traffic was sustained.

Consequently, do not divide the async 25/s peak by either historical 10/s or
25/s and present the result as a measured architecture multiplier. These old
boundaries can guide the next ladder, not supply its denominator. The earlier
host-local-PostgreSQL migration result and local-machine baseline are also not
substitutes for a matched AWS reference.

## Next work: a separate matched capacity comparison

Defer the fixed-one-worker async treatment for now. It isolates autoscaling's
contribution, while this next study compares the synchronous deployment with
the modernized elastic deployment as a whole. Neither is an equal-cost or
architecture-only causal comparison unless those additional controls are
explicitly established.

Before any new AWS run, implement and freeze a separate machine-readable
capacity contract and local validation. Do not modify the successful v6
waveform, its historical gates, or the paired reporter to accept diagnostic
evidence. Proposed starting choices for the contract review:

1. **Reference:** use the stronger `c7i-flex.large` synchronous configuration
   as the primary reference, avoiding selection of the weaker machine merely
   to enlarge the ratio. Keep `t3.small` as a separately labelled optional
   economical reference, not another required iteration. Freeze application
   identities and explicitly document all topology/resource differences.
2. **Workload:** start with the same `10, 25, 50, 100, 200` events/s ladder on
   both architectures, preserving distinct CREATED shipments and identical
   manifests apart from run namespaces. Use the same driver host/image,
   preallocation, seed, payload, authentication and downstream behavior. Stop
   each architecture at its first failing point; do not assume async passes
   50/s or alter worker maximum/policy between points.
3. **Duration:** propose 180 measured seconds per independent rate, not a full
   630- or 1,260-second elasticity waveform at every point. Freeze an equal,
   bounded premeasurement settling window for both architectures and retain
   its data separately. Reset application/queue state and prove a declared
   starting worker count before each point. Specify timeout behavior rather
   than waiting indefinitely for a favorable steady state. This measures
   short-window supported throughput, not ultimate production capacity.
4. **Completion:** add a shared generated-event-to-simulator-receipt deadline
   and a predeclared completed-throughput/non-growing-backlog gate during the
   measured window. Freeze the numeric delivery deadline, window boundaries
   and clock/timestamp method before executing either side. Separate HTTP
   acceptance latency from delivery latency; native queue age is not a
   substitute for per-event delivery timing. Keep exact reconciliation,
   zero drops/missing arrivals, zero duplicate effects, and correct final
   states mandatory. Post-load drain must not rescue a point that accumulated
   an unsustainable backlog during measurement.
5. **Infrastructure:** match the RDS configuration, simulator capacity and
   behavior, network/region conditions and driver placement as far as the
   two deployment architectures allow. Report remaining differences, API and
   worker resource envelopes, and measured resource usage. A larger elastic
   compute envelope is part of the deployment treatment, not evidence of
   equal-resource efficiency or lower cost.
6. **Reporting and safety:** retain each point's raw request and completion
   evidence, exact measurement windows, failures, environment identities and
   teardown verification. Distinguish measured overload from infrastructure
   or evidence-collection failure. Require both valid runs before emitting a
   ratio; missing evidence or a zero/absent passing denominator means no ratio.
   Label the result a highest-passing-tested-rate ratio under the frozen short
   protocol; a capped ladder is not a measured maximum. Freeze repetition and
   any refinement rules before measurement. Preserve unconditional teardown
   and require fresh session/budget approval.

The delivery deadline and matched deployment configuration are still design
decisions, not values already established by the retained runs. The next local
implementation should make those choices explicit and test the contract,
collector and rejection paths before producing an operator runbook and asking
for cloud approval. No new capacity command or publishable `X×` result exists
as a consequence of this review.
