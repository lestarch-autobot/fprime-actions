#!/usr/bin/env python3
"""D10 -- Every input port's behaviour is described in SDD prose.

Each typed input port name must appear in the SDD outside of tables (in a
paragraph, list item, or heading) so that the handler's behaviour is
documented, not merely listed.
"""

import re
import sys

from _report import finish, make_parser
from _sdd import load_sdd, typed_ports


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    args = parser.parse_args(argv)
    failures = []
    skipped_reason = None
    inputs = [p for p in typed_ports(args.module) if p.direction == "input"]
    sdd = load_sdd(args.module)
    if not inputs:
        skipped_reason = "no typed input ports in FPP model"
    elif sdd is None:
        failures.append("no docs/sdd.md found")
    else:
        prose = sdd.prose
        for port in inputs:
            if not re.search(rf"\b{re.escape(port.name)}\b", prose):
                failures.append(f"input port '{port.name}' is not described in SDD prose (only in a table, or absent)")
    return finish(
        check_id="D10",
        name="Input port behaviour documented",
        category="design",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(inputs)} input port(s) checked",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
