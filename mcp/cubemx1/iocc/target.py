"""Target JSON: shape validation and normalization.

Only answers 'is this target written correctly'. Whether an instance exists, a mode name is known, or a parameter name exists is checked by the build stage against the probe model (not here, to keep the two concerns apart).

Unknown fields are always rejected: better to fail on the spot than to 'ignore for now'. Ignoring is silent loss.
"""

from __future__ import annotations

import re

from .errors import TargetError

CONTRACT = "cubemx1.ioc/1"
#: supported contract versions. v2 adds extensions (non-semantic IOC keys) and ips[].modes on top of v1.
CONTRACTS = {"cubemx1.ioc/1": 1, "cubemx1.ioc/2": 2}

TOOLCHAINS = {"CMake", "Makefile", "STM32CubeIDE"}

TOP = {"contract", "device", "project", "pins", "ips", "clock", "nvic",
       "dma", "rtos", "constants"}
DEVICE = {"refname", "cpn"}
PROJECT = {"name", "output_dir", "toolchain", "heap", "stack", "library_copy"}
PROJECT_V2 = PROJECT | {"extra", "options"}
PIN = {"pin", "signal", "mode", "locked", "gpio"}
PIN_V2 = PIN | {"shared"}
SHARED_SLOT = {"signal", "mode"}
IP = {"instance", "mode", "params"}
IP_V2 = IP | {"modes"}
MODE_REF = {"name", "selected"}
TOP_V2 = TOP | {"extensions", "board", "allocation", "connections"}
ALLOCATION = {"pins", "dma", "tie_break", "release"}
CONNECTION = {"signals", "endpoint_pins", "name"}
ALLOC_MODES = ("auto", "manual")
TIE_BREAKS = ("stable",)
EXTENSIONS = {"ioc"}
BOARD = {"id", "initialize"}
BOARD_INITIALIZE = ("defaults", "materialized")
CLOCK = {"params", "targets"}
CLOCK_V2 = CLOCK | {"board_defaults"}
NVIC = {"priority_group", "vectors"}
NVIC_V2 = NVIC | {"flags"}
VECTOR = {"irq", "priority", "subpriority", "enabled"}
VECTOR_V2 = VECTOR | {"raw"}
CLOCK_TARGET = {"parameter", "peripheral", "mhz"}
DMA = {"request", "instance", "params"}
RTOS = {"instance", "tasks", "queues", "timers", "mutexes",
        "recursive_mutexes", "binary_semaphores", "counting_semaphores",
        "events"}
RTOS_KINDS = ("tasks", "queues", "timers", "mutexes", "recursive_mutexes",
              "binary_semaphores", "counting_semaphores", "events")


