#!/usr/bin/env python3
"""Tests for the component-checklist publisher.

Run with ``python3 component-checklist/tests/test_checklist.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent.parent / "coverage-common" / "scripts"))

import catalog  # noqa: E402
import run_checklists  # noqa: E402

# Stand-in for fprime-auto-checklist: REVIEW rows named in --previous become reviewer decisions.
FAKE_TOOL = r'''
import argparse, json, sys
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("command"); p.add_argument("--project"); p.add_argument("--module")
p.add_argument("--output"); p.add_argument("--json-output"); p.add_argument("--work-dir")
p.add_argument("--previous"); p.add_argument("--skip-analysis", action="store_true")
a = p.parse_args()
if a.module.endswith("Broken"):
    sys.exit(2)
reviewed = bool(a.previous) and "YES by review" in Path(a.previous).read_text()
items = [
    {"id": "1.1", "status": "YES" if reviewed else "REVIEW", "kind": "review" if reviewed else "tool"},
    {"id": "2.3", "status": "NO", "kind": "tool"},
]
summary = {"YES": int(reviewed), "NO": 1, "N/A": 0, "REVIEW": int(not reviewed), "UNCHECKED": 0}
Path(a.output).write_text(
    "# Checklist\n\n_Filled for `" + a.module + "`._\n\n| Item | Status | Notes |\n|---|---|---|\n"
    "| 1.1 | " + ("YES by review" if reviewed else "REVIEW") + " | a \\| b <script> |\n| 2.3 | NO | **D7** [doc](docs/sdd.md) |\n\n- **1.1** note\n"
)
Path(a.json_output).write_text(json.dumps({
    "checklist_version": "1.2.0", "tool": {"name": "fake", "version": "0"}, "summary": summary, "items": items,
    "codeql": {"ran": not a.skip_analysis, "tool": "clang-tidy", "findings": {"cpp/fprime/x": [{}, {}]}},
}))
sys.exit(1)
'''


def _project(root: Path) -> Path:
    for name, fpp in (
        ("Svc/Widget", "module Svc {\n  active component Widget {\n  }\n}\n"),
        ("Svc/Broken", "module Svc {\n  passive component Broken {\n  }\n}\n"),
        ("Fw/Types", "module Fw {\n  type T\n}\n"),
    ):
        (root / name).mkdir(parents=True)
        (root / name / (name.split("/")[1] + ".fpp")).write_text(fpp)
    modules = root / "modules.jsonl"
    modules.write_text("".join(json.dumps({"path": p, "has_ut": True}) + "\n" for p in ("Svc/Widget", "Svc/Broken", "Fw/Types")))
    (root / "fake_tool.py").write_text(FAKE_TOOL)
    return modules


def _run(root: Path, dest: Path, modules: Path, *tool_args: str) -> int:
    return run_checklists.main([
        "--root", str(root), "--dest", str(dest), "--modules-jsonl", str(modules),
        "--work-dir", str(root / "work"), "--ref", "devel", "--commit", "a" * 40,
        "--tool", sys.executable, str(root / "fake_tool.py"), "--", *tool_args,
    ])


def test_run_checklists_publishes_components_only():
    with tempfile.TemporaryDirectory() as td:
        root, dest = Path(td) / "repo", Path(td) / "dest"
        modules = _project(root)
        assert _run(root, dest, modules) == 0
        out = dest / "Svc/Widget/checklist"
        summary = json.loads((out / "summary.json").read_text())
        assert summary["artifact"] == "checklist" and summary["total"] == 2
        assert summary["counts"]["REVIEW"] == 1 and summary["reviewer_decisions"] == 0
        assert summary["source"] is None
        assert summary["static_analysis"] == {"tool": "clang-tidy", "ran": True, "findings": 2}
        # A failing tool run and a module without a component publish nothing.
        broken = json.loads((dest / "Svc/Broken/checklist/summary.json").read_text())
        assert broken["error"] and broken["total"] == 0 and broken["source"] is None
        assert not (dest / "Svc/Broken/checklist/index.html").exists()
        assert not (dest / "Fw/Types").exists()

        page = (out / "index.html").read_text()
        assert "<td>a | b &lt;script&gt;</td>" in page
        assert '<strong>D7</strong> <a href="docs/sdd.md">doc</a>' in page
        assert "<li><strong>1.1</strong> note</li>" in page
        assert "reviewer rows are open" in page


def test_run_checklists_carries_repository_checklist():
    with tempfile.TemporaryDirectory() as td:
        root, dest = Path(td) / "repo", Path(td) / "dest"
        modules = _project(root)
        (root / "Svc/Widget/docs").mkdir()
        (root / "Svc/Widget/docs/checklist.md").write_text("| 1.1 | YES by review | |\n")
        assert _run(root, dest, modules, "--skip-analysis") == 0
        summary = json.loads((dest / "Svc/Widget/checklist/summary.json").read_text())
        assert summary["reviewer_decisions"] == 1 and summary["counts"]["YES"] == 1
        assert summary["source"] == "Svc/Widget/docs/checklist.md"
        assert summary["static_analysis"]["ran"] is False
        # The repository copy is input only.
        assert (root / "Svc/Widget/docs/checklist.md").read_text() == "| 1.1 | YES by review | |\n"


def test_all_components_failing_is_an_error():
    with tempfile.TemporaryDirectory() as td:
        root, dest = Path(td) / "repo", Path(td) / "dest"
        _project(root)
        only_broken = root / "broken.jsonl"
        only_broken.write_text(json.dumps({"path": "Svc/Broken"}) + "\n")
        assert _run(root, dest, only_broken) == 1


def test_catalog_links_checklist():
    with tempfile.TemporaryDirectory() as td:
        root, dest = Path(td) / "repo", Path(td) / "dest"
        modules = _project(root)
        (root / "Svc/Widget/docs").mkdir()
        (root / "Svc/Widget/docs/checklist.md").write_text("| 1.1 | YES by review | |\n")
        assert _run(root, dest, modules) == 0
        # A checks summary gives the landing page a Checklist cell to extend.
        checks = dest / "Svc/Widget/checks"
        checks.mkdir(parents=True)
        (checks / "summary.json").write_text(json.dumps({"checks": [], "passed": 1, "failed": 0, "skipped": 0}))
        assert catalog.main([
            "--dest", str(dest), "--modules-jsonl", str(modules), "--ref", "devel", "--commit", "a" * 40,
        ]) == 0
        entry = {m["path"]: m for m in json.loads((dest / "catalog.json").read_text())["modules"]}
        assert entry["Svc/Widget"]["checklist"]["report"] == "Svc/Widget/checklist/index.html"
        assert entry["Svc/Widget"]["checklist"]["reviewer_decisions"] == 1
        assert entry["Svc/Widget"]["checklist"]["tier"] == "gold"
        assert entry["Svc/Broken"]["checklist"]["tier"] == "silver"
        assert entry["Fw/Types"]["checklist"] is None
        assert '<a href="Svc/Widget/checklist/index.html">checklist</a>' in (dest / "index.html").read_text()
        module_page = (dest / "Svc/Widget/index.html").read_text()
        assert "Component Development Checklist" in module_page
        assert '</span> <a href="checklist/index.html">1/2 satisfied, 1 not satisfied (1 decided by reviewers)</a>' in module_page
        assert "clang-tidy: 2 findings" in module_page


def test_checklist_tier():
    from _tiers import checklist_tier

    counts = {"YES": 70, "N/A": 3, "NO": 0, "REVIEW": 0, "UNCHECKED": 0}
    assert checklist_tier({"source": None, "counts": counts}) == "silver"
    assert checklist_tier({"source": "Svc/A/docs/checklist.md", "counts": counts}) == "platinum"
    for key in ("NO", "REVIEW", "UNCHECKED"):
        assert checklist_tier({"source": "x", "counts": {**counts, key: 1}}) == "gold"
    assert checklist_tier({"source": "x", "counts": {}}) == "gold"

    def entry(summary):
        return catalog.ModuleEntry(
            path="Svc/A", has_ut=False, ut_summary=None, ut_report="", int_summary=None, int_report="",
            codeql_checks={}, codeql_report="", sdd_summary={"artifact": "sdd", "tier": "platinum"},
            checklist_summary=summary,
        )

    assert entry(None).rollup_tier(None) == "platinum"  # not a component / action not used
    assert entry({"source": None, "counts": counts}).rollup_tier(None) == "silver"
    assert entry({"source": "x", "counts": {**counts, "REVIEW": 2}}).rollup_tier(None) == "gold"
    assert entry({"source": "x", "counts": counts}).rollup_tier(None) == "platinum"


TESTS = [
    test_run_checklists_publishes_components_only,
    test_run_checklists_carries_repository_checklist,
    test_all_components_failing_is_an_error,
    test_catalog_links_checklist,
    test_checklist_tier,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"OK   {test.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{failed} test(s) failed." if failed else f"\nAll {len(TESTS)} tests passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
