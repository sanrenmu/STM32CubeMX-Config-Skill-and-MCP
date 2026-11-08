"""Derive .ioc keys from the chip definition (does not read current state, does not start CubeMX).

Every rule must point to a source: either the chip definition XML or CubeMX's actual behaviour.
"""

from __future__ import annotations

import re

from ..util import suggest, suggest_clause
from .errors import IocUnknown

#: CubeMX's own rule (Mcu.initializeNewMcu): the bracketed part of RefName collapses to its last character.
_PAREN = re.compile(r"^(.*)\(.*(.)\)(.*)$")


def _norm(text) -> str:
    return (text or "").strip().lower()


def _canon(text) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(text))


def user_name(refname: str) -> str:
    """Get the name CubeMX writes into Mcu.UserName from the chip reference name.

    Basis: Mcu.initializeNewMcu does one regex replacement on names that contain brackets: the part before + the last character inside + the part after.
    Example: STM32F103C(8-B)Tx -> STM32F103CBTx.
    """
    m = _PAREN.match(refname or "")
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return refname or ""


def ip_by_instance(ips, instance: str, device: str | None = None):
    for ip in ips:
        if _norm(ip.instance) == _norm(instance):
            return ip
    known = ", ".join(sorted(x.instance for x in ips))
    near = suggest(instance, [x.instance for x in ips])
    domain = "instances of %s" % device if device else "device instances"
    raise IocUnknown("this device has no instance %r; available: %s%s"
                     % (instance, known, suggest_clause(domain, near)))


def _pin_short_names(pin_name: str) -> set:
    """Full pin name -> set of accepted short names.

    Pin names in the device XML carry a function suffix, and both spellings are seen: `PA0-WKUP` (hyphen) and `PB3 (JTDO/TRACESWO)` (space + brackets). The full name is written into the .ioc (measured: the H7 board seed uses full names), but a short name is more natural in the target JSON, so both resolve during parsing.
    """
    short = {pin_name}
    head = pin_name.split(" (")[0].strip()
    short.add(head)
    short.add(head.split("-")[0].strip())
    return short


def pin_by_name(device, name: str):
    """Find a pin by name; both the full name and the short name (PA0, PB3) work."""
    for pin in device.pins:
        if pin.name == name:
            return pin
    low = _norm(name)
    for pin in device.pins:
        if _norm(pin.name) == low:
            return pin
    hits = [pin for pin in device.pins
            if low in {_norm(alias) for alias in _pin_short_names(pin.name)}]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise IocUnknown("pin %r matches several full names: %s"
                         % (name, ", ".join(pin.name for pin in hits)))
    raise IocUnknown("device %s has no pin %r" % (device.refname, name))


def find_mode(ip, wanted: str):
    """Find a leaf mode by internal name / Name / UserName (case and underscore insensitive)."""
    leaf = [m for m in ip.modes if m.is_leaf]
    w = _norm(wanted)
    for attr in ("internal_name", "name", "user_name"):
        for mode in leaf:
            if _norm(getattr(mode, attr)) == w:
                return mode
    cw = _canon(wanted)
    if cw:
        for mode in leaf:
            if cw in (_canon(mode.internal_name), _canon(mode.name),
                      _canon(mode.user_name)):
                return mode
    known = []
    for mode in leaf:
        if mode.internal_name not in known:
            known.append(mode.internal_name)
    raise IocUnknown("peripheral %s has no mode %r; available: %s"
                     % (ip.instance, wanted, ", ".join(known[:20])))


def signal_modes(ip) -> dict:
    """Signal name -> internal names of the leaf modes that use it (list)."""
    out: dict = {}
    for mode in ip.modes:
        if not mode.is_leaf:
            continue
        for sig in mode.signals:
            out.setdefault(sig, [])
            if mode.internal_name not in out[sig]:
                out[sig].append(mode.internal_name)
    return out


def mode_virtual_pins(ip, mode) -> list:
    """Virtual pin of this mode itself (middleware/system IPs use it to express 'which mode is selected').

    Basis: a fixture saved by CubeMX - after enabling FREERTOS CMSIS_V2, the .ioc contains VP_FREERTOS_VS_CMSIS_V2.Signal / .Mode and no other mode keys.
    """
    if mode is None:
        return []
    sig_by = ip.signal_by_name()
    out = []
    for name in mode.signals:
        sig = sig_by.get(name)
        if sig is None or not sig.virtual:
            continue
        out.append({"pin": "VP_" + sig.name, "signal": sig.name,
                    "mode": mode.internal_name})
    return out


