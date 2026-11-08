"""FreeRTOS object tables: structured JSON <-> CubeMX table rows.

## What problem this solves

CubeMX stores FreeRTOS tasks/queues/timers/semaphores as **comma-separated table
rows**; the parameter names are `Tasks01` / `Queues01` ... and a value looks like
this:

    defaultTask,24,128,StartDefaultTask,Default,NULL,Dynamic,NULL,NULL

The caller would have to know what each of the 9 columns means. **CubeMX does not
validate a wrong column** (it takes whatever is there) and the generated code is
then wrong -- so this layer translates "I want a ledTask, priority 24, stack 128"
into that row, and translates it back on read-back to check it.

## Field layout ([measured] every entry verified, not guessed)

The source is the FreeRTOS plugin data shipped with CubeMX. **Both the write side
and the load side were read**, and only indices that agree on both sides count:

===========================  ================================================  ==
Object class                 Row fields (in order)                             #
===========================  ================================================  ==
`tasks`                      name, priority, stack, entry, option,             9
                             parameters, allocation, buffer, control_block
`queues`                     name, size, item_size, **flag (derived)**,        7
                             allocation, buffer, control_block
`timers`                     name, callback, type, option, parameters,         7
                             allocation, control_block
`mutexes`                    name, allocation, control_block, state            4
`recursive_mutexes`          name, allocation, control_block, state (blank)    4
`binary_semaphores`          name, allocation, control_block, state            4
`counting_semaphores`        name, max_count, allocation, control_block,       5
                             initial_count
`events`                     name, allocation, buffer                          3
===========================  ================================================  ==

Evidence: the fields and the column order were checked against what CubeMX itself
saves (the load side and the write side must agree; per-item references live in the
development notes, this layer keeps only the conclusions).

- The load side branches on the column count: a short row is filled with defaults,
  a very short one is dropped entirely, **with no error**.
- The write side has a fixed shape: one object kind becomes one parameter value,
  `;` between rows and `,` between columns.
- Column names and indices are read from the CubeMX plugin data.

**Column 4 of a queue is derived by CubeMX** (from whether the first character of
the type string is a digit), so the caller does not supply it and the tool computes
it with the same rule.

## Two mechanisms (ignore them and the write is wasted)

1. **One parameter value may hold several rows, separated by `;`**. When CubeMX
   saves, it concatenates the whole table into **one** row parameter, so this does
   the same -- it never invents `Tasks03`-style names that CubeMX would not know.
2. **Which row parameter takes effect is decided by `<Kind>Keys`**. So the rows go
   to the **first** name listed in Keys, and Keys itself is left alone: changing it
   would lose rows the user added in the GUI.

## Which object kinds are available: discovered from the db, not hardcoded

Each kind is decided by whether the `<Kind>Keys` parameter exists in the db. The
FREERTOS definitions of this machine's db declare no Keys for event groups, so
`events` is not handled in this module: its parameters are created by the plugin at
run time and the upper layer builds them (see `iocc/rtos.py`, which accepts them
only under CMSIS_V2).

## Automatic configUSE_*

CubeMX hands only the tables whose switch is 1 to code generation; binary
semaphores, queues and tasks are unconditional. In other words: **writing rows
without setting the switch means the objects never reach the generated code**. So
declaring an object kind also turns its switch on.
"""

from __future__ import annotations

import re

from .errors import CubemxError
from .ipxml import find_ip, modes_xml_path, ref_parameters


class NoObjectsInDb(CubemxError):
    """The db declares no object table for this kind, so writing the rows has no effect.

    A dedicated type instead of matching the message text: the caller has to turn
    this case into "unsupported" (a capability the plugin does not offer) while a
    malformed row stays "unknown" (bad input). Text matching would silently degrade
    as soon as the wording changes.
    """

# C identifier: names, entry functions, callbacks, buffer and control block names
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Allocation (measured: those two drop-down entries are all there is)
_ALLOCATIONS = ("Dynamic", "Static")

# Code generation option (measured: those three drop-down entries)
_OPTIONS = ("Default", "As external", "As weak")

