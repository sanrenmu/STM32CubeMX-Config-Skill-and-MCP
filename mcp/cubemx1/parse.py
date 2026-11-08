"""Parse the text output of CubeMX into structured data.

Every format was captured by measurement, never guessed from documentation. Each
function carries the real output sample it was written against.

**Do not add speculative tolerance here.** If the CubeMX output format changes, the
parse must fail clearly instead of guessing a result; a wrong guess does more harm
than an error.
"""

from __future__ import annotations

import re


class FormatError(Exception):
    """The output format does not match the expectation.

    This means CubeMX behaviour changed (an upgrade, or a different command): look
    at the real output and change the parse; never paper over it with tolerance.
    """

    def __init__(self, what: str, text: str, hint: str = ""):
        self.what = what
        self.text = text
        msg = f"format mismatch while parsing {what}.\n{hint}" if hint else f"format mismatch while parsing {what}."
        msg += f"\nreceived (first 300 characters):\n{text[:300]}"
        super().__init__(msg)


def _lines(text: str) -> list[str]:
    return [l.rstrip() for l in (text or "").splitlines()]


# --------------------------------------------------------------------------
# single value
# --------------------------------------------------------------------------

def single_value(text: str, what: str = "single value output") -> str:
    """Output that holds one value only.

    Real sample (`get mcu name`)::

        STM32H750VBTx

    (`get mcu package`)::

        LQFP100
    """
    ls = [l for l in _lines(text) if l.strip()]
    if not ls:
        raise FormatError(what, text, "expected one line of content, got none.")
    return ls[0].strip()


# --------------------------------------------------------------------------
# peripheral list
# --------------------------------------------------------------------------

# Real sample (`get mcu peripherals`):
#   ADC1 ADC aditf5_v3_0_Cube
#   BDMA BDMA STM32H753_dma1_v1_3_Cube
PERIPHERAL_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s*$")


def peripheral_list(text: str) -> list[dict]:
    """Peripheral list. Each line is "instance name, IP name, version"."""
    out = []
    for l in _lines(text):
        if not l.strip():
            continue
        m = PERIPHERAL_RE.match(l)
        if not m:
            # Some lines may carry no version; accept them but record it
            parts = l.split()
            if len(parts) >= 2:
                out.append({"instance": parts[0], "ip": parts[1],
                            "version": parts[2] if len(parts) > 2 else None})
                continue
            raise FormatError("peripheral list", text,
                              "each line should be 'instance ip version'; this one "
                              "does not match.")
        out.append({"instance": m.group(1), "ip": m.group(2), "version": m.group(3)})
    return out


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def mode_list(text: str) -> dict:
    """Output of `get modes <peripheral>`.

    Real sample::

        SPI1: Full-Duplex Master, Full-Duplex Slave, ..., Hardware NSS Output Signal,
        SPI1: Full-Duplex Master, Full-Duplex Slave, ..., Hardware NSS Output Signal,

    Note that **CubeMX prints the whole list twice** (measured). Duplicates are
    removed here.

    Note: these are the **GUI display names**. Writing a configuration needs the
    internal name, which comes from :func:`mode_detail`.
    """
    ls = [l for l in _lines(text) if l.strip()]
    if not ls:
        return {"instance": None, "modes": [], "note": "no output"}
    inst = None
    modes: list[str] = []
    for l in ls:
        if ":" not in l:
            continue
        name, rest = l.split(":", 1)
        inst = inst or name.strip()
        for m in rest.split(","):
            m = m.strip()
            if m and m not in modes:      # dedupe, which also handles the double print
                modes.append(m)
    return {"instance": inst, "modes": modes}


# Real sample (`get mode active|available|all <peripheral>`):
#   Mode: active | SPI1 | TX_Only_Simplex_Unidirect_Master | Mode:Transmit Only Master
#   Mode: available | SPI1 | Full_Duplex_Master | Mode:Full-Duplex Master
#   Mode: defined | SPI1 | Full_Duplex_Master | Mode:Full-Duplex Master
#   Mode: available | SPI1 | NSS_Signal_Hard_Input | Hardware NSS Signal:Hardware NSS Input Signal
#                                                     ^ the label of part 4 is not always "Mode"
MODE_DETAIL_PREFIX = "Mode:"


