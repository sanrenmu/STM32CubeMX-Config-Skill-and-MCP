"""Clock solving, write side: target frequencies -> RCC parameters.

Split out of iocc/build.py: this module only turns targets into RCC parameters, fills in bus dividers, and looks up fixed peripheral frequencies; it does not touch pins, lists or file output.
"""

from __future__ import annotations

import re

from ..errors import CubemxError
from . import derive
from .errors import IocConflict, IocUnknown


def rcc_param_name(rcc, solver_name: str) -> str:
    """Map a component name from the solver to the official RCC parameter name.

    The solver uses component names from the clock tree (CLI names), while .ioc files carry the official RCC parameter names. Most of them are identical; virtual parameters (such as PLLSource) have a Virtual suffix in the db and must be mapped there.
    """
    names = [param.name for param in rcc.parameters]
    low = solver_name.lower()
    # look for the Virtual variant first: CubeMX writes the virtual parameter name into .ioc (measured: PLLSource is
    # saved as PLLSourceVirtual), and writing the non-Virtual name makes the key disappear on re-save.
    for suffix in ("Virtual", "VirtualString"):
        for name in names:
            if name.lower() == (solver_name + suffix).lower():
                return name
    if solver_name in names:
        return solver_name
    for name in names:
        if name.lower() == low:
            return name
    raise IocUnknown(
        "clock solving produced parameter %r, but the RCC definition has no such parameter; examples of available ones: %s"
        % (solver_name, ", ".join(names[:16])))


def explicit_clock_inputs(machine, refname: str, explicit: dict) -> tuple:
    """Turn the RCC parameters written explicitly in the target into the current state the solver can consume.

    The crystal/dividers from a board seed come in this way: explicit values win over db defaults, the solver works from them, and it will not overwrite the board configuration with the default crystal.
    """
    from .. import clocksolve

    if not explicit:
        return {}, {}
    try:
        elements = [item["element"]
                    for item in clocksolve.value_model(machine, refname)["elements"]]
    except Exception:  # noqa: BLE001 - when the model is unavailable, pass params only
        return {}, dict(explicit)
    by_low = {name.lower(): name for name in elements}
    current = {}
    for name, value in explicit.items():
        element = by_low.get(name.lower())
        if element is None and name.lower().endswith("virtual"):
            element = by_low.get(name.lower()[: -len("virtual")])
        if element is not None:
            try:
                current[element] = float(value) if "." in str(value) else int(value)
            except (TypeError, ValueError):
                current[element] = value
    return current, dict(explicit)


#: How many candidate solutions per target the joint search keeps. Every target
#: can have several exact answers (a different M/N split, a different divider
#: split); keeping a few lets a later target backtrack instead of failing, while a
#: huge list would only cost time.
MAX_OPTIONS = 8

#: Upper bound of the one-shot escalation above. A cut search is retried with
#: more candidates before the failure is reported as incomplete; even this cap is
#: a budget, not a completeness proof.
MAX_OPTIONS_HARD = 32


def clock_element_index(machine, refname: str) -> tuple:
    """Index clock elements by element id and by the parameter names they carry.

    The solver talks in element ids for selectors and in `refParameter` names for
    arithmetic elements; .ioc files use the parameter names. This index is how a
    solution step is turned back into "which element is now fixed".
    """
    from ..clockvalues import value_model

    try:
        elements = value_model(machine, refname)["elements"]
    except CubemxError:
        return {}, {}
    names, refs = {}, {}
    for item in elements:
        element = item["element"]
        names[element] = element
        for name in str(item.get("ref_parameter") or "").split(","):
            name = name.strip()
            if name:
                refs.setdefault(name.lower(), element)
    return names, refs


def steps_fixed(index: tuple, steps) -> dict:
    """The element values a solution pins down (element id -> value)."""
    names, refs = index
    fixed = {}
    for name, value in steps or []:
        element = names.get(str(name)) or refs.get(str(name).lower())
        if element is not None:
            fixed[element] = value
    return fixed


