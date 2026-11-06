"""Shared Software Design Document (``docs/sdd.md``) readers for the
design-consistency checks: locating the SDD, splitting it into markdown
tables and prose, and reading typed-port declarations from FPP."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from _fpp import load_module_model, strip_comments

_DIVIDER_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_TYPED_PORT_RE = re.compile(
    r"\b(?:(async|sync|guarded)\s+input|(output))\s+port\s+(\w+)\s*:\s*(?:\[[^\]]*\]\s*)?([\w.]+)"
)
_COMPONENT_KIND_RE = re.compile(r"\b(active|passive|queued)\s+component\s+(\w+)")


@dataclass
class Table:
    headers: List[str]
    rows: List[List[str]]
    line: int

    def column(self, *keywords: str) -> Optional[int]:
        for i, header in enumerate(self.headers):
            lowered = header.lower()
            if any(k in lowered for k in keywords):
                return i
        return None

    def cells(self, col: Optional[int]) -> List[str]:
        if col is None:
            return []
        return [row[col].strip("` ") for row in self.rows if col < len(row) and row[col].strip()]


@dataclass
class Sdd:
    path: Path
    text: str
    tables: List[Table] = field(default_factory=list)
    prose_lines: List[str] = field(default_factory=list)
    headings: List[str] = field(default_factory=list)

    @property
    def prose(self) -> str:
        return "\n".join(self.prose_lines)

    def tables_with(self, *keywords: str) -> List[Table]:
        return [t for t in self.tables if t.column(*keywords) is not None]

    def has_heading(self, *keywords: str) -> bool:
        return any(any(k in h.lower() for k in keywords) for h in self.headings)

    def declares_not_applicable(self, *keywords: str) -> bool:
        """A prose line such as ``Sequence diagrams: N/A`` or
        ``No performance requirements`` opts a section out explicitly."""
        for line in self.prose_lines:
            lowered = line.lower()
            if not any(k in lowered for k in keywords):
                continue
            if re.search(r"\b(n/a|not applicable|none)\b", lowered) or re.match(
                r"^\s*(?:[-*]\s*)?(?:there (?:are|is) )?no\b", lowered
            ):
                return True
        return False


def _split_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def find_sdd(module_dir: Path) -> Optional[Path]:
    docs = module_dir / "docs"
    for name in ("sdd.md", "SDD.md", "Sdd.md"):
        if (docs / name).is_file():
            return docs / name
    candidates = sorted(docs.glob("*.md")) if docs.is_dir() else []
    return candidates[0] if candidates else None


def load_sdd(module_dir: Path) -> Optional[Sdd]:
    path = find_sdd(module_dir)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    sdd = Sdd(path=path, text=text)
    lines = text.splitlines()
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            sdd.prose_lines.append(line)
            i += 1
            continue
        if not in_fence and "|" in line and i + 1 < len(lines) and _DIVIDER_RE.match(lines[i + 1]):
            table = Table(headers=_split_row(line), rows=[], line=i + 1)
            j = i + 2
            while j < len(lines) and "|" in lines[j]:
                table.rows.append(_split_row(lines[j]))
                j += 1
            sdd.tables.append(table)
            i = j
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            sdd.headings.append(heading.group(1).strip())
        sdd.prose_lines.append(line)
        i += 1
    return sdd


@dataclass
class TypedPort:
    name: str
    direction: str  # input | output
    kind: str  # async | sync | guarded | output
    type: str


def typed_ports(module_dir: Path) -> List[TypedPort]:
    ports: List[TypedPort] = []
    for path in load_module_model(module_dir).fpp_files:
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for in_kind, out_kind, name, ptype in _TYPED_PORT_RE.findall(text):
            if out_kind:
                ports.append(TypedPort(name, "output", "output", ptype))
            else:
                ports.append(TypedPort(name, "input", in_kind, ptype))
    seen: Dict[str, TypedPort] = {}
    for p in ports:
        seen.setdefault(p.name, p)
    return list(seen.values())


def component_kinds(module_dir: Path) -> Dict[str, str]:
    """Component name -> active|passive|queued, from the module's FPP."""
    kinds: Dict[str, str] = {}
    for path in load_module_model(module_dir).fpp_files:
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for kind, name in _COMPONENT_KIND_RE.findall(text):
            kinds[name] = kind
    return kinds
