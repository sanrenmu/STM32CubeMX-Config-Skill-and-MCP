"""Compile-time 'current selection' state: turns pins/params already fixed by the target into a three-valued evaluation context.

Query (probe describe) and build share the choice logic in probe.resolve; this module only assembles it.
"""

from __future__ import annotations


def selection_state(ip, model, target, params_by_ip):
    """Assemble the 'selections already fixed by the target' into an evaluation state for probe.resolve to pick applicable definitions."""
    from ..probe import resolve as probe_resolve

    signals = [item["signal"] for item in target["pins"] if item.get("signal")]
    pins = {item["pin"]: bool(item.get("locked")) for item in target["pins"]}
    params = {}
    for name, value in (params_by_ip.get(ip.instance) or {}).items():
        params[(ip.instance, name)] = value
    return probe_resolve.ModelState(
        ip=ip, device=model.device, instance=ip.instance,
        selection={"signals": signals, "pins": pins, "params": params})


def parameter_default(ip, name, model, target, params_by_ip, notes):
    """Default value of a parameter: taken from the applicable definition (same function as probe describe).

    When the definition depends on a condition that is not decided yet, report it instead of guessing (and do not fall back to the first default).
    """
    from ..probe import resolve as probe_resolve

    state = selection_state(ip, model, target, params_by_ip)
    picked = probe_resolve.select_variant(ip, name, state)
    if picked.get("truth") == 1:
        variant = picked.get("variant")
        if variant is not None and variant.default not in (None, ""):
            return variant.default
        return None
    if picked.get("truth") is None:
        missing = sorted({item.get("name") for item in picked.get("unknown") or []
                          if item.get("name")})
        notes.append("parameter %s: applicable definition depends on undecided conditions (%s)"
                     % (name, ", ".join(missing) or "unknown condition"))
    return None
