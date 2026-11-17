"""Unit tests for the coverage action's helper scripts.

Run with:  python3 -m coverage.tests.test_coverage
or:        python3 coverage/tests/test_coverage.py

No external dependencies.  Each test prints OK or FAIL and the runner
exits 1 on any failure.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"
FIXTURES = HERE / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))

import discover  # noqa: E402
import compare  # noqa: E402
import mirror  # noqa: E402
import catalog  # noqa: E402
import codeql_findings  # noqa: E402
import fetch_dismissed_alerts  # noqa: E402
import versions  # noqa: E402
from _summary import Summary, load_summary  # noqa: E402
from _config import coverage_thresholds, load_config  # noqa: E402
from _tiers import CoverageThresholds, codeql_tier, coverage_tier, normalize_severity  # noqa: E402


def _make_module(
    root: Path,
    path: str,
    *,
    with_ut: bool = True,
    with_cpp: bool = True,
    register: str = "register_fprime_module",
) -> Path:
    """Create a fake F´ module dir with a CMakeLists.txt that triggers discovery."""
    mod_dir = root / path
    mod_dir.mkdir(parents=True, exist_ok=True)
    body = ["set(SOURCE_FILES foo.cpp)", f"{register}()"]
    if with_ut:
        body.append("register_fprime_ut()")
    (mod_dir / "CMakeLists.txt").write_text("\n".join(body) + "\n", encoding="utf-8")
    if with_cpp:
        (mod_dir / "Foo.cpp").write_text("int foo() { return 0; }\n", encoding="utf-8")
    return mod_dir


def _place_coverage(mod_dir: Path, fixture_name: str) -> None:
    """Drop a sample summary.json + minimal coverage.html under <mod>/coverage/."""
    cov = mod_dir / "coverage"
    cov.mkdir(exist_ok=True)
    shutil.copy2(FIXTURES / fixture_name, cov / "summary.json")
    (cov / "coverage.html").write_text(
        '<html><body>placeholder report '
        '<a href="coverage.123.html">detail</a></body></html>',
        encoding="utf-8",
    )
    (cov / "coverage.123.html").write_text("<html><body>detail</body></html>", encoding="utf-8")


def _modules_jsonl(records: list[dict]) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for rec in records:
        fh.write(json.dumps(rec) + "\n")
    fh.close()
    return Path(fh.name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discover_finds_modules_and_ut_flag():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_module(root, "Svc/CmdDispatcher", with_ut=True)
        _make_module(root, "Drv/LinuxGpio", with_ut=False)
        _make_module(root, "Fw/Cmd", with_ut=True)
        # Decoy: file with text "register_fprime_module" but commented out.
        (root / "Decoy").mkdir()
        (root / "Decoy" / "CMakeLists.txt").write_text(
            "# register_fprime_module() -- commented out\n", encoding="utf-8"
        )

        result = sorted(discover.discover(root))
        assert ("Drv/LinuxGpio", False, True) in result, result
        assert ("Svc/CmdDispatcher", True, True) in result, result
        assert ("Fw/Cmd", True, True) in result, result
        assert not any(p.startswith("Decoy") for p, _, _ in result), result


def test_discover_finds_library_modules():
    # Modules registered via register_fprime_library (e.g. Utils/Hash) count too.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_module(root, "Utils/Hash", with_ut=True, register="register_fprime_library")
        _make_module(root, "Some/Lib", with_ut=False, register="register_fprime_library")

        result = sorted(discover.discover(root))
        assert ("Utils/Hash", True, True) in result, result
        assert ("Some/Lib", False, True) in result, result


def test_discover_excludes_autocoder_only_modules_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_module(root, "Svc/CmdDispatcher", with_ut=True)
        # Autocoder-only: registers a module but has no C/C++ source (FPP only)
        ports = _make_module(root, "Fw/TypesOnly", with_ut=False, with_cpp=False)
        (ports / "Types.fpp").write_text("module Fw {}\n", encoding="utf-8")
        # Parent whose only C++ lives in a nested module is autocoder-only too
        _make_module(root, "Svc/Parent", with_ut=False, with_cpp=False)
        _make_module(root, "Svc/Parent/Child", with_ut=False)

        result = sorted(discover.discover(root))
        assert ("Fw/TypesOnly", False, False) in result, result
        assert ("Svc/Parent", False, False) in result, result
        assert ("Svc/Parent/Child", False, True) in result, result

        # CLI: excluded by default, included with --include-autocoder-only
        import io as _io
        from contextlib import redirect_stdout

        def run(argv):
            out = _io.StringIO()
            with redirect_stdout(out), redirect_stderr(_io.StringIO()):
                assert discover.main(argv) == 0
            return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]

        default_paths = {r["path"] for r in run(["--root", str(root)])}
        assert "Fw/TypesOnly" not in default_paths
        assert "Svc/Parent" not in default_paths
        assert {"Svc/CmdDispatcher", "Svc/Parent/Child"} <= default_paths

        all_records = run(["--root", str(root), "--include-autocoder-only"])
        all_paths = {r["path"] for r in all_records}
        assert {"Fw/TypesOnly", "Svc/Parent"} <= all_paths
        by_path = {r["path"]: r for r in all_records}
        assert by_path["Fw/TypesOnly"]["has_cpp"] is False
        assert by_path["Svc/CmdDispatcher"]["has_cpp"] is True


def test_discover_ignores_commented_out_calls():
    # Regression test for the regex: lines starting with `#` must not match.
    text = "# register_fprime_module()\n  register_fprime_ut()"
    assert not discover.MODULE_RE.search(text)
    assert discover.UT_RE.search(text)


def test_summary_load_handles_missing_file_and_zero_total():
    assert load_summary(Path("/nonexistent/path/summary.json")) is None
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "summary.json"
        f.write_text("{}", encoding="utf-8")
        s = load_summary(f)
        assert s is not None
        assert s.line.percent == 0.0
        assert s.line.total == 0


def test_summary_load_parses_gcovr_json():
    s = load_summary(FIXTURES / "summary_high.json")
    assert s is not None
    assert s.line.covered == 392 and s.line.total == 400
    assert s.line.percent == 98.0
    assert s.function.covered == 28 and s.function.total == 30
    assert s.function.percent == 93.33
    assert s.branch.percent == 87.5


def test_mirror_module_renames_html_when_coverage_present():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()
        mod_path = "Svc/CmdDispatcher"
        _place_coverage(_make_module(source, mod_path), "summary_high.json")

        has_cov = mirror.mirror_module(
            source=source, dest=dest, module_path=mod_path, has_ut=True, subdir="coverage"
        )
        assert has_cov is True
        dst_cov = dest / mod_path / "coverage"
        assert (dst_cov / "summary.json").is_file()
        assert (dst_cov / "index.html").is_file(), "coverage.html should be renamed to index.html"
        assert not (dst_cov / "coverage.html").exists(), "old coverage.html must be removed"
        assert (dst_cov / "coverage.123.html").is_file(), "sibling detail file must be preserved"


def test_mirror_module_writes_placeholder_when_no_ut():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()
        mod_path = "Drv/LinuxGpio"
        _make_module(source, mod_path, with_ut=False)

        has_cov = mirror.mirror_module(
            source=source, dest=dest, module_path=mod_path, has_ut=False, subdir="coverage"
        )
        assert has_cov is False
        placeholder = dest / mod_path / "coverage" / "index.html"
        assert placeholder.is_file()
        body = placeholder.read_text(encoding="utf-8")
        assert "No coverage recorded" in body
        assert "Drv/LinuxGpio" in body
        # The back-to-catalog link must climb past both the module path and the subdir.
        assert 'href="../../../index.html"' in body, body


def test_catalog_relative_path_math():
    # Sanity-check the back-link depth calculation for various inputs.
    assert mirror._catalog_relative_path("Drv/LinuxGpio", "coverage") == "../../../index.html"
    assert mirror._catalog_relative_path("Drv/LinuxGpio", "") == "../../index.html"
    assert mirror._catalog_relative_path("Fw/Cmd/Sub", "coverage") == "../../../../index.html"
    assert mirror._catalog_relative_path("TopLevel", "coverage") == "../../index.html"
    assert mirror._catalog_relative_path("TopLevel", "") == "../index.html"


def test_mirror_flatten_drops_coverage_subdir():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()
        mod_path = "Svc/CmdDispatcher"
        _place_coverage(_make_module(source, mod_path), "summary_high.json")

        mirror.mirror_module(
            source=source, dest=dest, module_path=mod_path, has_ut=True, subdir=""
        )
        # With subdir="", artifacts land directly under the module dir.
        assert (dest / mod_path / "summary.json").is_file()
        assert (dest / mod_path / "index.html").is_file()
        assert not (dest / mod_path / "coverage").exists()


def test_mirror_module_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()
        mod_path = "Svc/CmdDispatcher"
        _place_coverage(_make_module(source, mod_path), "summary_high.json")

        mirror.mirror_module(
            source=source, dest=dest, module_path=mod_path, has_ut=True, subdir="coverage"
        )
        # Drop a stale file that should be removed by the second run.
        (dest / mod_path / "coverage" / "stale.html").write_text("stale", encoding="utf-8")

        mirror.mirror_module(
            source=source, dest=dest, module_path=mod_path, has_ut=True, subdir="coverage"
        )
        assert not (dest / mod_path / "coverage" / "stale.html").exists()
        assert (dest / mod_path / "coverage" / "summary.json").is_file()


def _mirror_all(source: Path, dest: Path, records: list[dict]) -> None:
    """Mirror module + global coverage from source into dest (baseline tree)."""
    mirror.mirror_global(source=source, dest=dest, subdir="coverage")
    for rec in records:
        mirror.mirror_module(
            source=source, dest=dest, module_path=rec["path"],
            has_ut=rec["has_ut"], subdir="coverage",
        )


def test_tier_computation():
    thresholds = CoverageThresholds()
    assert coverage_tier(98.0, True, thresholds) == "platinum"
    assert coverage_tier(95.0, True, thresholds) == "platinum"
    assert coverage_tier(92.0, True, thresholds) == "gold"
    assert coverage_tier(85.0, True, thresholds) == "silver"
    assert coverage_tier(60.0, True, thresholds) == "bronze"
    assert coverage_tier(100.0, False, thresholds) == "bronze"  # no coverage is bronze
    custom = CoverageThresholds(platinum=99.0, gold=97.0, silver=50.0)
    assert coverage_tier(98.0, True, custom) == "gold"
    assert coverage_tier(60.0, True, custom) == "silver"

    assert codeql_tier(None) == "platinum"
    assert codeql_tier("low") == "silver"
    assert codeql_tier("medium") == "bronze"
    assert codeql_tier("error") == "bronze"

    assert normalize_severity("error") == "error"
    assert normalize_severity("warning") == "medium"
    assert normalize_severity("note") == "low"
    assert normalize_severity("", 9.1) == "error"
    assert normalize_severity("note", 5.0) == "medium"
    assert normalize_severity("error", 2.0) == "low"


def test_config_loading():
    # Missing file / None -> defaults
    assert coverage_thresholds(load_config(None)) == CoverageThresholds()
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.yml"
        assert coverage_thresholds(load_config(missing)) == CoverageThresholds()

        cfg = Path(tmp) / "module-checklist.yml"
        cfg.write_text(
            "coverage:\n  tiers:\n    platinum: 99\n    gold: 88\n"
            "future_section:\n  ignored: true\n",
            encoding="utf-8",
        )
        thresholds = coverage_thresholds(load_config(cfg))
        assert thresholds.platinum == 99.0
        assert thresholds.gold == 88.0
        assert thresholds.silver == 80.0  # default retained

        # Non-numeric value falls back to the default with a warning
        cfg.write_text(
            "coverage:\n  tiers:\n    platinum: high\n", encoding="utf-8"
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            thresholds = coverage_thresholds(load_config(cfg))
        assert thresholds.platinum == 95.0
        assert "not a number" in buf.getvalue()

        # Unparseable file -> defaults with a warning
        cfg.write_text("::: not yaml : [", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stderr(buf):
            assert coverage_thresholds(load_config(cfg)) == CoverageThresholds()


def test_catalog_groups_and_rollup():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()

        _place_coverage(_make_module(source, "Svc/CmdDispatcher"), "summary_high.json")
        _place_coverage(_make_module(source, "Svc/Health"), "summary_mid.json")
        _place_coverage(_make_module(source, "Drv/I2c"), "summary_low.json")
        _make_module(source, "Drv/LinuxGpio", with_ut=False)
        # Global --all run
        (source / "coverage").mkdir(exist_ok=True)
        shutil.copy2(FIXTURES / "summary_high.json", source / "coverage" / "summary.json")

        records = [
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Svc/Health", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
            {"path": "Drv/LinuxGpio", "has_ut": False},
        ]
        modules_jsonl = _modules_jsonl(records)
        _mirror_all(source, dest, records)

        rc = catalog.main([
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--coverage-subdirectory", "coverage",
            "--ref", "devel",
            "--ref-type", "branch",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ])
        assert rc == 0

        cat_doc = json.loads((dest / "catalog.json").read_text(encoding="utf-8"))
        assert cat_doc["schema"] == 6
        assert cat_doc["ref"] == "devel"
        assert cat_doc["commit"] == "deadbeefcafe1234"
        assert cat_doc["thresholds"] == {"platinum": 95.0, "gold": 90.0, "silver": 80.0}
        assert len(cat_doc["modules"]) == 4
        by_path = {m["path"]: m for m in cat_doc["modules"]}
        assert by_path["Drv/LinuxGpio"]["has_coverage"] is False
        assert by_path["Drv/LinuxGpio"]["has_ut"] is False
        assert by_path["Drv/LinuxGpio"]["tiers"]["ut"] == "bronze"  # no coverage is bronze
        assert by_path["Drv/LinuxGpio"]["tiers"]["overall"] == "bronze"
        # Missing SDD counts as bronze and drags the overall roll-up down
        assert by_path["Svc/CmdDispatcher"]["tiers"]["sdd"] == "bronze"
        assert by_path["Svc/CmdDispatcher"]["sdd"] is None
        assert by_path["Svc/CmdDispatcher"]["tiers"]["overall"] == "bronze"
        assert by_path["Svc/CmdDispatcher"]["ut"]["line_pct"] == 98.0
        assert by_path["Svc/CmdDispatcher"]["ut"]["function_pct"] == 93.33
        assert by_path["Svc/CmdDispatcher"]["tiers"]["ut"] == "platinum"
        assert by_path["Svc/CmdDispatcher"]["tiers"]["codeql"] is None  # not published yet
        assert by_path["Svc/CmdDispatcher"]["int"] is None  # int coverage deferred
        # Overall came from a high fixture
        assert cat_doc["overall"]["line_pct"] == 98.0
        assert cat_doc["overall"]["tier"] == "platinum"
        assert cat_doc["overall_codeql"] is None

        index_html = (dest / "index.html").read_text(encoding="utf-8")
        # Spot-check structure: checklist columns and tier badges
        assert "<details" in index_html and "Svc/" in index_html and "Drv/" in index_html
        assert "Svc/CmdDispatcher" in index_html
        assert "Drv/LinuxGpio" in index_html
        assert ">UT Coverage</th>" in index_html
        assert ">Int Coverage</th>" in index_html
        assert ">Checklist</th>" in index_html
        assert ">CodeQL</th>" in index_html
        assert "badge-platinum" in index_html
        assert "badge-bronze" in index_html
        assert "no UT" in index_html

        # Module rows link to the per-module roll-up page
        assert 'href="Svc/CmdDispatcher/index.html"' in index_html

        # Per-module roll-up pages link the artifact subtrees with badges
        mod_page = (dest / "Svc/CmdDispatcher/index.html").read_text(encoding="utf-8")
        assert 'href="coverage/index.html"' in mod_page
        assert "badge-platinum" in mod_page
        assert ">Unit Test Coverage</td>" in mod_page
        # h1 roll-up badge is bronze: the missing SDD drags it down
        assert '<span class="badge badge-bronze">Bronze</span></h1>' in mod_page
        assert 'class="welcome"' in mod_page
        assert ">Integration Test Coverage</td>" in mod_page
        assert ">Software Description Document</td>" in mod_page
        assert ">CodeQL</td>" in mod_page
        assert "no data" in mod_page  # codeql not published yet
        assert 'href="../../index.html"' in mod_page  # back-link to checklist


def _sample_sarif(tmp: Path) -> Path:
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "CodeQL",
                        "rules": [
                            {
                                "id": "cpp/high-risk",
                                "properties": {"security-severity": "8.8"},
                            },
                            {
                                "id": "cpp/style-note",
                                "defaultConfiguration": {"level": "note"},
                            },
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": "cpp/high-risk",
                        "level": "warning",
                        "message": {"text": "Dangerous thing."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "Svc/CmdDispatcher/CmdDispatcher.cpp"},
                                    "region": {"startLine": 42},
                                }
                            }
                        ],
                    },
                    {
                        "ruleId": "cpp/style-note",
                        "level": "",
                        "message": {"text": "Minor style thing."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "Drv/I2c/I2c.cpp"},
                                    "region": {"startLine": 7},
                                }
                            }
                        ],
                    },
                    {
                        "ruleId": "cpp/style-note",
                        "level": "note",
                        "message": {"text": "Outside any module."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "cmake/helper.cpp"},
                                    "region": {"startLine": 3},
                                }
                            }
                        ],
                    },
                ],
            }
        ],
    }
    path = tmp / "cpp.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")
    return path


def test_dismissed_alerts_excluded_and_tabulated():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "dest"
        dest.mkdir()
        sarif = _sample_sarif(tmp)
        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
        ])
        # Dismissed in the UI: same rule/path, line drifted by 2 (within tolerance)
        dismissed = tmp / "dismissed.json"
        dismissed.write_text(json.dumps([
            {"rule": "cpp/high-risk", "path": "Svc/CmdDispatcher/CmdDispatcher.cpp",
             "line": 44, "message": "Dangerous thing.",
             "reason": "won't fix", "comment": "accepted risk per review"},
        ]), encoding="utf-8")

        rc = codeql_findings.main([
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--repo-url", "https://github.com/org/repo",
            "--dismissed-alerts", str(dismissed),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ])
        assert rc == 0

        # The dismissed finding no longer counts against the module's tier
        cmd = json.loads((dest / "Svc/CmdDispatcher/codeql/summary.json").read_text())
        assert cmd["findings"] == 0
        assert cmd["tier"] == "platinum"
        assert cmd["dismissed"] == 1

        # ...but appears in the dismissed table with reason and comment
        page = (dest / "Svc/CmdDispatcher/codeql/index.html").read_text()
        assert "Dismissed findings (1)" in page
        assert "won&#x27;t fix" in page
        assert "accepted risk per review" in page
        assert "No active CodeQL findings" in page

        # Unaffected module keeps its active finding and has no dismissed table
        i2c = json.loads((dest / "Drv/I2c/codeql/summary.json").read_text())
        assert i2c["findings"] == 1
        assert i2c["dismissed"] == 0
        assert "Dismissed findings" not in (dest / "Drv/I2c/codeql/index.html").read_text()

        # Global rollup reflects the subtraction
        top = json.loads((dest / "codeql/summary.json").read_text())
        assert top["findings"] == 2  # 3 in SARIF minus 1 dismissed
        assert top["dismissed"] == 1

        d = codeql_findings.load_dismissed_alerts(dismissed, ["Svc/CmdDispatcher"])
        # Out-of-tolerance line drift + different message does NOT match
        f = codeql_findings.Finding(
            path="Svc/CmdDispatcher/CmdDispatcher.cpp", line=60, rule="cpp/high-risk",
            severity="error", message="A different alert.", module="Svc/CmdDispatcher")
        assert codeql_findings.filter_dismissed([f], d) == [f]
        # Identical message text matches even with large line drift
        f2 = codeql_findings.Finding(
            path="Svc/CmdDispatcher/CmdDispatcher.cpp", line=200, rule="cpp/high-risk",
            severity="error", message="Dangerous thing.", module="Svc/CmdDispatcher")
        assert codeql_findings.filter_dismissed([f2], d) == []


def test_dismissal_matching_is_one_to_one():
    """One UI dismissal must not swallow every same-rule finding in a file.

    Regression: repos with many findings per rule per file reported
    everything as dismissed ("clean") because a single dismissed alert
    matched all of them.
    """
    path = "Svc/CmdDispatcher/CmdDispatcher.cpp"
    def f(line, msg="Avoid magic numbers."):
        return codeql_findings.Finding(
            path=path, line=line, rule="cpp/fprime/magic-numbers",
            severity="medium", message=msg, module="Svc/CmdDispatcher")
    def d(line, msg="Avoid magic numbers."):
        return codeql_findings.DismissedAlert(
            path=path, line=line, rule="cpp/fprime/magic-numbers",
            message=msg, reason="won't fix", comment="", module="Svc/CmdDispatcher")

    # Three identical-message findings, one dismissal: two stay active,
    # and the dismissal claims the closest (exact-line) finding.
    findings = [f(10), f(50), f(90)]
    active, detected = codeql_findings.partition_dismissed(findings, [d(50)])
    assert active == [f(10), f(90)]
    assert len(detected) == 1

    # Two dismissals -> two claimed, one active.
    active, detected = codeql_findings.partition_dismissed(findings, [d(50), d(11)])
    assert active == [f(90)]
    assert len(detected) == 2

    # A dismissal with no line info claims only one finding, not all.
    active, detected = codeql_findings.partition_dismissed(
        [f(10, "msg A"), f(50, "msg B")], [d(0, "")])
    assert len(active) == 1 and len(detected) == 1


def test_stale_dismissed_alerts_not_shown():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "dest"
        dest.mkdir()
        sarif = _sample_sarif(tmp)
        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
        ])
        # One dismissal still detected by the SARIF, one stale (no longer found)
        dismissed = tmp / "dismissed.json"
        dismissed.write_text(json.dumps([
            {"rule": "cpp/high-risk", "path": "Svc/CmdDispatcher/CmdDispatcher.cpp",
             "line": 44, "message": "Dangerous thing.",
             "reason": "won't fix", "comment": "accepted risk per review"},
            {"rule": "cpp/retired-rule", "path": "Svc/CmdDispatcher/CmdDispatcher.cpp",
             "line": 10, "message": "Old finding no longer detected.",
             "reason": "false positive", "comment": "stale dismissal"},
        ]), encoding="utf-8")

        rc = codeql_findings.main([
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--repo-url", "https://github.com/org/repo",
            "--dismissed-alerts", str(dismissed),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ])
        assert rc == 0

        # Only the still-detected dismissal is counted and tabulated
        cmd = json.loads((dest / "Svc/CmdDispatcher/codeql/summary.json").read_text())
        assert cmd["dismissed"] == 1
        page = (dest / "Svc/CmdDispatcher/codeql/index.html").read_text()
        assert "Dismissed findings (1)" in page
        assert "cpp/retired-rule" not in page
        assert "stale dismissal" not in page

        top = json.loads((dest / "codeql/summary.json").read_text())
        assert top["dismissed"] == 1
        global_page = (dest / "codeql/index.html").read_text()
        assert "cpp/retired-rule" not in global_page

        # partition_dismissed drops stale alerts, keeps detected ones
        d = codeql_findings.load_dismissed_alerts(dismissed, ["Svc/CmdDispatcher"])
        f = codeql_findings.Finding(
            path="Svc/CmdDispatcher/CmdDispatcher.cpp", line=42, rule="cpp/high-risk",
            severity="error", message="Dangerous thing.", module="Svc/CmdDispatcher")
        active, detected = codeql_findings.partition_dismissed([f], d)
        assert active == []
        assert [x.rule for x in detected] == ["cpp/high-risk"]


def test_fetch_dismissed_alerts_extract():
    alerts = [
        {
            "rule": {"id": "cpp/high-risk"},
            "most_recent_instance": {
                "location": {"path": "Fw/Foo/Bar.cpp", "start_line": 12}
            },
            "dismissed_reason": "false positive",
            "dismissed_comment": "autocoded region",
        },
        {"rule": {}, "most_recent_instance": {}},  # malformed -> skipped
    ]
    alerts[0]["most_recent_instance"]["message"] = {"text": "Bad thing here."}
    out = fetch_dismissed_alerts.extract(alerts)
    assert out == [{
        "rule": "cpp/high-risk", "path": "Fw/Foo/Bar.cpp", "line": 12,
        "message": "Bad thing here.",
        "reason": "false positive", "comment": "autocoded region",
    }]


def test_codeql_findings_per_module_pages_and_summaries():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "dest"
        dest.mkdir()
        sarif = _sample_sarif(tmp)
        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
            {"path": "Fw/Clean", "has_ut": True},
        ])

        rc = codeql_findings.main([
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--repo-url", "https://github.com/org/repo",
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ])
        assert rc == 0

        # Module with a high-severity finding -> error/bronze
        cmd = json.loads((dest / "Svc/CmdDispatcher/codeql/summary.json").read_text())
        assert cmd["findings"] == 1
        assert cmd["worst"] == "error"  # security-severity 8.8 overrides SARIF warning
        assert cmd["tier"] == "bronze"
        page = (dest / "Svc/CmdDispatcher/codeql/index.html").read_text()
        assert "cpp/high-risk" in page
        assert "Dangerous thing." in page
        assert "blob/deadbeefcafe1234/Svc/CmdDispatcher/CmdDispatcher.cpp#L42" in page

        # Module with a note-level finding -> low/silver
        i2c = json.loads((dest / "Drv/I2c/codeql/summary.json").read_text())
        assert i2c["findings"] == 1
        assert i2c["worst"] == "low"
        assert i2c["tier"] == "silver"

        # Clean module -> platinum with clean page
        clean = json.loads((dest / "Fw/Clean/codeql/summary.json").read_text())
        assert clean["findings"] == 0
        assert clean["worst"] is None
        assert clean["tier"] == "platinum"
        assert "Clean" in (dest / "Fw/Clean/codeql/index.html").read_text()

        # Global page includes everything, incl. the unmapped finding
        glob = json.loads((dest / "codeql/summary.json").read_text())
        assert glob["findings"] == 3
        global_page = (dest / "codeql/index.html").read_text()
        assert "(other)" in global_page
        assert "cmake/helper.cpp" in global_page


def test_codeql_findings_idempotent_rerun_clears_stale():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "dest"
        dest.mkdir()
        sarif = _sample_sarif(tmp)
        modules_jsonl = _modules_jsonl([{"path": "Svc/CmdDispatcher", "has_ut": True}])
        argv = [
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
        ]
        assert codeql_findings.main(argv) == 0
        stale = dest / "Svc/CmdDispatcher/codeql/stale.html"
        stale.write_text("stale", encoding="utf-8")
        assert codeql_findings.main(argv) == 0
        assert not stale.exists()


def test_catalog_merges_codeql_and_coverage():
    """Simulate the two-writer flow: coverage publishes, then codeql publishes."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "src"
        dest = tmp / "dest"
        source.mkdir()
        dest.mkdir()

        _place_coverage(_make_module(source, "Svc/CmdDispatcher"), "summary_high.json")
        _place_coverage(_make_module(source, "Drv/I2c"), "summary_low.json")
        (source / "coverage").mkdir(exist_ok=True)
        shutil.copy2(FIXTURES / "summary_high.json", source / "coverage" / "summary.json")

        records = [
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
        ]
        modules_jsonl = _modules_jsonl(records)
        _mirror_all(source, dest, records)

        catalog_argv = [
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--ref-type", "branch",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ]
        assert catalog.main(catalog_argv) == 0

        # Second writer: codeql publishes into the same tree, regenerates catalog
        sarif = _sample_sarif(tmp)
        assert codeql_findings.main([
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
        ]) == 0
        assert catalog.main(catalog_argv) == 0

        cat_doc = json.loads((dest / "catalog.json").read_text(encoding="utf-8"))
        by_path = {m["path"]: m for m in cat_doc["modules"]}
        # Coverage data survived the codeql pass
        assert by_path["Svc/CmdDispatcher"]["ut"]["line_pct"] == 98.0
        assert by_path["Svc/CmdDispatcher"]["tiers"]["ut"] == "platinum"
        # CodeQL data present
        assert by_path["Svc/CmdDispatcher"]["codeql"]["findings"] == 1
        assert by_path["Svc/CmdDispatcher"]["tiers"]["codeql"] == "bronze"
        assert by_path["Drv/I2c"]["tiers"]["codeql"] == "silver"
        assert cat_doc["overall_codeql"]["findings"] == 3

        index_html = (dest / "index.html").read_text(encoding="utf-8")
        assert "badge-silver" in index_html
        assert "1 finding" in index_html
        assert "codeql/index.html" in index_html


def test_catalog_multiple_codeql_checks():
    """Two codeql publishers (e.g. security + jpl-standard) coexist and roll up."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "dest"
        dest.mkdir()
        sarif = _sample_sarif(tmp)
        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Drv/I2c", "has_ut": True},
        ])

        common = [
            "--sarif", str(sarif),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
        ]
        assert codeql_findings.main(
            common + ["--codeql-subdirectory", "codeql", "--check-label", "CodeQL Security"]
        ) == 0
        assert codeql_findings.main(
            common + ["--codeql-subdirectory", "codeql-jpl", "--check-label", "CodeQL JPL Standard"]
        ) == 0

        assert catalog.main([
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--ref-type", "branch",
            "--commit", "deadbeefcafe1234",
            "--generated-at", "2026-05-18T17:30:00Z",
        ]) == 0

        cat_doc = json.loads((dest / "catalog.json").read_text(encoding="utf-8"))
        by_path = {m["path"]: m for m in cat_doc["modules"]}
        cmd = by_path["Svc/CmdDispatcher"]
        # Aggregate sums both checks (same SARIF published twice -> 2 findings)
        assert cmd["codeql"]["findings"] == 2
        assert set(cmd["codeql_checks"]) == {"codeql", "codeql-jpl"}
        assert cmd["codeql_checks"]["codeql"]["findings"] == 1
        assert cmd["codeql_checks"]["codeql"]["label"] == "CodeQL Security"
        assert cmd["codeql_checks"]["codeql-jpl"]["label"] == "CodeQL JPL Standard"
        assert cat_doc["overall_codeql"]["findings"] == 6
        assert cat_doc["overall_codeql_checks"]["codeql"]["findings"] == 3

        # Landing page lists each check in the header strip
        index_html = (dest / "index.html").read_text(encoding="utf-8")
        assert "CodeQL Security:" in index_html
        assert "CodeQL JPL Standard:" in index_html

        # Per-module roll-up page has one row per check
        mod_page = (dest / "Svc/CmdDispatcher/index.html").read_text(encoding="utf-8")
        assert ">CodeQL Security</td>" in mod_page
        assert ">CodeQL JPL Standard</td>" in mod_page
        assert 'href="codeql/index.html"' in mod_page
        assert 'href="codeql-jpl/index.html"' in mod_page

        # Main index module row links to the roll-up page for the codeql cell
        assert 'href="Svc/CmdDispatcher/index.html">2 findings</a>' in index_html


def test_compare_flags_regression_and_new_module():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        baseline = Path(tmp) / "baseline"
        source.mkdir()
        baseline.mkdir()

        # PR side: Svc/CmdDispatcher regressed from high -> mid (98 -> 86)
        (source / "Svc" / "CmdDispatcher" / "coverage").mkdir(parents=True)
        shutil.copy2(FIXTURES / "summary_mid.json", source / "Svc/CmdDispatcher/coverage/summary.json")
        # New module on PR
        (source / "Svc" / "Foo" / "coverage").mkdir(parents=True)
        shutil.copy2(FIXTURES / "summary_high.json", source / "Svc/Foo/coverage/summary.json")
        # Global PR
        (source / "coverage").mkdir(exist_ok=True)
        shutil.copy2(FIXTURES / "summary_mid.json", source / "coverage/summary.json")

        # Baseline side: CmdDispatcher had high coverage
        (baseline / "Svc" / "CmdDispatcher" / "coverage").mkdir(parents=True)
        shutil.copy2(FIXTURES / "summary_high.json", baseline / "Svc/CmdDispatcher/coverage/summary.json")
        # Baseline global
        (baseline / "coverage").mkdir(exist_ok=True)
        shutil.copy2(FIXTURES / "summary_high.json", baseline / "coverage/summary.json")

        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
            {"path": "Svc/Foo", "has_ut": True},
            {"path": "Drv/LinuxGpio", "has_ut": False},
        ])
        out = Path(tmp) / "comment.md"
        regs = Path(tmp) / "regressions.json"

        rc = compare.main([
            "--source", str(source),
            "--baseline", str(baseline),
            "--modules-jsonl", str(modules_jsonl),
            "--coverage-subdirectory", "coverage",
            "--base-ref", "devel",
            "--threshold", "0.5",
            "--output", str(out),
            "--regressions-output", str(regs),
        ])
        assert rc == 0  # threshold ok but fail-on-regression not set
        body = out.read_text(encoding="utf-8")
        assert "Svc/CmdDispatcher" in body
        assert "-12.00" in body  # line delta: 86 - 98 = -12
        assert "-8.33" in body  # function delta: mid(85.0) - high(93.33)
        assert "Svc/Foo" in body
        assert "#### New modules" in body
        assert "#### Modules without UTs" in body
        assert "Drv/LinuxGpio" in body
        regression_list = json.loads(regs.read_text(encoding="utf-8"))
        paths = {r["path"] for r in regression_list}
        assert "Svc/CmdDispatcher" in paths
        # Now with fail-on-regression we expect a nonzero exit
        rc2 = compare.main([
            "--source", str(source),
            "--baseline", str(baseline),
            "--modules-jsonl", str(modules_jsonl),
            "--coverage-subdirectory", "coverage",
            "--base-ref", "devel",
            "--threshold", "0.5",
            "--output", str(out),
            "--regressions-output", str(regs),
            "--fail-on-regression",
        ])
        assert rc2 == 1


def test_compare_baseline_missing_reports_no_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        baseline = Path(tmp) / "baseline"
        source.mkdir()
        baseline.mkdir()
        (source / "Svc" / "CmdDispatcher" / "coverage").mkdir(parents=True)
        shutil.copy2(FIXTURES / "summary_high.json", source / "Svc/CmdDispatcher/coverage/summary.json")

        modules_jsonl = _modules_jsonl([
            {"path": "Svc/CmdDispatcher", "has_ut": True},
        ])
        out = Path(tmp) / "comment.md"
        rc = compare.main([
            "--source", str(source),
            "--baseline", str(baseline),
            "--modules-jsonl", str(modules_jsonl),
            "--coverage-subdirectory", "coverage",
            "--base-ref", "devel",
            "--threshold", "0.5",
            "--output", str(out),
            "--baseline-missing",
        ])
        assert rc == 0
        body = out.read_text(encoding="utf-8")
        assert "No baseline branch" in body
        # No regressions can be flagged when baseline is missing
        assert "#### Regressions" in body
        assert "_(none over threshold)_" in body


# ---------------------------------------------------------------------------
def test_mirror_int_artifact_lands_in_int_coverage():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        dest = Path(tmp) / "dest"
        source.mkdir()
        dest.mkdir()

        _place_coverage(_make_module(source, "Svc/Health"), "summary_mid.json")
        _make_module(source, "Drv/LinuxGpio", with_ut=False)
        (source / "coverage").mkdir(exist_ok=True)
        shutil.copy2(FIXTURES / "summary_high.json", source / "coverage" / "summary.json")

        # Pre-existing UT artifacts on the baseline branch must survive.
        ut_dir = dest / "Svc/Health" / "coverage"
        ut_dir.mkdir(parents=True)
        (ut_dir / "summary.json").write_text(
            (FIXTURES / "summary_high.json").read_text(encoding="utf-8"), encoding="utf-8"
        )

        records = [
            {"path": "Svc/Health", "has_ut": True},
            {"path": "Drv/LinuxGpio", "has_ut": False},
        ]
        modules_jsonl = _modules_jsonl(records)

        rc = mirror.main([
            "--source", str(source),
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--artifact", "int",
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
        ])
        assert rc == 0

        # Int artifacts land under int-coverage/ at module and root level.
        assert (dest / "Svc/Health" / "int-coverage" / "summary.json").is_file()
        assert (dest / "Svc/Health" / "int-coverage" / "index.html").is_file()
        assert (dest / "int-coverage" / "summary.json").is_file()
        # Unexercised module gets an int-specific placeholder.
        gpio = (dest / "Drv/LinuxGpio" / "int-coverage" / "index.html").read_text(encoding="utf-8")
        assert "integration-test" in gpio
        # UT artifacts were left untouched.
        assert (dest / "Svc/Health" / "coverage" / "summary.json").is_file()

        cat_doc = json.loads((dest / "catalog.json").read_text(encoding="utf-8"))
        assert cat_doc["overall_int"]["line_pct"] == 98.0
        assert cat_doc["overall_int"]["report"] == "int-coverage/coverage-all.html"
        by_path = {m["path"]: m for m in cat_doc["modules"]}
        assert by_path["Svc/Health"]["int"]["report"] == "Svc/Health/int-coverage/index.html"
        assert by_path["Svc/Health"]["tiers"]["int"] is not None

        index_html = (dest / "index.html").read_text(encoding="utf-8")
        assert '<strong>Int coverage:</strong> <span class="no-cov">no data</span>' not in index_html
        assert 'href="int-coverage/coverage-all.html"' in index_html

        # Module page links its filtered report and the full-system record.
        mod_page = (dest / "Svc/Health" / "index.html").read_text(encoding="utf-8")
        assert 'href="int-coverage/index.html"' in mod_page
        assert "full-system report" in mod_page
        assert "../../int-coverage/coverage-all.html#:~:text=Svc/Health" in mod_page


def test_catalog_without_int_data_keeps_no_data_cell():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "dest"
        dest.mkdir()
        records = [{"path": "Svc/Health", "has_ut": True}]
        modules_jsonl = _modules_jsonl(records)

        rc = catalog.main([
            "--dest", str(dest),
            "--modules-jsonl", str(modules_jsonl),
            "--ref", "devel",
            "--commit", "deadbeefcafe1234",
        ])
        assert rc == 0

        cat_doc = json.loads((dest / "catalog.json").read_text(encoding="utf-8"))
        assert cat_doc["overall_int"] is None
        index_html = (dest / "index.html").read_text(encoding="utf-8")
        assert '<strong>Int coverage:</strong> <span class="no-cov">no data</span>' in index_html
        mod_page = (dest / "Svc/Health" / "index.html").read_text(encoding="utf-8")
        assert "full-system report" not in mod_page


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode("utf-8").strip()


def _publish_branch(remote: Path, work: Path, branch: str, files: dict) -> str:
    """Commit ``files`` as the whole tree of orphan ``branch`` and push it to ``remote``."""
    _publish_branch.count = getattr(_publish_branch, "count", 0) + 1
    _git(work, "checkout", "-q", "--orphan", f"publish-{_publish_branch.count}")
    _git(work, "rm", "-rfq", "--ignore-unmatch", ".")
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", branch)
    _git(work, "push", "-qf", str(remote), f"HEAD:refs/heads/{branch}")
    return _git(work, "rev-parse", "HEAD")


def _version_page(ref: str, ref_type: str) -> dict:
    return {
        "index.html": f"<html><body><h1>{ref}</h1></body></html>",
        "catalog.json": json.dumps({"ref": ref, "ref_type": ref_type}),
        "Svc/Health/index.html": "<html><body>health</body></html>",
        "versions/stale/index.html": "<html><body>nested</body></html>",
    }


def test_versions_sync_copies_other_refs_and_links_them():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        work = root / "work"
        work.mkdir()
        _git(work, "init", "-q")
        _publish_branch(remote, work, "coverage/devel", {"index.html": "<html><body>devel</body></html>"})
        _publish_branch(remote, work, "coverage/v4.4.2", _version_page("v4.4.2", "tag"))
        _publish_branch(remote, work, "coverage/v4.10.0", _version_page("v4.10.0", "tag"))
        rel_head = _publish_branch(
            remote, work, "coverage/release/v4.4.x", _version_page("release/v4.4.x", "branch")
        )
        _publish_branch(remote, work, "coverage/broken", {"README": "no landing page"})
        _publish_branch(remote, work, "unrelated/v1", _version_page("v1", "tag"))

        dest = root / "dest"
        dest.mkdir()
        data = versions.sync_versions(
            dest=dest, ref="devel", prefix="coverage", remote=str(remote), cwd=work
        )
        # Newest version first; a release line sorts above its own tags; the
        # primary ref, non-site branches and other prefixes are not listed.
        assert [v["ref"] for v in data["versions"]] == ["v4.10.0", "release/v4.4.x", "v4.4.2"]
        assert data["primary"] == "devel"

        # A cap keeps the newest versions only and removes older copies.
        capped_dest = root / "capped"
        capped_dest.mkdir()
        versions.sync_versions(dest=capped_dest, ref="devel", prefix="coverage", remote=str(remote), cwd=work)
        capped = versions.sync_versions(
            dest=capped_dest, ref="devel", prefix="coverage", remote=str(remote), cwd=work, max_versions=2
        )
        assert [v["ref"] for v in capped["versions"]] == ["v4.10.0", "release/v4.4.x"]
        assert not (capped_dest / "versions/v4.4.2").exists()
        assert [v["ref_type"] for v in data["versions"]] == ["tag", "branch", "tag"]
        assert data["versions"][1]["baseline_commit"] == rel_head
        assert json.loads((dest / "versions.json").read_text(encoding="utf-8")) == data
        assert not (dest / "versions/broken").exists()
        assert not (dest / "versions/devel").exists()

        # Module pages are copied; a version's own versions/ subtree is not.
        assert (dest / "versions/release/v4.4.x/Svc/Health/index.html").is_file()
        assert not (dest / "versions/release/v4.4.x/versions").exists()

        # Copied landing pages carry the navigation, relative to their depth.
        page = (dest / "versions/release/v4.4.x/index.html").read_text(encoding="utf-8")
        assert page.count(versions.NAV_START) == 1
        assert '<a href="../../../index.html"><code>devel</code></a>' in page
        assert "<strong><code>release/v4.4.x</code></strong>" in page
        assert '<a href="../../../versions/v4.4.2/index.html">' in page
        assert page.index(versions.NAV_START) < page.index("<h1>")

        # Unchanged heads are not re-copied; a changed head is; a deleted
        # baseline branch is removed.  Re-injection never duplicates the nav.
        marker = dest / "versions/v4.4.2/local-marker"
        marker.write_text("kept", encoding="utf-8")
        changed = _version_page("release/v4.4.x", "branch")
        changed["Svc/New/index.html"] = "<html><body>new</body></html>"
        del changed["Svc/Health/index.html"]
        _publish_branch(remote, work, "coverage/release/v4.4.x", changed)
        _git(work, "push", "-q", str(remote), ":refs/heads/coverage/v4.10.0")
        data = versions.sync_versions(
            dest=dest, ref="devel", prefix="coverage", remote=str(remote), cwd=work
        )
        assert [v["ref"] for v in data["versions"]] == ["release/v4.4.x", "v4.4.2"]
        assert marker.is_file()
        assert (dest / "versions/release/v4.4.x/Svc/New/index.html").is_file()
        assert not (dest / "versions/release/v4.4.x/Svc/Health").exists()
        assert not (dest / "versions/v4.10.0").exists()
        page = (dest / "versions/v4.4.2/index.html").read_text(encoding="utf-8")
        assert page.count(versions.NAV_START) == 1
        assert "v4.10.0" not in page

        # Last other version gone: no versions/ directory, empty list.
        _git(work, "push", "-q", str(remote), ":refs/heads/coverage/v4.4.2",
             ":refs/heads/coverage/release/v4.4.x")
        data = versions.sync_versions(
            dest=dest, ref="devel", prefix="coverage", remote=str(remote), cwd=work
        )
        assert data["versions"] == []
        assert not (dest / "versions").exists()
        assert versions.render_nav(data, current="devel") == ""


def test_versions_main_skips_non_primary_ref():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        # No git access is attempted for a non-primary ref.
        assert versions.main(["--dest", str(dest), "--ref", "release/v4.4.x", "--remote", "/nonexistent"]) == 0
        assert versions.main(["--dest", str(dest), "--ref", "devel", "--primary-ref", ""]) == 0
        assert not (dest / "versions.json").exists()


def test_catalog_renders_versions_nav_from_versions_json():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "dest"
        dest.mkdir()
        modules = root / "modules.jsonl"
        modules.write_text(json.dumps({"path": "Svc/Health", "has_ut": True}) + "\n", encoding="utf-8")
        argv = ["--dest", str(dest), "--modules-jsonl", str(modules), "--ref", "devel", "--commit", "a" * 40]

        assert catalog.main(argv) == 0
        assert versions.NAV_START not in (dest / "index.html").read_text(encoding="utf-8")

        (dest / "versions.json").write_text(
            json.dumps({
                "schema": 1, "primary": "devel",
                "versions": [{"ref": "release/<x>", "ref_type": "branch", "path": "versions/release/<x>"}],
            }),
            encoding="utf-8",
        )
        assert catalog.main(argv) == 0
        index_html = (dest / "index.html").read_text(encoding="utf-8")
        assert "<strong><code>devel</code></strong>" in index_html
        assert '<a href="versions/release/&lt;x&gt;/index.html"><code>release/&lt;x&gt;</code></a>' in index_html

        (dest / "versions.json").write_text("not json", encoding="utf-8")
        assert catalog.main(argv) == 0
        assert versions.NAV_START not in (dest / "index.html").read_text(encoding="utf-8")


# Runner
# ---------------------------------------------------------------------------


TESTS = [
    test_discover_finds_modules_and_ut_flag,
    test_discover_finds_library_modules,
    test_discover_excludes_autocoder_only_modules_by_default,
    test_discover_ignores_commented_out_calls,
    test_summary_load_handles_missing_file_and_zero_total,
    test_summary_load_parses_gcovr_json,
    test_mirror_module_renames_html_when_coverage_present,
    test_mirror_module_writes_placeholder_when_no_ut,
    test_catalog_relative_path_math,
    test_mirror_flatten_drops_coverage_subdir,
    test_mirror_module_is_idempotent,
    test_tier_computation,
    test_config_loading,
    test_catalog_groups_and_rollup,
    test_codeql_findings_per_module_pages_and_summaries,
    test_dismissed_alerts_excluded_and_tabulated,
    test_dismissal_matching_is_one_to_one,
    test_stale_dismissed_alerts_not_shown,
    test_fetch_dismissed_alerts_extract,
    test_codeql_findings_idempotent_rerun_clears_stale,
    test_catalog_merges_codeql_and_coverage,
    test_catalog_multiple_codeql_checks,
    test_compare_flags_regression_and_new_module,
    test_compare_baseline_missing_reports_no_baseline,
    test_mirror_int_artifact_lands_in_int_coverage,
    test_catalog_without_int_data_keeps_no_data_cell,
    test_versions_sync_copies_other_refs_and_links_them,
    test_versions_main_skips_non_primary_ref,
    test_catalog_renders_versions_nav_from_versions_json,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                t()
            print(f"OK   {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            print(buf.getvalue())
            traceback.print_exc()
    print()
    if failed:
        print(f"{failed} test(s) failed.")
        return 1
    print(f"All {len(TESTS)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
