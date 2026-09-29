#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
"""Run host replay jobs with endpoint-style active trajectory slots.

This is the PoC scheduler that matches the original multi-turn endpoint model:

    slot 0 -> trajectory A setup -> replay -> done -> trajectory D setup -> replay
    slot 1 -> trajectory B setup -> replay -> done -> trajectory E setup -> replay

Each job owns its setup/materialization and mutable workspace.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import queue
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from materialize_host_environment import load_json, materialize
from run_single_host_replay_agent import source_cwd


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "job"


def load_jobs(path: Path) -> tuple[str, list[dict[str, Any]]]:
    suite = load_json(path)
    suite_id = str(suite.get("suite_id", path.stem))
    jobs = suite.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"{path} must contain a non-empty jobs list")
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"job {index} must be an object")
        for key in ("trajectory", "environment", "submitter_config"):
            if key not in job:
                raise ValueError(f"job {index} missing required key {key!r}")
    return suite_id, jobs


def resolve_path(path_value: str, repo_root: Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def replay_environment(resolved_config: dict[str, object]) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in resolved_config.items():
        env[f"MLPERF_{key.upper()}"] = str(value)
    return env


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile_value / 100.0
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def replay_summary_class(replay_summary: dict[str, Any] | None) -> str:
    if not replay_summary:
        return "UNSPECIFIED"
    by_class = replay_summary.get("trajectory_latency_by_class_ms")
    if not isinstance(by_class, dict) or not by_class:
        return "UNSPECIFIED"
    return sorted(str(name) for name in by_class)[0]


def replay_valid_latency_ms(replay_summary: dict[str, Any] | None) -> float | None:
    if not replay_summary:
        return None
    latency = replay_summary.get("valid_trajectory_latency_ms")
    if not isinstance(latency, dict):
        return None
    p50 = latency.get("p50")
    return float(p50) if p50 is not None else None


def replay_measured_duration_s(replay_summary: dict[str, Any] | None) -> float | None:
    if not replay_summary:
        return None
    duration = replay_summary.get("measured_duration_s")
    return float(duration) if duration is not None else None


def replay_count(replay_summary: dict[str, Any] | None, key: str) -> int:
    if not replay_summary:
        return 0
    value = replay_summary.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def run_job(
    *,
    job: dict[str, Any],
    job_index: int,
    slot_id: int,
    output_dir: Path,
    repo_root: Path,
    allow_mutating: bool,
    latency_targets_ms: list[float],
) -> dict[str, Any]:
    job_id = str(job.get("job_id") or f"job_{job_index:04d}")
    job_root = output_dir / "slots" / f"slot_{slot_id:03d}" / f"{job_index:04d}_{_safe_name(job_id)}"
    setup_root = job_root / "setup"
    replay_out = job_root / "replay"
    environment_path = resolve_path(str(job["environment"]), repo_root)
    submitter_path = resolve_path(str(job["submitter_config"]), repo_root)
    trajectory_path = resolve_path(str(job["trajectory"]), repo_root)

    started = time.monotonic()
    status = "completed"
    error: str | None = None
    manifest: dict[str, Any] | None = None

    try:
        manifest = materialize(
            environment_path=environment_path,
            submitter_path=submitter_path,
            output_root=setup_root,
            repo_root=repo_root,
        )
        recorded_cwd = source_cwd(environment_path, repo_root)
        active_workspace = Path(str(manifest["workspace"])).resolve()
        command = [
            sys.executable,
            str(repo_root / "scripts" / "agentic_host_tool_replay.py"),
            "--input",
            str(trajectory_path),
            "--output-dir",
            str(replay_out),
            "--target-concurrency",
            "1",
            "--cwd-map",
            f"{recorded_cwd}={active_workspace}",
        ]
        if allow_mutating:
            command.append("--allow-mutating")
        for target_ms in latency_targets_ms:
            command.extend(["--latency-target-ms", str(target_ms)])
        subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            env=replay_environment(dict(manifest.get("resolved_config", {}))),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        replay_summary_path = replay_out / "summary.json"
        replay_summary = (
            load_json(replay_summary_path) if replay_summary_path.exists() else None
        )
    except Exception as exc:  # noqa: BLE001 - benchmark harness records failures.
        status = "failed"
        error = str(exc)
        replay_summary = None

    elapsed = time.monotonic() - started
    result = {
        "job_id": job_id,
        "job_index": job_index,
        "slot_id": slot_id,
        "status": status,
        "error": error,
        "elapsed_seconds": round(elapsed, 6),
        "trajectory": str(trajectory_path),
        "environment": str(environment_path),
        "submitter_config": str(submitter_path),
        "job_root": str(job_root),
        "setup_manifest": manifest,
        "replay_output": str(replay_out),
        "replay_summary": replay_summary,
    }
    job_root.mkdir(parents=True, exist_ok=True)
    (job_root / "job_result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--target-concurrency", type=int, default=1)
    parser.add_argument("--repo-root", default=Path.cwd(), type=Path)
    parser.add_argument("--allow-mutating", action="store_true")
    parser.add_argument(
        "--latency-target-ms",
        action="append",
        default=[],
        type=float,
        help="Latency target used to count completed valid trajectories.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    suite_id, jobs = load_jobs(resolve_path(str(args.jobs), repo_root))
    workers = max(1, min(args.target_concurrency, len(jobs)))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    work_queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
    for index, job in enumerate(jobs):
        work_queue.put((index, job))

    def run_slot(slot_id: int) -> list[dict[str, Any]]:
        slot_results: list[dict[str, Any]] = []
        while True:
            try:
                index, job = work_queue.get_nowait()
            except queue.Empty:
                return slot_results
            try:
                slot_results.append(
                    run_job(
                        job=job,
                        job_index=index,
                        slot_id=slot_id,
                        output_dir=args.output_dir,
                        repo_root=repo_root,
                        allow_mutating=args.allow_mutating,
                        latency_targets_ms=args.latency_target_ms,
                    )
                )
            finally:
                work_queue.task_done()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(run_slot, slot_id) for slot_id in range(workers)]
        for future in concurrent.futures.as_completed(futures):
            results.extend(future.result())

    elapsed = time.monotonic() - started
    results.sort(key=lambda item: item["job_index"])
    completed_valid = [
        item
        for item in results
        if item["status"] == "completed"
        and (item.get("replay_summary") or {}).get("failed_trajectories") == 0
    ]
    valid_latencies_ms = [
        item["elapsed_seconds"] * 1000.0
        for item in completed_valid
    ]
    replay_valid_latencies_ms = [
        latency_ms
        for item in completed_valid
        if (latency_ms := replay_valid_latency_ms(item.get("replay_summary"))) is not None
    ]
    replay_durations_s = [
        duration_s
        for item in completed_valid
        if (duration_s := replay_measured_duration_s(item.get("replay_summary"))) is not None
    ]
    replay_window_s = max(replay_durations_s) if replay_durations_s else None
    replay_valid_completed = sum(
        replay_count(item.get("replay_summary"), "valid_completed_trajectories")
        for item in results
    )
    replay_completed = sum(
        replay_count(item.get("replay_summary"), "completed_trajectories")
        for item in results
    )
    replay_failed = sum(
        replay_count(item.get("replay_summary"), "failed_trajectories")
        for item in results
    )
    replay_p95_latency_ms = percentile(replay_valid_latencies_ms, 95)
    results_by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        results_by_class[replay_summary_class(item.get("replay_summary"))].append(item)
    job_p95_latency_ms = percentile(valid_latencies_ms, 95)
    replay_latency_targets: dict[str, int] = {
        str(target): 0 for target in args.latency_target_ms
    }
    for item in results:
        replay_summary = item.get("replay_summary") or {}
        by_target = replay_summary.get("completed_valid_trajectories_by_latency_target")
        if not isinstance(by_target, dict):
            continue
        for target in args.latency_target_ms:
            replay_latency_targets[str(target)] += int(by_target.get(str(target), 0))

    summary = {
        "suite_id": suite_id,
        "target_concurrency": args.target_concurrency,
        "active_slots": workers,
        "jobs": len(jobs),
        "completed_jobs": sum(1 for item in results if item["status"] == "completed"),
        "failed_jobs": sum(1 for item in results if item["status"] == "failed"),
        "runner_diagnostics": {
            "runner_elapsed_seconds": round(elapsed, 6),
            "job_wall_latency_ms": {
                "p50": percentile(valid_latencies_ms, 50),
                "p90": percentile(valid_latencies_ms, 90),
                "p95": job_p95_latency_ms,
                "p99": percentile(valid_latencies_ms, 99),
            },
            "job_wall_latency_by_class_ms": {
                trajectory_class: {
                    "count": len(items),
                    "valid": sum(
                        1
                        for item in items
                        if item["status"] == "completed"
                        and (item.get("replay_summary") or {}).get("failed_trajectories")
                        == 0
                    ),
                    "p50": percentile(
                        [
                            item["elapsed_seconds"] * 1000.0
                            for item in items
                            if item["status"] == "completed"
                            and (item.get("replay_summary") or {}).get(
                                "failed_trajectories"
                            )
                            == 0
                        ],
                        50,
                    ),
                    "p95": percentile(
                        [
                            item["elapsed_seconds"] * 1000.0
                            for item in items
                            if item["status"] == "completed"
                            and (item.get("replay_summary") or {}).get(
                                "failed_trajectories"
                            )
                            == 0
                        ],
                        95,
                    ),
                }
                for trajectory_class, items in sorted(results_by_class.items())
            },
        },
        "replay_metrics": {
            "replay_window_seconds": replay_window_s,
            "completed_trajectories": replay_completed,
            "failed_trajectories": replay_failed,
            "valid_completed_trajectories": replay_valid_completed,
            "valid_trajectory_rate": (
                replay_valid_completed / replay_completed if replay_completed else None
            ),
            "valid_completed_trajectories_per_s": (
                replay_valid_completed / replay_window_s if replay_window_s else None
            ),
            "valid_trajectory_latency_ms": {
                "p50": percentile(replay_valid_latencies_ms, 50),
                "p90": percentile(replay_valid_latencies_ms, 90),
                "p95": replay_p95_latency_ms,
                "p99": percentile(replay_valid_latencies_ms, 99),
            },
            "tail_trajectory_progress_per_s": (
                1000.0 / replay_p95_latency_ms if replay_p95_latency_ms else None
            ),
            "completed_valid_trajectories_by_latency_target": replay_latency_targets,
        },
        "replay_metrics_by_class": {
            trajectory_class: {
                "count": len(items),
                "valid": sum(
                    1
                    for item in items
                    if item["status"] == "completed"
                    and (item.get("replay_summary") or {}).get("failed_trajectories") == 0
                ),
                "p50": percentile(
                    [
                        latency_ms
                        for item in items
                        if item["status"] == "completed"
                        and (item.get("replay_summary") or {}).get("failed_trajectories") == 0
                        and (
                            latency_ms := replay_valid_latency_ms(
                                item.get("replay_summary")
                            )
                        )
                        is not None
                    ],
                    50,
                ),
                "p95": percentile(
                    [
                        latency_ms
                        for item in items
                        if item["status"] == "completed"
                        and (item.get("replay_summary") or {}).get("failed_trajectories") == 0
                        and (
                            latency_ms := replay_valid_latency_ms(
                                item.get("replay_summary")
                            )
                        )
                        is not None
                    ],
                    95,
                ),
            }
            for trajectory_class, items in sorted(results_by_class.items())
        },
        "results": results,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["failed_jobs"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
