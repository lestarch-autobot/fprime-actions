#!/usr/bin/env python3
"""I1 -- Source dependencies listed in CMakeLists.

Parses the component's non-test sources for ``#include`` dependencies on
other F´ modules and verifies each dependency module is declared in the
component's CMakeLists.txt (e.g. via DEPENDS or an explicit target name).
Exits non-zero listing any missing dependency.

Heuristics: an include of ``Fw/Types/Assert.hpp`` depends on module
``Fw/Types``.  System headers (no directory component), system/platform
headers under a lowercase top-level directory (``sys/socket.h``,
``arpa/inet.h``, ``linux/gpio.h``, ``mach/mach.h``, ``openssl/evp.h``,
...; F´ module directories are CapitalCase), the module's own headers
(including its test helpers), generated autocode headers
(``*Ac.hpp``/``*Ac.h``), and ``config``/``Fpp`` headers are skipped.
Root framework convenience headers (``Fw/FPrimeBasicTypes.hpp``: the
``Fw`` directory only registers an umbrella INTERFACE target), directly
included source files (``FppTest/component/common/typed.cpp``), and
includes resolving to a directory with no CMakeLists.txt (shared
source directories that register no target) are also skipped.
"""

import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import List, Optional, Set

from _report import finish, make_parser
from _sources import TEST_DIR_NAMES, impl_sources, includes_of

_SKIP_TOP_DIRS = {"config", "fpp", "gtest", "gmock"}

# Directories registering only an umbrella INTERFACE target: headers living
# directly in them (e.g. Fw/FPrimeBasicTypes.hpp) belong to no module and
# recommending the umbrella would make its own members depend on it.
_UMBRELLA_ONLY_DIRS = {"Fw"}

_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")


# Every F Prime module transitively depends on Fw_Types (cmake/API.cmake,
# GLOBAL_IMPLICIT_DEPENDENCY), so it never needs listing in DEPENDS.
_IMPLICIT_TARGETS = {"Fw_Types"}


@dataclass
class BuildContext:
    """What the build cache knows: include roots and dependencies FPP adds by itself."""

    include_dirs: List[Path] = field(default_factory=list)
    roots: List[Path] = field(default_factory=list)
    auto_targets: Set[str] = field(default_factory=set)
    cache: Optional[Path] = None
    project: Optional[Path] = None


def _module_rel(path: Path, ctx: BuildContext) -> Optional[str]:
    """Project/framework-relative path of the CMake module that owns ``path``."""
    path = path.resolve()
    if ctx.cache is not None and ctx.cache in path.parents:
        rel = path.relative_to(ctx.cache)
        if rel.parts and rel.parts[0] == "F-Prime":
            rel = Path(*rel.parts[1:])
            roots = [r for r in ctx.roots if r != ctx.project and r != ctx.cache]
        else:
            roots = [ctx.project] if ctx.project else []
        candidates = [r / rel for r in roots]
    else:
        candidates = [path]
    for candidate in candidates:
        for root in sorted(ctx.roots, key=lambda r: -len(r.parts)):
            if root == candidate or root not in candidate.parents:
                continue
            directory = candidate if candidate.is_dir() else candidate.parent
            while directory != root:
                if (directory / "CMakeLists.txt").is_file():
                    return directory.relative_to(root).as_posix()
                directory = directory.parent
    return None


