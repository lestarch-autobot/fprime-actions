"""Unit tests for the component-checks scripts.

Run with:  python3 component-checks/tests/test_checks.py

No external dependencies.  Each test prints OK or FAIL and the runner
exits 1 on any failure.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"
COMMON_SCRIPTS = HERE.parent.parent / "coverage-common" / "scripts"
FIXTURES = HERE / "fixtures"
WIDGET = FIXTURES / "Svc" / "Widget"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(COMMON_SCRIPTS))

import _fpp  # noqa: E402
import _requirements  # noqa: E402
import check_build_warnings  # noqa: E402
import check_command_handlers  # noqa: E402
import check_coverage_threshold  # noqa: E402
import check_dp_priorities  # noqa: E402
import check_dp_priority_configurable  # noqa: E402
import check_fpp_generate  # noqa: E402
import check_include_dependencies  # noqa: E402
import check_leaks  # noqa: E402
import check_port_handlers  # noqa: E402
import check_req_commands  # noqa: E402
import check_req_data_products  # noqa: E402
import check_req_design_mapping  # noqa: E402
import check_req_events  # noqa: E402
import check_req_parameters  # noqa: E402
import check_req_parent_trace  # noqa: E402
import check_req_ports  # noqa: E402
import check_req_telemetry  # noqa: E402
import check_req_verification  # noqa: E402
import check_req_kinds  # noqa: E402
import check_sdd_port_table  # noqa: E402
import check_sdd_dictionary_tables  # noqa: E402
import check_sdd_sections  # noqa: E402
import check_sdd_port_behavior  # noqa: E402
import check_hardware_run  # noqa: E402
import check_ci_status  # noqa: E402
import check_sdd  # noqa: E402
import _doxygen  # noqa: E402
import check_sm_actions  # noqa: E402
import check_static_analysis  # noqa: E402
import check_topology_build  # noqa: E402
import check_ut_artifacts  # noqa: E402
import check_ut_include_dependencies  # noqa: E402
import run_checks  # noqa: E402
from _tiers import checks_tier  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    if condition:
        print(f"OK   {name}")
    else:
        print(f"FAIL {name} {extra}")
        FAILURES.append(name)


def run(main, argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def test_fpp_parsing() -> None:
    model = _fpp.load_module_model(WIDGET)
    check("fpp: commands", sorted(model.commands) == ["SET_PRIORITY", "START", "STOP"])
    check("fpp: telemetry", model.telemetry == ["WidgetCount"])
    check("fpp: events", sorted(model.events) == ["WidgetError", "WidgetStarted"])
    check("fpp: parameters", model.parameters == ["THRESHOLD"])
    check("fpp: records", model.records == ["WidgetRecord"])
    check(
        "fpp: containers",
        [(c.name, c.has_default_priority) for c in model.containers]
        == [("WidgetContainer", True), ("OrphanContainer", False)],
    )
    check(
        "fpp: input ports",
        [(p.name, p.kind) for p in model.input_ports] == [("run", "async"), ("dataIn", "sync")],
    )
    check("fpp: output ports", [p.name for p in model.output_ports] == ["dataOut"])
    machines = {m.name: m.actions for m in model.state_machines}
    check("fpp: state machine actions", machines.get("DeviceSm") == ["doStart", "doStop"])


def test_requirements_parsing() -> None:
    reqs = _requirements.load_requirements([WIDGET / "docs" / "sdd.md"])
    check("reqs: count", len(reqs) == 9, f"got {len(reqs)}")
    by_id = {r.req_id: r for r in reqs}
    check("reqs: parent parsed", by_id["WID-001"].parent == "SYS-010")
    check("reqs: verification parsed", by_id["WID-004"].verification == "Analysis")
    check("reqs: missing parent empty", by_id["WID-007"].parent == "")


def test_requirement_checks() -> None:
    for name, mod in (
        ("R1 commands", check_req_commands),
        ("R2 telemetry", check_req_telemetry),
        ("R3 events", check_req_events),
        ("R4 parameters", check_req_parameters),
        ("R5 data products", check_req_data_products),
        ("R6 ports", check_req_ports),
    ):
        code, out = run(mod.main, ["--module", str(WIDGET)])
        check(f"{name}: passes", code == 0, out)

    code, out = run(check_req_parent_trace.main, ["--module", str(WIDGET)])
    check("R7: fails on WID-007", code == 1 and "WID-007" in out, out)

    code, out = run(check_req_verification.main, ["--module", str(WIDGET)])
    check("R8: passes", code == 0, out)

    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod_dir)
        sdd = mod_dir / "docs" / "sdd.md"
        sdd.write_text(sdd.read_text().replace("| Analysis", "| TBD"))
        code, out = run(check_req_verification.main, ["--module", str(mod_dir)])
        check("R8: fails on missing method", code == 1 and "WID-004" in out, out)

    # Remove a requirement reference -> R1 fails
    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod_dir)
        sdd = mod_dir / "docs" / "sdd.md"
        sdd.write_text(sdd.read_text().replace("START and STOP", "begin and halt"))
        code, out = run(check_req_commands.main, ["--module", str(mod_dir)])
        check("R1: fails on untraced command", code == 1 and "START" in out, out)


def test_sdd_consistency_checks() -> None:
    code, out = run(check_req_kinds.main, ["--module", str(WIDGET)])
    check("R9: passes (all kinds covered or waived)", code == 0, out)
    for name, mod in (
        ("D7 port table", check_sdd_port_table),
        ("D8 dictionary tables", check_sdd_dictionary_tables),
        ("D9 sections", check_sdd_sections),
        ("D10 port behavior", check_sdd_port_behavior),
    ):
        code, out = run(mod.main, ["--module", str(WIDGET)])
        check(f"{name}: passes", code == 0, out)
    code, out = run(check_hardware_run.main, ["--module", str(WIDGET)])
    check("C2: passes on well-formed record", code == 0, out)

    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod)
        sdd = mod / "docs" / "sdd.md"
        text = sdd.read_text()
        broken = text.replace("| dataOut | output | Fw.BufferSend |", "| dataOut | input | Fw.BufferSend |")
        broken = broken.replace("| STOP | Stop processing |\n", "")
        broken = broken.replace("Hardware I/O requirements: N/A", "")
        broken = broken.replace("`dataIn` to `dataOut`", "the input to the output")
        broken = broken.replace("sequenceDiagram", "graph TD")
        broken = broken.replace("| Behavior | SYS-010 |", "|  | SYS-010 |")
        sdd.write_text(broken)
        code, out = run(check_req_kinds.main, ["--module", str(mod)])
        check("R9: fails on untyped and missing kinds", code == 1 and "WID-001" in out and "hardware I/O" in out, out)
        code, out = run(check_sdd_port_table.main, ["--module", str(mod)])
        check("D7: fails on direction mismatch", code == 1 and "dataOut" in out, out)
        code, out = run(check_sdd_dictionary_tables.main, ["--module", str(mod)])
        check("D8: fails on missing command", code == 1 and "STOP" in out, out)
        code, out = run(check_sdd_sections.main, ["--module", str(mod)])
        check("D9: fails on missing sequence diagram", code == 1 and "sequence" in out, out)
        code, out = run(check_sdd_port_behavior.main, ["--module", str(mod)])
        check("D10: fails on undescribed input port", code == 1 and "dataIn" in out, out)
        (mod / "docs" / "hardware-run.md").write_text("Platform: Pi\nResult: FAIL\nCommit: nothex\n")
        code, out = run(check_hardware_run.main, ["--module", str(mod)])
        check("C2: fails on bad record", code == 1 and "not PASS" in out and "Deployment" in out, out)
        (mod / "docs" / "hardware-run.md").unlink()
        code, out = run(check_hardware_run.main, ["--module", str(mod)])
        check("C2: fails without record", code == 1 and "no docs/hardware-run.md" in out, out)

        runs = Path(tmp) / "runs.json"
        runs.write_text(json.dumps({"check_runs": [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "ut", "status": "completed", "conclusion": "failure", "html_url": "u"},
            {"name": "lint", "status": "in_progress", "conclusion": None},
        ]}))
        code, out = run(check_ci_status.main, ["--module", str(mod), "--check-runs-json", str(runs)])
        check("C3: fails on failed/incomplete runs", code == 1 and "ut" in out and "lint" in out, out)
        code, out = run(check_ci_status.main, ["--module", str(mod), "--check-runs-json", str(runs), "--ignore", "ut", "--ignore", "lint"])
        check("C3: passes with ignores", code == 0, out)
        code, out = run(check_ci_status.main, ["--module", str(mod)])
        check("C3: skips offline", code == 0 and "SKIP" in out, out)


def test_design_checks() -> None:
    code, out = run(check_port_handlers.main, ["--module", str(WIDGET)])
    check("D1: passes", code == 0, out)

    code, out = run(check_command_handlers.main, ["--module", str(WIDGET)])
    check("D2: passes", code == 0, out)

    code, out = run(check_sm_actions.main, ["--module", str(WIDGET)])
    check("D3: fails on doStop", code == 1 and "doStop" in out, out)

    code, out = run(check_dp_priorities.main, ["--module", str(WIDGET)])
    check("D4: fails on OrphanContainer", code == 1 and "OrphanContainer" in out, out)

    code, out = run(check_req_design_mapping.main, ["--module", str(WIDGET)])
    check("D5: fails on WID-007", code == 1 and "WID-007" in out, out)

    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod_dir)
        cpp = mod_dir / "Widget.cpp"
        cpp.write_text(cpp.read_text().replace("STOP_cmdHandler", "IGNORED"))
        code, out = run(check_command_handlers.main, ["--module", str(mod_dir)])
        check("D2: fails on missing handler", code == 1 and "STOP" in out, out)

        cpp.write_text(cpp.read_text().replace("dataIn_handler", "IGNORED"))
        code, out = run(check_port_handlers.main, ["--module", str(mod_dir)])
        check("D1: fails on missing handler", code == 1 and "dataIn" in out, out)


def test_implementation_checks() -> None:
    code, out = run(check_include_dependencies.main, ["--module", str(WIDGET)])
    check("I1: fails on Fw/Buffer", code == 1 and "Fw/Buffer" in out, out)
    check("I1: Os accepted", "'Os'" not in out, out)

    code, out = run(check_dp_priority_configurable.main, ["--module", str(WIDGET)])
    check("I3: passes via SET_PRIORITY", code == 0, out)

    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod_dir)
        fpp = mod_dir / "Widget.fpp"
        fpp.write_text(fpp.read_text().replace("SET_PRIORITY(priority: U32)", "SET_MODE(mode: U32)"))
        code, out = run(check_dp_priority_configurable.main, ["--module", str(mod_dir)])
        check(
            "I3: fails without priority knob",
            code == 1 and "WidgetContainer" in out and "OrphanContainer" in out,
            out,
        )

    # System/platform and same-module includes are not F´ dependencies
    dep = check_include_dependencies.dependency_of
    for include in ("sys/socket.h", "arpa/inet.h", "linux/gpio.h", "mach/mach.h", "openssl/evp.h"):
        check(f"I1: system include skipped ({include})", dep(include) is None)
    check("I1: F´ include mapped", dep("Fw/Types/Assert.hpp") == "Fw/Types")
    check("I1: test helper maps to module", dep("Drv/Ip/test/ut/SocketTestHelper.hpp") == "Drv/Ip")
    check("I1: root umbrella header skipped", dep("Fw/FPrimeBasicTypes.hpp") is None)
    check(
        "I1: directly-included source skipped",
        dep("FppTest/component/common/typed.cpp") is None,
    )

    # Ancestor umbrellas and non-target shared directories are not reportable
    reportable = check_include_dependencies.is_reportable_dependency
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        module = root / "FppTest" / "component" / "queued"
        module.mkdir(parents=True)
        shared = root / "FppTest" / "component" / "common"
        shared.mkdir()
        target = root / "Fw" / "Buffer"
        target.mkdir(parents=True)
        (target / "CMakeLists.txt").write_text("register_fprime_module()\n")
        check(
            "I1: non-target shared dir not reportable",
            not reportable("FppTest/component/common", module),
        )
        check(
            "I1: registered target dir reportable",
            reportable("Fw/Buffer", module),
        )
        check(
            "I1: ancestor umbrella not reportable",
            not reportable("FppTest", module),
        )
        own_config = module / "QueuedConfig"
        own_config.mkdir()
        (own_config / "CMakeLists.txt").write_text("register_fprime_config()\n")
        check(
            "I1: module's own config subdir not reportable",
            not reportable("QueuedConfig", module),
        )

    # I2 in log mode
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "build.log"
        log.write_text("foo.cpp:10:5: warning: unused variable 'x' [-Wunused]\nok\n")
        code, out = run(
            check_build_warnings.main, ["--module", str(WIDGET), "--log", str(log)]
        )
        check("I2: fails on warning in log", code == 1 and "unused variable" in out, out)
        log.write_text("all good\n")
        code, out = run(
            check_build_warnings.main, ["--module", str(WIDGET), "--log", str(log)]
        )
        check("I2: passes on clean log", code == 0, out)


def test_static_analysis_check() -> None:
    sarif = {
        "runs": [
            {
                "tool": {"driver": {"rules": [
                    {"id": "cpp/rule-a", "properties": {"security-severity": "8.0"}},
                    {"id": "cpp/rule-b"},
                ]}},
                "results": [
                    {
                        "ruleId": "cpp/rule-a",
                        "level": "warning",
                        "message": {"text": "bad thing"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "Svc/Widget/Widget.cpp"},
                            "region": {"startLine": 3},
                        }}],
                    },
                    {
                        "ruleId": "cpp/rule-b",
                        "level": "note",
                        "message": {"text": "minor"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "Svc/Widget/Widget.cpp"},
                            "region": {"startLine": 4},
                        }}],
                    },
                    {
                        "ruleId": "cpp/rule-b",
                        "level": "error",
                        "message": {"text": "elsewhere"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "Svc/Other/Other.cpp"},
                            "region": {"startLine": 5},
                        }}],
                    },
                ],
            }
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        sarif_path = Path(tmp) / "results.sarif"
        sarif_path.write_text(json.dumps(sarif))
        code, out = run(
            check_static_analysis.main,
            ["--module", "Svc/Widget", "--sarif", str(sarif_path)],
        )
        check("I4: fails on error finding", code == 1 and "rule-a" in out, out)
        check("I4: ignores other module", "Other" not in out, out)
        check("I4: low below threshold", "minor" not in out, out)
        code, out = run(
            check_static_analysis.main,
            ["--module", "Svc/Widget", "--sarif", str(sarif_path),
             "--severity-threshold", "low"],
        )
        check("I4: low threshold catches note", code == 1 and "minor" in out, out)


def test_unit_testing_checks() -> None:
    code, out = run(check_ut_include_dependencies.main, ["--module", str(WIDGET)])
    check("U3: passes (STest covers STest/Pick)", code == 0, out)

    with tempfile.TemporaryDirectory() as tmp:
        mod_dir = Path(tmp) / "Svc" / "Widget"
        shutil.copytree(WIDGET, mod_dir)
        cmake = mod_dir / "CMakeLists.txt"
        cmake.write_text(cmake.read_text().replace("STest", "Removed"))
        code, out = run(check_ut_include_dependencies.main, ["--module", str(mod_dir)])
        check("U3: fails on undeclared dependency", code == 1 and "STest/Pick" in out, out)

    # U4 leak regex (sole leak detector on the valgrind output path)
    leak_out = "==1== definitely lost: 8 bytes in 1 blocks\n==1== ERROR SUMMARY: 2 errors"
    check("U4: leak regex matches", len(check_leaks._LEAK_RE.findall(leak_out)) == 2)
    clean_out = "==1== definitely lost: 0 bytes\n==1== ERROR SUMMARY: 0 errors"
    check("U4: leak regex clean", not check_leaks._LEAK_RE.findall(clean_out))

    with tempfile.TemporaryDirectory() as tmp:
        artifacts = Path(tmp)
        code, out = run(
            check_ut_artifacts.main,
            ["--module", "Svc/Widget", "--artifacts-dir", str(artifacts)],
        )
        check("U1: fails when missing", code == 1, out)
        target = artifacts / "Svc" / "Widget" / "coverage"
        target.mkdir(parents=True)
        (target / "summary.json").write_text("{}")
        (target / "index.html").write_text("<html></html>")
        code, out = run(
            check_ut_artifacts.main,
            ["--module", "Svc/Widget", "--artifacts-dir", str(artifacts)],
        )
        check("U1: passes when present", code == 0, out)

        cov = Path(tmp) / "cov-summary.json"
        cov.write_text(json.dumps({"line_total": 100, "line_covered": 91}))
        code, out = run(
            check_coverage_threshold.main,
            ["--module", "Svc/Widget", "--summary-json", str(cov)],
        )
        check("U5: passes above threshold", code == 0, out)
        code, out = run(
            check_coverage_threshold.main,
            ["--module", "Svc/Widget", "--summary-json", str(cov), "--threshold", "95"],
        )
        check("U5: fails below threshold", code == 1 and "91.00%" in out, out)


def test_build_level_checks_offline() -> None:
    # D6 skips cleanly when fpp-check is unavailable
    import shutil as _shutil

    original_which = _shutil.which
    _shutil.which = lambda _name: None
    try:
        code, out = run(check_fpp_generate.main, ["--module", str(WIDGET)])
    finally:
        _shutil.which = original_which
    check("D6: skips without fpp-check", code == 0 and "SKIP" in out, out)

    # C1 topology-instantiation scan (pure text path)
    topo = "instance widget: Svc.Widget base id 0x100\n"
    check("C1: instance found", check_topology_build.instantiated_in("Widget", topo))
    check(
        "C1: commented instance ignored",
        not check_topology_build.instantiated_in("Widget", "# " + topo),
    )
    check(
        "C1: name in comment ignored",
        not check_topology_build.instantiated_in("Widget", "@ Widget goes here\n"),
    )


def test_checks_tier() -> None:
    check("tier: all pass", checks_tier(10, 0) == "platinum")
    check("tier: 90%+", checks_tier(19, 1) == "gold")
    check("tier: 90% boundary", checks_tier(9, 1) == "gold")
    check("tier: 80%+", checks_tier(8, 2) == "silver")
    check("tier: below", checks_tier(1, 9) == "bronze")
    check("tier: none", checks_tier(0, 0) == "bronze")


_SDD_INTRO = "The component manages widget processing with start and stop commands plus telemetry reporting for operators."
_SDD_PROSE = (
    "This section describes the behavior in detail including the handling of "
    "nominal and off nominal cases plus resource usage and timing constraints."
)


def _sdd_doc(sections: int, tables: str = "") -> str:
    parts = ["# Svc::Widget Component", "## 1. Introduction", _SDD_INTRO]
    for i in range(2, sections + 2):
        parts += [f"## {i}. Section {i}", _SDD_PROSE, _SDD_PROSE]
    if tables:
        parts += ["## Interfaces", tables]
    return "\n\n".join(parts) + "\n"


def test_sdd_grading() -> None:
    th = check_sdd.SddThresholds()

    check("sdd: missing is bronze", check_sdd.grade_sdd(None, th) == {"tier": "bronze", "has_sdd": False})

    headings_only = "# Title\n\n## 1. Introduction\n\n## 2. Design\n"
    check("sdd: headings-only is bronze", check_sdd.grade_sdd(headings_only, th)["tier"] == "bronze")

    placeholders = _sdd_doc(4).replace(_SDD_PROSE, "TBD " + _SDD_PROSE)
    graded = check_sdd.grade_sdd(placeholders, th)
    check("sdd: placeholder sections are bronze", graded["tier"] == "bronze", graded["tier"])
    check("sdd: placeholder hits counted", graded["placeholder_hits"] == 8, str(graded))

    graded = check_sdd.grade_sdd(_sdd_doc(1), th)
    check("sdd: silver", graded["tier"] == "silver", str(graded))

    graded = check_sdd.grade_sdd(_sdd_doc(4), th)
    check("sdd: gold", graded["tier"] == "gold", str(graded))

    port_table = "| Port | Kind |\n| --- | --- |\n| dataIn | sync input |\n"
    graded = check_sdd.grade_sdd(_sdd_doc(4, port_table), th)
    check("sdd: platinum without model", graded["tier"] == "platinum", str(graded))
    check("sdd: table kinds detected", graded["table_kinds"] == ["ports"], str(graded))

    model = _fpp.load_module_model(WIDGET)
    graded = check_sdd.grade_sdd(_sdd_doc(4, port_table), th, model)
    check("sdd: gold when tables miss declared kinds", graded["tier"] == "gold", str(graded))

    all_tables = "\n".join(
        f"| {kw[0].capitalize()} | Description |\n| --- | --- |\n| x | y |\n"
        for kw in check_sdd.TABLE_KEYWORDS.values()
    )
    graded = check_sdd.grade_sdd(_sdd_doc(4, all_tables), th, model)
    check("sdd: platinum with model coverage", graded["tier"] == "platinum", str(graded))

    strict = check_sdd.sdd_thresholds({"sdd": {"gold_sections": 6}})
    check("sdd: config override", strict.gold_sections == 6 and strict.gold_words == 150)
    graded = check_sdd.grade_sdd(_sdd_doc(4), strict)
    check("sdd: override demotes gold to silver", graded["tier"] == "silver", str(graded))


def test_doxygen_mapping() -> None:
    check(
        "doxygen: page mangling",
        _doxygen.doxygen_class_page("Svc::CommandDispatcherImpl")
        == "class_svc_1_1_command_dispatcher_impl.html",
    )
    check(
        "doxygen: nested namespaces",
        _doxygen.doxygen_class_page("Svc::Ccsds::ApidManager")
        == "class_svc_1_1_ccsds_1_1_apid_manager.html",
    )
    check(
        "doxygen: underscore escaped",
        _doxygen.doxygen_class_page("Fw::My_Type") == "class_fw_1_1_my___type.html",
    )
    classes = _doxygen.find_component_classes(WIDGET)
    check("doxygen: widget class found", classes == ["Svc::Widget"], str(classes))


def test_run_checks_and_catalog() -> None:
    import catalog

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(FIXTURES, root)
        dest = Path(tmp) / "dest"
        modules_jsonl = Path(tmp) / "modules.jsonl"
        modules_jsonl.write_text(json.dumps({"path": "Svc/Widget", "has_ut": True}) + "\n")

        extra = Path(tmp) / "extra.jsonl"
        extra.write_text(
            json.dumps(
                {
                    "id": "U5",
                    "name": "Sufficient code coverage",
                    "category": "unit-testing",
                    "module": "Svc/Widget",
                    "status": "pass",
                    "failures": [],
                    "detail": "line coverage 91.00% (threshold 80.00%)",
                }
            )
            + "\n"
        )

        code, out = run(
            run_checks.main,
            [
                "--root", str(root),
                "--dest", str(dest),
                "--modules-jsonl", str(modules_jsonl),
                "--extra-results", str(extra),
                "--ref", "devel",
                "--commit", "abc1234def",
            ],
        )
        check("run_checks: exit 0 (publish mode)", code == 0, out)

        summary_path = dest / "Svc" / "Widget" / "checks" / "summary.json"
        check("run_checks: summary written", summary_path.is_file())
        summary = json.loads(summary_path.read_text())
        ids = {c["id"] for c in summary["checks"]}
        check("run_checks: static checks recorded", {"R1", "R7", "D4", "I1", "U3"} <= ids)
        check("run_checks: extra results merged", "U5" in ids)
        check(
            "run_checks: counts",
            summary["passed"] == 18 and summary["failed"] == 5 and summary["skipped"] == 0,
            json.dumps(
                {
                    "passed": summary["passed"],
                    "failed": summary["failed"],
                    "skipped": summary["skipped"],
                }
            ),
        )
        page = (dest / "Svc" / "Widget" / "checks" / "index.html").read_text()
        check("run_checks: page has fail badge", "status-fail" in page)

        bogus = Path(tmp) / "bogus.jsonl"
        bogus.write_text(json.dumps({"id": "X9", "module": "Svc/Nope", "status": "pass"}) + "\n")
        code, out = run(
            run_checks.main,
            [
                "--root", str(root),
                "--dest", str(dest),
                "--modules-jsonl", str(modules_jsonl),
                "--extra-results", str(bogus),
                "--ref", "devel",
                "--commit", "abc1234def",
            ],
        )
        ids2 = {c["id"] for c in json.loads(summary_path.read_text())["checks"]}
        check("run_checks: unknown-module result dropped", code == 0 and "X9" not in ids2, out)

        rendered = run_checks.render_module_page(
            "Svc/Widget",
            [{"id": "Z1", "name": "Z", "category": "requirements", "status": "bogus"}],
            ref="devel",
            commit="abc1234def",
            generated_at="2026-01-01T00:00:00Z",
        )
        check(
            "render_module_page: unknown status sanitized to skip",
            "status-skip" in rendered and "status-bogus" not in rendered,
        )

        code, _ = run(
            run_checks.main,
            [
                "--root", str(root),
                "--dest", str(dest),
                "--modules-jsonl", str(modules_jsonl),
                "--gate",
            ],
        )
        check("run_checks: --gate exits 1", code == 1)

        # SDD grades published: Widget has an SDD, Empty does not
        (root / "Svc" / "Empty").mkdir(parents=True)
        sdd_modules_jsonl = Path(tmp) / "sdd-modules.jsonl"
        sdd_modules_jsonl.write_text(
            json.dumps({"path": "Svc/Widget", "has_ut": True}) + "\n"
            + json.dumps({"path": "Svc/Empty", "has_ut": False}) + "\n"
        )
        code, _ = run(
            check_sdd.main,
            [
                "--root", str(root),
                "--dest", str(dest),
                "--modules-jsonl", str(sdd_modules_jsonl),
                "--ref", "devel",
                "--commit", "abc1234def",
            ],
        )
        check("check_sdd: exit 0", code == 0)
        sdd_summary = json.loads((dest / "Svc" / "Widget" / "sdd" / "summary.json").read_text())
        check("check_sdd: widget summary", sdd_summary["has_sdd"] and sdd_summary["tier"] in ("bronze", "silver"))
        check("check_sdd: sdd.md copied", (dest / "Svc" / "Widget" / "sdd" / "sdd.md").is_file())
        check(
            "check_sdd: rendered url",
            sdd_summary.get("rendered_url")
            == "https://fprime.jpl.nasa.gov/devel/Svc/Widget/docs/sdd/",
            str(sdd_summary.get("rendered_url")),
        )
        check(
            "check_sdd: doxygen links",
            sdd_summary.get("doxygen")
            == [{
                "name": "Svc::Widget",
                "url": "https://fprime.jpl.nasa.gov/devel/docs/reference/api/cpp/html/"
                       "class_svc_1_1_widget.html",
            }],
            str(sdd_summary.get("doxygen")),
        )
        sdd_page = (dest / "Svc" / "Widget" / "sdd" / "index.html").read_text()
        check("check_sdd: page rendered link", "rendered view" in sdd_page and "sdd.md" in sdd_page)
        check("check_sdd: page doxygen link", "class_svc_1_1_widget.html" in sdd_page)
        empty_summary = json.loads((dest / "Svc" / "Empty" / "sdd" / "summary.json").read_text())
        check(
            "check_sdd: missing SDD is bronze",
            empty_summary["tier"] == "bronze" and empty_summary["has_sdd"] is False,
        )
        check("check_sdd: no sdd.md when missing", not (dest / "Svc" / "Empty" / "sdd" / "sdd.md").exists())

        # Catalog picks up the checks summary
        code, _ = run(
            catalog.main,
            [
                "--dest", str(dest),
                "--modules-jsonl", str(modules_jsonl),
                "--ref", "devel",
                "--commit", "abc1234def",
            ],
        )
        check("catalog: exit 0", code == 0)
        index = (dest / "index.html").read_text()
        check("catalog: Checklist column", "<th class=\"cell\">Checklist</th>" in index)
        check("catalog: checks link", "Svc/Widget/checks/index.html" in index)
        cat = json.loads((dest / "catalog.json").read_text())
        entry = cat["modules"][0]
        check("catalog: checks entry", entry["checks"] is not None)
        check("catalog: checks tier", entry["tiers"]["checks"] == "bronze", entry["tiers"]["checks"])
        check("catalog: sdd entry", entry["sdd"] is not None and entry["sdd"]["has_sdd"])
        check("catalog: sdd tier", entry["tiers"]["sdd"] == sdd_summary["tier"], entry["tiers"]["sdd"])
        check("catalog: overall includes sdd", entry["tiers"]["overall"] == "bronze")
        module_page = (dest / "Svc" / "Widget" / "index.html").read_text()
        check("catalog: module page Checklist row", "<td>Checklist</td>" in module_page)
        check(
            "catalog: module page SDD row",
            "<td>Software Description Document</td>" in module_page
            and "sdd/index.html" in module_page,
        )
        check(
            "catalog: module page rendered SDD link",
            "https://fprime.jpl.nasa.gov/devel/Svc/Widget/docs/sdd/" in module_page,
        )
        check(
            "catalog: module page Doxygen row",
            "<td>API Documentation (Doxygen)</td>" in module_page
            and "class_svc_1_1_widget.html" in module_page,
        )


def main() -> int:
    tests = [
        test_fpp_parsing,
        test_requirements_parsing,
        test_requirement_checks,
        test_design_checks,
        test_implementation_checks,
        test_static_analysis_check,
        test_unit_testing_checks,
        test_build_level_checks_offline,
        test_checks_tier,
        test_sdd_grading,
        test_doxygen_mapping,
        test_run_checks_and_catalog,
    ]
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001
            print(f"FAIL {test.__name__} raised:")
            traceback.print_exc()
            FAILURES.append(test.__name__)
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