def _obj(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise TargetError("expected an object here", where)
    return value


def _keys(obj: dict, allowed: set, where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise TargetError(
            "unrecognized field: %s (this version supports only: %s)"
            % (", ".join(extra), ", ".join(sorted(allowed))), where)


def _str(obj: dict, key: str, where: str, required: bool = True) -> str:
    if key not in obj or obj[key] in (None, ""):
        if required:
            raise TargetError("missing required field %r" % key, where)
        return ""
    if not isinstance(obj[key], str):
        raise TargetError("%r must be a string" % key, where)
    return obj[key].strip()


def _str_map(value, where: str) -> dict:
    obj = _obj(value, where)
    out = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            raise TargetError("key/value pairs must be string -> string", where)
        out[k] = str(v)
    return out


def parse(data) -> dict:
    """Validate and normalize a target. Return a dict that is ready to use (fixed shape; backward compatibility comes from contract)."""
    root = _obj(data, "target")
    # a normalized target carries internal markers and 'None for absent sections'; strip them before validating.
    root = {key: value for key, value in root.items()
            if key != "contract_version" and value is not None}
    contract = _str(root, "contract", "target")
    version = CONTRACTS.get(contract)
    if version is None:
        raise TargetError("contract must be %s (got %r)"
                          % (" or ".join(sorted(CONTRACTS)), contract), "contract")
    _keys(root, TOP_V2 if version >= 2 else TOP, "target")

    device = _obj(root.get("device") or {}, "device")
    _keys(device, DEVICE, "device")
    # the device may be omitted: with a v2 board section it is resolved from the board mountedDevice (filled in by prepare).
    refname = _str(device, "refname", "device", required=False)
    cpn = _str(device, "cpn", "device", required=False)

    project = _obj(root.get("project"), "project")
    _keys(project, PROJECT_V2 if version >= 2 else PROJECT, "project")
    project_extra = _str_map(project.get("extra") or {}, "project.extra")
    project_options = _obj(project.get("options") or {}, "project.options")
    name = _str(project, "name", "project")
    output_dir = _str(project, "output_dir", "project")
    toolchain = _str(project, "toolchain", "project", required=False) or "CMake"
    if version < 2 and toolchain not in TOOLCHAINS:
        raise TargetError("toolchain supports only %s" % ", ".join(sorted(TOOLCHAINS)),
                          "project.toolchain")
    # v2: an imported project may carry another toolchain (kept as is; whether it can generate is decided higher up, not guessed here).

    pins = []
    raw_pins = root.get("pins", [])
    if not isinstance(raw_pins, list):
        raise TargetError("pins must be an array", "pins")
    seen = {}
    for i, raw in enumerate(raw_pins):
        where = "pins[%d]" % i
        item = _obj(raw, where)
        _keys(item, PIN_V2 if version >= 2 else PIN, where)
        pin_name = _str(item, "pin", where)
        signal = _str(item, "signal", where, required=False) or ""
        locked = bool(item.get("locked", False))
        if not signal:
            # reserved pin: BSP expansion locks a pin without assigning a signal (for example PB0 while the LED is off).
            # allowed only in v2 and it must be locked, so a normal target cannot silently write an empty pin.
            if version < 2:
                raise TargetError("missing required field 'signal'", where)
            if not locked:
                raise TargetError(
                    "pins with an empty signal must be locked=true (reserved pin)", where)
        shared = []
        for slot_index, raw_slot in enumerate(item.get("shared") or []):
            swhere = where + ".shared[%d]" % slot_index
            slot = _obj(raw_slot, swhere)
            _keys(slot, SHARED_SLOT, swhere)
            shared.append({"signal": _str(slot, "signal", swhere),
                           "mode": _str(slot, "mode", swhere, required=False)})
        if pin_name in seen:
            raise TargetError("the same pin is written twice: %s" % pin_name, where)
        seen[pin_name] = signal
        entry = {"pin": pin_name, "signal": signal,
                 "mode": _str(item, "mode", where, required=False),
                 "locked": locked,
                 "gpio": _str_map(item.get("gpio") or {}, where + ".gpio"),
                 "shared": shared}
        pins.append(entry)

    ips = []
    raw_ips = root.get("ips", [])
    if not isinstance(raw_ips, list):
        raise TargetError("ips must be an array", "ips")
    for i, raw in enumerate(raw_ips):
        where = "ips[%d]" % i
        item = _obj(raw, where)
        _keys(item, IP_V2 if version >= 2 else IP, where)
        modes = []
        for index, raw_mode in enumerate(item.get("modes") or []):
            mwhere = where + ".modes[%d]" % index
            mode_item = _obj(raw_mode, mwhere)
            _keys(mode_item, MODE_REF, mwhere)
            modes.append({"name": _str(mode_item, "name", mwhere),
                          "selected": bool(mode_item.get("selected", True))})
        ips.append({
            "instance": _str(item, "instance", where),
            "mode": _str(item, "mode", where, required=False),
            "params": _str_map(item.get("params") or {}, where + ".params"),
            "modes": modes,
        })

    clock = _obj(root.get("clock") or {}, "clock")
    _keys(clock, CLOCK_V2 if version >= 2 else CLOCK, "clock")
    clock_params = _str_map(clock.get("params") or {}, "clock.params")
    board_defaults = clock.get("board_defaults") or []
    if not isinstance(board_defaults, list):
        raise TargetError("clock.board_defaults must be an array of parameter names",
                          "clock.board_defaults")
    clock_targets = []
    for index, raw in enumerate(clock.get("targets") or []):
        where = "clock.targets[%d]" % index
        item = _obj(raw, where)
        _keys(item, CLOCK_TARGET, where)
        file_parameter = _str(item, "parameter", where, required=False)
        peripheral = _str(item, "peripheral", where, required=False)
        if bool(file_parameter) == bool(peripheral):
            raise TargetError("exactly one of parameter and peripheral must be given", where)
        if "mhz" not in item:
            raise TargetError("missing mhz (target frequency, unit MHz)", where)
        try:
            mhz = float(item["mhz"])
        except (TypeError, ValueError):
            raise TargetError("mhz must be a number", where)
        if mhz <= 0:
            raise TargetError("mhz must be greater than 0", where)
        clock_targets.append({"parameter": file_parameter,
                              "peripheral": peripheral, "mhz": mhz})

    nvic_in = _obj(root.get("nvic") or {}, "nvic")
    _keys(nvic_in, NVIC_V2 if version >= 2 else NVIC, "nvic")
    nvic_flags = _str_map(nvic_in.get("flags") or {}, "nvic.flags")
    vectors = []
    for i, raw in enumerate(nvic_in.get("vectors") or []):
        where = "nvic.vectors[%d]" % i
        item = _obj(raw, where)
        _keys(item, VECTOR_V2 if version >= 2 else VECTOR, where)
        vectors.append({
            "irq": _str(item, "irq", where),
            "priority": int(item.get("priority", 0)),
            "subpriority": int(item.get("subpriority", 0)),
            "enabled": bool(item.get("enabled", True)),
            "raw": _str(item, "raw", where, required=False),
        })

    dma = []
    raw_dma = root.get("dma", [])
    if not isinstance(raw_dma, list):
        raise TargetError("dma must be an array", "dma")
    for index, raw in enumerate(raw_dma):
        where = "dma[%d]" % index
        item = _obj(raw, where)
        _keys(item, DMA, where)
        dma.append({
            "request": _str(item, "request", where),
            "instance": _str(item, "instance", where, required=False),
            "params": _str_map(item.get("params") or {}, where + ".params"),
        })

    constants = _str_map(root.get("constants") or {}, "constants")
    for key in constants:
        if not re.fullmatch(r"[A-Za-z_]\w*", key):
            raise TargetError(
                "constant name %r is not a valid C identifier (must start with a letter or underscore)" % key,
                "constants")
    rtos = None
    if "rtos" in root:
        rtos_in = _obj(root.get("rtos"), "rtos")
        _keys(rtos_in, RTOS, "rtos")
        objects = {}
        for kind in RTOS_KINDS:
            raw_kind = rtos_in.get(kind)
            if raw_kind is None:
                continue
            if not isinstance(raw_kind, list):
                raise TargetError("rtos.%s must be an array" % kind, "rtos.%s" % kind)
            rows = []
            for index, raw_row in enumerate(raw_kind):
                where = "rtos.%s[%d]" % (kind, index)
                if not isinstance(raw_row, dict):
                    raise TargetError("each item of rtos.%s must be an object" % kind, where)
                rows.append(dict(raw_row))
            objects[kind] = rows
        rtos = {"instance": _str(rtos_in, "instance", "rtos",
                                 required=False) or "FREERTOS",
                "objects": objects}

    board = None
    if not refname and "board" not in root:
        raise TargetError("missing required field %r (or give board so the board decides the device)" % "refname",
                          "device")
    if "board" in root:
        if version < 2:
            raise TargetError("board is only available in cubemx1.ioc/2", "board")
        board_in = _obj(root.get("board"), "board")
        _keys(board_in, BOARD, "board")
        initialize = _str(board_in, "initialize", "board", required=False) or "defaults"
        if initialize not in BOARD_INITIALIZE:
            raise TargetError(
                "board.initialize must be %s" % " / ".join(BOARD_INITIALIZE),
                "board.initialize")
        board = {"id": _str(board_in, "id", "board"),
                 "initialize": initialize}

    allocation = None
    connections = []
    if version >= 2:
        if "allocation" in root:
            alloc_in = _obj(root.get("allocation"), "allocation")
            _keys(alloc_in, ALLOCATION, "allocation")
            pins_mode = _str(alloc_in, "pins", "allocation", required=False) or "manual"
            dma_mode = _str(alloc_in, "dma", "allocation", required=False) or "manual"
            tie_break = _str(alloc_in, "tie_break", "allocation", required=False) or "stable"
            if pins_mode not in ALLOC_MODES:
                raise TargetError("allocation.pins must be auto/manual", "allocation.pins")
            if dma_mode not in ALLOC_MODES:
                raise TargetError("allocation.dma must be auto/manual", "allocation.dma")
            if tie_break not in TIE_BREAKS:
                raise TargetError("allocation.tie_break currently supports only stable",
                                  "allocation.tie_break")
            release = alloc_in.get("release") or []
            if not isinstance(release, list):
                raise TargetError("allocation.release must be an array (pin names or signal names)",
                                  "allocation.release")
            allocation = {"pins": pins_mode, "dma": dma_mode,
                          "tie_break": tie_break,
                          "release": [str(item) for item in release]}
        for index, raw in enumerate(root.get("connections") or []):
            where = "connections[%d]" % index
            item = _obj(raw, where)
            _keys(item, CONNECTION, where)
            signals = item.get("signals")
            if not isinstance(signals, list) or not signals:
                raise TargetError("connections[%d].signals must be a non-empty array" % index, where)
            endpoint = item.get("endpoint_pins") or []
            if not isinstance(endpoint, list):
                raise TargetError("connections[%d].endpoint_pins must be an array" % index, where)
            connections.append({
                "name": _str(item, "name", where, required=False),
                "signals": [str(name) for name in signals],
                "endpoint_pins": [str(name) for name in endpoint],
            })

    extensions = {}
    if "extensions" in root:
        if version < 2:
            raise TargetError("extensions is only available in cubemx1.ioc/2", "extensions")
        ext_in = _obj(root.get("extensions"), "extensions")
        _keys(ext_in, EXTENSIONS, "extensions")
        if "ioc" in ext_in:
            extensions["ioc"] = _str_map(ext_in.get("ioc"), "extensions.ioc")

    result = {
        "contract": contract,
        "contract_version": version,
        "device": {"refname": refname, "cpn": cpn},
        "project": {
            "name": name,
            "output_dir": output_dir,
            "toolchain": toolchain,
            "heap": _str(project, "heap", "project", required=False) or "0x200",
            "stack": _str(project, "stack", "project", required=False) or "0x400",
            "library_copy": _str(project, "library_copy", "project",
                                  required=False) or "0",
            "extra": project_extra,
            "options": dict(project_options),
        },
        "pins": pins,
        "ips": ips,
        "clock": {"params": clock_params, "targets": clock_targets,
                  "board_defaults": [str(name) for name in board_defaults]},
        "nvic": {"priority_group": _str(nvic_in, "priority_group", "nvic",
                                        required=False) or "NVIC_PRIORITYGROUP_4",
                 "vectors": vectors,
                 "flags": nvic_flags},
        "dma": dma,
        "rtos": rtos,
        "constants": constants,
        "extensions": extensions,
        "board": board,
        "allocation": allocation,
        "connections": connections,
    }
    if version < 2:
        # the normalized result of v1 carries none of the v2 sections. Keeping them (even empty) would make a second parse
        # (board/host derivation/allocation re-parse afterwards) reject them as unknown fields.
        for key in ("extensions", "board", "allocation", "connections"):
            result.pop(key, None)
        result["project"].pop("extra", None)
        result["project"].pop("options", None)
        for pin in result["pins"]:
            pin.pop("shared", None)
        for ip in result["ips"]:
            ip.pop("modes", None)
        result["clock"].pop("board_defaults", None)
        result["nvic"].pop("flags", None)
        for vector in result["nvic"]["vectors"]:
            vector.pop("raw", None)
    return result