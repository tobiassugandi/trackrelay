# Paired elasticity readiness audit

Reviewed 2026-09-09 with AWS off for the first step of
[Milestone 1](implementation-plan-2_post-mvp.md). This is a source-code and local
test audit, not a new experiment contract or approval to run AWS.

## Finding

Reuse the existing paired workflow and offline reporter. Most collection,
qualification, plotting, and cleanup machinery already exists. Before a new
run, resolve the rate-summary definition and timing labels below, then freeze
the prospective reporting rules. No qualified matching pair is established by
this audit; the standalone MVP remains the published result.

## What already exists

| Area | Implementation and finding |
| --- | --- |
| Paired workflow | [Session controller](../src/trackrelay/aws_elasticity_session.py), `run_elasticity_session`: fixed qualification → reset → worker-only transition → elastic treatment; cleanup precedes offline reporting. Workflow, cleanup, and report errors are retained. |
| Shared gates | [Fixed/shared collector](../src/trackrelay/aws_fixed_control.py), `evaluate_treatment_guardrails`: complete ingestion, exact reconciliation, stable drain, native evidence, non-worker headroom, and DLQ checks. Fixed qualification additionally requires one worker and observed queue pressure at peak. |
| Current profile | [v6 contract](aws-elasticity-v6-contract.md): 630 seconds, 5,730 events, 25/s peak, five VUs per offered event/s, policy v6 with a 120-second quiet threshold. Historical profiles retain their original rules. |
| Elastic qualification | [Elastic treatment](../src/trackrelay/aws_elastic_treatment.py): expansion, bounded work/age, observed recovery, and native corroboration supplement the shared gates. |
| Evidence integrity | [Reporter](../src/trackrelay/aws_elasticity_report.py), `load_comparison_evidence` and `build_comparison`: reject diagnostic evidence, mismatched workload/run identities, invalid reset/transition chronology, policy drift, and incomplete teardown evidence; recompute qualification. |
| Per-step support | Reporter `_step_results`: completed rate ≥ offered rate, non-growing outstanding work, backlog ≤1,500, oldest age ≤180 seconds, ingestion gates, and complete sampled/native windows. Shared treatment failures invalidate every step. Every occurrence of a repeated rate must pass. |
| Plot and output | Reporter `render_comparison_figure`: aligned four-row, two-treatment PNG/SVG with demand, workers, queue/outstanding work, and p95 plus the 500 ms line. JSON/Markdown retain reasons, unavailable values, and source hashes. |

## Gaps and decisions before the next run

### 1. Highest passing rate is not a consecutive envelope

`summarize_treatment` and `TreatmentReport.require_observed_rate` select the
maximum rate whose occurrences all pass. They do not require lower rates to
pass. Thus a failing 5/s rate followed by passing 10/s and 25/s rates can still
produce a 25/s headline. This matches the older report's stated rule but differs
from the new plan's **highest consecutive passing tested rate**.

Next implementation step: introduce an explicitly versioned prospective report
method that walks distinct offered rates in ascending order and stops at the
first unsupported rate. Retain all per-step outcomes and historical method
semantics. Add a regression case where a lower rate fails and a higher rate
passes, including the no-passing-baseline case. Update summary validation,
multiplier calculation, and prose together.

Even that envelope remains a short-waveform result. Rising and falling phases
have different histories; 30-second steps can qualify from as little as ten
seconds of actual samples. It is not a steady-state capacity measurement.

### 2. Scaling and drain fields need precise meanings

Current report fields have the following meanings:

| Field | Actual meaning |
| --- | --- |
| `scale_out_seconds_after_start` | First sampled running-worker count greater than one, relative to load start; not first observed eight workers or delay from demand detection. |
| `return_to_minimum_seconds_after_start` | Start of the final observed minimum-worker suffix in recovery, relative to load start; full qualification separately checks recovery evidence. |
| `stable_drain_seconds_after_load` | First sample in the qualifying post-load empty suffix, relative to actual load end. |
| `drain_confirmed_seconds_after_load` | Time at which that suffix has established 180 seconds of stability, relative to actual load end. |

The existing drain fields do not measure catch-up while peak traffic continues.
Before adding fields, define separate first-expansion and full-expansion
observations, the demand-change reference point, and what constitutes a
sustained return to low backlog during traffic. Report sampling uncertainty;
do not imply exact task transitions or billing stop times. Preserve current
field meanings for historical reports.

### 3. Fixed overload and invalid evidence are already partly separated

A fixed run can qualify as an experimental control while failing individual
completion/backlog support steps. Per-step overload does not by itself stop the
controller. The fixed run must still deliver and reconcile everything within
the drain contract, preserve ingestion and non-worker headroom, and retain
complete evidence. Failure of those conditions stops the paired workflow.

This distinction supports the intended comparison without relaxing guardrails.
Document it explicitly in the prospective protocol. Peak queue work greater
than zero is only the current controller's pressure check; it does not alone
prove the fixed worker cannot sustain peak demand. Use completion and backlog
evidence for that conclusion. Do not weaken the fixed gate to force a pair.

### 4. Presentation needs completion context

The main aligned figure is already implemented. Its latency row shows
ten-second ingestion p95 and native ALB p95, while acceptance is determined by
full-phase ingestion p95 under v6. Explain that distinction beside the figure.
The report already includes per-step completed throughput; carry that evidence
into the reader-facing comparison so low acceptance latency cannot imply fast
downstream delivery. A per-event delivery-latency percentile remains separate
instrumentation work for Milestone 2.

## Local validation

The focused existing suites for the reporter, paired session, fixed control,
and elastic treatment passed: **158 tests in 23.96 seconds** using
`uv run --locked pytest -q tests/test_aws_elasticity_report.py tests/test_aws_elasticity_session.py tests/test_aws_fixed_control.py tests/test_aws_elastic_treatment.py`.
They cover current-profile comparison generation,
completion versus ingestion, repeated-rate failures, missing evidence, actual
headless rendering, session handoffs, rejected treatments, and cleanup failures.
These are synthetic/local checks, not cloud performance evidence. Future
semantic changes need their own focused regression cases.

## Next action

The versioned consecutive-rate reporting rule is now implemented; see the
[reporting contract](post-mvp-reporting-contract.md). The findings above describe
the implementation at the time of this audit. Next, freeze the remaining timing
definitions and paired protocol, complete any
required local checks, and prepare a concrete session/cost review. Historical
results and the README headline remain unchanged during this preparation.