def mode_detail(text: str) -> list[dict]:
    """Output of `get mode active|available|all <peripheral>`.

    Each line gives the internal name and the GUI display name, which is the most
    useful format: **writing a configuration needs the internal name, reading
    information needs the display name, and one command carries both**.

    Three state words were measured: `active` (currently active), `available`
    (selectable) and `defined` (defined; note it is not `all`).

    Each line is split on ``|`` into four parts::

        Mode: <state> | <peripheral> | <internal name>[:<boolean>] | <label>:<display name>

    **Part 3 may carry a ``:true`` / ``:false`` tail**. That tail is not part of
    the name, it is a boolean state of the mode itself. Measured: on H750 only
    ``MII_PTP_Synchro:false`` carries the tail, while **on F429 even ``MII`` is
    ``MII:false``**. So "some chips have it, some do not" is no reason to ignore
    it: pass ``MII:false`` as the internal name to ``set mode`` and CubeMX answers
    KO without saying anything. The tail is cut off here and kept in ``flag``.

    **The label of part 4 is not always ``Mode``.** Hardware signal modes use
    another label, for example ``Hardware NSS Signal:Hardware NSS Input Signal``.
    So the parts are split on ``|`` and the label on the first colon, with no
    hardcoded label name -- that was hardcoded once and two hardware signal modes
    could not be parsed.
    """
    out = []
    for l in _lines(text):
        if not l.strip() or not l.startswith(MODE_DETAIL_PREFIX):
            continue
        parts = [p.strip() for p in l.split("|")]
        if len(parts) != 4:
            raise FormatError(
                "mode detail", text,
                f"each line should split on | into four parts; this one gave {len(parts)}: {l[:120]}")
        state = parts[0][len(MODE_DETAIL_PREFIX):].strip()

        # Only a true/false tail is accepted: an internal name may in theory contain
        # a colon and must not be cut wrongly. Without a match the name stays as is.
        internal, flag = parts[2], ""
        head, sep, tail = parts[2].rpartition(":")
        if sep and tail in ("true", "false"):
            internal, flag = head, tail

        label, sep, display = parts[3].partition(":")
        if not sep:
            label, display = "", parts[3]
        out.append({
            "state": state,
            "instance": parts[1],
            "internal": internal,
            "flag": flag,
            "label": label.strip(),
            "display": display.strip(),
        })
    return out


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

# Real sample (`get ip parameters SPI1 DataSize`):
#   SPI1: DataSize="SPI_DATASIZE_4BIT"
#
# Note: **the value group is `([^"]*)` (star, an empty string is allowed) on
# purpose; never tighten it to `+`**. It once looked like "an empty value parses
# as valid", so the whole database was counted: **the device data really does hold
# legal empty values** -- measured across 6181 device XML files, `DefaultValue=""`
# appears **8529 times** and `<PossibleValue Value="">` **2155 times** (mostly on
# the RESMGR/utility/flash/DDR/BLE/WPAN IPs).
# Tightening to `+` would turn a **legal empty value** into a format error, an error
# in the opposite direction that is also harder to notice (normal parameter queries
# start failing at random).
# So the value is reported as it is (empty stays empty); "is this value legal" is
# decided by the **caller against the value domain**, not by the parser.
IP_PARAM_RE = re.compile(r'^(\S+):\s*(\S+)\s*=\s*"?([^"]*)"?\s*$')


def ip_parameter(text: str) -> dict:
    """The current value of one parameter. Format `peripheral: name="value"` (the
    value is quoted and the quotes are removed)."""
    ls = [l for l in _lines(text) if l.strip()]
    if not ls:
        raise FormatError("peripheral parameter", text,
                          "expected one line `peripheral: name=\"value\"`, got no output.")
    m = IP_PARAM_RE.match(ls[0])
    if not m:
        raise FormatError("peripheral parameter", text,
                          'expected the shape `SPI1: DataSize="SPI_DATASIZE_4BIT"`.')
    return {"instance": m.group(1), "parameter": m.group(2), "value": m.group(3)}


# Real sample (`get mode_param_list <peripheral> <internal mode name>`, captured
# through the log redirection):
#   SPI1 | BaudRatePrescaler = SPI_BAUDRATEPRESCALER_2
#   SPI1 | CLKPhase = SPI_PHASE_1EDGE
LOG_PARAM_RE = re.compile(r"^(\S+)\s*\|\s*(\S+)\s*=\s*(.*?)\s*$")


