"""Unit tests for the ccb-resolver action's script.

Run with:  python3 ccb-resolver/tests/test_ccb_resolver.py

No external dependencies.  Each test prints OK or FAIL and the runner
exits 1 on any failure.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import ccb_resolver as ccb  # noqa: E402

APPROVED_STATUS = "Backlog - Deferred"
DONE = "Done"


def _item(status="CCB", resolution=None, content="Issue", state="OPEN", number=1):
    return ccb.Item(
        item_id=f"PVTI_{number}",
        status=status,
        resolution=resolution,
        content_type=content,
        content_id=f"I_{number}" if content != "DraftIssue" else None,
        number=number if content != "DraftIssue" else None,
        title=f"Item {number}",
        url=("" if content == "DraftIssue"
             else f"https://github.com/nasa/fprime/issues/{number}"),
        repository="nasa/fprime" if content != "DraftIssue" else "",
        state=state if content != "DraftIssue" else None,
    )


def _decide(item):
    return ccb.decide(item, APPROVED_STATUS, DONE)


# ---------------------------------------------------------------------------
# Decision table
# ---------------------------------------------------------------------------


def test_non_ccb_status_is_never_touched():
    for status in (None, "Backlog - Deferred", "Backlog - Resourced", "Assigned",
                   "In Progress", "Review", "Done"):
        for resolution in (None, "Approved", "Disapproved", "Tabled", "Needs Study"):
            item = _item(status=status, resolution=resolution)
            assert _decide(item) is None, (status, resolution)


def test_tabled_needs_study_blank_do_nothing():
    for resolution in (None, "Tabled", "Needs Study", "Something new"):
        assert _decide(_item(resolution=resolution)) is None, resolution


def test_approved_sets_status():
    action = _decide(_item(resolution="Approved"))
    assert action.kind == "set-status"
    assert action.new_status == APPROVED_STATUS
    # Approved applies regardless of content type/state.
    closed_pr = _item(resolution="Approved", content="PullRequest", state="CLOSED")
    assert _decide(closed_pr).kind == "set-status"
    assert _decide(_item(resolution="Approved", content="DraftIssue")).kind == "set-status"


def test_disapproved_open_issue_comments_and_closes():
    action = _decide(_item(resolution="Disapproved"))
    assert action.kind == "comment-and-close"
    assert action.new_status is None


def test_disapproved_closed_content_sets_done():
    action = _decide(_item(resolution="Disapproved", state="CLOSED"))
    assert action.kind == "set-status" and action.new_status == DONE
    action = _decide(_item(resolution="Disapproved", content="PullRequest", state="CLOSED"))
    assert action.kind == "set-status" and action.new_status == DONE


def test_blank_status_open_issue_is_taken_into_ccb():
    action = ccb.decide(_item(status=None), APPROVED_STATUS, DONE, intake_status="CCB")
    assert action.kind == "set-status" and action.new_status == "CCB"
    # Any resolution value is irrelevant when there is no Status yet.
    for resolution in ("Approved", "Disapproved", "Tabled"):
        action = ccb.decide(_item(status=None, resolution=resolution), APPROVED_STATUS, DONE,
                            intake_status="CCB")
        assert action.kind == "set-status" and action.new_status == "CCB"


def test_blank_status_other_content_is_left_alone():
    for content, state in (("Issue", "CLOSED"), ("PullRequest", "OPEN"),
                           ("PullRequest", "CLOSED"), ("DraftIssue", None), ("", None)):
        item = _item(status=None, content=content, state=state)
        assert ccb.decide(item, APPROVED_STATUS, DONE, intake_status="CCB") is None, content


def test_blank_status_rule_can_be_disabled():
    for intake in (None, ""):
        assert ccb.decide(_item(status=None), APPROVED_STATUS, DONE, intake_status=intake) is None
    assert _decide(_item(status=None)) is None  # pure function defaults to off


def test_disapproved_open_pr_or_draft_is_skipped():
    assert _decide(_item(resolution="Disapproved", content="PullRequest")).kind == "skip"
    assert _decide(_item(resolution="Disapproved", content="DraftIssue")).kind == "skip"


# ---------------------------------------------------------------------------
# Fake GraphQL API
# ---------------------------------------------------------------------------


class FakeGitHub:
    """In-memory stand-in for the GitHub GraphQL endpoint."""

    STATUS_OPTIONS = ["CCB", "Backlog - Deferred", "Backlog - Resourced", "Assigned",
                      "In Progress", "Review", "Done"]
    RESOLUTION_OPTIONS = ["Approved", "Tabled", "Needs Study", "Disapproved"]

    def __init__(self, items, page_size=2, owner_type="organization"):
        self.items = items  # list of dicts: id, status, resolution, content
        self.page_size = page_size
        self.owner_type = owner_type
        self.mutations = []
        self.comments = []
        self.closed = []
        self.fields = {
            "Status": {"id": "F_STATUS", "options": self.STATUS_OPTIONS},
            "CCB Resolution": {"id": "F_RES", "options": self.RESOLUTION_OPTIONS},
        }

    @staticmethod
    def _opt_id(field, name):
        return f"{field}:{name}"

    def __call__(self, payload):
        query = payload["query"]
        variables = payload["variables"]
        if query.lstrip().startswith("mutation"):
            return self._mutate(query, variables)
        if "projectV2(number" in query:
            return self._project(query, variables)
        if "items(first: 100" in query:
            return self._items(variables)
        raise AssertionError("unexpected query")

    def _project(self, query, variables):
        requested = "organization" if "organization(login" in query else "user"
        if requested != self.owner_type:
            return {"data": {requested: None},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
        nodes = [{"id": "F_TITLE"}]
        for name, spec in self.fields.items():
            nodes.append({"id": spec["id"], "name": name,
                          "options": [{"id": self._opt_id(spec["id"], o), "name": o}
                                      for o in spec["options"]]})
        return {"data": {requested: {"projectV2": {
            "id": "PROJECT", "title": "Fake board", "fields": {"nodes": nodes}}}}}

    def _items(self, variables):
        start = int(variables["cursor"] or 0)
        page = self.items[start:start + self.page_size]
        nodes = []
        for item in page:
            values = []
            if item.get("status"):
                values.append({"name": item["status"], "field": {"name": "Status"}})
            if item.get("resolution"):
                values.append({"name": item["resolution"], "field": {"name": "CCB Resolution"}})
            values.append({})  # non-single-select value renders as an empty object
            nodes.append({"id": item["id"], "type": "ISSUE",
                          "fieldValues": {"nodes": values}, "content": item.get("content")})
        end = start + len(page)
        return {"data": {"node": {"items": {
            "pageInfo": {"hasNextPage": end < len(self.items), "endCursor": str(end)},
            "nodes": nodes}}}}

    def _mutate(self, query, variables):
        self.mutations.append((query, variables))
        if "updateProjectV2ItemFieldValue" in query:
            assert variables["project"] == "PROJECT"
            assert variables["field"] == "F_STATUS"
            name = variables["option"].split(":", 1)[1]
            for item in self.items:
                if item["id"] == variables["item"]:
                    item["status"] = name
            return {"data": {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": variables["item"]}}}}
        if "addComment" in query:
            self.comments.append((variables["subject"], variables["body"]))
            return {"data": {"addComment": {"clientMutationId": None}}}
        if "closeIssue" in query:
            self.closed.append(variables["issue"])
            for item in self.items:
                content = item.get("content") or {}
                if content.get("id") == variables["issue"]:
                    content["state"] = "CLOSED"
            return {"data": {"closeIssue": {
                "issue": {"id": variables["issue"], "state": "CLOSED"}}}}
        raise AssertionError("unexpected mutation")


def _issue(number, state="OPEN"):
    return {"__typename": "Issue", "id": f"I_{number}", "number": number,
            "title": f"Issue {number}", "state": state,
            "url": f"https://github.com/nasa/fprime/issues/{number}",
            "repository": {"nameWithOwner": "nasa/fprime"}}


def _board():
    return [
        {"id": "P1", "status": "CCB", "resolution": "Approved", "content": _issue(1)},
        {"id": "P2", "status": "CCB", "resolution": "Disapproved", "content": _issue(2)},
        {"id": "P3", "status": "CCB", "resolution": "Tabled", "content": _issue(3)},
        {"id": "P4", "status": "Done", "resolution": "Approved", "content": _issue(4, "CLOSED")},
        {"id": "P5", "status": "CCB", "resolution": None, "content": _issue(5)},
        {"id": "P6", "status": "CCB", "resolution": "Disapproved", "content": _issue(6, "CLOSED")},
        {"id": "P7", "status": "CCB", "resolution": "Disapproved",
         "content": {"__typename": "DraftIssue", "title": "Draft"}},
        {"id": "P8", "status": "Backlog - Resourced", "resolution": None, "content": _issue(8)},
        {"id": "P9", "status": None, "resolution": None, "content": _issue(9)},
        {"id": "P10", "status": None, "resolution": None, "content": _issue(10, "CLOSED")},
        {"id": "P11", "status": None, "resolution": None,
         "content": dict(_issue(11), __typename="PullRequest")},
    ]


def _settings(apply):
    return ccb.Settings(apply=apply, mutation_delay=0, sleep=lambda _s: None)


def _client(fake, apply):
    return ccb.GraphQLClient("token", transport=fake, allow_writes=apply)


def test_dry_run_sends_no_mutations_and_paginates():
    fake = FakeGitHub(_board(), page_size=3)
    report = ccb.run(_client(fake, apply=False), "nasa", 21, _settings(False), log=lambda _m: None)
    assert report.total_items == 11 and report.ccb_items == 6 and report.blank_items == 1
    assert fake.mutations == []
    kinds = {a.item.item_id: a.kind for a in report.actions}
    assert kinds == {"P1": "set-status", "P2": "comment-and-close",
                     "P6": "set-status", "P7": "skip", "P9": "set-status"}
    assert not any(a.applied for a in report.actions)
    summary = ccb.summary_markdown(report, "nasa", 21, _settings(False))
    assert "dry-run" in summary and "would apply" in summary and "skipped" in summary
    assert "11 items scanned, 6 in `CCB`, 1 open issues with no Status, 5 actionable" in summary


def test_apply_writes_expected_mutations_and_is_idempotent():
    fake = FakeGitHub(_board(), page_size=100)
    report = ccb.run(_client(fake, apply=True), "nasa", 21, _settings(True), log=lambda _m: None)
    applied = sorted(a.item.item_id for a in report.actions if a.applied)
    assert applied == ["P1", "P2", "P6", "P9"]
    # P1 -> Backlog - Deferred, P6 -> Done, P2 comment + close, P9 (blank) -> CCB.
    status_updates = {v["item"]: v["option"] for q, v in fake.mutations
                      if "updateProjectV2ItemFieldValue" in q}
    assert status_updates == {"P1": "F_STATUS:Backlog - Deferred", "P6": "F_STATUS:Done",
                              "P9": "F_STATUS:CCB"}
    assert fake.comments == [("I_2", ccb.DEFAULT_DISAPPROVAL_COMMENT)]
    assert fake.closed == ["I_2"]
    assert "stateReason: NOT_PLANNED" in [q for q, _ in fake.mutations if "closeIssue" in q][0]
    # Items with non-CCB status, the draft, and the blank closed issue / PR
    # were not written.
    assert not any(v.get("item") in ("P3", "P4", "P5", "P7", "P8", "P10", "P11")
                   for _, v in fake.mutations)
    summary = ccb.summary_markdown(report, "nasa", 21, _settings(True))
    assert summary.count("| applied |") == 4

    # Second run: P1/P6 have moved; P9 is now a plain CCB item with no
    # resolution; P2's issue is closed (board workflow has not run yet) so it
    # is now the "set Done" case; P7 is still skipped.
    fake.mutations.clear()
    report = ccb.run(_client(fake, apply=True), "nasa", 21, _settings(True), log=lambda _m: None)
    assert [(a.item.item_id, a.kind) for a in report.actions] == [
        ("P2", "set-status"), ("P7", "skip")]
    assert len(fake.mutations) == 1 and fake.mutations[0][1]["option"] == "F_STATUS:Done"

    # Third run: nothing left but the manual item; no writes.
    fake.mutations.clear()
    report = ccb.run(_client(fake, apply=True), "nasa", 21, _settings(True), log=lambda _m: None)
    assert [a.kind for a in report.actions] == ["skip"]
    assert fake.mutations == []


def test_write_guard_blocks_mutations_in_dry_run():
    fake = FakeGitHub(_board())
    client = _client(fake, apply=False)
    try:
        client.mutate(ccb.SET_STATUS_MUTATION, {})
    except ccb.CcbResolverError as exc:
        assert "dry-run" in str(exc)
    else:
        raise AssertionError("mutation was sent during dry-run")
    assert fake.mutations == []


def test_forbidden_mutations_rejected_even_when_writes_enabled():
    fake = FakeGitHub(_board())
    client = _client(fake, apply=True)
    for name in ccb.FORBIDDEN_MUTATIONS:
        doc = "mutation { %s(input: {}) { clientMutationId } }" % name
        try:
            client.mutate(doc, {})
        except ccb.CcbResolverError as exc:
            assert "forbidden" in str(exc) or "allow-list" in str(exc)
        else:
            raise AssertionError(f"{name} was sent")
    try:
        client.query(ccb.SET_STATUS_MUTATION, {})
    except ccb.CcbResolverError:
        pass
    else:
        raise AssertionError("query() sent a mutation")
    assert fake.mutations == []


def test_shipped_mutations_only_touch_items_never_fields():
    for doc in (ccb.SET_STATUS_MUTATION, ccb.COMMENT_MUTATION, ccb.CLOSE_MUTATION):
        for name in ccb.FORBIDDEN_MUTATIONS:
            assert not ccb._mentions(doc, name), (name, doc)
    assert "updateProjectV2ItemFieldValue" in ccb.SET_STATUS_MUTATION


def test_user_owned_project_is_found():
    fake = FakeGitHub(_board(), owner_type="user")
    report = ccb.run(_client(fake, apply=False), "lestarch-autobot", 1, _settings(False),
                     log=lambda _m: None)
    assert report.total_items == 11


def test_missing_field_or_option_fails_before_any_write():
    fake = FakeGitHub(_board())
    del fake.fields["CCB Resolution"]
    try:
        ccb.run(_client(fake, apply=True), "nasa", 21, _settings(True), log=lambda _m: None)
    except ccb.CcbResolverError as exc:
        assert "CCB Resolution" in str(exc)
    else:
        raise AssertionError("missing field not detected")
    fake = FakeGitHub(_board())
    settings = _settings(True)
    settings.approved_status = "Backlog - Nope"
    try:
        ccb.run(_client(fake, apply=True), "nasa", 21, settings, log=lambda _m: None)
    except ccb.CcbResolverError as exc:
        assert "Backlog - Nope" in str(exc)
    else:
        raise AssertionError("missing option not detected")
    assert fake.mutations == []


def test_intake_disabled_leaves_blank_items_alone():
    fake = FakeGitHub(_board(), page_size=100)
    settings = _settings(True)
    settings.intake_status = None
    report = ccb.run(_client(fake, apply=True), "nasa", 21, settings, log=lambda _m: None)
    assert report.blank_items == 1
    assert not any(a.item.item_id == "P9" for a in report.actions)
    assert not any(v.get("item") == "P9" for _, v in fake.mutations)


def test_cli_empty_intake_status_disables_rule():
    assert ccb.parse_args(["--owner", "nasa", "--number", "21"]).intake_status == "CCB"
    assert ccb.parse_args(["--owner", "nasa", "--number", "21", "--intake-status", ""]
                          ).intake_status == ""


def test_failed_mutation_is_reported_and_others_continue():
    fake = FakeGitHub(_board(), page_size=100)
    original = fake._mutate

    def flaky(query, variables):
        if variables.get("item") == "P1":
            return {"errors": [{"message": "boom"}]}
        return original(query, variables)

    fake._mutate = flaky
    report = ccb.run(_client(fake, apply=True), "nasa", 21, _settings(True), log=lambda _m: None)
    by_id = {a.item.item_id: a for a in report.actions}
    assert by_id["P1"].error and "boom" in by_id["P1"].error and not by_id["P1"].applied
    assert by_id["P2"].applied and by_id["P6"].applied
    summary = ccb.summary_markdown(report, "nasa", 21, _settings(True))
    assert "FAILED: " in summary


def test_parse_item_handles_missing_content():
    item = ccb.parse_item({"id": "X", "fieldValues": {"nodes": [{}]}, "content": None})
    assert item.status is None and item.content_type == "" and item.label == "item"


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
