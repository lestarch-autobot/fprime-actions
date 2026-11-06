#!/usr/bin/env python3
"""R9 -- Requirement coverage by kind (behaviour, input, output, memory,
hardware I/O, performance).

The requirements table carries a ``Kind`` (or ``Category``/``Type``) column
whose values name the aspect a requirement constrains.  Every kind must be
covered by at least one requirement, or explicitly waived in the SDD prose
(e.g. ``Memory requirements: N/A`` or ``No hardware I/O requirements``).
"""

import sys

from _report import finish, make_parser
from _requirements import default_requirement_files, load_requirements
from _sdd import load_sdd

KINDS = {
    "behavior": ("behavior", "behaviour", "functional"),
    "input": ("input",),
    "output": ("output",),
    "memory": ("memory", "storage"),
    "hardware I/O": ("hardware", "hw", "i/o", "io"),
    "performance": ("performance", "timing", "latency", "throughput"),
}


def kind_of(value: str):
    lowered = value.lower()
    return [kind for kind, words in KINDS.items() if any(w in lowered for w in words)]


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    args = parser.parse_args(argv)
    reqs = load_requirements(args.requirements or default_requirement_files(args.module))
    failures = []
    skipped_reason = None
    if not reqs:
        skipped_reason = "no requirements found"
    else:
        covered = set()
        untyped = []
        for req in reqs:
            kinds = [v for k, v in req.extras.items() if any(w in k.lower() for w in ("kind", "category", "type"))]
            if not kinds or not kinds[0].strip():
                untyped.append(req.req_id)
                continue
            covered.update(kind_of(kinds[0]))
        if untyped:
            failures.append(
                f"requirement(s) {', '.join(untyped)} have no Kind column entry "
                "(add a Kind column: Behavior/Input/Output/Memory/Hardware I/O/Performance)"
            )
        sdd = load_sdd(args.module)
        for kind, words in KINDS.items():
            if kind in covered:
                continue
            if sdd is not None and sdd.declares_not_applicable(*words):
                continue
            failures.append(
                f"no {kind} requirement; add one or waive it in the SDD prose "
                f"(e.g. '{kind.capitalize()} requirements: N/A')"
            )
    return finish(
        check_id="R9",
        name="Requirement coverage by kind",
        category="requirements",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(reqs)} requirement(s) checked",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
