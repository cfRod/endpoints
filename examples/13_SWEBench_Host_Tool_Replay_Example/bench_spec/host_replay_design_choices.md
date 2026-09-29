<!-- SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Host Replay Benchmark Design Choices

This note describes the benchmark direction behind the host tool replay PoC.
The goal is to measure real host-side tool execution while keeping the agent
work fixed: fixed trajectory, fixed environment, declared submitter knobs, and
validation for completed work.

An agentic host can do several kinds of work around a model call: sandbox
provisioning, environment setup, orchestration, routing, state movement,
pre-processing, post-processing, tool execution, validation, and teardown.
Those activities all matter for end-to-end agent capacity. This PoC measures a
narrower slice: replayed host tool calls inside prepared task environments.

The timed path is the frozen tool command sequence, executed on the host and
validated step by step. Setup/materialization happens before the reported
replay window. Sandbox lifecycle, orchestration, routing, and
pre/post-processing are adjacent host activities that could be added later, but
they are left out of this PoC to keep the first benchmark shape focused on
replayed tool execution.

The host replay path complements the current Agentic Inference benchmark in the endpoints repository:

```text
frozen tool command
  -> real host execution
  -> validation
  -> next frozen tool command
```

The PoC is not a final MLPerf benchmark mode. It uses scripts and curated
trajectories to show which parts of the workload are fixed, which parts a
submitter can tune, and what still needs endpoint integration.

## Benchmark Idea

This proposal measures the host-side work inside agentic workflows. The model
is kept out of the timing loop. Instead, the benchmark replays fixed tool
trajectories that came from agent runs and checks that the requested work still
completed correctly.

At a high level, a run is:

1. prepare the fixed task environments outside the reported replay window;
2. choose an active agent count for the benchmark point;
3. issue trajectories according to the selected load-generation policy;
4. execute the frozen tool commands for each trajectory;
5. validate each trajectory;
6. report valid completed trajectory throughput at that active agent count.

The goal is not to benchmark one tool such as a compiler. The goal is to
measure how a host system handles the mix of shell, repository, build, test,
filesystem, runtime, and validation work that appears in agentic workflows.

The rest of this document describes the MLPerf-style rules needed to make that
simple idea reproducible: fixed workload definition, allowed submitter
configuration, validation, load generation, and reporting.

## Dataset Construction And Trajectory Classes

The official dataset should be constructed from frozen trajectories that
represent the host-side work seen in agentic systems. The dataset should not be
all one kind of task. It should include short inspection/edit flows, medium
mixed tool flows, and heavier build/test/package flows.

This PoC uses LIGHT, MEDIUM, and HEAVY labels to highlight why the dataset
needs a mix of trajectory shapes:

- LIGHT: short inspection, navigation, targeted edit, or targeted test work;
- MEDIUM: broader search/edit/test loops or mixed tool execution that is
  longer than a simple inspection task but not dominated by a sustained build;
- HEAVY: sustained build, test, package, code generation, or similar host-tool
  work with higher CPU, filesystem, process-launch, or dependency pressure.

The labels are useful for explaining the selected workload mix and analyzing
results after a run. They are not required runtime inputs to the scheduler. A
realistic submitted system should not need to know in advance that the next
trajectory is LIGHT, MEDIUM, or HEAVY.

The default execution model should use a fixed benchmark-provided job list.
The runner issues work from that list according to the load-generation policy.

## What Is Fixed

For a reported benchmark point, the proposal would fix the task definition:

- the trajectory: the tool commands and their order;
- the task environment: source state, setup steps, and dependency inputs;
- the success criteria: validators and required final state;
- the issued workload: the benchmark-provided job list and workload mix.

The job list connects the fixed artifacts:

```json
{
  "job_id": "fmtlib_3248",
  "trajectory": "trajectories/fmtlib_3248/host_replay.jsonl",
  "environment": "environments/fmtlib_3248_git.json",
  "submitter_config": "submitter_configs/fmtlib_3248_default.json"
}
```

The intent is that submitters tune execution without changing the task. A
submitted run should not use a different repo, skip commands, change test
targets, narrow search paths, rewrite frozen commands, or weaken validation for
the same benchmark point.

The environment manifest is the main contract for fixed setup. It can record
source state, setup commands, probes, dependency policy, software constraints,
and declared submitter knobs.

Each trajectory runs in its own mutable workspace. Steps are stateful within a
trajectory, so edits, generated files, rebuilds, test artifacts, and cache
effects carry forward. An active slot is one concurrent runner worker: it owns
one trajectory workspace at a time, runs that trajectory to completion, then
takes the next job from the fixed queue. Active slots must not share mutable
trajectory state.
Shared state should be limited to immutable inputs such as local mirrors or
read-only dependency caches.

