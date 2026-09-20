#!/usr/bin/env python3
"""Render runs/*.json into a single-file HTML trend page (Chart.js via CDN).

Three line charts -- total (split per pr-test.yml caller) / per-stage /
per-runner CI time over time. Runs
predating scrape.py's time_stats field are summarized on the fly via
scrape.summarize_run_times, so this works against any runs/ snapshot.

Usage: python demo.py [--out demo.html] [--open]
"""

import argparse
import json
import statistics
import webbrowser
from collections import defaultdict
from pathlib import Path

import scrape

REPO_ROOT = Path(__file__).resolve().parent
RUNS_DIR = REPO_ROOT / "runs"

SECONDS_PER_MIN = 60.0
FORWARD_FILL_MAX_GAP = 3  # runs; ~18-24h at the ~6-8h scrape cadence

# A run keeping fewer than this fraction of its neighbours' median archived-job
# count didn't complete a full pass. The observed split is clean: partial runs
# sit at ratio <= 0.27, healthy ones at >= 0.85, so 0.5 lands in empty space.
MIN_JOB_RATIO = 0.5
COMPLETENESS_WINDOW = 10  # runs looked at on each side for the median


def forward_filled_totals(records, entries_of):
    """Per-group forward-filled sums, one {group: total} per run.

    Only successful jobs are archived, so whatever a failed one would have
    contributed vanishes from run T; reusing its last seen value keeps that
    from reading as a speedup.
    """
    last_idx = defaultdict(dict)    # last_idx[group][item] = run index
    last_val = defaultdict(dict)    # last_val[group][item] = value
    per_run_totals = []
    for i, rec in enumerate(records):
        groups_here = set()
        for group, item, value in entries_of(rec):
            groups_here.add(group)
            last_idx[group][item] = i
            last_val[group][item] = value
        per_run_totals.append({
            g: sum(
                v for item, v in last_val[g].items()
                if i - last_idx[g][item] < FORWARD_FILL_MAX_GAP
            )
            for g in groups_here
        })
    return per_run_totals


def stage_group(suite):
    """Which part of pr-test.yml a suite's time belongs to.

    pr-test.yml runs its own base-* stages and calls pr-test-extra.yml,
    plus one workflow per specialized suite, so one run's per_stage spans them all.
    An unrecognized suite becomes its own group rather than joining a catch-all,
    so a new caller shows up as a new line instead of inflating someone else's.
    """
    if suite.startswith("base-"):
        return "base"
    if suite.startswith("extra-"):
        return "extra"
    if suite.startswith("call-"):
        return suite[len("call-"):]
    if suite.startswith("stage-"):
        return "legacy-stage"  # pre-2026-05-15 naming, predates the base/extra split
    return suite


def stage_split(per_stage):
    """{group: minutes} over per_stage -- a partition, so the parts sum to total."""
    totals = defaultdict(float)
    for suite, seconds in per_stage.items():
        totals[stage_group(suite)] += seconds
    return {g: round(v / SECONDS_PER_MIN, 1) for g, v in totals.items()}


def stage_test_entries(record):
    """(suite, test-file basename, elapsed) per archived timing.

    Keyed on basename because sglang moves files between dirs but keeps the
    name; path keying would double-count across a move.
    """
    for job in record.get("jobs", []):
        for timing in job["timings"]:
            yield job["suite"], timing["file"].rsplit("/", 1)[-1], timing["elapsed"]


def runner_wall_entries(record):
    """(runner type, job name, wall-clock seconds) per archived job."""
    for job in record.get("jobs", []):
        started, completed = job.get("started_at"), job.get("completed_at")
        if not started or not completed:
            continue
        wall = (scrape.parse_iso(completed) - scrape.parse_iso(started)).total_seconds()
        yield scrape.runner_type(job.get("labels", [])), job["name"], wall


