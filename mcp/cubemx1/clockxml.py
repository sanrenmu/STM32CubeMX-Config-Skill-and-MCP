"""Reading clock-tree definition files (CubeMX install dir `db/plugins/clock/*.xml`).

## Why this module exists

`clock set <element> <value>` expects **a different value form per element type**.
Measured (tried element by element):

| element type | value to pass | observed result |
|---|---|---|
| `devisor` | a number (and it must be in the allowed set) | enum name → KO with no reason |
| `fractional` | a number (Double) | same as above |
| `multiplexor` | **the index, not the source name** | source name → `Invalid index range is 0 to 2` |
| `activeOutput` (hclk) | a frequency value | derived readout; writing it has no effect |

Measured: `clock set PLLSource RCC_PLLSOURCE_HSI` reports
`Invalid index range is 0 to 2`, while `clock set PLLSource 1` takes effect and
reads back as `RCC_PLLSOURCE_CSI`.

**So where does source name → index come from?** From this XML: the `<Input>`
children of a `multiplexor` element are in document order, and that order is the
GUI radio-button order.

For PLLSource the XML order is HSI(0) / CSI(1) / HSE(2); writing 1 read back CSI —
**an exact match**, so this order is trustworthy.

## How the file is located

The device XML (`db/mcu/<device>.xml`) has a `ClockTree` attribute on its root
element whose value is the clock definition file name:

.. code-block:: xml

    <Mcu ClockTree="STM32H7" ... RefName="STM32H750VBTx">

So the path is: device name → device XML `ClockTree` attribute →
`db/plugins/clock/<that value>.xml`.

That way we never have to guess which family uses which definition file.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CubemxError
from .machine import Machine


class ClockXmlError(CubemxError):
    """Problem with the clock definition file."""


@dataclass
class ClockElement:
    """One element of the clock tree."""

    id: str
    type: str
    ref_parameter: str | None
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    """signalIds of this element's output connections (`<Output signalId=…>`).

    Why it is needed: **the connection itself can be a readout** — `<Signal
    id="PLLCLK" refParameter="PLLCLKFreq_Value"/>`. Judging "after changing this
    stage, what is this line in Hz and is it out of range" requires knowing which
    element drives which line (see `clocksolve`)."""
    """`multiplexor` inputs in document order. Each item is {"signal", "from", "ref_value"}.
    That order is the index order `clock set` expects."""
    ref_enable: str | None = None
    """Name of the enable parameter this element depends on (XML `refEnable`).
    For example `TraceFreq_Value` only exists when `TraceEnable` is set; None otherwise."""

    def index_of(self, ref_value: str) -> int | None:
        """Return the index of a source refValue (e.g. `RCC_PLLSOURCE_HSI`) among the candidates."""
        for i, x in enumerate(self.inputs):
            if x.get("ref_value") == ref_value:
                return i
        return None

    @property
    def ref_values(self) -> list:
        return [x.get("ref_value") for x in self.inputs if x.get("ref_value")]


def find_clock_xml(machine: Machine, refname: str) -> Path:
    """Find the clock definition file for a device name.

    The `ClockTree` attribute on the device XML root is the file name (without extension).
    """
    dev_xml = machine.db / "mcu" / ("%s.xml" % refname)
    if not dev_xml.is_file():
        raise ClockXmlError(
            "device file not found: %s\n"
            "(pass the refname, e.g. STM32H750VBTx, not the order code STM32H750VBT6)"
            % dev_xml)

    # **Parse the root `ClockTree` attribute through XML**, do not regex the first
    # 4000 bytes: attribute values may contain a bare `>`, and comments may hold a
    # `<Mcu … ClockTree="…">`; a regex gets both cases wrong.
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(dev_xml)).getroot()
    except ET.ParseError as ex:
        raise ClockXmlError("cannot parse device file: %s\n  %s" % (dev_xml, ex)) from ex
    tree = root.get("ClockTree")
    if not tree:
        raise ClockXmlError(
            "device file has no ClockTree attribute: %s\n"
            "cannot decide which clock definition to use." % dev_xml)

    clock_xml = machine.db / "plugins" / "clock" / ("%s.xml" % tree)
    if not clock_xml.is_file():
        raise ClockXmlError(
            "the clock definition file the device points to does not exist: %s\n"
            "(the device XML says ClockTree=\"%s\")" % (clock_xml, tree))
    return clock_xml


class ClockTree:
    """One clock definition file."""

    def __init__(self, path: Path):
        self.path = path
        try:
            root = ET.parse(str(path)).getroot()
        except ET.ParseError as e:
            raise ClockXmlError("cannot parse clock definition file: %s\n  %s"
                                % (path, e)) from e

        # **The clock-tree XML has two sections** (measured):
        #   <Element> — the "boxes" on the graph (divider/mux/output…), with
        #               refParameter/refEnable
        #   <Signal>  — the "readouts" on the connections; they also carry
        #               refParameter but **no refEnable**
        # Parsing only Element would leave signal-level readouts such as F429's
        # `VCOSAIOutputFreq_Value` without their enable condition → a disabled
        # branch would be misreported as out of range.
        self.signals: dict = {}
        for sig in root.iter("Signal"):
            sid = sig.get("id")
            ref = sig.get("refParameter")
            if sid and ref:
                self.signals[sid] = ref

        self.elements: dict = {}
        for el in root.iter("Element"):
            eid = el.get("id")
            if not eid:
                continue
            inputs = []
            for inp in el.findall("Input"):
                inputs.append({
                    "signal": inp.get("signalId"),
                    "from": inp.get("from"),
                    "ref_value": inp.get("refValue"),
                })
            outputs = [o.get("signalId") for o in el.findall("Output")
                       if o.get("signalId")]
            self.elements[eid] = ClockElement(
                id=eid,
                type=el.get("type") or "",
                ref_parameter=el.get("refParameter"),
                inputs=inputs,
                outputs=outputs,
                ref_enable=el.get("refEnable"),
            )

    def get(self, element_id: str) -> ClockElement | None:
        return self.elements.get(element_id)

    def multiplexer_index(self, element_id: str, ref_value: str) -> int | None:
        """Given a mux element and a source name, return the index to pass."""
        el = self.elements.get(element_id)
        if el is None:
            return None
        return el.index_of(ref_value)
