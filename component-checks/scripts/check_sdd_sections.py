#!/usr/bin/env python3
"""D9 -- SDD carries the design artefacts the checklist asks for.

Checks, against ``docs/sdd.md`` and the FPP model:

    * a block diagram (image or mermaid graph/flowchart/class diagram),
    * sequence diagrams (mermaid ``sequenceDiagram`` or an image under a
      "sequence" heading) or an explicit ``Sequence diagrams: N/A``,
    * every FPP state machine named in the SDD, and a use-case / scenario
      / behavior section,
    * the component kind (active/passive/queued) stated and matching FPP,
    * an external-interfaces / ICD / references section, a cited external
      document (``datasheet 002-26764 Rev. *D``, ``ICD-1234``, ...) or an explicit
      ``External interfaces: None``.
"""

import re
import sys

from _fpp import load_module_model
from _report import finish, make_parser
from _sdd import component_kinds, load_sdd

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\s")
_BLOCK_DIAGRAM_RE = re.compile(r"```mermaid\s*\n\s*(graph|flowchart|classDiagram|C4)", re.IGNORECASE)
# ASCII-art block diagram: a plain fenced block containing box-drawing / port-marker glyphs
_ASCII_DIAGRAM_RE = re.compile(r"```[a-z]*\s*\n(?:[^`]*[\u2500-\u257f\u25a0-\u25ff]){3,}[^`]*```")
# An external document cited by identifier: "datasheet 002-26764 Rev. *D", "ICD 1234-A", "FDD-07".
_EXTERNAL_DOC_RE = re.compile(
    r"\b(datasheet|data sheet|ICD|FDD|interface control document|user manual|reference manual)\b"
    r"[ :#-]*(?:(?:no\.?|number|doc\.?)\s*)?([A-Z]{0,4}\d[\w/-]*(?:\.[\w/-]+)*(?:\s+Rev\.?\s*\*?\w+)?)",
    re.IGNORECASE,
)
_SEQUENCE_RE = re.compile(r"```mermaid\s*\n\s*sequenceDiagram|```(plantuml|puml)\s*\n[^`]*->", re.IGNORECASE)


def external_references_table(sdd):
    """Rows of the SDD's External References table (``Document | Identifier / Revision | Type | Governs``)
    as ``"<Type> <Identifier>: <Document>"`` strings; an empty list when the table only declares
    ``None``; None when the SDD has no such table."""
    for table in sdd.tables:
        doc_col = table.column("document", "reference", "source")
        id_col = table.column("identifier", "revision", "number", "id")
        type_col = table.column("type", "kind")
        if doc_col is None or id_col is None or doc_col == id_col:
            continue
        rows = []
        for row in table.rows:
            if max(doc_col, id_col) >= len(row):
                continue
            doc = row[doc_col].strip("` ")
            if not doc or doc.lower() in ("none", "n/a", "-", "\u2014"):
                continue
            kind = row[type_col].strip() if type_col is not None and type_col < len(row) else ""
            rows.append(f"{kind + ' ' if kind else ''}{row[id_col].strip()}: {doc}".strip())
        return rows
    return None


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    args = parser.parse_args(argv)
    failures = []
    details = []
    sdd = load_sdd(args.module)
    if sdd is None:
        failures.append("no docs/sdd.md found")
    else:
        text = sdd.text
        design_heading = sdd.has_heading("diagram", "design", "architecture")
        if not (
            _BLOCK_DIAGRAM_RE.search(text)
            or (design_heading and (_IMAGE_RE.search(text) or _ASCII_DIAGRAM_RE.search(text)))
        ):
            failures.append(
                "no block diagram (add an image, a mermaid graph/flowchart, or an ASCII-art block under a design heading)"
            )
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
        references = external_references_table(sdd)
        cited = sorted({f"{m.group(1)} {m.group(2)}" for m in _EXTERNAL_DOC_RE.finditer(text)})
        if references is not None:
            details.append("external references table: " + ("; ".join(references) if references else "None (declared)"))
        elif cited:
            details.append("no External References table; cited inline: " + "; ".join(cited))
        elif sdd.has_heading("external", "interface control", "icd", "reference") or sdd.declares_not_applicable(
            "external interface", "icd", "fdd"
        ):
            details.append("external interfaces section present (no table)")
        else:
            failures.append(
                "no External References table (Document | Identifier | Type | Governs; a 'None' row waives), "
                "no cited datasheet/ICD/FDD, and no external-interfaces section"
            )
    return finish(
        check_id="D9",
        name="SDD design sections present",
        category="design",
        module=args.module,
        failures=failures,
        detail="; ".join(details),
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
