"""Badge-tier computation shared by the catalog and CodeQL scripts.

Tiers (best to worst): platinum, gold, silver, bronze.

Coverage tiers are driven by line-coverage percent -- the same metric the
delta detector (``compare.py``) uses for its regression gate -- against
configurable thresholds.  Modules with no coverage data are bronze.

CodeQL tiers are driven by the worst finding severity:
    * no findings          -> platinum (gold is also "no findings"; the
                              distinction is reserved for future criteria,
                              so today a clean module earns platinum)
    * worst is low/note    -> silver
    * worst is medium or
      error/high/critical  -> bronze
"""

from __future__ import annotations

from dataclasses import dataclass

TIERS = ("platinum", "gold", "silver", "bronze")

#: Normalized severity buckets, ordered worst-first.
SEVERITY_ORDER = ("error", "medium", "low")


@dataclass(frozen=True)
class CoverageThresholds:
    """Line-coverage percentage cut-offs for each tier."""

    platinum: float = 95.0
    gold: float = 90.0
    silver: float = 80.0


def coverage_tier(line_pct: float, has_coverage: bool, thresholds: CoverageThresholds) -> str:
    if not has_coverage:
        return "bronze"
    if line_pct >= thresholds.platinum:
        return "platinum"
    if line_pct >= thresholds.gold:
        return "gold"
    if line_pct >= thresholds.silver:
        return "silver"
    return "bronze"


def normalize_severity(level: str, security_severity: float | None = None) -> str:
    """Collapse SARIF levels / security severities into error, medium, or low.

    ``security_severity`` (CodeQL's ``security-severity`` rule property,
    0.0-10.0) takes precedence when provided: >= 7.0 is error (high/critical),
    >= 4.0 is medium, below is low.  Otherwise the SARIF ``level`` maps
    error -> error, warning -> medium, note/none -> low.
    """
    if security_severity is not None:
        if security_severity >= 7.0:
            return "error"
        if security_severity >= 4.0:
            return "medium"
        return "low"
    level = (level or "").lower()
    if level == "error":
        return "error"
    if level == "warning":
        return "medium"
    return "low"


def codeql_tier(worst_severity: str | None) -> str:
    """Map the worst normalized severity in a module to a tier."""
    if worst_severity is None:
        return "platinum"
    if worst_severity == "low":
        return "silver"
    return "bronze"


def checklist_tier(summary: dict) -> str:
    """Highest tier a component's development checklist allows.

    No checklist kept in the repository caps the component at silver.  With
    one, every item YES or N/A allows platinum (a reviewer's recorded
    override of a tool verdict counts as the reviewer's status); anything
    else caps it at gold.
    """
    if not summary.get("source"):
        return "silver"
    counts = summary.get("counts") or {}
    open_items = sum(int(counts.get(k, 0) or 0) for k in ("NO", "REVIEW", "UNCHECKED"))
    satisfied = sum(int(counts.get(k, 0) or 0) for k in ("YES", "N/A"))
    return "platinum" if open_items == 0 and satisfied > 0 else "gold"


def checks_tier(passed: int, failed: int) -> str:
    """Map checklist-check pass/fail counts to a tier.

    All passing is platinum; otherwise the pass ratio maps through the
    default coverage-style cut-offs (>= 90% gold, >= 80% silver, else
    bronze).  No graded checks at all is bronze.
    """
    graded = passed + failed
    if graded <= 0:
        return "bronze"
    if failed == 0:
        return "platinum"
    ratio = 100.0 * passed / graded
    if ratio >= 90.0:
        return "gold"
    if ratio >= 80.0:
        return "silver"
    return "bronze"
