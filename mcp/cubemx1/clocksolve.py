"""Target-frequency back-solver: given a peripheral target in MHz, compute the
upstream parameter values.

## What problem it solves

The user says "**make SPI1 run at 10 MHz**". `SPI1Freq_Value` is a **derived
readout** (a computed result) and cannot be written. Delivering it means
**back-solving the upstream**: set the PLL's M/N/Q so the output lands exactly on
10 MHz.

## Arithmetic model (data-driven, no family names)

`<Element>` entries in the clock-tree XML have a type, and the type says how it
transforms the frequency:

==============  ==========================================
type            transform
==============  ==========================================
`fixedSource`   the source itself (HSI/HSE), a fixed frequency
`variedSource`  variable source (external clock input); value is a parameter
`devisor`       ``out = in / value``
`multiplicator` / `multiplicatorFrac`   ``out = in * value``
`multiplexor`   ``out = in`` (it only picks one of several inputs)
`output` / `activeOutput`  pass-through (the readout itself)
==============  ==========================================

So a chain is "a run of multiply/divide from source to target":

    target = source * ∏(multipliers) / ∏(dividers)

## Key point: **the chain must agree with the mux selection** (an earlier version got this wrong; do not repeat it)

Every `<Input>` of a mux in the clock tree (e.g. H750's `SPI123Mult`) carries a
`from` — **which element feeds that input**. Measured on H750:

    index  input refValue                  from
    0      RCC_SPI123CLKSOURCE_PLL         DIVQ1     ← PLL1
    1      RCC_SPI123CLKSOURCE_PLL2        DIVP2     ← PLL2
    2      RCC_SPI123CLKSOURCE_PLL3        DIVP3     ← PLL3
    3      RCC_SPI123CLKSOURCE_PIN         I2S_CKIN
    4      RCC_SPI123CLKSOURCE_CLKP        CKPERSource

**An earlier solver decoupled the two**: the chain followed the current selection
into PLL2's `DIVP2/DIVN2/DIVM2`, but the output item then enumerated "every
candidate whose name contains PLL", producing an **inconsistent** solution such as
`SPI123Mult=PLL1Q + DIVP2/DIVN2/DIVM2` — after writing it the mux switched to
PLL1Q, the PLL2 dividers changed nothing, and reading back gave 50 MHz ≠ the
expected 10 MHz.

So the structure is: **fix the branch first (which mux input), then solve along
that branch**; a branch's "mux selection + multiply/divide values" are produced
**together**.

## Branch search (covers "switch to another PLL")

`<Input>` is the branch table itself, so "let SPI1 use PLL1Q instead of PLL2P" is
searchable: enumerate every input of the muxes **after** the PLL, rebuild the
chain per branch, solve each, and merge. That is the counterpart of MX2's
`--flexible-path` here (this version stops at mux level, with a depth limit).

**Source-side muxes are not enumerated** (e.g. H750's `PLLSource`): measured, it is
**shared by three PLLs**, so changing it changes the PLL2/PLL3 inputs too. The user
only wants SPI1 at 10 MHz and should not silently move someone else's PLL source.
So the source side always keeps the current selection.

## Enumeration (not brute force)

Taking H750's SPI1 on PLL2, the chain (source to target) is:

    HSE → PLLSource → DIVM2 → DIVN2 → DIVP2 → SPI123Mult → target

The unknowns are `DIVM2`(M) / `DIVN2`(N) / `DIVP2`(P); constraints come from the db:

    target = src / M * N / P
    VCO = src / M * N   must be inside the VCO band (measured H750: 150M..960M)
    VCO input = src / M must be inside the VCO input band (measured 1M..16M)

**Enumeration**: the outer loop enumerates only M (the smallest range); for each M
the VCO band gives the feasible interval for N, and `P = VCO / target` must be an
integer inside P's range. That is O(63 × a few).

**Discrete dividers** on the chain (enum type, e.g. `RCC_APB2_DIV4`) are not
unknowns, but they have several candidates, so their combinations are enumerated
(usually 0..2). The numeric meaning lives in the db's `PossibleValue Comment`
(measured `<PossibleValue Comment="4" Value="RCC_APB2_DIV4"/>`), see
`ipxml.ref_parameters`'s `value_numbers`.

## Preference (same as the earlier version)

**Current branch first** (change as little as possible), then lowest VCO (VCO
dominates power), then smaller M and smaller N for the same VCO. The earlier
`clock_solver.solve_pll` chose the same way.

## How it writes: everything through `clock set`, no two-step

The earlier version used two steps: write parameters with `set ip parameters RCC`,
**then replay `clock set`** to refresh derived frequencies (because the former was
said not to notify the clock tree).

This tool measured that **two steps are unnecessary**: `clock set <element>
<value>` enters the clock-tree model itself, and derived frequencies are recomputed
after save and reload (measured: `clock set DIVM1 8` moves `Tim1OutputFreq_Value`
from 50 MHz to 12.5 MHz). So **everything goes through `clock set`** — it validates
(an out-of-range value is a KO with a message), far safer than the generic
parameter path (which accepts silently).

## Closed loop

After writing, save and reload, then:

1. `clockvalues.check_tree`: check with the same constraints CubeMX uses whether
   the tree was broken (report the **delta**);
2. **read back the target element's `*Freq_Value`** and compare with the
   expectation (integer solutions may be inexact; allow a tolerance).
"""

from __future__ import annotations

from .util import truthy

import re

from dataclasses import dataclass, field
from fractions import Fraction

from .clockvalues import check_tree, value_model
from .clocktargets import targets
from .errors import CubemxError
from .ipxml import find_ip, modes_xml_path, ref_parameters, spec_for

# Numeric elements (usable in multiply/divide)
_DIV_TYPES = ("devisor",)
_MUL_TYPES = ("multiplicator", "multiplicatorFrac")

# **Element kinds that want a "number".** Measured: their value must be numeric;
# a non-numeric value is a KO, and **stdout says nothing**, so on the command line
# it looks like "KO with no explanation".
# [measured] On F429 `clock set PLLP RCC_PLLP_DIV6` fails exactly that way; write
# `clock set PLLP 6` instead. (In the db the number behind `RCC_PLLP_DIV6` is in
# `PossibleValue Comment="6"`, see `ipxml.ref_parameters`'s `value_numbers`.)
_ARITH_TYPES = _DIV_TYPES + _MUL_TYPES + ("fractional",)

# Branch-search limits: mux level only, at most two hops, and a cap on branches.
# Why the cap: every branch runs a full M×N enumeration, and one `clock set` takes
# about a second.
_MAX_BRANCHES = 12
_MAX_BRANCH_DEPTH = 2

# How many "splits" of one total ratio D are offered (see `_decompose_all`). More
# splits mean more candidates, but **only CubeMX knows which one keeps the clock tree
# intact**, so several groups are offered for the layer above to try.
_MAX_SPLITS = 4

#: Candidates per M layer before the search counts as cut. The fractional mode
#: enumerates D directly (D is a product of integer dividers), so it needs its own
#: bound; hitting it is reported as "truncated", never as a complete search.
_MAX_PAIRS = 4096


@dataclass
class Solution:
    """One solution: the value for each element to set, plus the expected frequency."""

    steps: list = field(default_factory=list)
    """The `clock set` commands to send (order: mux source first, then output
    dividers, then N, then M)."""
    expected_hz: float = 0.0
    exact: bool = False
    vco_hz: float = 0.0
    note: str = ""
    selections: dict = field(default_factory=dict)
    """Which source each mux selects (element name → candidate value name)."""
    is_current_branch: bool = True
    """Whether this solution touched a mux. Ones that did are sorted last (change as
    little as possible)."""
    settings: dict = field(default_factory=dict)
    """Every clock element value this solution relies on (element id -> value).

    A solution is only reproducible when **all** of these hold, not just the ones it
    writes: a target that is already met writes nothing, yet a later target must not
    move the divider or the source selection it depends on. The joint search pins
    this map, and the acceptance check recomputes every target from the final RCC
    values instead of trusting `expected_hz`.
    """

    def as_dict(self):
        return {"steps": self.steps, "expected_hz": self.expected_hz,
                "exact": self.exact, "vco_hz": self.vco_hz,
                "selections": self.selections, "note": self.note,
                "is_current_branch": self.is_current_branch,
                "settings": self.settings}


def _specs(machine, refname: str) -> dict:
    e = find_ip(machine, refname, "RCC")
    if e is None:
        raise CubemxError("the device XML has no RCC entry; value domains are unavailable")
    return ref_parameters(modes_xml_path(machine, e))


