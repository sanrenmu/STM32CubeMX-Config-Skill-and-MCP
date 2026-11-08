"""Read the MCU XML: pins, signals, IP instances, contexts.

Read only. The current state is not evaluated.
"""

from __future__ import annotations

from pathlib import Path

from .model import Condition, Device, IpEntry, Pin, Signal
from .modes import tokens_of
from .xmlio import attr, bool_attr, children, first, local, parse

KNOWN_ROOT = {
    "Core", "Frequency", "CCMRam", "MemoryMap", "Ram", "IONb", "Die", "Flash",
    "Voltage", "Current", "Temperature", "E2prom", "TrustZone", "LPBAM",
    "BootPath", "Context", "ContextProject", "GentypeFirmware", "IP", "Pin",
    "Signal", "RCC", "DBVersion",
}


def _rel(machine, path: Path) -> str:
    try:
        return str(path.relative_to(machine.db))
    except ValueError:
        return str(path)


def _signal_aliases(name: str, io_modes: str | None) -> list[str]:
    """One Pin/Signal in the MCU XML can stand for several IO modes.

    CubeMX rule: when IOModes is present, build the signal name as
    `<Name>_<Mode>`. For example GPIO + Input yields GPIO_Input.
    """
    out = [name]
    if io_modes:
        for tok in io_modes.split(","):
            tok = tok.strip()
            if not tok:
                continue
            low = tok.lower()
            if low.startswith("event"):
                alias = tok
            else:
                alias = name + "_" + tok
            if alias not in out:
                out.append(alias)
    return out


def read_device(machine, refname: str) -> Device:
    from ..ipxml import device_xml

    path = device_xml(machine, refname)
    root = parse(path)
    if local(root.tag) != "Mcu":
        raise ValueError("the device XML root element is not Mcu: %s" % path)

    dev = Device(
        refname=attr(root, "RefName") or refname,
        family=attr(root, "Family"),
        line=attr(root, "Line"),
        package=attr(root, "Package"),
        die=None,
        clock_tree=attr(root, "ClockTree"),
        db_version=attr(root, "DBVersion"),
        source=_rel(machine, path),
    )

    signal_map: dict[str, Signal] = {}
    pin_by_name: dict[str, Pin] = {}

    def add_signal(name: str, pin: Pin, io_modes: str | None,
                   direction: str | None):
        sig = signal_map.get(name)
        if sig is None:
            sig = Signal(name=name, io_modes=[], direction=direction,
                         source=dev.source)
            signal_map[name] = sig
        if pin.name not in sig.pins:
            sig.pins.append(pin.name)
        if io_modes:
            for tok in io_modes.split(","):
                tok = tok.strip()
                if tok and tok not in sig.io_modes:
                    sig.io_modes.append(tok)

    for el in children(root):
        tag = local(el.tag)
        if tag == "Die":
            dev.die = (el.text or "").strip() or None
        elif tag == "Context":
            dev.contexts.append({
                "name": attr(el, "Name"),
                "long_name": attr(el, "LongName"),
                "short_name": attr(el, "ShortName"),
                "core": attr(el, "Core"),
                "secure": bool_attr(el, "Secure"),
                "gen_type": attr(el, "GenType"),
                "group_name": attr(el, "GroupName"),
                "group_short_name": attr(el, "GroupShortName"),
                "power_domain": attr(el, "PowerDomain"),
            })
        elif tag == "IP":
            inst = attr(el, "InstanceName")
            name = attr(el, "Name")
            ver = attr(el, "Version")
            if inst and name and ver:
                dev.ips.append(IpEntry(
                    instance=inst, name=name, version=ver,
                    attrs=dict(el.attrib), source=dev.source))
        elif tag == "Pin":
            raw_name = attr(el, "Name") or ""
            # The pin name is the name from the XML; do not cut it: project files
            # use the full name (measured on an H7 board seed: `PB3 (JTDO/TRACESWO)`,
            # `PC14-OSC32_IN (OSC32_IN)`). The target JSON may use the short name
            # (PB3); pin_by_name maps it to the full name.
            name = raw_name.strip()
            pin = pin_by_name.get(name)
            if pin is not None:
                # The same pin name may appear more than once (power pins).
                # Merge the positions.
                pos = attr(el, "Position")
                if pos and pos not in pin.positions:
                    pin.positions.append(pos)
                continue
            pos0 = attr(el, "Position")
            pin = Pin(
                name=name,
                position=pos0,
                positions=[pos0] if pos0 else [],
                type=attr(el, "Type"),
                source=dev.source,
            )
            pin_by_name[name] = pin
            for cond in children(el, "Condition"):
                raw = attr(cond, "Expression")
                pin.conditions.append(Condition(
                    raw=raw,
                    diagnostic=attr(cond, "Diagnostic"),
                    tokens=tokens_of(raw),
                ))
            for sig_el in children(el, "Signal"):
                sig_name = attr(sig_el, "Name")
                if not sig_name:
                    continue
                io_modes = attr(sig_el, "IOModes")
                direction = attr(sig_el, "Direction")
                for alias in _signal_aliases(sig_name, io_modes):
                    add_signal(alias, pin, io_modes, direction)
                    if alias not in pin.signals:
                        pin.signals.append(alias)
            dev.pins.append(pin)
        elif tag not in KNOWN_ROOT:
            dev.unhandled.append({"tag": tag, "source": dev.source})

    dev.signals = list(signal_map.values())
    return dev
