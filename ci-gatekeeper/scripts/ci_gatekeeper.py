"""Approve held ("Approve and run") workflow runs on PRs the autobot review cleared.

Fork pull requests from first-time contributors sit with every workflow run in
``action_required`` until a maintainer clicks "Approve and run".  This script
performs that click automatically, but only for a PR whose *current head* has
been reviewed by an allow-listed autobot whose summary review says
**CI safety: Go**.

Algorithm (deterministic, no LLM involved):

1. Queue check.  Count the repository's ``queued`` + ``in_progress`` workflow
   runs (excluding this gatekeeper's own run).  If the count reaches the busy
   threshold, report "busy" and exit *without* looking at any PR.
2. Candidate discovery.  List open, non-draft PRs; keep those whose head SHA
   has at least one ``action_required`` run.
3. Verdict parse.  Fetch the PR's reviews; keep reviews by an allow-listed
   author that carry the ``<!-- fprime-review-summary v1 -->`` marker; take the
   latest.  Require its ``<!-- reviewed_head: <sha> -->`` to equal the PR head
   exactly and its ``| **CI safety** | ... | **Go** |`` table row to carry the
   exact token ``Go`` (see :func:`parse_summary` / :func:`decide`).
4. Selection.  Eligible PRs ordered by PR number (oldest first); the first
   ``--max-approvals`` of them are acted on.
5. Act.  ``POST /repos/{repo}/actions/runs/{run_id}/approve`` for each held
   run of the selected PR(s).  Dry-run (the default) only reports.

Fail closed: anything that is not an exact ``Go`` is "no verdict".

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

SUMMARY_MARKER = "<!-- fprime-review-summary v1 -->"
DEFAULT_REVIEWERS = ("lestarch-autobot", "thomas-bc-autowot")
DEFAULT_BUSY_THRESHOLD = 10
DEFAULT_MAX_APPROVALS = 1

# ---------------------------------------------------------------------------
# Parse rules (the contract: nasa/fprime .github/agents/_shared/review-contract.md
# and .github/agents/review-summary.agent.md)
# ---------------------------------------------------------------------------

# ``<!-- reviewed_head: <full 40-hex sha> -->`` on a line of its own.  Anything
# shorter than 40 hex characters does not match and therefore does not count.
REVIEWED_HEAD_RE = re.compile(
    r"^[ \t]*<!--[ \t]*reviewed_head:[ \t]*([0-9a-fA-F]{40})[ \t]*-->[ \t]*$",
    re.MULTILINE,
)

# The per-agent results table row ``| **CI safety** | — | — | — | — | — | <verdict> |``.
# The five tag-count cells may hold only dashes/whitespace; the last cell is
# captured verbatim and judged by CI_SAFETY_GO_RE / CI_SAFETY_NOGO_RE below.
CI_SAFETY_ROW_RE = re.compile(
    r"^[ \t]*\|[ \t]*\*\*CI safety\*\*[ \t]*\|"
    r"(?:[ \t]*[—–-]*[ \t]*\|){5}"
    r"(?P<cell>[^|\n]*)\|[ \t]*$",
    re.MULTILINE,
)
# The verdict cell must be exactly ``**Go**`` — no rationale, no other text.
CI_SAFETY_GO_RE = re.compile(r"^\*\*Go\*\*$")
# ``**No-Go**`` optionally followed by `` — <rationale>``.
CI_SAFETY_NOGO_RE = re.compile(r"^\*\*No-Go\*\*(?:\s*[—–-]+\s*(?P<rationale>.*))?$")

# Optional hidden metadata ``<!-- ci_safety: Go | No-Go -->`` (contract §2).  The
# trailing ``:`` keeps ``ci_safety_rationale`` from matching.
CI_SAFETY_META_RE = re.compile(
    r"^[ \t]*<!--[ \t]*ci_safety:[ \t]*(?P<verdict>[^\s>-]+(?:-[^\s>-]+)*)[ \t]*-->[ \t]*$",
    re.MULTILINE,
)

# ``### Recommend: Close`` heading or the bold ``**Recommend: Close**`` lead-in.
RECOMMEND_CLOSE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*Recommend:[ \t]*Close[ \t]*$|\*\*Recommend:[ \t]*Close\*\*)",
    re.MULTILINE,
)

GO = "Go"
NO_GO = "No-Go"


class GatekeeperError(Exception):
    """Raised for unrecoverable configuration or API failures."""


@dataclass
class SummaryVerdict:
    """What :func:`parse_summary` extracted from one summary review body."""

    reviewed_head: Optional[str]
    reviewed_heads: List[str]
    table_verdicts: List[str]       # one entry per CI safety row: "Go", "No-Go", or "?"
    table_rationale: Optional[str]
    meta_verdicts: List[str]        # one entry per <!-- ci_safety: --> line, verbatim
    recommend_close: bool

    @property
    def ci_safety_go(self) -> bool:
        """True iff every verdict source present says exactly ``Go``."""
        if not self.table_verdicts:
            return False
        if any(v != GO for v in self.table_verdicts):
            return False
        if any(v != GO for v in self.meta_verdicts):
            return False
        return not self.recommend_close


def parse_summary(body: str) -> SummaryVerdict:
    """Extract the CI-safety verdict and reviewed head from a summary review body."""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    heads = [m.group(1).lower() for m in REVIEWED_HEAD_RE.finditer(body)]
    unique_heads = sorted(set(heads))
    table_verdicts: List[str] = []
    rationale: Optional[str] = None
    for match in CI_SAFETY_ROW_RE.finditer(body):
        cell = match.group("cell").strip()
        if CI_SAFETY_GO_RE.match(cell):
            table_verdicts.append(GO)
            continue
        nogo = CI_SAFETY_NOGO_RE.match(cell)
        if nogo:
            table_verdicts.append(NO_GO)
            rationale = (nogo.group("rationale") or "").strip() or rationale
        else:
            table_verdicts.append("?")
    meta = [m.group("verdict") for m in CI_SAFETY_META_RE.finditer(body)]
    return SummaryVerdict(
        reviewed_head=unique_heads[0] if len(unique_heads) == 1 else None,
        reviewed_heads=heads,
        table_verdicts=table_verdicts,
        table_rationale=rationale,
        meta_verdicts=meta,
        recommend_close=RECOMMEND_CLOSE_RE.search(body) is not None,
    )


@dataclass
class Decision:
    eligible: bool
    reason: str
    review_id: Optional[int] = None
    review_url: Optional[str] = None


def select_summary_review(reviews: Iterable[dict], reviewers: Iterable[str]) -> Optional[dict]:
    """Latest non-dismissed summary review by an allow-listed author, or None."""
    allowed = {r.strip().lower() for r in reviewers if r.strip()}
    candidates = []
    for review in reviews:
        login = ((review.get("user") or {}).get("login") or "").lower()
        body = review.get("body") or ""
        if login not in allowed or SUMMARY_MARKER not in body:
            continue
        if (review.get("state") or "").upper() == "DISMISSED":
            continue
        candidates.append(review)
    if not candidates:
        return None
    candidates.sort(key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
    return candidates[-1]


def decide(head_sha: str, reviews: Iterable[dict], reviewers: Iterable[str]) -> Decision:
    """Decide whether a PR at ``head_sha`` may have its held runs approved."""
    review = select_summary_review(reviews, reviewers)
    if review is None:
        return Decision(False, "no autobot summary review")
    rid = review.get("id")
    url = review.get("html_url")
    body = review.get("body") or ""
    verdict = parse_summary(body)

    head = (head_sha or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return Decision(False, f"PR head SHA is not a full 40-hex SHA: {head_sha!r}", rid, url)

    fallback_note = ""
    if verdict.reviewed_heads:
        if len(set(verdict.reviewed_heads)) != 1:
            return Decision(False, "summary carries conflicting reviewed_head lines", rid, url)
        reviewed = verdict.reviewed_head or ""
    else:
        # Contract §2: consumers fall back to the review's commit_id only when the
        # reviewed_head line is absent.
        reviewed = (review.get("commit_id") or "").lower()
        fallback_note = " (reviewed_head absent; used review commit_id)"
        if not re.fullmatch(r"[0-9a-f]{40}", reviewed):
            return Decision(False, "no reviewed_head line and no usable review commit_id", rid, url)
    if reviewed != head:
        return Decision(False, f"stale verdict: reviewed_head {reviewed[:12]} != head {head[:12]}{fallback_note}", rid, url)

    if verdict.recommend_close:
        return Decision(False, "summary contains Recommend: Close", rid, url)
    if not verdict.table_verdicts:
        return Decision(False, "no CI safety row in summary table", rid, url)
    if any(v == "?" for v in verdict.table_verdicts):
        return Decision(False, "CI safety row verdict is not an exact Go/No-Go token", rid, url)
    if len(verdict.table_verdicts) > 1 and len(set(verdict.table_verdicts)) > 1:
        return Decision(False, "multiple CI safety rows disagree", rid, url)
    if verdict.table_verdicts[0] == NO_GO:
        why = f" — {verdict.table_rationale}" if verdict.table_rationale else ""
        return Decision(False, f"CI safety No-Go{why}", rid, url)
    if any(v != GO for v in verdict.meta_verdicts):
        return Decision(False, "ci_safety metadata disagrees with the Go table row", rid, url)
    return Decision(True, f"CI safety Go at head {head[:12]}{fallback_note}", rid, url)


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class RestClient:
    """Minimal GitHub REST client with a write guard.

    ``transport(method, url, body) -> (status, json)`` is injectable for tests.
    """

    def __init__(self, token: str, api_url: str = "https://api.github.com",
                 transport: Optional[Callable[[str, str, Optional[dict]], Tuple[int, object]]] = None,
                 allow_writes: bool = False):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.allow_writes = allow_writes
        self._transport = transport or self._http_transport
        self.calls: List[Tuple[str, str]] = []

    RETRIES = 3

    def _http_transport(self, method: str, url: str, body: Optional[dict]) -> Tuple[int, object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "fprime-actions-ci-gatekeeper",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        # Transient failures (dropped connections, 5xx) are retried; GETs are
        # idempotent and a retried approve is harmless (404/409 -> skipped).
        for attempt in range(1, self.RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                    return response.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw) if raw else None
                except ValueError:
                    payload = {"message": raw[:500]}
                if exc.code < 500 or attempt == self.RETRIES:
                    return exc.code, payload
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                if attempt == self.RETRIES:
                    raise GatekeeperError(f"{method} {url} failed after {attempt} attempts: {exc}") from exc
            time.sleep(2.0 * attempt)
        raise GatekeeperError(f"{method} {url}: retry loop exhausted")  # pragma: no cover

    def get(self, path: str, params: Optional[Dict[str, object]] = None) -> object:
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        self.calls.append(("GET", url))
        status, payload = self._transport("GET", url, None)
        if status >= 400:
            raise GatekeeperError(f"GET {path} -> HTTP {status}: {_message(payload)}")
        return payload

    def post(self, path: str, body: Optional[dict] = None) -> Tuple[int, object]:
        if not self.allow_writes:
            raise GatekeeperError("write attempted while writes are disabled (dry-run)")
        url = f"{self.api_url}{path}"
        self.calls.append(("POST", url))
        return self._transport("POST", url, body if body is not None else {})


def _message(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload)[:300]
    return str(payload)[:300]


# ---------------------------------------------------------------------------
# GitHub queries
# ---------------------------------------------------------------------------


def count_active_runs(client: RestClient, repo: str, own_run_id: Optional[int] = None,
                      own_repo: Optional[str] = None) -> int:
    """Number of queued + in-progress runs, minus this gatekeeper's own run."""
    total = 0
    for status in ("queued", "in_progress"):
        payload = client.get(f"/repos/{repo}/actions/runs", {"status": status, "per_page": 1})
        total += int((payload or {}).get("total_count") or 0)
    if own_run_id and own_repo and own_repo.lower() == repo.lower():
        total = max(0, total - 1)
    return total