# Timer type (NATIVE uses the first pair, CMSIS the second)
_TIMER_TYPES = ("osTimerOnce", "osTimerPeriodic", "one-shot", "auto-reload")

# Initial state of a semaphore or mutex (measured: those two entries)
_STATES = ("Available", "Depleted")

# Object kind definitions. `fields` is the **row field order** (evidence in the
# module docstring).
KINDS = {
    "tasks": {
        "keys": "TasksKeys", "max": "MaxTasks",
        "fields": ("name", "priority", "stack", "entry", "option",
                   "parameters", "allocation", "buffer", "control_block"),
        "columns": ("Task Name", "Priority", "Stack Size (Words)",
                    "Entry Function", "Code Generation Option", "Parameter",
                    "Allocation", "Buffer Name", "Control Block Name"),
        "required": ("name", "priority", "stack", "entry"),
        "use": None,
    },
    "queues": {
        "keys": "QueuesKeys", "max": "MaxQueues",
        "fields": ("name", "size", "item_size", "is_integer",
                   "allocation", "buffer", "control_block"),
        "columns": ("Queue Name", "Queue Size", "Item Size", "Allocation",
                    "Buffer Name", "Control Block Name"),
        "required": ("name", "size", "item_size"),
        "use": None,
    },
    "timers": {
        "keys": "TimersKeys", "max": "MaxTimers",
        "fields": ("name", "callback", "type", "option", "parameters",
                   "allocation", "control_block"),
        "columns": ("Timer Name", "Callback", "Type", "Code Generation Option",
                    "Parameter", "Allocation", "Control Block Name"),
        "required": ("name", "callback", "type"),
        "use": "configUSE_TIMERS",
    },
    "mutexes": {
        "keys": "MutexesKeys", "max": "MaxMutexes",
        "fields": ("name", "allocation", "control_block", "state"),
        "columns": ("Mutex Name", "Allocation", "Control Block Name", "State"),
        "required": ("name",),
        "use": "configUSE_MUTEXES",
    },
    "recursive_mutexes": {
        "keys": "RecursiveMutexesKeys", "max": "MaxRecursiveMutexes",
        "fields": ("name", "allocation", "control_block", "state"),
        "columns": ("Mutex Name", "Allocation", "Control Block Name", "State"),
        "required": ("name",),
        "use": "configUSE_RECURSIVE_MUTEXES",
    },
    "binary_semaphores": {
        "keys": "BinarySemaphoresKeys", "max": "MaxBinarySemaphores",
        "fields": ("name", "allocation", "control_block", "state"),
        "columns": ("Semaphore Name", "Allocation", "Control Block Name",
                    "Initial State"),
        "required": ("name",),
        "use": None,
    },
    "counting_semaphores": {
        "keys": "CountingSemaphoresKeys", "max": "MaxCountingSemaphores",
        "fields": ("name", "max_count", "allocation", "control_block",
                   "initial_count"),
        "columns": ("Semaphore Name", "Max Count", "Allocation",
                    "Control Block Name", "Initial Count"),
        "required": ("name", "max_count"),
        "use": "configUSE_COUNTING_SEMAPHORES",
    },
    "events": {
        "keys": "EventsKeys", "max": None,
        "fields": ("name", "allocation", "buffer"),
        "columns": ("Event flags Name", "Allocation", "Control Block Name"),
        "required": ("name",),
        "use": None,
    },
}

# These fields are derived by the tool with the rule CubeMX uses; the caller may not
# set them (so they cannot be wrong)
_DERIVED = {"is_integer": "column 4 of a queue is derived by CubeMX from whether "
                          "item_size is numeric"}


def _freertos_specs(machine, refname: str) -> dict:
    e = find_ip(machine, refname, "FREERTOS")
    if e is None:
        raise CubemxError("%s has no FREERTOS entry in its device XML" % refname)
    return ref_parameters(modes_xml_path(machine, e))