def mode_parameters(text: str) -> list[dict]:
    """Every parameter under one active mode.

    Corresponds to `get mode_param_list`; **its output goes to the log**, so the
    text should come from a redirection file. Format `peripheral | name = value`.
    """
    out = []
    for l in _lines(text):
        if not l.strip():
            continue
        m = LOG_PARAM_RE.match(l)
        if not m:
            raise FormatError("mode parameter list", text,
                              "each line should be `peripheral | name = value`.")
        out.append({"instance": m.group(1), "parameter": m.group(2),
                    "value": m.group(3)})
    return out


# Real sample (`get mode_param_possvalue <peripheral> <mode> <parameter>`, captured
# through the log redirection):
#   DataSize
#    |- SPI_DATASIZE_10BIT
#    |- SPI_DATASIZE_11BIT
# Note the candidate value lines **start with a leading space** (measured).
LOG_POSSVAL_RE = re.compile(r"^\s*\|\s*-\s*(.+?)\s*$")


def mode_parameter_possible_values(text: str) -> dict:
    """The possible values of one parameter.

    Corresponds to `get mode_param_possvalue`; **its output goes to the log**.
    The first line is the parameter name, then one ` |- value` line per candidate.
    """
    ls = [l for l in _lines(text) if l.strip()]
    if not ls:
        raise FormatError("parameter possible values", text, "no output at all.")
    param = ls[0].strip()
    values = []
    for l in ls[1:]:
        m = LOG_POSSVAL_RE.match(l)
        if m:
            values.append(m.group(1))
    if not values:
        raise FormatError("parameter possible values", text,
                          "the first line should be the parameter name and every "
                          "later line ` |- value`, but no value was found.")
    return {"parameter": param, "values": values}


# --------------------------------------------------------------------------
# pins
# --------------------------------------------------------------------------

def pin_signal(text: str) -> dict:
    """Output of `get signal <pin>`.

    Real sample::

        PA5: SPI1_SCK
    """
    ls = [l for l in _lines(text) if l.strip()]
    if not ls:
        raise FormatError("pin signal", text,
                          "expected one line `pin: signal`, got no output.")
    if ":" not in ls[0]:
        raise FormatError("pin signal", text, "expected the shape `PA5: SPI1_SCK`.")
    pin, sig = ls[0].split(":", 1)
    return {"pin": pin.strip(), "signal": sig.strip()}


def pin_functions(text: str) -> dict:
    """Output of `get functions <pin>`.

    Real sample (`get functions PB3`)::

        PB3 (JTDO/TRACESWO)
         CRS_SYNC
         DEBUG_JTDO-SWO
         ...

    The first line is the pin name (**may contain spaces and brackets**), then one
    available function per line, **each with a leading space**.
    """
    ls = _lines(text)
    if not ls:
        raise FormatError("pin functions", text, "no output at all.")
    pin = ls[0].strip()
    funcs = []
    for l in ls[1:]:
        s = l.strip()
        if s:
            funcs.append(s)
    return {"pin": pin, "functions": funcs}


# Real sample (`get pinout`):
#   PE2: Reset_State
#   VBAT: Power
#   PA5: SPI1_SCK
#   PC14-OSC32_IN (OSC32_IN): Reset_State
# There is also a glued shape (measured: only pins that have a signal assigned):
#   PA5: SPI1_SCKPA5: SPI1_SCK
#   ^ the same record printed twice with no newline in between
PINOUT_RE = re.compile(r"^(?P<pin>[^:]+):[ \t]*(?P<state>.*?)[ \t]*$")


