# nasa/fprime-actions/ci-gatekeeper

Approve held workflow runs on fork pull requests the autobot review has
cleared.  GitHub holds every workflow run on a PR from a first-time
contributor until a maintainer clicks **Approve and run**; this action
performs that click on a schedule, but only for a PR whose *current head*
carries a summary review with **CI safety: Go** from an allow-listed autobot,
and only when the repository's Actions queue is calm.

The logic is deterministic — regular expressions over the summary review
body documented in nasa/fprime's
[`review-contract.md`](https://github.com/nasa/fprime/blob/devel/.github/agents/_shared/review-contract.md)
and
[`review-summary.agent.md`](https://github.com/nasa/fprime/blob/devel/.github/agents/review-summary.agent.md).
No LLM is involved.  Anything that is not an exact `Go` is treated as *no
verdict* (fail closed).

## Algorithm

1. **Queue check first.**  Count the repository's `queued` + `in_progress`
   workflow runs (this gatekeeper's own run excluded).  If the count is at or
   above `busy-threshold`, write `busy (N runs), no action` to the step
   summary and exit 0.  No PR, run list, or review is fetched in this branch.
2. **Candidate discovery.**  List open, non-draft PRs.  For each head SHA
   query `GET /actions/runs?head_sha=<sha>&status=action_required`; PRs with
   no held run are skipped (their reviews are never fetched).
3. **Verdict parse.**  Fetch the PR's reviews.  Keep reviews whose author is
   in `reviewers` **and** whose body contains the literal marker
   `<!-- fprime-review-summary v1 -->`, dropping `DISMISSED` ones; take the
   latest by `submitted_at`.  Then, on that body (CRLF normalised to LF):

   | Rule | Pattern / check |
   |---|---|
   | reviewed head | `^\s*<!--\s*reviewed_head:\s*([0-9a-fA-F]{40})\s*-->\s*$` (multiline).  Must equal the PR's current head SHA exactly, full 40 hex, case-insensitive.  Several lines must all agree.  If the line is absent the review's `commit_id` is used, as contract §2 directs (`commit_id` is the head at submission time and is never newer than the body). |
   | CI safety row | `^\s*\|\s*\*\*CI safety\*\*\s*\|(?:\s*[—–-]*\s*\|){5}(?P<cell>[^\|\n]*)\|\s*$` (multiline) — the table row with five dash-only count cells; the last cell is the verdict. |
   | Go | the verdict cell, stripped, matches `^\*\*Go\*\*$` — nothing else in the cell. |
   | No-Go | the cell matches `^\*\*No-Go\*\*(?:\s*[—–-]+\s*(?P<rationale>.*))?$`; the rationale is reported. |
   | anything else | `**go**`, `Go`, `**Go** — note`, a missing row, a row with the wrong number of cells, or a cell that is neither of the above → no verdict. |
   | multiple rows | every CI safety row must say `**Go**`. |
   | hidden metadata | `^\s*<!--\s*ci_safety:\s*(\S+)\s*-->\s*$` (multiline).  Optional; when present every such line must be exactly `Go` (`ci_safety_rationale` lines do not match). |
   | Recommend: Close | `^\s*(?:#{1,6}\s*Recommend:\s*Close\s*$|\*\*Recommend:\s*Close\*\*)` (multiline) anywhere in the body → not eligible. |

   The `**Totals**` row and the per-agent `Verdict` cells are never consulted.
4. **Selection.**  Eligible PRs sorted by PR number ascending (oldest first);
   the first `max-approvals` (default 1) are acted on.
5. **Act.**  `POST /repos/{repo}/actions/runs/{run_id}/approve` for every
   held run of the selected PR(s), pausing `approve-delay-seconds` between
   calls.  `404`/`409`/`422` (already approved, no longer pending) are
   logged as skipped; any other error is reported and fails the step.  In
   dry-run (the default) nothing is posted and the client refuses writes.
6. **Step summary.**  Queue count and threshold, then one row per candidate
   PR (held runs, verdict, reason with a link to the review consulted, action
   taken or that would be taken).

## Safety properties

* **Queue first.**  A busy queue ends the run before any PR is read.
* **Fail closed.**  Only the exact token `**Go**` in the CI safety row of
  the latest allow-listed summary review for the exact current head counts.
* **One PR per run** by default, oldest first.
* **Dry-run by default.**  The REST client refuses `POST` unless
  `dry-run: false` is passed.
* **Idempotent.**  Runs already approved come back `404`/`409` and are
  skipped; a later run finds nothing held for that PR.
* **Auditable.**  Every candidate and decision is listed in
  `$GITHUB_STEP_SUMMARY` and the job log.

## Inputs

| Input | Default | Description |
|---|---|---|
| `token` | *(required)* | Token with Actions: write and Pull requests: read (see below) |
| `repository` | `${{ github.repository }}` | Repository whose PRs are gated |
| `busy-threshold` | `10` | Queue size (queued + in-progress, own run excluded) at or above which the run does nothing |
| `reviewers` | `lestarch-autobot,thomas-bc-autowot` | Allow-listed summary review authors |
| `dry-run` | `true` | `false` to approve; anything else only reports |
| `max-approvals` | `1` | Eligible PRs (oldest first) acted on per run |
| `approve-delay-seconds` | `1` | Pause after each approve call |

Outputs: `busy`, `queue-count`, `candidates`, `eligible`, `selected-pr`,
`approved`, `failed`.

### Token

The GitHub REST documentation for
[Approve a workflow run for a fork pull request](https://docs.github.com/en/rest/actions/workflow-runs#approve-a-workflow-run-for-a-fork-pull-request)
lists GitHub App installation access tokens with the *Actions: write*
repository permission as supported, so `${{ secrets.GITHUB_TOKEN }}` in a
workflow declaring `permissions: actions: write` qualifies.  A classic PAT
needs the `repo` scope.

## Usage

```yaml
on:
  schedule:
    - cron: '*/20 * * * *'
  workflow_dispatch:
    inputs:
      dry_run:
        type: boolean
        default: true

permissions:
  actions: write
  pull-requests: read
  contents: read

jobs:
  gatekeeper:
    runs-on: ubuntu-latest
    steps:
      - uses: nasa/fprime-actions/ci-gatekeeper@devel
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
          dry-run: ${{ github.event_name != 'schedule' && inputs.dry_run }}
```

Scheduled runs act; a `workflow_dispatch` run with the default
`dry_run: true` previews the candidate table without approving anything.

## Running locally

```bash
export GITHUB_TOKEN=<token>
python3 ci-gatekeeper/scripts/ci_gatekeeper.py --repository nasa/fprime                        # dry-run
python3 ci-gatekeeper/scripts/ci_gatekeeper.py --repository nasa/fprime --busy-threshold 1000  # dry-run, ignore the queue
python3 ci-gatekeeper/scripts/ci_gatekeeper.py --repository nasa/fprime --apply                # approve
```

## Tests

```bash
python3 ci-gatekeeper/tests/test_ci_gatekeeper.py
```

The tests cover the parse rules (exact Go; No-Go; No-Go whose rationale
contains "Go"; lowercase `go`; missing CI-safety row; missing summary
marker; stale `reviewed_head`; abbreviated SHA; wrong author; Recommend:
Close; `ci_safety` metadata disagreeing with the table; multiple rows;
CRLF bodies; dismissed and superseded reviews) and the orchestration (busy
short-circuit issues no PR request; own-run exclusion; oldest-first
selection; dry-run write guard; idempotent 409 handling; pacing) against an
in-memory fake of the REST API.  No network access is needed.
