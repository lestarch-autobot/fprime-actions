"""Requirements-source reader for checklist checks.

The canonical F´ requirements source is the requirement table in a module's
SDD (``docs/sdd.md``)::

    Requirement | Description | Verification Method
    ----------- | ----------- | -------------------
    CD-001      | The component shall ... | Unit Test

Optional columns are recognized when present:

    * ``Parent`` / ``Trace`` / ``Parent Requirement`` -- parent trace links
    * ``Design`` / ``Design Artifact`` -- design-artifact mapping

A CSV requirements file with a ``Requirement``/``ID`` header column is also
accepted for projects that keep requirements outside the SDD.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_DIVIDER_RE = re.compile(r"^\s*\|?\s*:?-{3,}")


@dataclass
class Requirement:
    req_id: str
    description: str = ""
    verification: str = ""
    parent: str = ""
    design: str = ""
    source: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Full searchable text of the requirement row."""
        return " ".join(
            [self.req_id, self.description, self.verification, self.parent, self.design]
            + list(self.extras.values())
        )


_HTML_TAG_RE = re.compile(r"<[^>]+>")
#: Header names that mark a requirement-ID column.  Exact (not substring)
#: matches, so a verification matrix with a "Requirements" column is not
#: mistaken for a requirements table.
_ID_HEADERS = {"id", "req id", "req. id", "req-id", "requirement id", "requirement", "req"}


def _split_row(line: str) -> List[str]:
    return [_HTML_TAG_RE.sub("", cell).strip() for cell in line.strip().strip("|").split("|")]


def _id_column(headers: List[str]) -> Optional[int]:
    for i, header in enumerate(headers):
        if header.strip().strip("*`").lower() in _ID_HEADERS:
            return i
    return None


def _column_index(headers: List[str], *keywords: str) -> Optional[int]:
    for i, header in enumerate(headers):
        lowered = header.lower()
        if any(k in lowered for k in keywords):
            return i
    return None


def _parse_markdown(path: Path) -> List[Requirement]:
    reqs: List[Requirement] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _DIVIDER_RE.match(lines[i + 1]):
            headers = _split_row(line)
            id_col = _id_column(headers)
            # Only treat tables whose first-ish column is a requirement ID.
            if id_col is not None and _column_index(headers, "requirement") is not None:
                desc_col = _column_index(headers, "description")
                ver_col = _column_index(headers, "verification")
                parent_col = _column_index(headers, "parent", "trace")
                design_col = _column_index(headers, "design")
                j = i + 2
                while j < len(lines) and "|" in lines[j]:
                    cells = _split_row(lines[j])
                    if len(cells) > id_col and cells[id_col]:
                        def cell(col):
                            return cells[col] if col is not None and col < len(cells) else ""
                        reqs.append(
                            Requirement(
                                req_id=cells[id_col].strip("` "),
                                description=cell(desc_col),
                                verification=cell(ver_col),
                                parent=cell(parent_col),
                                design=cell(design_col),
                                source=f"{path}:{j + 1}",
                                extras={
                                    headers[k]: cells[k]
                                    for k in range(min(len(headers), len(cells)))
                                    if k not in (id_col, desc_col, ver_col, parent_col, design_col)
                                },
                            )
                        )
                    j += 1
                i = j
                continue
        i += 1
    return reqs


def _parse_csv(path: Path) -> List[Requirement]:
    reqs: List[Requirement] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []

        def find(*keywords):
            for name in fieldnames:
                if any(k in name.lower() for k in keywords):
                    return name
            return None

        id_key = find("requirement", "req id", "id")
        desc_key = find("description")
        ver_key = find("verification")
        parent_key = find("parent", "trace")
        design_key = find("design")
        if id_key is None:
            return []
        for row_num, row in enumerate(reader, start=2):
            req_id = (row.get(id_key) or "").strip()
            if not req_id:
                continue
            reqs.append(
                Requirement(
                    req_id=req_id,
                    description=(row.get(desc_key) or "").strip() if desc_key else "",
                    verification=(row.get(ver_key) or "").strip() if ver_key else "",
                    parent=(row.get(parent_key) or "").strip() if parent_key else "",
                    design=(row.get(design_key) or "").strip() if design_key else "",
                    source=f"{path}:{row_num}",
                )
            )
    return reqs


def default_requirement_files(module_dir: Path) -> List[Path]:
    """Requirement sources for a module: docs/*.md plus docs/*.csv."""
    docs = module_dir / "docs"
    candidates: List[Path] = []
    if docs.is_dir():
        candidates.extend(sorted(docs.glob("*.md")))
        candidates.extend(sorted(docs.glob("*.csv")))
    return candidates


def load_requirements(paths: List[Path]) -> List[Requirement]:
    reqs: List[Requirement] = []
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix.lower() == ".csv":
            reqs.extend(_parse_csv(path))
        else:
            reqs.extend(_parse_markdown(path))
    return reqs


def requirements_referencing(reqs: List[Requirement], name: str) -> List[Requirement]:
    """Requirements whose text mentions ``name`` as a whole word (case-insensitive)."""
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return [r for r in reqs if pattern.search(r.text)]