def _paginate(client: RestClient, path: str, params: Dict[str, object], key: Optional[str]) -> List[dict]:
    items: List[dict] = []
    page = 1
    while True:
        payload = client.get(path, {**params, "per_page": 100, "page": page})
        batch = payload.get(key, []) if key else payload
        items.extend(batch or [])
        if not batch or len(batch) < 100 or page >= 50:
            return items
        page += 1


def list_open_prs(client: RestClient, repo: str) -> List[dict]:
    prs = _paginate(client, f"/repos/{repo}/pulls", {"state": "open", "sort": "created", "direction": "asc"}, None)
    return [p for p in prs if not p.get("draft")]


def held_runs_for_head(client: RestClient, repo: str, head_sha: str) -> List[dict]:
    return _paginate(client, f"/repos/{repo}/actions/runs",
                     {"head_sha": head_sha, "status": "action_required"}, "workflow_runs")


def list_reviews(client: RestClient, repo: str, number: int) -> List[dict]:
    return _paginate(client, f"/repos/{repo}/pulls/{number}/reviews", {}, None)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    number: int
    title: str
    url: str
    head_sha: str
    held_runs: List[dict]
    decision: Decision
    selected: bool = False
    approved: List[int] = field(default_factory=list)
    skipped: List[Tuple[int, str]] = field(default_factory=list)
    failed: List[Tuple[int, str]] = field(default_factory=list)


