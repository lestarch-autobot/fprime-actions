#!/usr/bin/env python3
"""Run the static checklist checks for every discovered module and write
per-module ``checks/summary.json`` + ``checks/index.html`` artifacts.

The *static* checks (pure model/source/doc parsing -- R1-R8, D1-D5, I1,
I3, U3) run in-process here.  The build-level checks (D6, I2, I4, U1, U4,
U5, C1) are separate CI steps; their JSON-Lines records (written via each
script's ``--json-output``) can be merged in with ``--extra-results`` so
the published summary covers everything the pipeline ran.

Output layout (matching the coverage/codeql publishers)::

    <dest>/<module>/<checks-subdir>/summary.json
    <dest>/<module>/<checks-subdir>/index.html

Exit code: 0 always by default (publisher mode); ``--gate`` exits 1 when
any check failed, for use as a CI gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
import tempfile
from pathlib import Path

import check_command_handlers
import check_dp_priorities
import check_dp_priority_configurable
import check_hardware_run
import check_include_dependencies
import check_port_handlers
import check_req_commands
import check_req_kinds
import check_req_data_products
import check_req_design_mapping
import check_req_events
import check_req_parameters
import check_req_parent_trace
import check_req_ports
import check_req_telemetry
import check_req_verification
import check_sdd_dictionary_tables
import check_sdd_port_behavior
import check_sdd_port_table
import check_sdd_sections
import check_sm_actions
import check_ut_include_dependencies

SCHEMA_VERSION = 1

#: (check id, module) ordered as they appear on the report page.
STATIC_CHECKS = [
    ("R1", check_req_commands),
    ("R2", check_req_telemetry),
    ("R3", check_req_events),
    ("R4", check_req_parameters),
    ("R5", check_req_data_products),
    ("R6", check_req_ports),
    ("R7", check_req_parent_trace),
    ("R8", check_req_verification),
    ("R9", check_req_kinds),
    ("D1", check_port_handlers),
    ("D2", check_command_handlers),
    ("D3", check_sm_actions),
    ("D4", check_dp_priorities),
    ("D5", check_req_design_mapping),
    ("D7", check_sdd_port_table),
    ("D8", check_sdd_dictionary_tables),
    ("D9", check_sdd_sections),
    ("D10", check_sdd_port_behavior),
    ("I1", check_include_dependencies),
    ("I3", check_dp_priority_configurable),
    ("U3", check_ut_include_dependencies),
    ("C2", check_hardware_run),
]

CATEGORY_LABELS = {
    "requirements": "Requirements",
    "design": "Design",
    "implementation": "Implementation",
    "unit-testing": "Unit Testing",
    "close-out": "Close-out",
}

CSS = """\
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 1.5rem; color: #1f2328; }
h1 { margin: 0 0 0.25rem 0; font-size: 1.2rem; }
.meta { color: #57606a; font-size: 0.9rem; margin-bottom: 1rem; }
.meta a { color: #0969da; text-decoration: none; }
table { border-collapse: collapse; min-width: 40rem; }
th, td { padding: 0.4rem 0.75rem; text-align: left; border-top: 1px solid #eaeef2; vertical-align: top; }
th { font-weight: 500; color: #57606a; font-size: 0.85rem; border-bottom: 1px solid #eaeef2; }
.status { display: inline-block; padding: 0.05rem 0.55rem; border-radius: 2em;
          font-size: 0.78rem; font-weight: 600; }
.status-pass { background: #dafbe1; color: #116329; }
.status-fail { background: #ffebe9; color: #a40e26; }
.status-skip { background: #f0f0f0; color: #57606a; }
ul { margin: 0.25rem 0 0.25rem 1rem; padding: 0; font-size: 0.85rem; }
.section { font-weight: 600; background: #f6f8fa; }
"""


def run_static_checks(module_path: str, root: Path, results_file: Path) -> None:
    for _check_id, mod in STATIC_CHECKS:
        argv = [
            "--module", str(root / module_path),
            "--json-output", str(results_file),
        ]
        try:
            mod.main(argv)
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001 -- one bad check must not kill the run
            print(f"ERROR: {mod.__name__} crashed on {module_path}: {exc}", file=sys.stderr)


def normalize_module(record_module: str, root: Path) -> str:
    """Reduce an absolute or root-relative module path to root-relative."""
    path = Path(record_module)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix().strip("/")


def render_module_page(module_path: str, checks: list[dict], *, ref: str, commit: str, generated_at: str) -> str:
    rows = []
    for category in CATEGORY_LABELS:
        in_category = [c for c in checks if c.get("category") == category]
        if not in_category:
            continue
        rows.append(
            f'<tr class="section"><td colspan="4">{html.escape(CATEGORY_LABELS[category])}</td></tr>'
        )
        for check in in_category:
            status = check.get("status", "skip")
            if status not in ("pass", "fail", "skip"):
                status = "skip"
            failures = check.get("failures") or []
            notes = ""
            if failures:
                items = "".join(f"<li>{html.escape(f)}</li>" for f in failures[:20])
                more = f"<li>... {len(failures) - 20} more</li>" if len(failures) > 20 else ""
                notes = f"<ul>{items}{more}</ul>"
            elif check.get("detail"):
                notes = html.escape(str(check["detail"]))
            rows.append(
                f"<tr><td><code>{html.escape(check.get('id', '?'))}</code></td>"
                f"<td>{html.escape(check.get('name', ''))}</td>"
                f'<td><span class="status status-{status}">{status.upper()}</span></td>'
                f"<td>{notes}</td></tr>"
            )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>Checks \u2014 {html.escape(module_path)}</title>"
        f"<style>{CSS}</style></head><body>"
        f"<h1>Checklist checks \u2014 <code>{html.escape(module_path)}</code></h1>"
        f'<div class="meta"><a href="../index.html">\u2190 module</a> &middot; '
        f"<code>{html.escape(ref)}</code> @ <code>{html.escape(commit[:12])}</code> "
        f"&middot; generated {html.escape(generated_at)}</div>"
        "<table><thead><tr><th>ID</th><th>Check</th><th>Status</th><th>Notes</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</body></html>\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="Repository checkout root")
    parser.add_argument("--dest", type=Path, required=True, help="Output tree (baseline worktree)")
    parser.add_argument(
        "--modules-jsonl", type=Path, required=True,
        help="JSON-Lines module list from coverage-common/scripts/discover.py",
    )
    parser.add_argument(
        "--checks-subdirectory", default="checks",
        help='Subdirectory under each module for check artifacts (default: "checks")',
    )
    parser.add_argument(
        "--extra-results", type=Path, action="append", default=[],
        help="JSON-Lines file(s) of additional check results to merge (from build-level checks)",
    )
    parser.add_argument("--ref", default="", help="Source ref name")
    parser.add_argument("--commit", default="", help="Source commit SHA")
    parser.add_argument(
        "--gate", action="store_true",
        help="Exit non-zero when any check failed (CI gate mode)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    modules = []
    with args.modules_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                modules.append(json.loads(line)["path"])

    scratch_dir = tempfile.TemporaryDirectory(prefix="component-checks-")
    results_file = Path(scratch_dir.name) / "_checks_results.jsonl"

    for module_path in modules:
        run_static_checks(module_path, root, results_file)

    by_module: dict[str, list[dict]] = {m: [] for m in modules}
    sources = [results_file] + list(args.extra_results)
    for source in sources:
        if not source.is_file():
            continue
        with source.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                module_path = normalize_module(record.get("module", ""), root)
                if module_path in by_module:
                    # Last record for a given check id wins.
                    by_module[module_path] = [
                        c for c in by_module[module_path] if c.get("id") != record.get("id")
                    ] + [record]
                else:
                    print(
                        f"WARNING: dropping result for unknown module "
                        f"'{record.get('module', '')}'",
                        file=sys.stderr,
                    )
    scratch_dir.cleanup()

    any_failed = False
    for module_path, checks in by_module.items():
        checks.sort(key=lambda c: (c.get("category", ""), c.get("id", "")))
        passed = sum(1 for c in checks if c.get("status") == "pass")
        failed = sum(1 for c in checks if c.get("status") == "fail")
        skipped = sum(1 for c in checks if c.get("status") == "skip")
        any_failed = any_failed or failed > 0
        out_dir = args.dest / module_path / args.checks_subdirectory
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "generated_at": generated_at,
                    "ref": args.ref,
                    "commit": args.commit,
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                    "checks": checks,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (out_dir / "index.html").write_text(
            render_module_page(
                module_path, checks, ref=args.ref, commit=args.commit, generated_at=generated_at
            ),
            encoding="utf-8",
        )

    print(f"run_checks: wrote check summaries for {len(by_module)} module(s)", file=sys.stderr)
    return 1 if (args.gate and any_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
