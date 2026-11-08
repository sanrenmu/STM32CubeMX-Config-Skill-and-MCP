"""The "legal value" model of clock elements (one uniform shape).

## Why this module exists

There are **two ways to write a clock parameter, and they behave differently**
(measured, H750, legal range of `DIVM1` is 1..63):

======================  ==================  ============================
command                 `DIVM1 999`         result
======================  ==================  ============================
`clock set DIVM1 999`    **KO**              rejected, with a message
`set ip parameters`      **OK**              **silently stores `RCC.DIVM1=999`**
======================  ==================  ============================

In other words: **the generic parameter command writes out-of-range values into
the project without complaining**. So "know the legal value domain before
writing" is required, not a nice-to-have.

## Where the legal values come from (two places, joined by refParameter name)

1. **Clock-tree XML** `db/plugins/clock/<family>.xml`
   (located via the `ClockTree` attribute on the device XML root)
   - the full element list and their types (`devisor` / `multiplexor` /
     `fractional` / `activeOutput` / `fixedSource` …)
   - **mux candidate sources**: the **document order of `<Input refValue="...">`
     is the index order** (`clock set <mux> <index>` wants exactly that number)
   - note: **there are no numeric ranges here**

2. **RCC definition XML** `db/mcu/IP/RCC-<version>_Modes.xml`
   (located via `<IP InstanceName="RCC" Name="RCC" Version="...">` in the device XML)
   - **numeric range**: `<RefParameter Name="DIVM1" Type="integer" Min="1" Max="63">`
   - **enumerations**: `<RefParameter Name="PLLSource" Type="list">` + `<PossibleValue>`
   - also carries `Unit` / `Display` / `DefaultValue`

The two are joined by the **`refParameter` name** (the clock tree's
`refParameter="DIVM1"` matches the RCC definition's `Name="DIVM1"`).

## Uniform output shape (how different value domains fit one output)

**One common shell plus a `kind` discriminator**; consumers branch on `kind`:

.. code-block:: json

    {
      "element": "DIVM1",            // element name in the clock tree (clock set uses it)
      "type": "multiplicatorFrac",   // element kind
      "ref_parameter": "DIVM1",      // matching .ioc parameter name
      "kind": "range",               // enum | range | free  ← discriminator
      "min": 1, "max": 63,           // present when kind=range
      "values": null,                // present when kind=enum (candidate list)
      "write_form": "number",        // number | index | name — what to pass on write
      "default": "32",
      "unit": null,
      "writable": true,
      "enable_parameter": null,      // which enable parameter this element needs (null if none)
      "sources": ["RCC-STM32H7_rcc_v1_0_Modes.xml"]
    }

Five `kind` values (**the first two are writable, the last three are not**):

| kind | what it is | how to use |
|---|---|---|
| `enum` | has a candidate list | pick from `values`; muxes use `write_form="index"` (`clock set` wants the index) |
| `range` | has numeric bounds | pass a number inside the range (`DIVM1` 1..63) |
| `fixed` | **fixed source** (HSI/LSE/RC48 …) | not writable; determined by the chip |
| `derived` | **derived readout** (`output`/`activeOutput` type) | **do not write** — it is a computed result; change the upstream; read it to verify |
| `free` | a **truly free input** that neither side constrains | writable, but the bounds are unknown; only `clock set` rejects it |

**The `derived` kind was split out by measurement, not by preference.** At first
every parameter without a db constraint was `free`; on H750, 8 of the 12 `free`
entries turned out to be derived readouts (`TraceFreq_Value` /
`Tim1OutputFreq_Value` / `MCO1PinFreq_Value` …). Three measurements:

- `clock set TraceFreq_Value 1000000` → **KO** (it is not a controllable node);
- `set ip parameters RCC TraceFreq_Value 1000000` → **OK and stored**;
- after `clock set DIVM1 8`, `Tim1OutputFreq_Value` **changed by itself** from
  50 MHz to 12.5 MHz.

So writing a value into a derived readout stores a value the upstream overwrites.
After the split, H750's `free` count dropped from 12 to **1**
(`I2S_CKIN` / `EXTERNAL_CLOCK_VALUE`, the external audio clock frequency, which
is whatever the user enters).

**By the way**: the `derived` kind **is** "the target frequency". You want "SPI1
at 10 MHz"; that 10 MHz is the target, but `SPI1Freq_Value` is the **result** —
it must be reached by changing the upstream (PLL / divider / mux) and cannot be
written. The earlier target-frequency solving (user gives MHz, solver backs out
the upstream) did exactly this.

## The checker (`possible_value`) is a reference, not evidence

A KO from `possible_value ip parameters <ip> <param> <value>` has **three
completely different meanings** (all three `return false` look the same):

1. the IP does not exist;
2. the parameter **is not in use in the current state** (measured: reading it
   returns null);
3. the value really is illegal.

On the CLI all three are the same KO and **cannot be told apart**. Therefore:

- **the db is the source of truth for value domains** (this module); the checker
  is only used for spot checks;
- when the checker says KO you **cannot** conclude "the value is illegal" —
  measured: on F429, `PLLSource=RCC_PLLSOURCE_HSI` was judged KO in one session
  state and allowed when run alone in another session, and that value is in the
  db candidates.
"""

