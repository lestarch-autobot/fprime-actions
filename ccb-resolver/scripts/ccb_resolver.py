"""Resolve CCB decisions recorded on a GitHub Projects (v2) board.

For every item whose ``Status`` single-select is ``CCB`` the script applies
the Change Control Board decision recorded in the ``CCB Resolution`` field:

``Approved``
    Set ``Status`` to the approved status (default ``Backlog - Deferred``).
``Disapproved``
    If the item is an open issue: post the disapproval comment and close the
    issue as *not planned*.  The board's built-in "closed -> Done" workflow is
    expected to move the item afterwards; if the item is still ``CCB`` on a
    later run (content already closed) its ``Status`` is set to ``Done``.
``Tabled`` / ``Needs Study`` / blank
    No action.

Items whose ``Status`` is anything other than ``CCB`` are never touched.

Safety properties:

* The only project mutation ever issued is the per-item
  ``updateProjectV2ItemFieldValue``.  Field and option *definitions* are
  resolved by name at runtime and are never created, modified, or deleted.
* Dry-run is the default.  Mutations are only sent when ``--apply`` is given
  (``dry-run: false`` on the action); the client refuses to send any mutation
  otherwise.
* Idempotent: a re-run after a successful run finds nothing to do.
* The whole board is paginated (100 items per page) and a small delay is
  inserted between mutations to respect secondary rate limits.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


STATUS_FIELD = "Status"
RESOLUTION_FIELD = "CCB Resolution"
CCB_STATUS = "CCB"
APPROVED = "Approved"
DISAPPROVED = "Disapproved"

DEFAULT_DISAPPROVAL_COMMENT = (
    "Our CCB team thanks you for this contribution, but we believe that this "
    "is outside the technical scope of the F Prime project and believe that "
    "this change is better contributed in a user's project."
)

# Mutations that touch field/option definitions.  They are never used and the
# client rejects any document that mentions them, as defence in depth.
FORBIDDEN_MUTATIONS = (
    "updateProjectV2Field",
    "createProjectV2Field",
    "deleteProjectV2Field",
    "updateProjectV2",
    "deleteProjectV2",
    "deleteProjectV2Item",
    "archiveProjectV2Item",
)

ALLOWED_MUTATIONS = ("updateProjectV2ItemFieldValue", "addComment", "closeIssue")


class CcbResolverError(Exception):
    """Raised for unrecoverable configuration or API failures."""


def _mentions(document: str, name: str) -> bool:
    """True when ``name`` appears in ``document`` as a whole identifier."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", document) is not None


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------


class GraphQLClient:
    """Minimal GitHub GraphQL client with a write guard."""

    def __init__(self, token: str, url: str = "https://api.github.com/graphql",
                 transport: Optional[Callable[[dict], dict]] = None,
                 allow_writes: bool = False):
        self.token = token
        self.url = url
        self.allow_writes = allow_writes
        self._transport = transport or self._http_transport

    def _http_transport(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "fprime-actions-ccb-resolver",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise CcbResolverError(f"GraphQL HTTP {exc.code}: {body[:500]}") from exc

    def query(self, document: str, variables: Optional[dict] = None) -> dict:
        if document.lstrip().startswith("mutation"):
            raise CcbResolverError("query() refuses to send a mutation")
        return self._send(document, variables)

    def mutate(self, document: str, variables: Optional[dict] = None) -> dict:
        stripped = document.lstrip()
        if not stripped.startswith("mutation"):
            raise CcbResolverError("mutate() requires a mutation document")
        for name in FORBIDDEN_MUTATIONS:
            if _mentions(document, name):
                raise CcbResolverError(f"forbidden mutation '{name}' requested")
        if not any(_mentions(document, name) for name in ALLOWED_MUTATIONS):
            raise CcbResolverError("mutation is not in the allow-list")
        if not self.allow_writes:
            raise CcbResolverError("write attempted while writes are disabled (dry-run)")
        return self._send(document, variables)

    def _send(self, document: str, variables: Optional[dict]) -> dict:
        result = self._transport({"query": document, "variables": variables or {}})
        if result.get("errors"):
            raise CcbResolverError("GraphQL errors: " + json.dumps(result["errors"]))
        return result.get("data") or {}


# ---------------------------------------------------------------------------
# Board model
# ---------------------------------------------------------------------------


@dataclass
class SingleSelectField:
    id: str
    name: str
    options: Dict[str, str]  # option name -> option id

    def option_id(self, name: str) -> str:
        try:
            return self.options[name]
        except KeyError:
            known = ", ".join(sorted(self.options))
            raise CcbResolverError(
                f"field '{self.name}' has no option named '{name}' (known: {known})"
            ) from None


@dataclass
class Item:
    item_id: str
    status: Optional[str]
    resolution: Optional[str]
    content_type: str  # "Issue", "PullRequest", "DraftIssue", or ""
    content_id: Optional[str] = None
    number: Optional[int] = None
    title: str = ""
    url: str = ""
    repository: str = ""
    state: Optional[str] = None  # OPEN / CLOSED for issues and PRs

    @property
    def label(self) -> str:
        where = f"{self.repository}#{self.number}" if self.number else self.content_type or "item"
        return f"{where} {self.title}".strip()


@dataclass
class Action:
    kind: str  # "set-status", "comment-and-close", "skip"
    item: Item
    detail: str
    new_status: Optional[str] = None
    applied: bool = False
    error: Optional[str] = None

    def label_line(self) -> str:
        return f"{self.item.label} ({self.item.url or 'no url'}) -> {self.detail}"


# ---------------------------------------------------------------------------
# Decision logic (pure)
# ---------------------------------------------------------------------------


def decide(item: Item, approved_status: str, done_status: str) -> Optional[Action]:
    """Return the action for ``item`` or ``None`` when nothing should happen."""
    if item.status != CCB_STATUS:
        return None
    if item.resolution == APPROVED:
        return Action("set-status", item, f"{APPROVED}: Status -> {approved_status}",
                      new_status=approved_status)
    if item.resolution == DISAPPROVED:
        if item.content_type == "Issue" and item.state == "OPEN":
            return Action("comment-and-close", item,
                          f"{DISAPPROVED}: comment and close as not planned")
        if item.state == "CLOSED":
            return Action("set-status", item,
                          f"{DISAPPROVED}: content already closed, Status -> {done_status}",
                          new_status=done_status)
        return Action("skip", item,
                      f"{DISAPPROVED}: {item.content_type or 'unknown'} content is not an "
                      "open issue; needs manual handling")
    return None


# ---------------------------------------------------------------------------
# Board access
# ---------------------------------------------------------------------------


PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  %s(login: $login) {
    projectV2(number: $number) {
      id
      title
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
    }
  }
}
"""

ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          type
          fieldValues(first: 50) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            __typename
            ... on DraftIssue { title }
            ... on Issue { id number title url state repository { nameWithOwner } }
            ... on PullRequest { id number title url state repository { nameWithOwner } }
          }
        }
      }
    }
  }
}
"""