def _spec_name(ref) -> str:
    """The db parameter name of a clock-tree element reference.

    The clock tree may declare a composite reference: `A,B` (equivalent names) or
    `PARAM/DIV` (a parameter plus the divisor of its fractional part, or the list of
    N values allowed to carry a fraction). The RCC definition is keyed by the first
    name only.
    """
    text = str(ref or "")
    for sep in (",", "/"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip()


def _spec_of(specs, x: dict) -> dict:
    """The RCC definition of an element's parameter (see `_spec_name`)."""
    return spec_for(specs, _spec_name(x.get("ref_parameter")))[0]


def _hz(v):
    """Normalize db frequency values to Hz (the db stores Hz; `Unit="MHz"` is only a
    display conversion)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_range(spec: dict) -> tuple:
    """Integer range of one db parameter; ``(None, None)`` when unavailable."""
    lo, hi = spec.get("min"), spec.get("max")
    try:
        return (int(float(lo)) if lo is not None else None,
                int(float(hi)) if hi is not None else None)
    except (TypeError, ValueError):
        return None, None


def _numeric(value, spec: dict):
    """Fold one element value into a number.

    It may be a plain number (`DIVM2=4`) or an **enum name** (`RCC_APB2_DIV4`);
    the latter's number is in the db's `PossibleValue Comment` (`value_numbers`).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return (spec.get("value_numbers") or {}).get(str(value))


def _normalize_setting(x: dict, spec: dict, value):
    """A setting in the form the joint search pins it.

    Arithmetic elements are pinned as **numbers** (a db enum name such as
    `RCC_HSI_DIV4` would not compare with a numeric candidate and would look like
    a conflict), selectors keep their candidate name.
    """
    if x.get("type") in _ARITH_TYPES + ("fractional",):
        num = _numeric(value, spec)
        if num is None:
            return value
        return int(num) if abs(num - round(num)) < 1e-9 else num
    return value


def _factor_of(x: dict, spec: dict, value):
    """The factor one element contributes (forward: ``out = in * factor``).

    `devisor` is ``1/value``; a multiplier is ``value``; a mux does not change the
    frequency (``1.0``).
    """
    if x["type"] == "multiplexor":
        return 1.0
    num = _numeric(value, spec)
    if num is None or num == 0:
        return None
    return (1.0 / num) if x["type"] in _DIV_TYPES else num


def _param_name(x: dict) -> str:
    """The RCC parameter name a solution step writes (refParameter name, else element id).

    The compiler writes RCC parameters, so the name is the element's
    `refParameter` — for a selector as well. A selector's element id is only the
    internal clock-tree name and is not a parameter at all (measured: C0's
    `MCOMult` element carries `RCC_MCO1Source`; writing `MCOMult` as an RCC
    parameter is rejected). When a parameter carries several names the first one is
    used; `rcc_param_name` still resolves the Virtual variants.
    """
    for name in str(x.get("ref_parameter") or "").split(","):
        name = name.strip()
        if name:
            return name
    return x["element"]


def _mux_param_name(tree, element_id: str) -> str:
    """RCC parameter name of a selector element (selector steps write a parameter)."""
    element = tree.get(element_id)
    return _param_name({"element": element_id, "type": "multiplexor",
                        "ref_parameter": getattr(element, "ref_parameter", None)})


def _write_value(x: dict, spec: dict, value):
    """Fold one db value into the form `clock set` **actually writes**.

    Arithmetic elements (div/mul/fractional) want a **number**: the db may hold an
    enum name (`RCC_PLLP_DIV6`) whose number is in `PossibleValue Comment`. When it
    cannot be folded this returns None (**no guessing**; the caller reports an error —
    measured: non-numeric values always fail with no message). Muxes want an
    **index** (converted in `apply_solution`), so the value is returned unchanged.
    """
    if x["type"] not in _ARITH_TYPES:
        return value
    num = _numeric(value, spec)
    if num is None:
        return None
    return str(int(num)) if abs(num - round(num)) < 1e-9 else repr(num)


# --------------------------------------------------------------------------
# VCO band parameter names (W4: taken per branch, not "the first one that exists")
# --------------------------------------------------------------------------
#
# Band parameter names **differ per family**, so neither a hard-coded name nor a
# per-family branch is acceptable. Forms seen in the 67 local RCC definitions
# (`db/mcu/IP/RCC-*_Modes.xml`):
#
#   output: `VCOOutputFreq_Value` (generic), `VCO1..VCO8OutputFreq_Value`,
#           `VCOOutput2Freq_Value`, `VCOPLL2OutputFreq_Value` /
#           `VCOPLL3OutputFreq_Value`, `VCOI2SOutputFreq_Value`,
#           `VCOSAIOutputFreq_Value` / `VCOSAI1OutputFreq_Value` /
#           `VCOSAI2OutputFreq_Value`
#   input:  `VCOInputFreq_Value` (generic), `VCOInput1..VCOInput8Freq_Value`,
#           `VCOInputMFreq_Value`, `VCOI2SInputFreq_Value`,
#           `VCOSAIInputFreq_Value`
#
# Branch element names (`<Element>` in `db/plugins/clock/*.xml`): `DIVM1/DIVM2/DIVM3`
# (H7 style, numbered), `PLLM` (main PLL, unnumbered), `PLLI2SM`,
# `PLLSAIM` / `PLLSAI1M` / `PLLSAI2M`, `PLL2M` / `PLL3M`.
#
# So the approach is: **derive the "which PLL" token from the branch element names →
# build candidate names from the token → try them in order**; only when none exists
# fall back to "the first one that exists" (the pre-W4 behaviour) and set
# `by_branch=False` so the caller reports it. Every criterion comes from the data
# above.
#
# Why per branch is required: **three families locally have different VCO bands** —
# `RCC-STM32MP13_rcc_v1_0_Modes.xml` (VCO1 992M..2000M, VCO2 800M..1600M,
# VCO3/VCO4 400M..800M) and `RCC-STM32MP_rcc_v1_0_Modes.xml` (VCO1/VCO2
# 800M..1600M, VCO3/VCO4 400M..800M). Taking the wrong one judges feasibility
# against another PLL's band. (The third difference, in `RCC-STM32F410`, is
# `VCOROutputFreq_Value`, which constrains the R output rather than the VCO band,
# so it is harmless in practice.)


def _branch_token(names) -> list:
    """Derive "which PLL" from this branch's element names.

    `DIVM2` → `["2"]`; `PLLM` → `[]` (main PLL, use the generic names);
    `PLLI2SM` → `["I2S"]`; `PLLSAI1M` → `["SAI1", "SAI"]`; `PLL2M` → `["2"]`.
    """
    toks = []
    for n in names or []:
        u = str(n).upper()
        if "I2S" in u:
            toks.append("I2S")
            continue
        if "SAI" in u:
            d = re.search(r"SAI(\d)", u)
            if d:
                toks.append("SAI" + d.group(1))
            toks.append("SAI")
            continue
        d = re.search(r"(?:DIVM|PLL)(\d+)", u)
        if d:
            toks.append(d.group(1))
    out, seen = [], set()
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _band_names(tokens, kind: str) -> list:
    """Build band parameter names from the tokens (`kind` is ``"output"`` /
    ``"input"``)."""
    out = []
    for t in tokens:
        if kind == "output":
            out += ["VCO%sOutputFreq_Value" % t,
                    "VCOOutput%sFreq_Value" % t,
                    "VCOPLL%sOutputFreq_Value" % t]
        else:
            out += ["VCOInput%sFreq_Value" % t,
                    "VCO%sInputFreq_Value" % t]
    out.append("VCOOutputFreq_Value" if kind == "output" else "VCOInputFreq_Value")
    return out


# Fallback order: **exactly as before W4**, so "nothing broke" stays verifiable
_FALLBACK_BANDS = {
    "output": ("VCO1OutputFreq_Value", "VCOOutputFreq_Value",
               "VCO2OutputFreq_Value", "VCO3OutputFreq_Value"),
    "input": ("VCOInput1Freq_Value", "VCOInputFreq_Value",
              "VCOInput2Freq_Value", "VCOInput3Freq_Value"),
}


def _band(specs, kind: str, names=None) -> tuple:
    """Get a VCO band, returning ``(min, max, parameter name, by_branch)``.

    `names` are this branch's element names. When given, the band is taken per
    branch; only when no token can be derived, or none of the built names exists in
    the db, does it fall back to the :data:`_FALLBACK_BANDS` order with
    `by_branch=False` — the caller must report that the old method was used.
    """
    for n in _band_names(_branch_token(names), kind):
        s = specs.get(n)
        if s and s.get("min") and s.get("max"):
            return float(s["min"]), float(s["max"]), n, True
    for n in _FALLBACK_BANDS[kind]:
        s = specs.get(n)
        if s and s.get("min") and s.get("max"):
            return float(s["min"]), float(s["max"]), n, False
    return None, None, None, False


def _vco_bounds(specs, names=None) -> tuple:
    """The legal **output** band of the VCO.

    [measured] H750 uses `VCO1OutputFreq_Value` (150M..960M), F4 uses
    `VCOOutputFreq_Value` (100M..432M) — both read from the db, no per-family branch.
    """
    return _band(specs, "output", names)


def _vco_input_bounds(specs, names=None) -> tuple:
    """The legal **input** band of the VCO (i.e. the PLL reference input), read from
    the db.

    [measured] H750 uses `VCOInput1Freq_Value` (1M..16M).
    **This constraint must be used too** — the closed loop caught it: with the output
    band alone, `DIVM1=2` with HSI=64MHz pushes the VCO input to 32MHz, outside
    1..16M, and after writing it `clock check` reports `VCOInput1Freq_Value` out of
    range.
    """
    return _band(specs, "input", names)


def _downstream_readouts(tree, start: str, cap: int = 64) -> list:
    """Names of the **readouts** (`output` / `activeOutput`) reachable downstream from
    `start`.

    Why it is needed: some elements are **shared**, so changing one affects more than
    the target clock domain. [measured] On F429, SPI1 has no dedicated kernel clock
    and must use the bus — the solver's solution then changes `SysClkSource` /
    `AHBPrescaler` / `APB2Prescaler`, i.e. **the chip's main frequency and bus
    dividers**, and every peripheral on those buses follows.
    Such a solution is not wrong (F4's SPI1 really has only that path), but **the
    user must see it**; the main frequency must not change silently.

    The criterion is "downstream readout count > 1", all derived from the clock-tree
    XML `from` attributes, with no hard-coding.
    """
    kids = {}
    for el in tree.elements.values():
        for inp in el.inputs:
            f = inp.get("from")
            if f:
                kids.setdefault(f, []).append(el.id)
    seen, stack, hits = set(), [start], []
    while stack and len(hits) < cap:
        cur = stack.pop()
        for k in kids.get(cur, []):
            if k in seen:
                continue
            seen.add(k)
            kel = tree.get(k)
            if kel is not None and kel.type in ("output", "activeOutput"):
                hits.append(k)
            stack.append(k)
    return hits


def _walk(tree, start_id: str, picks: dict, current: dict | None):
    """Walk upstream from the target element to the source, returning
    ``(forward, chosen)``.

    :param picks: forced mux selections ``{element name: refValue}``. When given but
        that input does not exist → ``(None, None)`` (this branch does not exist).
    :param current: ``{element name: current value}``. **A mux not named in picks
        follows its current value**, falling back to the first input only when that
        cannot be read. That way the "leave the muxes alone" branch is the project's
        present state.

    ``forward`` is the **source → target** element list; ``chosen`` is the mux
    selection actually walked. The two come out **together** — that is exactly where
    the "mux decoupled from dividers" bug was fixed.
    """
    rev, chosen, seen = [], {}, set()
    eid = start_id
    for _ in range(24):
        el = tree.get(eid)
        if el is None or eid in seen:
            break
        seen.add(eid)
        ins = [x for x in el.inputs if x.get("from")]
        if not ins:
            break

        def _match(want):
            if want is None:
                return None
            w = str(want).lower()
            return next((x for x in ins
                         if (x.get("ref_value") or "").lower() == w), None)

        pick = _match(picks.get(eid))
        if picks.get(eid) and pick is None:
            return None, None            # this branch does not exist
        if pick is None:
            pick = _match((current or {}).get(eid))
        if pick is None:
            # A fractional element is a **side input** of the multiplier (the
            # fractional part of N), not the signal path: walking through it would
            # leave the chain without a source. Prefer the signal input; the
            # fractional edge is resolved separately when the factor is computed.
            pick = next((x for x in ins
                         if (getattr(tree.get(x.get("from") or ""), "type", None)
                             != "fractional")), ins[0])
        if el.type == "multiplexor" and pick.get("ref_value"):
            chosen[eid] = pick["ref_value"]

        nxt = pick.get("from")
        nel = tree.get(nxt)
        if nel is None:
            break
        rev.append({"element": nxt, "type": nel.type,
                    "ref_parameter": nel.ref_parameter})
        eid = nxt
    return list(reversed(rev)), chosen


def _numeric_candidates(sp: dict, cap: int = 4096):
    """The **numbers this parameter can take** (sorted, de-duplicated); None when
    undecidable.

    - `range` type (db has `Min`/`Max`) → every integer in the interval;
    - **enum type** (db `Type="list"`) → the numbers in `PossibleValue Comment`
      (`ipxml.ref_parameters`'s `value_numbers`).

    Why the enum type must be handled (2026-09-27, found while widening W6):
    measured, **the PLL input divider (`PLLM`) of G0 / L4 / U0 is an enum in the
    db** (`RCC_PLLM_DIV1..8`), while `_unknowns` used to treat only
    `kind="range"` as an unknown → those families' PLL segments had `divs=[]` →
    "the PLL segment has no divider or multiplier" → **no PLL target could be solved
    at all**. It is not that those families lack clock support; the solver only
    understood one data shape.
    """
    lo, hi = _int_range(sp)
    if lo is not None and hi is not None:
        if hi - lo + 1 > cap:
            return None                     # interval too large: do not materialize (M/N
                                            # never get that big)
        return list(range(lo, hi + 1))
    nums = set()
    for v in (sp.get("value_numbers") or {}).values():
        try:
            nums.add(int(float(v)))
        except (TypeError, ValueError):
            pass
    return sorted(nums) if nums else None


def _unknowns(forward: list, model: dict) -> list:
    """The **solvable** multiply/divide elements on the chain: type is divider or
    multiplier, and the db says `range` **or enum**.

    `range` / `enum` both mean "the value comes from an enumerable set of numbers";
    `free` / `derived` are not "just fill in a number" unknowns, and neither is a
    fixed source.
    (The enum case was added 2026-09-27; see `_numeric_candidates` for the reason.)
    """
    return [x for x in forward
            if x["type"] in _DIV_TYPES + _MUL_TYPES
            and (model.get(x["element"]) or {}).get("kind") in ("range", "enum")]


def _frac(value) -> Fraction:
    """Fold one db factor into a reduced fraction.

    Why fractions instead of floats: an upstream divider may be ``1/3``, and floats
    distort the "N must be an integer" criterion (measured: at VCO-input resolution
    like 1..16 MHz, float noise invents or loses solutions).
    """
    try:
        return Fraction(value).limit_denominator(10 ** 6)
    except (TypeError, ValueError):
        return Fraction(int(round(float(value))))


def _divisors(n: int) -> list:
    """All positive divisors of n, ascending."""
    out, i = [], 1
    while i * i <= n:
        if n % i == 0:
            out.append(i)
            if i != n // i:
                out.append(n // i)
        i += 1
    return sorted(out)


def _in_cands(v: int, cands) -> bool:
    """Whether v is a legal value of this divider element."""
    if cands is None:
        return False
    if cands[0] == "range":
        return cands[1] <= v <= cands[2]
    return v in cands[1]


def _divisor_candidates(x: dict, sp: dict, model: dict):
    """The **candidate numbers** of one divider element.

    Returns ``("range", lo, hi)`` / ``("set", {values...})`` / ``None`` (no guessing
    when unavailable).

    The numeric meaning of an enum element is in the db's `PossibleValue Comment`
    (`value_numbers`); some elements' `values` are already numbers (measured: H750's
    `DIVP1` is `['2','4','6','8']`, while F429's `PLLP` holds names like
    `['RCC_PLLP_DIV2',...]`). Both are accepted.
    """
    kind = (model.get(x["element"]) or {}).get("kind")
    if kind == "range":
        lo, hi = _int_range(sp)
        if lo is None or hi is None:
            return None
        return ("range", lo, hi)
    vals = set()
    for v in (sp.get("value_numbers") or {}).values():
        try:
            vals.add(int(float(v)))
        except (TypeError, ValueError):
            pass
    for v in (sp.get("values") or []):
        try:
            vals.add(int(float(v)))
        except (TypeError, ValueError):
            pass
    if not vals and sp.get("default") is not None:
        try:
            vals.add(int(float(sp["default"])))
        except (TypeError, ValueError):
            pass
    vals = {v for v in vals if v > 0}
    return ("set", vals) if vals else None


def _forced_value(cands):
    """Return the value when the candidate set holds **exactly one**, else None.

    Such an element has **no degree of freedom** — `clock set` may not even have a
    command for it (measured: a command exists only when "there are several
    candidates, or a single value that was not optimized").
    Measured: F1's `HSIDivPLL` is `min=2 max=2` in the db and
    `clock set HSIDivPLL 2` prints the usage table and fails. So the solver **does
    not write it**, but its value still takes part in the arithmetic.
    """
    if not cands:
        return None
    if cands[0] == "range":
        return cands[1] if cands[1] == cands[2] else None
    vals = cands[1] or set()
    return next(iter(vals)) if len(vals) == 1 else None


def _decompose(D: int, elems: list, i: int = 0):
    """Split D into the product of the values of ``elems[i:]``; None when impossible.

    **Only the divisors of D are enumerated**, not each element's whole candidate
    range — so a range divider (e.g. `DIVP2` with 1..128) does not slow it down.
    Pruned by the product interval of the remaining elements.
    """
    if i >= len(elems):
        return [] if D == 1 else None
    x, cands = elems[i]
    rest_lo = rest_hi = 1
    for _, c in elems[i + 1:]:
        if c[0] == "range":
            rest_lo *= c[1]
            rest_hi *= c[2]
        else:
            vs = [v for v in c[1] if v > 0] or [1]
            rest_lo *= min(vs)
            rest_hi *= max(vs)
    for d in _divisors(D):
        if not _in_cands(d, cands):
            continue
        r = D // d
        if not (rest_lo <= r <= rest_hi):
            continue
        sub = _decompose(r, elems, i + 1)
        if sub is not None:
            return [(x, d)] + sub
    return None


def _decompose_all(D: int, elems: list, limit: int = 4) -> list:
    """Split D into the product of the element values, **returning at most `limit`
    splits** (each element's candidates tried from small to large).

    Why several: one D may have more than one split, and **only CubeMX knows which
    one keeps the clock tree intact**. Measured with F429 targeting SPI1=10 MHz:
    VCO=160 MHz, D=16 splits into `PLLP=2 AHB=1 APB2=8` (HCLK=80 MHz →
    `APB1Freq_Value` out of range, and APB1 hangs off HCLK and is **not on the chain
    we walk**, so it cannot be computed) or `PLLP=4 AHB=1 APB2=4` (HCLK=40 MHz →
    legal). Returning only the first would never find the second.
    """
    out = []
    _decompose_walk(D, elems, 0, [], out, limit)
    return out


def _decompose_walk(D, elems, i, acc, out, limit) -> None:
    if len(out) >= limit:
        return
    if i >= len(elems):
        if D == 1:
            out.append(list(acc))
        return
    x, cands = elems[i]
    rest_lo = rest_hi = 1
    for _, c in elems[i + 1:]:
        if c[0] == "range":
            rest_lo *= c[1]
            rest_hi *= c[2]
        else:
            vs = [v for v in c[1] if v > 0] or [1]
            rest_lo *= min(vs)
            rest_hi *= max(vs)
    for d in _divisors(D):
        if len(out) >= limit:
            return
        if not _in_cands(d, cands):
            continue
        r = D // d
        if not (rest_lo <= r <= rest_hi):
            continue
        acc.append((x, d))
        _decompose_walk(r, elems, i + 1, acc, out, limit)
        acc.pop()


def _readouts_through_muxes(tree, start: str) -> list:
    """The `output`/`activeOutput` readouts reachable from `start` **through muxes
    only**.

    Why "through muxes": [measured] on F429 `PLLP`'s next hop is the **mux**
    `SysClkSource`, and `PLLCLKFreq_Value` hangs behind it. Looking only at "who is
    fed directly" misses that hop — the first implementation missed it, so the
    constraint never applied. A mux does not change the frequency (`_factor_of`
    returns 1.0), so readouts below it have the upstream frequency.
    **Do not cross dividers/multipliers**: those change the frequency, and the
    readout would no longer belong to this stage.
    """
    out, seen, stack = [], set(), [start]
    while stack:
        cur = stack.pop()
        for el in tree.elements.values():
            if el.id in seen:
                continue
            if not any((i.get("from") or "") == cur for i in el.inputs):
                continue
            seen.add(el.id)
            if el.type in ("output", "activeOutput"):
                out.append(el)
            elif el.type == "multiplexor":
                stack.append(el.id)
    return out


def _signal_readouts(tree, start: str) -> list:
    """The **connection-level readouts** reachable from `start` **through muxes only**
    (`<Signal refParameter=…>`).

    [measured] F429's `PLLCLKFreq_Value` is not a box on the graph but the readout of
    `PLLP`'s output line `PLLCLK` (`STM32F4_F427-F437.xml:268`
    `<Signal id="PLLCLK" refParameter="PLLCLKFreq_Value"/>`). Looking only at
    element-level `output`/`activeOutput` misses it — the first implementation did.
    """
    out, seen, stack = [], set(), [start]
    while stack:
        cur = stack.pop()
        el = tree.get(cur)
        if el is None:
            continue
        for sid in el.outputs:
            ref = tree.signals.get(sid)
            if ref:
                out.append(ref)
        for nxt in tree.elements.values():
            if nxt.id in seen or nxt.type != "multiplexor":
                continue
            if any((i.get("from") or "") == cur for i in nxt.inputs):
                seen.add(nxt.id)
                stack.append(nxt.id)
    return out


# --------------------------------------------------------------------------
# Condition expressions and "condition-determined multiplier elements" (W5)
# --------------------------------------------------------------------------
#
# Many db parameters are **conditional**: one `<RefParameter>` name has several
# variants and `<Condition Expression="…">` decides which applies. The expressions
# mix three kinds of symbols:
#
#   1. `Semaphore` tokens such as `D2PPRE2_1`: declared on **another parameter's**
#      `PossibleValue/@Semaphore`, meaning "that parameter's current value is me"
#      (a db condition declaration);
#   2. `parameter=value` comparisons, e.g.
#      `RCC_TIM_PRescaler_Selection=RCC_TIMPRES_DESACTIVATED`;
#   3. device/family symbols (`STM32H7`, `DIE485`) — **undecidable**.
#
# Evaluation is **three-valued**: True / False / None. When undecidable the caller
# must treat it as unknown, **never as false** — that would turn ST's fallback rules
# into conclusions.

def _semaphore_defs(specs: dict) -> dict:
    """`{Semaphore name: (parameter name, value)}`, collected from every parameter's
    `PossibleValue/@Semaphore`."""
    out = {}
    for pname, sp in specs.items():
        for value, sem in (sp.get("semaphores") or {}).items():
            out.setdefault(sem, (pname, value))
    return out


def _cond_tokens(expr: str) -> list:
    s = (expr or "").replace("&amp;", "&")
    return [t for t in re.split(r"\s*([()!&|])\s*", s) if t != ""]


def _term_value(term: str, values: dict, sem_defs: dict):
    """Truth of one atomic term: True / False / None (undecidable)."""
    term = (term or "").strip()
    if not term:
        return None
    if "=" in term:
        left, _, right = term.partition("=")
        left = left.strip().split("$")[0]
        right = right.strip()
        if left in values:
            return str(values[left]) == right
        return None
    name = term.split("$")[0]
    if name in sem_defs:
        pname, pvalue = sem_defs[name]
        if pname in values:
            return str(values[pname]) == pvalue
        return None
    return None


def _and3(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _or3(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _not3(a):
    return None if a is None else (not a)


class _CondParser:
    """Recursive-descent evaluation of `&` / `|` / `!` / parentheses (three-valued
    logic)."""

    def __init__(self, tokens, values, sem_defs):
        self.t, self.i = tokens, 0
        self.values, self.sem = values, sem_defs

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self):
        v = self.peek()
        self.i += 1
        return v

    def run(self):
        return self.or_()

    def or_(self):
        v = self.and_()
        while self.peek() == "|":
            self.take()
            v = _or3(v, self.and_())
        return v

    def and_(self):
        v = self.not_()
        while self.peek() == "&":
            self.take()
            v = _and3(v, self.not_())
        return v

    def not_(self):
        if self.peek() == "!":
            self.take()
            return _not3(self.not_())
        return self.atom()

    def atom(self):
        tok = self.take()
        if tok == "(":
            v = self.or_()
            if self.peek() == ")":
                self.take()
            return v
        if tok is None or tok in (")", "&", "|"):
            return None
        return _term_value(tok, self.values, self.sem)


def _eval_cond(expr, values: dict, sem_defs: dict):
    """Evaluate a condition expression: True / False / None."""
    toks = _cond_tokens(expr)
    if not toks:
        return None
    return _CondParser(toks, values, sem_defs).run()


def _variant_scalar(v: dict):
    """The "value" a variant declares: the first candidate for list type, Min for
    integer type."""
    if v.get("values"):
        return v["values"][0]
    if v.get("min") is not None:
        try:
            return int(float(v["min"]))
        except (TypeError, ValueError):
            return None
    return None


def _determined_value(spec: dict, values: dict, sem_defs: dict):
    """What a **condition-determined** parameter should be now, returning
    `(value, explanation)`.

    Rules (based on the db `<Condition>` structure, not guesses):
    1. a variant whose condition is **definitely true** → use that variant's value;
    2. none is true → use the **unconditional fallback** variant (measured: in
       F429/H750 `RCC-*_Modes.xml` every such parameter has one);
    3. the conditions' truth is **undecidable** (device symbols) → return None,
       **no guessing**.
    """
    variants = spec.get("variants") or []
    unknown = []
    for v in variants:
        cond = v.get("condition")
        if not cond:
            continue
        r = _eval_cond(cond, values, sem_defs)
        if r is True:
            return _variant_scalar(v), "condition holds: %s" % cond
        if r is None:
            unknown.append(cond)
    for v in variants:
        if not v.get("condition"):
            return _variant_scalar(v), "no condition holds; using the unconditional fallback"
    if unknown:
        return None, "condition undecidable (contains unknown symbols): %s" % unknown[0]
    return None, "no variant's condition holds"


def determined_domain(spec: dict, values: dict, sem_defs: dict):
    """The **value domain actually in effect** for a parameter under the current
    conditions, returning ``(domain or None, explanation)``.

    A domain has two shapes, matching the two variant kinds in the db:

    - ``{"kind": "list", "values": [...]}`` — a ``Type="list"`` variant, whose
      legal values are those candidates;
    - ``{"kind": "range", "min": a, "max": b}`` — a ``Type="integer"`` variant,
      whose legal values are a **range**.

    A domain may carry ``provisional: True``: it comes from the "unconditional
    fallback" variant while **some other conditions are undecidable** (they contain
    device symbols). It is still returned, but the caller must treat it as a
    **hint** and warn — it may be wider than the branch that actually holds.

    ## Why the range kind must be returned too (fixed by measurement 2026-09-29)

    This used to accept only the candidates of `list` variants: a range variant has
    empty `values`, so it was skipped and the answer ended as "undecidable". The
    caller then fell back to the **union** from `get mode_param_possvalue` as the
    gate — **that union contains only `list` variants and drops range intervals
    entirely**, so legal values inside the interval were rejected.

    Measured `FMC.DataSetupTime1`: the db has two `list` variants (condition "burst
    mode is on", only candidate `255`) plus two `integer` variants (`1~255`). Burst
    mode is off, so the one that holds is `1~255`; the reference project's panel
    DATAST is `0x46 = 70`, yet the union gate rejected it ("available: 255").

    ## Why it exists (measured in W1)

    `PVDLevel` has 4 conditional variants, and the one behind
    `Sem_ExternalAnalogVolt` (set by mode `Mode_PVD_Ext`) **allows a single value**,
    `PWR_PVDLEVEL_7`. Measured: writing `PWR_PVDLEVEL_0` in that mode gives
    **command OK, value stored, but after reload it is back to `PWR_PVDLEVEL_7`** —
    the value had no effect.

    Meanwhile the CLI's `get mode_param_possvalue` returns the **union of all
    variants** (with reason), so using it as the gate lets `_0` through.
    The gate must therefore use the domain of **the variant that currently holds**.

    Returning ``None`` means "undecidable" — then the caller **must not** use the
    union to claim "this value is definitely legal/illegal"; it only warns and relies
    on reading back after writing.
    """
    variants = spec.get("variants") or []
    unknown = []

    def _domain_of(v):
        """The domain a variant declares: candidates for list, Min/Max for integer."""
        if v.get("values"):
            return {"kind": "list", "values": list(v["values"])}
        lo, hi = v.get("min"), v.get("max")
        if lo is not None and hi is not None:
            return {"kind": "range", "min": lo, "max": hi}
        return None

    def _with_default(domain: dict, variant: dict) -> dict:
        """Carry **this variant's own default** into the domain (key `default`).

        Why it is needed (2026-09-30): the `.ioc` **omits items equal to the
        default**, so the read-back path that reads the stored file must decide
        "is the key absent because it equals the default", and the criterion must be
        the default of **the variant in effect** — the merged outer `default` takes
        "the first non-empty one", which may not be this variant's (measured:
        `SPI5.NSS` has four variants with different defaults:
        `HARD_OUTPUT` / `HARD_INPUT` / `SOFT` / `SOFT`; the effective one is the
        unconditional variant → `SOFT`).
        """
        dflt = variant.get("default")
        if dflt and dflt != "null":
            return dict(domain, default=dflt)
        # This variant **declares no** default (common in the db: the
        # `Visible="false"` twin has `DefaultValue="null"`). Fall back to the defaults
        # declared by **all variants**, and use one only when there is **exactly
        # one** — the `.ioc` absence criterion treats it as "not written means the
        # default", and a wrong guess either passes or fails wrongly. More than one →
        # return None (the caller marks it "undecidable" and falls back to reload).
        vals = {str(v.get("default")).strip() for v in variants
                if v.get("default") and str(v.get("default")) != "null"}
        if len(vals) == 1:
            return dict(domain, default=vals.pop(), default_from_union=True)
        return domain

    for v in variants:
        cond = v.get("condition")
        if not cond:
            continue
        r = _eval_cond(cond, values, sem_defs)
        if r is True:
            d = _domain_of(v)
            if d:
                return _with_default(d, v), "condition currently holding: %s" % cond
        if r is None:
            unknown.append(cond)
    for v in variants:
        if not v.get("condition"):
            d = _domain_of(v)
            if d:
                if unknown:
                    return (dict(_with_default(d, v), provisional=True),
                            "no condition holds; using the unconditional fallback "
                            "variant (%d other conditions are undecidable and were "
                            "not counted: %s)"
                            % (len(unknown), unknown[0]))
                return _with_default(d, v), "no condition holds; using the unconditional fallback variant"
    if unknown:
        return None, "condition undecidable (contains unknown symbols): %s" % unknown[0]
    return None, "no variant's condition holds"


def determined_values(spec: dict, values: dict, sem_defs: dict):
    """Legacy interface returning only the candidate list (``(list or None, why)``).

    ⚠ **A parameter whose domain is a range returns `None` here** — that is not
    "undecidable", it is "this interface cannot express it". Callers that validate a
    value should use :func:`determined_domain`.
    """
    d, why = determined_domain(spec, values, sem_defs)
    if d and d.get("kind") == "list":
        return list(d["values"]), why
    if d and d.get("kind") == "range":
        return None, ("%s (the domain is the range %s~%s; this interface cannot "
                      "express it, use determined_domain)"
                      % (why, d.get("min"), d.get("max")))
    return None, why


def _owner_of_condition(spec: dict, chain_elems: list, specs: dict,
                        sem_defs: dict):
    """Which **divider on the chain** this multiplier's condition depends on.

    Returns `(chain element, that parameter name)`; `(None, None)` when the
    dependency is not on the chain.
    """
    names = set()
    for v in spec.get("variants") or []:
        for tok in _cond_tokens(v.get("condition") or ""):
            if tok in ("", "(", ")", "!", "&", "|"):
                continue
            if "=" in tok:
                names.add(tok.partition("=")[0].strip().split("$")[0])
            else:
                names.add(tok.split("$")[0])
    for nm in sorted(names):
        # Both spellings must be accepted (both measured in the db):
        #   1. a Semaphore token (`D2PPRE2_1`) → the parameter name it points at;
        #   2. **the parameter name written directly** (H5's
        #      `APB2CLKDivider=RCC_HCLK_DIV1`, see the condition of
        #      `TimPrescalerAPB2` in `RCC-STM32H5_rcc_v1_0_Modes.xml`).
        cand = sem_defs.get(nm, (None, None))[0] or nm
        for el in chain_elems:
            rps = [p.strip() for p in str(el.get("ref_parameter") or "").split(",")]
            if cand in rps:
                return el, cand
    return None, None


def _try_fold_mul(x: dict, chain_elems: list, specs: dict, sem_defs: dict,
                  base_values: dict):
    """Fold a "condition-determined multiplier" into the divider it depends on (W5).

    Shape (from the db clock definition)::

        D2PPRE1 (divider) ──→ Tim1Mul (multiplier) ──→ timer clock readout

    `Tim1Mul`'s value condition is `(D2PPRE1_1)&…` (a db value condition), and
    `D2PPRE1_1` is a Semaphore attached to `D2PPRE1`'s PossibleValue.
    So the pair **is equivalent to one divider**: effective divider = the divider's
    value ÷ the multiplier's value.

    **Without this fold, a condition-determined quantity becomes a free unknown** —
    exactly the inconsistent solution W5 worried about ("APB=1 but the timer divider
    is 2").

    :returns: `(upstream divider element, {effective divider: (raw value to write,
        multiplier value)})`; None when it must not guess.
    """
    spec = _spec_of(specs, x)
    if not spec:
        return None
    owner, pname = _owner_of_condition(spec, chain_elems, specs, sem_defs)
    if owner is None:
        return None
    owner_sp = spec_for(specs, owner.get("ref_parameter"))[0]
    vals = list(owner_sp.get("values") or [])
    if not vals:
        return None                     # never seen on range dividers; no guessing
    table = {}
    for v in vals:
        num = _numeric(v, owner_sp)
        if not num:
            continue
        mul, _why = _determined_value(spec, {**base_values, pname: v}, sem_defs)
        if mul is None:
            return None
        eff = Fraction(int(num), int(mul))
        if eff.denominator != 1:
            return None
        table.setdefault(int(eff), (v, int(mul)))
    return (owner, table) if table else None



def _enabled_now(el, params: dict | None) -> bool:
    """Whether this element's `refEnable` holds **right now**.

    `refEnable` may hold **several comma-separated names**; any one true means
    enabled (the same rule as `clockvalues.py`).

    **Undecidable is handled conservatively (return True = keep the constraint)**:
    measured on F0, 3 of 14 enable parameters
    (`ADCEnable`/`I2S1Enable`/`I2S2Enable`) are **missing** from saveext. Silently
    dropping the constraint there can produce a solution that "looks solved but is out
    of range"; keeping it at worst solves one target less, which is far safer.
    (`apply`'s closed loop runs the tree check again, but that is a second line of
    defence and should not be the first.)
    """
    en = getattr(el, "ref_enable", None)
    if not en:
        return True
    names = [x.strip() for x in en.split(",") if x.strip()]
    if not names or not params:
        return True
    got = [params.get(n) for n in names]
    if all(v is None for v in got):
        return True
    return any(truthy(v) for v in got)


def _stage_readout_constraints(tree, specs, forward, n_idx,
                               params: dict | None = None) -> dict:
    """Min/Max constraints of "this stage's readout" for every element on the chain.

    Readouts come from two sources and both are required:
      1. element level: `output` / `activeOutput` elements (possibly behind muxes);
      2. **connection level**: `<Signal refParameter=…>` (see `_signal_readouts`).
    """
    out = {}
    for x in forward[n_idx:]:
        # **Only readouts of elements that are really enabled now** are collected;
        # see `_enabled_now`: measured, F0's `USBOutput`
        # (`refEnable="USBEnable"`) still demands a PLL output of about 48 MHz with
        # USB disabled, which kills the 10 MHz solution.
        els = [el for el in _readouts_through_muxes(tree, x["element"])
               if _enabled_now(el, params)]
        names = [el.ref_parameter for el in els]
        names += _signal_readouts(tree, x["element"])
        cons, seen = [], set()
        for name in names:
            if not name or name in seen:
                continue
            seen.add(name)
            sp = spec_for(specs, name)[0]
            lo, hi = sp.get("min"), sp.get("max")
            if lo is None or hi is None:
                continue
            try:
                cons.append((name, float(lo), float(hi)))
            except (TypeError, ValueError):
                continue
        if cons:
            out[x["element"]] = cons
    return out


def _fits(cons, hz) -> bool:
    """Does this frequency satisfy the Min/Max of these readouts?"""
    return all(lo <= hz <= hi for _p, lo, hi in cons)


def _element_bands(specs, forward, model) -> dict:
    """Min/Max declared by an element's **own readout**, keyed by element.

    `_stage_readout_constraints` only collects the readings an element *feeds*; the
    reading it *is* (for example `HCLKFreq_Value` on the AHB output, or
    `SYSCLKFreq_VALUE` on the system-clock output) has a Min/Max of its own in the
    RCC definition XML, and a solution that violates it is not a legal
    configuration even when the target itself lands.

    **Only readout elements count.** For a settable element the same Min/Max is the
    range of its *value* (PLLM 2..63, a divider 1..512), not a frequency; treating it
    as a band would reject every legal solution on that path.
    """
    out = {}
    for x in forward:
        if (model.get(x["element"]) or {}).get("kind") != "derived":
            continue
        spec = _spec_of(specs, x)
        low, high = spec.get("min"), spec.get("max")
        if low is None and high is None:
            continue
        try:
            # a reading may declare only one side (measured: F4's SYSCLKFreq_VALUE has
            # a max and no min); an absent side means "no limit on that side"
            out.setdefault(x["element"], []).append(
                (x.get("ref_parameter") or x["element"],
                 float(low) if low is not None else float("-inf"),
                 float(high) if high is not None else float("inf")))
        except (TypeError, ValueError):
            continue
    return out


def _stages_fit(vco, n_el, forward, n_idx, split, stage_cons, specs, bands=None) -> bool:
    """Substitute one solution and **walk the chain stage by stage**, checking each
    stage's readout.

    `vco` already contains N, so the N stage's readout is checked against `vco`
    directly; after that every divider multiplies in its factor (`_factor_of`);
    muxes do not change the frequency, they only carry the readout through.
    `bands` are the Min/Max of the elements' own readings (see `_element_bands`).
    """
    by_el = {x["element"]: v for x, v in split}
    f = float(vco)
    if not _fits(stage_cons.get(n_el["element"], ()), f):
        return False
    if bands and not _fits(bands.get(n_el["element"], ()), f):
        return False
    for x in forward[n_idx + 1:]:
        if x["type"] in _DIV_TYPES:
            v = by_el.get(x["element"])
            if v is None:
                return False
            sp = _spec_of(specs, x)
            fac = _factor_of(x, sp, v)
            if not fac:
                return False
            f = f * float(fac)
        if not _fits(stage_cons.get(x["element"], ()), f):
            return False
        if bands and not _fits(bands.get(x["element"], ()), f):
            return False
    return True


#: Element types that are "sources" in the clock tree. **`distinctValsSource` must be
#: included**: L4's chain head `MSIRC` is one
#: (`db/plugins/clock/STM32L4.xml:59`); leaving it out → the source is skipped →
#: "cannot read the chain's source frequency".
_SOURCE_TYPES = ("fixedSource", "variedSource", "distinctValsSource")


def _source_hz(tree, specs, el, current: dict | None,
               params: dict | None) -> float | None:
    """Frequency (Hz) of one "source" element. None when it cannot be found (the caller
    reports it honestly; no guessing).

    ## Lookup order (all measured)

    1. **the mux's current value** (`current[element name]`) — the old path; a
       variable source may not be at its default;
    2. **the element refParameter's current value** (`params`, the full RCC values
       from one `config saveext`);
    3. the element refParameter's **db default**;
    4. **the readout on its output signal** (`<Signal refParameter=…>`) — this is how
       L4's `MSIRC` works: its `refParameter` is `MSIClockRange` (an **enum name**
       `RCC_MSIRANGE_6`, not a frequency), and the real number hangs on the output
       signal readout `MSI_VALUE` (`:470`, `DefaultValue="4000000"`; **the db stores
       Hz**, see `_hz`). The readout's current value wins (`params`), then the db
       default.

    ⚠ The readout path **must not convert by the `Unit` attribute**: `MSI_VALUE` has
    `Unit="KHz"` but a value of `4000000` — converting by KHz is off by 1000x.
    `_hz` always reads Hz, and this follows it.
    """
    # ⚠ The `el` passed in is a **dict** from `forward` (keys `element`/`type`/
    # `ref_parameter`), not a `clockxml.Element` object — index it, do not use
    # attributes.
    eid = el["element"]
    names = [x.strip() for x in (el.get("ref_parameter") or "").split(",")
             if x.strip()]
    # 1) the mux's current value
    got = _hz((current or {}).get(eid))
    if got:
        return got
    # 2) the element parameter's current value
    for nm in names:
        got = _hz((params or {}).get(nm))
        if got:
            return got
    # 3) the element parameter's db default
    for nm in names:
        sp = spec_for(specs, nm)[0]
        got = _hz(sp.get("default"))
        if got:
            return got
    # 4) the readout on the output signal
    for ref in _signal_readouts(tree, eid):
        got = _hz((params or {}).get(ref))
        if got:
            return got
        got = _hz(spec_for(specs, ref)[0].get("default"))
        if got:
            return got
    return None


# --------------------------------------------------------------------------
# Fractional PLL parts (data-driven from the clock tree and the RCC definition)
# --------------------------------------------------------------------------
#
# The db models a PLL feedback divider as `multiplicatorFrac` with a second input
# that is a `fractional` element (H7: `DIVN1` ← `PLLFRACN`). The arithmetic of that
# pair is  VCO = VCOin * (N + FRACN / div); the divisor comes from the tree reference
# (`PARAM/DIV`) and otherwise defaults to the plugin constant 8192.

#: Divisor of a fractional part when the db does not declare one (plugin default).
_DEFAULT_FRAC_DIVISOR = 8192


def _fractional_companion(tree, element_id: str):
    """The `fractional` element feeding a `multiplicatorFrac`, or None.

    Found through the clock-tree inputs (the second input is the fractional part), not
    by name, so `DIVN1`/`PLLN`/`PLL2N` all work the same way.
    """
    element = tree.get(element_id)
    for conn in (element.inputs if element is not None else []):
        sub = tree.get(conn.get("from") or "")
        if sub is not None and sub.type == "fractional":
            return sub
    return None


def _frac_divisor(ref) -> int | None:
    """Divisor of the fractional part: `PARAM/DIV` from the tree, else the plugin default."""
    text = str(ref or "")
    if "/" in text:
        try:
            return int(text.split("/", 1)[1])
        except (TypeError, ValueError):
            return None
    return _DEFAULT_FRAC_DIVISOR


def _frac_limits(specs, frac, divisor: int) -> tuple:
    """Allowed range of the fractional part (db Min/Max; at most divisor-1)."""
    spec = spec_for(specs, _spec_name(frac.ref_parameter))[0]
    lo, hi = _int_range(spec)
    lo = 0 if lo is None else lo
    hi = divisor - 1 if hi is None else min(hi, divisor - 1)
    return lo, hi


def _frac_current(specs, frac, current=None, params=None, overrides=None):
    """The fractional part value now: pin > current > saved value > db default."""
    element = frac.id
    if overrides and element in overrides:
        return overrides[element]
    if current and element in current:
        return current[element]
    name = _spec_name(frac.ref_parameter)
    if params and name in params:
        return params[name]
    return spec_for(specs, name)[0].get("default")


def _mul_factor(tree, specs, x: dict, value, current=None, params=None,
                overrides=None):
    """Forward factor of a multiplier element: N, or N + FRACN/div for `multiplicatorFrac`."""
    num = _numeric(value, _spec_of(specs, x))
    if num is None:
        return None
    if x["type"] != "multiplicatorFrac":
        return _frac(num)
    frac = _fractional_companion(tree, x["element"])
    if frac is None:
        return _frac(num)
    divisor = _frac_divisor(frac.ref_parameter)
    if not divisor:
        return None
    frac_value = _frac_current(specs, frac, current, params, overrides)
    frac_num = _numeric(frac_value, spec_for(specs, _spec_name(frac.ref_parameter))[0])
    if frac_num is None:
        return None
    return _frac(num + Fraction(int(round(frac_num)), divisor))


def _frac_pair(need: Fraction, divisor: int, lo: int, hi: int,
               n_set: set, forced_frac=None, pinned_n=None):
    """Split a required multiplier ratio into (N, F): `need = N + F/divisor`.

    Returns None when this ratio is not representable at the db resolution (for
    example it needs F = div/3). `forced_frac` pins the fractional part (an explicit
    value); `pinned_n` pins the integer part.
    """
    if need <= 0:
        return None
    whole = int(need)                       # floor: the fractional part is additive
    rest = need - whole
    if forced_frac is not None:
        frac = int(forced_frac)
        n_value = need - Fraction(frac, divisor)
        if n_value.denominator != 1:
            return None
        n_int = int(n_value)
        if n_int not in n_set:
            return None
        if pinned_n is not None and n_int != int(pinned_n):
            return None
        return n_int, frac
    scaled = rest * divisor
    if scaled.denominator != 1:
        return None                        # not representable at this resolution
    frac = int(scaled)
    n_int = whole
    if frac > hi or frac < lo:
        return None
    if frac == divisor:                    # carry (cannot happen with frac < divisor)
        n_int, frac = whole + 1, 0
    if n_int not in n_set:
        return None
    if pinned_n is not None and n_int != int(pinned_n):
        return None
    return n_int, frac


def _feeds(tree, el_id: str, cand_id: str) -> bool:
    """Whether `cand_id` feeds `el_id` **directly**."""
    el = tree.get(el_id)
    if el is None:
        return False
    return any((i.get("from") or "") == cand_id for i in el.inputs)


#: Pass-through elements: they do not change the frequency, they only draw a readout
#: box on the graph.
_PASS_TYPES = ("output", "activeOutput")


def _find_m_divisor(tree, divs: list, n_el: dict):
    """Find the divider that **directly** feeds multiplier N; **None when there is none
    (meaning M=1)**.

    ## Why it walks through pass-through elements

    L1's shape is `PLLSource → VCOIIuput(type=output pass-through) → PLLMUL`
    (the db clock definition) — N's input is a **pass-through readout**, not a
    divider. So the search upward from N **only walks through pass-through elements**
    (`output`/`activeOutput`) and stops at any other type.

    ⚠ **Only pass-through elements**, do not "walk up the chain freely": that would
    mistake a **source-side** divider (H750's `HSIDiv`) for M — another bug this tool
    fixed earlier (see the original comment in this code).
    """
    frontier, seen = [n_el["element"]], {n_el["element"]}
    for _ in range(8):                       # loop guard
        nxt = []
        for eid in frontier:
            for x in divs:
                if _feeds(tree, eid, x["element"]):
                    return x
            el = tree.get(eid)
            for i in (el.inputs if el else []):
                src = i.get("from") or ""
                if not src or src in seen:
                    continue
                sub = tree.get(src)
                if sub is None or sub.type not in _PASS_TYPES:
                    continue                 # not a pass-through element: this line ends here
                seen.add(src)
                nxt.append(src)
        if not nxt:
            return None
        frontier = nxt
    return None


def _solve_chain(tree, specs, model, forward: list, chosen: dict,
                 target_hz: float, limit: int, current: dict | None,
                 params: dict | None = None,
                 fixed: dict | None = None,
                 search_budget: int | None = None) -> dict:
    """Solve one branch: an already-met target, a plain divider path, or a PLL path.

    The shape decides the arithmetic, never the family name: a branch that goes
    through the PLL input selector is solved by the M/N search, everything else is a
    plain multiply/divide path (see :mod:`cubemx1.clockplain`). A target that the
    current settings already meet is answered with no writes at all.
    """
    from . import clockplain

    # `fixed` is part of the state: a target solved earlier pins dividers, so the
    # "already satisfied" answer must be computed with those values, not with the
    # db defaults (measured: 12 MHz + fixed HSISYS=1 was reported as met when the
    # real configuration gives 48 MHz).
    zero = clockplain.already_satisfied(tree, specs, model, forward, chosen,
                                        target_hz, current, params, fixed or {})
    if clockplain.pll_segment_index(forward) is None:
        got = clockplain.solve_plain(tree, specs, model, forward, chosen,
                                     target_hz, limit, current, params,
                                     fixed or {}, search_budget)
    else:
        got = _solve_pll(tree, specs, model, forward, chosen, target_hz, limit,
                         current, params, fixed)
        if not got.get("status"):
            # The M/N search has its own failure reasons: a branch that got past the
            # shape checks and still has no exact answer was searched completely, while
            # an early refusal means the shape itself is not modeled.
            got["status"] = (clockplain.STATUS_SOLVED if got["ok"] else
                             clockplain.STATUS_EXHAUSTED if "vco_bounds" in got
                             else clockplain.STATUS_UNSUPPORTED)
    if zero is None:
        return got
    return _zero_first(zero, got, limit)


def _zero_first(zero: dict, got: dict | None, limit: int) -> dict:
    """The zero-write answer stays **first**, the path's other answers follow it.

    A target that the current settings already meet has a correct answer that writes
    nothing, and that answer must stay the first candidate: solving one target on its
    own must not rewrite an equivalent configuration. It is not the **only** candidate
    on that path, though — the same frequency normally has other divider combinations,
    and a later target of a joint search may need one of them.

    Measured (STM32C031, reported by the independent review): HCLK = 12 MHz then
    SYSCLK = 48 MHz is satisfiable by `HSISYSCLKDivider=1` + `AHBCLKDivider=4`. The
    first target is already met by the db defaults, so the search used to stop there;
    its settings pinned the default divider, the second target could not backtrack,
    and the pair was wrongly rejected while the reversed order passed. Handing the
    alternatives out after the zero-write answer fixes the order dependence without
    writing the dividers of a target that needs no write.
    """
    keep = max(1, int(limit or 1))
    merged = list(zero.get("solutions") or [])
    seen = [dict(solution.settings or {}) for solution in merged]
    dropped = False
    for solution in (got or {}).get("solutions") or []:
        if len(merged) >= keep:
            dropped = True
            break
        settings = dict(solution.settings or {})
        if settings in seen:
            continue
        seen.append(settings)
        merged.append(solution)
    zero["solutions"] = merged
    # an answer left out because of the candidate budget is a cut search, not a
    # complete one
    zero["truncated"] = bool((got or {}).get("truncated") or dropped)
    return zero


def _solve_pll(tree, specs, model, forward: list, chosen: dict,
               target_hz: float, limit: int, current: dict | None,
               params: dict | None = None,
               fixed: dict | None = None) -> dict:
    """Solve **one** chain whose branch is already fixed (the branch is the mux
    selection, an input, not an unknown).

    ## Algorithm (rewritten 2026-09-26 to support long chains)

    The old version solved only the **direct chain** "target → single PLL → target":
    it required exactly **one** unknown output divider between the PLL and the
    target, with at most 4 stages. Measured, that rejected two real targets:

    - H750's `APB2Freq_Value`: 4 dividers between the PLL and the target
      (`DIVP1` → `D1CPRE` → `HPRE` → `D2PPRE2`), reported as "7 stages apart";
    - F429's `SAI_AClocksFreq_Value`: two cascaded output dividers
      (`PLLI2SQ` → `PLLI2SDivQ`), reported as "2 unknown output dividers, only one
      can be solved".

    The model is now **one run of multiply/divide, stage count ignored**:

        target = VCO / D          D = ∏(value of each divider after N)
        VCO    = src_at_m * N / M

    So ``N = target * D * M / src_at_m``. M and D are the two enumeration layers,
    but **D is not enumerated exhaustively**: "N must be an integer" implies D must be
    a multiple of some q — reduce ``target*M / src_at_m`` to the fraction
    ``step_n / q``, then ``D = q*t`` and ``N = step_n*t``. t is bounded above by
    N's range and the VCO band, so each layer has tens of candidates, not thousands of
    combinations.

    Finally D is **split into the product of the divider values** (for each element,
    filter the divisors of D); only a split that works counts as a solution.
    Enum and range dividers are treated alike here, and the stage count does not
    matter.
    """
    unknowns = _unknowns(forward, model)
    if not unknowns:
        return {"ok": False, "solutions": [], "forward": forward,
                "chosen": chosen, "mux_steps": [],
                "reason": "this chain has no divider/multiplier with kind=range "
                          "(no solvable PLL segment); only discrete dividers are "
                          "left. This tool only solves integer PLL solutions."}

    divs = [x for x in unknowns if x["type"] in _DIV_TYPES]
    muls = [x for x in unknowns if x["type"] in _MUL_TYPES]
    if not divs or not muls:
        return {"ok": False, "solutions": [], "forward": forward,
                "chosen": chosen, "mux_steps": [],
                "reason": "the PLL segment lacks a divider or multiplier "
                          "(divs=%s muls=%s), cannot solve"
                          % ([x["element"] for x in divs],
                             [x["element"] for x in muls])}
    # **M is "the divider that directly feeds N"**, judged by N's input `from` in the
    # clock tree. "The first divider on the chain" is no longer usable: the source
    # side may have a divider too (H750's `HSIDiv`), and enum elements now count as
    # unknowns (see `_numeric_candidates`), so "the first one" becomes `HSIDiv` — the
    # solved value is mathematically right, but what gets written is a source-side
    # divider with a completely different blast radius (measured: H750's solution moved
    # from writing `DIVM1` to writing `HSIDiv`).
    n_el = muls[0]
    m_el = _find_m_divisor(tree, divs, n_el)

    # --- Elements between the source and the PLL that fold into a "prescaler factor" ---
    # **The criterion is "before M" (or "before N" when there is no M)**,
    # ⚠ **not** "before the first unknown" — this used to be
    # `pre_els = forward[:forward.index(unknowns[0])]`, so a **fixed divider with a
    # single value** (db `min == max`, e.g. F1's `HSIDivPLL` with `min=2 max=2`) was
    # counted as an unknown and thus excluded from `pre_els`; and since it does not
    # feed N **directly** (F1's `PLLMUL` has a `VCO2output` in between),
    # `_find_m_divisor` could not find it either → **its ÷2 vanished from the
    # arithmetic**.
    # Measured symptom (2026-09-28): F1 `SPI1 → 10MHz` solved to `M=1 N=5 VCO=40MHz`
    # and read back **5 MHz** — the real chip's PLL input is HSI/2 = 4 MHz, not 8 MHz.
    pre_end = forward.index(m_el) if m_el is not None else forward.index(n_el)
    pre_els = forward[:pre_end]

    # --- source frequency ---
    # The branch is fixed, so the source is the first real source in pre_els (from the
    # chain head).
    src_hz, src_name = None, None
    for x in pre_els:
        if x["type"] not in _SOURCE_TYPES:
            continue
        src_hz = _source_hz(tree, specs, x, current, params)
        src_name = x["element"]
        break
    if not src_hz:
        return {"ok": False, "solutions": [], "forward": forward,
                "chosen": chosen, "mux_steps": [],
                "reason": "cannot read the chain's source frequency (no source "
                          "element upstream yields a frequency)"}

    # --- known dividers between the source and the PLL (e.g. HSIDiv / HSIDivPLL) ---
    # **Current values** win over db defaults: a default may differ from what the
    # project actually sets.
    #
    # ⚠ **Only real divider elements contribute a factor** (`_DIV_TYPES`).
    # This used to skip only "sources + muxes", so `output` / `activeOutput`
    # **readout elements** were folded in too — their `refParameter` is a frequency
    # readout (e.g. F1's `VCOOutput2Freq_Value` = `4000000`), and `_factor_of` treated
    # it as the factor **4000000**, turning `src_at_m` into 1.6e13 Hz.
    # It was invisible before because `pre_els` never reached those elements (see the
    # `pre_end` part above); now that `pre_els` extends to before M/N, the type check
    # must be exact.
    pre_factor = Fraction(1)
    pre_values = {}
    for x in pre_els:
        # **Source elements are always skipped** (they are not "divider/multiplier
        # factors").
        # ⚠ This used to read `("fixedSource", "variedSource", "multiplexor")`,
        # **missing `distinctValsSource`** — L4's chain head `MSIRC` is one, so it was
        # treated as a divider and `MSIRC`'s `refParameter` (`MSIClockRange`, whose
        # candidates are MSI ranges 100/200/…/4000) was folded into the factor
        # **4000**, turning `src_at_m` from 4 MHz into **16 GHz** → no target could be
        # solved. Measured symptom: "no exact solution found", while the source
        # frequency, shape, constraints and candidates were **all correct**.
        if x["type"] not in _DIV_TYPES:
            continue
        sp = _spec_of(specs, x)
        # The value order matches `_source_hz`: **live values first**, then the db
        # default.
        # `current` holds only **mux** values (`_current_mux_values`), so a settable
        # source-side divider (e.g. H750's `HSIDiv`) must be looked up in `params`
        # (the full RCC values from one `config saveext`) by refParameter — looking
        # only at `current` falls back to the db default, which may differ from what
        # the project sets.
        val = (current or {}).get(x["element"])
        if not val:
            for _nm in str(x["ref_parameter"] or "").split(","):
                val = (params or {}).get(_nm.strip())
                if val:
                    break
        if not val:
            val = sp.get("default")
        f = _factor_of(x, sp, val)
        if f is None:
            return {"ok": False, "solutions": [], "forward": forward,
                    "chosen": chosen, "mux_steps": [],
                    "reason": "the upstream %s (%s) has no numeric value, so the "
                              "source frequency cannot be determined"
                              % (x["element"], x["ref_parameter"])}
        pre_factor *= _frac(f)
        pre_values[x["element"]] = _normalize_setting(x, sp, val)

    # **`m_el is None` is a legal shape** (L1: `PLLSource → VCOIIuput(output
    # pass-through) → PLLMUL`), meaning **M = 1 (no input divider)**.
    # This used to fall back to `m_el = divs[0]`, taking the **post divider**
    # (L1's `PLLDIV`) as M, so `m_idx > n_idx` → "the PLL's input divider comes after
    # the multiplier, shape not understood" — which is exactly how L1 failed.

    # The VCO band is taken **per branch PLL** (W4). The token is derived from the
    # chain's element names (`DIVM2`→"2", `PLLI2SM`→"I2S", `PLLSAI1M`→"SAI1");
    # only when nothing can be derived, or no such name exists in the db, does it fall
    # back to "the first one that exists" with `by_branch=False` in the result note.
    branch_names = [x["element"] for x in unknowns]
    vmin, vmax, vname, v_by_branch = _vco_bounds(specs, branch_names)
    imin, imax, iname, i_by_branch = _vco_input_bounds(specs, branch_names)
    n_idx = forward.index(n_el)
    if m_el is not None:
        m_idx = forward.index(m_el)
        if m_idx > n_idx:
            return {"ok": False, "solutions": [], "forward": forward,
                    "chosen": chosen, "mux_steps": [],
                    "reason": "the PLL input divider %s comes after the multiplier "
                              "%s; this shape is not understood, no guessing."
                              % (m_el["element"], n_el["element"])}

    # Without M, `m_cands = [1]`: the enumeration keeps M at 1, and the write section
    # guards with `if m_lo != m_hi` → **no M command is emitted**.
    m_sp = spec_for(specs, m_el["ref_parameter"])[0] if m_el else {}
    n_sp = spec_for(specs, n_el["ref_parameter"])[0]
    # The set of usable numbers: a range gives the interval, **an enum gives
    # value_numbers** (G0/L4/U0's `PLLM` is an enum, see `_numeric_candidates`)
    m_cands = _numeric_candidates(m_sp) if m_el else [1]
    n_cands = _numeric_candidates(n_sp)
    if not m_cands or not n_cands:
        return {"ok": False, "solutions": [], "forward": forward,
                "chosen": chosen, "mux_steps": [],
                "reason": "%s or %s has no usable numbers in the db (neither Min/Max "
                          "nor candidates with numbers), cannot solve"
                          % (m_el["element"] if m_el else "(no input divider)",
                             n_el["element"])}
    n_set = set(n_cands)
    m_lo, m_hi = m_cands[0], m_cands[-1]
    n_lo, n_hi = n_cands[0], n_cands[-1]

    # --- **All** dividers between N and the target = the factors K to split ---
    # Enum and range types are both here, and the stage count is unlimited (that is
    # why "long chains" work).
    #
    # **Condition-determined multipliers are folded first (W5)**: when the chain
    # contains a multiplier whose value is decided by a chain divider's current value
    # (the condition references that divider's Semaphore), the pair is folded into
    # **one divider** (effective divider = divider value ÷ multiplier value) instead
    # of being treated as a free unknown (which yields inconsistent solutions like
    # "APB=1 but the timer divider is 2").
    tail = forward[n_idx + 1:]
    sem_defs = _semaphore_defs(specs)
    cond_values = {n: sp["default"] for n, sp in specs.items()
                   if sp.get("default") is not None}
    for el_id, v in (current or {}).items():
        _el = tree.get(el_id)
        if _el is not None and _el.ref_parameter:
            for _nm in str(_el.ref_parameter).split(","):
                cond_values[_nm.strip()] = v
    folded = {}
    for i, x in enumerate(tail):
        if x["type"] not in _MUL_TYPES:
            continue
        got = _try_fold_mul(x, tail[:i], specs, sem_defs, cond_values)
        if got is None:
            return {"ok": False, "solutions": [], "forward": forward,
                    "chosen": chosen, "mux_steps": [],
                    "reason": "the multiplier %s after the PLL segment is not "
                              "condition-determined (its value condition in the db "
                              "does not point at a chain divider, or the condition is "
                              "undecidable) — no guessing."
                              % x["element"]}
        owner, table = got
        folded[owner["element"]] = table

    k_elems = []
    for x in tail:
        if x["type"] in ("multiplexor", "output", "activeOutput"):
            continue
        if x["type"] in _MUL_TYPES:
            continue                      # already folded into the upstream divider
        if x["type"] not in _DIV_TYPES:
            continue
        if x["element"] in folded:
            # Composite divider: the candidates are "effective dividers" (the
            # multiplier's effect is already included)
            c = ("set", set(folded[x["element"]].keys()))
        else:
            c = _divisor_candidates(x, _spec_of(specs, x),
                                    model)
            if c is None:
                return {"ok": False, "solutions": [], "forward": forward,
                        "chosen": chosen, "mux_steps": [],
                        "reason": "divider element %s (%s) has no candidates in the "
                                  "db; no guessing."
                                  % (x["element"], x["ref_parameter"])}
        k_elems.append((x, c))

    # Constraints of the "outgoing readout" of N and every stage below it (see
    # `_stage_readout_constraints`)
    stage_cons = _stage_readout_constraints(tree, specs, forward, n_idx, params)
    # the elements' own readings (e.g. SYSCLKFreq_VALUE) are limits too
    bands = _element_bands(specs, forward, model)

    cands_by_el = {x["element"]: c for x, c in k_elems}

    # --- muxes to write: a command is emitted only when the value changes ---
    mux_steps = []
    for el_id, rv in chosen.items():
        cur = (current or {}).get(el_id)
        if cur is not None and str(cur) == str(rv):
            continue
        mux_steps.append((el_id, rv))
    is_current_branch = not mux_steps

    # **Arithmetic elements whose command name contains a comma: never measured, so no
    # guessing.**
    # [measured] An arithmetic element's registered command name is the XML
    # `refParameter` attribute **verbatim**. Measured on F429: two elements have a
    # comma-separated refParameter, but both are muxes (their command name comes from
    # multName, so they are unaffected); no arithmetic element has shown this yet.
    for _x in ([x for x, _ in k_elems] + ([m_el] if m_el else []) + [n_el]):
        if "," in str(_x.get("ref_parameter") or ""):
            return {"ok": False, "solutions": [], "forward": forward,
                    "chosen": chosen, "mux_steps": [],
                    "reason": "element %s has a comma-separated refParameter (%s); "
                              "which name `clock set` should use has never been "
                              "measured, so no guessing."
                              % (_x["element"], _x["ref_parameter"])}

    # --- enumeration: M on the outside, D (the total ratio of the divider chain)
    # inside ---
    src_at_m = Fraction(int(round(src_hz))) * pre_factor
    tgt = Fraction(int(round(target_hz)))       # frequencies as integer Hz, to avoid
                                                # float noise
    fixed = fixed or {}
    pinned_m = fixed.get(m_el["element"]) if m_el is not None else None
    pinned_n = fixed.get(n_el["element"])
    # fractional PLL: the feedback divider may add FRACN/div to N
    frac_el = _fractional_companion(tree, n_el["element"])
    frac_divisor = _frac_divisor(frac_el.ref_parameter) if frac_el is not None else None
    frac_lo = frac_hi = None
    frac_ok = set(n_set)
    pinned_frac = fixed.get(frac_el.id) if frac_el is not None else None
    if frac_el is not None:
        if not frac_divisor:
            return {"ok": False, "solutions": [], "forward": forward,
                    "chosen": chosen, "mux_steps": [], "truncated": False,
                    "status": "unsupported",
                    "reason": "the divisor of the fractional part %s cannot be read "
                              "from the db, so the fractional arithmetic is not modeled"
                              % frac_el.id}
        frac_lo, frac_hi = _frac_limits(specs, frac_el, frac_divisor)
        if pinned_frac is not None:
            try:
                pinned_frac = int(str(pinned_frac))
            except (TypeError, ValueError):
                return {"ok": False, "solutions": [], "forward": forward,
                        "chosen": chosen, "mux_steps": [], "truncated": False,
                        "status": "conflict",
                        "reason": "the fixed fractional part %r of %s is not an integer"
                                  % (fixed.get(frac_el.id), frac_el.id)}
            if not (frac_lo <= pinned_frac <= frac_hi):
                return {"ok": False, "solutions": [], "forward": forward,
                        "chosen": chosen, "mux_steps": [], "truncated": False,
                        "status": "conflict",
                        "reason": "the fixed fractional part %d of %s is outside its "
                                  "db range %d..%d"
                                  % (pinned_frac, frac_el.id, frac_lo, frac_hi)}
        ref_parts = str(n_el.get("ref_parameter") or "").split("/")
        if len(ref_parts) > 1:
            list_name = _spec_name(ref_parts[1])
            list_spec = spec_for(specs, list_name)[0]
            allowed = {int(_numeric(v, list_spec)) for v in (list_spec.get("values") or [])
                       if _numeric(v, list_spec)}
            if not allowed:
                return {"ok": False, "solutions": [], "forward": forward,
                        "chosen": chosen, "mux_steps": [], "truncated": False,
                        "status": "unsupported",
                        "reason": "%s declares which N values may carry a fraction "
                                  "(%s), but that list cannot be read"
                                  % (n_el["element"], list_name)}
            frac_ok = allowed
    out = []
    # a cut search must say so instead of claiming the model was searched out
    splits_cut = False
    solutions_cut = False
    for M in m_cands:
        if pinned_m is not None and int(M) != int(pinned_m):
            continue
        vco_in = src_at_m / M
        if vco_in <= 0:
            continue
        # **The VCO input band must hold too** (measured: leaving it out gets caught by
        # the closed loop)
        if imin is not None and not (imin <= float(vco_in) <= imax):
            continue
        ratio = tgt * M / src_at_m              # = step_n / q (reduced fraction)
        q, step_n = ratio.denominator, ratio.numerator
        # Bounds for D: VCO = target*D inside the band, and N = step_n*t inside its
        # range
        d_lo = int(-(-Fraction(int(round(vmin))) // tgt)) if vmin else 1
        caps = []
        if vmax:
            caps.append(int(Fraction(int(round(vmax))) // tgt))
        if target_hz > 0:
            n_cap = n_hi + 1 if frac_el is not None else n_hi
            caps.append(int(Fraction(n_cap) * src_at_m // (tgt * M)))
        d_hi = min(caps) if caps else max(d_lo, 1)
        if d_hi < d_lo:
            continue
        pairs = []
        if frac_el is not None:
            # Fractional feedback: the multiplier ratio N + F/div need not be an
            # integer, so D (a product of integer dividers) is enumerated directly and
            # the ratio is split into an integer N and a fractional part F.
            for D in range(max(1, d_lo), d_hi + 1):
                need = tgt * D / vco_in     # required N + F/div (exact Fraction)
                pair = _frac_pair(need, frac_divisor, frac_lo, frac_hi, frac_ok,
                                  forced_frac=pinned_frac, pinned_n=pinned_n)
                if pair is None:
                    continue
                N, F = pair
                vco = vco_in * need
                if vmin and not (vmin <= float(vco) <= vmax):
                    continue
                if vco / tgt != D:
                    continue
                pairs.append((N, F, D, vco))
                if len(pairs) >= _MAX_PAIRS:
                    solutions_cut = True
                    break
        else:
            t_lo = max(1, -(-d_lo // q))
            t_hi = d_hi // q
            for t in range(t_lo, t_hi + 1):
                D = q * t
                N = step_n * t
                if N not in n_set:
                    continue
                if pinned_n is not None and int(N) != int(pinned_n):
                    continue
                vco = vco_in * N
                if vmin and not (vmin <= float(vco) <= vmax):
                    continue
                if vco / tgt != D:              # exact equality, no float tolerance
                    continue
                pairs.append((N, 0, D, vco))
                if len(pairs) >= _MAX_PAIRS:
                    solutions_cut = True
                    break
        for N, F, D, vco in pairs:
            # **One D may have several splits, and all must be produced.**
            # Measured (W3, F429 targeting SPI1=10 MHz): VCO=160 MHz, D=16 splits into
            # `PLLP=2 AHB=1 APB2=8` (HCLK=80 MHz → `APB1Freq_Value` out of range)
            # or `PLLP=4 AHB=1 APB2=4` (HCLK=40 MHz → legal).
            # **Only CubeMX knows** which one keeps the clock tree intact (`APB1` hangs
            # off HCLK and is not on the chain we walk, so it cannot be computed), so
            # all candidates are handed out and `apply` tries them one by one, picking
            # with CubeMX's tree check (see step 4b in `cmds/apply.py`).
            splits = _decompose_all(D, k_elems, _MAX_SPLITS + 1)
            if len(splits) > _MAX_SPLITS:
                splits_cut = True
                splits = splits[:_MAX_SPLITS]
            for split in splits:
                # **Every stage's readout must hold too**: being right about the
                # target frequency is not enough — the readouts the chain feeds
                # (PLL output, HCLK…) must also be inside the db Min/Max.
                if not _stages_fit(float(vco), n_el, forward, n_idx, split,
                                   stage_cons, specs, bands):
                    continue
                k_steps = []
                bad = False
                for x, v in split:
                    # Composite divider: `v` is the **effective divider**; what gets
                    # written is the divider element's own raw value (the multiplier is
                    # derived by CubeMX from the condition, so it is not written).
                    fold = folded.get(x["element"])
                    write_v = fold[v][0] if fold else v
                    pinned_d = fixed.get(x["element"])
                    if pinned_d is not None and int(write_v) != int(pinned_d):
                        bad = True
                        break
                    # **An element with a single candidate gets no write command**: it
                    # has no degree of freedom, so writing it is either a no-op or
                    # rejected outright. Measured: F1's `HSIDivPLL` is `min=2 max=2` in
                    # the db (HSI fixed ÷2), and `clock set HSIDivPLL 2` prints the
                    # usage table and fails — `clock set` registers a command only when
                    # "there is more than one candidate, or a single value that was not
                    # optimized"; a fixed divider has no setter.
                    # Its value still takes part in the arithmetic (it is already in
                    # `split`); it is just **not written**.
                    if _forced_value(cands_by_el.get(x["element"])) is not None:
                        continue
                    w = _write_value(x, _spec_of(specs, x),
                                     write_v)
                    if w is None:
                        bad = True
                        break
                    k_steps.append((_param_name(x), w))
                if bad:
                    continue
                selector_steps = [(_mux_param_name(tree, element), value)
                                  for element, value in mux_steps]
                steps = selector_steps + k_steps
                # Same for M/N: with a single value in range it is not written either
                # (no degree of freedom)
                if m_lo != m_hi:
                    steps.append((_param_name(m_el), str(M)))
                if n_lo != n_hi:
                    steps.append((_param_name(n_el), str(N)))
                if frac_el is not None and F != 0:
                    # F = 0 is the db default, so it is not written (minimal change);
                    # CubeMX itself writes PLLFRACN=0 in saved files, which only means
                    # our keys stay a subset of the saved ones.
                    steps.append((_param_name({"element": frac_el.id,
                                              "type": "fractional",
                                              "ref_parameter": frac_el.ref_parameter}),
                                  str(F)))
                # The full value map this answer depends on: everything it writes,
                # plus the values it merely relies on (source side, fixed dividers,
                # selectors). Without it a later target could move a divider an
                # earlier target depends on.
                settings = dict(chosen)
                settings.update(pre_values)
                settings[n_el["element"]] = N
                if frac_el is not None:
                    settings[frac_el.id] = F
                if m_el is not None:
                    settings[m_el["element"]] = M
                for x, v in split:
                    # the written value, not the composite effective divider
                    fold = folded.get(x["element"])
                    write_v = fold[v][0] if fold else v
                    settings[x["element"]] = _normalize_setting(
                        x, _spec_of(specs, x), write_v)
                chain_txt = " ".join("%s=%s" % kv for kv in k_steps) or "(no divider)"
                out.append(Solution(
                    steps=steps, expected_hz=target_hz, exact=True,
                    vco_hz=float(vco), selections=dict(chosen),
                    is_current_branch=is_current_branch, settings=settings,
                    note="exact solution: M=%d N=%d, divider chain %s, VCO=%.3f MHz%s"
                         % (M, N, chain_txt, float(vco) / 1e6,
                            "" if is_current_branch
                            else " (switched to %s)" % ", ".join(
                                "%s=%s" % kv for kv in sorted(chosen.items())))))
                if len(out) >= limit * 6:
                    solutions_cut = True
                    break
            if len(out) >= limit * 6:
                break
        if len(out) >= limit * 6:
            break

    m_name = _param_name(m_el) if m_el else None

    def key(s):
        # Without M, `m_name` is None and `next(...)` yields 999 — equivalent to
        # "M does not take part in sorting" (M is always 1).
        m = next((int(v) for e, v in s.steps if e == m_name), 999)
        n = next((int(v) for e, v in s.steps if e == _param_name(n_el)), 999)
        fractional = 0
        if frac_el is not None:
            fractional = int(s.settings.get(frac_el.id) or 0)
        # an exact integer answer is preferred over a fractional one at the same VCO
        return (1 if fractional else 0, fractional, round(s.vco_hz), m, n)

    out.sort(key=key)
    out = out[:limit]
    return {
        "ok": bool(out),
        "truncated": splits_cut or solutions_cut,
        "solutions": out,
        "reason": None if out else (
                  "this branch has no exact solution. The target may not be an "
                  "integer ratio of the source frequency, or the chain may contain a "
                  "fixed divider that was not taken into account."
                  if frac_el is None else
                  "this branch has no exact solution: the multiplier has to be "
                  "N + F/%d, and no (M, D, N, F) inside the db ranges reproduces the "
                  "target (the fractional resolution is 1/%d of the VCO input)."
                  % (frac_divisor, frac_divisor)),
        "fractional": ({"element": frac_el.id, "divisor": frac_divisor,
                         "min": frac_lo, "max": frac_hi}
                        if frac_el is not None else None),
        "source": {"element": src_name, "hz": src_hz},
        "vco_bounds": {"min": vmin, "max": vmax, "parameter": vname,
                       "by_branch": v_by_branch},
        "vco_input_bounds": {"min": imin, "max": imax, "parameter": iname,
                             "by_branch": i_by_branch},
        "pll_segment": [x["element"] for x in unknowns],
        "m_element": _param_name(m_el) if m_el else None,
        "m_element_note": (None if m_el else
                           "this chain has **no input divider** (M = 1) — the "
                           "upstream element is a pass-through readout, not a divider."),
        "n_element": _param_name(n_el),
        # Which elements get **written** this time (tree ids, used to compute the blast
        # radius; they are not necessarily the command names in steps, see
        # `_cli_name`)
        "written_elements": ([el for el, _ in mux_steps]
                             + [x["element"] for x, _ in k_elems]
                             + [n_el["element"]]
                             + ([m_el["element"]] if m_el else [])),
        "post": [x["element"] for x, _ in k_elems],
        "forward": [x["element"] for x in forward],
        "chosen": chosen,
        "mux_steps": mux_steps,
    }


def _upstream_of_mul(tree, mux_id: str) -> bool:
    """Whether this mux is "shared on the source side" — criterion: a multiplier is
    reachable **without crossing a readout**.

    Used to decide "can this mux be enumerated". The PLL input selection (H750's
    `PLLSource`) feeds `DIVM*`→`DIVN*` (multipliers) directly, so touching it
    changes every PLL on the same source (measured: after switching `PLLSource` to
    HSI, the PLL2/PLL3 VCO inputs went out of range).

    **"Without crossing a readout" is required**: buses also carry multiplier
    elements (timer kernel clock ×2, the W5 kind), and those are **below readouts**.
    An earlier implementation ignored this, so L4's `SysClkSource` was misjudged as
    source-side (through `SysCLKOutput` its downstream reaches `TimPrescaler*`), and
    the "switch to PLL" branch was never enumerated → L4's SPI1 target could never be
    solved (measured in W6).
    """
    stack, seen = [mux_id], set()
    while stack:
        cur = stack.pop()
        for el in tree.elements.values():
            if el.id in seen:
                continue
            if not any((i.get("from") or "") == cur for i in el.inputs):
                continue
            seen.add(el.id)
            if el.type in _MUL_TYPES:
                return True
            if el.type in ("output", "activeOutput"):
                continue                    # below a readout is another clock domain, no
                                            # longer source-side
            if el.type == "multiplexor" or el.type in _DIV_TYPES:
                stack.append(el.id)
    return False


def _branches(tree, start_id: str, current: dict | None, allowed: set,
              limit: int = _MAX_BRANCHES) -> list:
    """Enumerate branches: switch the muxes in `allowed` to other inputs and rebuild
    the chain for each.

    `allowed` comes from the caller (**only muxes after the PLL**, see `solve`).
    Returns ``[(picks, forward, chosen)]``; the first entry is always "keep the
    current state".
    """
    out, queue, seen = [], [({}, 0)], set()
    depth_cut = False
    while queue and len(out) < limit:
        picks, depth = queue.pop(0)
        fwd, chosen = _walk(tree, start_id, picks, current)
        if fwd is None:
            continue
        key = tuple(sorted(chosen.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append((picks, fwd, chosen))
        if depth >= _MAX_BRANCH_DEPTH:
            if any(x["element"] in allowed and x["type"] == "multiplexor"
                   and len(tree.get(x["element"]).inputs) > 1
                   for x in fwd if tree.get(x["element"]) is not None):
                depth_cut = True
            continue
        for x in fwd:
            if x["element"] not in allowed or x["type"] != "multiplexor":
                continue
            el = tree.get(x["element"])
            cur_val = chosen.get(x["element"])
            for inp in (el.inputs if el else []):
                rv = inp.get("ref_value")
                if not rv or rv == cur_val:
                    continue
                np = dict(picks)
                np[x["element"]] = rv
                queue.append((np, depth + 1))
    # queue still holding work, a depth cut, or the list size cap all mean the
    # branch space was not enumerated completely
    return out, bool(queue) or depth_cut or (len(out) >= limit)


def _solve_impl(machine, refname: str, parameter: str, target_hz: float,
                limit: int = 8, current: dict | None = None,
                params: dict | None = None,
                fixed: dict | None = None,
                search_budget: int | None = None) -> dict:
    """Given an output-item parameter (e.g. `SPI123Freq_Value`) and a target
    frequency, solve for the upstream values.

    :param current: ``{element name: current value}`` for the muxes. **When omitted,
        the "chain-head input" is assumed**, and the result says so ("this will move
        other PLLs sharing that source").
    :param fixed: ``{element name: settled value}``. Already-solved M/N/output
        dividers are **no longer re-enumerated**; only the stages still open are
        solved (used for multi-target solving; omitting it means everything is free
        and behaviour is unchanged).
    :param params: ``{parameter name: current value}`` (the full RCC values from one
        `config saveext`), used to **judge `refEnable`**: when a readout constraint
        hangs on an element with an enable, whether it is collected depends on "is
        that enable really on now" (see `_enabled_now`).
        Omitted = treat everything as enabled (behaviour falls back to the old
        version, never worse).

    Flow: walk the chain once from the current state → recognize "the muxes after the
    PLL" on that chain → enumerate branches of those muxes and solve each → merge
    (**current branch first**).
    """
    from .clockxml import ClockTree, find_clock_xml

    allt = targets(machine, refname)
    item = next((x for x in allt["targets"] if x["parameter"] == parameter), None)
    if item is None:
        raise CubemxError(
            "the clock tree has no output item %s. Available: %s"
            % (parameter, ", ".join(x["parameter"] for x in allt["targets"][:12])))

    model = {x["element"]: x for x in value_model(machine, refname)["elements"]}
    specs = _specs(machine, refname)
    tree = ClockTree(find_clock_xml(machine, refname))

    # A target outside the window the db declares is not a configuration we deliver:
    # no search is needed to know that, and answering "no solution" keeps the number
    # honest instead of writing an overclocked configuration.
    target_spec = spec_for(specs, parameter)[0]
    low, high = target_spec.get("min"), target_spec.get("max")
    declared = []
    try:
        if high is not None and float(target_hz) > float(high):
            declared.append("above the declared maximum %.0f Hz of %s"
                            % (float(high), parameter))
        if low is not None and float(low) > 0 and float(target_hz) < float(low):
            declared.append("below the declared minimum %.0f Hz of %s"
                            % (float(low), parameter))
    except (TypeError, ValueError):
        pass
    if declared:
        return {
            "ok": False, "status": "exhausted", "parameter": parameter,
            "target_hz": target_hz, "solutions": [], "branches_tried": [],
            "limits": [{"parameter": parameter, "min": low, "max": high}],
            "reason": "the target is %s" % " and ".join(declared),
            "note": "the target is outside the window declared in the db, so it is "
                    "refused instead of writing an out-of-range configuration.",
        }

    # Walk the current state once: this yields both "the current branch" and "which
    # muxes can be enumerated".
    #
    # Criterion (changed 2026-09-27, fixed along with W5): **exclude muxes shared on
    # the source side**; every other mux on the chain can be enumerated. Shared on the
    # source side means **a multiplier is reachable downstream** — that is the PLL
    # input selection (H750's `PLLSource` is shared by three PLLs; touching it changes
    # the PLL2/PLL3 inputs, measured).
    #
    # Why the old "muxes after the first unknown" criterion cannot be used: when the
    # target chain's current branch **has no PLL segment** (e.g. the system clock runs
    # from HSI), the first unknown may be a trailing multiplier (W5's `Tim1Mul`), so
    # there is no mux "after" it → the "switch to the PLL" branch is never enumerated →
    # timer-like targets can never be solved (measured: `Tim1OutputFreq_Value` yielded
    # only the current branch).
    base_fwd, base_chosen = _walk(tree, item["element"], {}, current)
    allowed = {x["element"] for x in base_fwd
               if x["type"] == "multiplexor"
               and not _upstream_of_mul(tree, x["element"])}
    if not allowed:
        # Fallback: when nothing can be enumerated, at least let the stage closest to
        # the target move (old behaviour)
        muxes = [x["element"] for x in base_fwd if x["type"] == "multiplexor"]
        allowed = set(muxes[-1:])

    branches, branches_cut = _branches(tree, item["element"], current, allowed)
    results, tried, branch_results = [], [], []
    for picks, fwd, chosen in branches:
        # a selector pinned by the caller (explicit value or an earlier target) is a
        # constraint: a branch that uses another input is not a candidate
        pinned_mux = {element: value for element, value in chosen.items()
                      if element in (fixed or {})
                      and str(fixed[element]).lower() != str(value).lower()}
        if pinned_mux:
            branch_results.append({
                "ok": False, "status": "fixed_conflict", "truncated": False,
                "reason": "selector %s is fixed to %r, not %r"
                          % (", ".join(sorted(pinned_mux)),
                             ", ".join(str(value) for value in pinned_mux.values()),
                             ", ".join(str(chosen[element]) for element in pinned_mux)),
                "forward": [x["element"] for x in fwd]})
            tried.append({
                "branch": chosen,
                "forward": [x["element"] for x in fwd],
                "ok": False,
                "status": "fixed_conflict",
                "reason": "the selector is fixed to another value",
            })
            continue
        r = _solve_chain(tree, specs, model, fwd, chosen, target_hz, limit,
                         current, params, fixed, search_budget)
        r["picks"] = picks
        branch_results.append(r)
        tried.append({
            "branch": chosen,
            "forward": [x["element"] for x in fwd],
            "ok": r["ok"],
            "status": r.get("status"),
            "reason": r["reason"],
        })
        if r["ok"]:
            results.append(r)

    cut = branches_cut or any(r.get("truncated") for r in branch_results)
    if not results:
        return {
            "ok": False, "truncated": cut,
            "status": "truncated" if cut else _aggregate_status(branch_results),
            "parameter": parameter, "target_hz": target_hz,
            "solutions": [], "branches_tried": tried,
            "allowed_muxes": sorted(allowed),
            "note": "no branch produced an exact solution. See `branches_tried` for the "
                    "per-branch conclusions.",
        }

    # Merge: **current branch first** (change as little as possible), then lower VCO
    # within a branch, then smaller M and N.
    # But **do not let the current branch take every slot** — otherwise the user never
    # sees that "another PLL also works". So the merge rotates across branches: take
    # each branch's best first (current branch first), then the second best, …
    def _mn(r, s):
        m = next((int(v) for e, v in s.steps if e == r["m_element"]), 999)
        n = next((int(v) for e, v in s.steps if e == r["n_element"]), 999)
        return (m, n)

    by_branch = []
    for r in results:
        rs = sorted(r["solutions"], key=lambda s: (round(s.vco_hz), _mn(r, s)))
        by_branch.append((r, rs))
    by_branch.sort(key=lambda p: 0 if p[1][0].is_current_branch else 1)

    merged, i = [], 0
    while len(merged) < limit and any(len(rs) > i for _, rs in by_branch):
        for r, rs in by_branch:
            if len(merged) >= limit:
                break
            if len(rs) > i:
                merged.append((r, rs[i]))
        i += 1
    best = merged[0][0]

    # **Shared elements are reported separately.** Criterion: how many downstream
    # readouts changing this element affects. See `_downstream_readouts` (F429's SPI1
    # can only use the bus, so the main frequency moves).
    #
    # [measured] Two kinds must be told apart:
    #   H750 `DIVM2`/`DIVN2` affect 26 readouts and `DIVP2` 12 — **a PLL's VCO is
    #   shared by nature**, so changing M/N moves the same PLL's other outputs; that is
    #   the inherent cost of "back-solving a PLL" and a fixed note is enough;
    #   F429 `AHBPrescaler` 9 and `SysClkSource` 11 — these are **not** what the user
    #   asked to change; they are the bus dividers / main clock source the solver moved
    #   to hit the target, and they must be warned about prominently.
    # So: `write_impact` is reported in full (how many readouts each written element
    # affects), while "⚠" is attached only to shared elements **outside the PLL
    # segment**.
    impact = []
    for el in merged[0][0]["written_elements"]:
        el_obj = tree.get(el)
        hits = _downstream_readouts(tree, el)
        impact.append({
            "id": el,
            # What the user sees is the **CLI name** (what they would type), not the
            # tree id
            "element": _param_name({"element": el, "type": el_obj.type,
                                  "ref_parameter": el_obj.ref_parameter})
            if el_obj is not None else el,
            "readouts": len(hits), "affects": sorted(hits)[:8]})
    pll_set = set(best["pll_segment"])
    wide = [x for x in impact if x["readouts"] > 1 and x["id"] not in pll_set]
    pll_wide = [x for x in impact
                if x["readouts"] > 1 and x["id"] in pll_set]

    note = ("The solution is derived from the db value domains and VCO bands, with no "
            "per-family branch. **Each branch's mux selection and multiply/divide "
            "values are produced together** (mux inputs identify elements through the "
            "`from` attribute, see the module docstring). "
            "Ordering: current branch first (change as little as possible), then "
            "lowest VCO within a branch (power), then smaller M and N — a fixed "
            "preference that does not vary per family. "
            "%d of %d branches yielded an exact solution and the list rotates across "
            "branches, so the 'switch to another PLL' alternatives stay visible. "
            "Muxes enumerated: %s. Source-side muxes are **not enumerated** (H750's "
            "`PLLSource` is shared by three PLLs; touching it drags PLL2/PLL3 along)."
            % (len(branches), len(results),
               ", ".join(sorted(allowed)) or "none"))
    if wide:
        note = ("⚠ **this solution changes shared elements**: %s. Each affects several "
                "downstream clock readouts (not just the target), so every peripheral on "
                "the same clock/bus moves too.\n"
                % ", ".join("%s (%d readouts)" % (w["element"], w["readouts"])
                            for w in wide)) + note
    if pll_wide:
        note += ("\n(a PLL's VCO is shared by nature: the changed %s also moves the same "
                 "PLL's other outputs, reaching %s downstream readouts — the inherent "
                 "cost of 'back-solving a PLL'; see `write_impact` for details.)"
                 % ("、".join(x["element"] for x in pll_wide),
                    max(x["readouts"] for x in pll_wide)))

    if not (best["vco_bounds"].get("by_branch", True)
            and best["vco_input_bounds"].get("by_branch", True)):
        note += ("\n(this branch could not take its own PLL's band and fell back to "
                 "'the first one that exists': %s / %s — those numbers may not belong "
                 "to this branch; watch out when switching families.)"
                 % (best["vco_bounds"]["parameter"],
                    best["vco_input_bounds"]["parameter"]))

    return {
        "ok": True,
        "status": "solved",
        "truncated": cut or any(r.get("truncated") for r in results),
        "parameter": parameter,
        "target_hz": target_hz,
        "source": best["source"],
        "vco_bounds": best["vco_bounds"],
        "vco_input_bounds": best["vco_input_bounds"],
        "pll_segment": best["pll_segment"],
        "post": best["post"],
        "allowed_muxes": sorted(allowed),
        "branch_count": len(branches),
        "solved_branch_count": len(results),
        "wide_changes": wide,
        "write_impact": impact,
        "solutions": [s.as_dict() for _, s in merged],
        "branches_tried": tried,
        "note": note,
    }


def forward_from_rcc(machine, refname: str, parameter: str, values: dict) -> dict:
    """Recompute one clock reading from the RCC values that would be written.

    This is the **independent** side of the acceptance: it does not read any
    solver-made `expected_hz`. `values` is ``{RCC parameter name: value}`` (the keys
    written into the .ioc, plus the values the caller keeps explicit); the path is
    walked from the device's own clock tree, selectors take the value they would
    have after loading (given value, else the db default), and the arithmetic is the
    same multiplication/division the hardware performs.

    Returns ``{"hz": float | None, "reason": str | None, "path": [...],
    "source_hz": float}``; `hz` is None when the value cannot be established without
    guessing (a selector whose resolved value is not one of its inputs, an unreadable
    source, a non-zero fractional contribution that is not modeled yet).
    """
    from .clockvalues import value_model
    from .clockxml import ClockTree, find_clock_xml

    allt = targets(machine, refname)
    item = next((x for x in allt["targets"] if x["parameter"] == parameter), None)
    if item is None:
        return {"hz": None, "reason": "the clock tree has no output item %s" % parameter}
    model = {x["element"]: x for x in value_model(machine, refname)["elements"]}
    specs = _specs(machine, refname)
    tree = ClockTree(find_clock_xml(machine, refname))

    by_ref = {}
    for element, info in model.items():
        for name in str(info.get("ref_parameter") or "").split(","):
            if name.strip():
                by_ref.setdefault(name.strip().lower(), element)
    element_values = {}
    for name, value in (values or {}).items():
        element = by_ref.get(str(name).lower())
        if element is not None:
            element_values[element] = value
    for element, info in model.items():
        element_values.setdefault(element, info.get("default"))
    flat = {}
    for element, info in model.items():
        for name in str(info.get("ref_parameter") or "").split(","):
            if name.strip() and element in element_values:
                flat.setdefault(name.strip(), element_values[element])
    selectors = {element: value for element, value in element_values.items()
                 if (model.get(element) or {}).get("type") == "multiplexor"}
    forward, chosen = _walk(tree, item["element"], {}, selectors)
    if not forward:
        return {"hz": None, "reason": "the selector values do not form a path"}
    for element, value in (chosen or {}).items():
        given = element_values.get(element)
        if given is not None and str(given).lower() != str(value).lower():
            return {"hz": None,
                    "reason": "selector %s would load as %r, not as %r"
                              % (element, given, value)}
    hz, source_hz = None, None
    for x in forward:
        if x["type"] in _SOURCE_TYPES:
            got = _source_hz(tree, specs, x, selectors, flat)
            if not got:
                return {"hz": None,
                        "reason": "the source frequency of %s cannot be read"
                                  % x["element"]}
            hz, source_hz = Fraction(int(round(got))), got
            continue
        if hz is None:
            return {"hz": None, "reason": "the path starts without a source"}
        if x["type"] == "fractional":
            sp = _spec_of(specs, x)
            if _numeric(element_values.get(x["element"]), sp) not in (0, None):
                return {"hz": None,
                        "reason": "the fractional contribution of %s is not modeled"
                                  % x["element"]}
            continue
        if x["type"] not in _DIV_TYPES + _MUL_TYPES:
            continue
        sp = _spec_of(specs, x)
        factor = (_mul_factor(tree, specs, x, element_values.get(x["element"]),
                              overrides=element_values)
                  if x["type"] in _MUL_TYPES else
                  _factor_of(x, sp, element_values.get(x["element"])))
        if factor is None:
            return {"hz": None,
                    "reason": "no numeric value for %s" % x["element"]}
        hz = hz * _frac(factor)
    return {"hz": float(hz) if hz is not None else None, "reason": None,
            "path": [x["element"] for x in forward], "source_hz": source_hz,
            "selectors": chosen}


def _aggregate_status(results: list) -> str:
    """One status for the whole parameter, without overstating what was proven."""
    seen = [str(result.get("status") or "") for result in results]
    for status in ("truncated", "unsupported", "missing_source"):
        if status in seen:
            return status
    return "exhausted"


def _fixed_names(machine, refname: str, fixed: dict) -> list:
    """The user-facing parameter names of the fixed values (refParameter names)."""
    model = {x["element"]: x for x in value_model(machine, refname)["elements"]}
    names = []
    for element, value in sorted(fixed.items()):
        ref = str((model.get(element) or {}).get("ref_parameter") or element)
        names.append("%s=%s" % ((ref.split(",")[0].strip() or element), value))
    return names


def solve(machine, refname: str, parameter: str, target_hz: float,
          limit: int = 8, current: dict | None = None,
          params: dict | None = None,
          fixed: dict | None = None,
          search_budget: int | None = None) -> dict:
    """Solve one output parameter; `_solve_impl` does the search itself.

    ``fixed`` is a constraint, not a preference. When it makes the target
    unsolvable, the same target is solved again without it: if that works, the
    answer names the fixed value that is the conflict, so the caller can tell "this
    value cannot be combined with this frequency" apart from "the clock tree cannot
    do it".
    """
    result = _solve_impl(machine, refname, parameter, target_hz, limit, current,
                         params, fixed, search_budget)
    if result.get("ok") or not fixed:
        return result
    relaxed = _solve_impl(machine, refname, parameter, target_hz, limit, current,
                          params, None, search_budget)
    if not relaxed.get("ok"):
        return result
    relaxed["ok"] = False
    relaxed["status"] = "fixed_conflict"
    relaxed["solvable_without_fixed"] = True
    relaxed["fixed_conflict"] = _fixed_names(machine, refname, fixed)
    relaxed["note"] = (
        "the fixed parameter(s) %s conflict with this target: without them the same "        "target is solvable, so the constraint is what fails, not the clock tree. "        % ", ".join(relaxed["fixed_conflict"])) + str(relaxed.get("note") or "")
    return relaxed


def apply_solution(client, machine, solution: dict, save_to=None,
                   work_dir=None, parameter: str | None = None,
                   persist: bool = True) -> dict:
    """Write one solution and run the closed loop.

    Order: **set the muxes first (fixing the branch), then the output dividers, and
    only then N and M**.
    Why: with the branch and output dividers settled first and the PLL multiply/divide
    touched last, there is never an intermediate moment where "the PLL output is far
    out of range" (every step goes through `clock set`, which validates, but the right
    order avoids more failures).

    Everything goes through `clock set` (it validates ranges), then:

    1. save + reload (derived frequencies are recomputed only then);
    2. `clockvalues.check_tree` checks the tree with the same constraints CubeMX uses;
    3. **read back the target element's `*_Freq_Value`** and compare with the
       expectation (relative tolerance).
    """
    from .write import apply_mutations
    from .ipxml import project_refname

    steps = solution.get("steps") or []
    if not steps:
        raise CubemxError("this solution has no element to set")

    # **A mux wants an index, not a source name.** Measured: `clock set PLLSource
    # RCC_PLLSOURCE_HSI` → KO "Invalid index range is 0 to 2"; write
    # `clock set PLLSource 0`. The index order is the `<Input>` document order in the
    # clock-tree XML (`ClockTree.multiplexer_index` does this).
    from .clockxml import ClockTree, find_clock_xml

    tree_xml = ClockTree(find_clock_xml(machine, project_refname(client)))
    mutations = []
    for el, v in steps:
        el_obj = tree_xml.get(el)
        if (el_obj is not None and el_obj.type == "multiplexor"
                and not str(v).isdigit()):
            idx = tree_xml.multiplexer_index(el, v)
            if idx is None:
                raise CubemxError(
                    "source %s not found on %s (its candidates are: %s)"
                    % (el, v, ", ".join(x for x in el_obj.ref_values)))
            mutations.append("clock set %s %d" % (el, idx))
        else:
            mutations.append("clock set %s %s" % (el, v))
    # **The tree check reports a delta, so the baseline must be taken before
    # writing.**
    # Measured: the project in the session may **already** be broken (e.g. an earlier
    # experiment switched the PLL source to HSI, putting the PLL2/3 inputs out of
    # range). Reporting only "is it broken now" would charge that to this write.
    #
    # [measured] This used to run `apply_mutations` before taking the baseline — then
    # the baseline equals the post-write state, `new_violations` is **always empty**,
    # and the "0 newly introduced violations" check validated nothing (found while
    # doing W3 on 2026-09-27: F429 clearly introduced an `APB1Freq_Value` violation,
    # yet the interface reported 0). So the order **must not** be changed back.
    try:
        baseline = check_tree(client, machine, work_dir)
    except Exception:
        baseline = {"ok": True, "violations": []}
    base_bad = {v["parameter"] for v in baseline.get("violations", [])}

    if persist:
        saved = apply_mutations(client, mutations, save_to=save_to,
                                work_dir=work_dir)
    else:
        # **For trying solutions** (Q27b, measured 2026-09-30): after `clock set` the
        # derived readouts **are already new inside the session** (measured 1.8E8 →
        # 1.0E8, with no save and no reload), so both the tree check and the read-back
        # can run **in the current session**, with no "save + reload" per solution. The
        # commands written stay in the session: if they pass, that is the final state
        # (`apply`'s last `saveas` stores it); if not, the caller restores a snapshot.
        from .invoke import run as _run
        for cmd in mutations:
            _run(client, cmd, what="try clock solution")
        saved = {"executed": list(mutations), "saved_to": None}

    tree = check_tree(client, machine, work_dir)
    tree["baseline_violations"] = sorted(base_bad)
    tree["new_violations"] = [v for v in tree.get("violations", [])
                              if v["parameter"] not in base_bad]
    tree["ok_before"] = baseline.get("ok")

    # Read back the target frequency. `parameter` lives on the outer result (a single
    # solution does not carry it), so it must either be passed in or taken from the
    # solution — an earlier implementation took only the latter, got None, and skipped
    # the whole read-back (measured).
    parameter = parameter or solution.get("parameter")
    want = solution.get("expected_hz")
    got, err = None, ""
    if parameter:
        try:
            from .invoke import run
            text, _ = run(client, "get ip parameters RCC %s" % parameter,
                          what="read back target frequency")
            # output looks like `RCC: SPI123Freq_Value="10000000"`
            m = re.search(r'=\s*"?([0-9.eE+-]+)"?\s*$',
                          (text or "").strip().splitlines()[-1]
                          if text else "")
            got = float(m.group(1)) if m else None
            if got is None:
                err = "cannot parse: %r" % (text or "")
        except Exception as e:
            err = str(e).replace(chr(10), " ")[:120]

    tol = max(1.0, (want or 0) * 1e-4)          # relative tolerance of 1e-4
    hit = got is not None and want is not None and abs(got - want) <= tol

    return {
        "ok": bool(hit) and not tree["new_violations"],
        "executed": saved["executed"],
        "saved_to": saved["saved_to"],
        "parameter": parameter,
        "expected_hz": want,
        "read_back_hz": got,
        "read_back_ok": bool(hit),
        "read_back_error": err,
        "tree_check": tree,
        "note": ("Everything goes through `clock set` (which validates ranges). "
                 + ("Saved and reloaded, so the read-back is the derived frequency "
                    "recomputed after storing."
                    if persist else
                    "**Trial mode (not stored)**: derived readouts are already new in "
                    "the session, so the tree check and the read-back run in the current "
                    "session; the caller stores everything at the end.")
                 + "The tree check reports **newly introduced** violations (a baseline "
                   "was taken before writing); `baseline_violations` lists the ones "
                   "that existed before this write."),
    }


def _peripheral_branches(r: dict) -> list:
    """The **per-branch conclusions** the peripheral layer must carry (Gitea #153).

    When `solve()` fails, `branches_tried` is the evidence of "how far recognition
    got". This copies it into `tried[i].branches` (keeping only `branch` +
    `reason`).
    It is a function so that it **can be proven**: a probe that replaces it with
    "always return empty" is equivalent to deleting the code that writes `branches`.
    """
    return [{"branch": b.get("branch"), "reason": b.get("reason")}
            for b in (r.get("branches_tried") or [])]


def solve_peripheral(machine, refname: str, peripheral: str, target_hz: float,
                     parameter: str | None = None,
                     current: dict | None = None,
                     params: dict | None = None,
                     fixed: dict | None = None,
                     limit: int = 8) -> dict:
    """Peripheral entry point: given a peripheral name and a target frequency, pick an
    output item automatically and solve it.

    ## How it picks ("can solve" is not enough; "smallest change" matters too)

    A peripheral often has several output items. H750's SPI1 has two:

    - the bus `APB2Freq_Value`: the chain is
      `D2PPRE2 ← HPRE ← D1CPRE ← SysCLKOutput`, touching
      `SysClkSource` / `PLLSource` / `DIVP1` / `D1CPRE` / `HPRE` / `D2PPRE2`
      — **the chip's main frequency and bus dividers**;
    - the dedicated kernel clock `SPI123Freq_Value`: goes through PLL2 and touches
      only the PLL segment.

    [measured] **Since** the long-chain solver landed, both are solvable, so "take the
    first one that solves" would pick the bus item — changing the main frequency for a
    single peripheral is not what the caller wants.
    The criterion uses the number of `wide_changes` the solution reports (shared
    elements that affect several downstream readouts); when equal, the total readout
    count in `write_impact` breaks the tie. **Verified on both families**: H750 now
    lands on the dedicated kernel clock (6 shared elements on the bus vs 1 on the
    dedicated path); F429's SPI1 only ever had the bus item, so the choice is
    unchanged.

    A specific item can also be requested with `parameter`.
    """
    from .clocktargets import for_peripheral

    got = for_peripheral(machine, refname, peripheral)
    cands = ([x for x in got["targets"] if x["parameter"] == parameter]
             if parameter else got["targets"])
    if parameter and not cands:
        raise CubemxError("%s has no output item %s. Available: %s"
                          % (peripheral, parameter,
                             ", ".join(x["parameter"] for x in got["targets"])))
    tried = []
    best = None
    for t in cands:
        r = solve(machine, refname, t["parameter"], target_hz, limit=limit,
                  current=current, params=params, fixed=fixed)
        # ⚠ **On failure, carry "how far recognition got" up too** (Gitea #153):
        # `solve()`'s failures contain `branches_tried`, the **per-branch**
        # conclusions (which chain was tried, why it failed) — exactly the evidence of
        # "how far recognition got". Carrying only a `note` leaves the caller with one
        # sentence ("no branch produced an exact solution") and no way to tell
        # "**shape not recognized**" from "**recognized, just no exact solution**".
        # Measured (L151 / SPI1 @10MHz, reproducible offline): the inner layer actually
        # produced **4** branch conclusions (3 "the PLL segment lacks a divider or
        # multiplier…", 1 "no exact solution found…"), yet the peripheral layer only
        # saw one sentence pointing at `branches_tried` — a key that **does not exist
        # at this layer**.
        _reason = r["note"]
        if not r["ok"] and _reason:
            # The inner sentence points at `branches_tried`, a key **this layer does
            # not have** (it lives in the `branches` below) — retarget it to a name
            # that really exists here.
            _reason = str(_reason).replace("`branches_tried`", "`branches` in this entry")
        entry = {"parameter": t["parameter"], "ok": r["ok"],
                 "reason": None if r["ok"] else _reason}
        if not r["ok"]:
            entry["branches"] = _peripheral_branches(r)
        tried.append(entry)
        if not r["ok"]:
            continue
        # Change size: first the number of "shared elements affecting several
        # readouts", then the total readout count
        key = (len(r.get("wide_changes") or []),
               sum(x.get("readouts", 0) for x in (r.get("write_impact") or [])))
        if best is None or key < best[0]:
            best = (key, r)

    if best is not None:
        r = best[1]
        r["peripheral"] = peripheral
        r["tried"] = tried
        r["chosen_by"] = ("smallest change" if not parameter else "requested output item")
        return r
    return {"ok": False, "peripheral": peripheral, "target_hz": target_hz,
            "solutions": [], "tried": tried,
            # ⚠ The wording must point at a key **that exists at this layer**
            # (Gitea #153): it used to say "see `branches_tried`", a key the
            # peripheral layer **does not have** — it lives in `tried[i].branches`.
            "note": "no output item of this peripheral yields an exact solution. See "
                    "`tried` for the per-item conclusions; each entry's `branches` "
                    "holds the **per-branch** conclusions (which chain was tried, why "
                    "it failed) — use them to tell 'shape not recognized' from "
                    "'recognized but no exact solution'."}
