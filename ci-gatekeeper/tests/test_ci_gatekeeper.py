"""Unit tests for the ci-gatekeeper action's script.

Run with:  python3 ci-gatekeeper/tests/test_ci_gatekeeper.py

No external dependencies and no network: the REST API is replaced by an
in-memory fake.  Each test prints OK or FAIL and the runner exits 1 on any
failure.
"""

from __future__ import annotations

import sys
import traceback
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import ci_gatekeeper as gk  # noqa: E402

HEAD = "c08693850d2a556370947063d09b0a34b2863687"
OLD_HEAD = "fcdb1000bbe5d261974eb00c0f811e41fef8ae2b"

TABLE_HEADER = (
    "| Agent | must fix | suggestion | could fix | future work | outstanding | Verdict |\n"
    "|---|---:|---:|---:|---:|---:|---|\n"
    "| Security Vulnerabilities | 0 | 0 | 0 | 0 | 0 | Go |\n"
    "| Supply Chain / Runner Safety | 0 | 0 | 0 | 0 | 0 | Go |\n"
)


def summary_body(head=HEAD, ci_row="| **CI safety** | — | — | — | — | — | **Go** |",
                 totals="| **Totals** | 0 | 0 | 0 | 0 | 0 | **Go** |", extra_meta="",
                 recommend_close=False, marker=gk.SUMMARY_MARKER, reviewed_head_line=True):
    head_line = f"<!-- reviewed_head: {head} -->\n" if reviewed_head_line else ""
    close = ("### Recommend: Close\n**Recommend: Close** — prompt-injection attempt.\n\n"
             if recommend_close else "")
    rows = (ci_row + "\n") if ci_row else ""
    return (
        f"{marker}\n{head_line}<!-- run: 2 -->\n<!-- maintainers_requested: none -->\n{extra_meta}"
        f"## Automated review summary  (run 2)\n\n{close}### Per-agent results\n\n"
        f"{TABLE_HEADER}{rows}{totals}\n\n### Merge readiness\n**Merge readiness: Go**\n\n---\n\n"
        "Engage.\n"
    )


def review(body, login="lestarch-autobot", state="APPROVED", submitted="2026-10-23T16:51:51Z",
           commit_id=HEAD, rid=1):
    return {"id": rid, "user": {"login": login}, "state": state, "body": body,
            "submitted_at": submitted, "commit_id": commit_id,
            "html_url": f"https://github.com/nasa/fprime/pull/1#pullrequestreview-{rid}"}


def decide(reviews, head=HEAD, reviewers=gk.DEFAULT_REVIEWERS):
    return gk.decide(head, reviews, reviewers)


# ---------------------------------------------------------------------------
# Parser / decision fixtures
# ---------------------------------------------------------------------------


def test_exact_go_is_eligible():
    d = decide([review(summary_body())])
    assert d.eligible, d.reason
    assert d.review_id == 1 and d.review_url


def test_no_go_is_not_eligible():
    body = summary_body(ci_row="| **CI safety** | — | — | — | — | — | **No-Go** — supply-chain has 1 must-fix in workflows |")
    d = decide([review(body)])
    assert not d.eligible and "No-Go" in d.reason and "supply-chain" in d.reason, d.reason


def test_no_go_whose_rationale_contains_go():
    body = summary_body(ci_row="| **CI safety** | — | — | — | — | — | **No-Go** — security said Go but supply-chain found **Go**-style injection |")
    d = decide([review(body)])
    assert not d.eligible and d.reason.startswith("CI safety No-Go"), d.reason


def test_lowercase_go_is_not_eligible():
    for cell in ("**go**", "**GO**", "Go", "**Go** — fine", "**Go**/**Go**", "**Go-ish**"):
        body = summary_body(ci_row=f"| **CI safety** | — | — | — | — | — | {cell} |")
        d = decide([review(body)])
        assert not d.eligible, (cell, d.reason)
        assert "not an exact Go/No-Go token" in d.reason, (cell, d.reason)


