# Legacy local baseline v1

The maximum sustainable offered load is **250 events/s**. The first failing point was **500 events/s**: k6 reported a threshold or request-check failure; observed request count did not match the scheduled count; k6 dropped 2324 iterations; p95 response latency reached or exceeded 500 ms; incorrect final shipment states were nonzero.

![Latency versus offered load](latency-vs-load.png)

## Result

| Offered events/s | Observed requests/s | p95 latency | Error rate | Dropped | Unaccounted | Complete gate |
| ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| 10 | 10.0 | 43.0 | 0.00% | 0 | 0 | pass |
| 25 | 25.0 | 33.5 | 0.00% | 0 | 0 | pass |
| 50 | 50.0 | 20.5 | 0.00% | 0 | 0 | pass |
| 100 | 99.9 | 14.9 | 0.00% | 0 | 0 | pass |
| 250 | 249.8 | 60.1 | 0.00% | 0 | 0 | pass |
| 500 | 243.4 | 1506.4 | 0.00% | 2324 | 0 | fail |

A rate passes only when p95 latency is below 500 ms, request errors are below 1%, no scheduled requests are missing or dropped, and every reconciliation invariant passes. Capacity is the last consecutive passing rate before the first failure; a later passing diagnostic point cannot reopen the envelope.

## Environment

- Captured: `2026-08-20T13:55:51.048356+00:00`
- Operating system: `macOS-26.5.2-arm64-arm-64bit`
- Architecture: `arm64`
- Processor: `Apple M3 Pro`
- Logical CPUs: `11`
- Memory: `18.0 GiB`
- Python: `3.12.12`
- Database: `postgresql+psycopg://trackrelay:***@localhost:5433/trackrelay_baseline_final`
- Database server: `PostgreSQL 17 (postgres:17-alpine)`
- k6 image: `grafana/k6:2.1.0`
- API processes: `1`
- Downstream simulator processes: `1`
- API access log enabled: `False`
- Downstream access log enabled: `False`
- Workload shape: `one-created-event-per-shipment`
- Tier duration: `10` seconds
- Random seed: `20260806`

This result describes this fixed local environment; it is not normalized into a claim about other hardware. `benchmark-definition.json`, `summary.json`, and `ramp-results.csv` are the compact machine-readable evidence. Bulky per-run manifests, k6 summaries, runtime samples, and reconciliation details remain under the ignored raw-evidence paths recorded in the CSV and summary.
