"""IOC -> high-level target JSON (cubemx1.ioc/2) reverse conversion.

Principles:
- understood objects go to high-level fields (pins/modes/IP parameters/NVIC/DMA/RTOS/constants/project/clock);
- unexplained keys go to extensions.ioc and are reported one by one (kept != understood);
- key and value semantics live in iocc/codecs.py, not in a second copy here;
- no data loss: every key either has an owner or goes to extensions, and overlaps must be reported as conflicts.
"""

from __future__ import annotations

from pathlib import Path

from . import codecs, props
from .errors import IocUnknown
from . import target as target_contract

#: RTOS row -> field names (column order comes from the measured codec rules).
RTOS_COLUMNS = {
    "Tasks": ("name", "priority", "stack", "entry", "option", "parameters",
              "allocation", "buffer", "control_block"),
    "Queues": ("name", "size", "item_size", "is_integer", "allocation",
               "buffer", "control_block"),
    "Timers": ("name", "callback", "type", "option", "parameters",
               "allocation", "control_block"),
    "Mutexes": ("name", "allocation", "control_block", "state"),
    "RecursiveMutexes": ("name", "allocation", "control_block", "state"),
    "BinarySemaphores": ("name", "allocation", "control_block", "state"),
    "CountingSemaphores": ("name", "max_count", "allocation",
                           "control_block", "initial_count"),
    "Events": ("name", "allocation", "control_block"),
}

#: RTOS row parameter name -> kind name inside the target rtos section.
RTOS_KIND_NAMES = {
    "Tasks": "tasks", "Queues": "queues", "Timers": "timers",
    "Mutexes": "mutexes", "RecursiveMutexes": "recursive_mutexes",
    "BinarySemaphores": "binary_semaphores",
    "CountingSemaphores": "counting_semaphores", "Events": "events",
}

#: derived/fixed columns of a row: read in but not written back to the target (build would derive them again).
DERIVED_FIELDS = {"is_integer", "state"}

#: these keys are regenerated at build time, so import does not put them in extensions (avoids self-conflicts).
REGENERATED_PREFIXES = ("File.Version", "MxCube.Version", "MxDb.Version",
                        "KeepUserPlacement")


def _parse_vector(value: str, irq: str):
    parts = [part for part in str(value).split(":")]
    if len(parts) < 3:
        raise IocUnknown("NVIC.%s value %r is not the three fields enabled:preemption:sub" % (irq, value),
                         "NVIC." + irq)
    try:
        return {
            "irq": irq,
            "enabled": parts[0].strip().lower() == "true",
            "priority": int(parts[1]),
            "subpriority": int(parts[2]),
        }
    except ValueError:
        raise IocUnknown("NVIC.%s priority is not an integer: %r" % (irq, value), "NVIC." + irq)


def _vectors(payload):
    out = []
    for name, value in (payload.get("vectors") or {}).items():
        vector = _parse_vector(value, name)
        vector["raw"] = str(value)
        out.append(vector)
    return out


def _rtos_objects(payload):
    objects = {}
    for row_name, rows in (payload.get("rows") or {}).items():
        kind = "".join(ch for ch in row_name if not ch.isdigit())
        columns = RTOS_COLUMNS.get(kind)
        if columns is None:
            continue
        out = []
        for row in rows:
            cells = row.split(",")
            item = {}
            for index, field in enumerate(columns):
                if field in DERIVED_FIELDS:
                    continue
                cell = cells[index] if index < len(cells) else ""
                item[field] = cell
            out.append(item)
        if out:
            objects[RTOS_KIND_NAMES[kind]] = out
    return objects


def _dma(payload):
    requests = list(payload.get("requests") or [])
    grouped = {}
    for entry in payload.get("entries") or []:
        request = entry["request"]
        slot = entry["slot"]
        key = (request, slot)
        bucket = grouped.setdefault(key, {"params": {}})
        if entry["param"] == "Instance":
            bucket["instance"] = entry["value"]
        elif entry["param"] == "RequestParameters":
            bucket.setdefault("request_parameters", [])
        else:
            bucket["params"][entry["param"]] = entry["value"]
    out = []
    for request in requests:
        slots = sorted(key for key in grouped if key[0] == request)
        for _request, slot in slots:
            bucket = grouped[(_request, slot)]
            out.append({"request": request,
                        "instance": bucket.get("instance") or "",
                        "params": bucket.get("params") or {}})
    return out


def _ip_modes(pins_payload):
    """Infer each IP mode name from the virtual pins VP_<instance>_VS_<mode>."""
    found = {}
    for item in pins_payload.get("pins") or []:
        name = item.get("pin") or ""
        if not name.startswith("VP_"):
            continue
        rest = name[len("VP_"):]
        instance, sep, suffix = rest.partition("_VS_")
        if not sep or not instance:
            continue
        # the real mode name is in .Mode (the virtual signal name is only a display name for <mode>; the two may differ).
        mode = item.get("mode") or suffix
        if not mode:
            continue
        found.setdefault(instance, [])
        if mode not in found[instance]:
            found[instance].append(mode)
    return found


