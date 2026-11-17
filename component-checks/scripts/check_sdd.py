#!/usr/bin/env python3
"""Grade each module's Software Description Document and publish ``sdd/``
artifacts onto the baseline branch.

The grade is purely structural (no human or LLM adjudication) and derived
from the module's ``docs/sdd.md``:

    * bronze:   no SDD, or a stock/unfilled template (headings only, or a
                high TBD/TODO/placeholder ratio)
    * silver:   a non-placeholder intro plus a couple of filled sections
    * gold:     many filled sections with substantive prose
    * platinum: gold, plus markdown tables describing the interface
                (ports/commands/telemetry/parameters/...) covering every
                item kind the module's FPP model declares

Thresholds are configurable through the ``sdd:`` section of the checklist
config file (``.github/module-checklist.yml``).

Output layout (matching the checks publisher)::

    <dest>/<module>/<sdd-subdir>/summary.json
    <dest>/<module>/<sdd-subdir>/index.html
    <dest>/<module>/<sdd-subdir>/sdd.md      (verbatim copy, when present)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from _config import docs_site, load_config
from _doxygen import doxygen_class_page, find_component_classes
from _fpp import FppModel, load_module_model

SCHEMA_VERSION = 1

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_DIVIDER_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_PLACEHOLDER_RE = re.compile(
    r"\b(TBD|TODO|TBW|TBR|FIXME|XXX|to be (?:determined|written|completed|done)|"
    r"fill (?:in|me)|placeholder|lorem ipsum)\b",
    re.IGNORECASE,
)

#: FPP item kind -> keywords that identify its interface table header.
TABLE_KEYWORDS = {
    "commands": ("command", "opcode", "mnemonic"),
    "telemetry": ("telemetry", "channel"),
    "events": ("event",),
    "parameters": ("parameter",),
    "ports": ("port",),
    "data_products": ("product", "record", "container"),
}


@dataclass(frozen=True)
class SddThresholds:
    """Structural cut-offs for the SDD tier grade."""

    min_section_words: int = 15  # prose words for a section to count as filled
    silver_sections: int = 2
    gold_sections: int = 4
    gold_words: int = 150  # total prose words required for gold


def sdd_thresholds(config: dict) -> SddThresholds:
    """Read ``sdd:`` overrides from the checklist config, if any."""
    section = config.get("sdd") or {}
    defaults = SddThresholds()

    def _int(key: str, fallback: int) -> int:
        try:
            return int(section.get(key, fallback))
        except (TypeError, ValueError):
            print(f"config: sdd.{key} is not a number; using {fallback}", file=sys.stderr)
            return fallback

    return SddThresholds(
        min_section_words=_int("min_section_words", defaults.min_section_words),
        silver_sections=_int("silver_sections", defaults.silver_sections),
        gold_sections=_int("gold_sections", defaults.gold_sections),
        gold_words=_int("gold_words", defaults.gold_words),
    )


@dataclass
class Section:
    title: str
    prose_words: int = 0
    placeholder_hits: int = 0

    def filled(self, thresholds: SddThresholds) -> bool:
        return self.prose_words >= thresholds.min_section_words and self.placeholder_hits == 0


def _split_sections(lines: List[str]) -> List[Section]:
    """Split the document into heading-delimited sections, counting prose.

    Table rows, table dividers, code fences, and headings do not count as
    prose; everything else contributes its word count.
    """
    sections: List[Section] = []
    current = Section(title="(preamble)")
    in_code = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING_RE.match(line)
        if match:
            sections.append(current)
            current = Section(title=match.group(1).strip())
            continue
        # Table rows and divider lines do not count as prose.
        if "|" in line or _DIVIDER_RE.match(line):
            continue
        current.placeholder_hits += len(_PLACEHOLDER_RE.findall(line))
        current.prose_words += len(stripped.split())
    sections.append(current)
    return sections


def _table_kinds(lines: List[str]) -> List[str]:
    """FPP item kinds covered by interface tables in the document."""
    kinds: set[str] = set()
    for i, line in enumerate(lines):
        if "|" not in line or i + 1 >= len(lines) or not _DIVIDER_RE.match(lines[i + 1]):
            continue
        header = line.lower()
        for kind, keywords in TABLE_KEYWORDS.items():
            if any(k in header for k in keywords):
                kinds.add(kind)
    return sorted(kinds)


def grade_sdd(
    text: Optional[str],
    thresholds: SddThresholds,
    model: Optional[FppModel] = None,
) -> dict:
    """Grade an SDD's text; ``text=None`` means no SDD exists (bronze).

    Returns a metrics dict including the ``tier``.
    """
    if text is None:
        return {"tier": "bronze", "has_sdd": False}

    lines = text.splitlines()
    headed = [s for s in _split_sections(lines) if s.title != "(preamble)"]
    body = headed[1:]  # sections after the document title heading
    intro = body[0] if body else None
    filled = [s for s in body if s.filled(thresholds)]
    total_words = sum(s.prose_words for s in body)
    placeholder_hits = sum(s.placeholder_hits for s in body)
    table_kinds = _table_kinds(lines)

    # Which FPP item kinds the module declares, keyed like TABLE_KEYWORDS.
    declared: list[str] = []
    if model is not None:
        declared_items = {
            "commands": model.commands,
            "telemetry": model.telemetry,
            "events": model.events,
            "parameters": model.parameters,
            "ports": model.input_ports + model.output_ports,
            "data_products": model.records or model.containers,
        }
        declared = [k for k in TABLE_KEYWORDS if declared_items[k]]

    tier = "bronze"
    if intro is not None and intro.filled(thresholds) and len(filled) >= thresholds.silver_sections:
        tier = "silver"
        if len(filled) >= thresholds.gold_sections and total_words >= thresholds.gold_words:
            tier = "gold"
            needed = declared if declared else None
            covered = (
                all(kind in table_kinds for kind in needed)
                if needed is not None
                else bool(table_kinds)
            )
            if covered:
                tier = "platinum"

    return {
        "tier": tier,
        "has_sdd": True,
        "sections": len(body),
        "filled_sections": len(filled),
        "prose_words": total_words,
        "placeholder_hits": placeholder_hits,
        "table_kinds": table_kinds,
        "declared_kinds": sorted(declared),
    }


CSS = """\
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 1.5rem; color: #1f2328; }
h1 { margin: 0 0 0.25rem 0; font-size: 1.2rem; }
.meta { color: #57606a; font-size: 0.9rem; margin-bottom: 1rem; }
.meta a, table a { color: #0969da; text-decoration: none; }
table { border-collapse: collapse; min-width: 24rem; }
th, td { padding: 0.4rem 0.75rem; text-align: left; border-top: 1px solid #eaeef2; }
th { font-weight: 500; color: #57606a; font-size: 0.85rem; border-bottom: 1px solid #eaeef2; }
.badge { display: inline-block; padding: 0.05rem 0.55rem; border-radius: 2em;
         font-size: 0.78rem; font-weight: 600; border: 1px solid transparent; }
.badge-platinum { background: #eef1f4; color: #24292f; border-color: #afb8c1; }
.badge-gold     { background: #fff3c4; color: #7d5a00; border-color: #d4af37; }
.badge-silver   { background: #f0f0f0; color: #57606a; border-color: #c0c0c0; }
.badge-bronze   { background: #f5e0d1; color: #8a4412; border-color: #cd7f32; }
.missing { color: #6e7781; font-style: italic; }
"""

METRIC_LABELS = [
    ("sections", "Sections"),
    ("filled_sections", "Filled sections"),
    ("prose_words", "Prose words"),
    ("placeholder_hits", "Placeholder markers"),
    ("table_kinds", "Interface tables"),
    ("declared_kinds", "FPP-declared kinds"),
]


def render_page(module_path: str, metrics: dict, *, ref: str, commit: str, generated_at: str) -> str:
    tier = metrics["tier"]
    rows = []
    if metrics.get("has_sdd"):
        rendered = metrics.get("rendered_url")
        doc_html = '<a href="sdd.md">sdd.md</a>'
        if rendered:
            doc_html = (
                f'<a href="{html.escape(rendered)}">rendered view</a> &middot; '
                '<a href="sdd.md">raw markdown</a>'
            )
        rows.append(f"<tr><td>Document</td><td>{doc_html}</td></tr>")
        for key, label in METRIC_LABELS:
            value = metrics.get(key)
            if isinstance(value, list):
                value = ", ".join(value) if value else "none"
            rows.append(f"<tr><td>{label}</td><td>{html.escape(str(value))}</td></tr>")
    else:
        rows.append('<tr><td>Document</td><td><span class="missing">no docs/sdd.md found</span></td></tr>')
    doxygen = metrics.get("doxygen") or []
    if doxygen:
        links = " &middot; ".join(
            f'<a href="{html.escape(d["url"])}"><code>{html.escape(d["name"])}</code></a>'
            for d in doxygen
        )
        rows.append(f"<tr><td>API documentation (Doxygen)</td><td>{links}</td></tr>")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>SDD \u2014 {html.escape(module_path)}</title>"
        f"<style>{CSS}</style></head><body>"
        f"<h1>Software Description Document \u2014 <code>{html.escape(module_path)}</code> "
        f'<span class="badge badge-{tier}">{tier.capitalize()}</span></h1>'
        f'<div class="meta"><a href="../index.html">\u2190 module</a> &middot; '
        f"<code>{html.escape(ref)}</code> @ <code>{html.escape(commit[:12])}</code> "
        f"&middot; generated {html.escape(generated_at)}</div>"
        "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</body></html>\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="Repository checkout root")
    parser.add_argument("--dest", type=Path, required=True, help="Output tree (baseline worktree)")
    parser.add_argument(
        "--modules-jsonl", type=Path, required=True,
        help="JSON-Lines module list from coverage-common/scripts/discover.py",
    )
    parser.add_argument(
        "--sdd-subdirectory", default="sdd",
        help='Subdirectory under each module for SDD artifacts (default: "sdd")',
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Checklist config file (e.g. .github/module-checklist.yml)",
    )
    parser.add_argument("--ref", default="", help="Source ref name")
    parser.add_argument("--commit", default="", help="Source commit SHA")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    config = load_config(args.config)
    thresholds = sdd_thresholds(config)
    site = docs_site(config)
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    modules: list[str] = []
    with args.modules_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                modules.append(json.loads(line)["path"])

    for module_path in modules:
        module_dir = root / module_path
        sdd_path = module_dir / "docs" / "sdd.md"
        text = None
        if sdd_path.is_file():
            text = sdd_path.read_text(encoding="utf-8", errors="replace")
        metrics = grade_sdd(text, thresholds, load_module_model(module_dir))
        if site and args.ref:
            if text is not None:
                metrics["rendered_url"] = site.sdd_url(args.ref, module_path)
            metrics["doxygen"] = [
                {"name": name, "url": site.doxygen_url(args.ref, doxygen_class_page(name))}
                for name in find_component_classes(module_dir)
            ]

        out_dir = args.dest / module_path / args.sdd_subdirectory
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "artifact": "sdd",
                    "generated_at": generated_at,
                    "ref": args.ref,
                    "commit": args.commit,
                    **metrics,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (out_dir / "index.html").write_text(
            render_page(module_path, metrics, ref=args.ref, commit=args.commit, generated_at=generated_at),
            encoding="utf-8",
        )
        if text is not None:
            (out_dir / "sdd.md").write_text(text, encoding="utf-8")
        elif (out_dir / "sdd.md").exists():
            (out_dir / "sdd.md").unlink()

    print(f"check_sdd: wrote SDD grades for {len(modules)} module(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
