"""Data objects for static probing.

These objects describe definitions only, never current values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Condition:
    raw: str | None = None
    diagnostic: str | None = None
    action: str | None = None
    tokens: list[str] = field(default_factory=list)


@dataclass
class Value:
    value: str | None
    comment: str | None = None
    condition: Condition | None = None
    action: str | None = None
    semaphores: list[str] = field(default_factory=list)


@dataclass
class ParamVariant:
    refmode: str | None
    type: str | None = None
    default: str | None = None
    min: str | None = None
    max: str | None = None
    unit: str | None = None
    display: str | None = None
    visible: bool | None = None
    conditions: list[Condition] = field(default_factory=list)
    values: list[Value] = field(default_factory=list)
    group: str | None = None
    tab: str | None = None
    source: str = ""


@dataclass
class Parameter:
    name: str
    description: str | None = None
    variants: list[ParamVariant] = field(default_factory=list)


@dataclass
class RefMode:
    name: str
    base: str | None = None
    abstract: bool = False
    conditions: list[Condition] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)
    bsp_dependencies: list[dict] = field(default_factory=list)
    configs: list[str] = field(default_factory=list)
    group: str | None = None
    tab: str | None = None
    description: str | None = None
    source: str = ""


@dataclass
class Mode:
    id: str
    name: str
    internal_name: str
    parent: str | None = None
    operator: str | None = None
    is_leaf: bool = False
    signals: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    remove_condition: str | None = None
    #: XML attribute RemoveDisable: default modes are picked from it.
    remove_disable: bool = False
    semaphores: list[str] = field(default_factory=list)
    refmode: str | None = None
    user_name: str | None = None
    description: str | None = None
    path: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class Signal:
    name: str
    pins: list[str] = field(default_factory=list)
    io_modes: list[str] = field(default_factory=list)
    direction: str | None = None
    virtual: bool = False
    shareable_group: str | None = None
    exclusive_group: str | None = None
    modes: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class Pin:
    name: str
    position: str | None = None
    positions: list[str] = field(default_factory=list)
    type: str | None = None
    signals: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    source: str = ""


@dataclass
class IpEntry:
    instance: str
    name: str
    version: str
    attrs: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class IpModel:
    instance: str
    name: str
    version: str
    ip_type: str | None = None
    group: str | None = None
    db_version: str | None = None
    description: str | None = None
    root_conditions: list["Condition"] = field(default_factory=list)
    semaphores: list[dict] = field(default_factory=list)
    ip_dependencies: list[str] = field(default_factory=list)
    modes: list[Mode] = field(default_factory=list)
    refmodes: list[RefMode] = field(default_factory=list)
    parameters: list[Parameter] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    gpio_pins: list[dict] = field(default_factory=list)
    gpio_ports: list[dict] = field(default_factory=list)
    dma_flows: list[dict] = field(default_factory=list)
    dma_requests: list[dict] = field(default_factory=list)
    specific_parameters: list[dict] = field(default_factory=list)
    unhandled: list[dict] = field(default_factory=list)
    source: str = ""

    def mode_by_id(self) -> dict[str, Mode]:
        return {m.id: m for m in self.modes}

    def parameter_by_name(self) -> dict[str, Parameter]:
        return {p.name: p for p in self.parameters}

    def signal_by_name(self) -> dict[str, Signal]:
        return {s.name: s for s in self.signals}


@dataclass
class Device:
    refname: str
    family: str | None = None
    line: str | None = None
    package: str | None = None
    die: str | None = None
    clock_tree: str | None = None
    db_version: str | None = None
    pins: list[Pin] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    ips: list[IpEntry] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)
    unhandled: list[dict] = field(default_factory=list)
    source: str = ""

    def pin_by_name(self) -> dict[str, Pin]:
        return {p.name: p for p in self.pins}

    def signal_by_name(self) -> dict[str, Signal]:
        return {s.name: s for s in self.signals}


@dataclass
class RawElement:
    """One XML element exactly as read, in document order.

    The raw layer keeps what a compact index would lose: every attribute, the text
    and the tail, and the direct children in document order. The index layer points
    at these records by `seq` and never replaces them.
    """

    seq: int
    path: list[str] = field(default_factory=list)
    tag: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    text: str | None = None
    tail: str | None = None
    parent: int | None = None
    children: list[int] = field(default_factory=list)


@dataclass
class ClockNode:
    id: str
    tree: str
    type: str | None = None
    ref_parameter: str | None = None
    ref_enable: str | None = None
    ref_secure: str | None = None
    ref_lock: str | None = None
    is_key: bool = False
    conditions: list[Condition] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    source: str = ""
    #: Document-order record of this element in `ClockModel.raw`.
    seq: int = -1
    #: Every XML attribute of the element, not only the ones the index interprets.
    attrs: dict[str, str] = field(default_factory=dict)
    #: Tags of the direct children in document order (Condition, Input, Output and
    #: anything this version does not interpret).
    child_tags: list[str] = field(default_factory=list)
    #: Document-order records of the Input/Output declarations (see RawElement).
    input_seqs: list[int] = field(default_factory=list)
    output_seqs: list[int] = field(default_factory=list)


@dataclass
class ClockEdge:
    source: str
    target: str
    signal_id: str | None = None
    resolved: bool = True
    tree: str | None = None
    #: "ok" | "same-tree" | "ambiguous" | "missing" | "empty"
    resolution: str = "ok"
    #: Every candidate node id when one raw id matches more than one element.
    candidates: list[str] = field(default_factory=list)
    #: Declarations that produced this edge: `{"seq", "direction", "attrs"}`.
    declarations: list[dict] = field(default_factory=list)


@dataclass
class ClockModel:
    name: str
    nodes: list[ClockNode] = field(default_factory=list)
    edges: list[ClockEdge] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    unhandled: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    #: Every element of the file (elements, inputs, outputs, signals, labels...).
    raw: list[RawElement] = field(default_factory=list)
    #: `<Signals><Signal .../>` entries with every attribute kept.
    signal_entries: list[dict] = field(default_factory=list)
    #: Declarations that did not resolve to one node, with all candidates.
    unresolved: list[dict] = field(default_factory=list)
    #: Every merged connection (output side, input side, or both), one per endpoint
    #: pair + signal. Each entry keeps the declarations that produced it.
    connections: list[dict] = field(default_factory=list)


@dataclass
class Item:
    id: str
    kind: str
    label: str
    owner: str | None
    source: str
    fields: dict[str, Any] = field(default_factory=dict)
    conditions: list[dict] = field(default_factory=list)
    values: list[dict] = field(default_factory=list)
    persist_keys: list[str] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    evidence: str = ""
    static: bool = True


@dataclass
class ProbeModel:
    device: Device
    ips: list[IpModel] = field(default_factory=list)
    clock: ClockModel | None = None
    items: list[Item] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def item_by_id(self) -> dict[str, Item]:
        return {i.id: i for i in self.items}


def to_jsonable(obj):
    from dataclasses import asdict, is_dataclass
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return sorted(to_jsonable(v) for v in obj)
    return obj