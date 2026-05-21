# nasa/fprime-actions/coverage-comment

Composite action that posts the sticky per-module coverage comment
produced by [`coverage-check`](../coverage-check/) on a pull request.

This is the **second stage** of the two-stage coverage flow:

1. [`coverage-check`](../coverage-check/) (on `pull_request`) computes
   the comment body and uploads it as a workflow artifact.
2. **`coverage-comment`** (on `workflow_run`) downloads the artifact
   and posts / updates the sticky PR comment.

The split exists because GitHub strips write permissions from
`GITHUB_TOKEN` on fork-triggered `pull_request` events, so the first
stage cannot post a comment when the PR is opened from a fork. A
`workflow_run` job runs in the *base* repo's context with a full token
and can safely post. Because no fork code executes in the
`workflow_run` job, granting it `pull-requests: write` is safe.

## Required permissions

```yaml
permissions:
  contents: read         # read repository metadata (downloading artifact)
  actions: read          # list and download artifacts from the triggering run
  pull-requests: write   # upsert the sticky comment
```

## What it does

1. Verifies the action was triggered by a `workflow_run` event.
2. Locates the artifact named `artifact-name` on the triggering
   workflow run.
3. Downloads and unzips the artifact (it contains `comment.md`,
   `regressions.json`, `pr-number.txt`, and `comment-marker.txt`).
4. Looks up existing PR comments matching the comment marker. If one
   exists it is edited in place; otherwise a new comment is created.

If the artifact is missing, expired, or malformed, the job fails with a
clear error and no comment is posted.

## Inputs

| Input            | Default                              | Description                                                                                            |
|------------------|--------------------------------------|--------------------------------------------------------------------------------------------------------|
| `artifact-name`  | `fprime-coverage-comment`            | Artifact name. Must match the `artifact-name` passed to `coverage-check` on the originating PR run.    |
| `comment-marker` | `<!-- fprime-coverage-comment -->`   | Fallback marker if the artifact lacks `comment-marker.txt`. New artifacts always carry their own marker. |

## Usage

The caller adds a separate workflow file that runs on `workflow_run`,
keyed on the workflow that contains `coverage-check`:

```yaml
name: "Coverage Comment"

on:
  workflow_run:
    workflows: ["Coverage Check"]
    types: [completed]

permissions:
  contents: read
  actions: read
  pull-requests: write

jobs:
  coverage-comment:
    runs-on: ubuntu-latest
    if: >
      github.event.workflow_run.event == 'pull_request' &&
      github.event.workflow_run.conclusion == 'success'
    steps:
      - uses: nasa/fprime-actions/coverage-comment@devel
```

The `if:` guard ensures the action only runs when the upstream coverage
check actually succeeded, avoiding stale comments on cancelled or
failed runs. If you want to post comments even when the originating run
failed (e.g. to surface partial coverage data), drop the
`conclusion == 'success'` clause.

## Why a separate workflow file (not just a separate job)

`workflow_run` is a top-level workflow trigger, not a job-level
`needs:` dependency. The triggering workflow (`Coverage Check`)
finishes entirely with the fork's read-only token before this workflow
starts under the base repo's elevated token. They are intentionally
isolated processes.

This isolation is what makes the pattern safe: no fork-supplied code,
artifacts (only declared paths), or environment variables cross the
boundary into the elevated-permissions context except through the
artifact's well-defined `pr-number.txt` / `comment.md` /
`comment-marker.txt` files, which this action validates before use.
