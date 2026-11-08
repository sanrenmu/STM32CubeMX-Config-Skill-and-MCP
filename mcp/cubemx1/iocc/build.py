"""Target -> canonical .ioc. Pure data transformation, no session started.

Orchestration only: prepare the target, load the model, write keys section by section (pins/IP/DMA/NVIC/RTOS/clock/output). Clock solving details live in buildclock.py; the compile-time selection state lives in buildstate.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import CubemxError
from ..util import suggest, suggest_clause
from ..probe.catalog import load_model
from . import derive
from . import rtos as rtos_objects
from .buildclock import (clock_targets, complete_bus_dividers,
                         instance_clock_requirement)
from .buildstate import parameter_default, selection_state
from .emit import Ioc
from .errors import IocConflict, IocUnknown, IocUnsupported
from . import options as project_options
from .target import parse as parse_target


@dataclass
class BuildResult:
    """Build result: text + keys written + notes (for humans)."""

    text: str
    keys: dict
    notes: list = field(default_factory=list)

    def write(self, path):
        from pathlib import Path
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.text, encoding="ascii", newline="\n")
        return out


#: FREERTOS default task table. Code generation reads it (the rtos_default_thread template needs defaultTaskName);
#: without it generation fails inside FreeMarker with an empty defaultTaskName. The value comes from CubeMX's own saved
#: fixture (evidence/fixture_freertos.ioc).
FREERTOS_DEFAULT_TASKS = {"Tasks01":
                          "defaultTask,24,128,StartDefaultTask,Default,NULL,Dynamic,NULL,NULL"}

#: RTOS object table rows (tasks / queues / mutexes / semaphores / timers / event groups).
#: The rows themselves (column count, derived columns, list) are produced by iocc/rtos.py following CubeMX's write rules, but the entry point is the top-level
#: `rtos` section of the target: hand-written rows here would bypass that validation, so they are rejected with a pointer instead of accepting half a configuration.
RTOS_OBJECT_ROW = re.compile(
    r"^(Tasks|Queues|Mutexes|RecursiveMutexes|BinarySemaphores|"
    r"CountingSemaphores|Timers|Events)\d+$")
RTOS_SUPPORTED_ROWS = {"Tasks01"}


def build(machine, data) -> BuildResult:
    from .prepare import prepare  # deferred import: prepare depends on several submodules, avoiding a package initialization cycle

    target, prepare_notes = prepare(machine, data)
    refname = target["device"]["refname"]
    board = target.get("board") or {}
    model = load_model(machine, refname,
                       board=(board.get("id") if board else None))
    device = model.device
    notes: list = list(prepare_notes)
    ioc = Ioc()

    # ---- header: version taken live, not copied by hand (it must follow a version change) ----
    ioc.set("File.Version", "6")
    ioc.set("MxCube.Version", machine.cubemx_version)
    ioc.set("MxDb.Version", machine.db_version)
    ioc.set("KeepUserPlacement", "true")

    # ---- device ----
    uname = derive.user_name(device.refname)
    ioc.set("Mcu.Name", device.refname)
    ioc.set("Mcu.UserName", uname)
    if target["device"]["cpn"]:
        ioc.set("Mcu.CPN", target["device"]["cpn"])
    ioc.set("Mcu.Family", device.family or "")
    ioc.set("Mcu.Package", device.package or "")
    ioc.set("Mcu.ThirdPartyNb", "0")
    board = target.get("board")
    board_ctx = getattr(model, "board", None) or {}
    if board:
        ioc.set("board", str(board["id"]).split("@")[0])
        ioc.set("boardIOC", "true")
        # a BSP instance is only attached by the loader when board matches BSP_IP_NAME;
        # without that key the board instance in Mcu.IP* reports Unknown IP (measured).
        if board_ctx.get("has_bsp"):
            ioc.set("BSP_IP_NAME", str(board_ctx["name"]))
    else:
        ioc.set("board", "custom")
    # user constants: the format is 'name,value', several separated by ; (basis: saving/loading is the same code,
    # a single context has no suffix, only multiple contexts write 'name-context,value').
    constants = target["constants"]
    if constants:
        ioc.set("Mcu.UserConstants",
                ";".join("%s,%s" % (name, value)
                         for name, value in constants.items()))

    # ---- project settings (output dir/project type are decided here) ----
    project = target["project"]
    family_key = (device.family or "").replace("STM32", "", 1)
    firmware = machine.firmware_packages.get(family_key)
    if firmware is None:
        notes.append("no firmware package record for %s; add it before generating code" % device.family)
    ioc.set("ProjectManager.ProjectName", project["name"])
    ioc.set("ProjectManager.ProjectFileName", project["name"] + ".ioc")
    ioc.set("ProjectManager.DeviceId", uname)
    ioc.set("ProjectManager.TargetToolchain", project["toolchain"])
    ioc.set("ProjectManager.HeapSize", project["heap"])
    ioc.set("ProjectManager.StackSize", project["stack"])
    ioc.set("ProjectManager.LibraryCopy", project["library_copy"])
    ioc.set("ProjectManager.MainLocation", "Src")
    resolved_options = project_options.resolve(
        explicit=target["project"].get("options") or {})
    for option_name, (option_kind, _default) in project_options.OPTIONS.items():
        value = resolved_options[option_name]
        if option_kind == "bool":
            ioc.set("ProjectManager." + option_name,
                    "true" if value else "false")
    ioc.set("ProjectManager.UnderRoot", "false")
    ioc.set("ProjectManager.ProjectBuild", "false")
    ioc.set("ProjectManager.ComputerToolchain", "false")
    ioc.set("ProjectManager.CompilerLinker", "GCC")
    ioc.set("ProjectManager.CompilerOptimize", "6")
    ioc.set("ProjectManager.AskForMigrate", "true")

    ioc.set("ProjectManager.DefaultFWLocation", "true")
    ioc.set("ProjectManager.CustomerFirmwarePackage", "")
    ioc.set("ProjectManager.FreePins", "false")
    ioc.set("ProjectManager.FreePinsContext", "")

    ioc.set("ProjectManager.PreviousToolchain", "")
    ioc.set("ProjectManager.ProjectStructure", "")
    ioc.set("ProjectManager.RegisterCallBack", "")
    ioc.set("ProjectManager.ToolChainLocation", "")
    ioc.set("ProjectManager.UAScriptAfterPath", "")
    ioc.set("ProjectManager.UAScriptBeforePath", "")
    if firmware:
        ioc.set("ProjectManager.FirmwarePackage", firmware)
    # v2: other imported ProjectManager keys are written back as is (such as LastFirmware, functionlistsort).
    for key, value in (project.get("extra") or {}).items():
        full = "ProjectManager." + key
        if full in ioc.keys:
            raise IocConflict("%s in project.extra clashes with a key the compiler writes itself" % full,
                              "project.extra")
        ioc.set(full, value)


    # ---- peripherals: the three fixed system ones + those in the target ----
    ip_instances: list = []
    pin_entries: list = []
    params_by_ip: dict = {}
    modes_by_ip: dict = {}
    virtual_by_ip: dict = {}

    def want_ip(name: str):
        try:
            return derive.ip_by_instance(model.ips, name, model.device.refname)
        except IocUnknown:
            return None

    sys_ip = want_ip("SYS")
    if sys_ip is not None:
        ip_instances.append(sys_ip.instance)
        for item in derive.default_virtual_pins(sys_ip):
            pin_entries.append(dict(item))
    else:
        notes.append("this device has no SYS instance (uncommon; the system configuration may be incomplete)")

    for name in ("NVIC", "RCC"):
        ip = want_ip(name)
        if ip is not None:
            ip_instances.append(ip.instance)

    # the Cortex core's own configuration IP (CORTEX_M7 on H7, CORTEX_M0+ on C0, etc.):
    # the device XML has this entry and CubeMX always lists it in Mcu.IP* when it saves (measured on H7/C0).
    # without it a cross-check would be off by one on the IP list, so it is added from device data.
    for ip in model.ips:
        if ip.name.upper().startswith("CORTEX") and ip.instance not in ip_instances:
            ip_instances.append(ip.instance)

    for item in target["ips"]:
        try:
            ip = derive.ip_by_instance(model.ips, item["instance"], model.device.refname)
        except IocUnknown:
            board_ctx = getattr(model, "board", None) or {}
            if board_ctx.get("has_bsp"):
                raise IocUnknown(
                    "the BSP instance of board %s is %r; %r written in the target ips is not this board's BSP"
                    % (board_ctx.get("name"), board_ctx.get("name"),
                       item["instance"]), "ips[%s].instance" % item["instance"])
            raise
        if ip.instance not in ip_instances:
            ip_instances.append(ip.instance)
        if ip.instance.upper() == "RCC" and item["params"]:
            raise IocConflict(
                "RCC parameters must not be written in ips (got %s): clock settings go through the \"clock\" section"
                % ", ".join(sorted(item["params"])),
                "ips.%s.params" % ip.instance)
        mode = derive.find_mode(ip, item["mode"]) if item["mode"] else None
        modes_by_ip[ip.instance] = mode
        if mode is not None:
            vpins = derive.mode_virtual_pins(ip, mode)
            if vpins:
                virtual_by_ip[ip.instance] = vpins
        # v2: one IP may have several independent mode groups (recorded in ips[].modes on import),
        # and each group needs its own virtual pin, otherwise a rebuild loses the mode selection.
        for mode_ref in item.get("modes") or []:
            name = (mode_ref or {}).get("name")
            if not name:
                continue
            try:
                extra_mode = derive.find_mode(ip, name)
            except IocUnknown:
                continue
            bucket = virtual_by_ip.setdefault(ip.instance, [])
            known = {vpin["pin"] for vpin in bucket}
            for vpin in derive.mode_virtual_pins(ip, extra_mode):
                if vpin["pin"] not in known:
                    bucket.append(vpin)
                    known.add(vpin["pin"])
        allowed = derive.mode_parameter_names(ip, mode) if mode else set()
        params = {}
        is_rtos = ip.name.upper().startswith("FREERTOS") and mode is not None
        for name, value in item["params"].items():
            if (is_rtos and RTOS_OBJECT_ROW.match(name)
                    and name not in RTOS_SUPPORTED_ROWS):
                raise IocUnknown(
                    "RTOS object tables must not be written into peripheral parameters (got %r): put tasks/queues/"
                    "timers/semaphores/event groups in the top-level \"rtos\" section;"
                    "the tool assembles the rows by CubeMX's write rules and merges the parameter names into "
                    "FREERTOS.IPParameters." % name,
                    "ips.%s.params" % ip.instance)
            if derive.parameter(ip, name) is None:
                if target.get("contract_version", 1) >= 2:
                    # v2: an imported target may carry keys the model does not declare (real files do), carried as is.
                    notes.append("parameter %s.%s is not in the definition (v2 keeps it as is)"
                                 % (ip.instance, name))
                else:
                    candidates = sorted(allowed) if allowed else sorted(
                        parameter.name for parameter in ip.parameters)
                    near = suggest(name, candidates)
                    domain = ("parameters of %s in mode %r" % (ip.instance, item["mode"])
                              if item.get("mode") else "parameters of %s" % ip.instance)
                    raise IocUnknown(
                        "peripheral %s has no parameter %r%s"
                        % (ip.instance, name, suggest_clause(domain, near)))
            if allowed and name not in allowed:
                near = suggest(name, sorted(allowed))
                raise IocUnknown(
                    "parameter %r does not belong to the current mode of %s: %r%s"
                    % (name, ip.instance, item["mode"],
                       suggest_clause("parameters of %s in mode %r"
                                      % (ip.instance, item["mode"]), near)),
                    "ips.%s.params" % ip.instance)
            params[name] = value
        if is_rtos:
            if target["rtos"] is None and not any(name.startswith("Tasks")
                                                 for name in params):
                params.update(FREERTOS_DEFAULT_TASKS)
                notes.append(
                    "RTOS default task table: added CubeMX's default Tasks01"
                    " (without it, code generation fails inside the template)")
        params_by_ip[ip.instance] = params

    # ---- RTOS object tables (rtos section) ----
    if target["rtos"] is not None:
        instance = target["rtos"]["instance"]
        ip = derive.ip_by_instance(model.ips, instance, model.device.refname)
        if not ip.name.upper().startswith("FREERTOS"):
            raise IocUnknown("%s is not an RTOS middleware instance; the rtos section cannot be written on it" % instance,
                             "rtos.instance")
        mode = modes_by_ip.get(ip.instance)
        if mode is None:
            raise IocConflict(
                "the rtos section requires %s in ips with a mode (no mode means no object table)"
                % instance, "rtos.instance")
        extra, rtos_notes = rtos_objects.apply(model, machine, device, ip, mode,
                                               target["rtos"]["objects"])
        params_by_ip[ip.instance].update(extra)
        notes.extend(rtos_notes)

    # ---- pins ----
    occupied: dict = {}
    signal_only = 0
    for item in target["pins"]:
        pin = derive.pin_by_name(device, item["pin"])
        signal = item["signal"]
        if not signal:
            # reserved pin (locked, no signal): write .Locked only, not Signal/Mode.
            pin_entries.append({"pin": pin.name, "signal": "", "mode": "",
                                "locked": True, "gpio": item["gpio"]})
            continue
        shared_slots = item.get("shared") or []
        if shared_slots:
            # shared group imported in v2: the pin carries the 'group name', the real signal and mode are in alternatives.
            pin_entries.append({"pin": pin.name, "signal": signal, "mode": "",
                                "shared_entries": shared_slots,
                                "locked": item["locked"], "gpio": item["gpio"]})
            continue
        if signal not in pin.signals:
            raise IocUnknown("pin %s has no signal %r; available: %s"
                             % (pin.name, signal, ", ".join(pin.signals[:12])))
        is_gpio = signal.startswith("GPIO_")
        # GPIO_Input/GPIO_Output are generic signals and several pins may use them at once; other signals must be unique.
        if not is_gpio and signal in occupied:
            raise IocConflict("signal %s is written on both %s and %s"
                              % (signal, occupied[signal], pin.name))
        if not is_gpio:
            occupied[signal] = pin.name
        mode_name = "" if is_gpio else item["mode"]
        if not mode_name and not is_gpio:
            # the pin .Mode only accepts the mode the target selected for this instance: write it only when the mode's own signal table
            # really contains this signal. No more auto-filling from 'this signal has only one candidate mode' -
            # in real files CubeMX saves only Signal for such pins (the SYS/USB pins of board seeds
            # are all like that), and a filled-in .Mode is dropped on load.
            owner_ip = None
            for candidate in model.ips:
                if signal.startswith(candidate.instance + "_"):
                    owner_ip = candidate
                    break
            if owner_ip is not None:
                picked = modes_by_ip.get(owner_ip.instance)
                if picked is not None and signal in picked.signals:
                    mode_name = picked.internal_name
            if not mode_name:
                if target.get("contract_version", 1) >= 2:
                    signal_only = signal_only + 1
                else:
                    raise IocUnknown(
                        "signal %s of pin %s has no usable mode: the target gives no pin mode,"
                        " and the instance it belongs to has not selected a mode containing this signal"
                        % (pin.name, signal))
        owner_ip = None
        for candidate in model.ips:
            if signal.startswith(candidate.instance + "_"):
                owner_ip = candidate
                break
        shared = derive.shared_group(owner_ip, signal) if owner_ip else None
        if shared:
            # shared signals (remappable timer channels, shared ADC channels):
            #   <pin>.Signal=<group name>
            #   SH.<group name>.0=<real signal>,<mode internal name>
            #   SH.<group name>.ConfNb=1
            # this layout comes from a fixture CubeMX saved itself, not from guessing.
            entry = {"pin": pin.name, "signal": shared, "mode": "",
                     "shared": shared, "shared_signal": signal,
                     "shared_mode": mode_name,
                     "locked": item["locked"], "gpio": item["gpio"]}
        else:
            entry = {"pin": pin.name, "signal": signal, "mode": mode_name,
                     "locked": item["locked"], "gpio": item["gpio"]}
        pin_entries.append(entry)
    if signal_only:
        notes.append("%d pins write Signal only (the target gives no pin mode and the owning instance has not selected a mode containing it)"
                     % signal_only)

    # ---- NVIC / RCC ----
    nvic = target["nvic"]
    # with RTOS in use: the priority group is locked to 4, and a preemption priority below the RTOS minimum is raised.
    # measured basis: the RTOS minimum syscall priority comes from the FREERTOS parameter
    # configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY (writing 1 reads back 5).
    rtos_min = None
    for item in target["ips"]:
        ip = derive.ip_by_instance(model.ips, item["instance"])
        if not ip.name.upper().startswith("FREERTOS"):
            continue
        raw = item["params"].get(
            "configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY")
        if raw is None:
            raw = parameter_default(ip, "configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY",
                                 model, target, params_by_ip, notes)
        try:
            rtos_min = int(str(raw))
        except (TypeError, ValueError):
            rtos_min = 5
        break
    group = nvic["priority_group"]
    if rtos_min is not None and group != "NVIC_PRIORITYGROUP_4":
        notes.append("RTOS in use: priority group locked to 4 (the target had %s)" % group)
        group = "NVIC_PRIORITYGROUP_4"
    # Cortex-M0/M0+ has no priority grouping: written values are dropped (measured on C0), so skip it from device data.
    m0_core = any(ip.name.upper().startswith("CORTEX_M0")
                  for ip in model.ips)
    if not m0_core:
        ioc.set("NVIC.PriorityGroup", group)
    for name, value in (nvic.get("flags") or {}).items():
        ioc.set("NVIC." + name, value)
    for vector in nvic["vectors"]:
        priority = vector["priority"]
        raw = str(vector.get("raw") or "")
        # A vector that came from an imported file carries the value the real project
        # had (raw). CubeMX keeps that value on load (measured on H7: the Arm fault
        # exceptions stay at 0), so re-applying the RTOS minimum here would rewrite the
        # project instead of reproducing it. The clamp is only for vectors this build
        # writes from scratch.
        if rtos_min is not None and not raw and priority < rtos_min:
            notes.append("RTOS in use: %s has preemption priority %d below the RTOS minimum, clamped to %d"
                         % (vector["irq"], priority, rtos_min))
            priority = rtos_min
        segments = str(vector.get("raw") or "").split(":")
        if len(segments) >= 3:
            # v2: keep the extra fields CubeMX fills in itself, change only the first three semantic fields.
            segments[0] = str(vector["enabled"]).lower()
            segments[1] = str(priority)
            segments[2] = str(vector["subpriority"])
            value = ":".join(segments)
        else:
            value = "%s:%d:%d" % (str(vector["enabled"]).lower(),
                                   priority, vector["subpriority"])
        ioc.set("NVIC." + vector["irq"], value)
    clock_params = dict(target["clock"]["params"])
    board_defaults = set(target["clock"].get("board_defaults") or [])
    if target["clock"]["targets"] and board_defaults:
        # with target frequencies: derived readings brought by the board (the various *Freq_Value/dividers) are not reused,
        # only source values such as the crystal are kept as input; parameters written explicitly by the user stay.
        source_hints = ("HSE_VALUE", "LSE_VALUE", "HSI_VALUE", "LSI_VALUE")
        user_explicit = {name: value for name, value in clock_params.items()
                         if name not in board_defaults}
        board_inputs = {name: value for name, value in clock_params.items()
                        if name in board_defaults
                        and any(hint in name.upper() for hint in source_hints)}
        clock_params = dict(board_inputs)
        clock_params.update(user_explicit)
        if board_inputs:
            notes.append("board clock defaults: only source values are taken as input: %s"
                         % ", ".join(sorted(board_inputs)))
    if target["clock"]["targets"]:
        rcc = want_ip("RCC")
        if rcc is None:
            raise IocUnknown("the target gives clock target frequencies, but this device has no RCC instance")
        solved, clock_notes = clock_targets(machine, device, rcc,
                                             target["clock"]["targets"],
                                             explicit=clock_params)
        notes.extend(clock_notes)
        for name, value in solved.items():
            if name in clock_params and clock_params[name] != value:
                raise IocConflict(
                    "clock parameter %s is given explicitly as %r, but the target frequency solves to %r"
                    % (name, clock_params[name], value), "clock")
            clock_params[name] = value
    # ---- bus dividers: filled from the limits declared in the DB (only when HCLK is determined) ----
    hclk_hz = None
    for item in target["clock"]["targets"]:
        if (item["parameter"] or "").upper() == "HCLKFREQ_VALUE" and item["mhz"]:
            hclk_hz = float(item["mhz"]) * 1e6
            break
    if hclk_hz is not None:
        rcc = want_ip("RCC")
        if rcc is not None:
            clock_params.update(complete_bus_dividers(
                machine, device, clock_params, hclk_hz, rcc, notes))
    elif target["clock"]["targets"]:
        notes.append("clock targets have no HCLKFreq_Value; bus dividers are not filled automatically"
                     " (APB1/APB2 may exceed the device limit; give RCC parameters explicitly)")
    # ---- fixed-frequency clock needs of the instances themselves ----
    # instances are not picked by name: every instance is asked 'does a reading declare it as consumer and is the window narrow enough'
    # (data from RefParameter@IP + Min/Max in the RCC definition XML, see _instance_clock_requirement).
    for item in target["ips"]:
        instance = str(item["instance"])
        requirement, state = instance_clock_requirement(
            machine, model, device.refname, instance)
        if state == "none":
            continue
        if state == "unusable":
            notes.append(
                "instance %s has a consuming clock reading, but the db gives no fixed-frequency window (or the window is too wide),"
                " so it cannot be inverted automatically; give it in clock.targets or an explicit RCC parameter"
                % instance)
            continue
        param, target_hz = requirement
        covered = param in clock_params or any(
            (item["parameter"] or "").upper() == param.upper()
            for item in target["clock"]["targets"])
        if covered:
            continue
        rcc = want_ip("RCC")
        if rcc is None:
            raise IocUnknown("this device has no RCC instance")
        try:
            solved_usb, notes_usb = clock_targets(
                machine, device, rcc,
                [{"parameter": param, "peripheral": None,
                  "mhz": target_hz / 1e6}], explicit=clock_params,
                pinned=clock_params)
        except (IocUnknown, CubemxError):
            notes.append(
                "%s needs %.3g MHz (a window declared in the db), but the offline solver cannot invert it;"
                " give it in clock.targets or an explicit RCC parameter (verify before generation)"
                % (instance, target_hz / 1e6))
            continue
        notes.extend(notes_usb)
        for name, value in solved_usb.items():
            if name in clock_params and clock_params[name] != value:
                raise IocConflict(
                    "clock parameter %s is given explicitly as %r, but %s at %.3g MHz solves to %r"
                    % (name, clock_params[name], instance,
                       target_hz / 1e6, value), "clock")
            clock_params[name] = value

    if clock_params:
        rcc = want_ip("RCC")
        if rcc is None:
            raise IocUnknown("the target gives clock parameters, but this device has no RCC instance")
        for name in clock_params:
            if derive.parameter(rcc, name) is None and target.get("contract_version", 1) < 2:
                near = suggest(name, [parameter.name for parameter in rcc.parameters])
                raise IocUnknown("RCC has no parameter %r%s"
                                 % (name, suggest_clause("RCC parameters", near)))
        ioc.set("RCC.IPParameters", ",".join(sorted(clock_params)))
        for name, value in clock_params.items():
            ioc.set("RCC." + name, value)

    # ---- DMA ----
    # parameter names, order and visibility all come from the db (the RefMode chain of the DMA request + parameter definitions);
    # the write rules match CubeMX behaviour: write visible parameters only, skip empty values (evidence in the development-side docs).
    dma_entries = target["dma"]
    if dma_entries:
        from ..probe import resolve as probe_resolve
        from . import dmaauto
        from .designstate import DesignState

        dma_ips = dmaauto.controllers(model)
        if not dma_ips:
            raise IocUnknown("the target gives DMA configuration, but this device has no DMA request definitions")
        state = DesignState(model, target)
        # list only controllers that really carry a request in Mcu.IP*: H7 has DMA and BDMA at the same time,
        # listing all of them would put unused controllers into the list (measured: BDMA gets included and the IP list is off).
        ioc.set("Dma.RequestsNb", str(len(dma_entries)))
        for index, item in enumerate(dma_entries):
            request = item["request"]
            owner_ip = None
            for ip in dma_ips:
                try:
                    derive.dma_request(ip, request)
                except IocUnknown:
                    continue
                owner_ip = ip
                break
            if owner_ip is None:
                raise IocUnknown("this device has no DMA request %r" % request,
                                 "dma[%d].request" % index)
            if owner_ip.instance not in ip_instances:
                ip_instances.append(owner_ip.instance)
            flows = derive.dma_flows(owner_ip, request)
            candidates = []
            for flow in flows:
                for name in flow.get("instances") or [flow["instance"]]:
                    if name not in candidates:
                        candidates.append(name)
            instance = item["instance"] or ""
            if not instance:
                raise IocUnknown(
                    "DMA request %s has no instance: either give it explicitly (selectable streams in the definition: %s),"
                    " or write auto in allocation.dma to let the tool pick from the db stream table"
                    % (request, ", ".join(candidates) or "none"),
                    "dma[%d].instance" % index)
            if instance not in candidates:
                raise IocUnknown(
                    "DMA request %s cannot be routed to %r; selectable streams in the definition: %s"
                    % (request, instance, ", ".join(candidates) or "none"),
                    "dma[%d].instance" % index)
            names = derive.dma_request_parameters(owner_ip, request)
            written = []
            for param in names:
                picked = probe_resolve.select_variant(owner_ip, param, state)
                if picked.get("truth") is None:
                    raise IocUnknown(
                        "the applicable definition of DMA parameter %s depends on undecided state (%s)"
                        % (param, ", ".join(picked.get("unknown") or []) or "unknown"),
                        "dma[%d].params" % index)
                variant = picked.get("variant")
                visible = (True if variant is None or variant.visible is None
                           else bool(variant.visible))
                if not visible:
                    continue
                given = item["params"].get(param)
                if param == "Instance":
                    value = instance
                elif given is not None:
                    value = given
                else:
                    # look at the request's own declaration (override values) first, then fall back to the applicable definition of the root parameter.
                    values = derive.dma_request_parameter_values(
                        owner_ip, request, param)
                    if not values and variant is not None:
                        values = [entry.value for entry in (variant.values or [])
                                  if entry.value]
                    if len(values) == 1:
                        value = values[0]
                    elif variant is not None and variant.default not in (None, ""):
                        value = variant.default
                    else:
                        value = None
                if value is None or str(value) == "":
                    continue
                allowed = [entry.value for entry in
                           derive.value_labels(owner_ip, param)]
                if given is not None and allowed and given not in allowed:
                    raise IocUnknown(
                        "DMA parameter %s=%r is not among the candidates of request %s (%s)"
                        % (param, given, request, ", ".join(allowed)),
                        "dma[%d].params" % index)
                item["params"][param] = value
                written.append(param)
            extra = [key for key in item["params"] if key not in names]
            if extra:
                raise IocUnknown("DMA request parameters not supported: %s" % ", ".join(extra),
                                 "dma[%d].params" % index)
            ioc.set("Dma.Request%d" % index, request)
            ioc.set("Dma.%s.%d.RequestParameters" % (request, index),
                    ",".join(written))
            for param in written:
                ioc.set("Dma.%s.%d.%s" % (request, index, param),
                        str(item["params"][param]))

    # ---- write the list ----
    # board (BSP) instances follow the device IPs in native order: CubeMX attaches BSP last on load.
    bsp_names = [ip.instance for ip in model.ips if ip.ip_type == "bspip"]
    all_instances = set(ip_instances)
    ip_instances = sorted(name for name in all_instances
                          if name not in bsp_names)
    ip_instances += [name for name in bsp_names if name in all_instances]
    ioc.set("Mcu.IPNb", str(len(ip_instances)))
    for index, name in enumerate(ip_instances):
        ioc.set("Mcu.IP%d" % index, name)
    for name in ip_instances:
        params = params_by_ip.get(name)
        if params:
            ioc.set(name + ".IPParameters", ",".join(params))
            for key, value in params.items():
                ioc.set(name + "." + key, value)
    # peripherals the target gives no physical pins for (middleware/system): add the mode's own virtual pin.
    referenced = set()
    for item in pin_entries:
        if item["pin"].startswith("VP_"):
            continue
        for candidate in model.ips:
            if item["signal"].startswith(candidate.instance + "_"):
                referenced.add(candidate.instance)
                break
    known_pins = {item["pin"] for item in pin_entries}
    for instance, vpins in virtual_by_ip.items():
        if instance in referenced:
            continue
        for vpin in vpins:
            if vpin["pin"] in known_pins:
                continue
            pin_entries.append(dict(vpin))
            known_pins.add(vpin["pin"])
    # ---- mode signal completeness notice ----
    # CubeMX considers a mode enabled only when all its signals are mapped to it (otherwise the instance is dropped without an error,
    # measured: a USART in asynchronous mode with TX but no RX makes the whole USART1 disappear from the list). This only warns, never blocks:
    # whether to keep a disabled instance is the caller's choice.
    provided_signals = set()
    for item in pin_entries:
        # virtual pins count too: the signal of a mode is that virtual signal (SYS_VS_Systick,
        # <board>_VS_BSP_COMMON and the like); missing it would report an enabled mode as incomplete.
        provided_signals.add(str(item["signal"]))
        if item.get("shared_signal"):
            provided_signals.add(str(item["shared_signal"]))
    for instance, mode in modes_by_ip.items():
        if mode is None:
            continue
        missing = sorted(set(getattr(mode, "signals", None) or [])
                         - provided_signals)
        if missing:
            notes.append(
                "mode %s has incomplete signals (missing %s): CubeMX will not enable %s after loading"
                % (mode.internal_name, ", ".join(missing), instance))

    order = {pin.name: index for index, pin in enumerate(device.pins)}
    physical = [item for item in pin_entries if not item["pin"].startswith("VP_")]
    virtual = [item for item in pin_entries if item["pin"].startswith("VP_")]
    physical.sort(key=lambda item: order.get(item["pin"], 10 ** 6))
    virtual.sort(key=lambda item: item["pin"])
    pin_entries = physical + virtual
    ioc.set("Mcu.PinsNb", str(len(pin_entries)))
    for index, item in enumerate(pin_entries):
        ioc.set("Mcu.Pin%d" % index, item["pin"])
    for item in pin_entries:
        pin = item["pin"]
        # each pin decides for itself: generic GPIO_* signals have no pin-level .Mode in the file.
        # this used to reuse is_gpio from the previous loop, so when the last pins handled were GPIO pins
        # every .Mode was swallowed (SYS's VP_SYS_VS_Systick.Mode was lost this way and SYS dropped out of the IP list after loading).
        if not item["signal"]:
            # reserved pin: no Signal/Mode, only locking and (possibly) GPIO parameters.
            if item.get("locked"):
                ioc.set(pin + ".Locked", "true")
            gpio = item.get("gpio") or {}
            if gpio:
                ioc.set(pin + ".GPIOParameters", ",".join(sorted(gpio)))
                for key, value in gpio.items():
                    ioc.set(pin + "." + key, value)
            continue
        is_gpio = str(item["signal"]).startswith("GPIO_")
        ioc.set(pin + ".Signal", item["signal"])
        if item.get("shared_entries"):
            group = item["signal"]
            for index, slot in enumerate(item["shared_entries"]):
                value = slot["signal"]
                if slot.get("mode"):
                    value = "%s,%s" % (value, slot["mode"])
                ioc.set("SH." + group + ".%d" % index, value)
            ioc.set("SH." + group + ".ConfNb", str(len(item["shared_entries"])))
        elif item.get("shared"):
            ioc.set("SH." + item["shared"] + ".0",
                    "%s,%s" % (item["shared_signal"], item["shared_mode"]))
            ioc.set("SH." + item["shared"] + ".ConfNb", "1")
        elif not is_gpio and item["mode"]:
            ioc.set(pin + ".Mode", item["mode"])
        if item.get("locked"):
            ioc.set(pin + ".Locked", "true")
        gpio = item.get("gpio") or {}
        if gpio:
            ioc.set(pin + ".GPIOParameters", ",".join(sorted(gpio)))
            for key, value in gpio.items():
                ioc.set(pin + "." + key, value)

    # ---- v2: non-semantic IOC keys written back as is (nothing is lost on import + rebuild) ----
    extra = (target.get("extensions") or {}).get("ioc") or {}
    written = set(ioc.keys)
    conflicts = sorted(key for key in extra if key in written)
    if conflicts:
        raise IocConflict(
            "a key in extensions clashes with a key the compiler writes itself: %s" % ", ".join(conflicts[:6]),
            "extensions.ioc")
    for key, value in extra.items():
        ioc.set(key, value)
    if extra:
        notes.append("wrote back %d non-semantic IOC keys (from extensions.ioc)" % len(extra))

    keys = ioc.keys
    return BuildResult(text=ioc.render(), keys=keys, notes=notes)