from __future__ import annotations

from .util import truthy

import re
from pathlib import Path

from .clockxml import ClockTree, find_clock_xml
from .errors import CubemxError
from .ipxml import find_ip, modes_xml_path, ref_parameters, spec_for

# Element type → the value form it expects on write
_WRITE_FORM = {
    "multiplexor": "index",     # clock set <mux> <index>
    "fixedSource": "none",      # fixed source: no writable parameter
}
# These types are always numeric (divider / multiplier / fractional)
_NUMERIC_TYPES = {"devisor", "multiplicator", "multiplicatorFrac", "fractional"}


def value_model(machine, refname: str) -> dict:
    """Legal value model of the clock elements of the current chip.

    :returns: ``{"elements": [...], "sources": {...}, "counts": {...}}``
    """
    tree_path = find_clock_xml(machine, refname)
    tree = ClockTree(tree_path)

    rcc_params = {}
    rcc_file = None
    try:
        e = find_ip(machine, refname, "RCC")
        if e is not None:
            p = modes_xml_path(machine, e)
            if p.is_file():
                rcc_params = ref_parameters(p)
                rcc_file = p.name
    except CubemxError:
        pass

    out = []
    for eid, el in sorted(tree.elements.items()):
        ref = el.ref_parameter
        # refParameter may be a comma-separated list (see ipxml.spec_for)
        spec, _refname = spec_for(rcc_params, ref) if ref else (None, None)

        # Mux candidates come from the <Input> document order of the clock-tree XML
        # (that is the index order)
        enum_values = [x["ref_value"] for x in el.inputs if x.get("ref_value")]
        if el.type == "multiplexor" and enum_values:
            kind, values = "enum", enum_values
        elif spec and spec.get("strict"):
            kind, values = "enum", list(spec["list_values"])
        else:
            kind, values = None, []

        lo = hi = None
        if spec:
            lo, hi = spec.get("min"), spec.get("max")
        if kind is None:
            if lo is not None or hi is not None:
                kind = "range"
            elif el.type in _NUMERIC_TYPES and not enum_values:
                kind = "range"        # numeric, but neither side declares a range
            else:
                kind = "free"

        # **Split "no declared constraint" into three cases**, because they behave
        # completely differently (measured):
        #   fixedSource            → fixed source, not writable, constraints meaningless
        #   output / activeOutput  → **derived readout** (clock-tree leaf, a computed result)
        #                            measured: `clock set TraceFreq_Value 1000000` → KO
        #                            (not a controllable node), but `set ip parameters
        #                            RCC` **accepts and stores** it; it recomputes after
        #                            the upstream changes (measured: DIVM1 8 →
        #                            Tim1OutputFreq_Value from 50M to 12.5M)
        #   others + nothing in db → truly free input (e.g. external audio clock)
        if el.type == "fixedSource":
            kind, values = "fixed", []
        elif el.type in ("output", "activeOutput"):
            kind, values = "derived", []

        # ---- fractional PLL metadata (data-driven; see the dev plan) ----
        # A "fractional" element may declare its divisor on the tree as "PARAM/DIV";
        # without that, the device definition's fixed default 8192 applies. A
        # "multiplicatorFrac" may declare "N/LIST": the second name is the parameter
        # listing which N values may carry a fraction.
        frac_param = frac_divisor = frac_list_param = frac_element = None
        if el.type == "fractional" and ref:
            if "/" in ref:
                frac_param, _sep, _div = ref.partition("/")
                try:
                    frac_divisor = int(_div)
                except (TypeError, ValueError):
                    frac_divisor = None
            else:
                frac_param, frac_divisor = ref, 8192
        elif el.type == "multiplicatorFrac" and ref:
            main_ref = ref
            if "/" in ref:
                main_ref, _sep, frac_list_param = ref.partition("/")
            for conn in el.inputs:
                sub = tree.get(conn.get("from") or "")
                if sub is not None and sub.type == "fractional":
                    frac_element = sub.id
                    break
            frac_param = main_ref

        item = {
            "element": eid,
            "type": el.type,
            "ref_parameter": ref,
            "kind": kind,
            "frac_param": frac_param,
            "frac_divisor": frac_divisor,
            "frac_list_param": frac_list_param,
            "frac_element": frac_element,
            "min": _num(lo),
            "max": _num(hi),
            "values": values or None,
            "write_form": _WRITE_FORM.get(el.type, "number"),
            "default": (spec or {}).get("default"),
            "unit": (spec or {}).get("unit"),
            "writable": kind not in ("fixed", "derived"),
            "enable_parameter": getattr(el, "ref_enable", None),
            "upstream": ([x.get("from") for x in el.inputs if x.get("from")]
                         if kind == "derived" else None),
            "sources": [x for x in (tree_path.name,
                                    rcc_file if (spec and rcc_file) else None) if x],
        }
        out.append(item)

    kinds = {}
    for x in out:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    return {
        "device": refname,
        "clock_tree_file": str(tree_path),
        "rcc_file": rcc_file,
        "counts": kinds,
        "elements": out,
        "note": "Legal values come from two places: the clock-tree XML (element "
                "list + mux candidate sources; the `<Input>` document order is the "
                "index `clock set` wants) and the RCC definition XML (numeric Min/Max, "
                "PossibleValue enums, unit). The two are joined by the `refParameter` "
                "name. `kind` is the discriminator: enum looks at values, range at "
                "min/max, free means neither side declares a constraint (bounds "
                "unknown, so only `clock set` can reject it).",
    }


