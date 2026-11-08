"""Derive the unique host from the selected pins (current entry case: USB_DEVICE -> USB/USB_OTG_FS).

Rules (evidence layering is in the development docs):
- a controller in device mode provides a device semaphore (measured: USB Device mode gives `MW_USB_MW_DEVICE`, USB_OTG_FS Device_Only mode gives `MW_USB_OTG_FS_MW_DEVICE`);
- a middleware instance (such as USB_DEVICE) needs one such enabled controller - this is a convention of this tool; the CubeMX GUI dependency comes from its middleware database, which we do not copy, we only derive the observable result;
- candidates = controllers where all signals required by the device mode are already provided by target pins;
- unique -> add it to ips automatically; several -> keep the one explicitly given, otherwise report 'needs an explicit choice'; none -> report which signals are missing.
"""

from __future__ import annotations

from .errors import IocConflict, IocUnknown

#: middleware classes that need a device-type controller (matched by IP name prefix).
DEVICE_MIDDLEWARE = ("USB_DEVICE",)
#: middleware classes that need a host-type controller.
HOST_MIDDLEWARE = ("USB_HOST",)


def _device_modes(ip):
    """Leaf modes of this IP that provide a device semaphore."""
    out = []
    prefix = "MW_%s_MW_DEVICE" % ip.instance
    for mode in ip.modes:
        if not mode.is_leaf:
            continue
        if prefix in (mode.semaphores or []):
            out.append(mode)
    return out


def _host_modes(ip):
    out = []
    prefix = "MW_%s_MW_HOST" % ip.instance
    for mode in ip.modes:
        if not mode.is_leaf:
            continue
        if prefix in (mode.semaphores or []):
            out.append(mode)
    return out


def _signals_from_pins(pins) -> set:
    return {str(item.get("signal")) for item in pins or [] if item.get("signal")}


def _candidates(model, pins, modes_of):
    signals = _signals_from_pins(pins)
    out = []
    for ip in model.ips:
        if ip.name.upper().startswith(DEVICE_MIDDLEWARE + HOST_MIDDLEWARE):
            continue
        for mode in modes_of(ip):
            needed = set(mode.signals or [])
            if needed and needed <= signals:
                out.append({"instance": ip.instance, "ip": ip.name,
                            "mode": mode.internal_name,
                            "signals": sorted(needed)})
    return out


def derive(model, target) -> tuple:
    """Return (ips list to add, notes). Does not modify the argument."""
    ips = target.get("ips") or []
    pins = target.get("pins") or []
    declared = {item["instance"] for item in ips}
    notes = []
    added = []
    for wanted, modes_of, kind in ((DEVICE_MIDDLEWARE, _device_modes, "device"),
                                   (HOST_MIDDLEWARE, _host_modes, "host")):
        middlewares = [item for item in ips
                       if str(item["instance"]).upper().startswith(wanted)]
        if not middlewares:
            continue
        candidates = _candidates(model, pins, modes_of)
        available = [item for item in candidates if item["instance"] not in declared]
        if not candidates:
            raise IocUnknown(
                "has %s but no controller is in %s mode: either put the data pins in pins or add the controller IP explicitly"
                % (", ".join(item["instance"] for item in middlewares), kind),
                "ips")
        if len(candidates) > 1:
            # keep the explicitly chosen one; do not add a second; otherwise require an explicit choice.
            if not available:
                notes.append("host is already explicit: %s; not adding other candidates"
                             % ", ".join(item["instance"] for item in candidates
                                         if item["instance"] in declared))
                continue
            raise IocUnknown(
                "several controllers satisfy %s mode (%s); the target must name one explicitly"
                % (kind, ", ".join(item["instance"] + "/" + item["mode"]
                                   for item in candidates)), "ips")
        picked = candidates[0]
        # a controller is declared explicitly but its mode disagrees with the pins -> report both inputs.
        for item in ips:
            if item["instance"] == picked["instance"] and item.get("mode"):
                if item["mode"] != picked["mode"]:
                    raise IocConflict(
                        "controller %s has explicit mode %r, but the pins only imply %r"
                        % (picked["instance"], item["mode"], picked["mode"]), "ips")
        if picked["instance"] in declared:
            notes.append("host %s is already in the target; not adding it again" % picked["instance"])
            continue
        added.append({"instance": picked["instance"], "mode": picked["mode"],
                      "params": {}})
        notes.append("derived unique host from pins: %s (mode %s)"
                     % (picked["instance"], picked["mode"]))
    return added, notes