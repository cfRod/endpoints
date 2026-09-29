#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
"""Run the PoC heterogeneous host-replay experiment.

The experiment keeps setup outside the reported replay metric. It launches:

* one LIGHT trajectory alone with all cores available;
* one MEDIUM trajectory alone with all cores available;
* one HEAVY trajectory alone with all cores available;
* LIGHT + MEDIUM + HEAVY together with all cores available;
* LIGHT + MEDIUM + HEAVY together while sweeping the HEAVY make_jobs knob.

Generated job lists and run outputs are written under the output directory and
are intentionally ignored by git.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


POC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = POC_ROOT.parents[1]

LIGHT = {
    "label": "LIGHT",
    "job_id": "django_16333_light",
    "trajectory": "trajectories/django_16333/host_replay.jsonl",
    "environment": "environments/django_16333_git.json",
    "submitter_config": "submitter_configs/django_16333_default.json",
}
MEDIUM = {
    "label": "MEDIUM",
    "job_id": "pylint_6903_medium",
    "trajectory": "trajectories/pylint_6903/host_replay.jsonl",
    "environment": "environments/pylint_6903_git.json",
    "submitter_config": "submitter_configs/pylint_6903_default.json",
}
HEAVY = {
    "label": "HEAVY",
    "job_id": "protobuf_7cd0b6f_heavy",
    "trajectory": "trajectories/protobuf_7cd0b6f/host_replay.jsonl",
    "environment": "environments/protobuf_7cd0b6f_git.json",
}


def poc_path(path: str) -> str:
    return str((POC_ROOT / path).relative_to(REPO_ROOT))


def job(base: dict[str, str], *, make_jobs: int | None = None) -> dict[str, str]:
    out = {
        "job_id": base["job_id"],
        "trajectory": poc_path(base["trajectory"]),
        "environment": poc_path(base["environment"]),
    }
    if make_jobs is not None:
        out["submitter_config"] = poc_path(
            f"submitter_configs/protobuf_7cd0b6f_make_jobs_{make_jobs}.json"
        )
    else:
        out["submitter_config"] = poc_path(base["submitter_config"])
    return out


def write_job_list(path: Path, suite_id: str, jobs: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_copyright": (
                    "SPDX-FileCopyrightText: Copyright 2026 Arm Limited and/or "
                    "its affiliates <open-source-office@arm.com>"
                ),
                "_license": "SPDX-License-Identifier: Apache-2.0",
                "suite_id": suite_id,
                "jobs": jobs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def replay_latency_s(result: dict[str, Any]) -> float | None:
    summary = result.get("replay_summary") or {}
    latency = summary.get("valid_trajectory_latency_ms") or {}
    p50 = latency.get("p50")
    return None if p50 is None else float(p50) / 1000.0


def replay_window_s(results: list[dict[str, Any]]) -> float:
    durations = []
    for result in results:
        summary = result.get("replay_summary") or {}
        duration = summary.get("measured_duration_s")
        if duration is not None:
            durations.append(float(duration))
    return max(durations) if durations else 0.0


def valid_result(result: dict[str, Any]) -> bool:
    summary = result.get("replay_summary") or {}
    return result.get("status") == "completed" and summary.get("failed_trajectories") == 0


def run_condition(
    *,
    name: str,
    jobs: list[dict[str, str]],
    slots: int,
    output_dir: Path,
    continue_on_failure: bool,
) -> dict[str, Any]:
    job_list = output_dir / "generated_job_lists" / f"{name}.json"
    run_dir = output_dir / "runs" / name
    write_job_list(job_list, name, jobs)

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_host_replay_slots.py"),
        "--jobs",
        str(job_list),
        "--output-dir",
        str(run_dir),
        "--target-concurrency",
        str(slots),
        "--allow-mutating",
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    (run_dir / "runner.log").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0 and not continue_on_failure:
        raise SystemExit(
            f"{name} failed with exit code {completed.returncode}; see {run_dir / 'runner.log'}"
        )

    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    results = summary.get("results", [])
    replay_elapsed = replay_window_s(results)
    valid = sum(1 for item in results if valid_result(item))
    row = {
        "condition": name,
        "slots": slots,
        "jobs": len(jobs),
        "valid": valid,
        "replay_elapsed_s": replay_elapsed,
        "valid_trajectories_per_s": valid / replay_elapsed if replay_elapsed else 0.0,
        "runner_elapsed_s": (summary.get("runner_diagnostics") or {}).get(
            "runner_elapsed_seconds"
        ),
    }
    for item in results:
        label = "UNKNOWN"
        job_id = str(item.get("job_id", ""))
        if "django" in job_id:
            label = "LIGHT"
        elif "pylint" in job_id:
            label = "MEDIUM"
        elif "protobuf" in job_id:
            label = "HEAVY"
        row[f"{label.lower()}_latency_s"] = replay_latency_s(item)
        row[f"{label.lower()}_valid"] = valid_result(item)
    return row


def parse_make_jobs(value: str) -> list[int]:
    jobs = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not jobs:
        raise argparse.ArgumentTypeError("at least one make_jobs value is required")
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=POC_ROOT / "results" / "heterogeneous_experiment",
    )
    parser.add_argument("--all-cores-make-jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--sweep-make-jobs", type=parse_make_jobs, default=parse_make_jobs("8,16,32,64,96"))
    parser.add_argument("--continue-on-failure", action="store_true")
    args = parser.parse_args()

    all_cores = args.all_cores_make_jobs
    conditions = [
        ("single_light_all_cores", [job(LIGHT)], 1),
        ("single_medium_all_cores", [job(MEDIUM)], 1),
        ("single_heavy_all_cores", [job(HEAVY, make_jobs=all_cores)], 1),
        (
            "mixed_all_cores",
            [job(LIGHT), job(MEDIUM), job(HEAVY, make_jobs=all_cores)],
            3,
        ),
    ]
    for make_jobs in args.sweep_make_jobs:
        conditions.append(
            (
                f"mixed_heavy_make_jobs_{make_jobs}",
                [job(LIGHT), job(MEDIUM), job(HEAVY, make_jobs=make_jobs)],
                3,
            )
        )

    args.output_dir = args.output_dir.expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, jobs, slots in conditions:
        print(f"running {name}", flush=True)
        rows.append(
            run_condition(
                name=name,
                jobs=jobs,
                slots=slots,
                output_dir=args.output_dir,
                continue_on_failure=args.continue_on_failure,
            )
        )

    fields = [
        "condition",
        "slots",
        "jobs",
        "valid",
        "replay_elapsed_s",
        "valid_trajectories_per_s",
        "light_latency_s",
        "medium_latency_s",
        "heavy_latency_s",
        "light_valid",
        "medium_valid",
        "heavy_valid",
        "runner_elapsed_s",
    ]
    csv_path = args.output_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
