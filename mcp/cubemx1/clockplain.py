"""Clock solving for paths without a PLL: plain multiply/divide chains.

Split out of `clocksolve` because that module only knows the PLL shape
(`target = VCO / output divider`, `VCO = source / M * N`). A device without a PLL
(STM32C0) or a target fed straight from a fixed source has no VCO at all, so its
path has to be solved with the same arithmetic without inventing a PLL segment.
The db reading helpers are shared, so this module borrows them from `clocksolve`
instead of duplicating them; `clocksolve` imports this module lazily, which keeps
the import acyclic.

How a path is classified: `pll_segment_index` (the db names the PLL input selector
`PLL*Source`; a prescaler chain has no such element). Nothing here branches on a
family name or on a hardcoded frequency.
"""

from __future__ import annotations

from fractions import Fraction

from . import clocksolve as _cs
from .ipxml import spec_for

#: Result statuses of one solved chain. They are the solver's own vocabulary and
#: must not be collapsed into "the hardware cannot do it": an incomplete search is
#: not a proof, and an unmodeled structure is not a hardware limit.
STATUS_SOLVED = "solved"
STATUS_EXHAUSTED = "exhausted"          # complete search in the supported model
STATUS_TRUNCATED = "truncated"          # node budget hit: nothing proven
STATUS_UNSUPPORTED = "unsupported"      # arithmetic structure not modeled
STATUS_MISSING_SOURCE = "missing_source"
STATUS_FIXED_CONFLICT = "fixed_conflict"

#: Node budget of the plain search. The candidate sets are small (the db gives a
#: handful of dividers per path), so this only stops a pathological tree.
MAX_PLAIN_NODES = 200000


def result(ok, status, reason=None, **extra):
    """A chain result with every key the caller reads, so both paths look alike."""
    out = {
        "ok": ok,
        "solutions": [],
        "status": status,
        "reason": reason,
        "pll_segment": [],
        "m_element": None,
        "m_element_note": "this path has no PLL input divider",
        "n_element": None,
        "post": [],
        "written_elements": [],
        "mux_steps": [],
        "source": None,
        "limits": [],
        "vco_bounds": {"min": None, "max": None, "parameter": None, "by_branch": True},
        "vco_input_bounds": {"min": None, "max": None, "parameter": None, "by_branch": True},
    }
    out.update(extra)
    return out


def is_pll_source_selector(x: dict) -> bool:
    """Is this multiplexor the PLL input selector?

    The criterion is the db naming only: the element's `refParameter` is the source
    selection of a PLL (`PLLSource`, `PLLSourceVirtual`, `PLL1Source`, ...). A bus
    prescaler chain selects a system clock, not a PLL input, so it does not match.
    """
    for name in str(x.get("ref_parameter") or "").split(","):
        low = name.strip().lower()
        if not low.startswith("pll"):
            continue
        rest = low[3:]
        while rest and rest[0].isdigit():
            rest = rest[1:]
        if rest in ("source", "sourcevirtual", "sourcevirtualstring"):
            return True
    return False


def pll_segment_index(forward: list):
    """Index of the PLL input selector on this path, or None when there is no PLL.

    Everything before the index is source side (crystal, source divider); the
    multiplier after it is the feedback multiplier N. `None` means the path is a
    plain divide/multiply chain and has to be solved by `solve_plain`.
    """
    for i, x in enumerate(forward):
        if x["type"] == "multiplexor" and is_pll_source_selector(x):
            return i
    return None


def element_value(x: dict, sp: dict, current: dict | None, params: dict | None,
                  fixed: dict | None = None):
    """The value an element has right now: explicit pin, current state, saved value, db default."""
    element = x["element"]
    if fixed and element in fixed:
        return fixed[element]
    got = (current or {}).get(element)
    if got is not None:
        return got
    for name in str(x.get("ref_parameter") or "").split(","):
        name = name.strip()
        if not name:
            continue
        got = (params or {}).get(name)
        if got is not None:
            return got
    return sp.get("default")


