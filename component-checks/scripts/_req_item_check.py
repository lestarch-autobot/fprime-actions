"""Shared driver for the "requirement exists per <FPP item>" checks (R1-R6).

Each concrete check names an item kind; the driver extracts those items
from the module's FPP model, loads the requirements source, and fails for
every item no requirement references by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from _fpp import FppModel, load_module_model
from _report import finish, make_parser
from _requirements import (
    Requirement,
    default_requirement_files,
    load_requirements,
    requirements_referencing,
)


def run_item_check(
    *,
    check_id: str,
    name: str,
    kind: str,
    extractor: Callable[[FppModel], List[str]],
    argv=None,
    coverage: Optional[Callable[[Path, List[str], List[Requirement]], Dict[str, str]]] = None,
) -> int:
    """``coverage(module, items, reqs)`` may return alternative evidence per item
    (item -> reason) that counts as covered when no requirement names it."""
    parser = make_parser(f"Verify a requirement exists for every {kind} in the FPP model")
    args = parser.parse_args(argv)

    model = load_module_model(args.module)
    items = extractor(model)

    req_files = args.requirements or default_requirement_files(args.module)
    reqs = load_requirements(req_files)

    skipped_reason = None
    if not model.fpp_files:
        skipped_reason = "no FPP files found"
    elif not items:
        skipped_reason = f"no {kind}s declared in the FPP model"

    failures: List[str] = []
    if skipped_reason is None:
        if not reqs:
            failures.append(
                f"no requirements found (searched: {', '.join(str(p) for p in req_files) or 'nothing'}); "
                f"cannot trace {len(items)} {kind}(s)"
            )
        else:
            covered = {k: v for k, v in (coverage(args.module, items, reqs) if coverage else {}).items() if k in items}
            for item in items:
                if not requirements_referencing(reqs, item) and item not in covered:
                    failures.append(
                        f"{kind} '{item}' has no requirement referencing it "
                        f"(add a requirement mentioning '{item}')"
                    )

    return finish(
        check_id=check_id,
        name=name,
        category="requirements",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(items)} {kind}(s) traced against {len(reqs)} requirement(s)"
        + (f"; {len(covered)} covered by companion/SDD evidence" if skipped_reason is None and reqs and covered else ""),
        json_output=args.json_output,
    )
