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
  - workflow:   "PR Test" (nests pr-test-extra.yml + sibling workflows via
                workflow_call, so extra-* leaf jobs land under the same
                run_id; only `_pr-test-stage.yml` callers emit TIMINGS
                blocks, so non-stage nested jobs are auto-filtered by
                the empty-timings check in build_job_record)
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUNS_DIR = REPO_ROOT / "runs"

SOURCE_REPO = "sgl-project/sglang"
# `PR Test` is the only watched workflow because sglang's `pr-test.yml`
# nests `pr-test-extra.yml` (and a few other sibling workflows) as
# reusable workflow_call jobs: schedule cron triggers `PR Test`, which
# pulls extra-* leaf jobs under the same run_id. So extra suites are
# captured here despite `pr-test-extra.yml` itself having no schedule
# trigger. If upstream ever adds a standalone `schedule:` to extra,
# extend this tuple to `("PR Test", "PR Test Extra")` -- the rest of the
# pipeline is already generic over multiple workflows.
WORKFLOW_NAMES = ("PR Test",)
EVENTS = ("schedule", "workflow_dispatch")

MAX_NEW_RUNS = 50
LOOKBACK_HOURS = 48  # only fetch runs that started within this rolling window

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


def get_workflow_ids(repo):
    """Resolve every WORKFLOW_NAMES entry that currently exists in repo.

    sglang has >100 workflows so the workflows endpoint must be paginated
    -- a single un-paginated GET silently finds only id-earliest matches.
    Stop when every WORKFLOW_NAMES entry is matched or pages run out.
    Missing names are not an error (e.g. pr-test-extra.yml may not exist
    on a release branch), but having zero matches is, since it likely
    indicates the workflow was renamed.
    """
    want = set(WORKFLOW_NAMES)
    found = {}
    page = 1
    while want and page <= 10:  # 10 pages * 100 = 1000 workflows, sane cap
        data = gh_api(f"/repos/{repo}/actions/workflows?per_page=100&page={page}")
        batch = data["workflows"]
        if not batch:
            break
        for wf in batch:
            if wf["name"] in want:
                found[wf["name"]] = wf["id"]
                want.discard(wf["name"])
        page += 1
    if not found:
        raise RuntimeError(
            f"None of {WORKFLOW_NAMES} found in {repo}; the workflow may have been renamed"
        )
    # Preserve WORKFLOW_NAMES order for deterministic logs.
    return [(name, found[name]) for name in WORKFLOW_NAMES if name in found]


def list_recent_runs(repo, workflow_ids, lookback_hours=LOOKBACK_HOURS):
    """Union of completed runs across (workflow, event), within a rolling window.

    GitHub's event= filter only accepts a single value, so query each
    (workflow, event) pair separately and dedup by run_id (run_id is
    globally unique across workflows in a repo). Runs whose started_at
    predates `now - lookback_hours` are dropped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen = {}
    for _wf_name, workflow_id in workflow_ids:
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

def job_name_leaf(job_name):
    """Strip reusable-workflow caller prefixes (`a / b / leaf`) and the
    partition `(N)` suffix, leaving the leaf job name as it appears in
    the innermost `_pr-test-stage.yml`. Works for any nesting depth
    (0, 1, 2, ...) because GitHub Actions always puts the leaf last."""
    leaf = job_name.split(" / ")[-1]
    return re.sub(r"\s*\(\d+\)$", "", leaf)


def job_name_to_suite(job_name):
    return job_name_leaf(job_name)


def determine_backend(job_name):
    # Inspect the leaf only -- a caller-block id like "call-amd-stages"
    # would otherwise mislabel every cuda job nested under it.
    name = job_name_leaf(job_name).lower()
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

    new_jobs = []
    for job in get_successful_jobs(repo, run_id):
        if job["id"] in known_job_ids:
            continue
        built = build_job_record(repo, job)
        if built is not None:
            new_jobs.append(built)

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

def sync_runs(repo, max_new_runs):
    """Sync every candidate run in the lookback window to disk."""
    workflow_ids = get_workflow_ids(repo)
    wf_summary = ", ".join(f"{n} (id={i})" for n, i in workflow_ids)
    runs = list_recent_runs(repo, workflow_ids)[:max_new_runs]
    print(
        f"{len(runs)} candidate runs in last {LOOKBACK_HOURS}h "
        f"from {repo} across [{wf_summary}]",
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
    args = parser.parse_args()

    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        print(
            "note: GH_TOKEN/GITHUB_TOKEN not set; using gh CLI's own credential.",
            file=sys.stderr,
        )

    sync_runs(args.repo, args.max_new_runs)


if __name__ == "__main__":
    main()
