<!-- SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fmtlib_3248 Host Replay Walkthrough

This walkthrough follows one representative trajectory from the scheduler input
to the validated replay output.

The example is `fmtlib_3248` because it covers the core mechanics:

- fixed Git repository and base commit;
- CMake setup materialization;
- submitter-controlled `cmake_jobs`;
- stateful source edits;
- per-step validation;
- final diff validation;
- private workspace per scheduled job.

## 1. Job List

The scheduler starts from:

```text
examples/13_SWEBench_Host_Tool_Replay_Example/host_replay_jobs_fmtlib.json
```

It connects the three required artifacts:

```json
{
  "job_id": "fmtlib_3248_default",
  "trajectory": "examples/13_SWEBench_Host_Tool_Replay_Example/trajectories/fmtlib_3248/host_replay.jsonl",
  "environment": "examples/13_SWEBench_Host_Tool_Replay_Example/environments/fmtlib_3248_git.json",
  "submitter_config": "examples/13_SWEBench_Host_Tool_Replay_Example/submitter_configs/fmtlib_3248_default.json"
}
```

Meaning:

- `trajectory` is the frozen tool-call stream.
- `environment` defines the fixed source/setup contract.
- `submitter_config` provides values for allowed knobs only.

## 2. Environment Manifest

The manifest fixes the source:

```text
examples/13_SWEBench_Host_Tool_Replay_Example/environments/fmtlib_3248_git.json
```

```json
"source": {
  "kind": "git",
  "repo": "fmtlib/fmt",
  "repo_url": "https://github.com/fmtlib/fmt.git",
  "base_commit": "275b4b3417e26be3bdb5b45e16fa9af6584973a2"
}
```

The materializer clones that repository and checks out the fixed base commit in
a private output directory. Submitters do not choose a different repository or
commit for this trajectory.

The manifest also states what is fixed:

```json
"fixed": {
  "source": true,
  "base_commit": true,
  "replay_commands": true,
  "validation": true,
  "network_during_replay": false,
  "cmake_targets": ["format-test", "core-test"]
}
```

## 3. Submitter Config

The manifest declares the allowed knobs. For this trajectory, the key ones are:

```json
"configurable": {
  "cc": {"type": "executable", "default": "gcc"},
  "cxx": {"type": "executable", "default": "g++"},
  "cmake": {"type": "executable", "default": "cmake"},
  "cmake_jobs": {
    "type": "integer",
    "default": 2,
    "min": 1,
    "max": "host_logical_cpus"
  },
  "cpu_affinity": {"type": "string", "default": "", "pattern": "|[0-9,-]+"},
  "numa_cpunodebind": {"type": "string", "default": "", "pattern": "|[0-9,-]+"},
  "numa_membind": {"type": "string", "default": "", "pattern": "|[0-9,-]+"}
}
```

The default submitter config is:

```text
examples/13_SWEBench_Host_Tool_Replay_Example/submitter_configs/fmtlib_3248_default.json
```

```json
"values": {
  "cc": "gcc",
  "cxx": "g++",
  "cmake": "cmake",
  "cmake_jobs": 2,
  "cpu_affinity": "",
  "numa_cpunodebind": "",
  "numa_membind": ""
}
```

The runner rejects undeclared knobs. It also validates integer ranges, executable
presence, and placement string formats before setup/replay.

## 4. Setup

Setup commands are templates from the environment manifest:

```json
"setup": [
  "{cmake} -S \"{work}\" -B \"{work}/build\" -G \"Unix Makefiles\" -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=\"{cc}\" -DCMAKE_CXX_COMPILER=\"{cxx}\"",
  "{cmake} --build \"{work}/build\" --target format-test core-test -j \"{cmake_jobs}\""
]
```

With the default submitter config, the second setup command uses:

```text
cmake_jobs = 2
```

The generated build command is therefore:

```bash
cmake --build "<workspace>/build" --target format-test core-test -j "2"
```

This is a valid submitter knob because it changes build parallelism without
changing the target set or validation criteria.

## 5. Replay

The frozen replay file is:

```text
examples/13_SWEBench_Host_Tool_Replay_Example/trajectories/fmtlib_3248/host_replay.jsonl
```

Each line is one tool step. A typical row has:

```json
{
  "trajectory_id": "fmtlib__fmt-3248",
  "turn": 2,
  "tool_type": "bash",
  "command": "pwd; ls; git status --short; find . -maxdepth 2 -type d | head -30",
  "cwd": ".",
  "timeout_s": 900.0,
  "validates": {
    "kind": "stdout_contains",
    "patterns": ["include", "test"]
  }
}
```

Replay behavior:

```text
execute the frozen command in the materialized workspace
validate the result
record an event
continue with the mutated workspace state
```

Replay commands can consume declared submitter knobs through fixed environment
variables. For example:

```bash
cmake --build build --target format-test -j "${MLPERF_CMAKE_JOBS:-2}"
```

The command shape is fixed. The submitted value of `MLPERF_CMAKE_JOBS` is
validated through the manifest/config path.

## 6. Validation

Validation is encoded in each trajectory row.

Examples:

```json
{"kind": "exit_code", "expected": 0}
```

```json
{"kind": "stdout_contains", "patterns": ["rc=0"]}
```

```json
{
  "kind": "git_diff_contains",
  "paths": ["include/fmt/core.h", "test/format-test.cc"],
  "patterns": [
    "if (specs_.align == align::none) {",
    "EXPECT_EQ(\"-42   \", fmt::format(\"{:<06}\", -42));"
  ]
}
```

If validation fails, the step is invalid and the trajectory stops by default.

## 7. Run The Example

From the repository root:

```bash
python3 scripts/run_host_replay_slots.py \
  --jobs examples/13_SWEBench_Host_Tool_Replay_Example/host_replay_jobs_fmtlib.json \
  --output-dir /tmp/fmtlib_slot_run \
  --target-concurrency 1 \
  --allow-mutating
```

Inspect:

```text
/tmp/fmtlib_slot_run/slots/slot_000/0000_fmtlib_3248_default/
  setup/fmtlib-3248-cmake-cpp/setup.log
  setup/fmtlib-3248-cmake-cpp/materialized_manifest.json
  setup/fmtlib-3248-cmake-cpp/workspace/work/
  replay/events.jsonl
  replay/summary.json
  job_result.json
```

The checked-in example data is not mutated. The replay mutates only the private
workspace created under the output directory.
