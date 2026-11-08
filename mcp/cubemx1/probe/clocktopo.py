"""Clock topology queries over the raw + index model of probe/clock.py.

Three views, one entry point:

- `graph`: every node and connection (paginated, the counts say what was not shown);
- `upstream`: everything an output reads, following every MUX branch and the
  fractional input of a fractional multiplier;
- `raw`: the XML text of the file, or the original fragment of one node.

This module does not solve a frequency, does not start CubeMX and does not write
anything. The interpreter (an agent) reads the topology and then states its target
through the existing clock parameters.

What the views keep, and why:

- a MUX's inputs are **alternative branches**, never simultaneous frequency inputs;
  each keeps its document order, `from` and `refValue`;
- a fractional multiplier has a **frequency input and a fractional contribution**;
  the port role is kept so the second one is not mistaken for another source path;
- a shared upstream element keeps one node reference (it is one element, not copies);
- an ambiguous reference lists **all** candidates instead of picking the first;
- conditions, enable, secure and lock stay separate fields;
- an unknown tag or attribute is kept in the raw layer and named in the payload.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..util import suggest, suggest_clause
from .clock import KNOWN_ELEMENT_TYPES, read_clock, topology_summary
from .cond import CondState, evaluate_condition
from .model import ClockModel, ClockNode, ParamVariant, Condition

#: Default page size of the views. It limits what is returned, never what the model
#: knows: the payload reports how much was left out.
DEFAULT_SIZE = 200

#: Element types whose second input may be a fractional contribution when the type
#: is a fractional multiplier. The role is judged per input by the source element
#: type (db data), not by the element name.
FRACTIONAL_INPUT_TYPES = ("fractional",)


def _node_ref(node: ClockNode) -> str:
    """Stable reference of one node: its document-order record id."""
    return "raw:%d" % node.seq


class TargetState(CondState):
    """The part of a target document that can decide a clock condition.

    A clock condition names an instance ("FDCAN1_Exist") or a parameter. The target
    lists which instances it uses and (optionally) their parameters, so those names can
    be decided **true** from the target. The opposite is not decided: an instance the
    target does not mention is reported as unknown, never as false — the target is a
    partial description, and a condition that is not decidable here stays visible as
    a definition.
    """

    def __init__(self, target: dict | None):
        self.instances = set()
        self.params: dict[tuple, object] = {}
        if not isinstance(target, dict):
            return
        for item in target.get("ips") or []:
            instance = item.get("instance")
            if not instance:
                continue
            self.instances.add(str(instance))
            for name, value in (item.get("params") or {}).items():
                self.params[(str(instance), str(name))] = value

    def semaphore_or_usage(self, name: str):
        match = re.match(r"(.+)_Exist$", str(name or ""))
        if match and match.group(1) in self.instances:
            return 1
        return None

    def parameter(self, instance: str, name: str):
        return self.params.get((str(instance), str(name)))


def _condition_json(cond: Condition, state: TargetState | None = None) -> dict:
    out = {"expression": cond.raw, "diagnostic": cond.diagnostic,
           "action": cond.action, "tokens": list(cond.tokens)}
    if state is None:
        out["truth"] = None
        out["state"] = "no state given: the definition is shown, nothing is assumed"
        return out
    got = evaluate_condition(cond.raw, state)
    out["truth"] = got.get("truth")
    out["state"] = ("true" if got.get("truth") == 1 else
                    "false" if got.get("truth") == 0 else "unknown")
    out["unknown"] = got.get("unknown") or []
    return out


def _input_json(node: ClockNode, index: int, decl: dict, source: str | None,
                role: str | None) -> dict:
    return {
        "port": index,
        "role": role,
        "from": decl.get("from"),
        "source_ref": source,
        "signal_id": decl.get("signalId"),
        "ref_value": decl.get("refValue"),
        "attrs": dict(decl),
    }


def _output_json(node: ClockNode, index: int, decl: dict, target: str | None) -> dict:
    return {
        "port": index,
        "to": decl.get("to"),
        "target_ref": target,
        "signal_id": decl.get("signalId"),
        "attrs": dict(decl),
    }


def _raw_index(model: ClockModel) -> dict[str, list[ClockNode]]:
    """Node candidates per raw id, built once per view (ids may repeat)."""
    index: dict[str, list[ClockNode]] = {}
    for node in model.nodes:
        index.setdefault(node.id.rsplit("/", 1)[-1], []).append(node)
    return index


def _resolve_inside(model: ClockModel, raw_id: str | None,
                    same_tree: str | None,
                    index: dict[str, list[ClockNode]] | None = None
                    ) -> list[ClockNode]:
    """Every node a raw id can mean (no silent first pick)."""
    if not raw_id:
        return []
    table = index if index is not None else _raw_index(model)
    got = table.get(raw_id, [])
    if len(got) <= 1:
        return got
    same = [n for n in got if n.tree == same_tree]
    return same or got


def _node_json(model: ClockModel, node: ClockNode, role: str | None = None,
                state: TargetState | None = None,
                index: dict[str, list[ClockNode]] | None = None) -> dict:
    """One node of a view: identity, raw facts, ports and parameter binding."""
    table = index if index is not None else _raw_index(model)
    inputs = []
    for port, decl in enumerate(node.inputs):
        candidates = _resolve_inside(model, decl.get("from"), node.tree, table)
        source = candidates[0] if len(candidates) == 1 else None
        input_role = "input"
        if node.type == "multiplexor":
            input_role = "branch"
        elif node.type in ("multiplicatorFrac",) and candidates:
            if candidates[0].type in FRACTIONAL_INPUT_TYPES:
                input_role = "fractional"
        elif node.type in ("multiplicatorFrac",) and not candidates:
            input_role = "unresolved"
        inputs.append(_input_json(
            node, port, decl, _node_ref(source) if source is not None else None,
            input_role))
    outputs = []
    for port, decl in enumerate(node.outputs):
        candidates = _resolve_inside(model, decl.get("to"), node.tree, table)
        target = candidates[0] if len(candidates) == 1 else None
        outputs.append(_output_json(
            node, port, decl, _node_ref(target) if target is not None else None))
    payload = {
        "node_ref": _node_ref(node),
        "seq": node.seq,
        "raw_id": node.id.rsplit("/", 1)[-1],
        "id": node.id,
        "tree": node.tree,
        "path": list(node.path),
        "type": node.type,
        "role": role,
        "attrs": dict(node.attrs),
        "text": model.raw[node.seq].text if 0 <= node.seq < len(model.raw) else None,
        "child_tags": list(node.child_tags),
        "is_key": node.is_key,
        "conditions": [_condition_json(c, state) for c in node.conditions],
        "inputs": inputs,
        "outputs": outputs,
        "parameters": _binding_json(node),
    }
    if node.type and node.type not in _known_types(model):
        payload["unmodeled_type"] = True
    return payload


def _known_types(model: ClockModel) -> set[str]:
    """Element types the db declares (the model itself never depends on the set)."""
    return KNOWN_ELEMENT_TYPES


def _binding_json(node: ClockNode) -> dict:
    """The element's parameter reference: the raw string plus an interpreted map.

    The raw string is kept verbatim and is **not** split by one separator:
    `A,B` means equivalent names, `PARAM/DIV` means "parameter PARAM plus the
    divisor DIV of its fractional part", and `N/LIST` means "N plus the list of N
    values allowed to carry a fraction". The interpretation is a separate field so
    it never overwrites the raw fact.
    """
    raw = node.ref_parameter
    out: dict = {"raw": raw, "names": [], "divisor": None, "fraction_list": None,
                 "basis": []}
    if not raw:
        return out
    text = str(raw)
    if "," in text:
        head, rest = text.split(",", 1)
        out["names"] = [part.strip() for part in (head + "," + rest).split(",")
                        if part.strip()]
        out["basis"].append("comma: the names before and after are equivalent")
        text = head
    if "/" in text:
        left, right = text.split("/", 1)
        out["basis"].append("slash: the left part is the parameter, the right part "
                            "is a second declaration (divisor or allowed list)")
        if right.strip().isdigit():
            out["names"] = [left.strip()]
            out["divisor"] = int(right.strip())
            out["basis"].append("a numeric right part is the divisor of the "
                                "fractional part (PARAM/DIV)")
        else:
            out["names"] = [left.strip()]
            out["fraction_list"] = right.strip()
            out["basis"].append("a name on the right lists the values allowed to "
                                "carry a fraction (N/LIST)")
    else:
        out["names"] = [text.strip()]
    out["enable"] = node.ref_enable
    out["secure"] = node.ref_secure
    out["lock"] = node.ref_lock
    return out


#: The RCC definition does not change while the machine's db is the same; a clock
#: query may run several times for one device, so the parsed definitions are cached.
_DEFINITION_CACHE: dict[tuple, dict] = {}


def _parameter_definitions(machine, refname: str) -> dict:
    """RCC parameter definitions of the same device, per name, all variants kept."""
    from .catalog import load_model

    key = (str(getattr(machine, "db", "")), refname)
    if key in _DEFINITION_CACHE:
        return _DEFINITION_CACHE[key]
    try:
        model = load_model(machine, refname, ip_filter=["RCC"])
    except Exception:  # noqa: BLE001 - the topology is still readable without it
        return {}
    found = None
    for ip in model.ips:
        if ip.name.upper() == "RCC" or ip.instance.upper() == "RCC":
            found = ip
            break
    if found is None:
        return {}
    out: dict[str, list[dict]] = {}
    for parameter in found.parameters:
        variants = []
        for variant in parameter.variants:
            variants.append(_variant_json(variant))
        out[parameter.name] = variants
    _DEFINITION_CACHE[key] = out
    return out


def _variant_json(variant: ParamVariant) -> dict:
    return {
        "refmode": variant.refmode,
        "type": variant.type,
        "default": variant.default,
        "min": variant.min,
        "max": variant.max,
        "unit": variant.unit,
        "visible": variant.visible,
        "conditions": [_condition_json(c) for c in variant.conditions],
        "values": [
            {"value": value.value, "comment": value.comment,
             "semaphores": list(value.semaphores),
             "condition": _condition_json(value.condition)
                          if value.condition is not None else None}
            for value in variant.values
        ],
        "source": variant.source,
    }


def _summary(model: ClockModel) -> dict:
    summary = topology_summary(model)
    types: dict[str, int] = {}
    for node in model.nodes:
        types[node.type or "?"] = types.get(node.type or "?", 0) + 1
    summary["node_types"] = dict(sorted(types.items()))
    summary["unknown_types"] = sorted(t for t in types if t not in _known_types(model))
    summary["element_types"] = sorted(_known_types(model))
    summary["source"] = model.source
    return summary


def clock_topology(machine, device: str | None = None, target=None,
                   output: str | None = None, view: str = "graph",
                   size: int | None = None, seq: int | None = None,
                   tree: str | None = None) -> dict:
    """Query the clock topology of one device.

    :param device: device RefName; can come from `target.device.refname`
    :param target: a target JSON object (its device, and the instances that decide a
        condition; nothing is assumed about what it does not list)
    :param output: target readout name or element id (for `view=upstream`)
    :param view: graph / upstream / raw
    :param size: page size; it limits the returned list, never the model
    :param seq: raw record id (for `view=raw` with one node)
    :param tree: clock tree name override (defaults to the device declaration)
    """
    from .device import read_device

    refname, tree_name = _resolve(machine, device, target, tree)
    model = read_clock(machine, tree_name)
    size = DEFAULT_SIZE if size is None else max(1, int(size))
    payload = {
        "ok": True,
        "device": refname,
        "clock_tree": tree_name,
        "source": model.source,
        "view": view,
        "summary": _summary(model),
        "unresolved": model.unresolved[:size],
        "unresolved_total": len(model.unresolved),
        "warnings": model.warnings[:size],
        "warnings_total": len(model.warnings),
    }
    state = TargetState(target) if target else None
    if view == "graph":
        payload.update(_graph_view(model, size, state))
    elif view == "upstream":
        payload.update(_upstream_view(model, output, size, state, refname))
    elif view == "raw":
        payload.update(_raw_view(machine, model, seq, size))
    else:
        raise ValueError("view must be graph, upstream or raw (got %r)" % (view,))
    if not payload.get("parameters"):
        payload["parameters"] = _parameter_definitions(machine, refname)
    return payload


def _resolve(machine, device, target, tree) -> tuple[str, str]:
    from .device import read_device

    refname = device
    if not refname and isinstance(target, dict):
        refname = (target.get("device") or {}).get("refname")
    if not refname:
        raise ValueError("pass device (RefName), or a target with device.refname")
    clock_tree = tree
    if not clock_tree:
        clock_tree = read_device(machine, refname).clock_tree
    if not clock_tree:
        raise ValueError("device %s declares no clock tree in its XML" % refname)
    return refname, clock_tree


def _graph_view(model: ClockModel, size: int,
                state: TargetState | None = None) -> dict:
    nodes = model.nodes[:size]
    table = _raw_index(model)
    json_nodes = [_node_json(model, node, state=state, index=table)
                  for node in nodes]
    connections = []
    omitted = []
    for connection in model.connections:
        if len(connections) >= size:
            omitted.append({"source_ref": connection["source"],
                            "target_ref": connection["target"],
                            "signal_id": connection["signal_id"]})
            continue
        directions = sorted({row["direction"]
                             for row in connection["declarations"]})
        connections.append({
            "source_ref": connection["source"],
            "target_ref": connection["target"],
            "signal_id": connection["signal_id"],
            "direction": directions[0] if len(directions) == 1 else "both",
            "directions": directions,
            "resolution": connection["resolution"],
            "candidates": list(connection["candidates"]),
            "declarations": list(connection["declarations"]),
        })
    return {
        "nodes": json_nodes,
        "node_total": len(model.nodes),
        "not_expanded": [_node_ref(n) for n in model.nodes[size:]],
        "connections": connections,
        "connections_total": len(model.connections),
        "edges_total": len(model.edges),
        "connections_omitted": omitted,
        "not_expanded_total": max(0, len(model.nodes) - size),
    }


def _targets(model: ClockModel, output: str | None,
             device: str | None = None) -> list[ClockNode]:
    if not output:
        raise ValueError("view=upstream needs output "
                         "(a readout parameter name or an element id)")
    wanted = str(output)
    found = [n for n in model.nodes if n.id == wanted]
    if not found:
        found = [n for n in model.nodes
                 if n.id.rsplit("/", 1)[-1] == wanted]
    if not found:
        lowered = wanted.lower()
        found = [n for n in model.nodes
                 if n.ref_parameter and lowered in
                 [part.strip().lower() for part in
                  str(n.ref_parameter).replace("/", ",").split(",")]]
    if not found:
        names = set()
        for node in model.nodes:
            names.add(node.id.rsplit("/", 1)[-1])
            for part in str(node.ref_parameter or "").replace("/", ",").split(","):
                if part.strip():
                    names.add(part.strip())
        near = suggest(wanted, sorted(names))
        raise ValueError("no clock element matches %r%s. Give a readout parameter "
                         "name (for example HCLKFreq_Value) or an element id"
                         % (wanted,
                            suggest_clause("clock readouts of %s" % device
                                           if device else "clock readouts", near)))
    return found


def _predecessor_edges(model: ClockModel, node: ClockNode) -> list[tuple]:
    """Every (source node, input declaration, connection) that feeds `node`.

    A MUX contributes **all** its branches (the input declaration is what names a
    branch, even when the upstream element does not repeat it as an Output); that is
    the point of the MUX being a choice, not simultaneous inputs.
    """
    out = []
    for connection in model.connections:
        if connection["target"] != node.id:
            continue
        declaration = next((row for row in connection["declarations"]
                            if row.get("direction") == "input"), None)
        if declaration is None:
            continue
        source = next((n for n in model.nodes if n.id == connection["source"]),
                      None)
        if source is None:
            continue
        out.append((source, declaration, connection))
    out.sort(key=lambda row: row[1].get("seq", 0))
    return out


def _input_role(node: ClockNode, source: ClockNode, index: int) -> str:
    if node.type == "multiplexor":
        return "branch"
    if node.type == "multiplicatorFrac" and source.type in FRACTIONAL_INPUT_TYPES:
        return "fractional"
    return "frequency" if source.type and source.type.endswith("Source") else "input"


def _upstream_view(model: ClockModel, output: str | None, size: int,
                   state: TargetState | None = None,
                   device: str | None = None) -> dict:
    table = _raw_index(model)
    targets = _targets(model, output, device=device)
    visited: dict[str, int] = {}
    order: list[ClockNode] = []
    edges_out: list[dict] = []
    roles: dict[str, set] = {}
    cut: list[str] = []
    paths: list[list[dict]] = []
    max_paths = max(1, min(size, 32))

    def walk(node: ClockNode, path: list[dict], seen: set[str]):
        if len(paths) >= max_paths:
            cut.append(_node_ref(node))
            return
        if len(visited) >= size:
            cut.append(_node_ref(node))
            return
        if node.id not in visited:
            visited[node.id] = len(order)
            order.append(node)
        here = {"node_ref": _node_ref(node), "id": node.id}
        predecessors = _predecessor_edges(model, node)
        if (node.type in ("fixedSource", "variedSource", "distinctValsSource",
                          "pixelClockSource") or not predecessors
                or len(path) > size):
            paths.append(path + [here])
            return
        for index, (source, declaration, connection) in enumerate(predecessors):
            role = _input_role(node, source, index)
            roles.setdefault(node.id, set()).add(role)
            edge_payload = {
                "source_ref": connection["source"],
                "target_ref": connection["target"],
                "signal_id": connection["signal_id"],
                "resolution": connection["resolution"],
                "role": role,
                "declaration": declaration,
            }
            if edge_payload not in edges_out:
                edges_out.append(edge_payload)
            if source.id in seen:
                # A cycle in the definition file: stop here and name it instead of
                # walking forever. The raw layer keeps the declaration either way.
                paths.append(path + [here,
                                     {"node_ref": _node_ref(source),
                                      "id": source.id, "cycle": True}])
                continue
            walk(source, path + [here], seen | {node.id})

    for source_target in targets:
        walk(source_target, [], {source_target.id})

    used_refs = {_node_ref(n) for n in order}
    for node in order:
        if node.id not in roles:
            roles[node.id] = set()
    return {
        "output": output,
        "target_refs": [_node_ref(n) for n in targets],
        "target_ids": [n.id for n in targets],
        "nodes": [_node_json(model, node,
                             role="+".join(sorted(roles.get(node.id, []))) or None,
                             state=state, index=table)
                  for node in order],
        "node_total": len(order),
        "connections": edges_out,
        "paths": paths,
        "path_total": len(paths),
        "paths_truncated": bool(cut) or len(paths) >= max_paths,
        "not_expanded": sorted(set(cut) - used_refs),
        "all_paths_complete": not cut and len(paths) < max_paths,
    }


def _raw_view(machine, model: ClockModel, seq: int | None, size: int) -> dict:
    path = Path(model.source)
    if not path.is_absolute():
        path = Path(machine.db) / path
    if seq is None:
        text = path.read_text(encoding="utf-8")
        return {
            "raw": {"source": model.source, "seq": None,
                    "xml": text[:size] if size else text,
                    "chars": len(text),
                    "truncated": bool(size) and len(text) > size},
        }
    index = int(seq)
    if not (0 <= index < len(model.raw)):
        raise ValueError("raw record %d is outside 0..%d"
                         % (index, len(model.raw) - 1))
    fragment = _render(model, index)
    return {"raw": {"source": model.source, "seq": index,
                    "fragment": fragment[:size] if size else fragment,
                    "chars": len(fragment),
                    "truncated": bool(size) and len(fragment) > size}}


def _render(model: ClockModel, seq: int) -> str:
    """Reconstruct one element from its raw record (local tag names only)."""
    record = model.raw[seq]
    attrs = "".join(' %s="%s"' % (name, _escape(value))
                    for name, value in record.attrs.items())
    head = "<%s%s" % (record.tag, attrs)
    if not record.children and not record.text:
        return head + "/>" + (record.tail or "")
    body = _escape(record.text or "")
    for child in record.children:
        body += _render(model, child)
    return head + ">" + body + "</%s>" % (record.tag,) + (record.tail or "")


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
