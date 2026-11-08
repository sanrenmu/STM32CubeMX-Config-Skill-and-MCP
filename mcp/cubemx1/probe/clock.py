"""Read the clock tree XML: a raw layer plus an index over it.

Read only. Frequencies are not computed, no current state is evaluated, CubeMX is
not started.

Key points:

- A Tree can be nested (for example ClockTree holds PLL1/PLL2/PLL3).
- Output@to and Input@from reference element ids. An id may cross subtrees, and ids
  may repeat.
- `raw` keeps every element of the file in document order: tag, all attributes,
  text, tail and the direct children. The index (`nodes`, `edges`) points at
  those records; it never replaces them.
- A reference that matches more than one element is reported with **all** candidates;
  the first one is not silently chosen as "the" answer.
"""

from __future__ import annotations

from pathlib import Path

from .model import ClockEdge, ClockModel, ClockNode, Condition, RawElement
from .modes import tokens_of
from .xmlio import attr, bool_attr, children, local, parse

KNOWN_ROOT = {"Tree", "Signals", "Signal", "Label", "BButton", "Background"}

#: Element types declared by the clock definition XML. Used only to report what the
#: compact index does not interpret; an unknown type is kept, never dropped.
KNOWN_ELEMENT_TYPES = {
    "fixedSource", "variedSource", "distinctValsSource", "pixelClockSource",
    "devisor", "multiplicator", "multiplicatorFrac", "fractional",
    "multiplexor", "xbar", "output", "activeOutput",
}


def _conditions(el) -> list[Condition]:
    """Every Condition child, in document order (never only the first one)."""
    out = []
    for child in children(el, "Condition"):
        raw = attr(child, "Expression")
        out.append(Condition(raw=raw, diagnostic=attr(child, "Diagnostic"),
                             action=attr(child, "Action"),
                             tokens=tokens_of(raw)))
    return out


def read_clock(machine, tree_name: str) -> ClockModel:
    path = Path(machine.db) / "plugins" / "clock" / (tree_name + ".xml")
    root = parse(path)
    model = ClockModel(name=tree_name, source=_rel_path(machine, path))
    raw: list[RawElement] = []
    nodes: list[ClockNode] = []

    def walk(el, parent_seq: int | None, tree_path: list[str],
             tree_id: str | None) -> int:
        tag = local(el.tag)
        attrs = dict(el.attrib)
        own_id = attrs.get("id")
        self_path = list(tree_path)
        if own_id:
            self_path.append(own_id)
        elif tag == "Tree":
            self_path.append("tree")
        seq = len(raw)
        raw.append(RawElement(
            seq=seq, path=self_path, tag=tag, attrs=attrs,
            text=el.text, tail=el.tail, parent=parent_seq))
        if parent_seq is not None:
            raw[parent_seq].children.append(seq)
        next_tree = tree_id
        if tag == "Tree":
            next_tree = own_id or "tree"
        elif tag == "Element":
            child_tags = [local(x.tag) for x in list(el)]
            inputs = [dict(x.attrib) for x in children(el, "Input")]
            outputs = [dict(x.attrib) for x in children(el, "Output")]
            if own_id:
                nodes.append(ClockNode(
                    id="/".join(self_path),
                    tree=next_tree or "",
                    type=attrs.get("type"),
                    ref_parameter=attrs.get("refParameter"),
                    ref_enable=attrs.get("refEnable"),
                    ref_secure=attrs.get("refSecure"),
                    ref_lock=attrs.get("refLock"),
                    is_key=bool(bool_attr(el, "isKey")),
                    conditions=_conditions(el),
                    inputs=inputs,
                    outputs=outputs,
                    path=list(tree_path),
                    source=model.source,
                    seq=seq,
                    attrs=attrs,
                    child_tags=child_tags,
                ))
            else:
                model.unhandled.append({"tag": "Element(no id)",
                                        "tree": "/".join(tree_path)})
        elif tag == "Signal":
            model.signal_entries.append({"seq": seq, "id": own_id, "attrs": attrs})
            if own_id:
                model.signals.append(own_id)
        else:
            # A direct child of a Tree that is not an Element/Tree is a coverage gap
            # of the compact index (the raw layer keeps it either way). Do not report
            # the grandchildren of elements (Input/Output/Condition): those are part
            # of their parent element.
            parent_tag = raw[parent_seq].tag if parent_seq is not None else None
            if parent_tag == "Tree":
                model.unhandled.append({"tag": tag,
                                        "tree": "/".join(tree_path)})
        for child in list(el):
            walk(child, seq, self_path, next_tree)
        return seq

    for el in children(root):
        tag = local(el.tag)
        walk(el, None, [], None)
        if tag not in KNOWN_ROOT and tag != "Tree":
            model.unhandled.append({"tag": tag})
    model.raw = raw
    model.nodes = nodes
    # declaration records, in document order (the Input/Output elements themselves)
    for node in nodes:
        record = raw[node.seq] if 0 <= node.seq < len(raw) else None
        if record is None:
            continue
        for child_seq in record.children:
            tag = raw[child_seq].tag
            if tag == "Input":
                node.input_seqs.append(child_seq)
            elif tag == "Output":
                node.output_seqs.append(child_seq)

    by_raw: dict[str, list[ClockNode]] = {}
    for n in nodes:
        raw_id = n.id.rsplit("/", 1)[-1]
        by_raw.setdefault(raw_id, []).append(n)

    def resolve(raw_id: str | None, same_tree: str | None):
        if not raw_id:
            return [], "empty"
        got = by_raw.get(raw_id, [])
        if not got:
            return [], "missing"
        if len(got) == 1:
            return got, "ok"
        same = [n for n in got if n.tree == same_tree]
        if len(same) == 1:
            return same, "same-tree"
        return got, "ambiguous"

    by_key: dict[tuple, dict] = {}
    for n in nodes:
        for index, out in enumerate(n.outputs):
            raw_to = out.get("to")
            candidates, status = resolve(raw_to, n.tree)
            target = candidates[0].id if candidates else "CLOCK/?" + str(raw_to)
            key = (n.id, target, out.get("signalId"))
            _add_declaration(model, by_key, n, out, index, key, candidates, status,
                             "output", raw_to)
        for index, inp in enumerate(n.inputs):
            raw_from = inp.get("from")
            candidates, status = resolve(raw_from, n.tree)
            source = candidates[0].id if candidates else "CLOCK/?" + str(raw_from)
            key = (source, n.id, inp.get("signalId"))
            _add_declaration(model, by_key, n, inp, index, key, candidates, status,
                             "input", raw_from)
    # The compact forward graph keeps what the file declares with an Output (that is
    # how the graph was always drawn, and it stays acyclic). An input-only
    # declaration still forms a connection below: it is a raw fact, and it is shown
    # with the direction it was declared in instead of being turned into a fake edge.
    for connection in model.connections:
        if not any(row["direction"] == "output"
                   for row in connection["declarations"]):
            continue
        model.edges.append(ClockEdge(
            source=connection["source"],
            target=connection["target"],
            signal_id=connection["signal_id"],
            resolved=connection["resolution"] in ("ok", "same-tree"),
            tree=connection["tree"],
            resolution=connection["resolution"],
            candidates=list(connection["candidates"]),
            declarations=list(connection["declarations"]),
        ))

    return model