def _ips(payload, pins_payload):
    modes_by_ip = _ip_modes(pins_payload)
    out = []
    for item in payload.get("ips") or []:
        instance = item["instance"]
        modes = modes_by_ip.get(instance) or []
        entry = {
            "instance": instance,
            "mode": modes[0] if len(modes) == 1 else "",
            "params": dict(item.get("params") or {}),
            "modes": [{"name": name, "selected": True} for name in modes],
        }
        out.append(entry)
    return out


def _shared_alternatives(payload):
    """Shared group -> [{signal, mode}, ...] (SH.<group>.<i> are alternative uses of the same pin)."""
    out = {}
    for group, data in (payload.get("shared") or {}).items():
        slots = data.get("slots") or {}
        entries = []
        for index in sorted(slots, key=lambda value: int(value) if value.isdigit() else 0):
            raw = str(slots[index])
            signal, sep, mode = raw.partition(",")
            if not signal.strip():
                continue
            entries.append({"signal": signal.strip(),
                            "mode": mode.strip() if sep else ""})
        if entries:
            out[group] = entries
    return out


def _shared_modes(payload):
    """Take the mode name a shared signal needs from SH.<group>.0=<real signal>,<mode>.

    Shared signals (remappable timer channels, FMC address/data lines, ADC shared channels) have no pin-level .Mode row in the .ioc; the mode is recorded only in the SH row, and a rebuild without it reports 'several modes'.
    """
    out = {}
    for group, data in (payload.get("shared") or {}).items():
        slots = data.get("slots") or {}
        if not slots:
            continue
        first = slots.get("0") or next(iter(slots.values()))
        signal, sep, mode = str(first).partition(",")
        if sep and mode:
            out[group] = mode.strip()
    return out


def _pins(payload):
    shared_modes = _shared_modes(payload)
    alternatives = _shared_alternatives(payload)
    out = []
    for item in payload.get("pins") or []:
        # virtual pins (VP_*) are a by-product of the mode selection and do not go to pins; they are regenerated from ips[].modes.
        if str(item.get("pin") or "").startswith("VP_"):
            continue
        entry = {"pin": item["pin"], "signal": item.get("signal") or ""}
        if entry["signal"] in alternatives:
            entry["shared"] = alternatives[entry["signal"]]
        elif not entry["signal"].startswith("GPIO_"):
            # signals such as GPIO_Input/GPIO_Output have no pin-level .Mode in real files and must not be filled in.
            mode = item.get("mode") or shared_modes.get(entry["signal"])
            if mode:
                entry["mode"] = mode
        if item.get("locked"):
            entry["locked"] = True
        gpio = item.get("gpio") or {}
        if gpio:
            entry["gpio"] = {name: str(value) for name, value in gpio.items()}
        out.append(entry)
    return out