def _settings_for(specs, forward, chosen, current, params, fixed=None) -> dict:
    """Every value on this path that the answer depends on (element id -> value).

    The selectors it chose plus every divider/multiplier value (fixed, current or
    db default). The joint search pins this map, so a zero-write answer is
    reproduced by the next target instead of being silently moved.
    """
    out = dict(chosen or {})
    for x in forward:
        if x["type"] not in _cs._DIV_TYPES + _cs._MUL_TYPES + ("fractional",):
            continue
        sp = _cs._spec_of(specs, x)
        value = element_value(x, sp, current, params, fixed)
        if value is not None:
            out[x["element"]] = _cs._normalize_setting(x, sp, value)
    return out


def as_int(value):
    """A candidate/current value as an integer, or None when it is not one."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        pass
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return int(number) if abs(number - round(number)) < 1e-9 else None


def mux_steps(chosen: dict, current: dict | None, model: dict | None = None) -> list:
    """The selector writes this branch needs (only the ones that change the configuration).

    A selector with no current value is compared with its db default: choosing the
    default needs no write, which is what keeps the answer as small as possible.
    """
    out = []
    for element, value in (chosen or {}).items():
        now = (current or {}).get(element)
        if now is None and model:
            now = (model.get(element) or {}).get("default")
        if now is not None and str(now).lower() == str(value).lower():
            continue
        out.append((element, value))
    return out


def chain_frequency(tree, specs, forward, current, params, overrides=None):
    """Frequency at the end of the path, from `overrides` -> current -> db defaults.

    Returns `(hz, source_element, source_hz)`, or `(None, None, None)` when a value
    on the path cannot be read. `overrides` is ``{element: value}`` and is how a
    solution's assumed settings are checked against the arithmetic; a non-neutral
    fractional element returns None because its contribution is not modeled yet.
    """
    hz = None
    source, source_hz = None, None
    for x in forward:
        if x["type"] in _cs._SOURCE_TYPES:
            got = _cs._source_hz(tree, specs, x, current, params)
            if not got:
                return None, None, None
            hz = Fraction(int(round(got)))
            source, source_hz = x["element"], got
            continue
        if hz is None:
            return None, None, None
        if x["type"] == "fractional":
            sp = _cs._spec_of(specs, x)
            if as_int(element_value(x, sp, current, params, overrides)) != 0:
                return None, None, None
            continue
        if x["type"] not in _cs._DIV_TYPES + _cs._MUL_TYPES:
            continue
        sp = _cs._spec_of(specs, x)
        if x["type"] in _cs._MUL_TYPES:
            factor = _cs._mul_factor(tree, specs, x,
                                     element_value(x, sp, current, params, overrides),
                                     current, params, overrides)
        else:
            factor = _cs._factor_of(x, sp, element_value(x, sp, current, params,
                                                    overrides))
        if factor is None:
            return None, None, None
        hz = hz * _cs._frac(factor)
    return hz, source, source_hz


def already_satisfied(tree, specs, model, forward, chosen, target_hz, current,
                      params, fixed=None):
    """A solution that writes nothing, when the current settings already meet the target.

    A target that is already met is a success: the compiler must not re-solve it
    into a different but equivalent configuration, and a caller that asks for the
    same frequency again gets an empty step list instead of an error.

    Two conditions are required, and missing either one makes this answer wrong:
    the **fixed values are part of the state** (a target solved earlier pins
    dividers, so the current state must be evaluated with them), and the branch
    must need **no selector write** (a zero-step answer is loaded as-is, so it
    cannot rely on a selector the .ioc does not carry).
    """
    if mux_steps(chosen, current, model):
        return None                      # the branch itself needs a write
    for element, value in (chosen or {}).items():
        now = (current or {}).get(element)
        if now is not None and str(now).lower() != str(value).lower():
            return None
    hz, source, source_hz = chain_frequency(tree, specs, forward, current, params,
                                            overrides=fixed)
    if hz is None or hz != Fraction(int(round(target_hz))):
        return None
    settings = _settings_for(specs, forward, chosen, current, params, fixed)
    solution = _cs.Solution(
        steps=[], expected_hz=float(target_hz), exact=True, vco_hz=0.0,
        selections=dict(chosen or {}), is_current_branch=True, settings=settings,
        note="the current settings already give the target frequency; no parameter is written")
    return result(
        True, STATUS_SOLVED, None, solutions=[solution],
        mux_steps=mux_steps(chosen, current, model),
        source={"element": source, "hz": source_hz},
        forward=[x["element"] for x in forward], chosen=dict(chosen or {}))


def div_values(x: dict, sp: dict, model: dict) -> list:
    """The numeric values a divider can take, per its db definition."""
    cands = _cs._divisor_candidates(x, sp, model)
    if not cands:
        return []
    if cands[0] == "range":
        low, high = cands[1], cands[2]
        if high - low + 1 > 4096:
            return []
        return list(range(low, high + 1))
    return sorted(value for value in cands[1] if value and value > 0)


def _fits(cons, hz) -> bool:
    return all(low <= hz <= high for _name, low, high in cons)


def stages_fit(specs, forward, start, source_hz, values, stage_cons, bands,
               current, params) -> bool:
    """Replay the path, checking every reading at the stage that produces it.

    `values` are the chosen values of the free elements; every other element keeps
    its current (or db default) value, so the arithmetic is the one the device
    performs.
    """
    hz = float(source_hz)
    for i, x in enumerate(forward):
        if x["type"] in _cs._SOURCE_TYPES:
            continue
        if x["type"] in _cs._DIV_TYPES + _cs._MUL_TYPES:
            sp = _cs._spec_of(specs, x)
            value = values.get(x["element"])
            if value is None:
                value = element_value(x, sp, current, params)
            factor = _cs._factor_of(x, sp, value)
            if factor is None:
                return False
            hz *= float(factor)
        if i < start:
            continue
        if not _fits(stage_cons.get(x["element"], ()), hz):
            return False
        if not _fits(bands.get(x["element"], ()), hz):
            return False
    return True


def _build_solution(model, specs, forward, items, picks, chosen, current, params,
                   target_hz):
    """Turn picked values into a solution plus the element ids it writes."""
    steps = mux_steps(chosen, current, model)
    written = [element for element, _value in steps]
    for (x, sp, order, pinned), pick in zip(items, picks):
        if pinned or len(order) == 1:
            # an explicit constraint stays as it is; a single-valued element has no setter
            continue
        if as_int(element_value(x, sp, current, params)) == pick:
            continue                          # already correct: no write
        write = _cs._write_value(x, sp, pick)
        if write is None:
            return None, None                 # cannot express the value: no guess
        steps.append((_cs._param_name(x), write))
        written.append(x["element"])
    chain = ", ".join("%s=%s" % (x["element"], pick)
                      for (x, _sp, _order, _pinned), pick in zip(items, picks)) or "no divider"
    settings = _settings_for(specs, forward, chosen, current, params)
    for (x, _sp, _order, _pinned), pick in zip(items, picks):
        settings[x["element"]] = pick
    solution = _cs.Solution(
        steps=steps, expected_hz=float(target_hz), exact=True, vco_hz=0.0,
        selections=dict(chosen or {}),
        is_current_branch=not mux_steps(chosen, current, model), settings=settings,
        note="exact solution on a path without a PLL: %s (expected %.0f Hz)"
             % (chain, target_hz))
    return solution, written


def solve_plain(tree, specs, model, forward, chosen, target_hz, limit, current,
                params, fixed, budget=None):
    """Solve `target = source * product(multipliers) / product(dividers)`.

    Every divider and multiplier on the path is a variable whose candidate values
    come from its db definition; a single-valued element stays in the arithmetic but
    is never written. Values already set are tried first, so the cheapest answer
    wins. The path is enumerated once with interval pruning, and the result says
    whether the budget cut the search short (`truncated`) instead of claiming the
    target is impossible.
    """
    budget = MAX_PLAIN_NODES if budget is None else budget
    fixed = fixed or {}
    trace = {"forward": [x["element"] for x in forward], "chosen": dict(chosen or {})}
    source, source_hz = None, None
    for x in forward:
        if x["type"] in _cs._SOURCE_TYPES:
            source_hz = _cs._source_hz(tree, specs, x, current, params)
            source = x["element"]
            break
    if not source_hz:
        return result(False, STATUS_MISSING_SOURCE,
                      "the path has no source whose frequency can be read", **trace)

    first = len(forward)
    items, limits = [], []
    for i, x in enumerate(forward):
        if x["type"] == "fractional":
            sp = _cs._spec_of(specs, x)
            value = element_value(x, sp, current, params)
            if as_int(value) != 0:
                return result(
                    False, STATUS_UNSUPPORTED,
                    "the path contains the fractional element %s = %r; a fractional "
                    "PLL contribution is not modeled, so no exact answer is given"
                    % (x["element"], value), **trace)
            continue
        if x["type"] not in _cs._DIV_TYPES + _cs._MUL_TYPES:
            continue
        sp = _cs._spec_of(specs, x)
        if x["type"] in _cs._DIV_TYPES:
            values = div_values(x, sp, model)
        else:
            values = _cs._numeric_candidates(sp) or []
        if not values:
            now = as_int(element_value(x, sp, current, params))
            if now is None:
                return result(
                    False, STATUS_UNSUPPORTED,
                    "%s (%s) has no candidate values in the db and no value to keep"
                    % (x["element"], x["ref_parameter"]), **trace)
            values = [now]
        pinned = False
        if x["element"] in fixed:
            pin = as_int(fixed[x["element"]])
            if pin is None or pin not in values:
                return result(
                    False, STATUS_FIXED_CONFLICT,
                    "the fixed value %r of %s is not one of its values %r"
                    % (fixed[x["element"]], x["element"], values), **trace)
            values = [pin]
            pinned = True
        now = as_int(element_value(x, sp, current, params))
        order = []
        for value in (now, as_int(sp.get("default"))):
            if value in values and value not in order:
                order.append(value)
        order += [value for value in sorted(values) if value not in order]
        if not items:
            first = i
        items.append((x, sp, order, pinned))
        low, high = sp.get("min"), sp.get("max")
        if low is not None or high is not None:
            limits.append({"element": x["element"], "parameter": x["ref_parameter"],
                           "min": low, "max": high})

    stage_cons = _cs._stage_readout_constraints(tree, specs, forward, first, params)
    bands = _cs._element_bands(specs, forward, model)
    ratio = Fraction(int(round(target_hz))) / Fraction(int(round(source_hz)))

    factors = []
    for x, _sp, order, _pinned in items:
        if x["type"] in _cs._DIV_TYPES:
            factors.append([Fraction(1, value) for value in order])
        else:
            factors.append([Fraction(value) for value in order])
    count = len(items)
    low_rest = [Fraction(1)] * (count + 1)
    high_rest = [Fraction(1)] * (count + 1)
    for i in range(count - 1, -1, -1):
        low_rest[i] = low_rest[i + 1] * min(factors[i])
        high_rest[i] = high_rest[i + 1] * max(factors[i])

    found, nodes, truncated = [], [0], [False]

    def walk(index, partial, picks):
        if truncated[0] or len(found) >= limit:
            return
        nodes[0] += 1
        if nodes[0] > budget:
            truncated[0] = True
            return
        if index >= count:
            if partial != ratio:
                return
            values = {items[i][0]["element"]: picks[i] for i in range(count)}
            if not stages_fit(specs, forward, first, source_hz, values, stage_cons,
                              bands, current, params):
                return
            solution, _written = _build_solution(model, specs, forward, items, picks,
                                                 chosen, current, params, target_hz)
            if solution is not None:
                found.append(solution)
            return
        need = ratio / partial
        if not (low_rest[index] <= need <= high_rest[index]):
            return
        for factor, value in zip(factors[index], items[index][2]):
            walk(index + 1, partial * factor, picks + [value])

    walk(0, Fraction(1), [])
    # no change first, then the fewest writes: the cheapest answer wins
    found.sort(key=lambda sol: (len(sol.steps), str(sol.note)))
    if found:
        # the impact report needs element ids, while a step carries the command name
        by_name = {_cs._param_name(item[0]): item[0]["element"] for item in items}
        written = [by_name.get(str(name), str(name))
                   for name, _value in found[0].steps]
        return result(
            True, STATUS_SOLVED, None, solutions=found[:limit],
            mux_steps=mux_steps(chosen, current, model),
            written_elements=written,
            source={"element": source, "hz": source_hz}, limits=limits, **trace)
    if truncated[0]:
        return result(False, STATUS_TRUNCATED,
                      "the search budget (%d nodes) was reached before the whole path "
                      "was enumerated, so nothing is proven either way" % budget,
                      limits=limits, **trace)
    return result(False, STATUS_EXHAUSTED,
                  "the whole supported model was searched: no combination of the db "
                  "divider values gives this frequency exactly", limits=limits, **trace)