def shared_group(ip, signal_name: str):
    """Whether this signal belongs to a shared group; if so, return the group name used in .ioc.

    Basis: RefSignal@ShareableGroupName in the IP Modes XML. The group name may carry a template ($IpInstance) and an instance list suffix (:TIM1,TIM3,...); the .ioc carries the expanded name without the list. Example: TIM3_CH1 -> S_TIM3_CH1; ADC1_IN0 -> ADCx_IN0.
    """
    sig = ip.signal_by_name().get(signal_name)
    if sig is None or not sig.shareable_group:
        return None
    group = sig.shareable_group.split(":", 1)[0]
    group = group.replace("$IpInstance", ip.instance)
    group = group.replace("$SingleInstance", "")
    return group or None


def virtual_pins(ip) -> list:
    """Virtual pin of a system peripheral (signal with Virtual=true).

    The layout comes from CubeMX's own saved output: pin name = VP_ + signal name, with .Signal and .Mode keys.
    """
    sig_by = ip.signal_by_name()
    out = []
    seen = set()
    for mode in ip.modes:
        if not mode.is_leaf:
            continue
        for name in mode.signals:
            sig = sig_by.get(name)
            if sig is None or not sig.virtual:
                continue
            pin = "VP_" + sig.name
            if pin in seen:
                continue
            seen.add(pin)
            out.append({"pin": pin, "signal": sig.name,
                        "mode": mode.internal_name})
    return out


def _children(ip, mode) -> list:
    return [m for m in ip.modes if m.parent == mode.id]


def default_modes(ip) -> list:
    """Which modes are enabled by default.

    Basis: IP.setDefaultModes - for groups marked RemoveDisable, take the first sub-mode; then recurse into its sub-groups. Groups without RemoveDisable are not enabled automatically.
    """
    out = []

    def walk(mode):
        children = _children(ip, mode)
        if mode.remove_disable and children:
            picked = children[0]
            out.append(picked)
            walk(picked)
            return
        for child in children:
            walk(child)

    for mode in ip.modes:
        if mode.parent is None:
            walk(mode)
    return out


def default_virtual_pins(ip) -> list:
    """The virtual pins that only appear under default modes (for example No_Debug / SysTick of SYS)."""
    sig_by = ip.signal_by_name()
    out = []
    seen = set()
    for mode in default_modes(ip):
        for name in mode.signals:
            sig = sig_by.get(name)
            if sig is None or not sig.virtual:
                continue
            pin = "VP_" + sig.name
            if pin in seen:
                continue
            seen.add(pin)
            out.append({"pin": pin, "signal": sig.name,
                        "mode": mode.internal_name})
    return out


DMA_REQUEST_BASE = "DMA_Request"


def dma_requests(ip) -> list:
    """Which requests a classic DMA controller can carry (mode definitions with BaseMode=DMA_Request)."""
    return [ref.name for ref in ip.refmodes
            if _norm(ref.base) == _norm(DMA_REQUEST_BASE)]


def dma_request(ip, name: str):
    for ref in ip.refmodes:
        if _norm(ref.base) == _norm(DMA_REQUEST_BASE) and _canon(ref.name) == _canon(name):
            return ref
    known = dma_requests(ip)
    raise IocUnknown("DMA controller %s has no request %r; examples of available ones: %s"
                     % (ip.instance, name, ", ".join(known[:12])))




def dma_request_refmodes(ip) -> list:
    """DMA requests declared by this IP (RefMode with BaseMode=DMA_Request)."""
    return [ref for ref in ip.refmodes
            if _norm(ref.base) == _norm(DMA_REQUEST_BASE)]


def dma_request_parameters(ip, request: str) -> list:
    """Parameter names a request can write, base mode first and derived ones after (a repeated name keeps its first occurrence).

    Basis: the RefMode chain of a DMA request (RefMode BaseMode=...) is the declaration order of its parameters; CubeMX saves in that order into Dma.<request>.<index>.RequestParameters.
    """
    ref = dma_request(ip, request)
    chain = []
    seen = set()
    cur = ref
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        chain.append(cur)
        cur = next((item for item in ip.refmodes if item.name == cur.base), None)
    chain.reverse()
    names = []
    for item in chain:
        for param in item.parameters:
            name = param.get("name")
            if name and name not in names:
                names.append(name)
    return names


def dma_request_instances(ip, request: str) -> list:
    """Channels/streams selectable in a request definition (candidate values of the `Instance` parameter in the request RefMode).

    Note: `<Parameter Name="Instance"/>` in a RefMode is only a reference; the candidate values live on the root `RefParameter`, so look at IP-level parameters (`value_domain`). Family difference: on F1/F4 the channel Mode is itself a concrete instance and the candidates come from the mode tree; this function only matters for wildcard channel Modes (H7 DMAMUX).
    """
    cur = dma_request(ip, request)
    seen = set()
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        for param in cur.parameters:
            if _norm(param.get("name")) != "instance":
                continue
            values = [value.value for value in (param.get("values") or [])
                      if value.value]
            if not values:
                values = [value.value for value in value_labels(ip, "Instance")
                          if value.value]
            if values:
                return values
        cur = next((item for item in ip.refmodes if item.name == cur.base), None)
    return []