def import_ioc(machine, path) -> dict:
    """Turn one .ioc into a cubemx1.ioc/2 target (self-contained). Return {"target", "report"}."""
    path = Path(str(path))
    doc = props.loads_file(path)
    version = doc.get("File.Version")
    if version is None:
        raise IocUnknown("the file has no File.Version; the format version cannot be determined", "File.Version")
    try:
        file_version = int(str(version))
    except ValueError:
        raise IocUnknown("File.Version is not an integer: %r" % version, "File.Version")
    if file_version != 6:
        raise IocUnknown(
            "this .ioc is File.Version=%d; this tool only decodes v6, migration of older versions is not implemented"
            % file_version, "File.Version")

    decoded = codecs.decode(doc)
    payloads = decoded["payloads"]
    device = payloads["device"]
    refname = device.get("Mcu.Name")
    if not refname:
        raise IocUnknown("the file has no Mcu.Name; the device cannot be identified", "Mcu.Name")
    project_payload = payloads["project"]
    project_name = (project_payload["other"].get("ProjectName")
                    or doc.get("ProjectManager.ProjectFileName") or path.stem)
    if project_name.endswith(".ioc"):
        project_name = project_name[:-len(".ioc")]
    toolchain = project_payload["other"].get("TargetToolchain") or "CMake"
    # these keys are regenerated by the compiler from project fields; the rest is carried back as is (v2 project.extra).
    GENERATED_PROJECT_KEYS = {
        "ProjectName", "ProjectFileName", "DeviceId", "TargetToolchain",
        "HeapSize", "StackSize", "LibraryCopy", "MainLocation",
        "KeepUserCode", "UnderRoot", "ProjectBuild", "CoupleFile",
        "DeletePrevious", "ComputerToolchain", "CompilerLinker",
        "CompilerOptimize", "AskForMigrate", "BackupPrevious",
        "DefaultFWLocation", "CustomerFirmwarePackage", "FreePins",
        "FreePinsContext", "HalAssertFull", "NoMain", "PreviousToolchain",
        "ProjectStructure", "RegisterCallBack", "ToolChainLocation",
        "UAScriptAfterPath", "UAScriptBeforePath", "FirmwarePackage",
    }
    project_extra = {name: str(value)
                     for name, value in project_payload["other"].items()
                     if name not in GENERATED_PROJECT_KEYS}

    target = {
        "contract": "cubemx1.ioc/2",
        "device": {"refname": refname,
                   "cpn": device.get("Mcu.CPN") or ""},
        "project": {"name": project_name,
                    "output_dir": str(path.parent),
                    "toolchain": toolchain,
                    "extra": project_extra,
                    "options": dict(project_payload.get("options") or {})},
        "pins": _pins(payloads["pins"]),
        "ips": _ips(payloads["ips"], payloads["pins"]),
        "clock": {"params": dict(payloads["clock"]["params"]),
                  "targets": []},
        "nvic": {"priority_group": payloads["nvic"].get("priority_group"),
                 "vectors": _vectors(payloads["nvic"]),
                 "flags": dict(payloads["nvic"].get("flags") or {})},
        "dma": _dma(payloads["dma"]),
        "constants": dict(payloads["constants"].get("values") or {}),
        "extensions": {"ioc": dict(decoded["extensions"])},
    }
    # board identity: the .ioc only holds the board name (no revision/vendor). Keep the native board name,
    # and defer the directory binding until a seed/defaults is needed; what is imported is an already expanded project -> materialized.
    board_payload = payloads.get("board") or {}
    board_name = board_payload.get("bsp_ip") or board_payload.get("id")
    # `board=custom` marks 'no board selected' and is not a board identity; only a real board choice
    # (or a BSP_IP_NAME) fills the board section back in.
    if board_name and str(board_name) != "custom":
        target["board"] = {"id": str(board_name), "initialize": "materialized"}
    if target["nvic"]["priority_group"] is None:
        target["nvic"].pop("priority_group")
    rtos_objects = _rtos_objects(payloads["rtos"])
    if rtos_objects:
        target["rtos"] = dict(rtos_objects)
    # ordinary FREERTOS parameters (such as configTOTAL_HEAP_SIZE) are parameters of this instance itself;
    # object table rows are carried by the rtos section, and the parameter name list (IPParameters) is rebuilt from the actual parameters at build time, so it is not exported separately.
    rtos_params = {name: str(value)
                   for name, value in (payloads["rtos"].get("params") or {}).items()}
    if rtos_params:
        for entry in target["ips"]:
            if entry["instance"] == "FREERTOS":
                entry["params"].update(rtos_params)
                break
        else:
            raise IocUnknown(
                "FREERTOS has parameters (%s), but the Mcu.IP* list has no FREERTOS entry"
                % ", ".join(sorted(rtos_params)), "rtos")
    if not target["nvic"]["flags"]:
        target["nvic"].pop("flags")

    # a key that 'exists but has an empty value' (such as Mcu.UserConstants) must also be kept: it is a real fact in real files.
    if "Mcu.UserConstants" in doc and not target["constants"]:
        target["extensions"]["ioc"]["Mcu.UserConstants"] = doc["Mcu.UserConstants"]

    # self-check: normalization must pass (report a wrong shape right here instead of handing a half-built target to the caller)
    normalized = target_contract.parse(target)
    report = {
        "source": str(path),
        "file_version": file_version,
        "keys_total": len(doc),
        "keys_semantic": len(decoded["owned"]),
        "keys_extensions": len(decoded["extensions"]),
        "unsemanticized": sorted(decoded["extensions"]),
        "ip_list_gaps": list(payloads["ips"].get("gaps") or []),
        "counts": {"pins": len(normalized["pins"]),
                   "ips": len(normalized["ips"]),
                   "nvic": len(normalized["nvic"]["vectors"]),
                   "dma": len(normalized["dma"]),
                   "constants": len(normalized["constants"]),
                   "rtos_objects": sum(len(value)
                                       for value in (normalized["rtos"] or {}).get("objects", {}).values())},
        "notes": [],
    }
    gaps = report.get("ip_list_gaps") or []
    if gaps:
        report["notes"].append(
            "Mcu.IP list has gaps (no row written for index %s): this happens natively when a BSP component is expanded;"
            " such counts are skipped; the list is rewritten from the actual instances on rebuild"
            % ", ".join(str(index) for index in gaps))
    if report["unsemanticized"]:
        report["notes"].append(
            "%d keys are kept but not semantically decoded (see unsemanticized); they are written back as is"
            % len(report["unsemanticized"]))
    if toolchain not in target_contract.TOOLCHAINS:
        report["notes"].append(
            "toolchain %r is kept as is; this tool has only verified CMake generation" % toolchain)
    return {"target": target, "report": report}