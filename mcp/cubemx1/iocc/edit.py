"""Incremental edits: set / remove of cubemx1.ioc-edit/1.

Principles:
- base is a high-level target (v1 or v2); an omitted section means 'keep the current value', and an empty array does not mean 'clear';
- collections merge by stable identity: pin = full pin name, IP = instance name, parameter = instance name + parameter name, NVIC = IRQ name, DMA = request name (an explicit instance is required when the same request has several paths), RTOS object = kind + instance name + object name, constant = constant name;
- the same object in both set and remove, or a duplicate identity inside one edit -> report the specific conflict, never 'last line wins';
- removing a non-existent object is a no-op; removing an IP while pins still activate it -> report the relation conflict instead of deleting silently.
"""

from __future__ import annotations

from . import target as target_contract
from .target import RTOS_KINDS
from .errors import IocConflict, IocUnknown

CONTRACT = "cubemx1.ioc-edit/1"

SET_SECTIONS = ("device", "project", "pins", "ips", "clock", "nvic", "dma",
                "rtos", "constants")
REMOVE_SECTIONS = ("pins", "ips", "params", "nvic", "dma", "rtos", "constants")


def _obj(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise IocUnknown("expected an object here", where)
    return value


def _list(value, where: str) -> list:
    if not isinstance(value, list):
        raise IocUnknown("expected an array here", where)
    return value


def _keys(obj: dict, allowed, where: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise IocUnknown("unrecognized field: %s (available: %s)"
                         % (", ".join(extra), ", ".join(sorted(allowed))), where)


def _identity(entry: dict, fields, where: str):
    key = []
    for field in fields:
        value = entry.get(field)
        if value in (None, ""):
            raise IocUnknown("missing identity field %r" % field, where)
        key.append(str(value))
    return tuple(key)


def _check_duplicates(items, fields, where: str):
    seen = {}
    for index, item in enumerate(items):
        key = _identity(item, fields, "%s[%d]" % (where, index))
        if key in seen:
            raise IocConflict("%s: duplicate identity %s (item %d and item %d)"
                              % (where, "/".join(key), seen[key], index), where)
        seen[key] = index
    return seen


# ------------------------------------------------------------------ remove

def _remove_by_identity(items, removals, fields, where: str):
    """Delete by identity; deleting a non-existent one is a no-op. Return (kept list, removed key set)."""
    wanted = set()
    for index, raw in enumerate(removals or []):
        where_item = "%s[%d]" % (where, index)
        if isinstance(raw, (str, int)) and len(fields) == 1:
            item = {fields[0]: str(raw)}
        else:
            item = _obj(raw, where_item)
        wanted.add(_identity(item, fields, where_item))
    kept, removed = [], set()
    for item in items or []:
        key = _identity(item, fields, where)
        if key in wanted:
            removed.add(key)
            continue
        kept.append(item)
    return kept, removed


def _apply_remove(result: dict, remove: dict) -> None:
    _keys(remove, REMOVE_SECTIONS, "remove")
    if "pins" in remove:
        result["pins"], _ = _remove_by_identity(result["pins"], remove["pins"],
                                                ["pin"], "remove.pins")
    if "ips" in remove:
        result["ips"], _ = _remove_by_identity(result["ips"], remove["ips"],
                                               ["instance"], "remove.ips")
    if "params" in remove:
        for index, raw in enumerate(_list(remove["params"], "remove.params")):
            where = "remove.params[%d]" % index
            item = _obj(raw, where)
            instance = str(item.get("instance") or "")
            name = str(item.get("name") or "")
            if not instance or not name:
                raise IocUnknown("remove.params requires {instance, name}", where)
            for ip in result["ips"]:
                if ip["instance"] == instance:
                    ip["params"].pop(name, None)
                    break
    if "nvic" in remove:
        names = {str(item.get("irq") if isinstance(item, dict) else item)
                 for item in _list(remove["nvic"], "remove.nvic")}
        result["nvic"]["vectors"] = [vector for vector in result["nvic"]["vectors"]
                                     if vector["irq"] not in names]
    if "dma" in remove:
        wanted = []
        for index, raw in enumerate(_list(remove["dma"], "remove.dma")):
            where = "remove.dma[%d]" % index
            item = _obj(raw, where)
            if not item.get("request"):
                raise IocUnknown("remove.dma requires {request, instance}", where)
            wanted.append((str(item["request"]), str(item.get("instance") or "")))
        result["dma"] = [entry for entry in result["dma"]
                         if not any(entry["request"] == request
                                    and (not instance
                                         or entry.get("instance") == instance)
                                    for request, instance in wanted)]
    if "rtos" in remove:
        rtos = result.get("rtos")
        for index, raw in enumerate(_list(remove["rtos"], "remove.rtos")):
            where = "remove.rtos[%d]" % index
            item = _obj(raw, where)
            kind = str(item.get("kind") or "")
            name = str(item.get("name") or "")
            if not kind or not name:
                raise IocUnknown("remove.rtos requires {instance, kind, name}", where)
            if kind not in RTOS_KINDS:
                raise IocUnknown("rtos kinds are only %s" % ", ".join(RTOS_KINDS), where)
            rows = (rtos or {}).get(kind) or []
            if rows:
                rtos[kind] = [row for row in rows if row.get("name") != name]
    if "constants" in remove:
        for name in _list(remove["constants"], "remove.constants"):
            result["constants"].pop(str(name), None)


# --------------------------------------------------------------------- set

def _merge_device(result: dict, section: dict) -> None:
    _keys(section, ("refname", "cpn"), "set.device")
    for key in ("refname", "cpn"):
        if section.get(key) not in (None, ""):
            result["device"][key] = str(section[key])


def _merge_project(result: dict, section: dict) -> None:
    _keys(section, ("name", "output_dir", "toolchain", "heap", "stack",
                    "library_copy", "extra", "options"), "set.project")
    for key in ("name", "output_dir", "toolchain", "heap", "stack",
                "library_copy"):
        if section.get(key) not in (None, ""):
            result["project"][key] = str(section[key])
    if section.get("extra"):
        result["project"]["extra"] = dict(result["project"].get("extra") or {})
        result["project"]["extra"].update({key: str(value)
                                           for key, value in section["extra"].items()})
    if section.get("options"):
        from . import options as option_table
        incoming = option_table.validate(section["options"])
        merged_options = dict(result["project"].get("options") or {})
        merged_options.update(incoming)
        result["project"]["options"] = merged_options


def _merge_pins(result: dict, section: list) -> None:
    _check_duplicates(section, ["pin"], "set.pins")
    by_name = {item["pin"]: item for item in result["pins"]}
    order = [item["pin"] for item in result["pins"]]
    for raw in section:
        item = dict(raw)
        name = item["pin"]
        if name in by_name:
            merged = dict(by_name[name])
            merged.update(item)
            by_name[name] = merged
        else:
            by_name[name] = item
            order.append(name)
    result["pins"] = [by_name[name] for name in order]


def _merge_ips(result: dict, section: list) -> None:
    _check_duplicates(section, ["instance"], "set.ips")
    by_instance = {item["instance"]: item for item in result["ips"]}
    order = [item["instance"] for item in result["ips"]]
    for raw in section:
        item = dict(raw)
        name = item["instance"]
        current = by_instance.get(name)
        if current is None:
            by_instance[name] = item
            order.append(name)
            continue
        merged = dict(current)
        for key in ("mode",):
            if item.get(key):
                merged[key] = item[key]
        if item.get("modes"):
            merged["modes"] = item["modes"]
        if item.get("params"):
            merged["params"] = dict(merged.get("params") or {})
            merged["params"].update({key: str(value)
                                     for key, value in item["params"].items()})
        by_instance[name] = merged
    result["ips"] = [by_instance[name] for name in order]


def _merge_clock(result: dict, section: dict) -> None:
    _keys(section, ("params", "targets", "board_defaults"), "set.clock")
    if section.get("params"):
        result["clock"]["params"] = dict(result["clock"].get("params") or {})
        result["clock"]["params"].update({key: str(value)
                                          for key, value in section["params"].items()})
    if section.get("targets"):
        result["clock"]["targets"] = list(section["targets"])
    if section.get("board_defaults") is not None:
        existing = list(result["clock"].get("board_defaults") or [])
        for name in section["board_defaults"]:
            if name not in existing:
                existing.append(str(name))
        result["clock"]["board_defaults"] = existing


def _merge_nvic(result: dict, section: dict) -> None:
    _keys(section, ("priority_group", "vectors", "flags"), "set.nvic")
    if section.get("priority_group"):
        result["nvic"]["priority_group"] = str(section["priority_group"])
    if section.get("flags"):
        result["nvic"]["flags"] = dict(result["nvic"].get("flags") or {})
        result["nvic"]["flags"].update({key: str(value)
                                        for key, value in section["flags"].items()})
    vectors = section.get("vectors") or []
    if vectors:
        _check_duplicates(vectors, ["irq"], "set.nvic.vectors")
        by_irq = {vector["irq"]: vector for vector in result["nvic"]["vectors"]}
        order = [vector["irq"] for vector in result["nvic"]["vectors"]]
        for raw in vectors:
            item = dict(raw)
            irq = item["irq"]
            if irq in by_irq:
                merged = dict(by_irq[irq])
                merged.update(item)
                by_irq[irq] = merged
            else:
                by_irq[irq] = item
                order.append(irq)
        result["nvic"]["vectors"] = [by_irq[irq] for irq in order]


def _merge_dma(result: dict, section: list) -> None:
    for index, raw in enumerate(section):
        if not isinstance(raw, dict) or not raw.get("request"):
            raise IocUnknown("set.dma[%d] requires {request, instance}" % index,
                             "set.dma[%d]" % index)
    seen = {}
    for index, item in enumerate(section):
        request = str(item["request"])
        instance = str(item.get("instance") or "")
        if not instance:
            same = [entry for entry in result["dma"] if entry["request"] == request]
            if len(same) > 1 or any(entry["request"] == request for entry in section
                                    if entry is not item):
                raise IocConflict("DMA request %s has several paths; set.dma must name the instance explicitly"
                                  % request, "set.dma[%d]" % index)
        key = (request, instance)
        if key in seen:
            raise IocConflict("the same request appears twice in set.dma: %s/%s"
                              % (request, instance or "?"), "set.dma")
        seen[key] = index
    by_key = {(entry["request"], str(entry.get("instance") or "")): entry
              for entry in result["dma"]}
    order = [(entry["request"], str(entry.get("instance") or ""))
             for entry in result["dma"]]
    for raw in section:
        item = dict(raw)
        key = (str(item["request"]), str(item.get("instance") or ""))
        if key in by_key:
            merged = dict(by_key[key])
            merged["params"] = dict(merged.get("params") or {})
            merged["params"].update({name: str(value)
                                     for name, value in (item.get("params") or {}).items()})
            by_key[key] = merged
        else:
            by_key[key] = item
            order.append(key)
    result["dma"] = [by_key[key] for key in order]


def _merge_rtos(result: dict, section: dict) -> None:
    """raw form: the rtos section holds arrays directly by kind (tasks/queues/...)."""
    _keys(section, ("instance",) + tuple(RTOS_KINDS), "set.rtos")
    current = dict(result.get("rtos") or {})
    if section.get("instance"):
        current["instance"] = str(section["instance"])
    for kind in RTOS_KINDS:
        rows = section.get(kind)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise IocUnknown("set.rtos.%s should be an array" % kind, "set.rtos.%s" % kind)
        _check_duplicates(rows, ["name"], "set.rtos.%s" % kind)
        existing = list(current.get(kind) or [])
        by_name = {row.get("name"): dict(row) for row in existing}
        order = [row.get("name") for row in existing]
        for raw in rows:
            item = dict(raw)
            name = item.get("name")
            if name in by_name:
                merged = dict(by_name[name])
                merged.update(item)
                by_name[name] = merged
            else:
                by_name[name] = item
                order.append(name)
        current[kind] = [by_name[name] for name in order]
    useful = current.get("instance") or any(current.get(kind) for kind in RTOS_KINDS)
    if useful:
        result["rtos"] = current
    else:
        result.pop("rtos", None)

def _merge_constants(result: dict, section: dict) -> None:
    result["constants"] = dict(result.get("constants") or {})
    result["constants"].update({key: str(value) for key, value in section.items()})


SET_MERGERS = {
    "device": _merge_device,
    "project": _merge_project,
    "pins": _merge_pins,
    "ips": _merge_ips,
    "clock": _merge_clock,
    "nvic": _merge_nvic,
    "dma": _merge_dma,
    "rtos": _merge_rtos,
    "constants": _merge_constants,
}


def _check_ip_pin_conflict(result: dict, removed_ips) -> None:
    """An IP was removed but pins still use its signals -> report the specific relation instead of deleting silently."""
    if not removed_ips:
        return
    for pin in result["pins"]:
        signal = str(pin.get("signal") or "")
        for instance in removed_ips:
            prefix = instance + "_"
            if signal.startswith(prefix) or signal.endswith("_" + instance):
                raise IocConflict(
                    "removed IP %s, but pin %s still uses its signal %s; remove or change that pin"
                    % (instance, pin["pin"], signal), "remove.ips")


def _collect_identities(edit: dict) -> dict:
    """Object identities seen in set/remove (used to report 'the same object cannot be on both sides')."""
    counts = {}

    def add(kind, name):
        key = (kind, str(name))
        counts[key] = counts.get(key, 0) + 1

    set_part = edit.get("set") or {}
    remove_part = edit.get("remove") or {}
    for kind, field in (("pin", "pin"), ("ip", "instance")):
        for item in set_part.get(kind == "pin" and "pins" or "ips") or []:
            if isinstance(item, dict) and item.get(field):
                add(kind, item[field])
        for item in remove_part.get(kind == "pin" and "pins" or "ips") or []:
            name = item.get(field) if isinstance(item, dict) else item
            if name:
                add(kind, name)
    for item in set_part.get("params") or []:
        if isinstance(item, dict) and item.get("instance") and item.get("name"):
            add("param", "%s/%s" % (item["instance"], item["name"]))
    for item in remove_part.get("params") or []:
        if isinstance(item, dict) and item.get("instance") and item.get("name"):
            add("param", "%s/%s" % (item["instance"], item["name"]))
    for name in (set_part.get("constants") or {}):
        add("constant", name)
    for name in remove_part.get("constants") or []:
        add("constant", name.get("name") if isinstance(name, dict) else name)
    return counts

def apply_edit(base: dict, edit, base_contract: str | None = None) -> dict:
    """Apply an edit to base and return the new high-level target (the argument is not modified)."""
    edit = _obj(edit, "edit")
    _keys(edit, ("contract", "base", "set", "remove"), "edit")
    contract = edit.get("contract")
    if contract is not None and contract != CONTRACT:
        raise IocUnknown("edit contract must be %r (got %r)"
                         % (CONTRACT, contract), "contract")
    if edit.get("base") is not None:
        base = edit["base"]
    if base is None:
        raise IocUnknown("no base: the edit has no base and the command line did not give one", "base")
    result = {**base}
    # deep-copy the sections that may be modified
    import copy
    result = copy.deepcopy(base)
    set_part = edit.get("set") or {}
    remove_part = edit.get("remove") or {}
    _keys(set_part, SET_SECTIONS, "set")
    identities = _collect_identities(edit)
    duplicated = [key for key, count in identities.items() if count > 1]
    if duplicated:
        raise IocConflict(
            "the same object appears in both set and remove (or twice on one side): %s"
            % ", ".join("%s %s" % (kind, name) for kind, name in sorted(duplicated)),
            "edit")

    removed_ips = set()
    if remove_part:
        before = {ip["instance"] for ip in result["ips"]}
        _apply_remove(result, remove_part)
        removed_ips = before - {ip["instance"] for ip in result["ips"]}
    for section in ("device", "project", "pins", "ips", "clock", "nvic", "dma",
                    "rtos", "constants"):
        if section in set_part:
            SET_MERGERS[section](result, set_part[section])
    _check_ip_pin_conflict(result, removed_ips)
    if base_contract and base_contract != result.get("contract"):
        result["contract"] = base_contract
    # normalization self-check: the result must still be a valid target
    target_contract.parse(result)
    return result