def _num(v):
    """Turn a db number string into int/float; return it unchanged when it is not numeric."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() else f


#: db `Display` expression: `value` followed by one operator and one number.
#: Measured across the whole database (6181 device XMLs), only four forms exist:
#: `value/1000000` (5981), `value+1` (516), `value/1000` (505),
#: `value/10000000` (1).
_DISPLAY_RE = re.compile(r"^value\s*([+\-*/])\s*([0-9]*\.?[0-9]+)$")
_DISPLAY_OPS = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
                "*": lambda a, b: a * b, "/": lambda a, b: a / b}


def parse_display(expr):
    """Compile the db `Display` expression into a small function; None when unparsable.

    ## Why it is needed (the real root cause of "value labelled with the wrong unit")

    In the db, `Unit` is the unit of the **converted** value and the conversion rule
    lives in `Display`. The measured case:

        <RefParameter Name="VCO1OutputFreq_Value" Min="192000000" Max="836000000"
                      Type="double" Display="value/1000000" Unit="MHz" />

    `Min/Max` are in **Hz**, `Display` says "divide by 1e6 when showing", `Unit`
    says "the result is called MHz". **Reading only `Unit` and dropping `Display`**
    labels 131047619 as "MHz" — off by 10⁶ (that is exactly what was reported:
    131047619 labelled MHz).

    When parsing fails this returns **None** and the caller **shows the raw value**
    (and says the conversion could not be parsed) — **never attach a possibly wrong
    unit**.
    """
    if not expr:
        return None
    m = _DISPLAY_RE.match(str(expr).strip())
    if not m:
        return None
    op, num = _DISPLAY_OPS[m.group(1)], float(m.group(2))
    return lambda x: op(x, num)


def display_value(value, expr):
    """Convert a number through `Display`; None when unparsable."""
    if value is None:
        return None
    fn = parse_display(expr)
    if fn is None:
        return None
    try:
        return fn(float(value))
    except (TypeError, ValueError):
        return None


def format_violation(v: dict):
    """Turn one out-of-range item into "number for humans + unit".

    Returns ``(value, min, max, unit text)``.

    ## Why it exists (the root cause of the mislabelled unit)

    The db `Unit` is the unit of the **converted** value and the conversion rule
    lives in `Display` (see `parse_display`). **Reading only unit and dropping
    display** labels 131047619 Hz as "MHz" — off by 10⁶.

    **When `Display` cannot be parsed**: show the raw value **and attach no unit**
    — a possibly wrong unit is worse than none.

    ⚠ **Both the human message of `clock check` and the "clock tree broken" message
    of `apply` go through here**: the rule lives in one place, so a later change
    cannot miss one of the two call sites.
    """
    expr = v.get("display")
    scaled = display_value(v.get("value_hz"), expr)
    if scaled is None:
        return (str(v.get("value")), v.get("min"), v.get("max"),
                "(raw value; the db display conversion could not be parsed)")

    def f(x):
        if x is None:
            return "-"
        y = display_value(x, expr)
        return ("%.6g" % y) if y is not None else str(x)

    return "%.6g" % scaled, f(v.get("min")), f(v.get("max")), (v.get("unit") or "")


def check_value(machine, refname: str, element: str, value) -> dict:
    """Check one value before writing. Returns ``{"ok", "kind", "why", "model"}``.

    This plugs the hole where "the generic parameter command silently accepts
    out-of-range values": ask here before writing, reject illegal values and report
    the legal range together.
    """
    model = value_model(machine, refname)
    # **Both names must be accepted**: the element **id** in the tree, and its
    # **refParameter**. The latter is required because `clock set` addresses
    # **arithmetic elements** (div/mul) by refParameter (measured F429: the tree id
    # is `AHBPrescaler` but `clock set` only knows `AHBCLKDivider` — it shows up in
    # the usage table after PLL/SYSCLK are set; `clock find-clock` also returns the
    # latter). This used to look up by id only, so a name copied from `find-clock`
    # was rejected by the **pre-check** ("the clock tree has no element named
    # AHBCLKDivider"), while the id was rejected by `clock set` — neither worked, so
    # the `clock` section of `apply` was unusable on AHB/APB dividers (measured
    # 2026-09-28).
    el = next((x for x in model["elements"]
               if x["element"] == element
               or element in (x.get("ref_parameter") or "").split(",")), None)
    if el is None:
        return {"ok": False, "kind": None, "model": None,
                "why": "the clock tree has no element named %s" % element}
    if el["kind"] == "fixed":
        return {"ok": False, "kind": "fixed", "model": el,
                "why": "%s is a fixed source (%s) with no writable parameter. "
                       "It is determined by the chip, not a configuration item."
                       % (element, el["ref_parameter"] or "no parameter")}
    if el["kind"] == "derived":
        up = ", ".join(x for x in (el.get("upstream") or []) if x)
        return {"ok": False, "kind": "derived", "model": el,
                "why": "%s is a **derived readout** (%s), not a settable input — it "
                       "is a computed result. Measured: it recomputes after the "
                       "upstream changes (`clock set DIVM1 8` moves "
                       "Tim1OutputFreq_Value from 50MHz to 12.5MHz). %s"
                       "To make it take a value, change the **upstream**; read it "
                       "to verify."
                       % (element, el["ref_parameter"] or "no parameter",
                          ("its upstream is: %s." % up) if up else "")}

    if el["kind"] == "enum":
        values = el["values"] or []
        # Mux: `clock set` wants the index, but a source name is accepted too
        # (the caller converts it)
        if str(value) in [str(v) for v in values]:
            return {"ok": True, "kind": "enum", "model": el, "why": ""}
        if str(value).isdigit() and 0 <= int(value) < len(values):
            return {"ok": True, "kind": "enum", "model": el, "why": ""}
        return {"ok": False, "kind": "enum", "model": el,
                "why": "%s accepts: %s (a mux wants index 0~%d; the source name also works)"
                       % (element, ", ".join(str(v) for v in values),
                          len(values) - 1)}

    if el["kind"] == "range":
        lo, hi = el["min"], el["max"]
        if lo is None and hi is None:
            return {"ok": True, "kind": "range", "model": el,
                    "why": "%s has no declared range in the db, so it cannot be "
                           "pre-checked" % element}
        try:
            num = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "kind": "range", "model": el,
                    "why": "%s expects a number, got %r" % (element, value)}
        if (lo is not None and num < float(lo)) or \
           (hi is not None and num > float(hi)):
            return {"ok": False, "kind": "range", "model": el,
                    "why": "%s out of range: legal %s~%s%s, got %s"
                           % (element, lo, hi,
                              (" " + el["unit"]) if el["unit"] else "", value)}
        return {"ok": True, "kind": "range", "model": el, "why": ""}

    return {"ok": True, "kind": "free", "model": el,
            "why": "%s has neither a candidate list nor a range in the db, so it "
                   "**cannot be pre-checked**; on write only `clock set` can reject "
                   "it (it does reject out-of-range values); read it back after "
                   "writing" % element}


def _element_enables(machine, refname: str) -> dict:
    """``{refParameter: [enable parameter names, ...]}``, from the element `refEnable`.

    One refParameter may belong to several elements; the union is taken (recorded
    when any element carries an enable).
    """
    from .clockxml import ClockTree, find_clock_xml

    out = {}
    try:
        tree = ClockTree(find_clock_xml(machine, refname))
    except Exception:
        return out

    def add(param, enable):
        if not (param and enable):
            return
        names = [x.strip() for x in enable.split(",") if x.strip()]
        if not names:
            return
        out.setdefault(param, [])
        for n in names:
            if n not in out[param]:
                out[param].append(n)

    for el in tree.elements.values():
        # The box's own parameters
        for _n in [x.strip() for x in (el.ref_parameter or '').split(',') if x.strip()]:
            add(_n, el.ref_enable)
        # **The signal readouts it produces are gated by it too.** In the clock-tree
        # XML, `<Signal id=.. refParameter=..>` is a readout on a connection (e.g.
        # F429's `VCOSAIOutputFreq_Value`) and carries no refEnable of its own; it is
        # gated by the enable of the element that produces it.
        if not el.ref_enable:
            continue
        for sig_id, ref in tree.signals.items():
            # does this element output that signal
            if ref and sig_id in _outputs_of(tree, el.id):
                add(ref, el.ref_enable)
    return out


def _outputs_of(tree, element_id: str) -> set:
    """All signalIds output by one element (derived from the `<Input from=...>` of
    its downstream elements).

    The clock-tree XML has `<Output signalId=.. to=..>` inside `<Element>`, but the
    parser keeps only inputs. This infers from the downstream Input:
    `<Input signalId=S from=E>` means E outputs S. Good enough, and it does not
    require changing the parser structure.
    """
    out = set()
    for el in tree.elements.values():
        for inp in el.inputs:
            if inp.get("from") == element_id and inp.get("signal"):
                out.add(inp["signal"])
    return out


def check_tree(client, machine, work_dir=None) -> dict:
    """**The closed-loop criterion**: read back every RCC parameter, compare against
    the db-declared ranges, and report out-of-range items.

    ## Why this criterion (measured)

    CubeMX keeps an internal "can this clock be used to generate code" state; the
    criterion is: **walk the connected elements and record an error element
    whenever an output frequency violates its constraints**.

    At generation time CubeMX turns that state into a warning:
    "These peripherals still have some not configured or wrong parameter values: [Clock]
     Do you still want to generate code?" — **and it is a modal dialog**, which on
    the CLI simply hangs the command line (hit twice in measurements).

    So the closed loop cannot wait for generation; it must **check the same
    constraints up front**.

    ## How the check works

    One `config saveext` call returns every RCC parameter (defaults included);
    compare each against the db `Min`/`Max`. One command covers everything,
    two orders of magnitude faster than `get ip parameters` per parameter (1 s each).

    ## Note: derived frequencies are only recomputed **after save and reload**

    [measured] Right after writing `DIVM1=999` without reloading,
    `VCO1OutputFreq_Value` still shows the old 200 MHz; only after save and reload
    does it become 400400 Hz (0.4 MHz), and the db declares a 150 MHz minimum for the
    VCO → out of range. **So this check must run after save and reload**, otherwise
    it checks nothing.
    """
    from .fullconfig import dump_full_config
    from .ipxml import find_ip, modes_xml_path, project_refname, ref_parameters

    refname = project_refname(client)
    e = find_ip(machine, refname, "RCC")
    if e is None:
        raise CubemxError("the device XML has no RCC entry; value domains are unavailable")
    spec = ref_parameters(modes_xml_path(machine, e))

    text = dump_full_config(client, work_dir)
    values = {}
    for line in text.splitlines():
        if not line.startswith("RCC."):
            continue
        key, _, val = line.partition("=")
        name = key[len("RCC."):]
        if name.endswith(".Ext"):
            name = name[:-len(".Ext")]
        if name and name not in values:
            values[name] = val.strip()

    # **Parameters on disabled branches are not checked.**
    # Basis: CubeMX's `refreshErronousElementList()` walks only **connected**
    # elements, and "connected" is the element `refEnable` in the data:
    # measured, F429's `VCOSAIOutputFreq_Value` is produced by an element with
    # `refEnable="SAIAEnable,SAIBEnable"`; with SAI disabled its recomputed value
    # (49 MHz) is below the db Min (100 MHz) and CubeMX ignores it; without this
    # filter it would be a false positive.
    # `refEnable` holds **several comma-separated** names; any one true means enabled.
    enabled_params = _element_enables(machine, refname)
    skip_by_enable = set()
    for name in list(values):
        en = enabled_params.get(name)
        if not en:
            continue
        flags = [values.get(x) for x in en]
        if not any(truthy(f) for f in flags):
            skip_by_enable.add(name)

    violations, checked, unchecked, skipped_default = [], 0, [], []
    for name, s in spec.items():
        lo, hi = s.get("min"), s.get("max")
        if lo is None and hi is None:
            continue
        raw = values.get(name)
        if raw is None:
            unchecked.append(name)      # value not read; do not guess
            continue
        # **A value still at the db default means "this branch is not configured"
        # and does not take part in the criterion.**
        # Why this filter is required: measured on a clean project,
        # `PLLDSIVCOFreq_Value = 500000000` while the db declares
        # `Min="1000000000"` — **its default value violates its own Min**, because
        # the DSI branch is not enabled (the element carries
        # `refEnable="EnableHSEDSI"` and `<Condition Expression="(DSIHOST_Exist)"/>`).
        # Without the filter every project would report this false positive.
        # Basis: CubeMX's `refreshErronousElementList()` walks only **connected**
        # elements; disabled branches are not among them.
        if name in skip_by_enable:
            continue
        # ⚠ **A gated element must not take the "equals default → skip" path**
        # (Gitea #184): the intent of that skip is "this branch is not configured",
        # but once an element with `refEnable` **is enabled**, its conditional range
        # **applies** — measured on L476, after enabling the PLL `VCOOutput=32MHz`
        # is below the XML's 64MHz minimum, yet the skip hid it and `clock.check`
        # reported ok=true. Disabled branches are handled by `skip_by_enable` above,
        # so this skip is not needed as a safety net.
        dflt = s.get("default")
        if dflt and not str(dflt).startswith("=") and name not in enabled_params:
            try:
                if abs(float(raw) - float(dflt)) < 1e-9:
                    skipped_default.append(name)
                    continue
            except (TypeError, ValueError):
                pass
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        checked += 1
        if (lo is not None and num < float(lo)) or (hi is not None and num > float(hi)):
            violations.append({
                "parameter": name, "value": raw, "value_hz": num,
                "min": _num(lo), "max": _num(hi), "unit": s.get("unit"),
                # **`display` must be included**: `unit` is the unit of the
                # **converted** value and the conversion rule lives in `display`
                # (see `parse_display`). Carrying unit without display makes the
                # human message off by orders of magnitude.
                "display": s.get("display"),
                "comment": s.get("comment"),
            })

    # **Also check "a source is selected but not enabled"** (mux consistency): it
    # reuses the **same snapshot**, so it costs a few lines, not another
    # `config saveext`.
    src = check_source_selection(client, machine, text=text, work_dir=work_dir)

    return {
        "ok": not violations,
        "device": refname,
        "checked": checked,
        "source_warning_count": src["warning_count"],
        "source_warnings": src["warnings"],
        "source_undecided_count": len(src["undecided"]),
        "skipped_disabled_count": len(skip_by_enable),
        "violation_count": len(violations),
        "violations": violations,
        "unchecked_count": len(unchecked),
        "skipped_default_count": len(skipped_default),
        "skipped_default": skipped_default[:20],
        "note": "The criterion matches CubeMX: an output frequency outside the "
                "db-declared Min/Max counts as broken. CubeMX waits until code "
                "generation and shows a modal dialog (which hangs the CLI); this "
                "checks up front. **It must run after save and reload** — derived "
                "frequencies are recomputed only then. **Values still at the db "
                "default are not checked** — that means the branch is not "
                "configured, and some defaults violate their own Min (measured: the "
                "DSI VCO), so filtering is required to avoid false positives. "
                "**A second check reports 'a source is selected but not enabled'** "
                "(`source_warnings`) — that one is inferred from names (the db does "
                "not declare it), so it is weaker than the frequency criterion.",
    }

# --------------------------------------------------------------------------
# Clock-source consistency: is the source the mux selected actually "off"? (2026-09-30)
# --------------------------------------------------------------------------

def check_source_selection(client, machine, text: str | None = None,
                           work_dir=None) -> dict:
    """**Clock-source consistency**: is the source the mux currently selects actually
    switched off?

    ## Why it exists (a real rework, measured)

    A plan wrote `PLLSource = RCC_PLLSOURCE_HSE` but **did not** put the clock
    controller into "external crystal" mode. Result: `apply` returned ok, the read
    back matched, and the frequency-range criterion in this module reported 0
    violations — while **the generated code contained `RCC_HSE_OFF`**, so the PLL
    never locked on the board. It was only visible after generation, in the
    generated code (that time caught by an assertion the caller had written).

    ## The criterion: half from data, half **inferred from names** (must be stated)

    - **Data-driven part**: the muxes come from `db/plugins/clock/<family>.xml`;
      each `<Input refValue="RCC_PLLSOURCE_HSE" from="HSEOSC"/>` is the declaration
      of "**value ↔ source element**"; the current value is read from
      `RCC.<refParameter>`.
    - **Name-inferred part**: whether the source is on is **not declared in the db**
      (measured: none of the `PLLSource` candidate values in
      `RCC-..._Modes.xml` carries a `<Condition>`; the db **has** an
      `HSEEnabled` token but **nothing uses it** — measured: the CLI does not
      enforce this constraint, only the GUI does). So it is looked up by name: the
      `signalId` of the input declaration (`HSE`) → find `RCC.<signalId>State`
      (`HSEState`); if found and the value ends with `_OFF` (or is false) → report.
    - **When that parameter cannot be found, nothing is reported** (undecidable →
      stay silent) and it is recorded in `undecided`.

    ⚠ **This criterion is weaker than the frequency-range one**: it is inferred from
    names, not declared by data. The wording **says so**, so readers do not take it
    for a hard constraint.

    :param text: an already fetched `config saveext` snapshot; when omitted it is
        fetched here (about 1 second).
    """
    from .clockxml import ClockTree, find_clock_xml
    from .fullconfig import dump_full_config
    from .ipxml import project_refname

    refname = project_refname(client)
    tree = ClockTree(find_clock_xml(machine, refname))
    if text is None:
        text = dump_full_config(client, work_dir)

    values: dict = {}
    for line in text.splitlines():
        if not line.startswith("RCC."):
            continue
        key, _, val = line.partition("=")
        name = key[len("RCC."):]
        if name.endswith(".Ext"):
            name = name[:-len(".Ext")]
        if name and name not in values:
            values[name] = val.strip()

    warnings, undecided, checked, dormant = [], [], 0, 0
    for el in tree.elements.values():
        if el.type != "multiplexor" or not el.ref_parameter:
            continue
        # **Disabled branches are not judged**: the db declares `refEnable` on the
        # element (several comma-separated names; any one true means enabled). This is
        # the same rule that filters disabled branches in `check_tree` — measured,
        # without it there are **false positives**: on a clean project `RTCClkSource`
        # defaults to `RCC_RTCCLKSOURCE_LSE` while `LSEState=RCC_LSE_OFF`, but RTC is
        # not enabled at all and that selection is dormant (CubeMX ignores it too).
        if el.ref_enable:
            en = [x.strip() for x in el.ref_enable.split(",") if x.strip()]
            if not any(truthy(values.get(x)) for x in en):
                dormant += 1
                continue
        # Current value: `refParameter` may be a comma-separated list
        # (`PLLSourceVirtual,PLLSource`); take the first one that reads back
        cur = None
        for nm in el.ref_parameter.split(","):
            nm = nm.strip()
            if nm and values.get(nm):
                cur = values[nm]
                break
        if not cur:
            continue
        inp = next((x for x in el.inputs if x.get("ref_value") == cur), None)
        if inp is None:
            continue                    # value not among the declared inputs → no guessing
        src, sig = inp.get("from"), inp.get("signal")
        if not src or not sig:
            continue
        checked += 1
        state = values.get("%sState" % sig)
        if state is None:
            undecided.append({"element": el.id, "value": cur, "source": src,
                              "why": "the db has no `%sState` parameter, so whether "
                                     "this source is on cannot be decided" % sig})
            continue
        low = str(state).strip().lower()
        if low.endswith("_off") or low in ("false", "0"):
            warnings.append({
                "element": el.id, "value": cur, "source": src,
                "state_parameter": "%sState" % sig, "state": state,
                "message": (
                    "**clock source mismatch**: `%s` selects `%s` (source element "
                    "`%s`), but `RCC.%sState` is `%s` — **that source is off**. "
                    "The generated code will have a PLL expecting a source that is "
                    "switched off, so the PLL will not lock on the board. Either "
                    "enable that source (`peripherals describe RCC` lists its modes; "
                    "usually the external-crystal / bypass mode, which also takes the "
                    "crystal pins), or point `%s` at another source. "
                    "(⚠ this criterion is inferred from names — the db does not "
                    "declare it; the CubeMX GUI blocks it, the command line does not.)"
                    % (el.id, cur, src, sig, state, el.id)),
            })
    return {
        "ok": not warnings,
        "device": refname,
        "checked": checked,
        "skipped_dormant": dormant,
        "warning_count": len(warnings),
        "warnings": warnings,
        "undecided": undecided,
        "note": "**The structure comes from the db** (the mux `<Input refValue=… "
                "from=…>` declares 'value ↔ source element'), **but whether the "
                "source is on is inferred from names** (`<signalId>State`) — the db "
                "does not declare that constraint, so this criterion is weaker than "
                "the frequency-range one. **Disabled branches are not judged** "
                "(filtered by the `refEnable` declared on the element). When the "
                "state parameter is missing nothing is reported (recorded in "
                "`undecided`).",
    }