Replay should not depend on public network state. Package managers, if used,
would run against fixed benchmark sources such as vendored dependencies, local
mirrors, or prebuilt caches.

The PoC trajectories are Linux `/bin/bash` trajectories. The command stream
uses GNU/POSIX-like tools such as `grep`, `sed`, `find`, `git`, `cmake`, `make`,
`ctest`, `python3`, heredocs, `rm`, and `uname`. Cross-vendor support means
Linux-capable systems across Arm, x86, and other architectures. It does not
mean Windows `cmd.exe` support, and it does not assume macOS where BSD/GNU tool
behavior can differ.

## What The Submitter Can Change

Submitters can tune declared knobs, but they cannot change the work being
measured. The benchmark may expose knobs such as:

- build/test parallelism, for example `cmake_jobs`, `make_jobs`, or
  `ctest_jobs`;
- executable paths, for example `cc`, `cxx`, or `cmake`, when the manifest
  allows them;
- compiler, runtime, linker, or package-manager versions allowed by the
  declared compatibility rules;
- system policy such as CPU affinity, NUMA placement, cgroups, cpusets, SMT,
  frequency/governor, boost/turbo, filesystem, storage, kernel, OS, and
  scheduler settings, plus benchmark-defined use of local mirrors and read-only
  dependency or build caches.

Submitters can either set explicit system policy or rely on the OS scheduler
and runtime defaults. That choice is part of the submitted system. In both
cases, the fixed workload, queue, required work, validation, and isolation rules
must stay unchanged.

Declared knobs can change values, not command meaning. For example, if a
trajectory exposes `cmake_jobs`, the submitter can choose `N` in:

```text
cmake --build build --target core-test -j N
```

They cannot change the target, skip the build, replace the build system, use
prebuilt artifacts instead of required compilation, or change dependency
contents.

Toolchain changes need a declared compatibility rule. A trajectory captured
with GCC is not automatically the same benchmark point with Clang just because
the tests pass. The same applies to substitutions such as `libstdc++ -> libc++`,
`ld.bfd -> lld`, or `make -> ninja`. Version updates inside the same family can
be allowed when the trajectory declares that range, the exact tool version is
recorded, and all setup/replay validation passes.

## How Validity Is Checked

Every replay row has a `validates` block. The PoC uses:

| Validator | Checks |
| --- | --- |
| `exit_code` | expected exit code |
| `stdout_contains` | required bounded text patterns |
| `file_contains` | required file content after a mutation |
| `git_diff_contains` | expected paths or hunks are present |

Simple inspection steps can use exit-code checks. Steps that mutate source,
build targets, run tests, or produce final state should use stronger stdout,
file-content, artifact, or diff checks.

The replay loop is:

```text
read frozen step
execute frozen command in the trajectory workspace
capture exit code, stdout, stderr, timestamps, and output previews/digests
run the declared validator
mark the trajectory invalid if a required validator fails
```

A valid completed trajectory means:

```text
every required step executed in order
every required validator passed
no disallowed command, environment, or tool substitution occurred
final state validation passed
```

Only valid completed trajectories contribute to the reported result.

Validators can intentionally require intermediate broken states if the original
trajectory produced that state and later recovered. The per-step question is
whether the command produced the expected state transition, not whether every
intermediate state is already a final solved task.

This validation layer is different from SWE-style final correctness checks.
SWE-Bench and Multi-SWE style validation usually answer whether the final patch
solves the task. That remains useful, but a host benchmark also needs to prove
that measured build, test, package, mutation, artifact, and final-state work
actually happened.

It is also different from AgentSysPerf-style replay checks
(`https://github.com/intel/agentsysperf`). Those replay checks are useful for
confirming that a recorded command stream completed and for collecting
timing/resource metrics. For a host benchmark, each workload class also needs
clear checks for the host work it is meant to measure, so a fast run that
skipped or changed required build, test, package, mutation, or artifact work
does not count.

Production validation would likely be defined by workload class rather than by
one exhaustive validator list. C/C++ build flows, Python test flows, package
setup, generated artifacts, and final repository state may need different
checks.

For cross-architecture runs, a trajectory should mean the same task on every
supported architecture: same source state, dependency contents, command intent,
required targets/tests, parameters, and validators. The base image, setup
bundle, compiler path, or prebuilt dependency archive can differ by
architecture when needed, as long as they provide equivalent dependencies and
execute the same required work.