def settings_fixed(index: tuple, solution: dict) -> dict:
    """Every element value a solution relies on (its full `settings` map).

    Pinning only the written steps is not enough: a target that is already met writes
    nothing, yet the later targets have to keep the dividers and the source selection
    it depends on. Falls back to the steps when an older result carries no settings.
    """
    fixed = dict(solution.get("settings") or {})
    fixed.update(steps_fixed(index, solution.get("steps") or []))
    return fixed


def verify_targets(machine, refname: str, checks: list, params: dict) -> list:
    """Independent check: do the final RCC values reproduce every target?

    `checks` is ``[{"label":..., "parameter":..., "mhz":...}]``. Frequencies are
    recomputed with `clocksolve.forward_from_rcc` — clock-tree arithmetic over the
    final parameter values — never from the solver's own `expected_hz`. Returns one
    text per target that does not hold.
    """
    from .. import clocksolve

    problems = []
    for check in checks:
        got = clocksolve.forward_from_rcc(machine, refname, check["parameter"], params)
        want = float(check["mhz"]) * 1e6
        if got.get("hz") is None:
            problems.append("%s: cannot be verified from the final RCC values (%s)"
                            % (check["label"], got.get("reason") or "no reason"))
            continue
        if abs(float(got["hz"]) - want) > max(1.0, want * 1e-9):
            problems.append("%s: the final RCC values give %.6g Hz, the target is %.6g Hz"
                            % (check["label"], float(got["hz"]), want))
    return problems


def merge_steps(rcc, explicit: dict, params: dict, steps) -> str | None:
    """Fold one solution into the accumulated parameters; return a conflict text.

    An explicit value is a constraint, not a preference: if a solved step wants
    another value, this candidate is rejected here (the caller tries the next
    one), so a conflict is never discovered only after every target was solved.
    """
    for name, value in steps or []:
        param = rcc_param_name(rcc, str(name))
        if param in explicit and str(explicit[param]) != str(value):
            return "explicit %s=%r but the solution needs %r" % (
                param, explicit[param], value)
        if param in params and str(params[param]) != str(value):
            return "%s was already solved as %r, the solution needs %r" % (
                param, params[param], value)
        params[param] = str(value)
    return None


def solved_note(label: str, mhz, solution: dict) -> str:
    """One human-readable line per solved target (a target that changes nothing says so)."""
    steps = solution.get("steps") or []
    if not steps:
        return ("clock %s = %s MHz: already satisfied by the current settings,"
                " no parameter written" % (label, mhz))
    return ("clock %s = %s MHz: solved %d parameters (expected %.0f Hz)"
            % (label, mhz, len(steps), solution.get("expected_hz") or 0.0))


def clock_failure(label: str, mhz, failures: list, flow_truncated: bool = False) -> str:
    """Explain why a target could not be solved, keeping the solver statuses apart.

    "The search was cut short" and "the structure is not modeled" are not the same
    claim as "the whole model was searched and nothing fits", and none of them is
    "the hardware cannot do it".
    """
    statuses = {str(result.get("status") or "") for _item, result in failures}
    first = failures[0][1]
    detail = str(first.get("reason") or "")
    rows = [str(row.get("reason")) for row in (first.get("branches_tried") or [])
            if row.get("reason")]
    if rows:
        detail = "; ".join(rows[:3])
    conflict = first.get("fixed_conflict") or []
    if "fixed_conflict" in statuses:
        head = ("the fixed parameter(s) %s conflict with this target"
                % ", ".join(conflict) if conflict
                else "a fixed parameter conflicts with this target")
    elif "unsupported" in statuses:
        head = "the clock path uses a structure the offline solver does not model"
    elif "truncated" in statuses:
        head = "the search was cut short by its budget, so this target is not proven impossible"
    elif "missing_source" in statuses:
        head = ("the path has no readable source frequency"
                " (give HSE_VALUE/LSE_VALUE or an explicit RCC value)")
    elif "exhausted" in statuses:
        head = ("the joint search was cut short by its candidate limit, so this target "
                "set is not proven impossible" if flow_truncated else
                "the whole supported model was searched and nothing satisfies it exactly")
    else:
        head = "no usable branch"
    return ("clock target %s = %s MHz cannot be solved (offline solver): %s. Detail: %s"
            % (label, mhz, head, detail or "no reason reported"))


