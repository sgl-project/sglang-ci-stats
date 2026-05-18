#!/usr/bin/env python3
"""Scrape per-test elapsed times from sglang main-branch CI runs.

Output: one JSON file per archived run in `runs/`, raw form only. No
aggregation here -- consumers are responsible for deriving whatever
view they want from `runs/*.json`.

Layout:

  runs/<YYYY-MM-DD>T<HH-MM-SS>Z__<run_id>.json
      Idempotent at job_id granularity: every invocation re-scans each
      run in the lookback window and appends any newly-successful job
      (e.g. picked up from a "Re-run failed jobs" rerun) that isn't
      already on disk.

Filter:
  - workflow:   .github/workflows/pr-test.yml (matched by path, not display
                name -- sglang renames the name occasionally, e.g. "PR Test"
                -> "PR Test Base" in #25420)
  - branch:     main
  - event:      schedule OR workflow_dispatch
  - run-level:  status=completed (any conclusion)
  - job-level:  conclusion=success
  - parser:     primary = TIMINGS JSONL block (sglang#25232);
                fallback = legacy `filename=..., elapsed=N,` regex with
                keep-last retry dedup, used for pre-merge log format.

Usage:
    GH_TOKEN=... python scrape.py [--max-new-runs 50]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUNS_DIR = REPO_ROOT / "runs"

SOURCE_REPO = "sgl-project/sglang"
WORKFLOW_PATH = ".github/workflows/pr-test.yml"
EVENTS = ("schedule", "workflow_dispatch")

MAX_NEW_RUNS = 50
LOOKBACK_HOURS = 48  # only fetch runs that started within this rolling window
LOG_FETCH_WORKERS = 8  # parallel log downloads per run; IO-bound, well under
                       # GitHub's 5000/hr authenticated API rate-limit

LOG_PATTERN = re.compile(
    r"filename='[^']*?/sglang/((?:test|python)/[^']+\.py)', elapsed=(\d+),"
)

# pr-test.yml's `run-name` template emits "[<stage>] <pr_head_sha>" when
# the slash-command handler dispatches a fork-PR /rerun-stage at main HEAD
# (so the workflow code is trusted) while telling it to check out the PR
# commit. Those runs report head_branch=main but actually test PR code,
# so they must be excluded from main-elapsed statistics.
PR_RERUN_TITLE_RE = re.compile(r"^\[[^\]]+\] [0-9a-f]{40}$")


# ---------- gh api helpers ----------

def gh_api(endpoint, raw=False):
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=not raw, check=True)
    return result.stdout if raw else json.loads(result.stdout)


def get_workflow_id(repo):
    # Match by source-file path, not display name. sglang renames the
    # workflow name from time to time (e.g. "PR Test" -> "PR Test Base"
    # in #25420); the .github/workflows/*.yml path is the stable key.
    # sglang has >100 workflows; paginate so the match isn't id-luck.
    for page in range(1, 11):
        data = gh_api(f"/repos/{repo}/actions/workflows?per_page=100&page={page}")
        if not data["workflows"]:
            break
        for wf in data["workflows"]:
            if wf["path"] == WORKFLOW_PATH:
                return wf["id"]
    raise RuntimeError(f"Workflow '{WORKFLOW_PATH}' not found in {repo}")


def list_recent_runs(repo, workflow_id, lookback_hours=LOOKBACK_HOURS):
    """Union of completed runs across allowed events, within a rolling window.

    GitHub's event= filter only accepts a single value, so query each event
    separately and dedup by run_id. Runs whose started_at predates
    `now - lookback_hours` are dropped (anything older than the operational
    window is "ancient history" and shouldn't be backfilled).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen = {}
    for event in EVENTS:
        data = gh_api(
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
            f"?branch=main&status=completed&event={event}&per_page=100"
        )
        for run in data["workflow_runs"]:
            started = datetime.fromisoformat(
                run["run_started_at"].replace("Z", "+00:00")
            )
            if started < cutoff:
                continue
            if PR_RERUN_TITLE_RE.match(run.get("display_title") or ""):
                continue
            seen.setdefault(run["id"], run)
    return sorted(seen.values(), key=lambda r: r["run_started_at"], reverse=True)


def get_successful_jobs(repo, run_id):
    data = gh_api(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return [j for j in data["jobs"] if j["conclusion"] == "success"]


def job_logs_text(repo, job_id):
    try:
        raw = gh_api(f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True)
    except subprocess.CalledProcessError:
        return ""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


# ---------- per-job parsing ----------

def job_name_to_suite(job_name):
    # Reusable-workflow display: `caller / ... / leaf (N)`. The caller
    # block id right before the leaf is `with.self_name`, which is also
    # consumer's lookup key. Leaf id varies across sglang revisions
    # (was `run`, now back to self_name) so don't trust it.
    base = re.sub(r"\s*\(\d+\)$", "", job_name)
    tokens = base.split(" / ")
    return tokens[-2] if len(tokens) >= 2 else tokens[-1]


def determine_backend(job_name):
    name = job_name.lower()
    for backend in ("cpu", "amd", "npu"):
        if backend in name:
            return backend
    return "cuda"


TIMINGS_BLOCK_RE = re.compile(
    r"=+ TIMINGS BEGIN =+\n(.*?)\n=+ TIMINGS END =+",
    re.DOTALL,
)


def parse_job_timings(log_text):
    """Extract per-file elapsed times from one job's log.

    Primary path: parse the structured TIMINGS block emitted by sglang
    (sgl-project/sglang#25232, merged 2026-05-14). Each line is a JSON
    object with stable keys.

    Fallback path: pre-merge runs have no block; we scan free-form
    `filename=..., elapsed=N,` debug lines and keep-last per file to
    deduplicate per-attempt retry entries.

    Returns list[{file, elapsed, passed?}]; passed only present in
    primary-path output.
    """
    m = TIMINGS_BLOCK_RE.search(log_text)
    if m:
        out = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "file" not in entry:
                continue  # tolerate future summary lines
            out.append(
                {
                    "file": entry["file"],
                    "elapsed": int(entry["elapsed"]),
                    "passed": bool(entry.get("passed", True)),
                }
            )
        return out

    # Fallback: old free-form log format.
    last_seen = {}
    for m in LOG_PATTERN.finditer(log_text):
        last_seen[m.group(1)] = int(m.group(2))
    return [{"file": f, "elapsed": e} for f, e in last_seen.items()]


# ---------- runs/ archive I/O ----------

def find_archive_path(run_id):
    """Return the existing archive Path for run_id, or None."""
    if not RUNS_DIR.exists():
        return None
    for p in RUNS_DIR.glob(f"*__{run_id}.json"):
        return p
    return None


def build_job_record(repo, job):
    """Parse one job's log into our archive shape, or None to skip."""
    if job["name"] == "check-changes" or "health" in job["name"].lower():
        return None
    timings = parse_job_timings(job_logs_text(repo, job["id"]))
    if not timings:
        return None
    return {
        "job_id": job["id"],
        "name": job["name"],
        "suite": job_name_to_suite(job["name"]),
        "backend": determine_backend(job["name"]),
        # L1 inputs for setup-time / per-runner bias analysis.
        # runner_name (specific hostname) deliberately omitted to avoid
        # leaking infra identifiers into a public repo; labels carry the
        # runner type, which is what L1 needs.
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "labels": job.get("labels", []),
        "timings": timings,
    }


def sync_run(repo, run):
    """Ensure one run's successful jobs are persisted to disk.

    Idempotent: if the run is already archived, re-scan and append any
    newly-successful job not yet on disk (e.g. picked up from a "Re-run
    failed jobs" rerun). Returns (record, n_new_jobs).
    """
    run_id = run["id"]
    existing_path = find_archive_path(run_id)
    if existing_path is not None:
        record = json.loads(existing_path.read_text())
        known_job_ids = {j["job_id"] for j in record.get("jobs", [])}
    else:
        record = {
            "run_id": run_id,
            "started_at": run["run_started_at"],
            "event": run["event"],
            "head_sha": run["head_sha"],
            "html_url": run["html_url"],
            "display_title": run.get("display_title", ""),
            "jobs": [],
        }
        known_job_ids = set()

    todo = [
        j for j in get_successful_jobs(repo, run_id)
        if j["id"] not in known_job_ids
    ]
    # Parallelize the per-job log download (the wall-time hot spot:
    # each gh API call is ~1.3s, almost entirely network roundtrip).
    with ThreadPoolExecutor(max_workers=LOG_FETCH_WORKERS) as pool:
        new_jobs = [r for r in pool.map(partial(build_job_record, repo), todo) if r]

    if not new_jobs and existing_path is not None:
        return record, 0

    record["jobs"].extend(new_jobs)
    record["last_scraped_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    RUNS_DIR.mkdir(exist_ok=True)
    if existing_path is None:
        # Filename: runs/<YYYY-MM-DD>T<HH-MM-SS>Z__<run_id>.json
        # Sorts chronologically by glob; trailing run_id disambiguates
        # the rare same-second collision.
        safe_started = run["run_started_at"].replace(":", "-")
        existing_path = RUNS_DIR / f"{safe_started}__{run_id}.json"
    existing_path.write_text(json.dumps(record, indent=2) + "\n")
    return record, len(new_jobs)


# ---------- driver ----------

def sync_runs(repo, max_new_runs, lookback_hours):
    """Sync every candidate run in the lookback window to disk."""
    workflow_id = get_workflow_id(repo)
    runs = list_recent_runs(repo, workflow_id, lookback_hours)[:max_new_runs]
    print(
        f"{len(runs)} candidate runs in last {lookback_hours}h from {repo}",
        file=sys.stderr,
    )

    n_new = n_aug = 0
    for i, run in enumerate(runs, 1):
        existing = find_archive_path(run["id"]) is not None
        _, n_new_jobs = sync_run(repo, run)
        if not existing:
            n_new += 1
            tag = "new"
        elif n_new_jobs > 0:
            n_aug += 1
            tag = f"augmented +{n_new_jobs}"
        else:
            tag = "unchanged"
        print(
            f"({i}/{len(runs)}) {run['id']} {run['run_started_at']} "
            f"event={run['event']} [{tag}]",
            file=sys.stderr,
        )
    print(
        f"summary: {n_new} new, {n_aug} augmented, "
        f"{len(runs) - n_new - n_aug} unchanged",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=SOURCE_REPO)
    parser.add_argument(
        "--max-new-runs",
        type=int,
        default=MAX_NEW_RUNS,
        help="Cap on number of new runs archived per invocation",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=LOOKBACK_HOURS,
        help="Rolling window of run start times to consider",
    )
    args = parser.parse_args()

    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        print(
            "note: GH_TOKEN/GITHUB_TOKEN not set; using gh CLI's own credential.",
            file=sys.stderr,
        )

    sync_runs(args.repo, args.max_new_runs, args.lookback_hours)


if __name__ == "__main__":
    main()
