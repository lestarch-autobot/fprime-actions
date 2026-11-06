#!/usr/bin/env python3
"""C2 -- Hardware run record is present and well-formed.

Running the deployment on the target hardware is a human activity; this
check verifies the evidence it leaves behind.  The module's
``docs/hardware-run.md`` (or ``.yml``/``.yaml``) must carry:

    Platform:   <board / flight computer>
    Deployment: <deployment name>
    Commit:     <7-40 hex git SHA, an ancestor of HEAD when git is available>
    Date:       <YYYY-MM-DD>
    Result:     PASS
    Tester:     <name>

Fields may be written as ``Key: value`` lines or a two-column markdown table.
"""

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

from _report import finish, make_parser

FIELDS = ("platform", "deployment", "commit", "date", "result", "tester")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FIELD_RE = re.compile(r"^\s*\|?\s*\**(\w[\w /]*?)\**\s*[:|]\s*\|?\s*(.+?)\s*\|?\s*$")


def find_record(module_dir: Path):
    for name in ("hardware-run.md", "hardware_run.md", "hardware-run.yml", "hardware-run.yaml"):
        path = module_dir / "docs" / name
        if path.is_file():
            return path
    return None


def parse_record(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line)
        if match:
            key = match.group(1).strip().lower()
            if key in FIELDS and key not in fields:
                fields[key] = match.group(2).strip().strip("`*")
    return fields


def commit_is_ancestor(sha: str, cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # git unavailable: do not fail on environment
    if result.returncode == 128:
        return True  # not a git checkout
    return result.returncode == 0


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument("--max-age-days", type=int, default=None, help="Fail if the run is older than this")
    args = parser.parse_args(argv)
    failures = []
    record = find_record(args.module)
    if record is None:
        failures.append("no docs/hardware-run.md: record the hardware run (Platform, Deployment, Commit, Date, Result, Tester)")
    else:
        fields = parse_record(record.read_text(encoding="utf-8", errors="replace"))
        for key in FIELDS:
            if not fields.get(key):
                failures.append(f"{record.name}: missing field '{key.capitalize()}'")
        sha = fields.get("commit", "")
        if sha and not _SHA_RE.match(sha):
            failures.append(f"{record.name}: Commit '{sha}' is not a git SHA")
        elif sha and not commit_is_ancestor(sha, args.module):
            failures.append(f"{record.name}: Commit {sha} is not an ancestor of HEAD (record is from another line of development)")
        date = fields.get("date", "")
        if date:
            try:
                run_date = dt.date.fromisoformat(date[:10])
            except ValueError:
                failures.append(f"{record.name}: Date '{date}' is not YYYY-MM-DD")
            else:
                if args.max_age_days is not None and (dt.date.today() - run_date).days > args.max_age_days:
                    failures.append(f"{record.name}: run on {run_date} is older than {args.max_age_days} days")
        result = fields.get("result", "")
        if result and result.upper().split()[0] not in ("PASS", "PASSED", "SUCCESS"):
            failures.append(f"{record.name}: Result is '{result}', not PASS")
    return finish(
        check_id="C2",
        name="Hardware run record",
        category="close-out",
        module=args.module,
        failures=failures,
        detail=f"record {record.name}" if record else "",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
