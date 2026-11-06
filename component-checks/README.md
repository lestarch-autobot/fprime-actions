# nasa/fprime-actions/component-checks

Component checklist checks: a set of standalone CI-gate scripts covering the
F´ component development checklist (requirements, design, implementation,
unit testing, close-out), plus a composite action that publishes per-module
results to the baseline branch (`coverage/<ref-name>`) and adds a **Checks**
badge column to the checklist landing page, next to the existing coverage
and CodeQL (static analysis) badges.

Post-merge/git-side steps of the checklist (test code committed to git, CI
pipeline status) are intentionally **not** implemented here.

## The check scripts

Every script lives in [`scripts/`](scripts/), takes `--module <dir>`, prints
a clear, actionable message per problem, and exits non-zero on failure so it
can run directly as a CI gate. Each also supports `--json-output <file>` to
append a JSON-Lines record for aggregation, and `--requirements <file>` to
override the requirements source (default: the module's `docs/*.md` /
`docs/*.csv` requirement tables).

### Requirements

| ID | Script                       | Verifies                                                     |
|----|------------------------------|--------------------------------------------------------------|
| R1 | `check_req_commands.py`      | A requirement references every FPP command                   |
| R2 | `check_req_telemetry.py`     | A requirement references every telemetry channel             |
| R3 | `check_req_events.py`        | A requirement references every event (EVR)                   |
| R4 | `check_req_parameters.py`    | A requirement references every parameter                     |
| R5 | `check_req_data_products.py` | A requirement references every data product                  |
| R6 | `check_req_ports.py`         | A requirement references every port/interface                |
| R7 | `check_req_parent_trace.py`  | Every requirement traces to a parent (Parent/Trace column)   |
| R8 | `check_req_verification.py`  | Every requirement has a verification method (T/A/I/D)        |
| R9 | `check_req_kinds.py`         | Behavior/Input/Output/Memory/HW-I/O/Performance kinds each covered by a requirement or waived in the SDD (`Kind` column) |

### Design

| ID | Script                        | Verifies                                                     |
|----|-------------------------------|--------------------------------------------------------------|
| D1 | `check_port_handlers.py`      | Every typed input port has a `<port>_handler` override       |
| D2 | `check_command_handlers.py`   | Every command has a `<CMD>_cmdHandler`                       |
| D3 | `check_sm_actions.py`         | Every state-machine action has an `action_<name>` impl       |
| D4 | `check_dp_priorities.py`      | Every data product container has a `default priority`        |
| D5 | `check_req_design_mapping.py` | Every requirement maps to a design artifact                  |
| D6 | `check_fpp_generate.py`       | FPP passes `fpp-check` (and optionally `fprime-util build`)  |
| D7 | `check_sdd_port_table.py`     | SDD port table lists every typed port with matching kind/direction and type |
| D8 | `check_sdd_dictionary_tables.py` | SDD command/telemetry/event/parameter/data-product tables match the FPP model |
| D9 | `check_sdd_sections.py`       | SDD has a block diagram, sequence diagrams (or explicit N/A), names every state machine, states the component kind, and has an external-interfaces section |
| D10 | `check_sdd_port_behavior.py` | Every input port is described in SDD prose, not only in a table |

### Implementation

| ID | Script                              | Verifies                                                |
|----|-------------------------------------|----------------------------------------------------------|
| I1 | `check_include_dependencies.py`     | Source `#include` deps are declared in CMakeLists         |
| I2 | `check_build_warnings.py`           | Builds (or scans build logs) with zero compiler warnings  |
| I3 | `check_dp_priority_configurable.py` | Data product priorities configurable via command/param    |
| I4 | `check_static_analysis.py`          | No CodeQL SARIF findings at/above a severity threshold    |

### Unit testing

| ID | Script                             | Verifies                                                  |
|----|------------------------------------|------------------------------------------------------------|
| U1 | `check_ut_artifacts.py`            | UT results are published (e.g. on the coverage branch)     |
| U3 | `check_ut_include_dependencies.py` | UT `#include` deps are declared in the module's CMake      |
| U4 | `check_leaks.py`                   | `fprime-util check --leak` passes with no leaks            |
| U5 | `check_coverage_threshold.py`      | Line coverage meets a configurable threshold               |

### Close-out

| ID | Script                    | Verifies                                                       |
|----|---------------------------|----------------------------------------------------------------|
| C1 | `check_topology_build.py` | Component is instantiated and the topology builds per target   |
| C2 | `check_hardware_run.py`   | `docs/hardware-run.md` records Platform, Deployment, Commit (ancestor of HEAD), Date, Result PASS, Tester |
| C3 | `check_ci_status.py`      | GitHub check runs for `--repo`/`--sha` all completed successfully (needs `GITHUB_TOKEN`; `--check-runs-json` offline) |

The FPP model is read with a lightweight regex-level parser (no build cache
needed); the requirements source is the SDD requirement table
(`Requirement | Description | [Parent] | Verification Method`, plus optional
`Design` column) or a CSV with equivalent columns.

## The publish action

The composite action in this directory runs the *static* checks (R1-R8,
D1-D5, I1, I3, U3) for every module discovered by
`coverage-common/scripts/discover.py`, writes
`<module>/checks/summary.json` + `<module>/checks/index.html` to the
baseline branch, and regenerates `index.html`/`catalog.json` so every module
row gains a **Checks** badge (platinum: all pass; gold >= 90% pass;
silver >= 80%; bronze below).

Results from the build-level checks (D6, I2, I4, U1, U4, U5, C1), run as
earlier workflow steps with `--json-output`, can be merged in via the
`extra-results` input.

## Required permissions

```yaml
permissions:
  contents: write
```

## Inputs

| Input                    | Default                        | Description                                                    |
|--------------------------|--------------------------------|----------------------------------------------------------------|
| `working-directory`      | `.`                            | Repository checkout root (modules discovered here).            |
| `baseline-branch-prefix` | `coverage`                     | Prefix applied to `<ref-name>` to form the baseline branch.    |
| `checks-subdirectory`    | `checks`                       | Subdirectory under each module holding check results.          |
| `extra-results`          | `""`                           | Newline-separated JSON-Lines result files to merge.            |
| `config-file`            | `.github/module-checklist.yml` | Checklist config file (coverage tier thresholds; Checks badge cut-offs are fixed: platinum all pass, gold >= 90%, silver >= 80%). |

## Outputs

| Output            | Description                                              |
|-------------------|----------------------------------------------------------|
| `baseline-branch` | Name of the baseline branch written (e.g. `coverage/devel`). |

## Usage

```yaml
name: "Component Checks"
on:
  push:
    branches: [devel, release/**]
permissions:
  contents: write
jobs:
  component-checks:
    runs-on: ubuntu-latest
    concurrency:
      group: baseline-publish-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: nasa/fprime-actions/component-checks@devel
```

Run a single check locally:

```console
$ python3 component-checks/scripts/check_req_commands.py --module Svc/CmdDispatcher
```

Or everything as a gate:

```console
$ python3 coverage-common/scripts/discover.py --root . > /tmp/modules.jsonl
$ PYTHONPATH=component-checks/scripts python3 component-checks/scripts/run_checks.py \
    --root . --dest /tmp/checks --modules-jsonl /tmp/modules.jsonl --gate
```

## Tests

```console
$ python3 component-checks/tests/test_checks.py
```

No external dependencies; the runner exits non-zero on any failure.

