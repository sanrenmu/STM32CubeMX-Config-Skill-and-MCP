"""Turn the device model and the project file into "name -> value" for the
evaluator.

Evaluation has three input sources:

1. **Device definition** (XML): whether a mode exists, whether this chip has this
   instance, the die revision.
2. **Project file** (.ioc): which modes are selected, current parameter values,
   whether a pin is locked.
3. **Pseudo-number table of possible values**: which number an enumerated value is
   compared against inside an expression.

## Where the pseudo-number table comes from

Registration order, measured:

- every possible value is registered once;
- a value that reads as a number is registered as that number (VM:15-16);
- a value that does not read as a number gets a fake number starting at 1000, one
  per value (VM:10, VM:19).

So a condition like `CLKPolarity=SPI_POLARITY_LOW` turns both sides into the same
fake number and the equality holds. That is CubeMX behaviour, not ours.

Note on order: this implementation walks the model order (the IP order in the
device XML, the parameter order inside each IP). The order only changes which
fake number a label receives, never the equality result.

## Keys in the project file

Measured (db/plugins/boardmanager/boards/*.ioc):

- current peripheral mode: `USART2.VirtualMode=VM_ASYNC`;
- current parameter value: `RCC.SYSCLKSource=RCC_SYSCLKSOURCE_PLLCLK`;
- pins: `PA13.Locked=true`, `PA13.Mode=Serial-WireDebug`, `PA13.Signal=SYS_SWDIO`;
- device name: `Mcu.Name=STM32F091R(B-C)Tx`.

Note: the project file only holds values that changed. A missing key falls back to
the default from the definition (measured).
"""

from __future__ import annotations

import re

from .catalog import token_keys
from .cond import CondState

# One .ioc key/value line. Same shape as fullconfig.IOC_KEY_RE.
# Kept local: probe reads text with the standard library only and does not depend
# on the session layer.
IOC_KEY_RE = re.compile(r"^(\w+)\.(\w+)=(.*)$")
#: The shared parser returns keys with escapes already resolved; this only checks
#: for the word.word shape.
_WORD_RE = re.compile(r"^\w+$")
DIE_RE = re.compile(r"^DIE[0-9A-Fa-f]+$")
NAME_RE = re.compile(r"^STM32[A-Za-z0-9]+$")

# These keys are not parameters; they mean something else.
NOT_A_PARAMETER = {
    "IPParameters", "VirtualMode", "Mode", "Signal", "Locked",
    "GPIO_Label", "GPIO_Pin", "GPIOParameters", "PinState", "GPIO_ModeDefaultEXTI",
}


def parse_ioc(text: str) -> dict:
    """Read the project file and return {instance: {key: value}}. Only
    <word>.<word>=<value> keys are accepted.

    Parsing goes through the shared parser in `iocc/props.py` (Properties
    semantics, values kept faithful).
    """
    from ..iocc import props

    out = {}
    for key, value in props.loads(text or "").pairs():
        inst, sep, rest = key.partition(".")
        if not sep or not _WORD_RE.match(inst) or not _WORD_RE.match(rest):
            continue
        out.setdefault(inst, {})[rest] = value
    return out


def build_value_map(model) -> dict:
    """Possible value -> fake number (measured rules)."""
    out = {}
    idx = [1000.0]

    def add(label):
        if not label or label in out:
            return
        try:
            out[label] = float(label)
            return
        except ValueError:
            pass
        idx[0] += 1.0
        out[label] = idx[0]

    # Order (measured): the IP list is sorted by display name, then registered one
    # by one. We sort by instance name (the display name is not available, and the
    # order never affects equality).
    for ip in sorted(model.ips, key=lambda x: x.instance.lower()):
        for p in ip.parameters:
            for variant in p.variants:
                for val in variant.values:
                    add(val.value)
        for r in ip.refmodes:
            for p in r.parameters:
                for val in p.get("values", []):
                    add(val.value)
    return out


