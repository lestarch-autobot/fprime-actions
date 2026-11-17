#!/usr/bin/env python3
"""U4 -- Memory leak check passes.

Runs the component's unit-test executable under a leak checker and verifies it
passes with no leaks or memory errors.  F Prime native unit-test builds are
AddressSanitizer-instrumented (incompatible with valgrind), so those run with
LeakSanitizer enabled (``ASAN_OPTIONS=detect_leaks=1``); uninstrumented
executables run under valgrind (``--leak-check=full --error-exitcode=1``).
The executable is located in the project's unit-test build cache
(``build-fprime-*-ut/bin/<platform>/<module>_ut_exe``); when missing it is
built with ``fprime-util build --ut`` first.  Exits non-zero with the failing
output when leaks, memory errors or test failures are detected.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from _report import finish, make_parser

_LEAK_RE = re.compile(
    r"(definitely lost:\s*[1-9][\d,]*|indirectly lost:\s*[1-9][\d,]*|ERROR SUMMARY:\s*[1-9][\d,]*\s+errors)",
    re.IGNORECASE,
)
_VALGRIND = ["valgrind", "--leak-check=full", "--error-exitcode=1", "--errors-for-leak-kinds=definite,indirect"]
_SANITIZER_RE = re.compile(r"(ERROR: (?:Leak|Address)Sanitizer: [^\n]*|SUMMARY: (?:Leak|Address)Sanitizer: [^\n]*)")
_LSAN_EXIT = "23"


def is_asan_instrumented(exe: Path) -> bool:
    try:
        return b"__asan_init" in exe.read_bytes()
    except OSError:
        return False


def project_root(module: Path) -> Optional[Path]:
    """Nearest ancestor holding ``settings.ini`` or a ``build-fprime-*`` cache."""
    for parent in [module.resolve()] + list(module.resolve().parents):
        if (parent / "settings.ini").is_file() or any(parent.glob("build-fprime-*")):
            return parent
    return None


def ut_executables(module: Path) -> List[Path]:
    root = project_root(module)
    if root is None:
        return []
    target = module.resolve().relative_to(root).as_posix().replace("/", "_")
    return sorted(root.glob(f"build-fprime-*-ut/bin/*/{target}_ut_exe"))


def leaks_in(output: str) -> List[str]:
    return sorted(set(_LEAK_RE.findall(output)) | set(_SANITIZER_RE.findall(output)))


def run_leak_check(exe: Path) -> "subprocess.CompletedProcess[str]":
    if is_asan_instrumented(exe):
        env = dict(os.environ)
        env["ASAN_OPTIONS"] = "detect_leaks=1:exitcode=" + _LSAN_EXIT + ":" + env.get("ASAN_OPTIONS", "")
        env["LSAN_OPTIONS"] = "exitcode=" + _LSAN_EXIT + ":" + env.get("LSAN_OPTIONS", "")
        return subprocess.run([str(exe)], cwd=exe.parent, capture_output=True, text=True, env=env)
    return subprocess.run(_VALGRIND + [str(exe)], cwd=exe.parent, capture_output=True, text=True)


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument("--jobs", type=int, default=None, help="Parallel jobs for the unit-test build")
    args = parser.parse_args(argv)

    skipped_reason = None
    failures = []
    detail = ""
    exes = ut_executables(args.module)
    if shutil.which("valgrind") is None and not all(is_asan_instrumented(e) for e in exes):
        skipped_reason = "valgrind not found on PATH (needed for uninstrumented unit-test executables)"
    else:
        if not exes:
            if shutil.which("fprime-util") is None:
                skipped_reason = "no unit-test executable found and fprime-util not on PATH to build one"
            else:
                cmd = ["fprime-util", "build", "--ut"] + (["-j", str(args.jobs)] if args.jobs else [])
                proc = subprocess.run(cmd, cwd=args.module, capture_output=True, text=True)
                if proc.returncode != 0:
                    failures.append(f"'fprime-util build --ut' failed:\n{((proc.stdout or '') + (proc.stderr or ''))[-4000:]}")
                exes = ut_executables(args.module)
                if not exes and not failures:
                    failures.append("unit-test executable not found after 'fprime-util build --ut'")
        for exe in exes:
            proc = run_leak_check(exe)
            output = (proc.stdout or "") + (proc.stderr or "")
            leaks = leaks_in(output)
            if proc.returncode != 0 and not leaks:
                failures.append(f"{exe.name} failed under the leak checker (exit {proc.returncode}):\n{output[-4000:]}")
            failures.extend(f"{exe.name}: {leak}" for leak in leaks)
        if exes and not failures:
            tool = "LeakSanitizer" if all(is_asan_instrumented(e) for e in exes) else "valgrind"
            detail = f"{len(exes)} unit-test executable(s) clean under {tool}"
    return finish(
        check_id="U4",
        name="Memory leak check passes",
        category="unit-testing",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=detail,
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
