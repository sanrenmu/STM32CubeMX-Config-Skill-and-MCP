"""Pin <-> signal tables read from the db (no CubeMX, no project state).

Two directions of ambiguity come from the device XML's `<Pin Name="X"><Signal
Name="Y"/></Pin>`: one pin can carry several signals, and one signal can land on
several pins. The modes XML says which signals a mode needs, but the signal
declarations sit on different levels of the mode tree (an FMC leaf like
`8b-d1` has no signal list of its own; D0..D7 are declared on its grandparent
`Data1`), so the required set is the union over the whole root -> leaf path.

This module is the data layer of one query and of the compiler's pin gate. It
must exist once: parsing the same XML twice gives two answers.

Nothing here is hardcoded per family or peripheral: the prefix comes from the IP
name in the device XML, the signals from the modes file, the pins from the
device file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..ipxml import find_ip, modes_xml_path
from .device import read_device


def _local(tag: str) -> str:
    """Local name of a namespaced db tag."""
    return tag.rsplit("}", 1)[-1]


def pin_signals(machine, refname: str) -> dict:
    """The device XML's pin -> [signal names] table (only pins with signals).

    The bare `GPIO` signal is dropped: every IO pin carries it as its generic
    identity, not as a pin function.
    """
    out: dict = {}
    for pin in read_device(machine, refname).pins:
        signals = []
        for name in pin.signals:
            if name and name != "GPIO" and name not in signals:
                signals.append(name)
        if pin.name and signals:
            out[pin.name] = signals
    return out


def peripheral_signal_map(machine, refname: str, peripheral: str,
                          prefix: str | None = None) -> dict:
    """One peripheral's signals and their candidate pins, both directions.

    The scan is by signal name prefix (`<IP name>_`), which is how the db spells
    peripheral signals. A name that no device signal carries is not silently an
    empty set: the caller gets the prefixes that do exist so it can suggest one.
    """
    want = (prefix or peripheral).upper()
    pin_signals_map = pin_signals(machine, refname)
    hit_pins: list = []
    sig_to_pins: dict = {}
    for pin_name in sorted(pin_signals_map):
        mine = [name for name in pin_signals_map[pin_name]
                if name.upper().startswith(want + "_")]
        if not mine:
            continue
        hit_pins.append({"pin": pin_name, "signals": mine,
                         "ambiguous": len(mine) > 1})
        for name in mine:
            sig_to_pins.setdefault(name, []).append(pin_name)
    known = sorted({name.split("_", 1)[0] for names in pin_signals_map.values()
                    for name in names if "_" in name})
    return {
        "prefix": want,
        "pins": hit_pins,
        "signals": sorted(({"signal": key, "pins": pins}
                           for key, pins in sig_to_pins.items()),
                          key=lambda item: item["signal"]),
        "pin_with_many_signals": [item for item in hit_pins if item["ambiguous"]],
        "signal_on_many_pins": sorted(
            ({"signal": key, "pins": pins}
             for key, pins in sig_to_pins.items() if len(pins) > 1),
            key=lambda item: item["signal"]),
        "pin_count": len(hit_pins),
        "signal_count": len(sig_to_pins),
        "known_prefixes": known,
    }


_TREE_CACHE: dict = {}


def _iter_local(root, name: str):
    for element in root.iter():
        if _local(element.tag) == name:
            yield element


def _children_local(element, name: str):
    return [child for child in element if _local(child.tag) == name]


def _mode_tree(machine, refname: str, ip_name: str):
    key = (refname, ip_name)
    if key in _TREE_CACHE:
        return _TREE_CACHE[key]
    box = None
    entry = find_ip(machine, refname, ip_name)
    if entry is not None:
        path = modes_xml_path(machine, entry)
        if path is not None and path.exists():
            root = ET.parse(str(path)).getroot()
            parent = {child: node for node in root.iter() for child in node}
            box = (root, parent)
    _TREE_CACHE[key] = box
    return box


def mode_required_signals(machine, refname: str, ip_name: str,
                          mode_name: str) -> list | None:
    """Full signal names a leaf mode needs (union over the root -> leaf path).

    None means it cannot be decided offline (no IP entry, no modes file, or no
    leaf with that name): the caller says so instead of guessing.
    """
    box = _mode_tree(machine, refname, ip_name)
    if box is None:
        return None
    root, parent = box
    leaves = [mode for mode in _iter_local(root, "Mode")
              if mode.get("Name") == mode_name
              and not _children_local(mode, "Mode")]
    if not leaves:
        return None
    signals: set = set()
    for leaf in leaves:
        node = leaf
        while node is not None:
            if _local(node.tag) == "Mode":
                for operator in _children_local(node, "SignalLogicalOp"):
                    for signal in _children_local(operator, "Signal"):
                        name = signal.get("Name")
                        if not name:
                            continue
                        signals.add(name if name.startswith(ip_name + "_")
                                    else ip_name + "_" + name)
            node = parent.get(node)
    return sorted(signals)


def mode_pin_map(machine, refname: str, ip_name: str, mode_name: str,
                 prefix: str | None = None) -> dict | None:
    """A mode's required signals plus each signal's candidate pins.

    The result also marks the two ambiguities the writer has to resolve:
    a signal with several pins, and two required signals that only share one
    pin. A signal whose pins the device XML does not declare is reported in
    `missing`, never dropped.

    None means offline-undecidable; the caller must not read it as "no pins".
    """
    need = mode_required_signals(machine, refname, ip_name, mode_name)
    if need is None:
        return None
    peripheral = peripheral_signal_map(machine, refname, ip_name, prefix)
    sig_to_pins = {item["signal"]: item["pins"] for item in peripheral["signals"]}
    multi_pins = {item["pin"] for item in peripheral["pins"] if item["ambiguous"]}

    rows, ambiguous, missing = [], [], []
    for signal in need:
        pins = list(sig_to_pins.get(signal) or [])
        if not pins:
            missing.append(signal)
            continue
        row = {
            "signal": signal,
            "pins": pins,
            "pin_count": len(pins),
            "ambiguous": len(pins) > 1,
            "pins_with_many_signals": [pin for pin in pins if pin in multi_pins],
        }
        rows.append(row)
        if row["ambiguous"]:
            ambiguous.append(row)

    by_pin: dict = {}
    for row in rows:
        if row["pin_count"] == 1:
            by_pin.setdefault(row["pins"][0], []).append(row["signal"])
    conflicts = sorted(({"pin": pin, "signals": sorted(names)}
                        for pin, names in by_pin.items() if len(names) > 1),
                       key=lambda item: item["pin"])

    return {
        "signals": rows,
        "ambiguous": ambiguous,
        "conflicts": conflicts,
        "missing": missing,
        "required_count": len(need),
    }