def drop_partial_runs(records):
    """Drop cancelled / infra-mass-failed runs, whose totals collapse toward zero.

    Judged by job count, not `conclusion`: `failure` is the steady state for a
    complete pass, and `cancelled` can land after every job already finished.
    Local median because the job count grows 44 -> 66 over the archive.
    """
    counts = [len(r["jobs"]) for r in records]
    kept = []
    for i, rec in enumerate(records):
        window = (
            counts[max(0, i - COMPLETENESS_WINDOW):i]
            + counts[i + 1:i + 1 + COMPLETENESS_WINDOW]
        )
        median = statistics.median(window) if window else 0
        if median and counts[i] / median < MIN_JOB_RATIO:
            continue
        kept.append(rec)
    return kept, len(records) - len(kept)


def load_points():
    """One sorted-by-time point per run: {started_at, label, total_min, per_*}."""
    records = sorted(
        (json.loads(p.read_text()) for p in RUNS_DIR.glob("*.json")),
        key=lambda r: r["started_at"],
    )
    records, n_dropped = drop_partial_runs(records)
    stage_test_per_run = forward_filled_totals(records, stage_test_entries)
    runner_wall_per_run = forward_filled_totals(records, runner_wall_entries)

    points = []
    for rec, stage_test_secs, runner_secs in zip(
        records, stage_test_per_run, runner_wall_per_run
    ):
        ts = rec.get("time_stats") or scrape.summarize_run_times(rec)
        started = rec["started_at"]
        points.append(
            {
                "started_at": started,
                # "MM-DD HH:MM" -- reads cleanly on a category axis (no date adapter)
                "label": started[5:16].replace("T", " "),
                # total stays raw: it is the one number that must not be inflated
                # by fill, since a partial run is already excluded outright.
                "total_min": round(ts["total_wall_seconds"] / SECONDS_PER_MIN, 1),
                # same raw source as total_min, so the groups sum back to it
                "split": stage_split(ts["per_stage"]),
                "per_runner": {
                    k: round(v / SECONDS_PER_MIN, 1)
                    for k, v in runner_secs.items()
                },
                "stage_test": {
                    k: round(v / SECONDS_PER_MIN, 1)
                    for k, v in stage_test_secs.items()
                },
            }
        )
    return points, n_dropped


def series_for(points, field):
    """Build {key: [aligned values, None where the run lacks that key]}."""
    keys = sorted({k for pt in points for k in pt[field]})
    return {key: [pt[field].get(key) for pt in points] for key in keys}


def stage_family(suite):
    """Stage family = the part before "-test" ("base-c-test-deepep..." -> "base-c")."""
    return suite.split("-test")[0]


