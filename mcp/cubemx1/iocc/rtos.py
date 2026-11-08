"""RTOS object tables: the rtos section of the target JSON -> hidden parameter rows of FREERTOS.

Basis: case-by-case measurement + behaviour checks (details are in the development docs, not shipped). Key points:

- object tables are not under rtos.*; they are hidden parameters of the FREERTOS IP (Visible=false in XML): Tasks01 / Queues01 / Timers01 / Mutexes01 / RecursiveMutexes01 / BinarySemaphores01 / CountingSemaphores01, one table per class packed into one parameter value.
- rows are separated by ; and columns by ,; the plugin writes only the first key of each class (Tasks02 is never written).
- loading reads only the names listed in IPParameters (that is the loader whitelist), so the list must be assembled from the params keys automatically.
"""

from __future__ import annotations

import re

from .. import rtosobjects
from ..errors import CubemxError
from . import derive
from .errors import IocConflict, IocUnknown, IocUnsupported

KINDS = ("tasks", "queues", "timers", "mutexes", "recursive_mutexes",
         "binary_semaphores", "counting_semaphores")

#: CMSIS_V2 priority names -> numbers (measured: the conversion table in Mode).
_PRIO_NAMES_V2 = {"idle": 1, "low": 8, "belownormal": 16, "normal": 24,
                  "abovenormal": 32, "high": 40, "realtime": 48}


def _mode_kind(mode) -> str:
    name = (getattr(mode, "internal_name", "") or "").upper()
    if "CMSIS_V2" in name:
        return "V2"
    if "CMSIS_V1" in name:
        return "V1"
    return "NATIVE"


def _config_max_priorities(ip) -> int:
    raw = derive.static_default(ip, "configMAX_PRIORITIES")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 56


def _min_stack(ip) -> int:
    raw = derive.static_default(ip, "configMINIMAL_STACK_SIZE")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 128


def _priority(value, mode, ip, where: str) -> int:
    raw = str(value).strip()
    kind = _mode_kind(mode)
    if re.fullmatch(r"-?\d+", raw):
        number = int(raw)
    else:
        key = raw.lower().replace("ospriority", "")
        if kind == "V2" and key in _PRIO_NAMES_V2:
            number = _PRIO_NAMES_V2[key]
        else:
            raise IocUnknown(
                "%s: priority=%r is not recognized. This field accepts numbers only (or a CMSIS_V2 name: %s)"
                % (where, raw, ", ".join(sorted(_PRIO_NAMES_V2))), where)
    if kind == "V2":
        if 2 <= number <= 7:
            raise IocUnknown(
                "%s: priority=%d falls in the range CubeMX normalizes (2..7 become 24)."
                " write 1 or 8..56." % (where, number), where)
        if not (number == 1 or 8 <= number <= 56):
            raise IocUnknown(
                "%s: priority=%d is out of range. CMSIS_V2 accepts only 1 or 8..56."
                % (where, number), where)
    elif kind == "V1":
        if not -3 <= number <= 3:
            raise IocUnknown("%s: priority=%d is out of range. CMSIS_V1 accepts only -3..3."
                             % (where, number), where)
    else:
        limit = _config_max_priorities(ip)
        if not 0 <= number < limit:
            raise IocUnknown(
                "%s: priority=%d is out of range. The native API accepts only 0..%d."
                % (where, number, limit - 1), where)
    return number


def _check_names(kind: str, rows) -> None:
    seen = {}
    for index, row in enumerate(rows):
        name = row.get("name")
        if name in seen:
            raise IocConflict(
                "rtos.%s[%d]: name %r duplicates [%d]" % (kind, index, name, seen[name]),
                "rtos.%s" % kind)
        seen[name] = index


_IDENT = re.compile(r"[A-Za-z_]\w*")


def _ident_value(value, where: str, field: str) -> str:
    text = str(value or "")
    if not _IDENT.fullmatch(text):
        raise IocUnknown("%s: %s=%r is not a valid C identifier" % (where, field, value), where)
    return text


