"""Output parsing for CubeMX interactive mode.

This file gathers all knowledge about "why CubeMX output looks strange" in one
place. No other module may contain parsing logic.

## The end marker has two shapes

**Interactive mode (``-i``)**: ``<ms> OK`` / ``<ms> KO`` (measured sample: ``14 OK``)

**Script mode (each line of ``-q <script>``)**: a bare ``OK`` / ``KO`` without a
millisecond count; when the whole script ends there is one more **total marker with
a count** (measured sample: ``28 OK``).

So one line tells three things at once: **the command finished, whether it
succeeded, and how long it took** (script mode gives no duration).

## Which mode to use

Measured conclusion:

=================  ============  ============
Start mode         Window        Resident
=================  ============  ============
``-i``             yes           yes
``-q <script>``    **none**      **yes**
=================  ============  ============

So **use ``-q <script>``**: give a start-up script that only warms up, then keep
feeding commands. No window, and still the speed of a resident session.

(Note: ``-q`` **must** be followed by a script file. Without a script CubeMX falls
back to graphics mode; this was confirmed -- without a script the window handle is
non-zero, with a script it is 0.)

## Three dirty cases that must be handled

1. **The marker sticks to the content** without a newline. The measured output of
   ``get sub-modes SPI1`` is::

       Current Application RegionTransmit Only MasterSysTickCurrent Application RegionTransmit Only MasterSysTick2 OK

   Note the trailing ``2 OK`` is glued to the content. So a whole-line match does
   not work; match at the end of the line.

2. **Log lines are mixed into the output.** The log4j format is
   ``2026-09-25 19:02:03,532 [WARN] <logger>:<line> - message``.
   These are picked out and kept apart; they are not command output.

3. **The prompt ``MX> `` shows up in the output**, sometimes in the middle of logs.

## The output of parameter queries is not on standard output

The output of commands like ``get mode_param_list`` **goes to the log** (not to
stdout) and lands in the log file; standard output only carries the end marker.
Measured.

You may think "then just read the log file". But the log file is global and several
sessions would mix. The better way is the redirection CubeMX provides:
``<command> > <file path>``, measured -- it attaches a temporary file appender.
:func:`redirect_suffix` builds that suffix. **Do not quote the path**; the reason is
in that function.

## Another reason for a missing marker: the command raised inside

Do not assume a missing marker only means a slow command. **Measured: when a
command fails internally the end marker is never printed** -- the output holds a
stack trace and no `OK`/`KO` at all. So a missing marker must be judged as two
possibilities at once: **the command is slow, or it raised an unhandled
exception** (the timeout message must say so). The session layer therefore cannot
wait forever; it needs a timeout, and the timeout message must mention this cause,
otherwise the user assumes the command is merely slow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# End marker. Two shapes:
#   interactive mode  <ms> OK / <ms> KO
#   script mode       OK / KO (bare, no number)
# Anchored at the end of the line rather than the whole line, because the marker
# sticks to the content, for example "...SysTick2 OK".
# Measured in both interactive and script mode.
STATUS_RE = re.compile(r"(?:([0-9]+)[ \t]+)?(OK|KO)[ \t]*$", re.MULTILINE)

# Log line: 2026-09-25 19:02:03,532 [WARN] <logger>:<line> - message
LOG_LINE_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} "
    r"\[(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\]"
)

#: Same shape but **not anchored at the line start**: finds a log entry glued to real
#: content (see the clean-up loop below). Only with it can `value` and `the log tail
#: after the value` be split apart instead of failing the whole line as bad format.
LOG_ANYWHERE_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} "
    r"\[(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\]"
)

# Prompt. Appears at the line start or right after other output.
PROMPT_RE = re.compile(r"^MX>[ \t]?")

# A whole line that is just an end marker (possibly with a millisecond count).
# Used to clear a "late" marker left in the body.
#
# Why it is needed: the marker of the previous command may arrive late and land in
# this command's read window, and parse only truncates at the **last** marker, so
# the old one stays in the body and breaks the upper layer. Measured symptom: a
# `5 OK` line mixed into the output of `get pinout` made the pin parser report a
# format mismatch, and the error alone pointed nowhere near the cause.
STANDALONE_MARKER_RE = re.compile(r"^[0-9]*[ \t]*(OK|KO)[ \t]*$")


@dataclass
class Result:
    """The result of one command."""

    command: str
    ok: bool
    elapsed_ms: int | None
    """Execution time in milliseconds reported by CubeMX. Script mode has no such
    number, so this is None."""
    output: str
    """The useful output of the command. End markers, log lines and the prompt have
    been removed."""
    log_lines: list[str] = field(default_factory=list)
    """The log lines that were picked out (kept as they are, timestamps included)."""
    raw: str = ""
    """The raw text, used for diagnosis when something fails."""

    def lines(self) -> list[str]:
        """The useful output split into lines with blank lines removed."""
        return [l for l in (x.rstrip() for x in self.output.splitlines()) if l]


def parse(command: str, text: str) -> Result:
    """Parse one raw output block into a result.

    Raises :class:`~cubemx1.errors.NoStatusMarkerError` when no end marker is
    found: that means the session state is wrong and must not be treated as
    success.
    """
    from .errors import NoStatusMarkerError

    matches = list(STATUS_RE.finditer(text))
    if not matches:
        raise NoStatusMarkerError(
            f"no end marker arrived. likely causes, most likely first:\n"
            f"  1. the command raised inside CubeMX; the exception was swallowed, so no marker is printed\n"
            f"     common triggers: a quoted redirection path, a malformed argument.\n"
            f"  2. the command is slow and still running (loading a project takes 20-40 s, generating takes longer).\n"
            f"  3. the session is no longer healthy.\n"
            f"command sent: {command}\n"
            f"content received (last 500 characters):\n{text[-500:]}"
        )

    # Prefer a marker that carries a millisecond count (interactive mode). If any
    # numbered marker appeared, this is an interactive reply, so take the last
    # numbered one. That avoids a stray bare "OK" inside the content being taken as
    # the marker.
    numbered = [m for m in matches if m.group(1) is not None]
    m = numbered[-1] if numbered else matches[-1]
    ok = m.group(2) == "OK"
    elapsed = int(m.group(1)) if m.group(1) is not None else None
    body = text[: m.start()]

    clean_lines: list[str] = []
    logs: list[str] = []
    stale_markers: list[str] = []
    for line in body.splitlines():
        if LOG_LINE_RE.match(line):
            logs.append(line)
            continue
        # The prompt may be glued to real content; strip it before deciding
        stripped = PROMPT_RE.sub("", line)
        if LOG_LINE_RE.match(stripped):
            logs.append(stripped)
            continue
        # **A log entry glued to real content** (not at the line start) is handled by
        # splitting, **not** by relaxing the parse.
        #
        # Measured (2026-10-01): a valid query `get ip parameters SPI1 CLKPolarity`
        # occasionally returned exit 1 -- the value was a valid `SPI_POLARITY_LOW`
        # but **a DEBUG timestamp followed it immediately**, so
        #   1. `LOG_LINE_RE` only matches the line start, so the line counted as
        #      neither log nor clean output and went into the body untouched;
        #   2. the parameter regex is **anchored at the end of the line**, so the
        #      extra timestamp made the whole line mismatch and raised a format error.
        # So the split happens **at a log entry that is not at the line start**: the
        # front goes to the body, the tail goes to `logs`.
        # Note: **why not relax the parameter regex**: relaxing it would also let real
        # bad lines (missing value, truncated line) through, which trades a weaker
        # check for fewer errors. Splitting only routes information; the check stays
        # as strict.
        mm = LOG_ANYWHERE_RE.search(stripped)
        if mm and mm.start() > 0:
            logs.append(stripped[mm.start():])
            clean_lines.append(stripped[:mm.start()].rstrip())
            continue
        # A standalone end marker in the body: that is a late marker of the previous
        # command, not output. Keeping it would break the upper layer, so it is
        # recorded apart and not put into the body.
        if STANDALONE_MARKER_RE.match(stripped.strip()):
            stale_markers.append(stripped.strip())
            continue
        clean_lines.append(stripped)

    return Result(
        command=command,
        ok=ok,
        elapsed_ms=elapsed,
        output="\n".join(clean_lines),
        log_lines=logs,
        raw=text,
    )


def redirect_suffix(path) -> str:
    """Build the command suffix that redirects the log to a file.

    Used on commands whose output goes to the log, for example::

        get mode_param_list SPI1 XXX > /tmp/out.txt

    Measured: after a command that carries `>`, CubeMX sends the output of that
    command to the given file, so the global log need not be searched and other
    sessions cannot mix in.

    **Do not quote the path.** Measured: whatever follows ``>`` is used **as is**
    as the file name; nothing strips quotes. A quoted path therefore becomes part of
    the file name and the file is never created (measured: the command hangs and no
    file appears). Spaces in the path are fine because the whole right-hand side is
    the file name.

    :param path: target file path. **Do not add quotes.**
    """
    return f" > {path}"
