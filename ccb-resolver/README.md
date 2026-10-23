# nasa/fprime-actions/ccb-resolver

Apply Change Control Board (CCB) decisions recorded on the
[F´ Development](https://github.com/orgs/nasa/projects/21) GitHub Projects
(v2) board.  Maintainers record a decision in the board's **CCB Resolution**
field during the CCB meeting; this action sweeps the board afterwards and
performs the bookkeeping.

## Rules

Only items whose **Status** is `CCB` are considered.  For each of them:

| CCB Resolution | Linked content | Action |
|---|---|---|
| `Approved` | any | Set **Status** to `approved-status` (default `Backlog - Deferred`) |
| `Disapproved` | open issue | Post `disapproval-comment` on the issue, then close it as *not planned* |
| `Disapproved` | closed issue / PR | Set **Status** to `done-status` (default `Done`) |
| `Disapproved` | open PR, draft item | Nothing; listed in the summary as needing manual handling |
| `Tabled`, `Needs Study`, blank | any | Nothing |

Items whose Status is anything other than `CCB` are never touched: a human
has already moved them.  After a disapproved issue is closed the board's
built-in *"Item closed → Status: Done"* workflow (if enabled on the board)
moves the item; the `done-status` rule covers boards where it is not.

## Safety properties

* **Field definitions are never modified.**  The *only* project mutation the
  script issues is the per-item `updateProjectV2ItemFieldValue`.  Field and
  option IDs are resolved by name at runtime and the GraphQL client rejects
  any document naming `updateProjectV2Field`, `createProjectV2Field`,
  `deleteProjectV2Field`, or other definition/bulk mutations.
* **Dry-run by default.**  With `dry-run: true` (the default) the client
  refuses to send any mutation at all; the summary lists what *would* be
  done.  Writes require an explicit `dry-run: false`.
* **Idempotent.**  Re-running after a successful run finds nothing to do.
* **Rate-limit friendly.**  The whole board is paginated 100 items at a
  time and a short pause (`mutation-delay-seconds`) follows each mutation.
* **Auditable.**  Every item acted on, or that would be acted on, is listed
  in `$GITHUB_STEP_SUMMARY` and in the job log.

## Inputs

| Input | Default | Description |
|---|---|---|
| `token` | *(required)* | Token with project read/write and issues write (see below) |
| `project-owner` | `nasa` | Organization or user owning the project |
| `project-number` | `21` | Project number from the project URL |
| `dry-run` | `true` | `false` to write; anything else only reports |
| `disapproval-comment` | CCB scope message | Comment posted before closing a disapproved issue |
| `approved-status` | `Backlog - Deferred` | Status given to approved items |
| `done-status` | `Done` | Status given to disapproved items whose content is already closed |
| `mutation-delay-seconds` | `1` | Pause after each mutation |

Outputs: `planned`, `applied`, `failed`, `skipped` (counts).

### Token

`GITHUB_TOKEN` cannot read or write organization projects, so a dedicated
secret is required: a fine-grained PAT or GitHub App token with
*Organization → Projects: read and write* on the owning organization and
*Repository → Issues: write* on every repository whose issues appear on the
board (for `nasa/fprime`, at least `nasa/fprime`), or a classic PAT with the
`project` and `repo` scopes.

## Usage

```yaml
jobs:
  ccb-resolver:
    runs-on: ubuntu-latest
    steps:
      - uses: nasa/fprime-actions/ccb-resolver@devel
        with:
          token: ${{ secrets.FPRIME_CCB_RESOLVER_TOKEN }}
          dry-run: ${{ github.event_name != 'schedule' }}
```

A `workflow_dispatch` run with the default `dry-run: true` is the safe way
to preview a sweep; the step summary shows the planned changes without
writing anything.

## Running locally

```bash
export GITHUB_TOKEN=<token>
python3 ccb-resolver/scripts/ccb_resolver.py --owner nasa --number 21            # dry-run
python3 ccb-resolver/scripts/ccb_resolver.py --owner nasa --number 21 --apply    # write
```

## Tests

```bash
python3 ccb-resolver/tests/test_ccb_resolver.py
```

The tests exercise the decision table, the dry-run write guard, the
forbidden-mutation guard, pagination, idempotence, and the step summary
against an in-memory fake of the GraphQL API; no network access is needed.
