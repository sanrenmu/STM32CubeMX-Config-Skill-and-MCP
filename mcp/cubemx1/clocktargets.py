"""The clock tree's "output items" (sinks): peripheral → target frequency parameter → clock chain.

## What this layer does

The user says "**make SPI1 run at 10 MHz**". To deliver that we must first answer:

1. Which **parameter** carries `SPI1`'s target frequency? (`SPI1Freq_Value`)
2. Which **element** in the clock tree does that parameter belong to? (`SPI1Output`)
3. What is **upstream** of it? (which mux / divider / PLL)
4. How many **alternative paths** exist? (H7's `SPI123CLK` can take APB or a PLL output)
5. Which **constraints** apply to the target? (db Min/Max, plus text like
   "150 MHz max" in `Comment`)

This layer only detects and describes; it does **not solve** — solving is the next step.

## Where the data comes from (fully automatic, no hard-coding)

- **peripheral → frequency parameter**: `<RefParameter ... IP="SPI1,SPI2,...">` in
  the RCC definition XML. [measured] Of 2156 parameters in H750's RCC definition,
  1286 carry `IP=`, covering 83 peripheral names.
- **frequency parameter → clock-tree element**: the element XML `refParameter` attribute
- **upstream chain / multiple paths**: `<Input from="...">` in the element XML
- **constraints**: `Min`/`Max` in the RCC definition XML (text in `Comment` is a
  human-readable note)

## Relation to "target frequency"

In `clockvalues` all these output items are `kind="derived"` — they **cannot be
written directly**; the upstream must change. So this module produces the candidate
list of "which upstream elements to change", and the solver computes the values.
"""

from __future__ import annotations

from .clockxml import ClockTree, find_clock_xml
from .errors import CubemxError
from .ipxml import find_ip, modes_xml_path, ref_parameters

# Element types that count as "output endpoints" (derived readouts)
_OUTPUT_TYPES = ("output", "activeOutput")


def _chain(tree: ClockTree, element_id: str, depth: int = 12) -> list:
    """Walk upstream from an element and return the chain (element names, nearest first).

    `<Input from="...">` in `ClockTree` is the reverse edge. Multiple paths yield
    several edges; this returns one main chain, first input first, and the branch
    points are reported separately.
    """
    out, seen = [], set()

    def walk(eid, d):
        if d <= 0 or eid in seen:
            return
        el = tree.get(eid)
        if el is None:
            return
        seen.add(eid)
        ups = [x.get("from") for x in el.inputs if x.get("from")]
        for up in ups[:1]:          # the main chain follows the first input only
            up_el = tree.get(up)
            if up_el is None:
                continue
            out.append({"element": up, "type": up_el.type,
                        "ref_parameter": up_el.ref_parameter})
            walk(up, d - 1)

    walk(element_id, depth)
    return out


def _branch_points(tree: ClockTree, element_id: str, depth: int = 12) -> list:
    """**Branch points** on the upstream chain (places with several inputs = optional paths).

    These are the muxes that "changing the path" would touch. MX2's
    `--flexible-path` picks one itself; on our side we can only list the options.
    """
    out, seen = [], set()

    def walk(eid, d):
        if d <= 0 or eid in seen:
            return
        el = tree.get(eid)
        if el is None:
            return
        seen.add(eid)
        ups = [x.get("from") for x in el.inputs if x.get("from")]
        if len(ups) > 1:
            out.append({
                "element": eid,
                "type": el.type,
                "ref_parameter": el.ref_parameter,
                "options": [{"from": x.get("from"), "ref_value": x.get("ref_value")}
                            for x in el.inputs],
            })
        for up in ups[:1]:
            walk(up, d - 1)

    walk(element_id, depth)
    return out


def targets(machine, refname: str) -> dict:
    """List every "output item" (usable as a target-frequency endpoint).

    :returns: ``{"device", "count", "targets": [...], "by_peripheral": {...}}``
    """
    _load_ip_index(machine, refname)
    tree_path = find_clock_xml(machine, refname)
    tree = ClockTree(tree_path)

    rcc = {}
    rcc_file = None
    try:
        e = find_ip(machine, refname, "RCC")
        if e is not None:
            p = modes_xml_path(machine, e)
            if p.is_file():
                rcc = ref_parameters(p)
                rcc_file = p.name
    except CubemxError:
        pass

    out = []
    for eid, el in sorted(tree.elements.items()):
        if el.type not in _OUTPUT_TYPES:
            continue
        ref = el.ref_parameter
        spec = rcc.get(ref) if ref else None
        consumers = _consumers_of(rcc, ref)
        item = {
            "element": eid,
            "type": el.type,
            "parameter": ref,
            "consumers": consumers,
            "min": (spec or {}).get("min"),
            "max": (spec or {}).get("max"),
            "unit": (spec or {}).get("unit"),
            "comment": (spec or {}).get("comment"),
            "enable_parameter": el.ref_enable,
            "chain": _chain(tree, eid),
            "branch_points": _branch_points(tree, eid),
        }
        out.append(item)

    by_peripheral = {}
    for item in out:
        for c in item["consumers"]:
            by_peripheral.setdefault(c, []).append(item["parameter"])

    return {
        "device": refname,
        "clock_tree_file": str(tree_path),
        "rcc_file": rcc_file,
        "count": len(out),
        "targets": out,
        "by_peripheral": by_peripheral,
        "note": "Output items are the `output`/`activeOutput` elements of the clock "
                "tree; they are **derived readouts** (computed results) and cannot be "
                "written directly. To make one take a target value, change the upstream "
                "elements on `chain`; `branch_points` are the muxes on the chain "
                "(optional paths; changing the path touches them). Peripheral bindings "
                "come from the `IP=` attribute of the RCC definition XML.",
    }


