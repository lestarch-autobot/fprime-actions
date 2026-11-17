#!/usr/bin/env python3
"""R6 -- Requirement per port/interface (known/planned use-case).

Parses the module's FPP model for all typed ports and verifies each one has a
known use-case: a requirement referencing it by name, a non-empty description
in the SDD port table, or -- for companion ports named ``<base>Complete``,
``<base>Done``, ``<base>Callback``, ``<base>Response`` -- the use-case of
``<base>``.  Exits non-zero listing any port lacking all three.
"""

import re
import sys
from pathlib import Path
from typing import Dict, List

from _req_item_check import run_item_check
from _requirements import Requirement, requirements_referencing
from _sdd import load_sdd, split_names

_COMPANION_RE = re.compile(r"^(?P<base>.+?)(Complete|Completed|Done|Callback|Response|Return)$")
_DESCRIPTION_HEADERS = ("description", "purpose", "use", "role", "summary", "meaning")


def port_coverage(module: Path, ports: List[str], reqs: List[Requirement]) -> Dict[str, str]:
    covered: Dict[str, str] = {}
    sdd = load_sdd(module)
    if sdd is not None:
        for table in sdd.tables_with("port", "name"):
            name_col = table.column("port", "name")
            desc_col = table.column(*_DESCRIPTION_HEADERS)
            if desc_col is None or desc_col == name_col:
                continue
            for row in table.rows:
                if max(name_col, desc_col) < len(row) and row[desc_col].strip():
                    for name in split_names(row[name_col]):
                        covered.setdefault(name, f"described in the SDD port table (line {table.line})")
    names = set(ports)
    for port in ports:
        match = _COMPANION_RE.match(port)
        if match is None or match.group("base") not in names:
            continue
        base = match.group("base")
        if base in covered or requirements_referencing(reqs, base):
            covered.setdefault(port, f"companion of '{base}'")
    return covered


def main(argv=None) -> int:
    return run_item_check(
        check_id="R6",
        name="Requirement per port/interface",
        kind="port",
        extractor=lambda m: [p.name for p in m.ports],
        argv=argv,
        coverage=port_coverage,
    )


if __name__ == "__main__":
    sys.exit(main())