def discover(machine, refname: str) -> dict:
    """Which object kinds the FREERTOS of this chip supports in the db (without
    starting CubeMX).

    **The criterion is the presence of the `<Kind>Keys` parameter**: absent from the
    db means not listed (measured: H750 has no events).
    """
    sp = _freertos_specs(machine, refname)
    out = []
    for kind, spec in KINDS.items():
        if spec["keys"] not in sp:
            continue
        mx = sp.get(spec["max"]) if spec["max"] else None
        limit = None
        if mx is not None and mx.get("default") is not None:
            try:
                limit = int(float(mx["default"]))
            except (TypeError, ValueError):
                limit = None
        out.append({
            "kind": kind,
            "keys_parameter": spec["keys"],
            "row_parameter": (sp[spec["keys"]].get("default") or "").split(";")[0],
            "limit": limit,
            "fields": list(spec["fields"]),
            "columns": list(spec["columns"]),
            "required": list(spec["required"]),
            "config_use": spec["use"],
        })
    return {"device": refname, "kinds": out, "count": len(out)}


def _text(row: dict, field: str, where: str, default=None, allowed=None) -> str:
    v = row.get(field)
    if v is None or v == "":
        if default is None:
            raise CubemxError("%s is missing required field %s" % (where, field))
        v = default
    v = str(v)
    if allowed is not None and v not in allowed:
        raise CubemxError(
            "%s has %s=%r which is not allowed; allowed: %s"
            % (where, field, v, ", ".join(allowed)))
    return v


def _ident(row: dict, field: str, where: str) -> str:
    v = _text(row, field, where)
    if not _IDENT.match(v):
        raise CubemxError(
            "%s has %s=%r which is not a valid C identifier (a letter or underscore "
            "first, then letters, digits and underscores)"
            % (where, field, v))
    return v


def _alloc(row: dict, where: str):
    """Allocation mode, and the buffer / control_block names that follow it.

    Measured semantics (values and save behaviour of every table were checked):
    with Dynamic both buffer and control are `NULL`; with Static they are
    `<name>Buffer` / `<name>ControlBlock` (each table writes exactly those two
    columns, and the GUI fills them in with those names).
    """
    alloc = _text(row, "allocation", where, default="Dynamic",
                  allowed=_ALLOCATIONS)
    name = str(row.get("name") or "")
    if alloc == "Dynamic":
        return alloc, "NULL", "NULL"
    return (alloc,
            _text(row, "buffer", where, default="%sBuffer" % name),
            _text(row, "control_block", where, default="%sControlBlock" % name))


def _row_fields(kind: str, row: dict, i: int) -> dict:
    """Fold one structured object into "field name -> value"."""
    spec = KINDS[kind]
    where = "objects.%s[%d]" % (kind, i)
    if not isinstance(row, dict):
        raise CubemxError("%s must be an object, got %s" % (where, type(row).__name__))
    for f in row:
        if f in _DERIVED:
            raise CubemxError(
                "%s must not set %s: %s" % (where, f, _DERIVED[f]))
        if f not in spec["fields"]:
            raise CubemxError(
                "%s has unknown field %r; fields of %s are: %s"
                % (where, f, kind, ", ".join(spec["fields"])))
    for f in spec["required"]:
        if row.get(f) in (None, ""):
            raise CubemxError("%s is missing required field %s" % (where, f))

    out = {}
    for f in spec["fields"]:
        if f in ("buffer", "control_block"):
            continue                     # computed together with allocation
        if f == "is_integer":
            item = _text(row, "item_size", where)
            out[f] = "1" if item[:1].isdigit() else "0"   # measured derivation
        elif f == "allocation":
            continue
        elif f == "name":
            out[f] = _ident(row, "name", where)
        elif f in ("entry", "callback"):
            out[f] = _ident(row, f, where)
        elif f == "option":
            out[f] = _text(row, "option", where, default="Default",
                           allowed=_OPTIONS)
        elif f == "parameters":
            out[f] = _text(row, "parameters", where, default="NULL")
        elif f == "type":
            out[f] = _text(row, "type", where, allowed=_TIMER_TYPES)
        elif f == "state":
            if kind == "mutexes":
                # measured: column 4 is **dropped** and fixed to Available
                want = row.get("state")
                if want not in (None, "", "Available"):
                    raise CubemxError(
                        "%s only accepts state Available (CubeMX fixes the initial "
                        "state of a plain mutex)" % where)
                out[f] = "Available"
            elif kind == "recursive_mutexes":
                want = row.get("state")
                if want not in (None, "", "Available"):
                    raise CubemxError(
                        "%s only accepts an empty state or Available (CubeMX "
                        "ignores this column for a recursive mutex)" % where)
                out[f] = ""
            else:
                out[f] = _text(row, "state", where, default="Depleted",
                               allowed=_STATES)
        else:
            out[f] = _text(row, f, where)

    alloc, buf, ctrl = _alloc(row, where)
    if "allocation" in spec["fields"]:
        out["allocation"] = alloc
    if "buffer" in spec["fields"]:
        out["buffer"] = buf
    if "control_block" in spec["fields"]:
        out["control_block"] = ctrl
    return out


