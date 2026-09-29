#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
"""Run one host replay agent with its own setup and active workspace.

This is the simplest MLPerf host-replay PoC flow:

    read environment JSON
    read submitter config JSON
    validate allowed submitter knobs
    materialize this agent's workspace
    run the frozen trajectory once in that workspace

One agent owns one setup and one mutable workspace for one replay.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from materialize_host_environment import load_json, materialize


def source_cwd(environment_path: Path, repo_root: Path) -> Path:
    environment = load_json(environment_path)
    source = environment["source"]
    if source.get("kind") == "git":
        return repo_root
    if source.get("kind") != "local_fixture":
        raise ValueError(
            "automatic cwd mapping currently supports local_fixture and git sources only"
        )
    path = Path(str(source["path"])).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def replay_environment(resolved_config: dict[str, object]) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in resolved_config.items():
        env[f"MLPERF_{key.upper()}"] = str(value)
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--submitter-config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", default=Path.cwd(), type=Path)
    parser.add_argument("--allow-mutating", action="store_true")
    parser.add_argument("--target-concurrency", type=int, default=1)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    setup_root = args.output_dir / "setup"
    replay_out = args.output_dir / "replay"

    manifest = materialize(
        environment_path=args.environment,
        submitter_path=args.submitter_config,
        output_root=setup_root,
        repo_root=repo_root,
    )

    recorded_cwd = source_cwd(args.environment, repo_root)
    active_workspace = Path(str(manifest["workspace"])).resolve()
    replay_script = repo_root / "scripts" / "agentic_host_tool_replay.py"
    command = [
        sys.executable,
        str(replay_script),
        "--input",
        str(args.trajectory),
        "--output-dir",
        str(replay_out),
        "--target-concurrency",
        str(args.target_concurrency),
        "--cwd-map",
        f"{recorded_cwd}={active_workspace}",
    ]
    if args.allow_mutating:
        command.append("--allow-mutating")

    subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        env=replay_environment(dict(manifest.get("resolved_config", {}))),
    )

    run_manifest = {
        "agent_model": "single_agent_owns_setup_and_workspace",
        "environment": str(args.environment),
        "submitter_config": str(args.submitter_config),
        "trajectory": str(args.trajectory),
        "recorded_cwd": str(recorded_cwd),
        "active_workspace": str(active_workspace),
        "setup_manifest": manifest,
        "replay_output": str(replay_out),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "single_agent_manifest.json").write_text(
        json.dumps(run_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(run_manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
