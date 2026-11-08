"""Applicable definition selection: pick the definition that applies to the current
context out of several definitions that share one name.

Rules (measured behaviour):
- Candidates follow the XML **declaration order**; the first one whose condition
  is true wins;
- A definition without a condition counts as applicable;
- Condition evaluation uses the three-valued substitution of `probe/cond.py`
  (unknown never collapses to 0);
- The RefMode inheritance chain lists the **base mode first** and only decides
  which parameters are in scope.

The query layer (probe describe) and the compiler (iocc/build.py) both call this
module, so there is only one selection rule.
"""

from __future__ import annotations

from . import cond


def _flatten(items):
    out = []
    for item in items or []:
        out.append(item)
    return out


class ModelState(cond.CondState):
    """Evaluation state built from "device model + target selection".

    - A mode that is declared but not selected: its semaphores answer "maybe",
      that is None (unknown);
    - A name that the model does not have at all: answers 0 (certainly false),
      matching CubeMX behaviour;
    - Pins and parameters that can be looked up give a certain value; only a
      failed lookup is unknown.
    """

    def __init__(self, ip=None, device=None, selection=None, instance=None):
        super().__init__(instance=instance)
        self.ip = ip
        self.device = device
        self.selection = selection or {}
        self._enabled_modes, self._possible_modes = _mode_sets(ip, self.selection)
        self._declared_semaphores = set()
        if ip is not None:
            for mode in ip.modes:
                for name in (mode.semaphores or []):
                    self._declared_semaphores.add(name)
        self._pins = dict(self.selection.get("pins") or {})
        self._params = dict(self.selection.get("params") or {})
        # Semaphore values already known from outside (import or tests may pass
        # 1, 0 or None explicitly).
        self._sem_override = dict(self.selection.get("semaphores") or {})
        self._device_pins = set()
        if device is not None:
            self._device_pins = {pin.name for pin in device.pins}

    # ---- CondState hooks ----
    def semaphore(self, name: str):
        if name in self._sem_override:
            given = self._sem_override[name]
            if given is None:
                return None
            return 1.0 if float(given) else 0.0
        if name in self._selected_semaphores():
            return 1.0
        if name in self._declared_semaphores:
            if self._possible_modes:
                return None
            return 0.0
        return 0.0

    def _selected_semaphores(self):
        out = set()
        if self.ip is None:
            return out
        enabled = set(self._enabled_modes)
        for mode in self.ip.modes:
            if mode.internal_name in enabled:
                for name in (mode.semaphores or []):
                    out.add(name)
        return out

    def parameter(self, instance: str, name: str):
        key = (instance, name)
        if key in self._params:
            return self._params[key]
        if instance == (self.instance or ""):
            value = self._params.get(name)
            if value is not None:
                return value
        return None

    def mode_exists(self, instance: str, mode: str):
        if self.ip is None:
            return None
        names = {item.internal_name for item in self.ip.modes}
        names |= {item.name for item in self.ip.modes}
        return mode in names

    def pin_locked(self, pin: str):
        if pin in self._pins:
            return True if self._pins.get(pin) else False
        if pin in self._device_pins:
            return False
        return None

    def device_fact(self, name: str):
        if self.device is None:
            return None
        facts = set()
        for attr in ("die", "family", "line", "package", "refname"):
            value = getattr(self.device, attr, None)
            if value:
                facts.add(str(value).upper())
        token = str(name or "").upper()
        if token in facts:
            return True
        if token.startswith("STM32") and self.device.family:
            return None
        return None

    def pin_usage(self, token: str):
        if not token:
            return None
        if token in self._pins:
            return 1.0 if self._pins[token] else 0.0
        base = token.split("_S_")[0]
        if base in self._pins:
            return 1.0 if self._pins[base] else 0.0
        if base in self._device_pins:
            return 0.0
        return None


def _mode_sets(ip, selection):
    """Return (certainly enabled mode names, possibly enabled mode names).

    Rule: a leaf mode whose signals are all on selected pins is certainly
    enabled; otherwise, if it shares any signal with the selection, it is
    possibly enabled (unknown); everything else is not enabled.
    """
    if ip is None:
        return set(), set()
    signals = set((selection or {}).get("signals") or [])
    enabled, possible = set(), set()
    for mode in ip.modes:
        needed = set(mode.signals or [])
        if not needed:
            continue
        if needed <= signals:
            enabled.add(mode.internal_name)
        elif needed & signals:
            possible.add(mode.internal_name)
    return enabled, possible


