"""Assemble the static configuration catalog and the dependency graph.

Input: device, IP models, clock model.
Output: the Item list, the Edge list and a coverage summary.

No condition evaluation, no reading of current state.

The graph is built in two passes:
  the first pass only creates items (vertices) for the device, every IP and the
  clock tree;
  the second pass only creates edges.
So a cross-IP reference (for example a RefMode condition pointing at a parameter
of another IP) already has both endpoints when the second pass runs, and edges are
never lost or broken by the item creation order.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .clock import read_clock, topology_summary
from .device import read_device
from .model import (
    Condition, Device, Edge, IpModel, Item, ParamVariant, ProbeModel, Value,
    to_jsonable,
)
from .modes import read_modes
from .xmlio import attr, parse

CONDITION_TOKENS = {
    "modeexist", "pinlocked",
}

# Runtime variables in condition expressions. The engine supplies their values at
# evaluation time; they are not configuration items.
VARIABLES = ("$IpInstance", "$IpNumber", "$Index", "$SingleInstance",
             "$context", "$Mode", "$Param")
_VAR_RE = re.compile(r"[$](IpInstance|IpNumber|Index|SingleInstance|context|"
                     r"Mode|Param)")
# Shape of a device or family name, for example STM32H7, STM32H750, STM32U575
DEVICE_NAME_RE = re.compile(r"^STM32[A-Z][A-Z0-9x]*$")
# Instance enable flag shaped like SPI5Used_ForRCC
FORRCC_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*?)Used_ForRCC$")
MEMORY_RE = re.compile(r"(?i)(sram|psram|sdram|ccmram|ddr|flash)")
# Shape of a pin name, for example PA15, PC14, PH0
PIN_NAME_RE = re.compile(r"^P[A-K][0-9]{1,2}$")


def _squash(name: str) -> str:
    """Drop the runtime variables, collapse runs of underscores into one and strip
    the leading and trailing underscores."""
    out = _VAR_RE.sub("_", name)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def ref_parameter_id(instance: str, refmode: str, name: str) -> str:
    """Identifier of a RefMode-local parameter. Such a parameter exists only inside
    that mode."""
    return instance + "/@refmode/" + refmode + "/param/" + name


def semaphore_keys(name: str, instance: str | None) -> list[str]:
    """One semaphore name may be written in several shapes; every shape is listed as
    an index key.

    The declaration side usually writes $IpInstance while the reference side writes
    the real instance name, so both shapes must be registered.
    """
    keys = [name]
    templated = name.replace("$IpInstance", "{IP}")
    if templated != name:
        keys.append(templated)
        if instance:
            keys.append(templated.replace("{IP}", instance))
    squashed = _squash(name)
    if squashed and squashed != name:
        keys.append(squashed)
    masked = _mask_numbers(name)
    if masked and masked != name:
        keys.append(masked)
    out = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def _mask_numbers(name: str) -> str:
    """Replace runtime number variables and plain digits by #.

    The declaration side writes $IpNumber while the reference side writes the real
    number (for example Sem_WDG2_22channel).
    """
    out = re.sub(r"\$(IpNumber|Index)", "#", name)
    out = re.sub(r"[0-9]+", "#", out)
    return out


def token_keys(token: str, instance: str | None) -> list[str]:
    """List the candidate index keys for a reference, strongest match first."""
    keys = [token]
    if "$IpInstance" in token and instance:
        keys.insert(0, token.replace("$IpInstance", instance))
    squashed = _squash(token)
    if squashed and squashed != token:
        keys.append(squashed)
    masked = _mask_numbers(token)
    if masked and masked != token:
        keys.append(masked)
    out = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def _cond(c: Condition | None) -> dict | None:
    if c is None:
        return None
    return {"raw": c.raw, "diagnostic": c.diagnostic, "action": c.action,
            "tokens": list(c.tokens)}


def _value(v: Value) -> dict:
    return {
        "value": v.value,
        "comment": v.comment,
        "condition": _cond(v.condition),
        "action": v.action,
        "semaphores": list(v.semaphores),
    }


def _variant(v: ParamVariant) -> dict:
    return {
        "refmode": v.refmode,
        "type": v.type,
        "default": v.default,
        "min": v.min,
        "max": v.max,
        "unit": v.unit,
        "display": v.display,
        "visible": v.visible,
        "conditions": [_cond(c) for c in v.conditions],
        "values": [_value(x) for x in v.values],
        "group": v.group,
        "tab": v.tab,
        "source": v.source,
    }


class CatalogBuilder:
    def __init__(self, device: Device, ips: list[IpModel],
                 clock=None, warnings: list[str] | None = None):
        self.device = device
        self.ips = ips
        self.clock = clock
        self.warnings = list(warnings or [])
        self.items: list[Item] = []
        self.edges: list[Edge] = []
        self._item_by_id: dict[str, Item] = {}
        self._edge_seen: set = set()
        self._provider: dict[str, Item] = {}
        self._sem_keys: dict[str, list[str]] = {}
        self._sem_decl: list[tuple[str, str, str, str]] = []
        self._refparam_by_name: dict[str, list[str]] = {}
        self._value_by_label: dict[str, list[str]] = {}
        self._ip_by_instance = {ip.instance: ip for ip in ips}
        self._ip_by_name: dict[str, IpModel] = {}
        for ip in ips:
            self._ip_by_name.setdefault(ip.name.lower(), ip)

    # ---- basic insertion ----

    def add_item(self, item: Item) -> Item:
        """Add an item. Adding the same id twice merges the fields and never
        overwrites an existing non-empty value."""
        old = self._item_by_id.get(item.id)
        if old is not None:
            for key, val in item.fields.items():
                if key not in old.fields or old.fields[key] in (None, [], {}, ""):
                    old.fields[key] = val
            for key in item.persist_keys:
                if key not in old.persist_keys:
                    old.persist_keys.append(key)
            if not old.conditions:
                old.conditions = list(item.conditions)
            if not old.source:
                old.source = item.source
            return old
        self._item_by_id[item.id] = item
        self.items.append(item)
        return item

    def add_edge(self, source: str, target: str, kind: str, evidence: str = ""):
        if source not in self._item_by_id:
            raise KeyError("edge source missing: " + source)
        if target not in self._item_by_id:
            raise KeyError("edge target missing: " + target)
        key = (source, target, kind, evidence)
        if key in self._edge_seen:
            return
        self._edge_seen.add(key)
        self.edges.append(Edge(source=source, target=target, kind=kind,
                               evidence=evidence))

    def provider(self, token: str, scope: str | None = None,
                 resolved: bool = False, note: str = "",
                 category: str = "unknown") -> Item:
        pid = "PROVIDER/" + token
        item = self._provider.get(pid)
        if item is None:
            item = self.add_item(Item(
                id=pid, kind="provider", label=token,
                owner=scope, source="",
                fields={"token": token, "resolved": resolved,
                        "category": category, "note": note, "scopes": []},
            ))
            self._provider[pid] = item
        if scope and scope not in item.fields["scopes"]:
            item.fields["scopes"].append(scope)
        return item

    def explain_unresolved(self, token: str, ip: IpModel | None):
        """Classify a name that cannot be resolved and write one reason. It never
        guesses the truth value, it only says what the name is."""
        if MEMORY_RE.search(token) and not token.startswith("S_"):
            return ("memory_region",
                    "This is a memory region name. The linker script or the GUI "
                    "decides it; the static XML does not declare it.")
        if (token.startswith("Semaphore_") or token.startswith("Sem_")
                or token.startswith("S_")):
            return ("undeclared_semaphore",
                    "This db version declares no semaphore with this name. It may "
                    "come from another db version, or from a CubeMX internal "
                    "convention.")
        if "$" in token:
            return ("dynamic_name",
                    "The engine builds the name from runtime variables; static "
                    "probing can only record its shape.")
        if token.endswith("$context") or "$context" in token:
            return ("middleware_context",
                    "Middleware context flag. The current middleware configuration "
                    "decides the name.")
        if token.upper().startswith("USE_"):
            name = token[4:]
            if not any(x.instance.lower() == name.lower() for x in self.ips):
                return ("missing_instance",
                        "this device has no instance %s" % name)
        if token.endswith("_Value_Line") or token.endswith("Value_Line"):
            return ("device_fact",
                    "This is a device classification name. It does not match the "
                    "part number or family of this device.")
        if ":" in token:
            head = token.split(":", 1)[0]
            if not any(x.instance.lower() == head.lower() for x in self.ips):
                return ("missing_instance", "this device has no instance %s" % head)
        for suffix in ("Used_ForRCC", "_Exist", "_USED", "_Used", "_Exist_"):
            if token.endswith(suffix):
                name = token[:-len(suffix)]
                if name and not any(x.instance.lower() == name.lower()
                                    for x in self.ips):
                    return ("missing_instance",
                            "this device has no instance %s" % name)
                break
        m = FORRCC_RE.match(token)
        if m and not any(x.instance.lower() == m.group(1).lower()
                         for x in self.ips):
            return ("missing_instance",
                    "this device has no instance %s" % m.group(1))
        if token.endswith("_IRQn"):
            return ("interrupt_name",
                    "This is an interrupt name. The interrupt list lives in the NVIC "
                    "definitions, not in the current file.")
        head = token.partition("_")[0]
        if PIN_NAME_RE.match(head) and self.find_pin(head) is None:
            return ("missing_pin", "this device has no pin %s" % head)
        return ("unknown",
                "The static XML has no such name. It may be GUI internal state, or "
                "a definition from another db version.")

    def pin_of_token(self, token: str):
        """For a name shaped like PA15_S_TIM2_ETR the first part is the pin. Returns
        that pin when found."""
        head, sep, rest = token.partition("_")
        if not sep or not rest:
            return None
        pin = self.find_pin(head)
        if pin is not None:
            return "PIN/" + pin.name
        return None

    # ---- lookups ----

    def find_ip(self, name: str) -> IpModel | None:
        if not name:
            return None
        for ip in self.ips:
            if ip.instance.lower() == name.lower():
                return ip
        return self._ip_by_name.get(name.lower())

    def find_mode(self, ip: IpModel, name: str):
        for m in ip.modes:
            if m.internal_name == name or m.name == name:
                return m
        return None

    def find_param(self, ip: IpModel, name: str):
        for p in ip.parameters:
            if p.name == name:
                return p
        return None

    def find_pin(self, name: str):
        low = name.lower()
        for p in self.device.pins:
            if p.name.lower() == low:
                return p
        # Conditions use the port name (PC14) while the full pin name carries the
        # role (PC14-OSC32_IN).
        for p in self.device.pins:
            if p.name.lower().startswith(low + "-"):
                return p
        return None

    def find_signal(self, name: str):
        for s in self.device.signals:
            if s.name == name:
                return s
        for ip in self.ips:
            for s in ip.signals:
                if s.name == name:
                    return s
        return None

    # ---- condition tokens ----

    def _index_semaphore(self, keys: list[str], item_id: str):
        for k in keys:
            got = self._sem_keys.setdefault(k, [])
            if item_id not in got:
                got.append(item_id)

    def declare_semaphore(self, instance: str, name: str, owner: str,
                          source: str) -> Item | None:
        """Register one semaphore declaration and return its item. Semaphores are how
        modes influence each other."""
        if not name:
            return None
        templated = name.replace("$IpInstance", "{IP}")
        iid = "SEM/" + instance + "/" + templated
        item = self._item_by_id.get(iid)
        if item is None:
            item = self.add_item(Item(
                id=iid, kind="semaphore", label=name, owner=instance,
                source=source,
                fields={"name": name, "declared_by": [], "aliases": []},
            ))
        if owner and owner not in item.fields["declared_by"]:
            item.fields["declared_by"].append(owner)
        aliases = item.fields["aliases"]
        for k in semaphore_keys(name, instance):
            if k not in aliases:
                aliases.append(k)
        self._index_semaphore(semaphore_keys(name, instance), item.id)
        return item

    def build_semaphore_index(self):
        """Register the semaphores an IP declares as items. Declarations come from
        modes, possible values and BSP dependencies."""
        for ip in self.ips:
            for s in ip.semaphores:
                item = self.declare_semaphore(ip.instance, s.get("name"),
                                              ip.instance, ip.source)
                if item:
                    self._sem_decl.append((ip.instance, item.id,
                                           "sets_semaphore_on_ip", ip.source))
            for m in ip.modes:
                for name in m.semaphores:
                    item = self.declare_semaphore(ip.instance, name, m.id,
                                                  m.source)
                    if item:
                        self._sem_decl.append((m.id, item.id, "sets_semaphore",
                                               m.source))
            for p in ip.parameters:
                pid = ip.instance + "/" + p.name
                for v in p.variants:
                    for val in v.values:
                        for name in val.semaphores:
                            item = self.declare_semaphore(
                                ip.instance, name, pid, ip.source)
                            if item:
                                self._sem_decl.append((
                                    pid, item.id, "sets_semaphore_on_value",
                                    ip.source))
            for r in ip.refmodes:
                rid = ip.instance + "/@refmode/" + r.name
                for p in r.parameters:
                    for val in p.get("values", []):
                        for name in getattr(val, "semaphores", []):
                            item = self.declare_semaphore(
                                ip.instance, name, rid, ip.source)
                            if item:
                                self._sem_decl.append((
                                    rid, item.id, "sets_semaphore_on_value",
                                    ip.source))
                for bsp in r.bsp_dependencies:
                    name = bsp.get("semaphore")
                    if not name:
                        continue
                    item = self.declare_semaphore(ip.instance, name, rid,
                                                  ip.source)
                    if item:
                        self._sem_decl.append((rid, item.id,
                                               "sets_semaphore_on_bsp",
                                               ip.source))
    
    def find_semaphore(self, token: str, ip: IpModel | None):
        """Look a semaphore up by candidate keys. Several hits that cannot be told
        apart return None so the caller can flag the ambiguity."""
        instance = ip.instance if ip else None
        for key in token_keys(token, instance):
            got = self._sem_keys.get(key)
            if not got:
                continue
            if len(got) == 1:
                return got[0]
            if instance:
                same = [x for x in got if x.startswith("SEM/" + instance + "/")]
                if len(same) == 1:
                    return same[0]
            return None
        return None

    @staticmethod
    def _scope_prefixes(scope: str | None, ip: IpModel | None) -> list[str]:
        """Derive lookup prefixes from the item that states the condition, narrowest
        first."""
        out = []
        if scope:
            marker = "/@refmode/"
            if marker in scope:
                rid = scope.split("/param/")[0]
                out.append(rid + "/param/")
                out.append(rid.split(marker)[0] + marker
                           + scope.split(marker, 1)[1].split("/")[0] + "/param/")
            out.append(scope.rsplit("/", 1)[0] + "/")
        if ip is not None:
            out.append(ip.instance + "/")
        seen = []
        for x in out:
            if x not in seen:
                seen.append(x)
        return seen

    def context_flag_item(self, ip: IpModel, flag: str, token: str) -> Item:
        """A middleware context flag, for example FATFS$context:_FS_READONLY."""
        iid = "CTXFLAG/" + ip.instance + "/" + flag.strip("_")
        item = self.add_item(Item(
            id=iid, kind="context_flag", label=flag, owner=ip.instance,
            source=ip.source,
            fields={"flag": flag, "token": token,
                    "note": "Middleware context flag. It is a by-product of some "
                            "middleware setting."},
        ))
        key = (item.id, ip.instance, "context_of_ip")
        if ip.instance in self._item_by_id and key not in self._edge_seen:
            self.add_edge(item.id, ip.instance, "context_of_ip", ip.source)
        return item

    def variable_item(self, token: str) -> Item:
        name = token.strip("$")
        return self.add_item(Item(
            id="VAR/" + name, kind="variable", label=token, owner=None,
            source="",
            fields={"name": token,
                    "note": "Runtime variable; the engine supplies the value at "
                            "evaluation time, it is not a configuration item"},
        ))

    def device_fact_item(self, value: str) -> Item:
        dev = self.device
        which = None
        matches = False
        for kind, val in (("family", dev.family), ("line", dev.line),
                          ("package", dev.package), ("die", dev.die),
                          ("refname", dev.refname)):
            if val and val.lower() == value.lower():
                which, matches = kind, True
                break
        if which is None:
            which = "other" if not value.startswith("DIE") else "die"
        return self.add_item(Item(
            id="DEVICEFACT/" + value, kind="device_fact", label=value,
            owner=dev.refname, source=dev.source,
            fields={"value": value, "kind": which, "matches_device": matches,
                    "note": ("belongs to this device" if matches else
                             "does not belong to this device (this device is %s)"
                             % dev.refname)},
        ))

    def usage_flag_item(self, name: str, kind: str = "usage") -> Item | None:
        """USE_XXX means a peripheral is enabled; XXX_Exist means the peripheral
        exists.

        An instance that can be found is linked; otherwise the flag stays
        unresolved.
        """
        target = None
        for ip in self.ips:
            if ip.instance.lower() == name.lower():
                target = ip.instance
                break
        if target is None:
            for ip in self.ips:
                if ip.name.lower() == name.lower():
                    target = ip.instance
                    break
        note = {"usage": "true when this instance is used",
                "existence": "this instance exists in this device"}[kind]
        item = self.add_item(Item(
            id="USEFLAG/" + name, kind="usage_flag", label=name,
            owner=target, source="",
            fields={"name": name, "target": target, "flag_kind": kind,
                    "note": (note if target else
                             "no instance with that name; left unresolved")},
        ))
        return item if target else None

    def resolve_token(self, token: str, ip: IpModel | None,
                      scope: str | None = None):
        """Resolve a name from a condition to an item in the graph. Returns None when
        it cannot be resolved.

        scope is the item that states the condition. A RefMode-local parameter is
        looked up inside the same mode first.
        """
        raw = token
        if raw.startswith("$modeexist_"):
            name = raw[len("$modeexist_"):]
            for cur in self.ips:
                m = self.find_mode(cur, name)
                if m:
                    return m.id
            return None
        if raw.startswith("$pinlocked_"):
            name = raw[len("$pinlocked_"):]
            pin = self.find_pin(name)
            return "PIN/" + pin.name if pin else None
        if raw.startswith("#"):
            raw = raw[1:]
        # Semaphores first. Middleware writes FATFS$context:_FS_READONLY, with a
        # colon.
        sem = self.find_semaphore(raw, ip)
        if sem:
            return sem
        if ":" in raw:
            iname, pname = raw.split(":", 1)
            # Middleware context flag: <middleware>$context:<flag>
            if iname.endswith("$context"):
                cur = self.find_ip(iname[:-len("$context")])
                if cur is not None:
                    return self.context_flag_item(cur, pname, raw).id
            vals = self._value_by_label.get(pname, [])
            if len(vals) == 1:
                return vals[0]
            cur = self.find_ip(iname)
            if cur is not None and pname.startswith("$ModeExist_"):
                mode = self.find_mode(cur, pname[len("$ModeExist_"):])
                if mode:
                    return mode.id
            if cur:
                p = self.find_param(cur, pname)
                if p:
                    return cur.instance + "/" + p.name
                m = self.find_mode(cur, pname)
                if m:
                    return m.id
            return None
        # runtime variables
        if raw.startswith("$"):
            base = raw.split("=", 1)[0].strip()
            if base in VARIABLES or base.startswith("$"):
                return self.variable_item(base).id
        # RefMode-local parameter (declared inside one mode only)
        refp = self._refparam_by_name.get(raw, [])
        if len(refp) == 1:
            return refp[0]
        if len(refp) > 1:
            for prefix in self._scope_prefixes(scope, ip):
                same = [x for x in refp if x.startswith(prefix)]
                if len(same) == 1:
                    return same[0]
        if ip is not None:
            replaced = (raw.replace("$IpInstance", ip.instance)
                           .replace("$IpNumber", ip.instance[-1:] or "1")
                           .replace("$Index", ip.instance[-1:] or "1"))
            p = self.find_param(ip, replaced)
            if p:
                return ip.instance + "/" + p.name
            m = self.find_mode(ip, replaced)
            if m:
                return m.id
            if replaced.endswith("_Used"):
                cur = self.find_ip(replaced[:-5])
                if cur:
                    return cur.instance
        # a parameter or mode of another IP
        for cur in self.ips:
            p = self.find_param(cur, raw)
            if p:
                return cur.instance + "/" + p.name
        for cur in self.ips:
            m = self.find_mode(cur, raw)
            if m:
                return m.id
        # Possible value or interrupt name: a condition that references a value is
        # linked to that value item
        for key in (raw, _squash(raw), raw.replace("$context", "")):
            vals = self._value_by_label.get(key, [])
            if not vals:
                continue
            if len(vals) == 1:
                return vals[0]
            if ip is not None:
                same = [x for x in vals if x.startswith(ip.instance + "/")]
                if len(same) == 1:
                    return same[0]
                sig = [x for x in vals if x.startswith(ip.instance + "/@refmode/")]
                if len(sig) == 1:
                    return sig[0]
        # pin plus signal name, for example PA15_S_TIM2_ETR
        pin_token = self.pin_of_token(raw)
        if pin_token:
            return pin_token
        # peripheral enable flag / existence flag
        if raw.upper().startswith("USE_"):
            item = self.usage_flag_item(raw[len("USE_"):], "usage")
            if item:
                return item.id
        if raw.endswith("_Exist"):
            item = self.usage_flag_item(raw[:-len("_Exist")], "existence")
            if item:
                return item.id
        # static device facts (family / line / package / die number)
        dev = self.device
        if raw and raw.lower() in {
                x.lower() for x in (dev.family, dev.line, dev.package,
                                    dev.die, dev.refname) if x}:
            return self.device_fact_item(raw).id
        if DEVICE_NAME_RE.match(raw) or (raw.startswith("DIE")
                                         and raw[3:].isdigit()):
            return self.device_fact_item(raw).id
        if raw.endswith("_Used"):
            cur = self.find_ip(raw[:-5])
            if cur:
                return cur.instance
        pin = self.find_pin(raw)
        if pin:
            return "PIN/" + pin.name
        sig = self.find_signal(raw)
        if sig:
            return "SIGNAL/" + sig.name
        if raw == self.device.refname:
            return "DEVICE/" + self.device.refname
        return None

    def add_condition_edges(self, owner: str, cond: Condition | None,
                            ip: IpModel | None, evidence: str = ""):
        if cond is None:
            return
        for token in cond.tokens:
            target = self.resolve_token(token, ip, owner)
            if target is None:
                category, note = self.explain_unresolved(token, ip)
                item = self.provider(token, scope=ip.instance if ip else None,
                                     resolved=False, note=note,
                                     category=category)
                target = item.id
            raw = (cond.raw or "")[:300]
            label = (evidence + " | " + raw) if evidence else raw
            self.add_edge(owner, target, "condition_token", label)

    # ---- first pass: items ----

    def build(self) -> ProbeModel:
        self.build_device_items()
        for ip in self.ips:
            self.build_ip_items(ip)
        self.build_clock_items()
        self.build_semaphore_index()
        self.build_device_edges()
        for ip in self.ips:
            self.build_ip_edges(ip)
        self.build_semaphore_edges()
        self.build_clock_edges()
        return ProbeModel(
            device=self.device, ips=self.ips, clock=self.clock,
            items=self.items, edges=self.edges,
            coverage=self.coverage(), warnings=self.warnings,
        )

    def build_device_items(self):
        dev = self.device
        self.add_item(Item(
            id="DEVICE/" + dev.refname, kind="device", label=dev.refname,
            owner=None, source=dev.source,
            fields={"family": dev.family, "line": dev.line,
                    "package": dev.package, "die": dev.die,
                    "clock_tree": dev.clock_tree,
                    "db_version": dev.db_version},
        ))
        for ctx in dev.contexts:
            name = ctx.get("name") or "context"
            self.add_item(Item(
                id="CONTEXT/" + name, kind="context", label=name,
                owner=dev.refname, source=dev.source, fields=ctx,
            ))
        for p in dev.pins:
            self.add_item(Item(
                id="PIN/" + p.name, kind="pin", label=p.name,
                owner=dev.refname, source=p.source,
                fields={"position": p.position, "positions": list(p.positions),
                        "type": p.type, "signals": list(p.signals)},
                conditions=[c for c in (_cond(x) for x in p.conditions) if c],
                persist_keys=[p.name + ".Signal", p.name + ".Locked",
                              p.name + ".GPIOParameters"],
            ))
        for s in dev.signals:
            self.add_item(Item(
                id="SIGNAL/" + s.name, kind="signal", label=s.name,
                owner=dev.refname, source=s.source,
                fields={"pins": list(s.pins), "io_modes": list(s.io_modes),
                        "direction": s.direction, "virtual": s.virtual},
            ))
        for e in dev.ips:
            self.add_item(Item(
                id=e.instance, kind="ip", label=e.instance,
                owner=dev.refname, source=e.source,
                fields={"name": e.name, "version": e.version,
                        "config_file": e.attrs.get("ConfigFile"),
                        "clock_enable_mode": e.attrs.get("ClockEnableMode"),
                        "power_domain": e.attrs.get("PowerDomain")},
                persist_keys=[e.instance + ".IPParameters"],
            ))

    def build_device_edges(self):
        """Edges of the device layer. They can only be added once the first pass has
        created every IP item."""
        # visibility conditions of a pin
        for p in self.device.pins:
            pid = "PIN/" + p.name
            for c in p.conditions:
                self.add_condition_edges(pid, c, None, "Pin " + p.name)
        # which pins a device-level signal can land on (pin candidates)
        for s in self.device.signals:
            sid = "SIGNAL/" + s.name
            for pin_name in s.pins:
                pid = "PIN/" + pin_name
                if pid in self._item_by_id:
                    self.add_edge(sid, pid, "candidate_pin", s.source)
                else:
                    prov = self.provider(
                        "PIN:" + pin_name, scope=self.device.refname,
                        category="missing_pin",
                        note="A signal names this pin, but the device XML has no "
                             "such pin.")
                    self.add_edge(sid, prov.id, "candidate_pin", s.source)

    def build_semaphore_edges(self):
        """Semaphore declaration edges: which item sets which semaphore."""
        for owner, sem_id, kind, source in self._sem_decl:
            if owner in self._item_by_id and sem_id in self._item_by_id:
                self.add_edge(owner, sem_id, kind, source)

    def add_value_item(self, owner_id: str, val, instance: str, source: str,
                       refmode: str | None = None) -> str:
        """Create one possible-value item and register its name in the index.

        A condition that references a value (for example AUDIO_HS of USB) is linked
        through this index.
        """
        value = val.value or "?"
        vid = owner_id + "/" + value
        if refmode:
            vid += "@" + refmode
        self.add_item(Item(
            id=vid, kind="value", label=value, owner=instance, source=source,
            fields={"comment": val.comment, "action": val.action,
                    "refmode": refmode,
                    "semaphores": list(val.semaphores)},
            conditions=[c for c in (_cond(val.condition),) if c],
        ))
        for label in value.split(":"):
            label = label.strip()
            if not label:
                continue
            got = self._value_by_label.setdefault(label, [])
            if vid not in got:
                got.append(vid)
            squashed = _squash(label)
            if squashed and squashed != label:
                got = self._value_by_label.setdefault(squashed, [])
                if vid not in got:
                    got.append(vid)
        return vid

    def build_ip_items(self, ip: IpModel):
        self.add_item(Item(
            id=ip.instance, kind="ip", label=ip.instance,
            owner=self.device.refname, source=ip.source,
            fields={"name": ip.name, "version": ip.version,
                    "ip_type": ip.ip_type, "group": ip.group,
                    "db_version": ip.db_version,
                    "description": ip.description,
                    "ip_dependencies": list(ip.ip_dependencies),
                    "root_conditions": [_cond(c) for c in ip.root_conditions]},
            persist_keys=[ip.instance + ".IPParameters"],
        ))
        for m in ip.modes:
            self.add_item(Item(
                id=m.id, kind="mode", label=m.name, owner=ip.instance,
                source=m.source,
                fields={"internal_name": m.internal_name,
                        "parent": m.parent, "operator": m.operator,
                        "is_leaf": m.is_leaf, "signals": list(m.signals),
                        "refmode": m.refmode, "user_name": m.user_name,
                        "description": m.description,
                        "path": list(m.path)},
                conditions=[c for c in (_cond(x) for x in m.conditions) if c],
            ))
        for r in ip.refmodes:
            self.add_item(Item(
                id=ip.instance + "/@refmode/" + r.name, kind="refmode",
                label=r.name, owner=ip.instance, source=r.source,
                fields={"base": r.base, "abstract": r.abstract,
                        "parameters": list(r.parameters),
                        "bsp_dependencies": list(r.bsp_dependencies),
                        "configs": list(r.configs), "group": r.group,
                        "tab": r.tab, "description": r.description},
                conditions=[c for c in (_cond(x) for x in r.conditions) if c],
            ))
        for r in ip.refmodes:
            rid = ip.instance + "/@refmode/" + r.name
            for p in r.parameters:
                pname = p.get("name")
                if not pname:
                    continue
                pid = ref_parameter_id(ip.instance, r.name, pname)
                self.add_item(Item(
                    id=pid, kind="refmode_parameter", label=pname,
                    owner=rid, source=ip.source,
                    fields={"refmode": r.name,
                            "ref_parameter": p.get("ref_parameter"),
                            "group": p.get("group"), "tab": p.get("tab")},
                    conditions=[c for c in (_cond(p.get("condition")),) if c],
                ))
                got = self._refparam_by_name.setdefault(pname, [])
                if pid not in got:
                    got.append(pid)
                for val in p.get("values", []):
                    self.add_value_item(pid, val, ip.instance, ip.source,
                                        refmode=r.name)
        for p in ip.parameters:
            pid = ip.instance + "/" + p.name
            self.add_item(Item(
                id=pid, kind="parameter", label=p.name, owner=ip.instance,
                source=ip.source,
                fields={"description": p.description,
                        "variants": [_variant(v) for v in p.variants]},
                persist_keys=[ip.instance + "." + p.name],
            ))
            for v in p.variants:
                for val in v.values:
                    self.add_value_item(pid, val, ip.instance, ip.source,
                                        refmode=v.refmode)
        for s in ip.signals:
            self.add_item(Item(
                id="SIGNAL/" + s.name, kind="signal", label=s.name,
                owner=ip.instance, source=s.source,
                fields={"pins": list(s.pins), "io_modes": list(s.io_modes),
                        "direction": s.direction, "virtual": s.virtual,
                        "shareable_group": s.shareable_group,
                        "exclusive_group": s.exclusive_group},
            ))
        for gp in ip.gpio_pins:
            name = gp.get("name")
            if not name:
                continue
            self.add_item(Item(
                id="PIN/" + name, kind="pin", label=name, owner=ip.instance,
                source=ip.source,
                fields={"port": gp.get("port"), "signals": [],
                        "specific": gp.get("specific", [])},
            ))
            for sig in gp.get("signals", []):
                sname = sig.get("name")
                if sname:
                    self.add_item(Item(
                        id="SIGNAL/" + sname, kind="signal", label=sname,
                        owner=ip.instance, source=ip.source,
                        fields={"pins": [name]},
                    ))
        for flow in ip.dma_flows:
            fname = flow.get("name")
            if not fname:
                continue
            self.add_item(Item(
                id="DMA/" + ip.instance + "/flow/" + fname, kind="dma_flow",
                label=fname, owner=ip.instance, source=ip.source,
                fields={"requests": flow.get("requests", [])},
            ))
        for req in ip.dma_requests:
            rname = req.get("name")
            if not rname:
                continue
            self.add_item(Item(
                id="DMA/" + ip.instance + "/request/" + rname,
                kind="dma_request", label=rname, owner=ip.instance,
                source=ip.source,
                fields={"flow": req.get("flow"),
                        "master_ip_name": req.get("master_ip_name"),
                        "section": req.get("section")},
            ))

    def build_ip_edges(self, ip: IpModel):
        ip_id = ip.instance
        mode_ids = {m.id for m in ip.modes}
        # IP-level conditions (declared on the root element)
        for c in ip.root_conditions:
            self.add_condition_edges(ip_id, c, ip, "IP " + ip.instance)
        # IP-level dependencies (declared in the XML)
        for dep in ip.ip_dependencies:
            target = self.find_ip(dep)
            if target:
                self.add_edge(ip_id, target.instance, "ip_dependency", ip.source)
            else:
                prov = self.provider(
                    "IPDEP:" + dep, scope=ip.instance,
                    category="missing_ip_dependency",
                    note="An IP-level dependency names this instance, but the device "
                         "does not carry it.")
                self.add_edge(ip_id, prov.id, "ip_dependency", ip.source)
        refmode_ids = {r.name: ip.instance + "/@refmode/" + r.name
                       for r in ip.refmodes}
        # modes
        for m in ip.modes:
            self.add_edge(ip_id, m.id, "owns_mode", m.source)
            if m.parent and m.parent in mode_ids:
                self.add_edge(m.id, m.parent, "parent_mode", m.source)
            if m.refmode:
                rid = refmode_ids.get(m.refmode)
                if rid:
                    self.add_edge(m.id, rid, "uses_refmode", m.source)
            for c in m.conditions:
                self.add_condition_edges(m.id, c, ip, "Mode " + m.name)
        for m in ip.modes:
            if m.parent and m.parent in mode_ids:
                parent = self._item_by_id[m.parent]
                op = (parent.fields.get("operator") or "").upper()
                if op.startswith("XOR") or op == "LIST":
                    self.add_edge(m.parent, m.id, "selection_group",
                                  "operator=" + op)
        # RefMode
        for r in ip.refmodes:
            rid = refmode_ids[r.name]
            self.add_edge(ip_id, rid, "owns_refmode", r.source)
            if r.base and r.base in refmode_ids:
                self.add_edge(rid, refmode_ids[r.base], "base_refmode", r.source)
            for c in r.conditions:
                self.add_condition_edges(rid, c, ip, "RefMode " + r.name)
            for p in r.parameters:
                pname = p.get("name")
                if not pname:
                    continue
                pid = ip.instance + "/" + pname
                refpid = ref_parameter_id(ip.instance, r.name, pname)
                if pid in self._item_by_id:
                    self.add_edge(rid, pid, "declares_parameter", r.source)
                    self.add_edge(refpid, pid, "binds_ref_parameter", r.source)
                elif refpid in self._item_by_id:
                    self.add_edge(rid, refpid, "declares_parameter", r.source)
                    base = p.get("ref_parameter")
                    if base:
                        gpid = ip.instance + "/" + base
                        if gpid in self._item_by_id:
                            self.add_edge(refpid, gpid, "binds_ref_parameter",
                                          r.source)
                if p.get("condition"):
                    self.add_condition_edges(rid, p["condition"], ip,
                                             "RefMode parameter " + pname)
        # parameters
        for p in ip.parameters:
            pid = ip.instance + "/" + p.name
            self.add_edge(ip_id, pid, "owns_parameter", ip.source)
            for v in p.variants:
                for c in v.conditions:
                    self.add_condition_edges(pid, c, ip, "parameter " + p.name)
                for val in v.values:
                    vid = pid + "/" + (val.value or "?")
                    if v.refmode:
                        vid += "@" + v.refmode
                    self.add_edge(pid, vid, "possible_value", ip.source)
                    if val.condition:
                        self.add_condition_edges(vid, val.condition, ip,
                                                 "value " + str(val.value))
        # signals
        for s in ip.signals:
            sid = "SIGNAL/" + s.name
            for m in ip.modes:
                if s.name in m.signals and m.id in mode_ids:
                    self.add_edge(m.id, sid, "requires_signal", m.source)
            for pin_name in s.pins:
                pid = "PIN/" + pin_name
                if pid in self._item_by_id:
                    self.add_edge(sid, pid, "candidate_pin", s.source)
        for gp in ip.gpio_pins:
            name = gp.get("name")
            if not name:
                continue
            pid = "PIN/" + name
            for sig in gp.get("signals", []):
                sname = sig.get("name")
                if sname and ("SIGNAL/" + sname) in self._item_by_id:
                    self.add_edge(pid, "SIGNAL/" + sname, "pin_signal", ip.source)
        # DMA
        for flow in ip.dma_flows:
            fname = flow.get("name")
            if fname:
                self.add_edge(ip_id, "DMA/" + ip.instance + "/flow/" + fname,
                              "owns_dma_flow", ip.source)
        for req in ip.dma_requests:
            rname = req.get("name")
            if not rname:
                continue
            rid = "DMA/" + ip.instance + "/request/" + rname
            self.add_edge(ip_id, rid, "owns_dma_request", ip.source)
            if req.get("flow"):
                fid = "DMA/" + ip.instance + "/flow/" + req["flow"]
                if fid in self._item_by_id:
                    self.add_edge(rid, fid, "dma_flow_candidate", ip.source)
                else:
                    prov = self.provider(
                        "DMAFLOW:" + req["flow"], scope=ip.instance,
                        category="missing_dma_flow",
                        note="A DMA request names this flow, but the IP definition "
                             "has no such flow.")
                    self.add_edge(rid, prov.id, "dma_flow_candidate", ip.source)

    def _clock_item_id(self, node_id: str) -> str:
        return "CLOCK/" + node_id

    def build_clock_items(self):
        if self.clock is None:
            return
        for n in self.clock.nodes:
            self.add_item(Item(
                id=self._clock_item_id(n.id), kind="clock_element", label=n.id,
                owner=self.clock.name, source=n.source,
                fields={"tree": n.tree, "type": n.type,
                        "ref_parameter": n.ref_parameter,
                        "ref_enable": n.ref_enable,
                        "ref_secure": n.ref_secure,
                        "ref_lock": n.ref_lock,
                        "is_key": n.is_key,
                        "inputs": list(n.inputs),
                        "outputs": list(n.outputs),
                        "path": list(n.path)},
                conditions=[c for c in (_cond(x) for x in n.conditions) if c],
                persist_keys=(["RCC." + n.ref_parameter]
                              if n.ref_parameter else []),
            ))

    def build_clock_edges(self):
        if self.clock is None:
            return
        clock = self.clock
        for n in clock.nodes:
            iid = self._clock_item_id(n.id)
            for c in n.conditions:
                self.add_condition_edges(iid, c, self.find_ip("RCC"),
                                         "Clock " + n.id)
            if n.ref_parameter:
                pid = "RCC/" + n.ref_parameter
                if pid in self._item_by_id:
                    self.add_edge(iid, pid, "clock_uses_parameter", n.source)
                else:
                    p = self.provider(
                        "CLOCKPARAM:" + n.ref_parameter, scope=clock.name,
                        category="unresolved_clock_parameter",
                        note="A clock element references this RCC parameter, but the "
                             "RCC definition has no such parameter. It may exist only "
                             "in another db version.")
                    self.add_edge(iid, p.id, "clock_uses_parameter", n.source)
        for e in clock.edges:
            src = self._clock_item_id(e.source)
            if src not in self._item_by_id:
                prov = self.provider(
                    "CLOCKSRC:" + e.source, scope=clock.name,
                    category="unresolved_clock_source",
                    note="The clock edge starts at an element that was not read.")
                src = prov.id
            if e.resolved and self._clock_item_id(e.target) in self._item_by_id:
                tgt = self._clock_item_id(e.target)
                kind = "clock_output"
            else:
                prov = self.provider(
                    "CLOCK:" + e.target, scope=clock.name,
                    category="unresolved_clock_target",
                    note="The clock edge ends at an element that was not read. That "
                         "element may exist only in another clock tree version.")
                tgt = prov.id
                kind = "clock_output_unresolved"
            self.add_edge(src, tgt, kind, "signal=" + str(e.signal_id))

    # ---- coverage ----

    def coverage(self) -> dict:
        kinds: dict[str, int] = {}
        for i in self.items:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        ekinds: dict[str, int] = {}
        for e in self.edges:
            ekinds[e.kind] = ekinds.get(e.kind, 0) + 1
        providers = [i for i in self.items if i.kind == "provider"]
        unresolved = [i for i in providers if not i.fields.get("resolved")]
        return {
            "device": {
                "refname": self.device.refname,
                "family": self.device.family,
                "line": self.device.line,
                "package": self.device.package,
                "pins": len(self.device.pins),
                "signals": len(self.device.signals),
                "ips": len(self.device.ips),
                "contexts": len(self.device.contexts),
                "unhandled": self.device.unhandled,
            },
            "ips": [{
                "instance": ip.instance, "name": ip.name,
                "version": ip.version, "ip_type": ip.ip_type,
                "modes": len(ip.modes), "refmodes": len(ip.refmodes),
                "parameters": len(ip.parameters), "signals": len(ip.signals),
                "dma_flows": len(ip.dma_flows),
                "dma_requests": len(ip.dma_requests),
                "unhandled": ip.unhandled,
            } for ip in self.ips],
            "clock": topology_summary(self.clock) if self.clock else None,
            "counts": {
                "items": len(self.items),
                "edges": len(self.edges),
                "item_kinds": kinds,
                "edge_kinds": ekinds,
                "providers": len(providers),
                "unresolved_providers": len(unresolved),
            },
            "unhandled": self.device.unhandled + [
                dict(x, instance=ip.instance) for ip in self.ips
                for x in ip.unhandled
            ],
            "warnings": self.warnings,
        }


def load_model(machine, refname: str, ip_filter: Iterable[str] | None = None,
               with_clock: bool = True, board: str | None = None) -> ProbeModel:
    """Read the static model of one device.

    When ip_filter is given only those instances are read (case-insensitive), for
    fast queries. When board is given, the native BSP definition of that board is
    read into the same model (instance name = board name, ip_type = bspip): the
    query layer and the compiler share this one context instead of building a
    separate board model.
    """
    device = read_device(machine, refname)
    wanted = None
    if ip_filter:
        wanted = {x.lower() for x in ip_filter}
    ips = []
    warnings = []
    for entry in device.ips:
        if wanted is not None and entry.instance.lower() not in wanted:
            continue
        path = Path(machine.db) / "mcu" / "IP" / (
            "%s-%s_Modes.xml" % (entry.name, entry.version))
        if not path.is_file():
            warnings.append("missing IP Modes file: %s (%s)"
                            % (entry.instance, path.name))
            continue
        try:
            ips.append(read_modes(path, entry.instance, entry.name,
                                  entry.version))
        except Exception as exc:
            warnings.append("failed to parse: %s (%s): %s"
                            % (entry.instance, path.name, exc))
    board_ctx = None
    if board:
        from . import boards as board_query

        board_ctx = board_query.board_context(machine, board)
        bsp_modes = board_ctx.get("bsp_modes")
        if bsp_modes is not None and (wanted is None
                                      or board_ctx["name"].lower() in wanted):
            try:
                bsp_version = ""
                try:
                    bsp_version = attr(parse(bsp_modes), "Version") or ""
                except Exception:
                    pass
                ips.append(read_modes(bsp_modes, board_ctx["name"],
                                      board_ctx["name"], bsp_version))
            except Exception as exc:
                warnings.append("failed to parse the BSP definition: %s: %s"
                                % (Path(bsp_modes).name, exc))
    clock = None
    if with_clock and device.clock_tree:
        clock_path = Path(machine.db) / "plugins" / "clock" / (
            device.clock_tree + ".xml")
        if clock_path.is_file():
            try:
                clock = read_clock(machine, device.clock_tree)
            except Exception as exc:
                warnings.append("failed to parse the clock tree: %s: %s"
                                % (device.clock_tree, exc))
        else:
            warnings.append("missing clock tree file: %s" % clock_path.name)
    builder = CatalogBuilder(device, ips, clock, warnings)
    model = builder.build()
    model.board = board_ctx
    return model


def build_catalog(device: Device, ips: list[IpModel], clock=None,
                  warnings: list[str] | None = None) -> ProbeModel:
    return CatalogBuilder(device, ips, clock, warnings).build()


def summary(model: ProbeModel) -> dict:
    return {
        "device": model.device.refname,
        "items": len(model.items),
        "edges": len(model.edges),
        "coverage": model.coverage,
        "warnings": model.warnings,
    }