def _rel_path(machine, path: Path) -> str:
    try:
        return str(path.relative_to(machine.db))
    except ValueError:
        return str(path)


def _add_declaration(model: ClockModel, by_key: dict, node: ClockNode,
                     decl: dict, index: int, key: tuple, candidates: list,
                     status: str, direction: str, other_raw: str | None) -> None:
    """Record one Input/Output declaration and merge it onto its connection.

    A connection keeps **both** declarations (the Output@to side and the Input@from
    side); an input-only declaration is a connection too, marked with the direction it
    was declared in. When the raw id matches more than one element, every candidate is
    listed; the stored endpoint is only the first candidate so the connection stays
    addressable, and `resolution` says it is ambiguous.
    """
    source_id = (node.id if direction == "output" else
                 (candidates[0].id if candidates else "CLOCK/?" + str(other_raw)))
    target_id = (node.id if direction == "input" else
                 (candidates[0].id if candidates else "CLOCK/?" + str(other_raw)))
    candidate_ids = [candidate.id for candidate in candidates]
    connection = by_key.get(key)
    if connection is None:
        connection = {
            "source": source_id,
            "target": target_id,
            "signal_id": decl.get("signalId"),
            "tree": node.tree,
            "resolution": status,
            "candidates": candidate_ids,
            "declarations": [],
        }
        by_key[key] = connection
        model.connections.append(connection)
    else:
        if (status in ("ok", "same-tree")
                and connection["resolution"] not in ("ok", "same-tree")):
            connection["resolution"] = status
        for candidate_id in candidate_ids:
            if candidate_id not in connection["candidates"]:
                connection["candidates"].append(candidate_id)
    seqs = node.output_seqs if direction == "output" else node.input_seqs
    decl_seq = seqs[index] if 0 <= index < len(seqs) else node.seq
    connection["declarations"].append({"seq": decl_seq, "direction": direction,
                                       "attrs": dict(decl)})
    if status in ("missing", "empty", "ambiguous"):
        model.unresolved.append({
            "declaration_seq": decl_seq,
            "element": node.id,
            "direction": direction,
            "reference": other_raw,
            "candidates": [c.id for c in candidates],
            "reason": status,
        })
        if status == "ambiguous":
            model.warnings.append(
                "clock element id is ambiguous: %s -> %s matches %d elements (%s)"
                % (node.id, other_raw, len(candidates),
                   ", ".join(c.id for c in candidates[:4])))
        elif status == "missing":
            model.warnings.append(
                "clock %s has no matching element: %s -> %s"
                % (direction, node.id, other_raw))


def topology_summary(model: ClockModel) -> dict:
    """Topology summary for tests and commands."""
    ids = {n.id for n in model.nodes}
    outgoing = {e.source for e in model.edges}
    incoming = {e.target for e in model.edges if e.resolved}
    unresolved = [e for e in model.edges if not e.resolved]
    trees: dict[str, int] = {}
    for n in model.nodes:
        trees[n.tree] = trees.get(n.tree, 0) + 1
    return {
        "name": model.name,
        "nodes": len(model.nodes),
        "edges": len(model.edges),
        "resolved_edges": len(model.edges) - len(unresolved),
        "unresolved_edges": len(unresolved),
        "roots": sorted(ids - incoming),
        "sinks": sorted(ids - outgoing),
        "trees": trees,
        "signals": len(model.signals),
        "raw_elements": len(model.raw),
        "connections": len(model.connections),
        "input_only_connections": sum(
            1 for c in model.connections
            if not any(d["direction"] == "output" for d in c["declarations"])),
        "ambiguous_edges": sum(1 for e in model.edges
                               if e.resolution == "ambiguous"),
        "warnings": model.warnings,
    }
