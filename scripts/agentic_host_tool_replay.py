#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
"""Prototype host/tool trajectory replay benchmark.

This is intentionally separate from the production endpoint CLI. It prototypes
the proposed host-side agentic benchmark shape:

    execute recorded bash tool -> validate -> next step

Input is JSONL with one tool step per line. Minimal row shape:

    {
      "trajectory_id": "example",
      "turn": 2,
      "tool_call_id": "functions.bash:0",
      "tool_type": "bash",
      "command": "grep -n Agentic src/...",
      "cwd": ".",
      "timeout_s": 30
    }

Output:
  - events.jsonl with TOOL_STARTED/TOOL_COMPLETED/TOOL_FAILED and trajectory events
  - summary.json with tool latency percentiles, trajectory latency, tool share,
    and completed trajectories/sec at target concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shlex
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any


MUTATING_COMMANDS = {
    "cat",  # only mutating when used with redirection, handled by substring check
    "chmod",
    "chown",
    "conda",
    "cp",
    "curl",
    "mkdir",
    "mv",
    "pip",
    "python",  # only mutating with known heredoc/temp-writing patterns
    "python3",
    "rm",
    "sed",  # only mutating with -i, handled by substring check
    "source",
    "touch",
    "wget",
}

SAFE_COMMANDS = {
    "awk",
    "cat",
    "diff",
    "echo",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "python",
    "python3",
    "sed",
    "tail",
    "test",
    "which",
}


@dataclass(frozen=True)
class ToolStep:
    trajectory_id: str
    turn: int
    tool_call_id: str
    command: str
    model_latency_s: float
    cwd: Path
    timeout_s: float
    trajectory_class: str = "UNSPECIFIED"
    tool_type: str = "bash"
    validates: dict[str, Any] | None = None
    placement: dict[str, str] | None = None


class JsonlEventWriter:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, event: dict[str, Any]) -> None:
        event.setdefault("timestamp_ns", time.monotonic_ns())
        line = json.dumps(event, sort_keys=True)
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100.0
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _command_head(command: str) -> str:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return ""
    return Path(parts[0]).name if parts else ""


def _format_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }


def template_values_from_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("MLPERF_"):
            values[key.removeprefix("MLPERF_").lower()] = value
    return values


def expand_command_template(template: str, values: dict[str, str]) -> str:
    missing = sorted(_format_fields(template) - set(values))
    if missing:
        raise ValueError(
            f"command_template references missing submitter parameters: {missing}"
        )
    return template.format(**values)


def placement_policy_from_env() -> dict[str, str]:
    policy: dict[str, str] = {}
    for name in ("cpu_affinity", "numa_cpunodebind", "numa_membind"):
        value = os.environ.get(f"MLPERF_{name.upper()}")
        if value:
            policy[name] = value
    return policy


def wrap_command_for_placement(command: str, placement: dict[str, str]) -> str:
    prefix: list[str] = []
    cpu_affinity = placement.get("cpu_affinity")
    if cpu_affinity:
        prefix.extend(["taskset", "-c", cpu_affinity])

    numa_options: list[str] = []
    if cpunodebind := placement.get("numa_cpunodebind"):
        numa_options.append(f"--cpunodebind={cpunodebind}")
    if membind := placement.get("numa_membind"):
        numa_options.append(f"--membind={membind}")
    if numa_options:
        prefix.extend(["numactl", *numa_options])

    if not prefix:
        return command
    return " ".join(shlex.quote(part) for part in [*prefix, "bash", "-lc", command])


def classify_command(command: str) -> str:
    head = _command_head(command)
    if head in {"grep", "sed", "cat", "find", "ls", "head", "tail", "awk"}:
        return "file_inspection"
    if head in {"python", "python3", "pytest", "tox"} or "pytest" in command:
        return "python_pytest"
    if head == "git":
        return "git_repository"
    if head in {"pip", "conda", "uv", "poetry"}:
        return "package_environment"
    if head in {
        "make",
        "cmake",
        "ninja",
        "gcc",
        "clang",
        "cargo",
        "go",
        "npm",
        "yarn",
        "mvn",
        "gradle",
    }:
        return "build_native_test"
    if head in {"rm", "cp", "mv", "mkdir", "chmod"}:
        return "filesystem_mutation"
    return "other_bash"


def _looks_mutating(command: str) -> bool:
    head = _command_head(command)
    if head in {"rm", "cp", "mv", "mkdir", "chmod", "chown", "touch"}:
        return True
    if head == "sed" and (" -i" in command or command.startswith("sed -i")):
        return True
    if ">" in command or "<<" in command:
        return True
    if head in {"pip", "conda", "uv", "poetry", "curl", "wget"}:
        return True
    return False


def _validate_command(command: str, allow_mutating: bool) -> None:
    head = _command_head(command)
    if not head:
        raise ValueError(f"cannot parse command: {command!r}")
    if not allow_mutating and _looks_mutating(command):
        raise ValueError(
            f"refusing potentially mutating command without --allow-mutating: {command!r}"
        )
    if not allow_mutating and head not in SAFE_COMMANDS:
        raise ValueError(
            f"command head {head!r} is not in the prototype safe list; "
            "pass --allow-mutating to run the raw trace"
        )


def _resolve_cwd(path_value: str | Path, base: Path) -> Path:
    cwd = Path(path_value).expanduser()
    if not cwd.is_absolute():
        cwd = (base / cwd).resolve()
    return cwd


def parse_cwd_maps(values: list[str], base: Path) -> list[tuple[Path, Path]]:
    maps: list[tuple[Path, Path]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"cwd map must be FROM=TO, got {value!r}")
        source, target = value.split("=", 1)
        maps.append((_resolve_cwd(source, base), _resolve_cwd(target, base)))
    return maps


def remap_cwd(cwd: Path, cwd_maps: list[tuple[Path, Path]]) -> Path:
    for source, target in cwd_maps:
        try:
            suffix = cwd.relative_to(source)
        except ValueError:
            continue
        return target / suffix
    return cwd


def load_steps(
    path: Path,
    default_cwd: Path,
    default_model_latency_ms: float,
    cwd_maps: list[tuple[Path, Path]] | None = None,
) -> list[ToolStep]:
    steps: list[ToolStep] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            trajectory_id = str(
                row.get("trajectory_id")
                or row.get("conversation_id")
                or row.get("id")
                or f"trajectory_{line_no}"
            )
            turn = int(row.get("turn") or row.get("step_id") or line_no)
            tool_call_id = str(row.get("tool_call_id") or f"{trajectory_id}:{turn}")
            command = row.get("command")
            command_template = row.get("command_template")
            if isinstance(command_template, str) and command_template:
                command = expand_command_template(
                    command_template, template_values_from_env()
                )
            if not isinstance(command, str) or not command:
                raise ValueError(
                    f"{path}:{line_no}: missing non-empty command or command_template"
                )
            model_latency_ms = float(
                row.get("model_latency_ms", default_model_latency_ms)
            )
            timeout_s = float(row.get("timeout_s", 30.0))
            cwd = _resolve_cwd(row.get("cwd") or default_cwd, Path.cwd())
            cwd = remap_cwd(cwd, cwd_maps or [])
            steps.append(
                ToolStep(
                    trajectory_id=trajectory_id,
                    turn=turn,
                    tool_call_id=tool_call_id,
                    command=command,
                    model_latency_s=model_latency_ms / 1000.0,
                    cwd=cwd,
                    timeout_s=timeout_s,
                    trajectory_class=str(
                        row.get("trajectory_class") or row.get("class") or "UNSPECIFIED"
                    ),
                    tool_type=str(row.get("tool_type", "bash")),
                    validates=row.get("validates")
                    if isinstance(row.get("validates"), dict)
                    else None,
                    placement=placement_policy_from_env(),
                )
            )
    return steps


async def _run_validation_command(
    command: str,
    cwd: Path,
    timeout_s: float = 30.0,
) -> tuple[int | None, str, str]:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/bash",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        return None, "", str(exc)


def _resolve_validation_path(step: ToolStep, path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return step.cwd / path


def _contains_all(text: str, patterns: list[str]) -> bool:
    return all(pattern in text for pattern in patterns)


def _contains_none(text: str, patterns: list[str]) -> bool:
    return all(pattern not in text for pattern in patterns)


async def validate_step_result(
    step: ToolStep,
    exit_code: int | None,
    stdout_text: str,
    stderr_text: str,
) -> dict[str, Any]:
    spec = step.validates
    if not spec:
        return {
            "kind": "default_exit_code",
            "expected": 0,
            "actual": exit_code,
            "valid": exit_code == 0,
        }

    kind = spec.get("kind")
    details: dict[str, Any] = {"kind": kind, "valid": False}

    def require_expected_exit_code(valid: bool) -> bool:
        if "expected_exit_code" not in spec:
            return valid
        expected_exit_code = int(spec["expected_exit_code"])
        details.update(
            {
                "expected_exit_code": expected_exit_code,
                "actual_exit_code": exit_code,
            }
        )
        return valid and exit_code == expected_exit_code

    if kind == "exit_code":
        expected = int(spec.get("expected", 0))
        details.update({"expected": expected, "actual": exit_code})
        details["valid"] = exit_code == expected
        return details

    if kind == "stdout_contains":
        patterns = list(spec.get("patterns") or [])
        missing = [pattern for pattern in patterns if pattern not in stdout_text]
        details.update({"missing": missing, "patterns": patterns})
        details["valid"] = require_expected_exit_code(not missing)
        return details

    if kind in {"file_contains", "file_state"}:
        path = _resolve_validation_path(step, str(spec["path"]))
        text = path.read_text(encoding="utf-8")
        contains = list(spec.get("contains") or spec.get("patterns") or [])
        not_contains = list(spec.get("not_contains") or [])
        details.update(
            {
                "path": str(path),
                "missing": [pattern for pattern in contains if pattern not in text],
                "unexpected": [
                    pattern for pattern in not_contains if pattern in text
                ],
            }
        )
        details["valid"] = require_expected_exit_code(
            _contains_all(text, contains) and _contains_none(text, not_contains)
        )
        return details

    if kind == "file_sha256":
        path = _resolve_validation_path(step, str(spec["path"]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = str(spec["sha256"])
        details.update({"path": str(path), "expected": expected, "actual": digest})
        details["valid"] = require_expected_exit_code(digest == expected)
        return details

    if kind in {"git_diff_empty", "git_diff_contains"}:
        paths = list(spec.get("paths") or [])
        quoted_paths = " ".join(shlex.quote(path) for path in paths)
        command = "git diff -- " + quoted_paths if quoted_paths else "git diff"
        rc, diff_stdout, diff_stderr = await _run_validation_command(
            command,
            cwd=step.cwd,
            timeout_s=float(spec.get("timeout_s", 30.0)),
        )
        details.update(
            {
                "command": command,
                "exit_code": rc,
                "stderr_preview": diff_stderr[:2048],
            }
        )
        if kind == "git_diff_empty":
            details["diff_bytes"] = len(diff_stdout.encode("utf-8"))
            details["valid"] = require_expected_exit_code(rc == 0 and diff_stdout == "")
            return details
        patterns = list(spec.get("patterns") or [])
        missing = [pattern for pattern in patterns if pattern not in diff_stdout]
        details.update({"missing": missing, "diff_preview": diff_stdout[:4096]})
        details["valid"] = require_expected_exit_code(rc == 0 and not missing)
        return details

    details["error"] = f"unknown validation kind: {kind!r}"
    return details


def group_steps(steps: list[ToolStep]) -> dict[str, list[ToolStep]]:
    grouped: dict[str, list[ToolStep]] = defaultdict(list)
    for step in steps:
        grouped[step.trajectory_id].append(step)
    for trajectory_steps in grouped.values():
        trajectory_steps.sort(key=lambda s: (s.turn, s.tool_call_id))
    return dict(grouped)


async def execute_step(
    step: ToolStep,
    events: JsonlEventWriter,
    allow_mutating: bool,
    max_output_bytes: int,
) -> dict[str, Any]:
    await asyncio.sleep(step.model_latency_s)
    _validate_command(step.command, allow_mutating=allow_mutating)
    command_family = classify_command(step.command)
    execution_command = wrap_command_for_placement(step.command, step.placement or {})
    await events.write(
        {
            "event_type": "MODEL_LATENCY_REPLAYED",
            "trajectory_id": step.trajectory_id,
            "turn": step.turn,
            "tool_call_id": step.tool_call_id,
            "model_latency_ns": int(step.model_latency_s * 1_000_000_000),
        }
    )
    await events.write(
        {
            "event_type": "TOOL_STARTED",
            "trajectory_id": step.trajectory_id,
            "turn": step.turn,
            "tool_call_id": step.tool_call_id,
            "tool_type": step.tool_type,
            "command_family": command_family,
            "command": step.command,
            "execution_command": execution_command,
            "cwd": str(step.cwd),
            "placement": step.placement or {},
        }
    )
    start_ns = time.monotonic_ns()
    timed_out = False
    exit_code: int | None
    stdout = b""
    stderr = b""
    try:
        proc = await asyncio.create_subprocess_shell(
            execution_command,
            cwd=step.cwd,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/bash",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=step.timeout_s
            )
        except TimeoutError:
            timed_out = True
            proc.kill()
            stdout, stderr = await proc.communicate()
        exit_code = proc.returncode
    except Exception as exc:
        exit_code = None
        stderr = str(exc).encode("utf-8", errors="replace")
    end_ns = time.monotonic_ns()
    latency_ns = end_ns - start_ns
    full_stdout_text = stdout.decode("utf-8", errors="replace")
    full_stderr_text = stderr.decode("utf-8", errors="replace")
    stdout_text = stdout[:max_output_bytes].decode("utf-8", errors="replace")
    stderr_text = stderr[:max_output_bytes].decode("utf-8", errors="replace")
    validation = await validate_step_result(
        step,
        exit_code=exit_code,
        stdout_text=full_stdout_text,
        stderr_text=full_stderr_text,
    )
    event_type = "TOOL_FAILED" if timed_out or not validation["valid"] else "TOOL_COMPLETED"
    result = {
        "event_type": event_type,
        "trajectory_id": step.trajectory_id,
        "turn": step.turn,
        "tool_call_id": step.tool_call_id,
        "tool_type": step.tool_type,
        "command_family": command_family,
        "command": step.command,
        "execution_command": execution_command,
        "cwd": str(step.cwd),
        "placement": step.placement or {},
        "exit_code": exit_code,
        "timed_out": timed_out,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "latency_ns": latency_ns,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_preview": stdout_text,
        "stderr_preview": stderr_text,
        "stdout_truncated": len(stdout) > max_output_bytes,
        "stderr_truncated": len(stderr) > max_output_bytes,
        "validation": validation,
    }
    await events.write(result)
    return result


async def run_trajectory(
    trajectory_id: str,
    steps: list[ToolStep],
    events: JsonlEventWriter,
    allow_mutating: bool,
    max_output_bytes: int,
    stop_on_invalid_step: bool,
) -> dict[str, Any]:
    trajectory_start_ns = time.monotonic_ns()
    trajectory_class = steps[0].trajectory_class if steps else "UNSPECIFIED"
    await events.write(
        {
            "event_type": "TRAJECTORY_STARTED",
            "trajectory_id": trajectory_id,
            "trajectory_class": trajectory_class,
            "steps": len(steps),
        }
    )
    tool_results = []
    failed = False
    for step in steps:
        result = await execute_step(
            step,
            events=events,
            allow_mutating=allow_mutating,
            max_output_bytes=max_output_bytes,
        )
        tool_results.append(result)
        if result["event_type"] == "TOOL_FAILED":
            failed = True
            if stop_on_invalid_step:
                break
    trajectory_end_ns = time.monotonic_ns()
    trajectory_latency_ns = trajectory_end_ns - trajectory_start_ns
    total_tool_ns = sum(int(r["latency_ns"]) for r in tool_results)
    summary = {
        "event_type": "TRAJECTORY_COMPLETED",
        "trajectory_id": trajectory_id,
        "trajectory_class": trajectory_class,
        "steps": len(steps),
        "steps_executed": len(tool_results),
        "failed": failed,
        "start_ns": trajectory_start_ns,
        "end_ns": trajectory_end_ns,
        "trajectory_latency_ns": trajectory_latency_ns,
        "total_tool_execution_ns": total_tool_ns,
        "tool_share": total_tool_ns / trajectory_latency_ns
        if trajectory_latency_ns
        else None,
    }
    await events.write(summary)
    return summary


async def run_all(
    trajectories: dict[str, list[ToolStep]],
    target_concurrency: int,
    events: JsonlEventWriter,
    allow_mutating: bool,
    max_output_bytes: int,
    stop_on_invalid_step: bool,
) -> list[dict[str, Any]]:
    queue: asyncio.Queue[tuple[str, list[ToolStep]] | None] = asyncio.Queue()
    for item in trajectories.items():
        queue.put_nowait(item)
    workers = max(1, min(target_concurrency, len(trajectories)))
    summaries: list[dict[str, Any]] = []

    async def worker() -> None:
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return
            trajectory_id, steps = item
            try:
                summaries.append(
                    await run_trajectory(
                        trajectory_id,
                        steps,
                        events=events,
                        allow_mutating=allow_mutating,
                        max_output_bytes=max_output_bytes,
                        stop_on_invalid_step=stop_on_invalid_step,
                    )
                )
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    await queue.join()
    for _ in tasks:
        queue.put_nowait(None)
    await asyncio.gather(*tasks)
    return summaries


def build_summary(
    trajectory_summaries: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    duration_ns: int,
    target_concurrency: int,
    latency_targets_ms: list[float],
) -> dict[str, Any]:
    tool_latencies_ms = [event["latency_ns"] / 1_000_000 for event in tool_events]
    trajectory_latencies_ms = [
        item["trajectory_latency_ns"] / 1_000_000 for item in trajectory_summaries
    ]
    valid_trajectory_summaries = [
        item for item in trajectory_summaries if not item["failed"]
    ]
    valid_trajectory_latencies_ms = [
        item["trajectory_latency_ns"] / 1_000_000 for item in valid_trajectory_summaries
    ]
    total_tool_ns = sum(event["latency_ns"] for event in tool_events)
    total_trajectory_ns = sum(item["trajectory_latency_ns"] for item in trajectory_summaries)
    command_families = Counter(event["command_family"] for event in tool_events)
    p95_latency_ms = _percentile(valid_trajectory_latencies_ms, 95)
    measured_duration_s = duration_ns / 1_000_000_000
    summaries_by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trajectory_summaries:
        summaries_by_class[str(item.get("trajectory_class", "UNSPECIFIED"))].append(item)
    return {
        "target_concurrency": target_concurrency,
        "measured_duration_s": measured_duration_s,
        "completed_trajectories": len(trajectory_summaries),
        "failed_trajectories": sum(1 for item in trajectory_summaries if item["failed"]),
        "valid_completed_trajectories": len(valid_trajectory_summaries),
        "valid_trajectory_rate": (
            len(valid_trajectory_summaries) / len(trajectory_summaries)
            if trajectory_summaries
            else None
        ),
        "valid_completed_trajectories_per_s": (
            len(valid_trajectory_summaries) / measured_duration_s
            if measured_duration_s
            else None
        ),
        "tail_trajectory_progress_per_s": (
            1000.0 / p95_latency_ms if p95_latency_ms else None
        ),
        "completed_valid_trajectories_by_latency_target": {
            str(target): sum(
                1 for latency_ms in valid_trajectory_latencies_ms if latency_ms <= target
            )
            for target in latency_targets_ms
        },
        "tool_calls_executed": len(tool_events),
        "total_measured_tool_execution_ms": total_tool_ns / 1_000_000,
        "aggregate_tool_share": total_tool_ns / total_trajectory_ns
        if total_trajectory_ns
        else None,
        "tool_latency_ms": {
            "mean": statistics.fmean(tool_latencies_ms) if tool_latencies_ms else None,
            "p50": _percentile(tool_latencies_ms, 50),
            "p90": _percentile(tool_latencies_ms, 90),
            "p99": _percentile(tool_latencies_ms, 99),
        },
        "trajectory_latency_ms": {
            "mean": statistics.fmean(trajectory_latencies_ms)
            if trajectory_latencies_ms
            else None,
            "p50": _percentile(trajectory_latencies_ms, 50),
            "p90": _percentile(trajectory_latencies_ms, 90),
            "p99": _percentile(trajectory_latencies_ms, 99),
        },
        "valid_trajectory_latency_ms": {
            "mean": statistics.fmean(valid_trajectory_latencies_ms)
            if valid_trajectory_latencies_ms
            else None,
            "p50": _percentile(valid_trajectory_latencies_ms, 50),
            "p90": _percentile(valid_trajectory_latencies_ms, 90),
            "p95": p95_latency_ms,
            "p99": _percentile(valid_trajectory_latencies_ms, 99),
        },
        "trajectory_latency_by_class_ms": {
            trajectory_class: {
                "count": len(items),
                "valid": sum(1 for item in items if not item["failed"]),
                "p95": _percentile(
                    [
                        item["trajectory_latency_ns"] / 1_000_000
                        for item in items
                        if not item["failed"]
                    ],
                    95,
                ),
            }
            for trajectory_class, items in sorted(summaries_by_class.items())
        },
        "tool_calls_by_command_family": dict(sorted(command_families.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Tool-step JSONL")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--target-concurrency", type=int, default=1)
    parser.add_argument("--default-cwd", type=Path, default=Path("."))
    parser.add_argument("--default-model-latency-ms", type=float, default=0.0)
    parser.add_argument(
        "--cwd-map",
        action="append",
        default=[],
        metavar="FROM=TO",
        help="Map recorded working-directory prefixes to materialized workspaces.",
    )
    parser.add_argument("--allow-mutating", action="store_true")
    parser.add_argument(
        "--continue-on-invalid-step",
        action="store_true",
        help="Continue executing remaining steps after a failed validation.",
    )
    parser.add_argument(
        "--latency-target-ms",
        action="append",
        default=[],
        type=float,
        help="Latency target used to count completed valid trajectories.",
    )
    parser.add_argument("--max-output-bytes", type=int, default=4096)
    return parser.parse_args()


async def amain() -> None:
    args = parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    steps = load_steps(
        args.input,
        default_cwd=args.default_cwd,
        default_model_latency_ms=args.default_model_latency_ms,
        cwd_maps=parse_cwd_maps(args.cwd_map, Path.cwd()),
    )
    trajectories = group_steps(steps)
    events = JsonlEventWriter(events_path)

    start_ns = time.monotonic_ns()
    trajectory_summaries = await run_all(
        trajectories,
        target_concurrency=args.target_concurrency,
        events=events,
        allow_mutating=args.allow_mutating,
        max_output_bytes=args.max_output_bytes,
        stop_on_invalid_step=not args.continue_on_invalid_step,
    )
    end_ns = time.monotonic_ns()

    tool_events: list[dict[str, Any]] = []
    with events_path.open(encoding="utf-8") as f:
        for line in f:
            event = json.loads(line)
            if event["event_type"] in {"TOOL_COMPLETED", "TOOL_FAILED"}:
                tool_events.append(event)
    summary = build_summary(
        trajectory_summaries,
        tool_events,
        duration_ns=end_ns - start_ns,
        target_concurrency=args.target_concurrency,
        latency_targets_ms=args.latency_target_ms,
    )
    with (output_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["failed_trajectories"]:
        raise SystemExit(1)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
