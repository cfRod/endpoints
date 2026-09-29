#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
"""Materialize a host replay environment from a benchmark manifest.

This is the setup side of the host replay prototype. It intentionally avoids
Ansible: the benchmark owns a JSON environment contract, the submitter provides
only declared knob values, and this script turns that into a prepared workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from string import Formatter
from typing import Any


class ConfigError(ValueError):
    """Raised when a manifest or submitter config is invalid."""


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def host_logical_cpus() -> int:
    return os.cpu_count() or 1


def _parse_non_negative_ranges(value: str, label: str) -> list[int]:
    items: list[int] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            raise ConfigError(f"{label} contains an empty range component: {value!r}")
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ConfigError(f"{label} range start is greater than end: {part!r}")
            items.extend(range(start, end + 1))
        else:
            items.append(int(part))
    if any(item < 0 for item in items):
        raise ConfigError(f"{label} must not contain negative values: {value!r}")
    return items


def _validate_cpu_affinity(value: str) -> None:
    if not value:
        return
    if shutil.which("taskset") is None:
        raise ConfigError("cpu_affinity requires taskset, but taskset was not found")
    cpus = _parse_non_negative_ranges(value, "cpu_affinity")
    max_cpu = host_logical_cpus() - 1
    invalid = [cpu for cpu in cpus if cpu > max_cpu]
    if invalid:
        raise ConfigError(
            f"cpu_affinity references CPUs outside 0..{max_cpu}: {invalid}"
        )


def _validate_numa_nodes(value: str, label: str) -> None:
    if not value:
        return
    if shutil.which("numactl") is None:
        raise ConfigError(f"{label} requires numactl, but numactl was not found")
    nodes = _parse_non_negative_ranges(value, label)
    node_root = Path("/sys/devices/system/node")
    if not node_root.exists():
        return
    invalid = [node for node in nodes if not (node_root / f"node{node}").exists()]
    if invalid:
        raise ConfigError(f"{label} references unavailable NUMA nodes: {invalid}")


def validate_placement_controls(resolved: dict[str, Any]) -> None:
    _validate_cpu_affinity(str(resolved.get("cpu_affinity") or ""))
    _validate_numa_nodes(str(resolved.get("numa_cpunodebind") or ""), "numa_cpunodebind")
    _validate_numa_nodes(str(resolved.get("numa_membind") or ""), "numa_membind")


def _version_tuple(version: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)*", version)
    if not match:
        raise ConfigError(f"could not parse compiler version from {version!r}")
    return tuple(int(part) for part in match.group(0).split("."))


def _compare_versions(actual: tuple[int, ...], expected: tuple[int, ...]) -> int:
    width = max(len(actual), len(expected))
    left = actual + (0,) * (width - len(actual))
    right = expected + (0,) * (width - len(expected))
    return (left > right) - (left < right)


def _version_satisfies(actual: str, envelope: str) -> bool:
    actual_tuple = _version_tuple(actual)
    for raw_clause in envelope.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        match = re.fullmatch(r"(<=|>=|==|<|>)\s*(\d+(?:\.\d+)*)", clause)
        if not match:
            raise ConfigError(f"unsupported version constraint: {clause!r}")
        operator, expected = match.groups()
        comparison = _compare_versions(actual_tuple, _version_tuple(expected))
        if operator == "<" and not comparison < 0:
            return False
        if operator == "<=" and not comparison <= 0:
            return False
        if operator == ">" and not comparison > 0:
            return False
        if operator == ">=" and not comparison >= 0:
            return False
        if operator == "==" and not comparison == 0:
            return False
    return True


def _probe_executable(executable: str, args: list[str], timeout_s: float = 10.0) -> str:
    result = subprocess.run(
        [executable, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError(
            f"{executable} {' '.join(args)} failed with exit code {result.returncode}: "
            f"{result.stdout[:400]}"
        )
    return result.stdout


def _probe_compiler_macros(executable: str, timeout_s: float = 10.0) -> str:
    result = subprocess.run(
        [executable, "-dM", "-E", "-x", "c", "-"],
        input="",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError(
            f"{executable} compiler macro probe failed with exit code {result.returncode}: "
            f"{result.stdout[:400]}"
        )
    return result.stdout


def _validate_executable_constraints(name: str, executable: str, spec: dict[str, Any]) -> None:
    allowed_family = spec.get("allowed_family")
    allowed_versions = spec.get("allowed_versions")
    if not allowed_family and not allowed_versions:
        return

    version_output = _probe_executable(executable, ["--version"])
    version_line = version_output.splitlines()[0] if version_output.splitlines() else ""
    compiler_version = _probe_executable(
        executable,
        ["-dumpfullversion", "-dumpversion"],
    ).strip()

    if allowed_family:
        family = str(allowed_family).lower()
        lowered_version = version_output.lower()
        if family == "gcc":
            macros = _probe_compiler_macros(executable)
            if "clang" in lowered_version:
                raise ConfigError(
                    f"{name}={executable!r} is not in allowed compiler family gcc: "
                    f"{version_line}"
                )
            if "__clang__" in macros or "__GNUC__" not in macros:
                raise ConfigError(
                    f"{name}={executable!r} is not in allowed compiler family gcc: "
                    f"{version_line}"
                )
            if not compiler_version:
                raise ConfigError(f"{name}={executable!r} did not report a GCC version")
        else:
            raise ConfigError(f"unsupported allowed_family for {name}: {allowed_family!r}")

    if allowed_versions and not _version_satisfies(compiler_version, str(allowed_versions)):
        raise ConfigError(
            f"{name}={executable!r} version {compiler_version} outside allowed range "
            f"{allowed_versions!r}"
        )


def validate_submitter_config(
    environment: dict[str, Any],
    submitter: dict[str, Any],
) -> dict[str, Any]:
    environment_id = str(environment["environment_id"])
    if submitter.get("environment_id") != environment_id:
        raise ConfigError(
            "submitter config environment_id does not match "
            f"{environment_id!r}: {submitter.get('environment_id')!r}"
        )

    allowed = environment.get("configurable", {})
    values = submitter.get("values", {})
    if not isinstance(allowed, dict) or not isinstance(values, dict):
        raise ConfigError("configurable and submitter values must be objects")

    extra = sorted(set(values) - set(allowed))
    if extra:
        raise ConfigError(f"undeclared submitter knobs: {extra}")

    resolved: dict[str, Any] = {}
    for name, spec in allowed.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"configurable knob {name!r} must be an object")
        value = values.get(name, spec.get("default"))
        kind = spec.get("type")

        if kind == "integer":
            value = int(value)
            min_value = int(spec.get("min", value))
            max_spec = spec.get("max", value)
            max_value = host_logical_cpus() if max_spec == "host_logical_cpus" else int(max_spec)
            if value < min_value or value > max_value:
                raise ConfigError(f"{name}={value} outside allowed range {min_value}..{max_value}")
        elif kind == "enum":
            allowed_values = list(spec.get("values", []))
            if value not in allowed_values:
                raise ConfigError(f"{name}={value!r} not in allowed values {allowed_values}")
        elif kind == "executable":
            value = str(value)
            if not shutil.which(value) and not Path(value).exists():
                raise ConfigError(f"{name} executable was not found: {value}")
            _validate_executable_constraints(name, value, spec)
        elif kind == "path":
            value = str(value)
            if not Path(value).expanduser().exists():
                raise ConfigError(f"{name} path does not exist: {value}")
        elif kind == "string":
            value = str(value)
            pattern = spec.get("pattern")
            if pattern and not re.fullmatch(str(pattern), value):
                raise ConfigError(f"{name}={value!r} does not match {pattern!r}")
        else:
            raise ConfigError(f"unknown configurable type for {name!r}: {kind!r}")

        resolved[name] = value

    validate_placement_controls(resolved)
    return resolved


def _format_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }


def expand(template: str, values: dict[str, Any]) -> str:
    missing = sorted(_format_fields(template) - set(values))
    if missing:
        raise ConfigError(f"template {template!r} references missing fields: {missing}")
    return template.format(**values)


def placement_policy(resolved: dict[str, Any]) -> dict[str, str]:
    policy: dict[str, str] = {}
    for name in ("cpu_affinity", "numa_cpunodebind", "numa_membind"):
        value = str(resolved.get(name) or "")
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


def run_command(
    command: str,
    cwd: Path,
    log_file: Any,
    timeout_s: float,
    placement: dict[str, str] | None = None,
) -> None:
    execution_command = wrap_command_for_placement(command, placement or {})
    log_file.write(f"$ {command}\n")
    if execution_command != command:
        log_file.write(f"[placement] {execution_command}\n")
    log_file.flush()
    result = subprocess.run(
        execution_command,
        cwd=cwd,
        shell=True,
        executable="/bin/bash",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
    )
    log_file.write(result.stdout)
    log_file.write(f"exit_code={result.returncode}\n\n")
    log_file.flush()
    if result.returncode != 0:
        raise RuntimeError(f"setup command failed with exit code {result.returncode}: {command}")


def copy_local_fixture(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)

    def ignore(_: str, names: list[str]) -> set[str]:
        ignored = {".git", "__pycache__", "build"}
        ignored.update(name for name in names if name.endswith((".pyc", ".class")))
        return ignored.intersection(names)

    shutil.copytree(source, destination, ignore=ignore)


def initialize_git_index(workspace: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)


def materialize(
    environment_path: Path,
    submitter_path: Path,
    output_root: Path,
    repo_root: Path,
) -> dict[str, Any]:
    environment = load_json(environment_path)
    submitter = load_json(submitter_path)
    resolved = validate_submitter_config(environment, submitter)

    source = environment["source"]
    if not isinstance(source, dict):
        raise ConfigError("source must be an object")

    environment_id = str(environment["environment_id"])
    env_root = output_root / environment_id
    workspace_root = env_root / "workspace"
    work = workspace_root / "work"
    env_root.mkdir(parents=True, exist_ok=True)

    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True)

    setup_values = {
        **source,
        **resolved,
        "environment_id": environment_id,
        "workspace_root": str(workspace_root),
        "work": str(work),
    }

    source_kind = source.get("kind")
    if source_kind == "local_fixture":
        source_path = Path(str(source["path"])).expanduser()
        if not source_path.is_absolute():
            source_path = repo_root / source_path
        copy_local_fixture(source_path, work)
    elif source_kind == "git":
        repo_url = shlex.quote(str(source["repo_url"]))
        run_command(f"git clone {repo_url} {shlex.quote(str(work))}", workspace_root, sys.stdout, 300)
        run_command(
            f"git -C {shlex.quote(str(work))} checkout {shlex.quote(str(source['base_commit']))}",
            workspace_root,
            sys.stdout,
            300,
        )
    else:
        raise ConfigError(f"unknown source kind: {source_kind!r}")

    if environment.get("git_index", {}).get("initialize", False):
        initialize_git_index(work)

    setup_log = env_root / "setup.log"
    setup_placement = placement_policy(resolved)
    probe_results: list[dict[str, Any]] = []
    with setup_log.open("w", encoding="utf-8") as log:
        log.write(f"environment_id={environment_id}\n")
        log.write(f"resolved_config={json.dumps(resolved, sort_keys=True)}\n")
        log.write(f"placement={json.dumps(setup_placement, sort_keys=True)}\n")
        log.write(f"work={work}\n\n")
        for probe in environment.get("probes", []):
            command = expand(str(probe), setup_values)
            log.write(f"[probe] ")
            run_command(
                command,
                work,
                log,
                float(environment.get("setup_timeout_s", 300)),
                setup_placement,
            )
            probe_results.append({"command": command})
        for command_template in environment.get("setup", []):
            command = expand(str(command_template), setup_values)
            run_command(
                command,
                work,
                log,
                float(environment.get("setup_timeout_s", 300)),
                setup_placement,
            )

    manifest = {
        "environment_id": environment_id,
        "fixed": environment.get("fixed", {}),
        "source": source,
        "resolved_config": resolved,
        "placement": setup_placement,
        "setup_log": str(setup_log),
        "workspace": str(work),
        "probes": probe_results,
    }
    manifest_path = env_root / "materialized_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--submitter-config", required=True, type=Path)
    parser.add_argument("--output-root", default=Path("/tmp/mlperf_host_replay"), type=Path)
    parser.add_argument("--repo-root", default=Path.cwd(), type=Path)
    args = parser.parse_args()

    manifest = materialize(
        environment_path=args.environment,
        submitter_path=args.submitter_config,
        output_root=args.output_root,
        repo_root=args.repo_root.resolve(),
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
