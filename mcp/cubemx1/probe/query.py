"""Shared implementation of the probe queries: both the CLI and the MCP layer call
only this module, so the logic exists once.

The applicable definition is selected by `resolve.py`, the same code the
compiler uses; this module only assembles "device + target selection" into an
evaluation state and projects it to JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..util import suggest, suggest_clause
from . import boards as boards_query
from . import resolve
from .catalog import load_model


def _read_target(target) -> dict:
    if target is None:
        return {}
    if isinstance(target, dict):
        return target
    text = Path(str(target)).read_text(encoding="utf-8")
    return json.loads(text)


def _selection_from_target(target: dict, instance: str | None) -> dict:
    signals = []
    pins = {}
    params = {}
    for item in target.get("pins") or []:
        if item.get("signal"):
            signals.append(item["signal"])
        if item.get("pin"):
            pins[item["pin"]] = bool(item.get("locked"))
    for item in target.get("ips") or []:
        if instance and item.get("instance") != instance:
            continue
        for name, value in (item.get("params") or {}).items():
            params[(item["instance"], name)] = value
    return {"signals": signals, "pins": pins, "params": params}


def describe(machine, device: str | None = None, target=None, ip: str | None = None,
             instance: str | None = None, board: str | None = None) -> dict:
    """Describe the definition of one IP: modes, parameters, applicable definition,
    three-valued conditions and pin candidates.

    When board is given, the query and the compiler share one board context: the
    device comes from the board record, and the BSP instance (instance name =
    board name) is queried through its own Modes file.
    """
    target_doc = _read_target(target)
    refname = device or (target_doc.get("device") or {}).get("refname")
    board_ctx = None
    if board:
        board_ctx = boards_query.board_context(machine, board)
        refname = refname or board_ctx.get("mounted_device")
    if not refname:
        raise ValueError("pass --device / --board, or a --target with device.refname")
    wanted = ip or instance
    if not wanted:
        raise ValueError("pass --ip (for example USART1)")
    model = load_model(machine, refname, ip_filter=[wanted], board=board)
    found = None
    for item in model.ips:
        if item.instance == wanted or item.name == wanted:
            found = item
            break
    if found is None:
        from .device import read_device

        entries = read_device(machine, refname).ips
        known = sorted({entry.instance for entry in entries}
                       | {entry.name for entry in entries})
        near = suggest(wanted, known)
        raise KeyError("%s is not defined on %s%s"
                       % (wanted, refname,
                          suggest_clause("IP instances and names declared by %s"
                                         % refname, near)))
    selection = _selection_from_target(target_doc, found.instance)
    state = resolve.ModelState(ip=found, device=model.device, selection=selection,
                               instance=found.instance)
    payload = resolve.describe(found, state, instance=found.instance)
    payload["device"] = refname
    payload["ip_type"] = found.ip_type
    if board_ctx is not None:
        payload["board"] = board_ctx["id"]
    payload["selected_from"] = "target" if target_doc else "definitions-only"
    return payload


def clock(machine, device: str | None = None, target=None,
          output: str | None = None, view: str = "graph",
          size: int | None = None, seq: int | None = None,
          tree: str | None = None) -> dict:
    """Clock topology of one device: graph, upstream of one readout, or raw XML.

    The same handler serves the CLI and the MCP layer. It reads the device XML and
    the clock definition XML only; no frequency is solved and CubeMX is not started.
    """
    from .clocktopo import clock_topology

    target_doc = _read_target(target)
    return clock_topology(machine, device=device, target=target_doc or None,
                          output=output, view=view, size=size, seq=seq,
                          tree=tree)


def boards(machine, keyword: str | None = None, limit: int = 50) -> dict:
    items = boards_query.list_boards(machine, keyword, limit=limit)
    overview = boards_query.seed_overview(machine)
    return {"ok": True, "keyword": keyword, "count": len(items),
            "boards": items, "seed_files": overview["total"],
            "seed_files_unmatched": overview["unmatched"]}


def _seed_config(machine, seed) -> dict:
    """Basic configuration summary of a board seed: used instances/modes, pins and
    clock inputs.

    Parsing uses the iocc importer (the same rules as the compiler); there is no
    separate board configuration parser.
    """
    from ..iocc import import_ioc  # delayed: avoid importing iocc at probe import

    target = import_ioc.import_ioc(machine, seed)["target"]
    ips = []
    for entry in target.get("ips") or []:
        modes = [item.get("name") for item in (entry.get("modes") or [])
                 if item.get("name")]
        if not modes and entry.get("mode"):
            modes = [entry["mode"]]
        ips.append({"instance": entry["instance"], "modes": modes,
                    "params": dict(entry.get("params") or {})})
    pins = [{"pin": item.get("pin"), "signal": item.get("signal")}
            for item in target.get("pins") or []]
    clock = dict(((target.get("clock") or {}).get("params")) or {})
    clock_inputs = {name: value for name, value in clock.items()
                    if name.endswith("_VALUE") and "Freq" not in name
                    and not value.startswith("=")}
    return {"seed": Path(str(seed)).name,
            "ips": ips,
            "pins": pins,
            "clock_inputs": clock_inputs}


def board(machine, wanted: str) -> dict:
    ctx = boards_query.board_context(machine, wanted)
    detail = ctx["detail"]
    item = dict(detail) if detail else {
        "name": ctx["name"],
        "revision": None,
        "vendor": None,
    }
    item["id"] = detail["id"] if detail else ctx["id"]
    item.setdefault("mounted_device", ctx.get("mounted_device"))
    item["resolved"] = detail is not None
    item["ambiguous"] = ctx["ambiguous"]
    item["seeds"] = list(ctx.get("seeds") or [])
    item["has_bsp"] = ctx["has_bsp"]
    if ctx.get("seed") is not None:
        try:
            item["config"] = _seed_config(machine, ctx["seed"])
        except Exception as exc:  # report, never hide a bad seed as empty
            item["config"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    if ctx.get("has_bsp"):
        try:
            item["bsp"] = describe(machine, board=ctx["id"],
                                   ip=ctx["name"])
        except Exception as exc:
            item["bsp"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "board": item}

def peripherals(machine, device: str | None = None, target=None,
                board: str | None = None) -> dict:
    """Every peripheral/middleware instance the device declares (one device at a time).

    The list comes from the device XML's `<IP>` entries (the same data CubeMX's
    device page is built from). `type` is the `IPType` of the modes file when it
    can be read; a missing modes file is reported as `None` and counted, never
    dropped.
    """
    from ..ipxml import ip_type, modes_xml_path
    from .device import read_device

    target_doc = _read_target(target)
    refname = device or (target_doc.get("device") or {}).get("refname")
    if not refname and board:
        refname = boards_query.board_context(machine, board).get("mounted_device")
    if not refname:
        raise ValueError("pass --device / --board, or a --target with device.refname")

    device_model = read_device(machine, refname)
    rows = []
    for entry in device_model.ips:
        path = modes_xml_path(machine, entry)
        kind = None
        if path is not None and path.is_file():
            try:
                kind = ip_type(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001 - report the instance, type stays None
                kind = None
        rows.append({
            "instance": entry.instance,
            "name": entry.name,
            "version": entry.version,
            "type": kind,
            "attrs": dict(entry.attrs),
        })
    rows.sort(key=lambda row: row["instance"].upper())
    missing = [row["instance"] for row in rows if row["type"] is None]
    return {"ok": True, "device": refname, "count": len(rows),
            "peripherals": rows,
            "missing_modes_file": missing,
            "note": "the list is the device definition's IP inventory; "
                    "missing_modes_file means the XML declares the instance but no "
                    "modes file was found, so its parameters cannot be enumerated"}


def pins(machine, device: str | None = None, target=None, ip: str | None = None,
         mode: str | None = None, board: str | None = None,
         limit: int = 50) -> dict:
    """Pin choices of one IP: signals <-> candidate pins, and what a mode needs.

    The data comes from the device XML (pin -> signals) and the modes XML (mode ->
    required signals, union over the root -> leaf path); nothing is hardcoded per
    family. Three scopes:

    - no mode: the whole peripheral (pins/signals in both directions) plus one
      summary row per leaf mode;
    - a leaf mode: the required signals, each one's candidate pins, and the two
      ambiguities a writer has to resolve (signal on several pins, two signals
      sharing one pin);
    - a group mode: its leaf modes, so the caller can pick one.

    Read only: no frequency is solved and CubeMX is not started.
    """
    from . import pinmap

    details = describe(machine, device=device, target=target, ip=ip, board=board)
    refname = details["device"]
    ip_name = details["ip"]
    instance = details["instance"]

    peripheral = pinmap.peripheral_signal_map(machine, refname, instance)
    if not peripheral["pin_count"]:
        near = suggest(ip_name, peripheral["known_prefixes"])
        raise ValueError("no signal of %s carries the prefix %r%s; pass the IP name "
                         "(for example FMC) or a signal prefix"
                         % (instance, peripheral["prefix"],
                            suggest_clause("signal prefixes declared by %s" % refname,
                                           near)))

    modes = details.get("modes") or []
    leaves = [item for item in modes if item.get("is_leaf")]
    wanted = (mode or "").strip()
    if wanted:
        matched = [item for item in modes
                   if item.get("internal_name", "").lower() == wanted.lower()
                   or (item.get("name") or "").lower() == wanted.lower()]
        if not matched:
            known = sorted({item["internal_name"] for item in leaves})
            near = suggest(wanted, known)
            raise ValueError("no mode %r on %s%s; give a leaf mode name (for "
                             "example from probe describe --ip %s), or leave "
                             "--mode out for a per-mode summary"
                             % (wanted, instance,
                                suggest_clause("leaf modes of %s" % instance, near),
                                instance))
        picked = matched[0]
        if picked.get("is_leaf"):
            block = pinmap.mode_pin_map(machine, refname, instance,
                                        picked["internal_name"])
            if block is None:
                raise ValueError("the modes file of %s has no signal table for leaf "
                                 "mode %r; offline-undecidable, not empty"
                                 % (ip_name, picked["internal_name"]))
            return {"ok": True, "device": refname, "instance": instance,
                    "ip": ip_name, "scope": "mode", "mode": picked["internal_name"],
                    "pins": peripheral, "block": block,
                    "note": "pins[] are the candidate pins from the device XML; "
                            "ambiguous means the signal has more than one pin, "
                            "conflicts means two required signals share one pin, "
                            "missing means the device XML declares no pin for it"}
        group_leaves = [item for item in leaves if _under(mode, item, modes)]
        rows = []
        for item in group_leaves:
            block = pinmap.mode_pin_map(machine, refname, instance,
                                        item["internal_name"])
            rows.append(_pin_row(item["internal_name"], block))
        return {"ok": True, "device": refname, "instance": instance,
                "ip": ip_name, "scope": "group", "mode": mode,
                "pins": peripheral, "children": rows,
                "children_total": len(rows),
                "note": "children are the leaf modes under that group; ask again "
                        "with one of them for the full signal table"}

    rows = []
    for item in leaves:
        block = pinmap.mode_pin_map(machine, refname, instance,
                                    item["internal_name"])
        rows.append(_pin_row(item["internal_name"], block))
    shown = rows[:limit] if limit else rows
    return {"ok": True, "device": refname, "instance": instance, "ip": ip_name,
            "scope": "peripheral", "pins": peripheral,
            "mode_summary": shown, "modes_total": len(rows),
            "modes_truncated": len(shown) < len(rows),
            "note": "the peripheral level gives both ambiguity directions; each "
                    "mode row counts what that leaf mode needs and how much of it "
                    "must be written explicitly. Ask again with --mode for the "
                    "full signal table."}


def _under(group: str, item: dict, modes: list) -> bool:
    """Whether a mode is a leaf below the named group (by mode id path)."""
    wanted = str(group).lower()
    node = item
    seen = set()
    while node is not None and node.get("id") not in seen:
        seen.add(node.get("id"))
        internal = str(node.get("internal_name") or "").lower()
        if internal == wanted:
            return True
        node_id = node.get("id") or ""
        if wanted in [part.lower() for part in node_id.split('/')]:
            return True
        parent = node.get("parent")
        node = next((entry for entry in modes if entry.get("id") == parent), None)
    return False


def _pin_row(mode: str, block) -> dict:
    if block is None:
        return {"mode": mode, "decidable": False}
    conflict_signals = {signal for conflict in block["conflicts"]
                        for signal in conflict["signals"]}
    must = sorted({item["signal"] for item in block["ambiguous"]} | conflict_signals)
    return {"mode": mode, "decidable": True,
            "required": block["required_count"],
            "ambiguous": len(block["ambiguous"]),
            "conflicts": len(block["conflicts"]),
            "missing": len(block["missing"]),
            "must_be_explicit": must}


def _entry_for(device_model, instance: str):
    """The device XML IP entry for one instance (exact, case-insensitive)."""
    for entry in device_model.ips:
        if entry.instance.lower() == str(instance).lower():
            return entry
    return None
