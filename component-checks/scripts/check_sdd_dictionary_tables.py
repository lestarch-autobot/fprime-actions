#!/usr/bin/env python3
"""D8 -- SDD command/telemetry/event/parameter/data-product tables match
the FPP model.

For every item kind the FPP model declares, the SDD must contain a markdown
table (identified by its header keywords) listing exactly the declared
names: missing and extra entries are reported.
"""

import sys

from _fpp import load_module_model
from _report import finish, make_parser
from _sdd import load_sdd

TABLE_KEYWORDS = {
    "command": ("command", "opcode", "mnemonic"),
    "telemetry channel": ("telemetry", "channel"),
    "event": ("event", "evr"),
    "parameter": ("parameter", "param"),
    "data product": ("product", "record", "container"),
}


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    args = parser.parse_args(argv)
    model = load_module_model(args.module)
    declared = {
        "command": model.commands,
        "telemetry channel": model.telemetry,
        "event": model.events,
        "parameter": model.parameters,
        "data product": model.data_products,
    }
    failures = []
    skipped_reason = None
    sdd = load_sdd(args.module)
    total = sum(len(v) for v in declared.values())
    if total == 0:
        skipped_reason = "FPP model declares no dictionary items"
    elif sdd is None:
        failures.append("no docs/sdd.md found")
    else:
        for kind, names in declared.items():
            if not names:
                continue
            tables = [t for t in sdd.tables if any(any(k in h.lower() for k in TABLE_KEYWORDS[kind]) for h in t.headers)]
            if not tables:
                failures.append(f"no {kind} table in SDD ({len(names)} declared in FPP)")
                continue
            documented = set()
            for table in tables:
                col = table.column("name", "mnemonic", *TABLE_KEYWORDS[kind])
                documented.update(table.cells(col if col is not None else 0))
            documented = {d.split("(")[0].strip() for d in documented}
            for name in names:
                if name not in documented:
                    failures.append(f"{kind} '{name}' is missing from the SDD {kind} table")
            for extra in sorted(documented - set(names)):
                if extra and not extra.startswith("-"):
                    failures.append(f"SDD {kind} table lists '{extra}', which the FPP model does not declare")
    return finish(
        check_id="D8",
        name="SDD dictionary tables match FPP",
        category="design",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{total} dictionary item(s) checked",
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