def serialize(machine, refname: str, objects: dict) -> dict:
    """Structured objects -> the parameters written into the project (row strings and
    the configUSE_* switches).

    :param objects: ``{"tasks": [ {...}, ... ], "queues": [...]}``.
    :returns: ``{"params": {...}, "rows": {kind: [row strings...]}, "flags": {...}}``
    """
    if not isinstance(objects, dict) or not objects:
        raise CubemxError("objects must be a non-empty object, like "
                          '{"tasks": [{"name": "ledTask", ...}]}')
    avail = {x["kind"]: x for x in discover(machine, refname)["kinds"]}
    params, rows, flags = {}, {}, {}
    fields_by_kind = {}
    for kind, items in objects.items():
        if kind not in KINDS:
            raise CubemxError(
                "unknown object kind %r; available: %s" % (kind, ", ".join(KINDS)))
        if kind not in avail:
            raise NoObjectsInDb(
                "%s has no %s objects in its db (missing the %s parameter); writing "
                "them has no effect" % (refname, kind, KINDS[kind]["keys"]))
        if not isinstance(items, list) or not items:
            raise CubemxError("objects.%s must be a non-empty array" % kind)
        limit = avail[kind]["limit"]
        if limit and len(items) > limit:
            raise CubemxError(
                "objects.%s has %d entries, above the db limit %s=%d"
                % (kind, len(items), KINDS[kind]["max"], limit))
        spec = KINDS[kind]
        texts, fields = [], []
        for i, row in enumerate(items):
            fv = _row_fields(kind, row, i)
            fields.append(fv)
            texts.append(",".join(fv[f] for f in spec["fields"]))
        rows[kind] = texts
        fields_by_kind[kind] = fields
        # Several rows in one parameter value, separated by `;` (same as CubeMX)
        params[avail[kind]["row_parameter"] or
               (spec["keys"].replace("Keys", "") + "01")] = ";".join(texts)
        if spec["use"]:
            flags[spec["use"]] = "1"
    params.update(flags)
    return {"device": refname, "params": params, "rows": rows,
            "fields": fields_by_kind, "flags": flags, "objects": objects}


def parse(machine, refname: str, kind: str, text: str) -> list:
    """Row string -> structured (for read-back). **Same field table as
    serialize.**"""
    if kind not in KINDS:
        raise CubemxError("unknown object kind %r" % kind)
    spec = KINDS[kind]
    out = []
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != len(spec["fields"]):
            raise CubemxError(
                "%s row has %d columns, expected the %d measured columns: %r"
                % (kind, len(parts), len(spec["fields"]), chunk))
        out.append(dict(zip(spec["fields"], parts)))
    return out


def compare(kind: str, want: list, got: list) -> list:
    """Compare "what was written" with "what was read back" item by item and return
    the differences (empty means all equal)."""
    spec = KINDS[kind]
    bad = []
    if len(want) != len(got):
        return ["%s row count differs: wrote %d rows, read back %d rows"
                % (kind, len(want), len(got))]
    for i, (w, g) in enumerate(zip(want, got)):
        for f in spec["fields"]:
            wv = str(w[f])
            gv = str(g.get(f, ""))
            if f == "is_integer":
                continue          # derived column, not compared
            if wv != gv:
                bad.append("%s[%d].%s: expected %r, read back %r" % (kind, i, f, wv, gv))
    return bad