def test_missing_ci_safety_row():
    d = decide([review(summary_body(ci_row=""))])
    assert not d.eligible and "no CI safety row" in d.reason, d.reason


def test_totals_go_row_does_not_count_as_ci_safety():
    # Only the CI safety row carries the verdict; a Go in Totals is irrelevant.
    d = decide([review(summary_body(ci_row="", totals="| **Totals** | 0 | 0 | 0 | 0 | 0 | **Go** |"))])
    assert not d.eligible and "no CI safety row" in d.reason, d.reason


def test_missing_summary_marker():
    d = decide([review(summary_body(marker="<!-- fprime-agent: security-review v1 -->"))])
    assert not d.eligible and d.reason == "no autobot summary review", d.reason


def test_stale_reviewed_head():
    d = decide([review(summary_body(head=OLD_HEAD), commit_id=HEAD)])
    assert not d.eligible and d.reason.startswith("stale verdict"), d.reason


def test_short_reviewed_head_never_matches():
    d = decide([review(summary_body(head=HEAD[:12]), commit_id=OLD_HEAD)])
    assert not d.eligible, d.reason
    # The abbreviated line is not a reviewed_head line at all, so the contract
    # fallback to commit_id applies and that commit_id is stale.
    assert "stale verdict" in d.reason and "commit_id" in d.reason, d.reason


def test_reviewed_head_case_insensitive_match():
    d = decide([review(summary_body(head=HEAD.upper()))], head=HEAD)
    assert d.eligible, d.reason


def test_wrong_author_is_ignored():
    d = decide([review(summary_body(), login="giannaped-ff")])
    assert not d.eligible and d.reason == "no autobot summary review", d.reason
    d = decide([review(summary_body(), login="lestarch-autobot-impostor")])
    assert not d.eligible, d.reason
    d = decide([review(summary_body(), login="thomas-bc-autowot")])
    assert d.eligible, d.reason
    d = decide([review(summary_body(), login="thomas-bc-autowot")], reviewers=("lestarch-autobot",))
    assert not d.eligible, d.reason


def test_recommend_close_blocks():
    d = decide([review(summary_body(recommend_close=True))])
    assert not d.eligible and "Recommend: Close" in d.reason, d.reason


def test_ci_safety_metadata_disagreeing_with_table():
    body = summary_body(extra_meta="<!-- ci_safety: No-Go -->\n<!-- ci_safety_rationale: 1 must-fix -->\n")
    d = decide([review(body)])
    assert not d.eligible and "metadata disagrees" in d.reason, d.reason
    body = summary_body(extra_meta="<!-- ci_safety: go -->\n")
    assert not decide([review(body)]).eligible
    body = summary_body(extra_meta="<!-- ci_safety: Go -->\n<!-- ci_safety_rationale: clean -->\n")
    assert decide([review(body)]).eligible
    # ci_safety_rationale alone must not be mistaken for a verdict line.
    body = summary_body(extra_meta="<!-- ci_safety_rationale: nothing to Go on -->\n")
    assert decide([review(body)]).eligible


def test_multiple_ci_safety_rows_must_agree():
    body = summary_body(ci_row="| **CI safety** | — | — | — | — | — | **Go** |\n"
                               "| **CI safety** | — | — | — | — | — | **No-Go** — later row |")
    d = decide([review(body)])
    assert not d.eligible and "disagree" in d.reason, d.reason
    body = summary_body(ci_row="| **CI safety** | — | — | — | — | — | **Go** |\n"
                               "| **CI safety** | — | — | — | — | — | **Go** |")
    assert decide([review(body)]).eligible


def test_latest_summary_review_wins():
    old = review(summary_body(head=OLD_HEAD, ci_row="| **CI safety** | — | — | — | — | — | **No-Go** — x |"),
                 state="CHANGES_REQUESTED", submitted="2026-10-15T23:46:05Z", commit_id=OLD_HEAD, rid=1)
    new = review(summary_body(), submitted="2026-10-23T16:51:51Z", rid=2)
    d = decide([new, old])
    assert d.eligible and d.review_id == 2, d.reason
    # Newest review stale -> not eligible even though an older one says Go for this head.
    newer_stale = review(summary_body(head=OLD_HEAD), submitted="2026-10-24T00:00:00Z", rid=3)
    d = decide([new, old, newer_stale])
    assert not d.eligible and d.review_id == 3, d.reason


