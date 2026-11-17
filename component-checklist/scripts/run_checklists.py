#!/usr/bin/env python3
"""Fill the F Prime Component Development Checklist of every component.

For each module holding an FPP component, runs ``fprime-auto-checklist run``
and writes into ``<dest>/<module>/<checklist-subdirectory>/``:

* ``checklist.md``   -- the filled checklist (the record);
* ``checklist.json`` -- the tool's machine-readable evidence;
* ``index.html``     -- the checklist rendered for the published site;
* ``summary.json``   -- counts read by ``coverage-common/scripts/catalog.py``.

When the module carries a checklist in the repository
(``<module>/<source-checklist>``, default ``docs/checklist.md``) it is passed
as ``--previous``: rows decided by a reviewer (a person or an LLM) are kept,
rows decided by tools are recomputed.  The repository is never modified.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

SCHEMA_VERSION = 1
STATUSES = ("YES", "NO", "N/A", "REVIEW", "UNCHECKED")
REVIEWER_KINDS = ("review", "override")
_COMPONENT_RE = re.compile(r"^\s*(active|passive|queued)\s+component\s+\w+", re.MULTILINE)

CSS = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:2rem;color:#1f2328;}"
    "table{border-collapse:collapse;margin:0.75rem 0;}"
    "th,td{border:1px solid #d0d7de;padding:0.3rem 0.6rem;vertical-align:top;text-align:left;}"
    "th{background:#f6f8fa;}code{background:#f6f8fa;padding:0 0.2rem;border-radius:3px;}"
    ".meta{color:#59636e;font-size:0.9rem;margin-bottom:1rem;}"
)


def is_component(module_dir: Path) -> bool:
    """True when the module directory holds an FPP component definition."""
    for fpp in sorted(module_dir.glob("*.fpp")):
        try:
            if _COMPONENT_RE.search(fpp.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def _inline(text: str) -> str:
    """Escape ``text`` and render inline code, bold, italics and links."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])_([^_]+)_(?![\w*])", r"<em>\1</em>", out)

    def link(match: "re.Match[str]") -> str:
        href = match.group(2)
        if not re.match(r"^(https?://|#|\.{0,2}/|[\w.-]+/)", href):
            return match.group(0)
        return f'<a href="{html.escape(href)}">{match.group(1)}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, out)


def _cells(line: str) -> List[str]:
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def markdown_to_html(text: str) -> str:
    """Render the checklist Markdown subset: headings, tables, lists, paragraphs."""
    out: List[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if not stripped:
            i += 1
        elif heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
        elif stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in _cells(line)) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table>")
        elif re.match(r"^[-*]\s+", stripped):
            out.append("<ul>")
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                out.append(f"<li>{_inline(re.sub(r'^[-*]\s+', '', lines[i].strip()))}</li>")
                i += 1
            out.append("</ul>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|\||[-*]\s)", lines[i].strip()):
                para.append(_inline(lines[i].strip()))
                i += 1
            out.append("<p>" + "<br>".join(para) + "</p>")
    return "\n".join(out)


def render_page(module: str, markdown: str, *, ref: str, commit: str, source: Optional[str]) -> str:
    origin = (
        f"reviewer decisions carried from <code>{html.escape(source)}</code>"
        if source
        else "no checklist in the repository; reviewer rows are open"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>Checklist \u2014 {html.escape(module)}</title><style>{CSS}</style></head><body>"
        f'<div class="meta"><a href="../index.html">\u2190 {html.escape(module)}</a> &middot; '
        f"<code>{html.escape(ref)}</code> @ <code>{html.escape(commit[:12])}</code> &middot; {origin} &middot; "
        '<a href="checklist.md">Markdown</a> &middot; <a href="checklist.json">evidence (JSON)</a></div>'
        f"{markdown_to_html(markdown)}</body></html>\n"
    )