@dataclass
class Settings:
    repo: str
    busy_threshold: int = DEFAULT_BUSY_THRESHOLD
    reviewers: Tuple[str, ...] = DEFAULT_REVIEWERS
    max_approvals: int = DEFAULT_MAX_APPROVALS
    apply: bool = False
    approve_delay: float = 1.0
    own_run_id: Optional[int] = None
    own_repo: Optional[str] = None
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Report:
    queue_count: int
    busy_threshold: int
    busy: bool
    candidates: List[Candidate] = field(default_factory=list)
    scanned_prs: int = 0


def approve_run(client: RestClient, repo: str, run_id: int) -> Tuple[str, str]:
    """Approve one run.  Returns ("approved" | "skipped" | "failed", detail)."""
    status, payload = client.post(f"/repos/{repo}/actions/runs/{run_id}/approve")
    if status in (200, 201, 202, 204):
        return "approved", f"HTTP {status}"
    if status in (404, 409, 422):
        # Already approved / no longer pending / not an approvable run.
        return "skipped", f"HTTP {status}: {_message(payload)}"
    return "failed", f"HTTP {status}: {_message(payload)}"


def run(client: RestClient, settings: Settings, log: Callable[[str], None] = print) -> Report:
    count = count_active_runs(client, settings.repo, settings.own_run_id, settings.own_repo)
    busy = count >= settings.busy_threshold
    report = Report(count, settings.busy_threshold, busy)
    log(f"{settings.repo}: {count} queued/in-progress runs (threshold {settings.busy_threshold})")
    if busy:
        log("busy — no action")
        return report

    prs = list_open_prs(client, settings.repo)
    report.scanned_prs = len(prs)
    for pr in prs:
        head = ((pr.get("head") or {}).get("sha") or "")
        held = held_runs_for_head(client, settings.repo, head)
        if not held:
            continue
        reviews = list_reviews(client, settings.repo, int(pr["number"]))
        decision = decide(head, reviews, settings.reviewers)
        report.candidates.append(Candidate(
            number=int(pr["number"]),
            title=pr.get("title") or "",
            url=pr.get("html_url") or "",
            head_sha=head,
            held_runs=held,
            decision=decision,
        ))
    report.candidates.sort(key=lambda c: c.number)

    mode = "APPLY" if settings.apply else "DRY-RUN"
    chosen = [c for c in report.candidates if c.decision.eligible][: max(0, settings.max_approvals)]
    for cand in report.candidates:
        log(f"PR #{cand.number} ({len(cand.held_runs)} held): "
            f"{'ELIGIBLE' if cand.decision.eligible else 'not eligible'} — {cand.decision.reason}")
    for cand in chosen:
        cand.selected = True
        for held in cand.held_runs:
            run_id = int(held["id"])
            name = held.get("name") or held.get("display_title") or str(run_id)
            log(f"[{mode}] approve run {run_id} ({name}) for PR #{cand.number}")
            if not settings.apply:
                continue
            try:
                outcome, detail = approve_run(client, settings.repo, run_id)
            except GatekeeperError as exc:
                outcome, detail = "failed", str(exc)
            if outcome == "approved":
                cand.approved.append(run_id)
            elif outcome == "skipped":
                cand.skipped.append((run_id, detail))
            else:
                cand.failed.append((run_id, detail))
                log(f"::error::run {run_id}: {detail}")
            settings.sleep(settings.approve_delay)
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summary_markdown(report: Report, settings: Settings) -> str:
    mode = "apply (approvals enabled)" if settings.apply else "dry-run (no approvals)"
    lines = [f"## CI gatekeeper — {settings.repo}", "",
             f"Mode: **{mode}** — queue: {report.queue_count} queued/in-progress runs, "
             f"threshold {report.busy_threshold}"]
    if report.busy:
        lines += ["", f"**Busy ({report.queue_count} runs), no action.**"]
        return "\n".join(lines) + "\n"
    lines += ["", f"Scanned {report.scanned_prs} open non-draft PRs; "
                  f"{len(report.candidates)} with held runs."]
    if not report.candidates:
        lines += ["", "Nothing to do: no open PR has runs awaiting approval."]
        return "\n".join(lines) + "\n"
    lines += ["", "| PR | Held runs | Verdict | Reason | Action |", "|---|---:|---|---|---|"]
    for cand in report.candidates:
        verdict = "Go" if cand.decision.eligible else "—"
        reason = cand.decision.reason
        if cand.decision.review_url:
            reason += f" ([review]({cand.decision.review_url}))"
        if cand.selected:
            if not settings.apply:
                action = f"would approve {len(cand.held_runs)} run(s)"
            else:
                parts = [f"approved {len(cand.approved)}"]
                if cand.skipped:
                    parts.append(f"skipped {len(cand.skipped)}")
                if cand.failed:
                    parts.append(f"FAILED {len(cand.failed)}")
                action = ", ".join(parts)
        elif cand.decision.eligible:
            action = "eligible, waiting (max-approvals reached)"
        else:
            action = "none"
        link = f"[#{cand.number}]({cand.url})" if cand.url else f"#{cand.number}"
        lines.append(f"| {link} | {len(cand.held_runs)} | {verdict} | {reason} | {action} |")
    for cand in report.candidates:
        for run_id, detail in cand.failed:
            lines.append(f"\n- FAILED run {run_id} (PR #{cand.number}): {detail}")
    return "\n".join(lines) + "\n"