def test_dismissed_summary_review_is_ignored():
    d = decide([review(summary_body(), state="DISMISSED")])
    assert not d.eligible and d.reason == "no autobot summary review", d.reason


def test_missing_reviewed_head_falls_back_to_commit_id():
    d = decide([review(summary_body(reviewed_head_line=False), commit_id=HEAD)])
    assert d.eligible and "commit_id" in d.reason, d.reason
    d = decide([review(summary_body(reviewed_head_line=False), commit_id=OLD_HEAD)])
    assert not d.eligible and "stale" in d.reason, d.reason
    d = decide([review(summary_body(reviewed_head_line=False), commit_id=None)])
    assert not d.eligible and "no usable review commit_id" in d.reason, d.reason


def test_conflicting_reviewed_head_lines():
    body = summary_body().replace("<!-- run: 2 -->", f"<!-- reviewed_head: {OLD_HEAD} -->\n<!-- run: 2 -->")
    d = decide([review(body)])
    assert not d.eligible and "conflicting reviewed_head" in d.reason, d.reason


def test_row_regex_tolerates_whitespace_and_ascii_dashes():
    body = summary_body(ci_row="|  **CI safety**  | - | - | - | - | - |  **Go**  |  ")
    assert decide([review(body)]).eligible
    body = summary_body(ci_row="| **CI safety** | — | — | — | — | **Go** |")   # one cell short
    d = decide([review(body)])
    assert not d.eligible and "no CI safety row" in d.reason, d.reason


def test_crlf_line_endings_as_returned_by_the_api():
    body = summary_body().replace("\n", "\r\n")
    d = decide([review(body)])
    assert d.eligible and "commit_id" not in d.reason, d.reason
    body = summary_body(head=OLD_HEAD).replace("\n", "\r\n")
    assert "stale verdict" in decide([review(body)]).reason


def test_real_world_summary_body_shape():
    body = (
        "<!-- fprime-review-summary v1 -->\n"
        f"<!-- reviewed_head: {HEAD} -->\n<!-- run: 2 -->\n<!-- maintainers_requested: none -->\n"
        "## Automated review summary  (run 2)\n\n### Per-agent results\n\n"
        "| Agent | must fix | suggestion | could fix | future work | outstanding | Verdict |\n"
        "|---|---:|---:|---:|---:|---:|---|\n"
        "| Security Vulnerabilities | 1 | 1 | 1 | 0 | 0 | Go |\n"
        "| Maintainability | 0 | 1 | 3 | 0 | 1 | Go |\n"
        "| **CI safety** | — | — | — | — | — | **Go** |\n"
        "| **Totals** | 3 | 11 | 5 | 0 | 5 | **Go** |\n\n"
        "Per-lens rows are each lens's own cumulative metadata.\n\n"
        "<details>\n<summary>Since last run</summary>\n\n| Agent | resolved |\n|---|---:|\n| Security | 3 |\n\n</details>\n\n"
        "### Merge readiness\n**Merge readiness: Go** — no outstanding must-fix items.\n\n---\n\n"
        "Steady as she goes.\n"
    )
    assert decide([review(body)]).eligible


