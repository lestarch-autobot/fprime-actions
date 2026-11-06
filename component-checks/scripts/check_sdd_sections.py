#!/usr/bin/env python3
"""D9 -- SDD carries the design artefacts the checklist asks for.

Checks, against ``docs/sdd.md`` and the FPP model:

    * a block diagram (image or mermaid graph/flowchart/class diagram),
    * sequence diagrams (mermaid ``sequenceDiagram`` or an image under a
      "sequence" heading) or an explicit ``Sequence diagrams: N/A``,
    * every FPP state machine named in the SDD, and a use-case / scenario
      / behavior section,
    * the component kind (active/passive/queued) stated and matching FPP,
    * an external-interfaces / ICD / references section or an explicit
      ``External interfaces: None``.
"""

import re
import sys

from _fpp import load_module_model
from _report import finish, make_parser
from _sdd import component_kinds, load_sdd

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\s")
_BLOCK_DIAGRAM_RE = re.compile(r"```mermaid\s*\n\s*(graph|flowchart|classDiagram|C4)", re.IGNORECASE)
_SEQUENCE_RE = re.compile(r"```mermaid\s*\n\s*sequenceDiagram|```(plantuml|puml)\s*\n[^`]*->", re.IGNORECASE)


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    args = parser.parse_args(argv)
    failures = []
    sdd = load_sdd(args.module)
    if sdd is None:
        failures.append("no docs/sdd.md found")
    else:
        text = sdd.text
        if not (_BLOCK_DIAGRAM_RE.search(text) or (_IMAGE_RE.search(text) and sdd.has_heading("diagram", "design", "architecture"))):
            failures.append("no block diagram (add an image or a mermaid graph/flowchart under a design heading)")
        if not (_SEQUENCE_RE.search(text) or (sdd.has_heading("sequence") and _IMAGE_RE.search(text)) or sdd.declares_not_applicable("sequence")):
            failures.append("no sequence diagram (add a mermaid sequenceDiagram, or state 'Sequence diagrams: N/A' with rationale)")
        model = load_module_model(args.module)
        for sm in model.state_machines:
            if not re.search(rf"\b{re.escape(sm.name)}\b", text):
                failures.append(f"state machine '{sm.name}' is not described in the SDD")
        if not sdd.has_heading("use case", "scenario", "behavior", "behaviour", "operation", "functional description", "design"):
            failures.append("no use-case / behavior section heading in the SDD")
        prose = sdd.prose.lower()
        for name, kind in component_kinds(args.module).items():
            if not re.search(rf"\b{kind}\b", prose):
                failures.append(f"SDD does not state that {name} is a{'n' if kind[0] in 'aeiou' else ''} {kind} component")
            for other in ("active", "passive", "queued"):
                if other != kind and re.search(rf"\b{other}\s+component\b", prose):
                    failures.append(f"SDD calls the component '{other}' but FPP declares {name} {kind}")
        if not (sdd.has_heading("external", "interface control", "icd", "reference") or sdd.declares_not_applicable("external interface", "icd", "fdd")):
            failures.append("no external interfaces / references section (add one, or state 'External interfaces: None')")
    return finish(
        check_id="D9",
        name="SDD design sections present",
        category="design",
        module=args.module,
        failures=failures,
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