def apply(model, machine, device, ip, mode, objects: dict):
    """Validate + serialize. Return (params, notes); params are merged into the FREERTOS parameter dict directly."""
    objects = dict(objects or {})
    notes = []
    # event groups: the parameters are created by the FreeRTOS plugin at runtime (the FREERTOS mode file in DB does not declare
    # EventsKeys/Events01, but the Events page of the GUI works under CMSIS_V2, and saving writes
    # FREERTOS.Events01). So the row is assembled here, without rtosobjects (which decides availability from DB declarations).
    events = objects.pop("events", None)
    extra_params = {}
    if events:
        if not isinstance(events, list):
            raise IocUnknown("rtos.events must be an array", "rtos.events")
        if _mode_kind(mode) != "V2":
            # the plugin decides whether event groups take effect from the CMSIS API version: only V2 uses the event table,
            # under V1 / native API no code reads a written Events01.
            raise IocUnsupported(
                "rtos.events only exists under CMSIS_V2; CMSIS_V1 / native API have no event groups,"
                "so writing them generates no code. Change the middleware mode to CMSIS_V2 to use event groups.",
                "rtos.events")
        _check_names("events", events)
        rows = []
        for index, row in enumerate(events):
            where = "rtos.events[%d]" % index
            name = _ident_value(row.get("name"), where, "name")
            alloc = str(row.get("allocation") or "Dynamic")
            if alloc not in ("Dynamic", "Static"):
                raise IocUnknown("%s: allocation must be Dynamic / Static" % where, where)
            ctrl = ("NULL" if alloc == "Dynamic"
                    else _ident_value(row.get("control_block") or (name + "ControlBlock"),
                                      where, "control_block"))
            rows.append("%s,%s,%s" % (name, alloc, ctrl))
        extra_params["Events01"] = ";".join(rows)
        notes.append("RTOS event groups: the parameters are created by the plugin at runtime and are not declared in the DB;"
                     "CubeMX prints one 'Invalid parameter' line on load (already allowed) and generation works.")
    rows = objects.get("tasks") or []
    if rows:
        _check_names("tasks", rows)
        first = str(rows[0].get("name") or "")
        if first != "defaultTask":
            minimum = _min_stack(ip)
            default_prio = 24 if _mode_kind(mode) == "V2" else 0
            rows.insert(0, {"name": "defaultTask", "priority": default_prio,
                            "stack": minimum, "entry": "StartDefaultTask"})
            notes.append("RTOS: the first row of the task table must be the default task; a row was prepended"
                         " defaultTask (the code template uses it as the default task)")
    for kind in KINDS:
        items = objects.get(kind)
        if not items:
            continue
        if not isinstance(items, list):
            raise IocUnknown("rtos.%s must be an array" % kind, "rtos.%s" % kind)
        if kind != "tasks":
            _check_names(kind, items)
        if kind == "tasks":
            minimum = _min_stack(ip)
            for index, row in enumerate(items):
                where = "rtos.tasks[%d]" % index
                row["priority"] = _priority(row.get("priority"), mode, ip, where)
                try:
                    stack = int(str(row.get("stack")))
                except (TypeError, ValueError):
                    raise IocUnknown("%s: stack must be an integer (unit: words)" % where, where)
                if stack < minimum:
                    raise IocUnknown(
                        "%s: stack=%d is below the minimum %d of this chip"
                        % (where, stack, minimum), where)
                row["stack"] = stack
    if objects.get("queues"):
        for index, row in enumerate(objects["queues"]):
            where = "rtos.queues[%d]" % index
            try:
                size = int(str(row.get("size")))
            except (TypeError, ValueError):
                raise IocUnknown("%s: size must be a positive integer" % where, where)
            if size <= 0:
                raise IocUnknown("%s: size=%d is invalid: queue length must be > 0"
                                 % (where, size), where)
            item = str(row.get("item_size") or "")
            if not (item.isdigit() or re.fullmatch(r"[A-Za-z_]\w*", item)):
                raise IocUnknown("%s: item_size=%r must be an integer or a type name"
                                 % (where, item), where)
    if objects.get("counting_semaphores"):
        for index, row in enumerate(objects["counting_semaphores"]):
            where = "rtos.counting_semaphores[%d]" % index
            try:
                top = int(str(row.get("max_count")))
                start = int(str(row.get("initial_count") or 0))
            except (TypeError, ValueError):
                raise IocUnknown("%s: max_count/initial_count must be integers"
                                 % where, where)
            if top < 0 or not 0 <= start <= top:
                raise IocUnknown(
                    "%s requires max_count >= initial_count >= 0 (got %d / %d)"
                    % (where, top, start), where)
    # when only event groups remain, objects is already popped empty: then the 'serialize by DB declaration'
    # path is not needed (the DB has no event table), otherwise it is rejected as 'no objects'.
    params = {}
    if objects:
        try:
            serialized = rtosobjects.serialize(machine, device.refname, objects)
        except rtosobjects.NoObjectsInDb as exc:
            # the db has no such object table: a capability the plugin does not offer
            raise IocUnsupported(str(exc), "rtos")
        except CubemxError as exc:
            raise IocUnknown(str(exc), "rtos")
        params = dict(serialized["params"])
    params.update(extra_params)
    return params, notes