def clock_targets(machine, device, rcc, wanted: list, explicit: dict | None = None,
                   pinned: dict | None = None, solver=None) -> tuple:
    """Hand the target frequencies to the offline solver; return (RCC parameters, notes).

    The solving logic itself stays in `clocksolve`; this function only composes
    several targets and the caller's constraints:

    - explicit RCC parameters and `pinned` values are constraints from the first
      target on, not preferences applied at the end;
    - the targets are searched jointly: each target contributes a few exact
      answers and a later target may fail on one of them, so the search backtracks
      instead of locking the first answer of the first target;
    - a target that changes nothing is a success with an empty step list;
    - a failure carries the solver status (exhausted / truncated / unsupported /
      missing source / conflicting fixed value) instead of one flat "cannot solve".

    `solver`: test seam. When given, it replaces `clocksolve.solve` so the joint
    search can be exercised with a tiny graph (the real one needs a device).
    """
    from .. import clocksolve

    current, current_params = explicit_clock_inputs(
        machine, device.refname, explicit or {})
    index = clock_element_index(machine, device.refname)
    base = fixed_elements(machine, device.refname, pinned or {})
    base.update(fixed_elements(machine, device.refname, explicit or {}))
    def run(cap: int):
        """One joint pass keeping at most `cap` candidates per target."""
        states = [{"params": {}, "fixed": dict(base), "notes": [], "checks": []}]
        # a cut candidate list must be reported, not hidden behind "searched out"
        cut = False
        for item in wanted:
            label = item["parameter"] or item["peripheral"]
            nxt, failures = [], []
            for state in states:
                fixed = state["fixed"] or None
                if solver is not None:
                    got = solver(label, item["mhz"] * 1e6, fixed)
                elif item["parameter"]:
                    got = clocksolve.solve(machine, device.refname,
                                           item["parameter"], item["mhz"] * 1e6,
                                           limit=cap,
                                           current=current or None,
                                           params=current_params or None,
                                           fixed=fixed)
                else:
                    got = clocksolve.solve_peripheral(
                        machine, device.refname, item["peripheral"],
                        item["mhz"] * 1e6, limit=cap, current=current or None,
                        params=current_params or None, fixed=fixed)
                if got.get("truncated"):
                    cut = True
                if not got.get("ok") or got.get("status") == "fixed_conflict":
                    failures.append((item, got))
                    continue
                taken = 0
                for solution in got.get("solutions") or []:
                    params = dict(state["params"])
                    clash = merge_steps(rcc, explicit or {}, params,
                                        solution.get("steps") or [])
                    if clash is not None:
                        failures.append((item, {
                            "ok": False, "status": "fixed_conflict",
                            "reason": clash, "fixed_conflict": [clash]}))
                        continue
                    # pin the whole settings map: what the target relies on, not
                    # only what it happened to write
                    fixed_next = dict(state["fixed"])
                    fixed_next.update(settings_fixed(index, solution))
                    notes = list(state["notes"])
                    notes.append(solved_note(label, item["mhz"], solution))
                    checks = list(state["checks"])
                    parameter = item["parameter"] or got.get("parameter")
                    if parameter:
                        checks.append({"label": label, "parameter": parameter,
                                       "mhz": item["mhz"]})
                    nxt.append({"params": params, "fixed": fixed_next,
                                "notes": notes, "checks": checks})
                    taken += 1
                    if taken >= cap:
                        cut = True
                        break
            if not nxt:
                return None, cut, (label, item, failures)
            if len(nxt) > cap:
                cut = True
            states = nxt[:cap]
        return states, cut, None

    # The candidate cap is a budget, not a proof: when a pass is cut and fails,
    # retry with a larger cap before saying anything about the model.
    states, cut, failure = run(MAX_OPTIONS)
    while failure is not None and cut and MAX_OPTIONS < MAX_OPTIONS_HARD:
        bigger = min(MAX_OPTIONS_HARD, max(MAX_OPTIONS * 4, MAX_OPTIONS + 1))
        states, cut, failure = run(bigger)
        if bigger >= MAX_OPTIONS_HARD:
            break
    if failure is not None:
        label, item, failures = failure
        raise IocUnknown(clock_failure(label, item["mhz"], failures, cut),
                         "clock.targets")
    best = states[0]
    if solver is None:
        # Independent acceptance: the values that will be written have to reproduce
        # every target by clock-tree arithmetic. The solver's own expected_hz is not
        # evidence for that (a zero-write answer can claim a frequency it does not
        # produce once a later target moves a shared divider).
        final = dict(explicit or {})
        final.update(best["params"])
        problems = verify_targets(machine, device.refname, best["checks"], final)
        if problems:
            raise IocConflict(
                "clock targets are not all satisfied by the final RCC values: %s"
                % "; ".join(problems), "clock.targets")
    return best["params"], best["notes"]

