"""Copy the other published refs' renderings into this baseline worktree.

Every ref publishes its quality site to its own orphan branch
(``<prefix>/<ref-name>``).  When the primary ref (``devel``) publishes, this
script copies the tree of every other ``<prefix>/*`` branch to
``versions/<ref-name>/`` in the primary worktree and writes ``versions.json``,
which ``catalog.py`` renders as the version navigation in the landing-page
header.  Each copied landing page gets the same navigation, so a reader can
move between versions from any of them.

A version is re-copied only when its baseline branch head changed (recorded in
``versions.json``); directories of branches that no longer exist are removed.

Run from a checkout of the source repository (``git`` talks to ``--remote``).
"""

from __future__ import annotations

import argparse
import html
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

VERSIONS_DIR = "versions"
VERSIONS_FILE = "versions.json"
SCHEMA_VERSION = 1
NAV_START = "<!-- versions-nav -->"
NAV_END = "<!-- /versions-nav -->"
NAV_STYLE = (
    "font-size:0.9rem;margin-bottom:0.75rem;padding:0.4rem 0.75rem;"
    "background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;"
)
_NAV_RE = re.compile(re.escape(NAV_START) + r".*?" + re.escape(NAV_END), re.S)
_BODY_RE = re.compile(r"<body[^>]*>")


def _git(*args: str, cwd: Optional[Path] = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE
    ).stdout


def list_baseline_branches(remote: str, prefix: str, cwd: Optional[Path] = None) -> Dict[str, str]:
    """Map ``<ref-name>`` to the head commit of ``<prefix>/<ref-name>`` on the remote."""
    head_prefix = f"refs/heads/{prefix}/"
    out = _git("ls-remote", remote, f"{head_prefix}*", cwd=cwd).decode("utf-8")
    branches: Dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(head_prefix):
            branches[ref[len(head_prefix):]] = sha.strip()
    return branches


def _version_key(ref: str) -> tuple:
    """Sort key: highest version first; refs without a version last, by name."""
    match = re.search(r"v(\d+)\.(\d+)(?:\.(\d+|x))?", ref)
    if not match:
        return (1, (), ref)
    patch = match.group(3)
    numbers = (
        -int(match.group(1)),
        -int(match.group(2)),
        -(10**9) if patch in (None, "x") else -int(patch),
    )
    return (0, numbers, ref)