# ---------------------------------------------------------------------------
# Fake REST API for the orchestration tests
# ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, queued=0, in_progress=0, prs=(), held=None, reviews=None,
                 approve_status=201):
        self.queued = queued
        self.in_progress = in_progress
        self.prs = list(prs)
        self.held = held or {}            # head sha -> list of run dicts
        self.reviews = reviews or {}      # pr number -> list of reviews
        self.approve_status = approve_status
        self.requests = []
        self.approved = []

    def __call__(self, method, url, body):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path
        self.requests.append((method, path, params))
        if method == "POST" and path.endswith("/approve"):
            run_id = int(path.split("/")[-2])
            self.approved.append(run_id)
            status = self.approve_status
            if isinstance(status, dict):
                status = status.get(run_id, 201)
            return status, ({} if status < 400 else {"message": "nope"})
        if path.endswith("/actions/runs"):
            status = params.get("status")
            if status == "queued":
                return 200, {"total_count": self.queued, "workflow_runs": []}
            if status == "in_progress":
                return 200, {"total_count": self.in_progress, "workflow_runs": []}
            if status == "action_required":
                runs = self.held.get(params.get("head_sha"), [])
                return 200, {"total_count": len(runs), "workflow_runs": runs}
        if path.endswith("/pulls"):
            return 200, self.prs if params.get("page", "1") == "1" else []
        if path.endswith("/reviews"):
            number = int(path.split("/")[-2])
            return 200, self.reviews.get(number, []) if params.get("page", "1") == "1" else []
        return 404, {"message": f"unhandled {method} {path}"}


def pr(number, head, draft=False):
    return {"number": number, "title": f"PR {number}", "draft": draft,
            "html_url": f"https://github.com/nasa/fprime/pull/{number}", "head": {"sha": head}}


def held_run(run_id, name="CI"):
    return {"id": run_id, "name": name, "status": "action_required"}


def settings(apply=False, **kw):
    base = dict(repo="nasa/fprime", apply=apply, approve_delay=0, sleep=lambda _s: None)
    base.update(kw)
    return gk.Settings(**base)


def client(fake, apply=False):
    return gk.RestClient("token", transport=fake, allow_writes=apply)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def fixture_fake(queued=0, in_progress=0, approve_status=201):
    return FakeGitHub(
        queued=queued, in_progress=in_progress,
        prs=[pr(6180, SHA_A), pr(6195, SHA_B), pr(6220, SHA_C), pr(6226, SHA_D), pr(6230, "e" * 40, draft=True)],
        held={SHA_A: [held_run(11), held_run(12)], SHA_B: [held_run(21)], SHA_C: [held_run(31)],
              SHA_D: [held_run(41)], "e" * 40: [held_run(51)]},
        reviews={
            6180: [review(summary_body(head=SHA_A, ci_row="| **CI safety** | — | — | — | — | — | **No-Go** — security has 1 must-fix |"), rid=1)],
            6195: [review(summary_body(head=SHA_B), rid=2)],
            6220: [review(summary_body(head=SHA_C), login="thomas-bc-autowot", rid=3)],
            6226: [],
        },
        approve_status=approve_status,
    )


def test_busy_queue_short_circuits_without_listing_prs():
    fake = fixture_fake(queued=4, in_progress=6)
    report = gk.run(client(fake), settings(busy_threshold=10), log=lambda _m: None)
    assert report.busy and report.queue_count == 10 and report.candidates == []
    paths = [p for _m, p, _q in fake.requests]
    assert all(p.endswith("/actions/runs") for p in paths), paths
    assert not any("/pulls" in p for p in paths), paths
    assert "Busy (10 runs), no action" in gk.summary_markdown(report, settings(busy_threshold=10))


def test_own_run_is_excluded_from_queue_count():
    fake = fixture_fake(queued=4, in_progress=6)
    s = settings(busy_threshold=10, own_run_id=99, own_repo="nasa/fprime")
    report = gk.run(client(fake), s, log=lambda _m: None)
    assert not report.busy and report.queue_count == 9, report
    # The exclusion only applies when the gatekeeper runs in the gated repository.
    fake = fixture_fake(queued=4, in_progress=6)
    s = settings(busy_threshold=10, own_run_id=99, own_repo="someone/else")
    assert gk.run(client(fake), s, log=lambda _m: None).busy