class ProjectState(CondState):
    """Answers "what is the value of this name now".

    With an empty ioc_text it degrades to "static device facts only".
    """

    def __init__(self, model, ioc_text: str = "", instance: str | None = None):
        super().__init__(instance=instance)
        self.model = model
        self.device = model.device
        self.ioc = parse_ioc(ioc_text)
        self.values = build_value_map(model)
        self.by_id = model.item_by_id()
        self.ip_by_instance = {ip.instance: ip for ip in model.ips}
        self.ip_by_lower = {ip.instance.lower(): ip for ip in model.ips}
        self._alias_to_sem = {}
        self._provider_by_token = {}
        for item in model.items:
            if item.kind == "semaphore":
                for alias in item.fields.get("aliases", []):
                    self._alias_to_sem.setdefault(alias, item.id)
            elif item.kind == "provider":
                self._provider_by_token[item.fields.get("token")] = item.fields
        self.semaphore_on = set()
        self._derive_semaphores()

    # ---- derivation ----

    def selected_modes(self) -> dict:
        """Which modes are selected: {instance: mode name}."""
        out = {}
        for inst, keys in self.ioc.items():
            if inst.lower() not in self.ip_by_lower:
                continue
            for key in ("VirtualMode", "Mode"):
                if key in keys:
                    out[inst] = keys[key]
                    break
        return out

    def find_mode(self, ip, wanted: str):
        for mode in ip.modes:
            if wanted in (mode.internal_name, mode.name, mode.user_name):
                return mode
        return None

    def _derive_semaphores(self):
        on = set()
        for inst, wanted in self.selected_modes().items():
            ip = self.ip_by_lower[inst.lower()]
            mode = self.find_mode(ip, wanted)
            if mode is None:
                continue
            # The selected mode and all of its ancestors count as on.
            by_id = {m.id: m for m in ip.modes}
            cur = mode
            while cur is not None:
                on.update(cur.semaphores)
                cur = by_id.get(cur.parent) if cur.parent else None
            # Root-level semaphores: set when this IP is in use.
            for sem in ip.semaphores:
                if sem.get("name"):
                    on.add(sem["name"])
        # When a parameter holds a possible value, the semaphores declared by that
        # value are set as well.
        for inst, keys in self.ioc.items():
            if inst.lower() not in self.ip_by_lower:
                continue
            ip = self.ip_by_lower[inst.lower()]
            for key, value in keys.items():
                if key in NOT_A_PARAMETER:
                    continue
                for p in ip.parameters:
                    if p.name != key:
                        continue
                    for variant in p.variants:
                        for val in variant.values:
                            if val.value == value:
                                on.update(val.semaphores)
        # Parameters the project file does not list: use the default from the
        # definition. Measured rule: set when the parameter is in use and its value
        # equals this possible value. Note: this is an approximation, "is the
        # parameter in use" is not available, so only the default is used.
        for inst, ip in self.ip_by_lower.items():
            keys = self.ioc.get(inst, {})
            for p in ip.parameters:
                if p.name in keys:
                    continue
                for variant in p.variants:
                    if variant.default is None:
                        continue
                    for val in variant.values:
                        if val.value == variant.default:
                            on.update(val.semaphores)
                    break
        resolved = set()
        for name in on:
            item_id = self.resolve_semaphore(name)
            if item_id:
                resolved.add(item_id)
            elif name in self._alias_to_sem:
                resolved.add(self._alias_to_sem[name])
        self.semaphore_on = resolved

    def resolve_semaphore(self, name: str):
        for key in token_keys(name, self.instance):
            got = self._alias_to_sem.get(key)
            if got:
                return got
        return None

    # ---- CondState interface ----

    def semaphore(self, name: str):
        item_id = self.resolve_semaphore(name)
        if item_id is None:
            return None                    # no source for the name: unknown
        return 1.0 if item_id in self.semaphore_on else 0.0

    def semaphore_or_usage(self, name: str):
        """Look the semaphore up first; when that fails, ask whether the instance is
        enabled."""
        got = self.semaphore(name)
        if got is not None:
            return got
        flag = self.usage_flag(name)
        return None if flag is None else (1.0 if flag else 0.0)

    def parameter(self, instance: str, name: str):
        inst = instance or self.instance or ""
        keys = self.ioc.get(inst)
        if keys and name in keys:
            return self.number_of(keys[name])
        # Key missing: use the default from the definition (measured).
        ip = self.ip_by_lower.get(inst.lower())
        if ip is None:
            return None
        for p in ip.parameters:
            if p.name != name:
                continue
            for variant in p.variants:
                if variant.default is not None:
                    return self.number_of(variant.default)
        return None

    def number_of(self, label: str):
        """The number for one value label. A number stays a number, an enumeration
        gets a fake number."""
        if label is None:
            return None
        try:
            return float(label)
        except ValueError:
            pass
        return self.values.get(label)

    def mode_exists(self, instance: str, mode: str):
        ip = self.ip_by_lower.get((instance or "").lower())
        if ip is None:
            return False               # same as CubeMX: a null IP means it does not exist
        return self.find_mode(ip, mode) is not None

    def pin_locked(self, pin: str):
        keys = self.ioc.get(pin)
        if not keys:
            return False
        return keys.get("Locked", "false").lower() == "true"

    def context_suffix(self, instance: str) -> str:
        return ""

    def device_fact(self, name: str):
        """A static device fact. Returns true/false when known, None otherwise."""
        dev = self.device
        low = (name or "").lower()
        known = {x.lower() for x in (dev.family, dev.line, dev.package,
                                     dev.die, dev.refname) if x}
        if low in known:
            return True
        if DIE_RE.match(name or "") or NAME_RE.match(name or ""):
            return False
        # An instance or pin this device does not have: statically false.
        provider = self._provider_by_token.get(name)
        if provider and provider.get("category") in ("missing_instance",
                                                     "missing_pin"):
            return False
        return None

    # ---- value table used by the evaluator ----

    def used_instances(self) -> set:
        """Instances the project file lists as enabled.

        Measured: the Mcu.IP0..Mcu.IPNb run in the project file, for example:
            Mcu.IP1=RCC
            Mcu.IP3=USART2
        """
        out = set()
        for key, value in (self.ioc.get("Mcu") or {}).items():
            if key.startswith("IP"):
                out.add(value)
        for inst, keys in self.ioc.items():
            if "VirtualMode" in keys or "Mode" in keys:
                out.add(inst)
        return out

    def usage_flag(self, name: str):
        """Names shaped like ADC1_Used / USE_ADC1: is this instance used right now."""
        want = name
        if want.upper().startswith("USE_"):
            want = want[4:]
        elif want.endswith("_Used"):
            want = want[:-len("_Used")]
        else:
            return None
        if want.lower() not in self.ip_by_lower:
            return None
        return want in self.used_instances()

    def pin_usage(self, token: str):
        """Pin usage, from <pin>.Signal in the project file.

        Two forms appear in conditions:
          - a bare pin name (PB6): is this pin used at all;
          - pin_signal (PA15_S_TIM2_ETR): is this pin connected to that signal.
        The project file only holds changed values, so "no Signal key" means the
        pin is not used.
        """
        head, sep, rest = token.partition("_")
        if not head.startswith("P") or len(head) < 3:
            return None
        pin = None
        low = head.lower()
        for p in self.device.pins:
            if p.name.lower() == low:
                pin = p
                break
        if pin is None:
            for p in self.device.pins:
                if p.name.lower().startswith(low + "-"):
                    pin = p
                    break
        if pin is None:
            return None                      # this device has no such pin
        keys = self.ioc.get(pin.name) or {}
        signal = keys.get("Signal")
        if not signal:
            return False
        if sep and rest:
            return signal == rest
        return True

    def resolve_name(self, name: str):
        """Names left in the expression: use the possible-value table when it has
        them, otherwise unknown."""
        got = self.values.get(name)
        if got is None:
            return None
        return got