def select_variant(ip, name: str, state: cond.CondState | None = None):
    """Pick the first definition, in declaration order, whose condition is true.
    Returns a selection result dict.

    The result holds `variant` (the chosen one), `index` (declaration number),
    `truth` (three-valued), `unknown` (names that stayed unknown) and
    `candidates` (the verdict for every definition, for the query projection).
    """
    param = None
    for item in getattr(ip, "parameters", []) or []:
        if item.name == name:
            param = item
            break
    if param is None:
        return {"parameter": None, "variant": None, "index": None, "truth": None,
                "unknown": [], "candidates": []}
    state = state or cond.CondState()
    candidates = []
    unknown_all = []
    for index, variant in enumerate(param.variants):
        verdicts = []
        unknown = []
        for condition in (variant.conditions or []):
            result = cond.evaluate_condition(condition.raw, state, state.instance)
            truth = result.get("truth")
            verdicts.append(truth)
            for item in result.get("unknown") or []:
                unknown.append(item)
        if any(truth == 0 for truth in verdicts):
            applicability = False
        elif any(truth is None for truth in verdicts):
            applicability = None
        else:
            applicability = True
        candidates.append({
            "index": index,
            "refmode": variant.refmode,
            "type": variant.type,
            "default": variant.default,
            "min": variant.min,
            "max": variant.max,
            "conditions": [c.raw for c in (variant.conditions or [])],
            "applicable": applicability,
            "unknown": unknown,
        })
        if applicability is True:
            return {"parameter": param.name, "variant": variant, "index": index,
                    "truth": 1, "unknown": unknown_all, "candidates": candidates}
        if applicability is None:
            # An earlier definition may still apply: do not skip ahead to a later
            # unconditional one, that would be a guess.
            unknown_all.extend(unknown)
            return {"parameter": param.name, "variant": None, "index": index,
                    "truth": None, "unknown": unknown_all, "candidates": candidates}
    truth = 0 if not unknown_all else None
    return {"parameter": param.name, "variant": None, "index": None, "truth": truth,
            "unknown": unknown_all, "candidates": candidates}


def value_domain(variant):
    """Values the chosen definition accepts: a list yields the candidates (filtered
    by Value.condition), an integer yields min/max."""
    if variant is None:
        return {"type": None, "values": [], "min": None, "max": None,
                "default": None, "unknown": []}
    values = []
    unknown = []
    for value in variant.values or []:
        if value.condition is None:
            values.append(value.value)
            continue
        result = cond.evaluate_condition(value.condition.raw, cond.CondState())
        truth = result.get("truth")
        if truth == 1:
            values.append(value.value)
        elif truth is None:
            values.append(value.value)
            unknown.append(value.value)
    return {"type": variant.type, "values": values, "min": variant.min,
            "max": variant.max, "default": variant.default, "unknown": unknown}


def default_value(ip, name: str, state=None):
    """The default of the chosen definition (None when it has none). Shared by the
    query layer and the compiler."""
    picked = select_variant(ip, name, state)
    variant = picked.get("variant")
    if variant is None or variant.default in (None, ""):
        return None
    return variant.default


def parameter_names_in_scope(ip, enabled_modes):
    """Parameter names (in order) that these modes involve, including the BaseMode
    chain with the base mode first."""
    by_name = {item.name: item for item in getattr(ip, "refmodes", []) or []}
    out = []
    seen = set()
    for mode_name in enabled_modes:
        chain = []
        current = mode_name
        while current and current in by_name and current not in chain:
            chain.append(current)
            current = by_name[current].base
        for name in reversed(chain):
            for param in by_name[name].parameters or []:
                pname = param.get("name")
                if pname and pname not in seen:
                    seen.add(pname)
                    out.append(pname)
    return out


def describe(ip, state, instance=None):
    """Project the definition of one IP into a readable structure (for probe
    describe)."""
    instance = instance or getattr(ip, "instance", None)
    state = state or ModelState(ip=ip, instance=instance)
    enabled, possible = state._enabled_modes, state._possible_modes
    params = []
    for parameter in getattr(ip, "parameters", []) or []:
        picked = select_variant(ip, parameter.name, state)
        variant = picked.get("variant")
        domain = value_domain(variant)
        params.append({
            "name": parameter.name,
            "selected": None if variant is None else {
                "index": picked["index"],
                "refmode": variant.refmode,
                "type": variant.type,
                "default": variant.default,
                "min": variant.min,
                "max": variant.max,
                "values": domain["values"],
                "conditions": [c.raw for c in (variant.conditions or [])],
            },
            "truth": picked["truth"],
            "unknown": picked["unknown"],
            "variants": picked["candidates"],
            "domain": domain,
        })
    modes = []
    for mode in getattr(ip, "modes", []) or []:
        modes.append({
            "id": mode.id,
            "internal_name": mode.internal_name,
            "name": mode.name,
            "is_leaf": bool(mode.is_leaf),
            "parent": mode.parent,
            "enabled": mode.internal_name in enabled,
            "possible": mode.internal_name in possible,
            "signals": list(mode.signals or []),
            "refmode": mode.refmode,
        })
    signals = []
    for signal in getattr(ip, "signals", []) or []:
        signals.append({
            "name": signal.name,
            "pins": list(signal.pins or []),
            "virtual": bool(signal.virtual),
        })
    return {
        "instance": instance,
        "ip": getattr(ip, "name", None),
        "version": getattr(ip, "version", None),
        "enabled_modes": sorted(enabled),
        "possible_modes": sorted(possible),
        "modes": modes,
        "parameters": params,
        "signals": signals,
        "source": getattr(ip, "source", ""),
    }