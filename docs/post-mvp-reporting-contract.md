# Post-MVP paired reporting rules

This records the local reporting change for
[Milestone 1](implementation-plan-2_post-mvp.md). It does not authorize a cloud
run or change workload v6, policy v6, collection, or qualification gates.
The definitions below govern report diagnostics; the complete paired execution
protocol still needs review before a cloud session is proposed.

## Consecutive supported rates

Newly generated reports use `short-plateau-completion-v4`. After applying the
existing per-step support checks, sort the distinct offered rates in ascending
order. A rate passes only when every occurrence passes. Stop at the first
unsupported rate; report the last passing rate before it. Preserve higher
passing steps as diagnostics. If the lowest rate fails, the supported rate is
unavailable, not zero.

For example, if 1/s passes, 5/s fails, and 10/s passes, the reported rate is
1/s. If 1/s fails, there is no supported-rate result even if 25/s passes.
If every rate passes, report the highest tested rate without claiming a measured
maximum. No interpolation or new load points are introduced.

The multiplier divides the elastic consecutive supported rate by the fixed
consecutive supported rate only when both treatments qualify, measurement checks
pass, and both rates are available. Otherwise it remains null. A supported
elasticity finding and a rate improvement remain separate claims.

These short, ordered waveform steps have different histories. This rule does
not turn them into independent steady-state capacity measurements, establish
per-event delivery latency, or produce an async-versus-sync multiplier.

## Historical compatibility and reproduction

Each treatment records `rate_rule`. Missing values in old JSON default to
`all-occurrences`, the existing maximum-of-passing-rates rule. Comparison model
validation requires v4 to use `consecutive` for both treatments and v1–v3 to use
the historical rule. Existing report files and source evidence are not rewritten.

The offline generator and paired session's default reporter use the new rule.
To reproduce the old method in a fresh output directory, run:

```shell
uv run --locked trackrelay-aws-elasticity-report \
  --session-directory results/aws-sessions/SESSION_ID \
  --output-directory results/historical-comparison-UNIQUE_ID \
  --rate-rule all-occurrences
```

Replace the placeholders with a retained paired session and an unused output
path. Diagnostic sessions remain ineligible. Omit `--rate-rule` (or pass
`consecutive`) for v4. Explicitly reanalyzing older paired evidence with v4
creates a differently labelled report; it does not change its original result.
The internal `build_comparison` and `summarize_treatment` helpers retain their
historical defaults; the generator passes its selected rule explicitly.

## Verification

Regression cases cover a failing lower rate with higher passing diagnostics,
failure at the lowest rate, unchanged per-step evidence, withheld multipliers,
method/rule mismatch rejection, old JSON without the new field, and new versus
historical generation from the same saved evidence. Existing reporter/session
tests also exercise rendering, evidence checks, and cleanup behavior.

## Observed timing diagnostics

New consecutive-rate reports include an optional `timings` object with method
`observed-timings-v1`. Old JSON without it remains readable and historical
rate-rule generation leaves it unavailable. These fields do not change
qualification or the rate multiplier.

All times use retained sample timestamps and scheduled waveform boundaries.
For the current workload, the first demand rise starts at 30 seconds, the peak
at 90 seconds, and low-demand recovery at 330 seconds. Demand rise means the
first scheduled rate above baseline, not when an alarm detects demand.

| Measurement | Definition |
| --- | --- |
| Existing first expansion | First observed running count >1 after load starts. Its historical field retains its meaning. |
| Full expansion | First sample after demand rise and before recovery with desired=running=8 and pending=0; report both time after load start and elapsed time from scheduled demand rise. Fixed controls have no full-expansion value. |
| Return after recovery starts | Existing observed return-to-minimum time minus scheduled recovery start. Existing qualification separately establishes the required live suffix and native corroboration. |
| Peak backlog-clear suffix | First zero-outstanding sample in the final zero-outstanding suffix of the peak, after a positive backlog was observed since demand rose. The suffix must span at least 60 seconds and its final sample must be within 30 seconds of peak end. |
| Peak clearance confirmation | First sample at least 60 seconds into that qualifying suffix, measured from peak start. |
| Existing post-load stable drain | First sample in the qualifying empty post-load suffix, followed by confirmation of 180 seconds of stability; both remain relative to actual load end. |

Outstanding means persisted events minus completed deliveries. Peak clearance
does not mean SQS is empty, measure an individual event's waiting time, or prove
zero backlog between samples. If no backlog was observed, clearance is
unavailable rather than an invented zero-duration recovery. A late clearance,
a transient empty sample, or incomplete measurement coverage cannot establish
the peak-clearance result. New full-expansion and peak-clearance observations
are withheld on measurement failures. Current coverage permits gaps up to 30
seconds; no exact transition time is inferred inside those intervals.

The first full-expansion sample is distinct from the existing sustained
expanded-peak qualification. Timings can describe a rejected treatment and must
be read with its rejection reasons. Service counts do not establish when every
container exited or billing ended. A successful post-load drain cannot rescue a
failed load step or establish that backlog cleared during peak demand.