def fixed_elements(machine, refname: str, pinned: dict) -> dict:
    """Fold 'already solved RCC parameters' into 'fixed component values' so the solver does not re-enumerate them.

    Parameter names and clock tree components are matched through ``refParameter`` (one component may carry several names, comma separated). Without a clock tree, return empty - the caller falls back to 'solve everything freely'.
    """
    if not pinned:
        return {}
    from ..clockvalues import value_model

    try:
        elements = value_model(machine, refname)["elements"]
    except CubemxError:
        return {}
    fixed = {}
    for item in elements:
        for name in str(item.get("ref_parameter") or "").split(","):
            name = name.strip()
            if not name or name not in pinned:
                continue
            try:
                fixed[item["element"]] = int(str(pinned[name]))
            except (TypeError, ValueError):
                fixed[item["element"]] = pinned[name]
            break
    return fixed


def divider_factor(value: str):
    """Take the divider number from an enumeration name: RCC_HCLK_DIV4 -> 4; None when there is no digit."""
    match = re.search(r"DIV(\d+)$", str(value).upper())
    if not match:
        return None
    try:
        factor = int(match.group(1))
    except ValueError:
        return None
    return factor if factor > 0 else None


def complete_bus_dividers(machine, device, clock_params: dict, hclk_hz: float,
                           rcc, notes: list) -> dict:
    """Fill in bus dividers up to the limits declared in the DB (APB1/APB2...).

    Basis (all in the XML of the install dir, not hardcoded here):
    - the RCC definition XML gives bus readings a Max, for example F4's
      "APB1Freq_Value Max=45000000" and "APB2Freq_Value Max=90000000";
    - the clock tree XML links a divider to its downstream reading: the devisor's refParameter is "APB1CLKDivider" and its output goes to the activeOutput "APB1Freq_Value".

    So among the PossibleValues given by XML, take the smallest divider whose result does not exceed Max; the bus limit needs no guessing. If the clock tree is unavailable, record one note instead of filling in something.
    """
    from ..clockxml import ClockTree, find_clock_xml
    from ..clockvalues import value_model

    try:
        tree = ClockTree(find_clock_xml(machine, device.refname))
        elements = {item["element"]: item
                    for item in value_model(machine, device.refname)["elements"]}
    except CubemxError:
        notes.append("clock tree definition unavailable; bus dividers are not filled in automatically (give RCC parameters explicitly)")
        return {}

    known = {"HCLKFreq_Value": float(hclk_hz)}
    completed = {}
    for element_id, element in tree.elements.items():
        item = elements.get(element_id) or {}
        if item.get("type") != "devisor" or not item.get("values"):
            continue
        # input frequency of this stage: whether the input component is a reading we know (currently only HCLK).
        base = None
        for link in element.inputs:
            source = tree.get(link.get("from") or "")
            if source is not None and source.ref_parameter in known:
                base = known[source.ref_parameter]
                break
        if base is None:
            continue
        # the limit declared by the downstream reading. Note that outputs are only signalIds; a downstream component must be looked up through Input@from,
        # because the link itself may also be a reading (<Signal id refParameter>).
        cap = None
        readout = ""
        candidates = []
        for signal_id in element.outputs:
            if signal_id in tree.signals:
                candidates.append(tree.signals[signal_id])
        for other in tree.elements.values():
            if any(link.get("from") == element_id for link in other.inputs):
                candidates.append(other.ref_parameter)
        for ref in candidates:
            for downstream in elements.values():
                if downstream.get("ref_parameter") != ref:
                    continue
                if (downstream.get("kind") == "derived"
                        and isinstance(downstream.get("max"), (int, float))):
                    cap = float(downstream["max"])
                    readout = ref or ""
                    break
            if cap is not None:
                break
        if cap is None:
            continue
        choices = sorted((factor, value) for factor, value in
                         ((divider_factor(value), value)
                          for value in item["values"]) if factor)
        if not choices:
            continue
        param = rcc_param_name(rcc, element.ref_parameter or "")
        pick = None
        for factor, value in choices:
            if base / factor <= cap:
                pick = (factor, value)
                break
        if pick is None:
            notes.append("%s: no candidate divider gets under the %s limit %.0f Hz (input %.0f Hz)"
                         % (param, readout or "downstream reading", cap, base))
            continue
        given = clock_params.get(param)
        if given is not None:
            factor = divider_factor(given)
            if factor and base / factor > cap:
                notes.append("%s explicitly gives %s, but %s would reach %.0f Hz (limit %.0f Hz);"
                             "written as given, verify before generation"
                             % (param, given, readout or "downstream reading",
                                base / factor, cap))
            continue
        completed[param] = pick[1]
        notes.append("%s=%s: derived from the XML limit %.0f Hz (input %.0f Hz)"
                     % (param, pick[1], cap, base))
    return completed

