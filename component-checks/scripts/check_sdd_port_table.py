#!/usr/bin/env python3
"""D7 -- SDD port table matches the FPP model.

The SDD must contain a markdown table with a ``Port``/``Name`` column listing
every typed port of the component.  Where the table carries ``Kind`` /
``Direction`` and ``Type`` columns they must agree with the FPP declaration
(async/sync/guarded input or output; port type).
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
    ports = typed_ports(args.module)
    sdd = load_sdd(args.module)
    if not ports:
        skipped_reason = "no typed ports in FPP model"
    elif sdd is None:
        failures.append("no docs/sdd.md found")
    else:
        tables = [t for t in sdd.tables_with("port", "name") if t.column("kind", "direction", "type") is not None]
        if not tables:
            failures.append("no port table in SDD (table with Port and Kind/Type columns)")
        else:
            documented = {}
            for table in tables:
                name_col = table.column("port", "name")
                kind_col = table.column("kind", "direction")
                type_col = table.column("type")
                for row in table.rows:
                    if name_col is None or name_col >= len(row):
                        continue
                    name = row[name_col].strip("` ")
                    documented[name] = (
                        row[kind_col].lower() if kind_col is not None and kind_col < len(row) else "",
                        row[type_col].strip("` ") if type_col is not None and type_col < len(row) else "",
                    )
            fpp_names = {p.name: p for p in ports}
            for name, port in sorted(fpp_names.items()):
                if name not in documented:
                    failures.append(f"port '{name}' ({port.kind} {port.direction} {port.type}) is missing from the SDD port table")
                    continue
                kind, ptype = documented[name]
                if kind and port.direction not in kind:
                    failures.append(f"port '{name}' is documented as '{kind}' but FPP declares {port.kind} {port.direction}")
                elif kind and port.direction == "input" and port.kind not in kind and any(k in kind for k in ("async", "sync", "guarded")):
                    failures.append(f"port '{name}' is documented as '{kind}' but FPP declares {port.kind} input")
                if ptype and re.split(r"::|\.", ptype)[-1] != port.type.split(".")[-1]:
                    failures.append(f"port '{name}' is documented with type '{ptype}' but FPP declares {port.type}")
            for name in sorted(set(documented) - set(fpp_names)):
                failures.append(f"SDD port table lists '{name}', which is not a typed port in the FPP model")
    return finish(
        check_id="D7",
        name="SDD port table matches FPP",
        category="design",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(ports)} port(s) checked",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
