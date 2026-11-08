"""Extract the parameter name lists from the full configuration dump.

## Why it is needed

Peripherals such as RCC and NVIC **have no modes** (`get mode active RCC` returns
nothing, `get mode active NVIC` answers KO), so `get mode_param_list` cannot be
used and there is no way to ask "which parameters does this peripheral have".

Their parameter names can only be read from the output of `config saveext` -- that
command writes the **full parameter set including defaults**, one line per
peripheral:

::

    RCC.IPParameters=ADCFreq_Value,AHB12Freq_Value,...,VCOInput3Freq_Value   (95 entries)
    NVIC.PriorityGroup=NVIC_PRIORITYGROUP_4
    NVIC.SysTick_IRQn=true\\:15\\:0\\:false\\:...

NVIC has no `IPParameters` line (its keys are scattered), so NVIC gets extra
handling here: every `NVIC.xxx=` key name is collected and the interrupt vectors
(`*_IRQn`) are excluded.

## Two cautions

- **The path must be quoted.** The `config` commands parse their arguments, so a
  path with a space would be split. This is the **opposite** of the `>`
  redirection rule (quoting there breaks it).
- This only writes a snapshot to a temporary file; the loaded project is
  **not touched**.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

from .errors import CubemxError
from .invoke import CommandFailed, path_arg, run

IP_PARAMS_RE = re.compile(r"^(\w+)\.IPParameters=(.*)$")

# One `.ioc` key/value line: `<peripheral>.<key>=<value>`
#
# **This regex used to be written out in three files** (nvic / peripherals /
# fullconfig) parsing the same thing. It lives here once; other modules import it.
IOC_KEY_RE = re.compile(r"^(\w+)\.(\w+)=(.*)$")

# An NVIC interrupt line: `NVIC.<name>_IRQn=<colon-separated fields>`
NVIC_IRQ_RE = re.compile(r"^NVIC\.(\w+_IRQn)=(.*)$")

# The scattered NVIC keys (everything that is not an interrupt vector);
# **both key and value** are needed.
#
# Why two groups: this regex has two users and both need the value --
#   - `nvic.list_interrupts` needs `(name, value)` to read PriorityGroup
#   - `fullconfig.ip_parameter_names` needs the name only
# There used to be one group here while nvic used two, so `nvic list-interrupts`
# raised IndexError on any `NVIC.PriorityGroup=...` line.
# Cause: extracting the local definition in nvic into a shared import was done
# without noticing the group count differed -- extracting a shared implementation is
# a **change**, not an equivalent substitution.
NVIC_KEY_RE = re.compile(r"^NVIC\.(?!\w+_IRQn=)(\w+)=(.*)$")

_WORD_RE = re.compile(r"^\w+$")


def ioc_pairs(text: str, instance: str | None = None):
    """Extract `<peripheral>.<key>=<value>` from the full configuration text.

    :param instance: only take this peripheral; without it all are taken.
    :returns: [(peripheral, key, value), ...]
    """
    from .iocc import props

    out = []
    for key, value in props.loads(text or "").pairs():
        inst, sep, rest = key.partition(".")
        if not sep or not rest or "." in rest:
            continue
        if not _WORD_RE.match(inst) or not _WORD_RE.match(rest):
            continue
        if instance is not None and inst != instance:
            continue
        out.append((inst, rest, value))
    return out


# Java Properties escapes as used in `.ioc`. **Build the backslash with chr(92)**,
# never write it out directly (this tool has been bitten by heredocs several times: a
# `\ ` sequence loses one level, the result looks right and never matches).
_BS = chr(92)
_IOC_ESCAPES = {" ": " ", ":": ":", "=": "=", "#": "#", "!": "!", _BS: _BS}


def unescape_ioc_key(key: str) -> str:
    """Undo the escapes in the **key name** of a `.ioc` line.

    `.ioc` uses the Java Properties escape rules (measured), so special characters
    in keys and values are escaped. Two measured cases, both present in one file:

    - **a space in the key**: `PB3\\ (JTDO/TRACESWO).Locked=true` (a pin name with a
      parenthesised role)
    - **a colon in the value**: `NVIC.SysTick_IRQn=true\\:15\\:0\\:false...`
      (the value layer already does this in `parse.nvic_value`: `parse.py:385`)

    Without undoing the key escapes the name does not match the canonical name from
    `get functions`, which shows up as "the pin is locked but reading back says it
    is not" -- the classic "looks like a failure but succeeded".

    The rules are the ones `Properties.load` recognises: `\\ ` space, `\\:` colon,
    `\\=` equals, `\\#` hash, `\\!` bang, `\\\\` backslash.
    """
    from .iocc import props

    return props.unescape_value(key)


def dump_full_config(client, work_dir: Path | None = None) -> str:
    """Export the full configuration including defaults and return the text.

    Uses `config saveext`. The path is quoted; the reason is in the module
    docstring.

    Note: inside an `invoke.cache_queries()` scope this really runs only once. The
    temporary file name differs on every call (so caching by command text never
    hits), but semantically it is the same thing: "a full snapshot of the current
    configuration". `gate_params` in `apply` asks for it once per instance
    (measured: about 1.2 s each), which wasted seconds per round. A **write
    command** clears this snapshot with the scope, so stale state is never read.
    """
    from . import invoke
    return invoke.scoped_memo("fullconfig", lambda: _dump_full_config(client, work_dir))


def _dump_full_config(client, work_dir: Path | None = None) -> str:
    """The layer that really does the work (one unique path, one `saveext`)."""
    d = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    # Mind the parentheses: / and % share precedence and associate to the left;
    # without them this becomes (d / "name") % x
    f = d / ("cubemx1_saveext_%d.ioc" % time.time_ns())
    run(client, 'config saveext %s' % path_arg(f), what="export the full configuration")

    for _ in range(30):
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if text:
                return text
        time.sleep(0.1)
    raise CommandFailed(
        "no file appeared after exporting the full configuration: %s\n"
        "CubeMX reported success but the file did not appear; the path or the "
        "permissions are probably wrong." % f)


def ip_parameter_names(text: str, instance: str) -> list:
    """Return the parameter name list of one peripheral from the full configuration
    text.

    For peripherals without an ``IPParameters`` line, such as NVIC, the scattered
    key names are used instead.

    An empty list means "this peripheral has no parameter record in this
    configuration", which is a valid result, not an error.
    """
    names = []
    for line in text.splitlines():
        m = IP_PARAMS_RE.match(line)
        if m and m.group(1) == instance:
            return [x for x in m.group(2).split(",") if x]

    if instance == "NVIC":
        for line in text.splitlines():
            m = NVIC_KEY_RE.match(line)
            if m:
                names.append(m.group(1))
    return names
