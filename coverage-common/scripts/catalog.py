"""Generate ``catalog.json`` and the top-level checklist ``index.html``.

Inputs:
    * A list of discovered modules (path, has_ut) from ``discover.py``.
    * The baseline-branch worktree (``--dest``), which holds whatever
      artifact subtrees have been published so far:
          <mod>/<coverage-subdir>/summary.json       UT coverage
          <mod>/<int-coverage-subdir>/summary.json   integration coverage
          <mod>/<codeql-subdir>/summary.json         CodeQL findings

Outputs (written to ``--dest``):
    * ``catalog.json`` -- machine-readable; see ``schema`` field for version.
    * ``index.html``   -- self-contained checklist landing page: one row per
      module with a link + tier badge (platinum/gold/silver/bronze) per
      artifact type.
    * ``<mod>/index.html`` -- per-module roll-up page linking each published
      artifact subtree (coverage, int-coverage, codeql) with its badge.
      Skipped when the coverage subdirectory is flattened (""), where the
      module root already is the coverage report.

Because all inputs are read from ``--dest`` (the baseline branch itself),
regeneration is idempotent and order-independent: any publisher (coverage,
int-coverage, codeql) can rerun this script after mirroring its own subtree
and the page reflects the union of everything on the branch.

The HTML page is intentionally a single file with no external assets so the
``coverage/<ref>`` orphan branch is self-hosting.  ``<details>``/``<summary>``
elements provide collapse/expand without JavaScript.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import quote

from _config import coverage_thresholds, load_config
from _summary import Summary, Totals, load_summary
import versions as versions_mod
from _tiers import SEVERITY_ORDER, TIERS, CoverageThresholds, checks_tier, codeql_tier, coverage_tier

SCHEMA_VERSION = 6

CSS = """\
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 1.5rem; color: #1f2328; background: #ffffff;
}
h1 { margin: 0 0 0.25rem 0; font-size: 1.4rem; }
.meta { color: #57606a; font-size: 0.9rem; margin-bottom: 1rem; }
.overall {
  display: flex; gap: 1rem; align-items: baseline; flex-wrap: wrap;
  padding: 0.75rem 1rem; background: #f6f8fa; border: 1px solid #d0d7de;
  border-radius: 6px; margin-bottom: 1rem;
}
.overall a { color: #0969da; text-decoration: none; }
.overall a:hover { text-decoration: underline; }
details { border: 1px solid #d0d7de; border-radius: 6px; margin-bottom: 0.5rem; background: #ffffff; }
details > summary {
  cursor: pointer; padding: 0.5rem 0.75rem; font-weight: 600;
  display: grid; grid-template-columns: 1fr 11rem 11rem 11rem 11rem; gap: 0.5rem;
  align-items: baseline; list-style: none;
}
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "\\25B6"; display: inline-block; margin-right: 0.4rem; transition: transform 0.1s; }
details[open] > summary::before { transform: rotate(90deg); }
table { width: 100%; border-collapse: collapse; }
table th, table td { padding: 0.35rem 0.75rem; text-align: left; }
table th { font-weight: 500; color: #57606a; font-size: 0.85rem; border-bottom: 1px solid #eaeef2; }
table td.cell, table th.cell { width: 11rem; white-space: nowrap; }
table tr.row { border-top: 1px solid #eaeef2; }
table tr.row:hover { background: #f6f8fa; }
table a { color: #0969da; text-decoration: none; }
table a:hover { text-decoration: underline; }
.badge {
  display: inline-block; padding: 0.05rem 0.55rem; border-radius: 2em;
  font-size: 0.78rem; font-weight: 600; border: 1px solid transparent;
  vertical-align: baseline;
}
.badge-platinum { background: #eef1f4; color: #24292f; border-color: #afb8c1; }
.badge-gold     { background: #fff3c4; color: #7d5a00; border-color: #d4af37; }
.badge-silver   { background: #f0f0f0; color: #57606a; border-color: #c0c0c0; }
.badge-bronze   { background: #f5e0d1; color: #8a4412; border-color: #cd7f32; }
.no-ut, .no-cov { color: #6e7781; font-style: italic; font-size: 0.85rem; }
.section-title { margin: 1.25rem 0 0.5rem 0; font-size: 1.05rem; }
.welcome { color: #1f2328; font-size: 0.95rem; margin-bottom: 1rem; max-width: 60rem; }
"""

MODULE_CSS = """\
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 1.5rem; color: #1f2328; background: #ffffff;
}
h1 { margin: 0 0 0.25rem 0; font-size: 1.2rem; }
.meta { color: #57606a; font-size: 0.9rem; margin-bottom: 1rem; }
.meta a, table a { color: #0969da; text-decoration: none; }
.meta a:hover, table a:hover { text-decoration: underline; }
table { border-collapse: collapse; min-width: 32rem; }
table th, table td { padding: 0.45rem 0.75rem; text-align: left; }
table th { font-weight: 500; color: #57606a; font-size: 0.85rem; border-bottom: 1px solid #eaeef2; }
table tr { border-top: 1px solid #eaeef2; }
table tr:hover { background: #f6f8fa; }
.badge {
  display: inline-block; padding: 0.05rem 0.55rem; border-radius: 2em;
  font-size: 0.78rem; font-weight: 600; border: 1px solid transparent;
  vertical-align: baseline;
}
.badge-platinum { background: #eef1f4; color: #24292f; border-color: #afb8c1; }
.badge-gold     { background: #fff3c4; color: #7d5a00; border-color: #d4af37; }
.badge-silver   { background: #f0f0f0; color: #57606a; border-color: #c0c0c0; }
.badge-bronze   { background: #f5e0d1; color: #8a4412; border-color: #cd7f32; }
.no-cov { color: #6e7781; font-style: italic; font-size: 0.85rem; }
.welcome { color: #1f2328; font-size: 0.95rem; margin-bottom: 1rem; max-width: 60rem; }
"""

WELCOME_HTML = (
    "These pages grade every F\u00b4 module against the project's quality "
    "criteria. Each module is scored per artifact \u2014 unit test coverage, "
    "integration test coverage, static analysis findings, and the component "
    "development checklist \u2014 on a tier scale of "
    '<span class="badge badge-platinum">Platinum</span> (best), '
    '<span class="badge badge-gold">Gold</span>, '
    '<span class="badge badge-silver">Silver</span>, and '
    '<span class="badge badge-bronze">Bronze</span>. '
    "Click a module for its detail page, or a cell for the underlying "
    "report. Pages are regenerated automatically from the latest published "
    "CI results for the ref shown above."
)

TIER_LABELS = {
    "platinum": "Platinum",
    "gold": "Gold",
    "silver": "Silver",
    "bronze": "Bronze",
}


def badge_html(tier: str) -> str:
    return f'<span class="badge badge-{tier}">{TIER_LABELS[tier]}</span>'


def load_codeql_summary(path: Path) -> Optional[dict]:
    """Load a codeql summary.json, returning None on failure."""
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or "findings" not in doc:
        return None
    return doc


def load_sdd_summary(path: Path) -> Optional[dict]:
    """Load an SDD summary.json, returning None on failure."""
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("artifact") != "sdd" or doc.get("tier") not in TIERS:
        return None
    return doc


def load_checks_summary(path: Path) -> Optional[dict]:
    """Load a checklist-checks summary.json, returning None on failure."""
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or "checks" not in doc:
        return None
    return doc


def aggregate_codeql(summaries: Iterable[Optional[dict]]) -> Optional[dict]:
    """Combine several codeql-check summaries into one roll-up entry."""
    present = [s for s in summaries if s is not None]
    if not present:
        return None
    by_severity = {
        sev: sum(int((s.get("by_severity") or {}).get(sev, 0) or 0) for s in present)
        for sev in SEVERITY_ORDER
    }
    worst = next((sev for sev in SEVERITY_ORDER if by_severity[sev] > 0), None)
    return {
        "findings": sum(int(s.get("findings", 0) or 0) for s in present),
        "by_severity": by_severity,
        "worst": worst,
        "tier": codeql_tier(worst),
        "dismissed": sum(int(s.get("dismissed", 0) or 0) for s in present),
    }


def _check_label(subdir: str, *summaries: Optional[dict]) -> str:
    for s in summaries:
        if s is not None and s.get("label"):
            return str(s["label"])
    return "CodeQL" if subdir == "codeql" else subdir


def discover_codeql_check_subdirs(
    dest: Path,
    module_paths: List[str],
    *,
    default_subdir: str,
    coverage_subdirs: Iterable[str],
) -> List[str]:
    """Find every codeql-check subdirectory published on the baseline branch.

    A check subdirectory is any directory (under the branch root or under a
    module) whose ``summary.json`` is codeql-shaped (has a ``findings`` key).
    This keeps the catalog order-independent: any publisher writing a new
    check (e.g. ``codeql-jpl``) shows up on regeneration without new flags.
    The default subdirectory sorts first; others follow alphabetically.
    """
    excluded = {s for s in coverage_subdirs if s}
    names: set[str] = set()
    for base in [dest] + [dest / p for p in module_paths]:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir() or child.name in excluded or child.name in names:
                continue
            if load_codeql_summary(child / "summary.json") is not None:
                names.add(child.name)
    ordered = sorted(n for n in names if n != default_subdir)
    if default_subdir in names or not ordered:
        ordered.insert(0, default_subdir)
    return ordered


@dataclass
class ModuleEntry:
    """One row in the catalog."""

    path: str  # e.g. "Svc/CmdDispatcher"
    has_ut: bool
    ut_summary: Optional[Summary]
    ut_report: str
    int_summary: Optional[Summary]
    int_report: str
    #: Ordered mapping of check subdirectory -> summary (None when the check
    #: has not published data for this module).
    codeql_checks: dict
    codeql_report: str  # primary (first) check's findings page
    checks_summary: Optional[dict] = None
    checks_report: str = ""
    sdd_summary: Optional[dict] = None
    sdd_report: str = ""
    module_report: Optional[str] = None  # roll-up page, when one is written

    @property
    def top_dir(self) -> str:
        return self.path.split("/", 1)[0] if "/" in self.path else self.path

    @property
    def has_coverage(self) -> bool:
        return self.ut_summary is not None and self.ut_summary.line.total > 0

    @property
    def has_int_coverage(self) -> bool:
        return self.int_summary is not None and self.int_summary.line.total > 0

    @property
    def codeql_summary(self) -> Optional[dict]:
        """Aggregated roll-up across every published codeql check."""
        return aggregate_codeql(self.codeql_checks.values())

    def rollup_tier(self, thresholds: CoverageThresholds) -> str:
        """Worst (minimum) tier across every artifact with published data.

        Artifacts without data are excluded, except the SDD, which always
        participates: a missing SDD counts as bronze.
        """
        tiers: list[str] = []
        if self.ut_summary is not None:
            tiers.append(coverage_tier(self.ut_summary.line.percent, self.has_coverage, thresholds))
        if self.int_summary is not None:
            tiers.append(coverage_tier(self.int_summary.line.percent, self.has_int_coverage, thresholds))
        agg = self.codeql_summary
        if agg is not None:
            tiers.append(codeql_tier(agg.get("worst")))
        if self.checks_summary is not None:
            tiers.append(checks_tier(
                int(self.checks_summary.get("passed", 0) or 0),
                int(self.checks_summary.get("failed", 0) or 0),
            ))
        tiers.append(self.sdd_tier)
        return TIERS[max(TIERS.index(t) for t in tiers)]

    @property
    def sdd_tier(self) -> str:
        """SDD tier; bronze when no SDD grade has been published."""
        if self.sdd_summary is None:
            return "bronze"
        return self.sdd_summary["tier"]


@dataclass
class Group:
    """Modules grouped by their top-level directory (Fw, Svc, ...)."""

    name: str
    modules: List[ModuleEntry] = field(default_factory=list)

    def rollup(self) -> Summary:
        """Sum-weighted rollup across all modules in the group."""
        line = Totals(
            covered=sum(m.ut_summary.line.covered for m in self.modules if m.ut_summary),
            total=sum(m.ut_summary.line.total for m in self.modules if m.ut_summary),
        )
        function = Totals(
            covered=sum(m.ut_summary.function.covered for m in self.modules if m.ut_summary),
            total=sum(m.ut_summary.function.total for m in self.modules if m.ut_summary),
        )
        branch = Totals(
            covered=sum(m.ut_summary.branch.covered for m in self.modules if m.ut_summary),
            total=sum(m.ut_summary.branch.total for m in self.modules if m.ut_summary),
        )
        return Summary(line=line, function=function, branch=branch)

    def codeql_worst(self) -> tuple[bool, Optional[str]]:
        """(any codeql data present, worst severity across modules)."""
        present = False
        worst_rank = None
        order = {"error": 0, "medium": 1, "low": 2}
        for m in self.modules:
            if m.codeql_summary is None:
                continue
            present = True
            worst = m.codeql_summary.get("worst")
            if worst in order and (worst_rank is None or order[worst] < order[worst_rank]):
                worst_rank = worst
        return present, worst_rank

    def checks_worst(self) -> tuple[bool, Optional[str]]:
        """(any checks data present, worst checks tier across modules)."""
        present = False
        worst_idx = None
        for m in self.modules:
            if m.checks_summary is None:
                continue
            present = True
            tier = checks_tier(
                int(m.checks_summary.get("passed", 0) or 0),
                int(m.checks_summary.get("failed", 0) or 0),
            )
            idx = TIERS.index(tier)
            if worst_idx is None or idx > worst_idx:
                worst_idx = idx
        return present, (TIERS[worst_idx] if worst_idx is not None else None)


def _group_modules(entries: Iterable[ModuleEntry]) -> List[Group]:
    """Group entries by top-level directory, sorted alphabetically.

    Modules with no top-level component (paths without a ``/``) are placed in
    a synthetic ``"(root)"`` group.
    """
    groups: dict[str, Group] = {}
    for entry in entries:
        groups.setdefault(entry.top_dir, Group(name=entry.top_dir)).modules.append(entry)
    for g in groups.values():
        g.modules.sort(key=lambda m: m.path)
    return [groups[k] for k in sorted(groups.keys())]


def _coverage_cell(
    summary: Optional[Summary],
    has_coverage: bool,
    report: str,
    thresholds: CoverageThresholds,
    *,
    missing_note: str,
) -> str:
    if not has_coverage:
        tier = coverage_tier(0.0, False, thresholds)
        return f'{badge_html(tier)} <span class="no-cov">{html.escape(missing_note)}</span>'
    tier = coverage_tier(summary.line.percent, True, thresholds)
    return (
        f'{badge_html(tier)} '
        f'<a href="{html.escape(report)}">{summary.line.percent:.2f}%</a>'
    )


def _int_cell(entry: ModuleEntry, thresholds: CoverageThresholds) -> str:
    if entry.int_summary is None:
        return '<span class="no-cov">&mdash;</span>'
    return _coverage_cell(
        entry.int_summary, entry.has_int_coverage, entry.int_report, thresholds,
        missing_note="no coverage",
    )


def _codeql_cell(entry: ModuleEntry) -> str:
    summary = entry.codeql_summary
    if summary is None:
        return '<span class="no-cov">&mdash;</span>'
    count = int(summary.get("findings", 0))
    tier = codeql_tier(summary.get("worst"))
    label = "clean" if count == 0 else f"{count} finding{'s' if count != 1 else ''}"
    published = [s for s in entry.codeql_checks.values() if s is not None]
    href = (
        entry.codeql_report
        if len(published) == 1 or entry.module_report is None
        else entry.module_report
    )
    return f'{badge_html(tier)} <a href="{html.escape(href)}">{label}</a>'


def _checks_cell(entry: ModuleEntry) -> str:
    summary = entry.checks_summary
    if summary is None:
        return '<span class="no-cov">&mdash;</span>'
    passed = int(summary.get("passed", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    tier = checks_tier(passed, failed)
    label = f"{passed}/{passed + failed + skipped} - {skipped} skipped"
    return f'{badge_html(tier)} <a href="{html.escape(entry.checks_report)}">{label}</a>'


def _render_group_header(group: Group, thresholds: CoverageThresholds) -> str:
    rollup = group.rollup()
    has_coverage = rollup.line.total > 0
    label_esc = html.escape(group.name + "/")
    if has_coverage:
        ut_tier = coverage_tier(rollup.line.percent, True, thresholds)
        ut_html = f"{badge_html(ut_tier)} {rollup.line.percent:.2f}%"
    else:
        ut_html = f'{badge_html("bronze")} <span class="no-cov">&mdash;</span>'
    int_html = '<span class="no-cov">&mdash;</span>'
    present, worst = group.codeql_worst()
    codeql_html = badge_html(codeql_tier(worst)) if present else '<span class="no-cov">&mdash;</span>'
    checks_present, checks_worst = group.checks_worst()
    checks_html = badge_html(checks_worst) if checks_present and checks_worst else '<span class="no-cov">&mdash;</span>'
    return (
        f"<span>{label_esc}</span><span>{ut_html}</span><span>{int_html}</span>"
        f"<span>{codeql_html}</span><span>{checks_html}</span>"
    )


def _render_group(group: Group, thresholds: CoverageThresholds) -> str:
    open_attr = " open" if group.name in {"Fw", "Svc"} else ""
    header = _render_group_header(group, thresholds)

    rows: list[str] = []
    for mod in group.modules:
        path_esc = html.escape(mod.path)
        missing_note = "no UT" if not mod.has_ut else "no coverage"
        ut_html = _coverage_cell(
            mod.ut_summary, mod.has_coverage, mod.ut_report, thresholds,
            missing_note=missing_note,
        )
        rows.append(
            f'<tr class="row">'
            f'<td><a href="{html.escape(mod.module_report or mod.ut_report)}">{path_esc}</a> '
            f'{badge_html(mod.rollup_tier(thresholds))}</td>'
            f'<td class="cell">{ut_html}</td>'
            f'<td class="cell">{_int_cell(mod, thresholds)}</td>'
            f'<td class="cell">{_codeql_cell(mod)}</td>'
            f'<td class="cell">{_checks_cell(mod)}</td>'
            f"</tr>"
        )

    return (
        f"<details{open_attr}><summary>{header}</summary>"
        f'<table><thead><tr><th>Module</th><th class="cell">UT Coverage</th>'
        f'<th class="cell">Int Coverage</th><th class="cell">CodeQL</th>'
        f'<th class="cell">Checklist</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"</details>"
    )


def render_module_index_html(
    *,
    entry: ModuleEntry,
    ref: str,
    ref_type: str,
    commit: str,
    generated_at: str,
    thresholds: CoverageThresholds,
    coverage_subdir: str,
    int_subdir: str,
    checks_subdir: str,
    sdd_subdir: str,
    check_labels: dict,
) -> str:
    """Render a module's roll-up page linking its published artifact subtrees.

    Links are relative to the module directory itself, so the page works
    wherever the baseline branch is hosted.
    """
    rollup = entry.rollup_tier(thresholds)
    welcome = (
        f"This page summarizes the quality grades for the "
        f"<code>{html.escape(entry.path)}</code> module of F\u00b4: test "
        "coverage, static analysis findings, component development "
        "checklist results, and the software description document, each "
        "graded on a Platinum/Gold/Silver/Bronze "
        "tier scale. The overall badge above is the minimum tier across all "
        "graded artifacts. Follow a row's link for the underlying report."
    )

    rows: list[str] = []

    missing_note = "no UT" if not entry.has_ut else "no coverage"
    ut_html = _coverage_cell(
        entry.ut_summary, entry.has_coverage, f"{coverage_subdir}/index.html",
        thresholds, missing_note=missing_note,
    )
    rows.append(f"<tr><td>Unit Test Coverage</td><td>{ut_html}</td></tr>")

    if entry.int_summary is not None:
        int_html = _coverage_cell(
            entry.int_summary, entry.has_int_coverage, f"{int_subdir}/index.html",
            thresholds, missing_note="no coverage",
        )
        # Also link the module's record in the full-system integration report
        # (scroll-to-text fragment highlights the module's files where supported).
        full_href = (
            "../" * (entry.path.count("/") + 1)
            + f"{int_subdir}/coverage-all.html#:~:text={quote(entry.path)}"
        )
        int_html += f' &middot; <a href="{html.escape(full_href)}">full-system report</a>'
    else:
        int_html = '<span class="no-cov">no data</span>'
    rows.append(f"<tr><td>Integration Test Coverage</td><td>{int_html}</td></tr>")

    for subdir, summary in entry.codeql_checks.items():
        row_label = html.escape(check_labels.get(subdir, subdir))
        if summary is not None:
            count = int(summary.get("findings", 0))
            dismissed = int(summary.get("dismissed", 0) or 0)
            tier = codeql_tier(summary.get("worst"))
            label = "clean" if count == 0 else f"{count} finding{'s' if count != 1 else ''}"
            if dismissed:
                label += f" ({dismissed} dismissed)"
            codeql_html = (
                f'{badge_html(tier)} <a href="{html.escape(subdir)}/index.html">{label}</a>'
            )
        else:
            codeql_html = '<span class="no-cov">no data</span>'
        rows.append(f"<tr><td>{row_label}</td><td>{codeql_html}</td></tr>")

    if entry.checks_summary is not None:
        passed = int(entry.checks_summary.get("passed", 0) or 0)
        failed = int(entry.checks_summary.get("failed", 0) or 0)
        skipped = int(entry.checks_summary.get("skipped", 0) or 0)
        tier = checks_tier(passed, failed)
        label = (
            f"{passed} passed / {skipped} skipped / {failed} failed / "
            f"{passed + failed + skipped} total"
        )
        checks_html = (
            f'{badge_html(tier)} <a href="{html.escape(checks_subdir)}/index.html">{label}</a>'
        )
    else:
        checks_html = '<span class="no-cov">no data</span>'
    rows.append(f"<tr><td>Checklist</td><td>{checks_html}</td></tr>")

    if entry.sdd_summary is not None:
        label = "grade" if entry.sdd_summary.get("has_sdd") else "missing"
        sdd_html = (
            f'{badge_html(entry.sdd_tier)} '
            f'<a href="{html.escape(sdd_subdir)}/index.html">{label}</a>'
        )
        rendered = entry.sdd_summary.get("rendered_url")
        if rendered:
            sdd_html += f' &middot; <a href="{html.escape(str(rendered))}">rendered view</a>'
    else:
        sdd_html = f'{badge_html("bronze")} <span class="no-cov">no data</span>'
    rows.append(f"<tr><td>Software Description Document</td><td>{sdd_html}</td></tr>")

    doxygen = (entry.sdd_summary or {}).get("doxygen") or []
    if doxygen:
        links = " &middot; ".join(
            f'<a href="{html.escape(str(d.get("url", "")))}"><code>{html.escape(str(d.get("name", "")))}</code></a>'
            for d in doxygen
            if d.get("url")
        )
        if links:
            rows.append(f"<tr><td>API Documentation (Doxygen)</td><td>{links}</td></tr>")

    depth = entry.path.count("/") + 1
    checklist_href = "../" * depth + "index.html"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(entry.path)} \u2014 {html.escape(ref)}</title>"
        f"<style>{MODULE_CSS}</style></head><body>"
        f"<h1><code>{html.escape(entry.path)}</code> {badge_html(rollup)}</h1>"
        f'<div class="meta"><a href="{checklist_href}">\u2190 all modules</a> &middot; '
        f"{html.escape(ref_type)} <code>{html.escape(ref)}</code> "
        f"@ <code>{html.escape(commit[:12])}</code> &middot; generated {html.escape(generated_at)}</div>"
        f'<div class="welcome">{welcome}</div>'
        "<table><thead><tr><th>Artifact</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</body></html>\n"
    )


def render_index_html(
    *,
    ref: str,
    ref_type: str,
    commit: str,
    generated_at: str,
    overall: Optional[Summary],
    overall_report: str,
    overall_int: Optional[Summary],
    overall_int_report: str,
    overall_codeql_checks: dict,
    check_labels: dict,
    entries: List[ModuleEntry],
    thresholds: CoverageThresholds,
    versions_nav: str = "",
) -> str:
    """Render the checklist landing page."""
    groups = _group_modules(entries)

    parts = []
    if overall is not None and overall.line.total > 0:
        tier = coverage_tier(overall.line.percent, True, thresholds)
        parts.append(
            f"<strong>UT coverage:</strong> {badge_html(tier)} "
            f'<a href="{html.escape(overall_report)}">{overall.line.percent:.2f}% line</a> '
            f"({overall.function.percent:.2f}% function, {overall.branch.percent:.2f}% branch)"
        )
    else:
        parts.append('<strong>UT coverage:</strong> <span class="no-cov">no data</span>')
    if overall_int is not None and overall_int.line.total > 0:
        int_tier = coverage_tier(overall_int.line.percent, True, thresholds)
        parts.append(
            f"<strong>Int coverage:</strong> {badge_html(int_tier)} "
            f'<a href="{html.escape(overall_int_report)}">{overall_int.line.percent:.2f}% line</a> '
            f"({overall_int.function.percent:.2f}% function, {overall_int.branch.percent:.2f}% branch)"
        )
    else:
        parts.append('<strong>Int coverage:</strong> <span class="no-cov">no data</span>')
    for check_subdir, check_summary in overall_codeql_checks.items():
        name = html.escape(check_labels.get(check_subdir, check_subdir))
        if check_summary is not None:
            count = int(check_summary.get("findings", 0))
            tier = codeql_tier(check_summary.get("worst"))
            label = "clean" if count == 0 else f"{count} finding{'s' if count != 1 else ''}"
            parts.append(
                f"<strong>{name}:</strong> {badge_html(tier)} "
                f'<a href="{html.escape(check_subdir)}/index.html">{label}</a>'
            )
        else:
            parts.append(f'<strong>{name}:</strong> <span class="no-cov">no data</span>')
    overall_html = " &middot; ".join(f"<span>{p}</span>" for p in parts)

    group_html = "\n".join(_render_group(g, thresholds) for g in groups)

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>F\u00b4 Component Quality \u2014 {html.escape(ref)}</title>"
        f"<style>{CSS}</style></head><body>"
        f"{versions_nav}"
        f"<h1>F\u00b4 Component Quality</h1>"
        f'<div class="meta">{html.escape(ref_type)} <code>{html.escape(ref)}</code> '
        f"@ <code>{html.escape(commit[:12])}</code> &middot; generated {html.escape(generated_at)}</div>"
        f'<div class="welcome">{WELCOME_HTML}</div>'
        f'<div class="overall">{overall_html}</div>'
        f'<div class="section-title">Modules</div>'
        f"{group_html}"
        f"</body></html>\n"
    )


def build_catalog(
    *,
    modules: List[ModuleEntry],
    ref: str,
    ref_type: str,
    commit: str,
    generated_at: str,
    overall: Optional[Summary],
    overall_report: str,
    overall_int: Optional[Summary],
    overall_int_report: str,
    overall_codeql_checks: dict,
    thresholds: CoverageThresholds,
) -> dict:
    out_modules = []
    for m in modules:
        ut_entry = None
        if m.has_coverage:
            ut_entry = m.ut_summary.to_catalog_entry()
        if ut_entry is not None:
            ut_entry["report"] = m.ut_report
            ut_entry["tier"] = coverage_tier(m.ut_summary.line.percent, True, thresholds)
        int_entry = None
        if m.has_int_coverage:
            int_entry = m.int_summary.to_catalog_entry()
            int_entry["report"] = m.int_report
            int_entry["tier"] = coverage_tier(m.int_summary.line.percent, True, thresholds)
        agg = m.codeql_summary
        codeql_entry = None
        if agg is not None:
            codeql_entry = dict(agg)
            codeql_entry["report"] = m.codeql_report
            codeql_entry.setdefault("tier", codeql_tier(agg.get("worst")))
        checks_entry = None
        if m.checks_summary is not None:
            passed = int(m.checks_summary.get("passed", 0) or 0)
            failed = int(m.checks_summary.get("failed", 0) or 0)
            checks_entry = {
                "passed": passed,
                "failed": failed,
                "skipped": int(m.checks_summary.get("skipped", 0) or 0),
                "tier": checks_tier(passed, failed),
                "report": m.checks_report,
            }
        sdd_entry = None
        if m.sdd_summary is not None:
            sdd_entry = {
                "tier": m.sdd_tier,
                "has_sdd": bool(m.sdd_summary.get("has_sdd")),
                "report": m.sdd_report,
            }
        codeql_checks_entry = {}
        for check_subdir, check_summary in m.codeql_checks.items():
            if check_summary is None:
                codeql_checks_entry[check_subdir] = None
                continue
            check_entry = dict(check_summary)
            check_entry["report"] = f"{m.path}/{check_subdir}/index.html"
            check_entry.setdefault("tier", codeql_tier(check_summary.get("worst")))
            codeql_checks_entry[check_subdir] = check_entry
        out_modules.append(
            {
                "path": m.path,
                "has_ut": m.has_ut,
                "has_coverage": m.has_coverage,
                "ut": ut_entry,
                "int": int_entry,
                "codeql": codeql_entry,
                "codeql_checks": codeql_checks_entry,
                "checks": checks_entry,
                "sdd": sdd_entry,
                "tiers": {
                    "overall": m.rollup_tier(thresholds),
                    "ut": coverage_tier(
                        m.ut_summary.line.percent if m.has_coverage else 0.0,
                        m.has_coverage,
                        thresholds,
                    ),
                    "int": (
                        coverage_tier(m.int_summary.line.percent, True, thresholds)
                        if m.has_int_coverage
                        else None
                    ),
                    "codeql": (
                        codeql_tier(agg.get("worst")) if agg is not None else None
                    ),
                    "checks": (
                        checks_entry["tier"] if checks_entry is not None else None
                    ),
                    # SDD always participates; missing SDD grades as bronze.
                    "sdd": m.sdd_tier,
                },
            }
        )

    overall_entry = None
    if overall is not None and overall.line.total > 0:
        overall_entry = overall.to_catalog_entry()
        overall_entry["report"] = overall_report
        overall_entry["tier"] = coverage_tier(overall.line.percent, True, thresholds)

    overall_int_entry = None
    if overall_int is not None and overall_int.line.total > 0:
        overall_int_entry = overall_int.to_catalog_entry()
        overall_int_entry["report"] = overall_int_report
        overall_int_entry["tier"] = coverage_tier(overall_int.line.percent, True, thresholds)

    overall_codeql_entry = None
    overall_agg = aggregate_codeql(overall_codeql_checks.values())
    if overall_agg is not None:
        overall_codeql_entry = dict(overall_agg)
        first_present = next(
            s for s, v in overall_codeql_checks.items() if v is not None
        )
        overall_codeql_entry["report"] = f"{first_present}/index.html"
    overall_checks_entry = {}
    for check_subdir, check_summary in overall_codeql_checks.items():
        if check_summary is None:
            overall_checks_entry[check_subdir] = None
            continue
        check_entry = dict(check_summary)
        check_entry["report"] = f"{check_subdir}/index.html"
        check_entry.setdefault("tier", codeql_tier(check_summary.get("worst")))
        overall_checks_entry[check_subdir] = check_entry

    return {
        "schema": SCHEMA_VERSION,
        "ref": ref,
        "ref_type": ref_type,
        "commit": commit,
        "generated_at": generated_at,
        "thresholds": {
            "platinum": thresholds.platinum,
            "gold": thresholds.gold,
            "silver": thresholds.silver,
        },
        "overall": overall_entry,
        "overall_int": overall_int_entry,
        "overall_codeql": overall_codeql_entry,
        "overall_codeql_checks": overall_checks_entry,
        "modules": out_modules,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", type=Path, required=True,
        help="Baseline worktree root; summaries are read from and outputs written to it",
    )
    parser.add_argument(
        "--modules-jsonl",
        type=Path,
        required=True,
        help="JSON-Lines module list from discover.py",
    )
    parser.add_argument(
        "--coverage-subdirectory",
        default="coverage",
        help="Subdirectory under each module holding UT coverage artifacts; "
        '"" flattens (default: "coverage")',
    )
    parser.add_argument(
        "--int-coverage-subdirectory",
        default="int-coverage",
        help='Subdirectory under each module holding integration coverage (default: "int-coverage")',
    )
    parser.add_argument(
        "--codeql-subdirectory",
        default="codeql",
        help="Primary subdirectory under each module holding CodeQL findings "
        '(default: "codeql").  Additional check subdirectories published on '
        "the baseline branch are auto-discovered and listed as well.",
    )
    parser.add_argument(
        "--checks-subdirectory",
        default="checks",
        help="Subdirectory under each module holding checklist-check results "
        '(default: "checks")',
    )
    parser.add_argument(
        "--sdd-subdirectory",
        default="sdd",
        help="Subdirectory under each module holding the SDD grade artifacts "
        '(default: "sdd")',
    )
    parser.add_argument("--ref", required=True, help="Source ref name (branch or tag)")
    parser.add_argument("--ref-type", default="branch", choices=("branch", "tag"))
    parser.add_argument("--commit", required=True, help="Source commit SHA")
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO8601 timestamp; defaults to now() UTC",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Checklist config file (e.g. .github/module-checklist.yml); "
        "defaults apply when omitted or missing",
    )
    args = parser.parse_args(argv)

    dest = args.dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)

    subdir = args.coverage_subdirectory
    subdir_segment = f"{subdir}/" if subdir else ""
    int_subdir = args.int_coverage_subdirectory or "int-coverage"
    codeql_subdir = args.codeql_subdirectory or "codeql"
    checks_subdir = args.checks_subdirectory or "checks"
    sdd_subdir = args.sdd_subdirectory or "sdd"
    thresholds = coverage_thresholds(load_config(args.config))

    generated_at = args.generated_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    records: list[dict] = []
    with args.modules_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    check_subdirs = discover_codeql_check_subdirs(
        dest,
        [rec["path"] for rec in records],
        default_subdir=codeql_subdir,
        coverage_subdirs=(subdir, int_subdir, checks_subdir, sdd_subdir),
    )

    entries: list[ModuleEntry] = []
    for rec in records:
        path = rec["path"]
        has_ut = bool(rec.get("has_ut", False))
        mod_dir = dest / path
        ut_summary = load_summary(
            (mod_dir / subdir if subdir else mod_dir) / "summary.json"
        )
        int_summary = load_summary(mod_dir / int_subdir / "summary.json")
        codeql_checks = {
            s: load_codeql_summary(mod_dir / s / "summary.json") for s in check_subdirs
        }
        entries.append(
            ModuleEntry(
                path=path,
                has_ut=has_ut,
                ut_summary=ut_summary,
                ut_report=f"{path}/{subdir_segment}index.html",
                int_summary=int_summary,
                int_report=f"{path}/{int_subdir}/index.html",
                codeql_checks=codeql_checks,
                codeql_report=f"{path}/{check_subdirs[0]}/index.html",
                checks_summary=load_checks_summary(mod_dir / checks_subdir / "summary.json"),
                checks_report=f"{path}/{checks_subdir}/index.html",
                sdd_summary=load_sdd_summary(mod_dir / sdd_subdir / "summary.json"),
                sdd_report=f"{path}/{sdd_subdir}/index.html",
                module_report=f"{path}/index.html" if subdir else None,
            )
        )

    overall_codeql_checks = {
        s: load_codeql_summary(dest / s / "summary.json") for s in check_subdirs
    }
    check_labels = {
        s: _check_label(
            s,
            overall_codeql_checks.get(s),
            *(e.codeql_checks.get(s) for e in entries),
        )
        for s in check_subdirs
    }

    # Per-module roll-up pages.  In flatten mode (subdir == "") the module
    # root already holds the coverage report's own index.html, so skip.
    if subdir:
        for entry in entries:
            mod_dir = dest / entry.path
            mod_dir.mkdir(parents=True, exist_ok=True)
            (mod_dir / "index.html").write_text(
                render_module_index_html(
                    entry=entry,
                    ref=args.ref,
                    ref_type=args.ref_type,
                    commit=args.commit,
                    generated_at=generated_at,
                    thresholds=thresholds,
                    coverage_subdir=subdir,
                    int_subdir=int_subdir,
                    checks_subdir=checks_subdir,
                    sdd_subdir=sdd_subdir,
                    check_labels=check_labels,
                ),
                encoding="utf-8",
            )

    overall_dir = dest / subdir if subdir else dest
    overall_summary = load_summary(overall_dir / "summary.json")
    overall_report = f"{subdir_segment}coverage-all.html" if subdir else "coverage-all.html"
    overall_int_summary = load_summary(dest / int_subdir / "summary.json")
    overall_int_report = f"{int_subdir}/coverage-all.html"

    catalog = build_catalog(
        modules=entries,
        ref=args.ref,
        ref_type=args.ref_type,
        commit=args.commit,
        generated_at=generated_at,
        overall=overall_summary,
        overall_report=overall_report,
        overall_int=overall_int_summary,
        overall_int_report=overall_int_report,
        overall_codeql_checks=overall_codeql_checks,
        thresholds=thresholds,
    )
    (dest / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

    html_text = render_index_html(
        ref=args.ref,
        ref_type=args.ref_type,
        commit=args.commit,
        generated_at=generated_at,
        overall=overall_summary,
        overall_report=overall_report,
        overall_int=overall_int_summary,
        overall_int_report=overall_int_report,
        overall_codeql_checks=overall_codeql_checks,
        check_labels=check_labels,
        entries=entries,
        thresholds=thresholds,
        versions_nav=versions_mod.render_nav(
            versions_mod.load_versions(dest), current=args.ref
        ),
    )
    (dest / "index.html").write_text(html_text, encoding="utf-8")

    print(f"catalog: wrote {dest/'catalog.json'} ({len(entries)} modules)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
