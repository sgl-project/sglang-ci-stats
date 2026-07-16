# sglang-ci-stats

Auto-scraped per-test elapsed-time history for [`sgl-project/sglang`](https://github.com/sgl-project/sglang)'s `PR Test` workflow.

## What's here

| Path | Contents |
| --- | --- |
| `runs/<YYYY-MM-DD>T<HH-MM-SS>Z__<run_id>.json` | Per-run archive. One file per scraped CI run; jobs grow append-only as reruns succeed. Each file also carries a `time_stats` block (this run's wall-clock data point). |
| `model.json` | Derived partition model: per-(file, suite, backend) `est` (p90) and per-suite `(coeff, bias, r_squared, method)` fitting `wall_clock = coeff * sum(elapsed) + bias`. OLS is emitted only when identifiable and explanatory (`coeff > 0`, `r_squared >= 0.5`); otherwise the constant-overhead model (`coeff=1.0`, `bias=median(wall - sum_elapsed)`, `method="overhead"`). Deterministic function of `runs/` within the same UTC day. `git log model.json` is the historical archive. |
| `scrape.py` | The scraper. Pulls completed `PR Test` runs on `main` (events: `schedule` + `workflow_dispatch`) within a 48h rolling window and writes `runs/*.json`, each stamped with a per-run `time_stats` summary. No cross-run aggregation. |
| `derive.py` | The deriver. Reads `runs/*.json` and emits one fresh `models/*.json` snapshot per invocation. |
| `demo.py` | Trend viewer. Walks every run's `time_stats` and renders a single-file `demo.html` (Chart.js via CDN) with line charts of CI time over time (total / per-stage / per-runner). The generated `demo.html` is gitignored. |
| `.github/workflows/scrape.yml` | Auto-runs `scrape.py` + `derive.py` every 6h via GitHub Actions and commits any new files. |

## How to consume

Each `runs/<...>.json` looks like:

```json
{
  "run_id": 25792056628,
  "started_at": "2026-05-13T09:59:44Z",
  "event": "schedule",
  "head_sha": "...",
  "jobs": [
    {
      "job_id": 75759852398,
      "name": "stage-c-test-4-gpu-h100 (2)",
      "suite": "stage-c-test-4-gpu-h100",
      "backend": "cuda",
      "started_at": "2026-05-13T10:05:10Z",
      "completed_at": "2026-05-13T10:26:28Z",
      "labels": ["4-gpu-h100"],
      "timings": [
        {"file": "test/registered/foo.py", "elapsed": 120, "passed": true},
        ...
      ]
    }
  ],
  "time_stats": {
    "total_wall_seconds": 42772,
    "per_stage":  {"base-b-test-1-gpu-large": 15504, ...},
    "per_runner": {"1-gpu-h100": 21800, ...}
  }
}
```

`time_stats` is this run's one data point in the time series. `wall-clock`
per job is `completed_at - started_at`; parallel jobs are summed (runner-time
consumed, **not** end-to-end latency). `per_stage` (by suite) and `per_runner`
(by runner type) are two partitions of the same `total_wall_seconds`.

Clone the repo and walk `runs/` directly, or `curl` individual files via `raw.githubusercontent.com`.

### Time trend

```bash
python demo.py --open   # writes demo.html (gitignored) and opens it
```

Line charts of total / per-stage / per-runner CI time across all archived runs.

## Filter / parser

- workflow: `PR Test`
- branch: `main`
- event: `schedule` or `workflow_dispatch` (PR-triggered runs excluded)
- run-level: `status=completed` (any conclusion)
- job-level: `conclusion=success`
- log parser: primary = `TIMINGS BEGIN/END` JSONL block ([sglang#25232](https://github.com/sgl-project/sglang/pull/25232)); fallback = legacy `filename=..., elapsed=N,` regex with keep-last retry dedup

## Updating manually

```bash
gh workflow run scrape.yml -R sgl-project/sglang-ci-stats
```

or trigger from the GitHub Actions UI.