def summarize(doc: dict, *, module: str, ref: str, commit: str, source: Optional[str]) -> dict:
    counts = {status: int((doc.get("summary") or {}).get(status, 0) or 0) for status in STATUSES}
    analysis = doc.get("codeql") or {}
    findings = analysis.get("findings") or {}
    return {
        "schema": SCHEMA_VERSION,
        "artifact": "checklist",
        "module": module,
        "ref": ref,
        "commit": commit,
        "checklist_version": doc.get("checklist_version"),
        "tool": doc.get("tool"),
        "counts": counts,
        "total": sum(counts.values()),
        "reviewer_decisions": sum(1 for item in doc.get("items") or [] if item.get("kind") in REVIEWER_KINDS),
        "source": source,
        "static_analysis": {
            "tool": analysis.get("tool"),
            "ran": bool(analysis.get("ran")),
            "findings": sum(len(v) for v in findings.values()),
        },
    }


def run_module(args, module: str, tool_args: List[str]) -> Optional[dict]:
    module_dir = args.root / module
    out_dir = args.dest / module / args.checklist_subdirectory
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / "checklist.md"
    json_path = out_dir / "checklist.json"
    source_path = module_dir / args.source_checklist
    source = f"{module}/{args.source_checklist}" if source_path.is_file() else None

    command = [
        *args.tool,
        "run",
        "--project", str(args.root),
        "--module", module,
        "--output", str(markdown_path),
        "--json-output", str(json_path),
        "--work-dir", str(args.work_dir / module),
        *(["--previous", str(source_path)] if source else []),
        *tool_args,
    ]
    for stale in (markdown_path, json_path):
        stale.unlink(missing_ok=True)
    result = subprocess.run(command, cwd=args.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # The tool exits non-zero when an item is NO; only missing output is a failure.
    if not markdown_path.is_file() or not json_path.is_file():
        tail = result.stdout.decode("utf-8", "replace").strip().splitlines()[-5:]
        print(f"WARNING: checklist for {module} failed (exit {result.returncode}):", *tail, sep="\n  ", file=sys.stderr)
        # Still a component: publish an empty summary so it is graded as having no checklist.
        summary = summarize({}, module=module, ref=args.ref, commit=args.commit, source=None)
        summary["error"] = "fprime-auto-checklist produced no checklist"
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        (out_dir / "index.html").unlink(missing_ok=True)
        return None

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    summary = summarize(doc, module=module, ref=args.ref, commit=args.commit, source=source)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out_dir / "index.html").write_text(
        render_page(module, markdown_path.read_text(encoding="utf-8"), ref=args.ref, commit=args.commit, source=source),
        encoding="utf-8",
    )
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="Project checkout root")
    parser.add_argument("--dest", type=Path, required=True, help="Baseline worktree root")
    parser.add_argument("--modules-jsonl", type=Path, required=True, help="Module list from discover.py")
    parser.add_argument("--checklist-subdirectory", default="checklist")
    parser.add_argument("--source-checklist", default="docs/checklist.md",
                        help="Checklist kept in the repository, relative to the module directory")
    parser.add_argument("--work-dir", type=Path, required=True, help="Scratch directory for the tool's logs")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tool", nargs="+", default=[sys.executable, "-m", "fprime_auto_checklist"],
                        help="Command that runs fprime-auto-checklist")
    args, tool_args = parser.parse_known_args(argv)
    if tool_args[:1] == ["--"]:
        tool_args = tool_args[1:]
    args.root = args.root.resolve()
    args.dest = args.dest.resolve()
    args.work_dir = args.work_dir.resolve()

    written = failed = 0
    with args.modules_jsonl.open("r", encoding="utf-8") as fh:
        modules = [json.loads(line)["path"] for line in fh if line.strip()]
    for module in modules:
        if not is_component(args.root / module):
            continue
        if run_module(args, module, tool_args) is None:
            failed += 1
        else:
            written += 1
    print(f"run_checklists: wrote {written} checklist(s), {failed} failed", file=sys.stderr)
    return 0 if written or not failed else 1


if __name__ == "__main__":
    sys.exit(main())