def write_outputs(report: Report) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    selected = [c for c in report.candidates if c.selected]
    values = {
        "busy": "true" if report.busy else "false",
        "queue-count": report.queue_count,
        "candidates": len(report.candidates),
        "eligible": sum(1 for c in report.candidates if c.decision.eligible),
        "selected-pr": selected[0].number if selected else "",
        "approved": sum(len(c.approved) for c in report.candidates),
        "failed": sum(len(c.failed) for c in report.candidates),
    }
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repository", required=True, help="owner/name of the repository to gate")
    parser.add_argument("--busy-threshold", type=int, default=DEFAULT_BUSY_THRESHOLD,
                        help="queued+in_progress runs at or above which the run exits without scanning")
    parser.add_argument("--reviewers", default=",".join(DEFAULT_REVIEWERS),
                        help="comma-separated allow-list of summary review authors")
    parser.add_argument("--max-approvals", type=int, default=DEFAULT_MAX_APPROVALS,
                        help="maximum number of PRs whose held runs are approved per run")
    parser.add_argument("--apply", action="store_true",
                        help="Approve runs.  Without this flag the run is a dry-run.")
    parser.add_argument("--approve-delay", type=float, default=1.0,
                        help="Seconds to sleep after each approve call")
    parser.add_argument("--own-run-id", type=int, default=_env_int("GITHUB_RUN_ID"),
                        help="This workflow run's id, excluded from the queue count")
    parser.add_argument("--own-repository", default=os.environ.get("GITHUB_REPOSITORY"),
                        help="Repository this workflow runs in (for the own-run exclusion)")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                        help="File to append the markdown summary to")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    return parser.parse_args(argv)


def _env_int(name: str) -> Optional[int]:
    value = os.environ.get(name)
    try:
        return int(value) if value else None
    except ValueError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("::error::GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    settings = Settings(
        repo=args.repository,
        busy_threshold=args.busy_threshold,
        reviewers=tuple(r.strip() for r in args.reviewers.split(",") if r.strip()),
        max_approvals=args.max_approvals,
        apply=args.apply,
        approve_delay=args.approve_delay,
        own_run_id=args.own_run_id,
        own_repo=args.own_repository,
    )
    client = RestClient(token, api_url=args.api_url, allow_writes=args.apply)
    try:
        report = run(client, settings)
    except GatekeeperError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    summary = summary_markdown(report, settings)
    print(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    write_outputs(report)
    return 1 if any(c.failed for c in report.candidates) else 0


if __name__ == "__main__":
    sys.exit(main())