def load_build_context(build_cache: Optional[Path], module_dir: Path) -> BuildContext:
    ctx = BuildContext()
    if build_cache is None or not build_cache.is_dir():
        return ctx
    ctx.cache = build_cache.resolve()
    ctx.project = ctx.cache.parent
    compdb = ctx.cache / "compile_commands.json"
    module_resolved = module_dir.resolve()
    if compdb.is_file():
        try:
            entries = json.loads(compdb.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries = []
        for entry in entries:
            if module_resolved not in Path(entry.get("file", "")).resolve().parents:
                continue
            words = entry.get("arguments") or shlex.split(entry.get("command", ""))
            for i, word in enumerate(words):
                inc = None
                if word in ("-I", "-isystem") and i + 1 < len(words):
                    inc = words[i + 1]
                elif word.startswith("-I") and len(word) > 2:
                    inc = word[2:]
                if inc and Path(inc).is_dir():
                    inc_path = Path(inc).resolve()
                    if inc_path not in ctx.include_dirs:
                        ctx.include_dirs.append(inc_path)
            break
    # Module names are relative to a CMake project root: the project itself and the
    # F Prime framework (the include root holding Fw/Types), not every -I directory.
    ctx.roots = [ctx.project] + [
        d for d in ctx.include_dirs if d != ctx.project and ctx.cache not in d.parents and (d / "Fw" / "Types").is_dir()
    ]
    try:
        module_rel = module_resolved.relative_to(ctx.project)
    except ValueError:
        return ctx
    fpp_cache = ctx.cache / module_rel / "fpp-cache"
    for name in ("direct.txt", "framework.txt"):
        try:
            lines = (fpp_cache / name).read_text(encoding="utf-8").split()
        except OSError:
            continue
        for line in lines:
            if name == "framework.txt":
                ctx.auto_targets.add(line.strip())
                continue
            rel = _module_rel(Path(line), ctx)
            if rel:
                ctx.auto_targets.add(rel.replace("/", "_"))
    return ctx


def resolve_dependency(include: str, ctx: BuildContext) -> Optional[str]:
    """Module path owning ``include`` by looking it up on the build's include path."""
    for inc_dir in ctx.include_dirs:
        candidate = inc_dir / include
        if candidate.is_file():
            return _module_rel(candidate, ctx)
    return None


def dependency_of(include: str) -> str | None:
    """Map an include target to its F´ dependency module path, or None."""
    path = PurePosixPath(include)
    if len(path.parts) < 2:
        return None  # system or local header
    if path.name.endswith(("Ac.hpp", "Ac.h", "Ac.cpp")):
        return None  # generated autocode
    if path.suffix.lower() in _SOURCE_SUFFIXES:
        return None  # directly-included source file, not a library dependency
    top = path.parts[0]
    if top.lower() in _SKIP_TOP_DIRS:
        return None
    if not top[:1].isupper():
        return None  # system/platform/third-party header (F´ modules are CapitalCase)
    # A test-helper include such as Drv/Ip/test/ut/Helper.hpp depends on Drv/Ip.
    parts = list(path.parts[:-1])
    for i, part in enumerate(parts):
        if part.lower() in TEST_DIR_NAMES:
            parts = parts[:i]
            break
    if not parts:
        return None
    dep = "/".join(parts)
    if dep in _UMBRELLA_ONLY_DIRS:
        return None  # root convenience header of an umbrella-only directory
    return dep


def is_reportable_dependency(dep: str, module_dir: Path) -> bool:
    """False when the dependency should not be reported for this module:
    the module lives inside the dependency directory (an ancestor umbrella
    such as ``Fw`` for ``Fw/Types``), the dependency is a subdirectory of
    the module itself (e.g. a subtopology config module), or the dependency
    directory exists in the tree but registers no CMake target (no
    CMakeLists.txt -- a shared source directory such as
    ``FppTest/component/common``)."""
    module_resolved = module_dir.resolve()
    dep_parts = tuple(dep.split("/"))
    ancestors = [module_resolved] + list(module_resolved.parents)
    for ancestor in ancestors:
        if ancestor.parts[-len(dep_parts):] == dep_parts:
            return False  # the module itself or an enclosing directory
    for parent in ancestors:
        candidate = parent / dep
        if candidate.is_dir():
            if parent == module_resolved:
                return False  # the module's own subdirectory (e.g. a config module)
            return (candidate / "CMakeLists.txt").is_file()
    return True  # not found in the tree; report conservatively


def declared_in_cmake(dep: str, cmake_text: str) -> bool:
    """True when the dependency module (or an ancestor library that provides
    it, e.g. ``STest`` for ``STest/Pick``) is named in the CMake text."""
    parts = dep.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for candidate in candidates:
        for token in (candidate, candidate.replace("/", "_")):
            if re.search(rf"(?<![\w/]){re.escape(token)}(?![\w/])", cmake_text):
                return True
    return False


def main(argv=None) -> int:
    parser = make_parser(__doc__)
    parser.add_argument(
        "--cmake",
        default="CMakeLists.txt",
        help="CMake file relative to the module (default: CMakeLists.txt)",
    )
    parser.add_argument(
        "--build-cache",
        type=Path,
        default=None,
        help="F Prime build cache: resolves includes on the real include path and treats "
        "FPP-derived dependencies (fpp-cache/direct.txt, framework.txt) as declared",
    )
    args = parser.parse_args(argv)
    ctx = load_build_context(args.build_cache, args.module)

    cmake_path = args.module / args.cmake
    sources = impl_sources(args.module)

    skipped_reason = None
    cmake_text = ""
    if not cmake_path.is_file():
        skipped_reason = f"no {args.cmake} found"
    elif not sources:
        skipped_reason = "no C++ sources found"
    else:
        cmake_text = cmake_path.read_text(encoding="utf-8", errors="replace")

    deps_needed = {}
    auto_satisfied = set()
    for include in includes_of(sources):
        dep = resolve_dependency(include, ctx) if ctx.include_dirs else None
        if dep is None and ctx.include_dirs and any((d / include).is_file() for d in ctx.include_dirs):
            continue  # found on the include path but owned by no module (platform/config header)
        if dep is None:
            dep = dependency_of(include)
        if dep is None:
            continue
        if not is_reportable_dependency(dep, args.module):
            continue
        if dep.replace("/", "_") in _IMPLICIT_TARGETS | ctx.auto_targets:
            auto_satisfied.add(dep)
            continue
        deps_needed.setdefault(dep, include)

    failures = []
    if skipped_reason is None:
        for dep, example in sorted(deps_needed.items()):
            if not declared_in_cmake(dep, cmake_text):
                failures.append(
                    f"dependency '{dep}' (from #include \"{example}\") is not declared "
                    f"in {args.cmake} (add it to DEPENDS as {dep.replace('/', '_')})"
                )

    return finish(
        check_id="I1",
        name="Source dependencies listed in CMakeLists",
        category="implementation",
        module=args.module,
        failures=failures,
        skipped_reason=skipped_reason,
        detail=f"{len(deps_needed)} dependency module(s) checked"
        + (f"; {len(auto_satisfied)} provided by FPP/implicit framework dependencies" if auto_satisfied else ""),
        json_output=args.json_output,
    )


if __name__ == "__main__":
    sys.exit(main())