SET_STATUS_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field,
            value: {singleSelectOptionId: $option}}
  ) { projectV2Item { id } }
}
"""

COMMENT_MUTATION = """
mutation($subject: ID!, $body: String!) {
  addComment(input: {subjectId: $subject, body: $body}) { clientMutationId }
}
"""

CLOSE_MUTATION = """
mutation($issue: ID!) {
  closeIssue(input: {issueId: $issue, stateReason: NOT_PLANNED}) { issue { id state } }
}
"""


@dataclass
class Project:
    id: str
    title: str
    fields: Dict[str, SingleSelectField] = field(default_factory=dict)


def load_project(client: GraphQLClient, owner: str, number: int) -> Project:
    """Resolve the project and its single-select fields by name."""
    last_error: Optional[Exception] = None
    for owner_type in ("organization", "user"):
        try:
            data = client.query(PROJECT_QUERY % owner_type, {"login": owner, "number": number})
        except CcbResolverError as exc:
            last_error = exc
            continue
        node = (data.get(owner_type) or {}).get("projectV2")
        if node:
            fields: Dict[str, SingleSelectField] = {}
            for raw in node["fields"]["nodes"]:
                if raw and "options" in raw:
                    fields[raw["name"]] = SingleSelectField(
                        raw["id"], raw["name"], {o["name"]: o["id"] for o in raw["options"]}
                    )
            return Project(node["id"], node["title"], fields)
    raise CcbResolverError(
        f"project #{number} not found for owner '{owner}'"
        + (f": {last_error}" if last_error else "")
    )


def parse_item(raw: dict) -> Item:
    values: Dict[str, str] = {}
    for value in raw.get("fieldValues", {}).get("nodes", []):
        if value and value.get("field") and value.get("name") is not None:
            values[value["field"]["name"]] = value["name"]
    content = raw.get("content") or {}
    repo = (content.get("repository") or {}).get("nameWithOwner", "")
    return Item(
        item_id=raw["id"],
        status=values.get(STATUS_FIELD),
        resolution=values.get(RESOLUTION_FIELD),
        content_type=content.get("__typename", ""),
        content_id=content.get("id"),
        number=content.get("number"),
        title=content.get("title", ""),
        url=content.get("url", ""),
        repository=repo,
        state=content.get("state"),
    )


def fetch_items(client: GraphQLClient, project_id: str) -> List[Item]:
    items: List[Item] = []
    cursor: Optional[str] = None
    while True:
        data = client.query(ITEMS_QUERY, {"project": project_id, "cursor": cursor})
        connection = data["node"]["items"]
        items.extend(parse_item(raw) for raw in connection["nodes"] if raw)
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return items
        cursor = page["endCursor"]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    approved_status: str = "Backlog - Deferred"
    done_status: str = "Done"
    disapproval_comment: str = DEFAULT_DISAPPROVAL_COMMENT
    apply: bool = False
    mutation_delay: float = 1.0
    sleep: Callable[[float], None] = time.sleep


def validate_fields(project: Project, settings: Settings) -> None:
    for name in (STATUS_FIELD, RESOLUTION_FIELD):
        if name not in project.fields:
            known = ", ".join(sorted(project.fields))
            raise CcbResolverError(
                f"project '{project.title}' has no single-select field '{name}' (known: {known})"
            )
    status = project.fields[STATUS_FIELD]
    for name in (CCB_STATUS, settings.approved_status, settings.done_status):
        status.option_id(name)
    resolution = project.fields[RESOLUTION_FIELD]
    for name in (APPROVED, DISAPPROVED):
        resolution.option_id(name)


def apply_action(client: GraphQLClient, project: Project, action: Action,
                 settings: Settings) -> None:
    status = project.fields[STATUS_FIELD]
    if action.kind == "set-status":
        client.mutate(SET_STATUS_MUTATION, {
            "project": project.id,
            "item": action.item.item_id,
            "field": status.id,
            "option": status.option_id(action.new_status or ""),
        })
        settings.sleep(settings.mutation_delay)
    elif action.kind == "comment-and-close":
        client.mutate(COMMENT_MUTATION, {
            "subject": action.item.content_id,
            "body": settings.disapproval_comment,
        })
        settings.sleep(settings.mutation_delay)
        client.mutate(CLOSE_MUTATION, {"issue": action.item.content_id})
        settings.sleep(settings.mutation_delay)
    else:
        raise CcbResolverError(f"cannot apply action kind '{action.kind}'")


@dataclass
class Report:
    project_title: str
    total_items: int
    ccb_items: int
    actions: List[Action]


def run(client: GraphQLClient, owner: str, number: int, settings: Settings,
        log: Callable[[str], None] = print) -> Report:
    project = load_project(client, owner, number)
    validate_fields(project, settings)
    items = fetch_items(client, project.id)
    ccb_items = sum(1 for i in items if i.status == CCB_STATUS)
    log(f"Project '{project.title}' ({owner} #{number}): {len(items)} items, "
        f"{ccb_items} in {CCB_STATUS}")

    actions = [a for a in (decide(i, settings.approved_status, settings.done_status)
                           for i in items) if a]
    mode = "APPLY" if settings.apply else "DRY-RUN"
    for action in actions:
        log(f"[{mode}] {action.kind}: {action.label_line()}")
        if not settings.apply or action.kind == "skip":
            continue
        try:
            apply_action(client, project, action, settings)
            action.applied = True
        except CcbResolverError as exc:
            action.error = str(exc)
            log(f"::error::{action.item.label}: {exc}")
    return Report(project.title, len(items), ccb_items, actions)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summary_markdown(report: Report, owner: str, number: int, settings: Settings) -> str:
    mode = "apply (writes enabled)" if settings.apply else "dry-run (no writes)"
    actions = report.actions
    lines = [
        f"## CCB resolver — {report.project_title} ({owner} project #{number})",
        "",
        f"Mode: **{mode}** — {report.total_items} items scanned, "
        f"{report.ccb_items} in `{CCB_STATUS}`, {len(actions)} actionable",
        "",
    ]
    if not actions:
        lines.append("Nothing to do: no `CCB` items with an actionable `CCB Resolution`.")
        return "\n".join(lines) + "\n"
    lines += ["| Item | Action | Result |", "|---|---|---|"]
    for action in actions:
        link = f"[{action.item.label}]({action.item.url})" if action.item.url else action.item.label
        if action.kind == "skip":
            result = "skipped"
        elif action.error:
            result = f"FAILED: {action.error}"
        elif action.applied:
            result = "applied"
        else:
            result = "would apply"
        lines.append(f"| {link} | {action.detail} | {result} |")
    return "\n".join(lines) + "\n"


def write_outputs(actions: List[Action]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    counts = {
        "planned": sum(1 for a in actions if a.kind != "skip"),
        "applied": sum(1 for a in actions if a.applied),
        "failed": sum(1 for a in actions if a.error),
        "skipped": sum(1 for a in actions if a.kind == "skip"),
    }
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in counts.items():
            handle.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--owner", required=True, help="Project owner (org or user login)")
    parser.add_argument("--number", required=True, type=int, help="Project number")
    parser.add_argument("--apply", action="store_true",
                        help="Send mutations.  Without this flag the run is a dry-run.")
    parser.add_argument("--approved-status", default="Backlog - Deferred")
    parser.add_argument("--done-status", default="Done")
    parser.add_argument("--disapproval-comment", default=DEFAULT_DISAPPROVAL_COMMENT)
    parser.add_argument("--mutation-delay", type=float, default=1.0,
                        help="Seconds to sleep after each mutation")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                        help="File to append the markdown summary to")
    parser.add_argument("--api-url", default="https://api.github.com/graphql")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("::error::GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    settings = Settings(
        approved_status=args.approved_status,
        done_status=args.done_status,
        disapproval_comment=args.disapproval_comment,
        apply=args.apply,
        mutation_delay=args.mutation_delay,
    )
    client = GraphQLClient(token, url=args.api_url, allow_writes=args.apply)
    try:
        report = run(client, args.owner, args.number, settings)
    except CcbResolverError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    actions = report.actions
    summary = summary_markdown(report, args.owner, args.number, settings)
    print(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    write_outputs(actions)
    return 1 if any(a.error for a in actions) else 0


if __name__ == "__main__":
    sys.exit(main())
