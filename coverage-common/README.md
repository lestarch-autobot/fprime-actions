# nasa/fprime-actions/coverage-common

Shared coverage-generation steps used by [`coverage-check`](../coverage-check/)
(PR-only) and [`coverage-update`](../coverage-update/) (push-only).

Most users should not invoke this action directly; use `coverage-check` or
`coverage-update` instead. It is exposed as its own action so the
discover / global-coverage / per-module steps stay in one place and so that
custom workflows can reuse it if needed.

## Prerequisites

The caller must have already generated and built the UT cache. Use
`run-unit-tests` with `run-check: 'false'`:

```yaml
- uses: nasa/fprime-actions/run-unit-tests@devel
  with:
    run-check: 'false'
    jobs: random
- uses: nasa/fprime-actions/coverage-common@devel
```

gcovr is provided by `fprime-tools`'s pip dependencies (via the `setup`
action).

### Runner version (important)

**Pin the job to `runs-on: ubuntu-24.04` (or newer).**  Ubuntu 22.04 runners
ship `gcc-11` / `gcov-11`, which has two known regressions that make this
action unusable on real-world F´ projects:

* Counter overflow ([gcc#68080](https://gcc.gnu.org/bugzilla/show_bug.cgi?id=68080)) crashes gcovr on heavily-exercised branches.
* Pathological symbol-table walks on heavily-templated test code can turn a 30-second coverage run into 30+ minutes (in the wild we have seen single `.gcda` files take 22 minutes of single-threaded CPU each).

Both are fixed in `gcc-12` and later; `ubuntu-24.04` ships `gcc-13` by
default so this is the path of least resistance.  The action passes
`--gcov-ignore-parse-errors=negative_hits.warn_once_per_file` to gcovr as a
defensive measure against the counter-overflow bug, but there is nothing
it can do about the symbol-walk slowness short of upgrading the toolchain.
If you must run on `ubuntu-22.04` for unrelated reasons, expect coverage
runs to be 10-60x slower (or hang outright) on projects with templated
test fixtures.

## What it does

1. Discovers F´ modules by grepping every `CMakeLists.txt` under
   `working-directory` for `register_fprime_module(`. Modules that also call
   `register_fprime_ut(` are eligible for coverage.
2. Runs `fprime-util check --all --coverage` once for the global headline
   number. Passes `--gcov-ignore-parse-errors=negative_hits.warn_once_per_file`
   to gcovr as defense against counter-overflow bugs.
3. Runs `fprime-util check --coverage` in each module directory with a UT.
4. Renames each module's `coverage.html` to `index.html`. The global
   `coverage-all.html` is **not** renamed.

## Inputs

| Input               | Default | Description                                                                          |
|---------------------|---------|--------------------------------------------------------------------------------------|
| `working-directory` | `.`     | Directory to run `fprime-util` from.                                                  |
| `target-platform`   | `""`    | Target platform/toolchain passed to `fprime-util`.                                    |
| `jobs`              | `""`    | Parallel job count for check. `random` picks 1-32 each run; empty omits `-j`.        |
| `strict`            | `false` | When `true`, fail the action if any module's UT/coverage build fails. Default `false` so per-module failures surface as missing `summary.json` (rendered as "no coverage" downstream) without failing CI. |
| `debug`             | `false` | When `true`, forward gcovr's `-v` verbose output for global and per-module steps. Useful for diagnosing slow or stuck coverage runs. |

## Outputs

| Output          | Description                                                       |
|-----------------|-------------------------------------------------------------------|
| `modules-jsonl` | Absolute path to the JSON-Lines file listing discovered modules.  |

## Scripts (under `scripts/`)

* `discover.py` &mdash; emits JSON-Lines of `{path, has_ut}` for each module.
* `run_coverage.py` &mdash; runs `fprime-util check --coverage` per module.
  Exits 0 by default even if individual modules fail (lenient); pass
  `--strict` (or set the `strict` input on the action to `'true'`) to
  propagate per-module failures as a non-zero exit.
* `compare.py` &mdash; PR-side delta + sticky comment markdown (used by
  `coverage-check`).
* `mirror.py` &mdash; copies coverage outputs into the baseline worktree,
  writes placeholder pages, invokes `catalog.py` (used by `coverage-update`).
* `catalog.py` &mdash; produces `catalog.json` + folder-tree `index.html`.
* `_summary.py` &mdash; shared gcovr `--json-summary` reader.

## Tests

```bash
python3 coverage-common/tests/test_coverage.py
```

No external dependencies; the test runner exits non-zero on the first failure.