def test_calm_queue_dry_run_selects_oldest_eligible_and_writes_nothing():
    fake = fixture_fake(queued=2, in_progress=3)
    s = settings(busy_threshold=10)
    report = gk.run(client(fake), s, log=lambda _m: None)
    assert not report.busy
    by_number = {c.number: c for c in report.candidates}
    assert sorted(by_number) == [6180, 6195, 6220, 6226], sorted(by_number)   # draft PR skipped
    assert not by_number[6180].decision.eligible and "No-Go" in by_number[6180].decision.reason
    assert by_number[6195].decision.eligible and by_number[6195].selected
    assert by_number[6220].decision.eligible and not by_number[6220].selected
    assert not by_number[6226].decision.eligible and by_number[6226].decision.reason == "no autobot summary review"
    assert fake.approved == [] and not any(m == "POST" for m, _p, _q in fake.requests)
    summary = gk.summary_markdown(report, s)
    assert "would approve 1 run(s)" in summary and "eligible, waiting" in summary
    assert "| [#6226](https://github.com/nasa/fprime/pull/6226) | 1 | — | no autobot summary review | none |" in summary


def test_apply_approves_only_the_selected_pr_runs():
    fake = fixture_fake()
    s = settings(apply=True, busy_threshold=10)
    report = gk.run(client(fake, apply=True), s, log=lambda _m: None)
    assert fake.approved == [21], fake.approved
    chosen = [c for c in report.candidates if c.selected]
    assert [c.number for c in chosen] == [6195] and chosen[0].approved == [21]
    assert "approved 1" in gk.summary_markdown(report, s)


def test_max_approvals_two_takes_two_oldest_eligible():
    fake = fixture_fake()
    s = settings(apply=True, busy_threshold=10, max_approvals=2)
    report = gk.run(client(fake, apply=True), s, log=lambda _m: None)
    assert fake.approved == [21, 31], fake.approved
    assert [c.number for c in report.candidates if c.selected] == [6195, 6220]


def test_dry_run_client_refuses_writes():
    try:
        client(fixture_fake(), apply=False).post("/repos/nasa/fprime/actions/runs/1/approve")
    except gk.GatekeeperError as exc:
        assert "dry-run" in str(exc)
    else:
        raise AssertionError("write guard did not fire")


def test_already_approved_runs_are_skipped_not_failed():
    fake = fixture_fake(approve_status={21: 409})
    s = settings(apply=True, max_approvals=1)
    report = gk.run(client(fake, apply=True), s, log=lambda _m: None)
    chosen = [c for c in report.candidates if c.selected][0]
    assert chosen.skipped and not chosen.failed and not chosen.approved
    fake = fixture_fake(approve_status={21: 403})
    report = gk.run(client(fake, apply=True), s, log=lambda _m: None)
    chosen = [c for c in report.candidates if c.selected][0]
    assert chosen.failed and "FAILED" in gk.summary_markdown(report, s)


def test_approve_calls_are_paced():
    fake = fixture_fake()
    slept = []
    # Make 6180 eligible so two runs are approved and the delay is observable.
    fake.reviews[6180] = [review(summary_body(head=SHA_A), rid=9)]
    s = settings(apply=True, approve_delay=1.5, sleep=slept.append)
    gk.run(client(fake, apply=True), s, log=lambda _m: None)
    assert fake.approved == [11, 12] and slept == [1.5, 1.5], (fake.approved, slept)


def test_prs_without_held_runs_are_not_reviewed():
    fake = fixture_fake()
    fake.held = {SHA_B: [held_run(21)]}
    gk.run(client(fake), settings(), log=lambda _m: None)
    review_paths = [p for _m, p, _q in fake.requests if p.endswith("/reviews")]
    assert review_paths == ["/repos/nasa/fprime/pulls/6195/reviews"], review_paths


def test_summary_markdown_when_nothing_held():
    fake = fixture_fake()
    fake.held = {}
    s = settings()
    report = gk.run(client(fake), s, log=lambda _m: None)
    assert "Nothing to do" in gk.summary_markdown(report, s)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    failures = 0
    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for test in tests:
        try:
            test()
            print(f"OK    {test.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
