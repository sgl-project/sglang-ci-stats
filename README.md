# sglang-ci-stats

Auto-scraped per-test elapsed-time history for [`sgl-project/sglang`](https://github.com/sgl-project/sglang)'s `PR Test` workflow.

## What's here

| Path | Contents |
| --- | --- |
| `runs/<YYYY-MM-DD>T<HH-MM-SS>Z__<run_id>.json` | Raw per-run archive. One file per scraped CI run; jobs grow append-only as reruns succeed. |
| `scrape.py` | The scraper. Pulls completed `PR Test` runs on `main` (events: `schedule` + `workflow_dispatch`) within a 48h rolling window and writes `runs/*.json`. No aggregation -- consumers derive their own views. |
| `.github/workflows/scrape.yml` | Auto-runs `scrape.py` every 6h via GitHub Actions and commits any new/augmented `runs/*.json`. |

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
