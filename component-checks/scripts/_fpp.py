"""Lightweight FPP model extraction for checklist checks.

Parses the ``.fpp``/``.fppi`` files of a single module directory and extracts
the names of the dictionary-visible items a component declares:

    * commands            (``async|sync|guarded command NAME``)
    * telemetry channels  (``telemetry NAME: Type``)
    * events              (``event NAME(...)``)
    * parameters          (``param NAME: Type``)
    * data products       (``product record|container NAME``)
    * ports               (typed input/output port instances)
    * state machines      (``state machine NAME``) with their declared
      ``action`` names

This is intentionally a regex-level reader, not a full FPP front end: it
needs no build cache and no locations, so it can run as a fast CI gate on a
bare checkout.  Comments (``# ...``) and annotations (``@ ...``) are
stripped before matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

_COMMENT_RE = re.compile(r"(#|@<|@)[^\n]*")
_COMMAND_RE = re.compile(r"\b(?:async|sync|guarded)\s+command\s+(\w+)")
_TELEMETRY_RE = re.compile(r"\btelemetry\s+(\w+)\s*:")
_EVENT_RE = re.compile(r"\bevent\s+(?!port\b)(\w+)")
_PARAM_RE = re.compile(r"\bparam\s+(\w+)\s*:")
_PRODUCT_RECORD_RE = re.compile(r"\bproduct\s+record\s+(\w+)")
_PRODUCT_CONTAINER_RE = re.compile(r"\bproduct\s+container\s+(\w+)")
_INPUT_PORT_RE = re.compile(r"\b(async|sync|guarded)\s+input\s+port\s+(\w+)\s*:")
_OUTPUT_PORT_RE = re.compile(r"\boutput\s+port\s+(\w+)\s*:")
_STATE_MACHINE_RE = re.compile(r"\bstate\s+machine\s+(\w+)")
_SM_ACTION_RE = re.compile(r"\baction\s+(\w+)")
# "product container NAME id X default priority N" -- id clause optional
_CONTAINER_STMT_RE = re.compile(
    r"\bproduct\s+container\s+(\w+)((?:\s+id\s+\S+)?(?:\s+default\s+priority\s+\S+)?)"
)


@dataclass
class Port:
    name: str
    kind: str  # "async" | "sync" | "guarded" for inputs; "output" for outputs
    direction: str  # "input" | "output"


@dataclass
class Container:
    name: str
    has_default_priority: bool


@dataclass
class StateMachine:
    name: str
    actions: List[str] = field(default_factory=list)


@dataclass
class FppModel:
    """Names extracted from a module's FPP sources."""

    commands: List[str] = field(default_factory=list)
    telemetry: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    parameters: List[str] = field(default_factory=list)
    records: List[str] = field(default_factory=list)
    containers: List[Container] = field(default_factory=list)
    input_ports: List[Port] = field(default_factory=list)
    output_ports: List[Port] = field(default_factory=list)
    state_machines: List[StateMachine] = field(default_factory=list)
    fpp_files: List[Path] = field(default_factory=list)

    @property
    def data_products(self) -> List[str]:
        return self.records + [c.name for c in self.containers]

    @property
    def ports(self) -> List[Port]:
        return self.input_ports + self.output_ports


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = item if isinstance(item, str) else getattr(item, "name", item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_state_machines(text: str) -> List[StateMachine]:
    machines: List[StateMachine] = []
    for match in _STATE_MACHINE_RE.finditer(text):
        name = match.group(1)
        # Actions are declared within the machine's brace-delimited body.
        # Find the matching closing brace by brace counting.
        idx = text.find("{", match.end())
        if idx < 0:
            # "state machine instance NAME" or forward declaration
            if name != "instance":
                machines.append(StateMachine(name=name))
            continue
        depth = 0
        end = idx
        for end in range(idx, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        body = text[idx:end]
        actions = _dedupe(_SM_ACTION_RE.findall(body))
        if name != "instance":
            machines.append(StateMachine(name=name, actions=actions))
    return machines


def parse_fpp_text(text: str, model: FppModel) -> None:
    text = strip_comments(text)
    model.commands.extend(_COMMAND_RE.findall(text))
    model.telemetry.extend(_TELEMETRY_RE.findall(text))
    model.events.extend(_EVENT_RE.findall(text))
    model.parameters.extend(_PARAM_RE.findall(text))
    model.records.extend(_PRODUCT_RECORD_RE.findall(text))
    for match in _CONTAINER_STMT_RE.finditer(text):
        model.containers.append(
            Container(
                name=match.group(1),
                has_default_priority="default priority" in re.sub(r"\s+", " ", match.group(2)),
            )
        )
    for kind, name in _INPUT_PORT_RE.findall(text):
        model.input_ports.append(Port(name=name, kind=kind, direction="input"))
    for name in _OUTPUT_PORT_RE.findall(text):
        model.output_ports.append(Port(name=name, kind="output", direction="output"))
    model.state_machines.extend(_parse_state_machines(text))


def load_module_model(module_dir: Path) -> FppModel:
    """Parse every .fpp/.fppi file directly inside ``module_dir`` (recursively,
    excluding test directories)."""
    model = FppModel()
    files = sorted(
        p
        for pattern in ("*.fpp", "*.fppi")
        for p in module_dir.rglob(pattern)
        if not any(
            part.lower() in ("test", "tests", "ut") or part.startswith("build-fprime")
            for part in p.relative_to(module_dir).parts
        )
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        model.fpp_files.append(path)
        parse_fpp_text(text, model)
    model.commands = _dedupe(model.commands)
    model.telemetry = _dedupe(model.telemetry)
    model.events = _dedupe(model.events)
    model.parameters = _dedupe(model.parameters)
    model.records = _dedupe(model.records)
    model.containers = _dedupe(model.containers)
    return model
