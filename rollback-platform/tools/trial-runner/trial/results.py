"""Stores each trial: the full timeline + measurements as JSON, and one CSV row."""

import csv
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"

COLUMNS = [
    "run_id", "started_at", "git_commit", "app", "strategy", "fault", "image_tag", "rep",
    "outcome", "caught_by", "ttd_s", "detect_from_start_s", "ttr_s",
    "max_exposure_pct", "user_exposed_s",
    "user_requests", "user_5xx", "user_slow",
    "mirrored_requests", "mirrored_5xx", "smoke_requests", "smoke_5xx",
    "release_duration_s", "trial_pod_minutes", "notes",
]


def save(run_id: str, info: dict, timeline: dict, m: dict) -> Path:
    (RESULTS / "timelines").mkdir(parents=True, exist_ok=True)
    path = RESULTS / "timelines" / f"{run_id}.json"
    path.write_text(json.dumps({"run_id": run_id, "info": info, "measurements": m,
                                "timeline": timeline}, indent=1))

    row = {
        "run_id": run_id, **info, **{k: m.get(k) for k in COLUMNS if k in m},
        "user_requests": m.get("user", {}).get("total"),
        "user_5xx": m.get("user", {}).get("5xx"),
        "user_slow": m.get("user", {}).get("slow"),
        "mirrored_requests": m.get("mirrored", {}).get("total"),
        "mirrored_5xx": m.get("mirrored", {}).get("5xx"),
        "smoke_requests": m.get("smoke", {}).get("total"),
        "smoke_5xx": m.get("smoke", {}).get("5xx"),
    }
    # Rewrite the CSV with this run's row replaced (or appended), so re-measuring
    # a run never leaves two rows for it.
    csv_path = RESULTS / "trials.csv"
    rows = []
    if csv_path.exists():
        with csv_path.open() as f:
            rows = [r for r in csv.DictReader(f) if r["run_id"] != run_id]
    rows.append(row)
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def done_run_ids() -> set[str]:
    """Run IDs already in the CSV, so a restarted matrix can skip them."""
    csv_path = RESULTS / "trials.csv"
    if not csv_path.exists():
        return set()
    with csv_path.open() as f:
        return {r["run_id"] for r in csv.DictReader(f)}
