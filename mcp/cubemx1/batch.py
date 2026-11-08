"""Pack a series of CubeMX commands into one script and send it with a single
`script <file>` call (the "fast path").

## Why it exists (measured 2026-09-29)

The interactive path **pays about one second per command**: 20 commands sent one by
one took 20.66 s (1.03 s each), while the per-command time CubeMX itself reports is
often only 0-2 ms. **That second is the polling interval waiting for input, not the
command's own cost.**

**`script <file>`** (pack the commands into a file, send one command) is measured
to be **much faster**: **the same 20 commands in one script took 0.99 s (20.9x)**.
It runs line by line and does not pay that interval.

## Three things to remember (all measured)

1. **A script swallows failures.** The per-line `OK`/`KO` are filtered out by our
   own parser as "late end markers", so from the tool's point of view **no failing
   command can be seen**. Therefore **judge by reading back**, not by OK/KO.
2. **A bare `config load` inside a script hits the `Warning: Load Project` dialog
   and locks the session up.** `config saveas` to an **unrelated file** first, to
   clear the dirty flag (what `write.safe_load` does).
3. **`config load` really round-trips** (measured: after `saveas`, one value in the
   file was **edited by hand** and then `load` read that change back), so
   "save + reload + read back" inside a script **is a meaningful check**, not an
   empty assertion.

## The path must be quoted here

Paths inside a script **need quotes** (use `invoke.path_arg`). Measured: without
quotes the command answers KO, and the KO is filtered out, so it **looks like
success** (this happened once: a fake "33x" was computed).

## It is not a silver bullet

- It saves the **polling**, not the real work of a command (`config load`
  measured between 1.5 and 17 s).
- When something blocks half way (a dialog), the whole block is **one command**, so
  you only learn at the end: **never put a blocking action into a script**
  (for example `project generate`).
- A failure **does not stop the script** (measured: after one failing command the
  rest still runs), so transactionality can only be "roll back afterwards".
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import protocol
from .invoke import path_arg, run

#: Where script files go (one batch at a time, nanosecond names to avoid clashes)
_BATCH_DIR = "cubemx1_batch"


def write_script(work_dir, lines: list) -> Path:
    """Write the commands into one script file (**the path quotes itself**, see the
    module docstring)."""
    d = Path(work_dir) / _BATCH_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / ("b_%d.txt" % time.time_ns())
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def run_script(client, work_dir, lines: list, what: str = "batch commands") -> dict:
    """Send one script. Returns ``{"seconds", "output", "file", "lines"}``.

    **Do not judge success by OK/KO inside `output`**: the parser filters both
    markers out.
    """
    f = write_script(work_dir, lines)
    t0 = time.time()
    got = run(client, "script %s" % path_arg(f), what=what)
    dt = time.time() - t0
    parsed, resp = got if isinstance(got, tuple) else (got, {})
    out = parsed if isinstance(parsed, str) else str(parsed)
    # Note: `output` is **cleaned** (each OK/KO was filtered out as a "late end
    # marker"), while the per-command verdict can only come from the **raw** text,
    # so both are returned.
    return {"seconds": dt, "output": out,
            "raw": resp.get("raw") or out,
            "per_command": parse_script(resp.get("raw") or out, lines),
            "file": str(f), "lines": lines}


def safe_save_and_reload(work_dir, target) -> list:
    """**The "save + reload" lines (including the dirty-flag step)**, for use inside
    a script.

    All three steps are required:
    1. `config saveas <unrelated temporary file>`: **clears the "unsaved changes"
       flag**, otherwise the `load` in step 3 raises `Warning: Load Project` and
       locks the session up (measured);
    2. `config saveas <target>`: really writes the file;
    3. `config load <target>`: reloads it (measured to **really round-trip**).
    """
    d = Path(work_dir) / _BATCH_DIR
    d.mkdir(parents=True, exist_ok=True)
    throwaway = d / ("clear_%d.ioc" % time.time_ns())
    return [
        "config saveas %s" % path_arg(throwaway),
        "config saveas %s" % path_arg(target),
        "config load %s" % path_arg(target),
    ]


# --------------------------------------------------------------------------
# Split into per-command blocks (the fast path still gets each OK/KO)
# --------------------------------------------------------------------------

#: The marker of each command is **bare** (on its own line). That is the measured
#: shape; do not confuse it with the numbered total marker.
_BARE_MARKER_RE = re.compile(r"^[ 	]*(OK|KO)[ 	]*$")

#: The **numbered** total marker at the end of the run (measured `28 OK`; it is OK
#: even when some command in the middle answered KO).
_TOTAL_MARKER_RE = re.compile(r"^([0-9]+)[ 	]+(OK|KO)[ 	]*$")


def parse_script(raw: str, commands: list) -> list:
    """Split the raw text of `script <file>` into **one block per command**, each with
    its own OK/KO.

    Returns ``[{"command", "ok", "output"}, ...]``; `ok` is `None` when it cannot
    be decided.

    ## The measured raw shape (2026-09-29, 5 commands)

    ::

        get mcu name                  <- the echoed command line (identical to the script)
        STM32F429I(E-G)Tx             <- its output
        OK                            <- the marker of **this** command (bare; KO on failure)
        set mode FMC NoSuchModeEver
        KO
        ...
        28 OK                         <- the **total** marker of the run (numbered)

    Two rules, both measured:

    1. **The echoed command line** splits the blocks (matched command by command in
       script order, never guessed from the content);
    2. the **last line** of a block, when it is a bare `OK`/`KO`, is that command's
       verdict.

    So the fast path **does not sacrifice failure localisation**: the speed stays
    (one script call) and every verdict is still available. The session layer only
    has to do one thing: **wait for the numbered total marker** (see
    :mod:`cubemx1.session`), otherwise a slow command makes it return early and
    truncate silently.
    """
    lines = [l.rstrip() for l in (raw or "").splitlines()]
    # **Strip the numbered total marker of the run together with everything after
    # it** (shaped like `28 OK`).
    #
    # Note: the rule used to be "strip only when the total marker is the last line".
    # That was a **wrong assumption**: CubeMX logs **asynchronously** (its own log4j
    # thread), so the total marker **may** be followed by one more line -- a log line,
    # or noise that is **not in log format** such as
    # `<class>:<line> - exiting critical section`. Then the total marker is not
    # stripped and stays inside **the last command's block**: that block ends with
    # `1664 OK` while `_finish_block` only accepts a **bare** `OK`/`KO`, so
    # **the last command gets `ok=None`** ("verdict unknown").
    # A single-command script is worse: no block can be split out
    # (`len(parsed) != len(commands)`), so the caller **drops the whole batch**
    # (slower is better than misaligned).
    #
    # Measured shape (reproducible fully offline; four negative cases in the suite
    # guard it):
    #   ['lock pin "PH0/OSC_IN" true', 'OK',
    #    'lock pin "PH1/OSC_OUT" true', 'OK', '1664 OK',
    #    '2026-09-30 15:19:00,000 [DEBUG] <its log>']
    #   -> the second command gets `ok=None`; replacing the last line with
    #   non-log-format noise fails the same way.
    #
    # So the rule now is: **scan backwards for the last total marker and drop it with
    # everything after it** -- what follows the total marker **belongs to no
    # command** (the script has already finished). Scanning backwards also avoids
    # the case where the output of some command happens to contain a line that looks
    # like the total marker.
    total = None
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        m = _TOTAL_MARKER_RE.match(lines[i].strip())
        if m:
            total = m.group(2) == "OK"      # group(1) is the ms count, group(2) is OK/KO
            del lines[i:]
            break
    want = [c.strip() for c in (commands or [])]
    out: list = []
    ci = 0            # which command echo we are waiting for
    cur = None
    for line in lines:
        s = line.strip()
        if ci < len(want) and s == want[ci]:
            if cur is not None:
                out.append(_finish_block(cur))
            cur = {"command": want[ci], "body": []}
            ci += 1
            continue
        if cur is None:
            continue          # noise before any echo (session leftovers), dropped
        if protocol.LOG_LINE_RE.match(line):
            continue          # a log line is not output
        if s.startswith("MX>"):
            s = s[3:].strip()
        cur["body"].append(s)
    if cur is not None:
        out.append(_finish_block(cur))
    for x in out:
        x["total_ok"] = total
    return out


def _finish_block(blk: dict) -> dict:
    """Finish one block: take the **trailing bare OK/KO** as this command's
    verdict."""
    body = blk["body"]
    ok = None
    while body and body[-1] == "":
        body.pop()
    if body:
        m = _BARE_MARKER_RE.match(body[-1])
        if m:
            ok = m.group(1) == "OK"
            body.pop()
    return {"command": blk["command"], "ok": ok,
            "output": chr(10).join(body)}


def run_script_checked(client, work_dir, lines: list, what: str = "batch commands") -> dict:
    """Send one script and get the speed **and** the per-command OK/KO
    (:func:`parse_script`).

    Returns one field more than :func:`run_script`: ``"per_command"``. It costs
    **nothing**: the script is still sent once (that saves the one second per
    command of polling), the raw text is only parsed once more.
    """
    got = run_script(client, work_dir, lines, what=what)
    got["failed"] = [x["command"] for x in got.get("per_command") or []
                     if x["ok"] is False]
    return got