Across architectures, some outputs may not be byte-identical even when the same
work was done correctly. For example, an Arm binary and an x86 binary may have
different hashes. In those cases, validators should check the semantic outcome:
the required target was built, the required test passed, the expected file
content exists, or the expected patch/state is present.

## What The PoC Currently Proves

The checked-in PoC currently shows:

- frozen JSONL replay of Linux `/bin/bash` tool commands;
- private workspace materialization and stateful replay per trajectory;
- per-step validation and valid completed trajectory throughput metrics;
- validation of submitter-provided tuning values, such as build parallelism,
  compiler choice, and placement settings.

The PoC can be used to run a small heterogeneous contention experiment:

```text
single_light_all_cores:   L
single_medium_all_cores:  M
single_heavy_all_cores:   H
mixed_all_cores:          L M H
mixed_heavy_make_jobs_N:  L M H, with only the HEAVY make_jobs value changed
```

The single-trajectory runs show each class running alone with the default
all-core submitter config. The mixed run shows whether the same trajectories
behave differently when launched together. The `make_jobs` sweep then changes
one declared submitter knob for the HEAVY trajectory while leaving the LIGHT
and MEDIUM trajectories unchanged.

The comparison should keep host, runner, setup inputs, validation, and the job
list fixed. Setup/materialization should happen outside the reported replay
timing window.

The primary result should be valid completed trajectory throughput as active
agent count increases:

```text
valid completed trajectories/sec
^
|                         *
|                    *
|               *
|          *
|     *
+------------------------------------> active agents
      1        8       16       32
```

This approach tests the host under load. In real agentic systems, tool calls do
not run one at a time in isolation; multiple agents can be building, testing,
searching, editing, and validating at the same time.

This curve characterizes host capacity for agentic tool execution. If the line
rises nearly linearly, the host has available capacity for more concurrent
agent replays. If the line flattens, the host is approaching saturation in one
or more resources such as CPU, process launch, filesystem, memory bandwidth,
cache, or storage. If throughput drops or validity falls, the active-agent
level is beyond what the submitted system can sustain for that workload.

Validation gates throughput. A trajectory that times out, fails validation,
skips required work, or uses a disallowed substitution contributes zero valid
completed trajectories.

Replay latency could be reported as supporting information, especially when
explaining whether higher throughput comes with much slower individual
trajectory progress. For mixed workloads, latency can be broken down after the
run by dataset class. Aggregate tail-latency numbers may be dominated by the
longest trajectories, so class-level breakdowns can make the result easier to
interpret.

For a mixed-workload point, the queue would be fixed. Submitters can tune
declared parameters and placement, but not reorder the queue or choose an easier
trajectory mix for the same point. Each active slot runs one trajectory at a
time; when a slot completes, the scheduler issues the next job from the fixed
queue.

## Load Generation Strategy

Rolling fixed-queue execution is closer to the current multi-turn endpoint load
generator:

```text
prepare a fixed queue of K trajectories
start A active slots
each slot runs one trajectory at a time
when a slot finishes, issue the next trajectory from the fixed queue
stop when the fixed queue or measurement policy is complete
```

In this model, `A` is the active-agent count for the benchmark point. `K` is
the fixed amount of work to issue at that point. If `K` is larger than `A`, the
load generator keeps replacing completed trajectories until the fixed queue is
empty or the measurement window ends. This keeps up to `A` active agents in
flight for most of the run, so the host sees a steadier load.

In the PoC runner, `A` is controlled by `--target-concurrency`. One active
slot corresponds to one active agent replay. The job list controls the
workload mix by listing the trajectories to issue. For example, an 8-agent
point uses `--target-concurrency 8` and a fixed job list containing the desired
eight initial jobs, such as four LIGHT jobs, two MEDIUM jobs, and two HEAVY
jobs. A 3-agent mixed point uses `--target-concurrency 3` and a three-job list,
such as one LIGHT `django_16333`, one MEDIUM `pylint_6903`, and one HEAVY
`protobuf_7cd0b6f`.

The queue is fixed by the workload definition. The submitter may tune declared
knobs and placement, but cannot reorder the queue, choose easier trajectories,
drop long trajectories, or replenish one workload class more aggressively than
another. The scheduler's job is only to issue the next queued trajectory to the
next available active slot.

This rolling strategy is the more natural endpoint benchmark mode. A throughput
curve can be built by varying `A`.

For each point, the benchmark reports valid completed trajectories per
measurement interval. Replay latency can be reported as supporting information,
including class-level latency after the run. Validation still gates throughput:
failed, invalid, timed-out, or disallowed trajectories do not count as valid
completed work.
