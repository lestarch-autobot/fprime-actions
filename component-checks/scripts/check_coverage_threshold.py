#!/usr/bin/env python3
"""U5 -- Sufficient code coverage.

Verifies the component's line coverage meets a configurable threshold.
Either parses an existing gcovr ``--json-summary`` file (``--summary-json``,
e.g. from the coverage baseline branch or a coverage-common run), or runs
``fprime-util check --coverage`` and gcovr locally.  Reports the measured
coverage and exits non-zero when below the threshold.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _report import finish, make_parser

_LINES_RE = re.compile(r"^lines:\s*([0-9.]+)%", re.MULTILINE)
_TOTAL_RE = re.compile(r"^TOTAL\s+(\d+)\s+(\d+)\s+(\d+)%", re.MULTILINE)


def line_percent_txt(text: str):
    """Line percentage from gcovr text output: the ``--txt`` table's ``TOTAL lines exec cover%``
    row (exact ratio) or a ``--print-summary`` ``lines: 98.7% (...)`` line."""
    total = _TOTAL_RE.search(text)
    if total and int(total.group(1)) > 0:
        return round(100.0 * int(total.group(2)) / int(total.group(1)), 2)
    match = _LINES_RE.search(text)
    return float(match.group(1)) if match else None


def line_percent(doc: dict) -> float:
    total = int(doc.get("line_total", 0) or 0)
    covered = int(doc.get("line_covered", 0) or 0)
    if total > 0:
        return round(100.0 * covered / total, 2)
    return float(doc.get("line_percent", 0.0) or 0.0)


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument(
        "--threshold", type=float, default=80.0,
        help="Minimum line coverage percent (default: 80)",
    )
    parser.add_argument(
        "--summary-json", type=Path, default=None,
        help="Existing gcovr --json-summary file to evaluate instead of running checks",
    )
    parser.add_argument(
        "--summary-txt", type=Path, default=None,
        help="Existing fprime-util gcovr summary.txt to evaluate instead of running checks",
    )
    args = parser.parse_args(argv)

    skipped_reason = None
    failures = []
    detail = ""

    summary_path = args.summary_json
    doc_override = None
    tempdir = None
    if args.summary_txt is not None:
        pct = line_percent_txt(args.summary_txt.read_text(encoding="utf-8", errors="replace"))
        if pct is None:
            failures.append(f"cannot find a 'lines:' entry in {args.summary_txt}")
        else:
            doc_override = {"line_percent": pct}
        detail = f"from {args.summary_txt}"
    elif summary_path is None:
        if shutil.which("fprime-util") is None or shutil.which("gcovr") is None:
            skipped_reason = "fprime-util/gcovr not found on PATH and no --summary-json given"
        else:
            proc = subprocess.run(
                ["fprime-util", "check", "--coverage"],
                cwd=args.module, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                failures.append(
                    "'fprime-util check --coverage' failed:\n"
                    + ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
                )
            elif (args.module / "coverage" / "summary.txt").is_file():
                # written by fprime-util's gcovr target (--txt summary) with the module filter applied
                summary_txt = args.module / "coverage" / "summary.txt"
                pct = line_percent_txt(summary_txt.read_text(encoding="utf-8", errors="replace"))
                if pct is None:
                    failures.append(f"cannot find a 'lines:' entry in {summary_txt}")
                else:
                    doc_override = {"line_percent": pct}
            else:
                tempdir = tempfile.mkdtemp(prefix="cov-check-")
                summary_path = Path(tempdir) / "summary.json"
                proc = subprocess.run(
                    ["gcovr", "-r", ".", "--json-summary", str(summary_path)],
                    cwd=args.module, capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    failures.append(f"gcovr failed:\n{(proc.stderr or proc.stdout)[-4000:]}")
                    summary_path = None

    if skipped_reason is None and not failures and (summary_path is not None or doc_override is not None):
        doc = doc_override
        try:
            if doc is None:
                doc = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"cannot read coverage summary {summary_path}: {exc}")
        if doc is not None:
            pct = line_percent(doc)
            detail = f"line coverage {pct:.2f}% (threshold {args.threshold:.2f}%)"
            if pct < args.threshold:
                failures.append(
                    f"line coverage {pct:.2f}% is below the required threshold "
                    f"{args.threshold:.2f}%"
                )

    return finish(
        check_id="U5",
        name="Sufficient code coverage",
        category="unit-testing",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=detail,
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