def load_versions(dest: Path) -> Optional[dict]:
    """Read ``versions.json`` from a baseline worktree; None when absent or invalid."""
    try:
        data = json.loads((dest / VERSIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        return None
    return data


def render_nav(versions: Optional[dict], *, current: str, root: str = "") -> str:
    """HTML for the version navigation; ``root`` is the relative URL prefix of the primary site."""
    if not versions or not versions.get("versions"):
        return ""
    primary = str(versions.get("primary", ""))
    items = [(primary, f"{root}index.html")] + [
        (str(v["ref"]), f"{root}{v['path']}/index.html") for v in versions["versions"]
    ]
    links = []
    for ref, href in items:
        label = f"<code>{html.escape(ref)}</code>"
        if ref == current:
            links.append(f"<strong>{label}</strong>")
        else:
            links.append(f'<a href="{html.escape(href)}">{label}</a>')
    return (
        f'{NAV_START}<div class="versions" style="{NAV_STYLE}">'
        f"<strong>Versions:</strong> {' &middot; '.join(links)}</div>{NAV_END}"
    )


def inject_nav(page: Path, nav: str) -> bool:
    """Insert (or replace) the navigation at the top of ``page``'s body."""
    try:
        text = page.read_text(encoding="utf-8")
    except OSError:
        return False
    text = _NAV_RE.sub("", text)
    match = _BODY_RE.search(text)
    if not match:
        return False
    page.write_text(text[: match.end()] + nav + text[match.end():], encoding="utf-8")
    return True


def _extract(remote: str, branch: str, target: Path, cwd: Optional[Path]) -> None:
    """Replace ``target`` with the tree of ``branch`` (minus its own ``versions/``)."""
    _git("fetch", "--quiet", "--depth", "1", remote, f"refs/heads/{branch}", cwd=cwd)
    archive = _git("archive", "--format=tar", "FETCH_HEAD", cwd=cwd)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    root = target.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            top = member.name.split("/", 1)[0]
            if top in (VERSIONS_DIR, VERSIONS_FILE) or not (member.isfile() or member.isdir()):
                continue
            out = (root / member.name).resolve()
            if root != out and root not in out.parents:
                continue
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is not None:
                with out.open("wb") as fh:
                    shutil.copyfileobj(source, fh)


def _ref_type(target: Path) -> str:
    try:
        catalog = json.loads((target / "catalog.json").read_text(encoding="utf-8"))
        return "tag" if catalog.get("ref_type") == "tag" else "branch"
    except (OSError, ValueError, AttributeError):
        return "branch"


def sync_versions(
    *,
    dest: Path,
    ref: str,
    prefix: str,
    remote: str = "origin",
    cwd: Optional[Path] = None,
    max_versions: int = 0,
) -> dict:
    """Bring ``dest/versions/`` in line with the remote's baseline branches.

    ``max_versions`` > 0 keeps only that many, newest version first.
    """
    branches = list_baseline_branches(remote, prefix, cwd=cwd)
    branches.pop(ref, None)
    if max_versions > 0:
        branches = {name: branches[name] for name in sorted(branches, key=_version_key)[:max_versions]}
    previous = {
        str(v.get("ref")): v for v in (load_versions(dest) or {}).get("versions", [])
    }
    versions_root = dest / VERSIONS_DIR

    entries: List[dict] = []
    for name in sorted(branches, key=_version_key):
        target = versions_root / name
        head = branches[name]
        old = previous.get(name)
        if not (old and old.get("baseline_commit") == head and (target / "index.html").is_file()):
            _extract(remote, f"{prefix}/{name}", target, cwd)
        if not (target / "index.html").is_file():
            shutil.rmtree(target, ignore_errors=True)
            continue
        entries.append(
            {
                "ref": name,
                "ref_type": _ref_type(target),
                "path": f"{VERSIONS_DIR}/{name}",
                "baseline_commit": head,
            }
        )

    # Drop copies of baseline branches that no longer exist.
    current = {e["ref"] for e in entries}
    for name in previous:
        if name not in current and name:
            shutil.rmtree(versions_root / name, ignore_errors=True)
    if versions_root.is_dir():
        for folder in sorted((p for p in versions_root.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
        if not any(versions_root.iterdir()):
            versions_root.rmdir()

    data = {"schema": SCHEMA_VERSION, "primary": ref, "versions": entries}
    (dest / VERSIONS_FILE).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for entry in entries:
        depth = entry["path"].count("/") + 1
        inject_nav(
            dest / entry["path"] / "index.html",
            render_nav(data, current=entry["ref"], root="../" * depth),
        )
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, required=True, help="Baseline worktree root")
    parser.add_argument("--ref", required=True, help="Ref being published")
    parser.add_argument("--primary-ref", default="devel",
                        help='Ref whose site hosts the other versions (default: "devel"); '
                        "nothing is copied when --ref differs")
    parser.add_argument("--baseline-branch-prefix", default="coverage")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--max-versions", type=int, default=5,
                        help="Keep only the newest N other versions (default: 5; 0 keeps all)")
    args = parser.parse_args(argv)

    if not args.primary_ref or args.ref != args.primary_ref:
        print(f"versions: {args.ref} is not the primary ref; nothing to copy", file=sys.stderr)
        return 0
    data = sync_versions(
        dest=args.dest.resolve(),
        ref=args.ref,
        prefix=args.baseline_branch_prefix,
        remote=args.remote,
        max_versions=args.max_versions,
    )
    print(f"versions: {len(data['versions'])} other version(s) linked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