def instance_clock_requirement(machine, model, refname: str, instance: str):
    """Look up the fixed-frequency clock reading this instance needs.

    Return ``(parameter name or None, status)``: status ``"none"`` = no reading declares that it is consumed; ``"unusable"`` = declared but the window is too wide to be a fixed-frequency requirement; ``"ok"`` = requirement found.

    Basis: RefParameter entries in the RCC definition XML list who consumes the reading through the `IP=` attribute (F4 has `48MHZClocksFreq_Value IP="USB_OTG_HS,USB_OTG_FS"`, F1 uses another name), and declare the allowed window with Min/Max. So:
    - a very narrow window (10 percent of the middle or less) means a fixed-frequency requirement; take the integer MHz inside the window;
    - a window that contains 48 MHz means USB full speed needs 48 MHz (hardware fact), so the target is 48 MHz;
    - a wide window means it is just an ordinary reading, not a requirement; return None.
    """
    from ..ipxml import find_ip, modes_xml_path, ref_parameters

    target_ip = None
    try:
        target_ip = derive.ip_by_instance(model.ips, instance, model.device.refname)
    except IocUnknown:
        target_ip = None
    names = {instance.upper()}
    if target_ip is not None:
        names.add(target_ip.name.upper())
    try:
        entry = find_ip(machine, refname, "RCC")
    except Exception:  # noqa: BLE001 - without a definition, treat it as no requirement
        return None, "none"
    if entry is None:
        return None, "none"
    try:
        specs = ref_parameters(modes_xml_path(machine, entry))
    except CubemxError:
        return None, "none"
    best = None
    saw_consumer = False
    for name, spec in specs.items():
        consumers = {str(x).upper() for x in (spec.get("consumers") or [])}
        if not consumers & names:
            continue
        try:
            low = float(str(spec.get("min")))
            high = float(str(spec.get("max")))
        except (TypeError, ValueError):
            continue
        if high <= low:
            continue
        # only readings that really declare a frequency window count as requirements; those without Min/Max are ordinary readings.
        saw_consumer = True
        mid = (low + high) / 2.0
        if low <= 48e6 <= high:
            hz = 48e6
        elif high - low <= 0.1 * mid:
            hz = round(mid / 1e6) * 1e6
        else:
            continue
        if best is None or hz > best[1]:
            best = (name, hz)
    if best is not None:
        return best, "ok"
    return None, ("unusable" if saw_consumer else "none")
