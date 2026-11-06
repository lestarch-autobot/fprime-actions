#!/usr/bin/env python3
"""D6 -- FPP compiles and generates auto-code.

Runs the FPP front-end check (``fpp-check``) over the module's FPP files
(plus any ``--include`` dependency files/directories) and verifies it
completes with no errors.  When ``--build`` is given, additionally runs
``fprime-util build`` in the module directory so the full auto-code
generation path is exercised.  Exits non-zero on any failure.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from _fpp import load_module_model
from _report import finish, make_parser


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument(
        "--include",
        type=Path,
        action="append",
        default=[],
        help="Additional FPP file or directory of FPP files the module depends on",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Also run 'fprime-util build' in the module directory",
    )
    args = parser.parse_args(argv)

    model = load_module_model(args.module)

    skipped_reason = None
    if not model.fpp_files:
        skipped_reason = "no FPP files found"
    elif shutil.which("fpp-check") is None:
        skipped_reason = "fpp-check not found on PATH (install fprime-fpp)"

    failures = []
    if skipped_reason is None:
        extra = []
        for inc in args.include:
            if inc.is_dir():
                extra.extend(sorted(inc.rglob("*.fpp")) + sorted(inc.rglob("*.fppi")))
            elif inc.is_file():
                extra.append(inc)
        # .fppi files are pulled in by the .fpp that includes them; passing them
        # as top-level inputs makes the front-end reject their bare members.
        top_level = [p for p in model.fpp_files if p.suffix == ".fpp"]
        cmd = ["fpp-check"] + [str(p) for p in extra + top_level]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).strip()
            failures.append(f"fpp-check failed:\n{output}")

        if not failures and args.build:
            if shutil.which("fprime-util") is None:
                failures.append("--build requested but fprime-util not found on PATH")
            else:
                proc = subprocess.run(
                    ["fprime-util", "build"],
                    cwd=args.module,
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    failures.append(
                        "fprime-util build failed:\n" + (proc.stderr or proc.stdout)[-4000:]
                    )

    return finish(
        check_id="D6",
        name="FPP compiles and generates auto-code",
        category="design",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(model.fpp_files)} FPP file(s) checked",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
