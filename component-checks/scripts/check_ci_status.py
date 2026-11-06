#!/usr/bin/env python3
"""C3 -- CI passes on the module's commit.

Queries the GitHub check-runs API for ``--sha`` in ``--repo`` (``owner/name``)
and fails if any check run is incomplete or concluded with failure,
cancelled, timed_out, or action_required.  Requires ``GITHUB_TOKEN`` (or
``GH_TOKEN``); skips with a reason when offline.  ``--check-runs-json``
reads a saved API response instead (for tests and air-gapped use).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from _report import finish, make_parser

BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}


def fetch_check_runs(repo: str, sha: str, token: str) -> list:
    url = f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "fprime-component-checks",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("check_runs", [])


def evaluate(check_runs: list, ignore: set) -> list:
    failures = []
    for run in check_runs:
        name = run.get("name", "?")
        if name in ignore:
            continue
        if run.get("status") != "completed":
            failures.append(f"check '{name}' has not completed ({run.get('status')})")
        elif run.get("conclusion") in BAD:
            failures.append(f"check '{name}' concluded {run.get('conclusion')}: {run.get('html_url', '')}")
    return failures


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"), help="commit to inspect")
    parser.add_argument("--ignore", action="append", default=[], help="check-run name to ignore (repeatable)")
    parser.add_argument("--check-runs-json", type=Path, default=None, help="saved check-runs API response")
    args = parser.parse_args(argv)
    failures = []
    skipped_reason = None
    check_runs = None
    if args.check_runs_json is not None:
        check_runs = json.loads(args.check_runs_json.read_text(encoding="utf-8")).get("check_runs", [])
    elif not args.repo or not args.sha:
        skipped_reason = "no --repo/--sha (or GITHUB_REPOSITORY/GITHUB_SHA)"
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            skipped_reason = "no GITHUB_TOKEN"
        else:
            try:
                check_runs = fetch_check_runs(args.repo, args.sha, token)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                skipped_reason = f"GitHub API unavailable: {exc}"
    if check_runs is not None:
        ignore = set(args.ignore)
        if not check_runs:
            failures.append(f"no check runs reported for {args.sha}")
        failures.extend(evaluate(check_runs, ignore))
    return finish(
        check_id="C3",
        name="CI passes on commit",
        category="close-out",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(check_runs)} check run(s)" if check_runs is not None else "",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