def dma_flows(ip, request: str) -> list:
    """Which channels/streams a request can reach, read from the mode tree instead of guessing from the parameter table.

    Basic shape: the controller root Mode (DMA1) has channel/stream Modes below it (DMA1_Channel4), and each channel has Modes for the requests it can carry (named after the request, a few with a channel suffix). `instances` are the values that can really be written into the `Instance` parameter: concrete stream names come from the request definition when available (the only way for wildcard Modes such as H7), otherwise from the Mode name.
    """
    by_id = {mode.id: mode for mode in ip.modes}
    wanted = _canon(request)
    out = []
    concrete = dma_request_instances(ip, request)
    for mode in ip.modes:
        base, _sep, suffix = mode.internal_name.partition(":")
        if _canon(base) != wanted or mode.parent is None:
            continue
        channel = by_id.get(mode.parent)
        if channel is None or channel.parent is None:
            continue
        controller = by_id.get(channel.parent)
        if controller is None:
            continue
        channel_name = channel.internal_name
        if "[" in channel_name:
            # wildcard channel Mode (H7's `DMA1_Stream[0-7]:DMA2_Stream[0-7]`):
            # concrete stream names exist only in the candidate values of the `Instance` parameter (DMAMUX lets any stream carry a request).
            instances = [value.value for value in value_labels(ip, "Instance")
                         if value.value] or [channel_name]
        else:
            # concrete channel Mode (F1/F4): the Mode name is the only usable channel,
            # do not use the whole `Instance` set from the root (that would widen the request to all streams).
            instances = [channel_name]
        out.append({"controller": controller.internal_name,
                    "instance": channel_name,
                    "instances": instances,
                    "request_mode": mode,
                    "channel_hint": suffix or None})
    return out


def dma_request_channel_default(ip, request: str):
    """Channel declared in the request definition (for example Channel = DMA_CHANNEL_4 on F4).

    Used for checking and explanation only: CubeMX does not write Channel on save (that parameter is invisible in the definition, and by CubeMX save behaviour invisible parameters are not written; evidence in the development-side docs).
    """
    ref = dma_request(ip, request)
    for param in ref.parameters:
        if _norm(param.get("name")) != "channel":
            continue
        values = [value.value for value in (param.get("values") or [])
                  if value.value]
        if len(values) == 1:
            return values[0]
    return None

def dma_request_parameter_values(ip, request: str, param: str) -> list:
    """Parameter candidate values declared by the request itself (first declaration up the RefMode chain).

    A DMA request overrides Direction/Mode/Channel values in its own RefMode; parameters without an override fall back to the root RefParameter definition.
    """
    cur = dma_request(ip, request)
    seen = set()
    wanted = _canon(param)
    while cur is not None and cur.name not in seen:
        seen.add(cur.name)
        for item in cur.parameters:
            if _canon(item.get("name")) != wanted:
                continue
            values = [value.value for value in (item.get("values") or [])
                      if value.value]
            if values:
                return values
        cur = next((ref for ref in ip.refmodes if ref.name == cur.base), None)
    return []
def refmode_chain(ip, mode) -> list:
    """RefMode chain of the current mode (up the BaseMode links), used to check whether a parameter name is valid."""
    by_name = {r.name: r for r in ip.refmodes}
    out = []
    cur = mode.refmode or mode.internal_name
    seen = set()
    while cur and cur in by_name and cur not in seen:
        seen.add(cur)
        ref = by_name[cur]
        out.append(ref)
        cur = ref.base
    return out


def mode_parameter_names(ip, mode) -> set:
    """Parameter names allowed under this mode."""
    names = set()
    for ref in refmode_chain(ip, mode):
        for param in ref.parameters:
            if param.get("name"):
                names.add(param["name"])
    return names


def parameter(ip, name: str):
    for param in ip.parameters:
        if _norm(param.name) == _norm(name):
            return param
    return None


def static_default(ip, name: str):
    param = parameter(ip, name)
    if param is None:
        return None
    for variant in param.variants:
        if variant.default not in (None, ""):
            return variant.default
    return None


def value_labels(ip, name: str):
    """Candidate values of a parameter (union). Used for checking only, never as a writing basis."""
    param = parameter(ip, name)
    if param is None:
        return []
    out = []
    for variant in param.variants:
        for value in variant.values:
            if value.value and value.value not in [v.value for v in out]:
                out.append(value)
    return out