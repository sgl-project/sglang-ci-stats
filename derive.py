#!/usr/bin/env python3
"""Derive a partition prediction model from runs/*.json.

Output: model.json (always overwritten; `git log model.json` is the archive).

    pred_shard_wall_clock = sum(est[file] for file in shard) * coeff + bias

Where:

  est        per (file, suite, backend), p90 of recent passing elapsed.
  coeff      per suite, OLS slope of wall_clock vs sum_elapsed.
             (Each sglang suite is 1:1 with one runner pool, so the
             extra runner dimension is redundant.)
  bias       per suite, OLS intercept (~ shard setup overhead).
  r_squared  diagnostic for whether the linear fit is meaningful.

The output is a deterministic function of (runs/, code, fit window).
`data_as_of` is pinned to UTC midnight (not wall-clock now) so two
invocations on the same UTC day with unchanged runs/ produce
byte-identical JSON -- the workflow can simply diff & commit.

Consumers should fall back to coeff=1.0, bias=0 (and the in-source
static est_time) whenever a lookup is missing or r_squared is too low.

Usage:
    python derive.py [--fit-window-days 7]
"""

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUNS_DIR = REPO_ROOT / "runs"

MIN_EST_SAMPLES = 3
MIN_FIT_SAMPLES = 3
MAX_SAMPLES = 16  # newest-first cap per bin; tracks ~one weekly rotation


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_runs(cutoff):
    """Load runs/*.json with started_at >= cutoff, newest first."""
    if not RUNS_DIR.exists():
        return []
    out = []
    for p in RUNS_DIR.glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if parse_iso(rec["started_at"]) < cutoff:
            continue
        out.append(rec)
    out.sort(key=lambda r: r["started_at"], reverse=True)
    return out


def collect_est_samples(records):
    """Per (file, suite, backend) -> list of passed elapsed values.

    Failed-attempt entries (`passed=False`) are excluded so timeout-
    capped numbers don't pollute p90. Old-format entries default to True.
    """
    bins = defaultdict(list)
    for r in records:
        for job in r["jobs"]:
            for t in job["timings"]:
                if not t.get("passed", True):
                    continue
                bins[(t["file"], job["suite"], job["backend"])].append(t["elapsed"])
    return bins


def collect_fit_samples(records):
    """Per suite -> list of (sum_elapsed, wall_clock_seconds).

    sum_elapsed includes every per-file elapsed in the job (passed or not).
    Bias will absorb any per-file retry overhead the post-retry dedup hid.
    """
    bins = defaultdict(list)
    for r in records:
        for job in r["jobs"]:
            started, completed = job.get("started_at"), job.get("completed_at")
            if not started or not completed:
                continue
            wall = (parse_iso(completed) - parse_iso(started)).total_seconds()
            sum_elapsed = sum(t["elapsed"] for t in job["timings"])
            if sum_elapsed <= 0:
                continue
            bins[job["suite"]].append((sum_elapsed, wall))
    return bins


def ols_fit(samples):
    """Single-variable OLS: returns {coeff, bias, r_squared, n_samples} or None."""
    n = len(samples)
    if n < MIN_FIT_SAMPLES:
        return None
    xs = [s[0] for s in samples]
    ys = [s[1] for s in samples]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return None
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    coeff = ss_xy / ss_xx
    bias = mean_y - coeff * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (coeff * x + bias)) ** 2 for x, y in zip(xs, ys))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {
        "coeff": round(coeff, 4),
        "bias": round(bias, 1),
        "r_squared": round(r_squared, 4),
        "n_samples": n,
    }


def p90(values):
    return round(statistics.quantiles(values, n=10, method="inclusive")[8])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fit-window-days",
        type=int,
        default=7,
        help="Only use runs whose started_at falls within the last N days",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "model.json"),
        help="Destination path for the model snapshot",
    )
    args = parser.parse_args()

    # Pin cutoff to UTC midnight (not wall-clock now) so the model is a
    # deterministic function of runs/ within the same UTC day -- two
    # consecutive cron ticks produce identical output and the commit step
    # naturally no-ops.
    now = datetime.now(timezone.utc)
    today_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today_utc - timedelta(days=args.fit_window_days)
    records = load_runs(cutoff)

    # Bins arrive newest-first (load_runs sorts that way). Slicing to
    # MAX_SAMPLES then keeps only the most recent samples per key.
    # Output shape: {suite: {file: p90}}. Backend is dropped because each
    # sglang suite is 1:1 with one backend (e.g. stage-c-test-4-gpu-h100
    # is always cuda), so the dimension is redundant for lookup.
    est_bins = collect_est_samples(records)
    est_by_suite = defaultdict(dict)
    for (file, suite, _backend), values in est_bins.items():
        recent = values[:MAX_SAMPLES]
        if len(recent) < MIN_EST_SAMPLES:
            continue
        est_by_suite[suite][file] = p90(recent)
    est = {
        suite: dict(sorted(est_by_suite[suite].items()))
        for suite in sorted(est_by_suite)
    }

    fit_bins = collect_fit_samples(records)
    fit = {}
    for suite, samples in sorted(fit_bins.items()):
        recent = samples[:MAX_SAMPLES]
        f = ols_fit(recent)
        if f is not None:
            fit[suite] = f

    payload = {
        "version": 1,
        "data_as_of": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fit_window_days": args.fit_window_days,
        "n_runs": len(records),
        "est": est,
        "fit": fit,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    n_est_leaves = sum(len(v) for v in est.values())
    print(
        f"wrote {out_path}: "
        f"{len(records)} runs -> "
        f"{n_est_leaves} est entries across {len(est)} suites, "
        f"{len(fit)} fit entries"
    )


if __name__ == "__main__":
    main()
