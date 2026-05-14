# sglang-ci-stats

Auto-scraped per-test elapsed-time history for [`sgl-project/sglang`](https://github.com/sgl-project/sglang)'s `PR Test` workflow.

## What's here

| Path | Contents |
| --- | --- |
| `runs/<YYYY-MM-DD>T<HH-MM-SS>Z__<run_id>.json` | Raw per-run archive. One file per scraped CI run; jobs grow append-only as reruns succeed. |
| `model.json` | Derived partition model: per-(file, suite, backend) `est` (p90) and per-suite OLS `(coeff, bias, r_squared)` fitting `wall_clock = coeff * sum(elapsed) + bias`. Deterministic function of `runs/` within the same UTC day. `git log model.json` is the historical archive. |
| `scrape.py` | The scraper. Pulls completed `PR Test` + `PR Test Extra` runs on `main` (events: `schedule` + `workflow_dispatch`) within a 48h rolling window and writes `runs/*.json`. No aggregation. |
| `derive.py` | The deriver. Reads `runs/*.json` and emits one fresh `models/*.json` snapshot per invocation. |
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
  ]
}
```

Clone the repo and walk `runs/` directly, or `curl` individual files via `raw.githubusercontent.com`.

## Filter / parser

- workflow: `PR Test` or `PR Test Extra` (label-gated nightly-class pipeline added in [sglang#24725](https://github.com/sgl-project/sglang/pull/24725); contributes 0 runs until upstream adds a `schedule:` trigger, then automatically starts populating the same `runs/` archive — `extra-a-*` / `extra-b-*` suites get their own keys in `model.json`)
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