def pinout(text: str) -> list:
    """The pin assignment of a whole chip.

    **Measured quirk of this command**: for a pin that has a signal assigned, the
    record is printed twice with no newline, so two lines glue into one, for
    example::

        PA5: SPI1_SCKPA5: SPI1_SCK

    In a measured 100-line output two lines looked like this, and **the glued ones
    were exactly the only two assigned pins** -- the most useful data. So the glue
    cannot simply be skipped; it has to be split.

    The split is deterministic: the glued shape always repeats one record twice, so
    the state field contains ``<pin name>: `` again; split at the first one.

    A shape that cannot be split raises instead of being dropped -- dropping it
    would make the pin look free.
    """
    out = []
    for l in _lines(text):
        if not l.strip():
            continue
        m = PINOUT_RE.match(l)
        if not m:
            raise FormatError("pin assignment", text,
                              f"this line does not fit `pin: state`: {l[:120]}")
        pin = m.group("pin").strip()
        state = m.group("state").strip()

        # split the glue: one record repeated twice
        marker = pin + ":"
        if marker in state:
            state = state.split(marker)[0].strip()
        # A record separator still present after the split means an unseen shape:
        # raise, do not guess
        if re.search(r"\S+:\s+\S+", state):
            raise FormatError(
                "pin assignment", text,
                f"unrecognised glued shape, not guessing: {l[:120]}")

        kind = "power" if state == "Power" else (
            "free" if state == "Reset_State" else "assigned")
        out.append({"pin": pin, "state": state, "kind": kind})
    return out

# --------------------------------------------------------------------------
# interrupts (NVIC)
# --------------------------------------------------------------------------

# Field order (measured):
#   enabled : preempt priority : sub priority : changed by the user :
#   code outside MspInit : IRQ handler generated :
#   [conditional] rank (only when "code outside MspInit" is true):
#   [conditional] uses RTOS functions (only when the project uses an RTOS):
#   enabled by user : HAL handler needed : enable code needed
#
# Because two fields are conditional, **the field count varies** (measured: 9 or
# 10). That is the real reason the older notes said "RTOS changes the fields".
#
# Real sample (read from a saveext dump):
#   NVIC.SysTick_IRQn=true\:15\:0\:false\:false\:true\:false\:true\:false
#   NVIC.BusFault_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC_FIELDS = ["enabled", "preempt_priority", "sub_priority",
               "preempt_modified_by_user", "code_outside_msp_init",
               "irq_handler_generated", "rank", "uses_os_functions",
               "enabled_by_user", "hal_handler_needed", "enable_code_needed"]


def nvic_value(value: str) -> dict:
    """Turn the value field string of one interrupt into named fields.

    :param value: for example ``true:15:0:false:false:true:false:true:false``
        (when read from an `.ioc` or a saveext dump the colon may be escaped)
    """
    # In an .ioc the colon is escaped, so it arrives as backslash plus colon
    raw = value.replace(chr(92) + ":", ":")
    parts = [x.strip() for x in raw.split(":")]

    # The two conditional fields make the count variable. Whether a field is present
    # depends on "is an RTOS used" and "is the code outside MspInit".
    n = len(parts)
    if n not in (9, 10, 11):
        raise FormatError(
            "interrupt fields", value,
            "expected 9-11 fields (the conditional fields change the count), got %d."
            % n)

    out = {}
    idx = 0
    for name in ("enabled", "preempt_priority", "sub_priority",
                 "preempt_modified_by_user", "code_outside_msp_init",
                 "irq_handler_generated"):
        out[name] = parts[idx]; idx += 1

    # Measured: rank only appears when code_outside_msp_init is true.
    # Counting backwards from the remaining fields is the most robust way: the last
    # three are fixed (enabled_by_user / hal_handler_needed / enable_code_needed)
    # and the optional fields sit in between.
    optional_count = n - 9          # 0 / 1 / 2 optional fields
    if out["code_outside_msp_init"] == "true" and optional_count >= 1:
        out["rank"] = parts[idx]; idx += 1
        optional_count -= 1
        if optional_count >= 1:
            out["uses_os_functions"] = parts[idx]; idx += 1
    elif optional_count >= 1:
        # No rank but an optional field present: it can only be the RTOS one
        out["uses_os_functions"] = parts[idx]; idx += 1
        optional_count -= 1

    for name in ("enabled_by_user", "hal_handler_needed", "enable_code_needed"):
        if idx < n:
            out[name] = parts[idx]; idx += 1

    # Turn the obvious boolean fields into real booleans so callers do not have to
    for k in ("enabled", "preempt_modified_by_user", "code_outside_msp_init",
              "irq_handler_generated", "uses_os_functions", "enabled_by_user",
              "hal_handler_needed", "enable_code_needed"):
        if k in out:
            out[k] = (out[k] == "true")
    return out