def _consumers_of(rcc: dict, ref) -> list:
    """Which peripherals consume this frequency parameter (the db `IP=` attribute).

    `ref_parameters` does not carry `IP` yet, so it is read once here; when it
    cannot be read the result is an empty list (no guessing).
    """
    if not ref:
        return []
    return list(_IP_INDEX.get(ref, ()))


# Parameter name → peripheral list. Filled by `_load_ip_index()`.
_IP_INDEX: dict = {}
_IP_INDEX_FOR: str | None = None


def _load_ip_index(machine, refname: str) -> None:
    """Collect the `IP=` parameters of the RCC definition into an index (once)."""
    global _IP_INDEX_FOR
    if _IP_INDEX_FOR == refname:
        return
    _IP_INDEX.clear()
    try:
        e = find_ip(machine, refname, "RCC")
        p = modes_xml_path(machine, e) if e else None
        if p and p.is_file():
            # **Read it through the XML structure**, do not regex the raw text. The
            # earlier version (`<RefParameter\s+([^>]*?)>` + `Name="([^"]+)"`) had two
            # traps: `Name="` has no word boundary (it hits `TabName="…"` first), and
            # a bare `>` inside an attribute value truncates the tag.
            import xml.etree.ElementTree as ET
            for el in ET.parse(str(p)).getroot().iter():
                if el.tag.rsplit("}", 1)[-1] != "RefParameter":
                    continue
                nm, ip = el.get("Name"), el.get("IP")
                if not (nm and ip):
                    continue
                names = [x.strip() for x in ip.split(",") if x.strip()]
                if names:
                    _IP_INDEX.setdefault(nm, [])
                    for x in names:
                        if x not in _IP_INDEX[nm]:
                            _IP_INDEX[nm].append(x)
    except Exception:
        pass
    _IP_INDEX_FOR = refname


def for_peripheral(machine, refname: str, peripheral: str) -> dict:
    """Clock output items of one peripheral (the entry point for "make SPI1 run at 10 MHz").

    Peripheral names are case-insensitive. When nothing matches, the error lists
    similar peripheral names.
    """
    _load_ip_index(machine, refname)
    allt = targets(machine, refname)
    want = peripheral.upper()
    hits = []
    for item in allt["targets"]:
        if any(c.upper() == want for c in item["consumers"]):
            hits.append(item)
    if not hits:
        # **Report the two cases separately**, otherwise it misleads (measured: F429's
        # ADC is the second case):
        #   (a) the peripheral name is not in the db `IP=` at all → wrong name, or the
        #       peripheral does not hang off an RCC frequency parameter
        #   (b) the peripheral has a frequency parameter in the db, but that parameter
        #       is **not in the clock tree** → the endpoint was never drawn into the
        #       tree, so automatic back-solving is impossible (record as a warning)
        db_params = sorted({k for k, v in _IP_INDEX.items()
                            if any(c.upper() == want for c in v)})
        if db_params:
            raise CubemxError(
                ("%s has frequency parameters in the db (%s), but **they are not in "
                 "the clock tree**, so its upstream cannot be back-solved."
                 % (peripheral, ", ".join(db_params[:4]))) + chr(10) +
                ("(the clock-tree XML draws only some endpoints: the tree for %s has "
                 "only %d output items and this peripheral's clock readout is not "
                 "among them. Such cases are recorded as warnings, not failures.)"
                 % (refname, allt["count"])) + chr(10) +
                "for these peripherals the values must be worked out from the manual "
                "or with the GUI.")
        near = sorted({c for cs in allt["by_peripheral"].values() for c in cs
                       if want in c.upper()})[:12]
        raise CubemxError(
            ("the RCC definition of %s has no peripheral %s."
             % (refname, peripheral)) + chr(10) +
            "(the peripheral list comes from the db `IP=` attribute, not from us. %s)"
            % (("similar names: " + ", ".join(near)) if near else
               "none at all — this peripheral probably does not hang directly off an "
               "RCC frequency parameter."))
    return {
        "device": refname,
        "peripheral": peripheral,
        "count": len(hits),
        "targets": hits,
        "note": "This is where a target frequency lands: every `parameter` is a "
                "derived readout and cannot be changed; what must change are the "
                "upstream elements on its `chain`. `branch_points` are the muxes on "
                "the chain (multiple paths).",
    }
