# F Prime GitHub Actions

This repository contains GitHub [Actions and Reusable Workflows](https://docs.github.com/en/actions/sharing-automations/avoiding-duplication) for use with F Prime projects.

## Actions

| Action | Purpose |
|---|---|
| [`ccb-resolver`](ccb-resolver/) | Apply CCB decisions recorded on the F´ Development project board: move `Approved` items to `Backlog - Deferred`; comment on and close `Disapproved` issues as not planned. Dry-run by default. |

Every other directory in this repository is also a composite action or
reusable-workflow helper; see the `README.md` inside each one.