def family_series(points):
    """{family: {short_suite: aligned col}} -- one chart per base-*/extra-* family,
    one line per suite within it. Short label drops the "<family>-test-" prefix.
    Legacy stage-a/b/c suites (renamed ~2026-05-15) are dropped.
    """
    suites = sorted({s for pt in points for s in pt["stage_test"]})
    families = {}
    for suite in suites:
        fam = stage_family(suite)
        if fam.startswith("stage-"):
            continue
        short = suite.split("-test-", 1)[1] if "-test-" in suite else suite
        col = [pt["stage_test"].get(suite) for pt in points]
        families.setdefault(fam, {})[short] = col
    return dict(sorted(families.items()))


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sglang CI time trend</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 32px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .card {{ background: #fff; border: 1px solid #e2e2e2; border-radius: 8px;
           padding: 16px; margin-bottom: 24px; }}
  canvas {{ max-height: 420px; }}
  .stage-wrap {{ display: flex; gap: 16px; }}
  .catlist {{ flex: 0 0 150px; }}
  .cat {{ padding: 8px 10px; border-radius: 6px; cursor: pointer;
          color: #444; font-size: 14px; }}
  .cat:hover {{ background: #f0f0f0; }}
  .cat.active {{ background: #2563eb; color: #fff; }}
  .chart-area {{ flex: 1; min-width: 0; }}
</style>
</head>
<body>
<h1>sglang CI time consumption over time</h1>
<div class="meta">{n_runs} runs &middot; {first} &rarr; {last} &middot;
  wall-clock = sum of per-job (completed - started), i.e. runner-time, not
  end-to-end latency &middot; click a legend entry to toggle a series<br>
  {n_dropped} partial runs excluded (cancelled, or mass-failed on infra:
  fewer than half the neighbouring median of successful jobs)</div>

<div class="card"><h2>Total runner-time per run &mdash; wall-clock (min)</h2>
  <div class="meta">split by the part of pr-test.yml that owns the job: its own
    <code>base-*</code> stages, the called <code>pr-test-extra.yml</code>, and one
    group per specialized called workflow. The groups partition the total.</div>
  <canvas id="total"></canvas></div>
<div class="card">
  <h2>Per stage family, by gpu type &mdash; test time (min)</h2>
  <div class="meta">sum of per-file elapsed; missing shards filled with
    last-seen value of the same basename, evicted after 3 runs of absence</div>
  <div class="stage-wrap">
    <div id="catlist" class="catlist"></div>
    <div class="chart-area"><canvas id="stage-chart"></canvas></div>
  </div>
</div>
<div class="card"><h2>Per typed runner &mdash; wall-clock (min)</h2>
  <div class="meta">sum of per-job wall-clock; jobs missing from a run filled
    with last-seen value of the same job name, evicted after 3 runs of
    absence</div>
  <canvas id="runner"></canvas></div>

<script>
const DATA = {data_json};

function color(i, n) {{
  const h = Math.round((360 * i) / Math.max(n, 1));
  return `hsl(${{h}}, 65%, 50%)`;
}}

function lineChart(canvasId, labels, seriesMap) {{
  const keys = Object.keys(seriesMap);
  const datasets = keys.map((k, i) => ({{
    label: k,
    data: seriesMap[k],
    // "total" is the sum of its neighbours, so draw it apart from the ramp
    borderColor: k === "total" ? "#111" : color(i - 1, keys.length - 1),
    backgroundColor: "transparent",
    borderWidth: k === "total" ? 3 : 2,
    pointRadius: 2,
    spanGaps: false,
    tension: 0.2,
  }}));
  return new Chart(document.getElementById(canvasId), {{
    type: "line",
    data: {{ labels, datasets }},
    options: {{
      responsive: true,
      interaction: {{ mode: "nearest", intersect: false }},
      scales: {{ y: {{ beginAtZero: true, title: {{ display: true, text: "minutes" }} }} }},
      plugins: {{ legend: {{ position: "bottom" }} }},
    }},
  }});
}}

lineChart("total", DATA.labels, DATA.total);
lineChart("runner", DATA.labels, DATA.per_runner);

// Per-stage: left category list switches the single right-hand chart.
let stageChart = null;
function selectFamily(fam) {{
  document.querySelectorAll(".cat").forEach(
    (el) => el.classList.toggle("active", el.dataset.fam === fam)
  );
  if (stageChart) stageChart.destroy();
  stageChart = lineChart("stage-chart", DATA.labels, DATA.families[fam]);
}}

const catlist = document.getElementById("catlist");
Object.keys(DATA.families).forEach((fam) => {{
  const el = document.createElement("div");
  el.className = "cat";
  el.dataset.fam = fam;
  el.textContent = fam;
  el.addEventListener("click", () => selectFamily(fam));
  catlist.appendChild(el);
}});
selectFamily(Object.keys(DATA.families)[0]);
</script>
</body>
</html>
"""


def render(points, n_dropped=0):
    data = {
        "labels": [pt["label"] for pt in points],
        "total": {
            "total": [pt["total_min"] for pt in points],
            **series_for(points, "split"),
        },
        "families": family_series(points),
        "per_runner": series_for(points, "per_runner"),
    }
    # Escape "</" so a "</script>" in any path can't break out of the script block
    data_json = json.dumps(data).replace("</", "<\\/")
    return HTML_TEMPLATE.format(
        n_runs=len(points),
        n_dropped=n_dropped,
        first=points[0]["started_at"] if points else "-",
        last=points[-1]["started_at"] if points else "-",
        data_json=data_json,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "demo.html"))
    parser.add_argument("--open", action="store_true", help="open in browser")
    args = parser.parse_args()

    points, n_dropped = load_points()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(points, n_dropped))
    print(f"wrote {out_path}: {len(points)} runs, {n_dropped} partial dropped")
    if args.open:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
