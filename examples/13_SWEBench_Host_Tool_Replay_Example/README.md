<!-- SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Host Tool Replay PoC

This directory is a curated PoC subset for the proposed MLPerf host replay
benchmark. It carries selected frozen host-tool trajectories used to
show the design policies, not the final benchmark dataset.

## Benchmark Idea

The PoC measures the host-side work inside agentic workflows while keeping the
model out of the timing loop. It replays fixed tool trajectories from agent
runs, validates that the requested work completed, and reports valid completed
trajectory throughput over active agent count.

The goal is not to benchmark one tool such as a compiler. The goal is to show
how a host system handles the mix of shell, repository, build, test,
filesystem, runtime, and validation work that appears in agentic workflows.
The detailed MLPerf-style rules are in
`bench_spec/host_replay_design_choices.md`.

## What Is In This PoC

```text
trajectories/
  Frozen replay JSONL files.

environments/
  Fixed source/setup manifests and declared submitter knobs.

submitter_configs/
  Example values for allowed knobs.

host_replay_jobs_fmtlib.json
host_replay_jobs_representative.json
  Checked-in job lists for the smoke run and representative PoC run.

walkthroughs/
  Step-by-step run walkthroughs.

bench_spec/
  Benchmark policy and design notes.

tools/
  PoC-local experiment helpers.
```

## Key Trajectories

The curated trajectories are representative, not exhaustive. They cover several
host-tool patterns that the benchmark policy needs to handle.

| Trajectory | Replayed tool calls | Command surfaces | What it shows |
| --- | ---: | --- | --- |
| `django_16333` | 14 | `bash`, `git`, `grep`, `sed`, `python3`, `tests.runtests` | LIGHT Python/Django functional-test trajectory for short latency-sensitive work. |
| `pylint_6903` | 12 | `bash`, `git`, `grep`, `sed`, `find`, `python3`, `pytest` | MEDIUM Python inspection/edit/targeted-pytest trajectory for mixed workload and placement experiments. |
| `zeromicro_go_zero_964` | 20 | `bash`, `git`, `grep`, `sed`, `python3`, `go`, `gofmt`, `go test`, `go build` | HEAVY Go trajectory selected for CPU allocation experiments because its build/test work has broader CPU demand. |
| `protobuf_7cd0b6f` | 4 | `bash`, `git`, `grep`, `make`, `protoc` | HEAVY Autotools/Make build trajectory with `make_jobs=32` by default, intended to exercise build parallelism above small `-j2` examples. |
| `fmtlib_3248` | 17 | `bash`, `git`, `grep`, `sed`, `find`, `python3`, `cmake`, `make` | C++ CMake build/test replay with a declared `cmake_jobs` parameter, stateful source edits, and final diff validation. |

## How It Runs

```text
host_replay_jobs_fmtlib.json or host_replay_jobs_representative.json
  -> run_host_replay_slots.py
      -> run_single_host_replay_agent.py
          -> materialize_host_environment.py
              inputs: environment manifest + submitter config
              output: isolated workspace + setup artifacts
          -> agentic_host_tool_replay.py
              input: trajectory JSONL
              output: replay events + trajectory summary
      -> slot-level latency/completion summary
```

A job entry is the basic unit of work:

```json
{
  "job_id": "fmtlib_3248_default",
  "trajectory": "examples/13_SWEBench_Host_Tool_Replay_Example/trajectories/fmtlib_3248/host_replay.jsonl",
  "environment": "examples/13_SWEBench_Host_Tool_Replay_Example/environments/fmtlib_3248_git.json",
  "submitter_config": "examples/13_SWEBench_Host_Tool_Replay_Example/submitter_configs/fmtlib_3248_default.json"
}
```

The runner validates the submitter config, prepares a private workspace,
replays the frozen tool commands in order, checks each declared validator, and
writes latency/completion results. The checked-in trajectory data is not
mutated.

## Run With Docker

For a fresh machine, the PoC Dockerfile provides the expected Linux tools,
GCC/CMake/Make, Autotools, Go/`gofmt`, `protoc`, Python venv support,
`numactl`, and `taskset`.
Build it from the repository root:

```bash
docker build -f examples/13_SWEBench_Host_Tool_Replay_Example/Dockerfile \
  --build-arg USER_ID=$(id -u) \
  --build-arg GROUP_ID=$(id -g) \
  -t host-replay-poc \
  examples/13_SWEBench_Host_Tool_Replay_Example
```

For an explicit architecture build, use `docker buildx` with
`--platform linux/amd64` or `--platform linux/arm64`. Building for a
non-native architecture requires a buildx builder with binfmt/QEMU emulation or
a native builder for that architecture.

Run the container with the repository mounted:

```bash
docker run --rm -it \
  -v "$(pwd)":/workspace \
  -w /workspace \
  host-replay-poc bash
```

Then run a single fmtlib smoke example inside the container:

```bash
python3 scripts/run_host_replay_slots.py \
  --jobs examples/13_SWEBench_Host_Tool_Replay_Example/host_replay_jobs_fmtlib.json \
  --output-dir /tmp/fmtlib_slot_run \
  --target-concurrency 1 \
  --allow-mutating
```

To validate the representative five-trajectory set inside the same Docker
environment:

```bash
python3 scripts/run_host_replay_slots.py \
  --jobs examples/13_SWEBench_Host_Tool_Replay_Example/host_replay_jobs_representative.json \
  --output-dir /tmp/host_replay_representative_docker \
  --target-concurrency 1 \
  --allow-mutating
```

The expected successful PoC result is `failed_jobs: 0`,
`replay_metrics.valid_completed_trajectories: 5`, and
`replay_metrics.valid_trajectory_rate: 1.0`.

Inspect:

```text
/tmp/fmtlib_slot_run/slots/slot_000/0000_fmtlib_3248_default/
  setup/fmtlib-3248-cmake-cpp/setup.log
  setup/fmtlib-3248-cmake-cpp/materialized_manifest.json
  replay/events.jsonl
  replay/summary.json
  job_result.json
```

## Where To Read More

- `bench_spec/host_replay_design_choices.md`: benchmark policy and open design
  choices.
- `walkthroughs/host_replay_fmtlib_step_by_step.md`: concrete single-trajectory
  walkthrough.
- `host_replay_jobs_representative.json`: five-workload representative job
  list.
- `tools/run_heterogeneous_experiment.py`: LIGHT/MEDIUM/HEAVY experiment
  helper. It generates experiment job lists and outputs under `results/`, which
  is intentionally ignored by git.
