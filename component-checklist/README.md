# component-checklist

Fills the F Prime Component Development Checklist (v1.2.0) of every component
with [fprime-auto-checklist](https://github.com/LeStarch/fprime-auto-checklist)
and publishes it beside the coverage pages on the `coverage/<ref-name>`
baseline branch.

For each module holding an FPP component the action writes
`<module>/checklist/`:

| File | Contents |
|------|----------|
| `checklist.md`   | The filled checklist (the record) |
| `checklist.json` | The tool's machine-readable evidence |
| `index.html`     | The checklist rendered for the published site |
| `summary.json`   | Counts read by `coverage-common/scripts/catalog.py` |

The landing page links each component's checklist from its Checklist cell, and
the module page gains a "Component Development Checklist" row.

## Tool rows and reviewer rows

Rows a tool can decide (component checks, the fprime clang-tidy plugin) are
recomputed on every run.  Rows that need judgement stay `REVIEW` until a
reviewer (a person or an LLM) decides them with `fprime-auto-checklist decide`
and commits the checklist to the repository as `<module>/docs/checklist.md`
(`source-checklist`).  The action passes that file as `--previous`, so
reviewer decisions are carried into the published checklist, stamped with the
commit they were made at and flagged when the component changed since.  The
action never commits to the source branch.

## Grading

A component whose checklist is published is capped by it in the catalog's
overall tier:

| Checklist | Highest tier |
|-----------|--------------|
| None kept in the repository (`source-checklist` missing) | silver |
| Kept in the repository, some item not YES or N/A | gold |
| Kept in the repository, every item YES or N/A | platinum |

A reviewer's recorded override of a tool verdict (`fprime-auto-checklist
decide`, a note is required) counts as the reviewer's status.  Modules that
are not components are not affected.

## Static analysis

`analyzer: clang-tidy` (default) builds the fprime clang-tidy plugin against
LLVM 15 and runs it per component; it needs a built project with
`compile_commands.json` (`fprime-util generate -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
then `fprime-util build`).  It is published in addition to CodeQL, which
`codeql-publish` keeps publishing.  `analyzer: none` leaves the static-analysis
items `UNCHECKED`.

## Usage

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: nasa/fprime-actions/setup@devel
- run: fprime-util generate -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && fprime-util build -j"$(nproc)"
- uses: nasa/fprime-actions/component-checklist@devel
  with:
    compile-commands: build-fprime-automatic-native
```

The job needs `contents: write` to push the baseline branch.

## Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `working-directory` | `.` | Project checkout root |
| `baseline-branch-prefix` | `coverage` | Prefix of the baseline branches |
| `checklist-subdirectory` | `checklist` | Per-module publish subdirectory |
| `source-checklist` | `docs/checklist.md` | Checklist kept in the repository, relative to each component |
| `tool-repository` | `LeStarch/fprime-auto-checklist` | Repository of the tool |
| `tool-ref` | pinned SHA | Commit of the tool to install |
| `analyzer` | `clang-tidy` | `clang-tidy` or `none` |
| `compile-commands` | `""` | `compile_commands.json` or its build directory |
| `extra-args` | `""` | Extra arguments for `fprime-auto-checklist run` |
| `publish-from-forks` | `false` | Publish from a fork to its own baseline branch |
| `config-file` | `.github/module-checklist.yml` | Checklist config file |

## Tests

```
python3 component-checklist/tests/test_checklist.py
```
