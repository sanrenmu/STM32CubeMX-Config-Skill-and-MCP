"""Basic actions for talking to a CubeMX session: send one command, write a path,
suppress popups.

Both the core layer and the command layer need these, so they live in the core
(**not in `cmds/`**). The location matters: moving this into `cmds/` would force
core modules to import it inside a function body, and the CI check rejects that
style.

Three things:

- :func:`run`: the fixed "send -> check -> parse" sequence, in one place
- :func:`path_arg`: how a path must be written when spliced into a command
  (absolute path plus quotes)
- :func:`enable_popup_wrapper` / :func:`disable_popup_wrapper`: the popup
  suppression CubeMX provides (a modal dialog blocks the command line)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .errors import CubemxError


class CommandFailed(CubemxError):
    """CubeMX answered KO, or the session broke."""


# --------------------------------------------------------------------------
# **Scoped cache** for read-only commands (avoids asking the same thing twice)
# --------------------------------------------------------------------------

_CACHE: dict | None = None
_CACHE_STATS: dict | None = None


class cache_queries:
    """Cache the results of **read-only commands** inside a `with` block.

    ## Why (measured 2026-09-29)

    Every command costs about one second of stdin polling, and the check phase of
    `apply` **asks the same thing about the same instance again and again** (a plan
    with 8 FMC entries asks `get mode all FMC` and `config saveext` several times).
    A measured `apply` run had dozens of such repeats, which is dozens of seconds.

    ## Rule (how behaviour is kept unchanged)

    - **Only read-only commands are cached** (`get ` / `check ` prefix, no `>`
      redirection);
    - **any write command clears the whole cache**: state changed, so earlier answers
      are no longer usable. The order "first check -> write modes -> second check"
      is therefore naturally correct: the mode writes are not read-only, they empty
      the cache, and the second check asks again.
    - It only affects the inside of the `with` block; callers outside are untouched.
    """

    def __init__(self):
        self.cleared = 0

    def __enter__(self):
        global _CACHE, _CACHE_STATS, _MEMO
        _CACHE = {}
        _MEMO = {}
        _CACHE_STATS = {"hit": 0, "miss": 0, "cleared": 0}
        self._stats = _CACHE_STATS
        return self

    def __exit__(self, *exc):
        global _CACHE, _CACHE_STATS, _MEMO
        self.hit = (_CACHE_STATS or {}).get("hit", 0)
        self.miss = (_CACHE_STATS or {}).get("miss", 0)
        self.cleared = (_CACHE_STATS or {}).get("cleared", 0)
        _CACHE = None
        _MEMO = None
        _CACHE_STATS = None
        return False


def _is_readonly(command: str) -> bool:
    s = command.strip()
    if ">" in s:                      # redirected commands (log capture) are not cached
        return False
    return s.startswith("get ") or s.startswith("check ")


_MEMO: dict | None = None


def scoped_memo(key: str, factory):
    """Remember one result under a **logical key** inside a `cache_queries` scope;
    outside the block it simply computes.

    ## Why it is needed (the `config saveext` case)

    Some "export a snapshot" actions **use a different path every time**
    (`dump_full_config` generates a unique temporary file name each call), so
    **caching by command text never hits**. Semantically they are the same thing:
    "a full snapshot of the current configuration". `gate_params` in `apply` asks
    for a snapshot **once per instance** (measured: about 1.2 s each), which wasted
    several seconds per round.

    This caches by the **name the caller gives** (for example `"fullconfig"`): the
    first call inside the scope really runs, later ones reuse it, and **any write
    command clears the whole block** (the same rule as the command cache).

    Note: only actions that do not change project state may be cached this way.
    `config saveext` exports a snapshot, so it qualifies; `config saveas`
    **changes the current project file name**, so it must **never** be wrapped.
    """
    if _MEMO is None:
        return factory()
    if key in _MEMO:
        if _CACHE_STATS is not None:
            _CACHE_STATS["hit"] += 1
        return _MEMO[key]
    if _CACHE_STATS is not None:
        _CACHE_STATS["miss"] += 1
    v = factory()
    _MEMO[key] = v
    return v


# --------------------------------------------------------------------------
# Send "a batch of read-only commands we already know about" at once (fast prefetch)
# --------------------------------------------------------------------------

_PREFETCH: "prefetch | None" = None


class prefetch:
    """Send a batch of read-only commands **that are already known to be needed** in
    one go; `run()` inside the block takes them from the buffer.

    ## Why (measured 2026-09-30)

    The **check phase** of `apply` asks `get functions <pin>` one by one (a fixture
    with 39 pins = **41.5 s**) and `get mode available/all` per instance (about
    22 s) ... while all of those commands **are known before the check starts** (the
    pins and instances are in the plan). Sent one by one, each pays about one second
    of stdin polling; packed into one script (`script <file>`) only the parse time
    remains.

    Division of labour with `cache_queries`: that one solves "**the same thing is
    asked several times**", this one solves "**a batch of known read-only commands is
    sent one by one**". They can be combined.

    ## Three rules (all to keep behaviour unchanged)

    1. **Only read-only commands enter the buffer** (`get ` / `check ` prefix, no
       redirection, not `capture_log=True`): a command captured through redirection
       **uses a different file name every time**, so buffering it would turn the
       empty standard output into the result;
    2. **a write command inside the block drops the whole batch**: state changed, so
       earlier answers are unusable (the same rule as `cache_queries`);
    3. **the number of split entries must equal the number sent**; when it does not,
       the **whole batch is dropped** and the commands are really sent one by one.
       Taking entries out of order is worse than being slow -- this feeds read-back
       checks, so slow is acceptable and misaligned is not.

    A command that cannot be taken **falls back to a real single send**, so "the
    prefetch list missed one" never fails, it just saves nothing.

    Usage::

        with prefetch(client, work_dir, ["get functions PA5", ...]) as pf:
            ...            # commands asked through run() here come from the buffer
            pf.take("get functions PA5")      # explicit take (returns the raw response)
    """

    def __init__(self, client, work_dir=None, cmds=None, what: str = "prefetch batch"):
        self.rows: dict = {}
        self.used = False
        self.sent = 0
        self.served = 0
        cmds = [c for c in (cmds or []) if _is_readonly(c)]
        self.sent = len(cmds)
        if not cmds:
            return
        try:
            from . import batch
            got = batch.run_script_checked(client, work_dir, cmds, what=what)
            parsed = got.get("per_command") or []
            if len(parsed) != len(cmds):
                return                    # mismatch -> drop the batch, send one by one
            # **A verdict that cannot be decided drops the whole batch** (added
            # 2026-09-30).
            #
            # Why so strict: `parse_script` splits blocks by the echoed command
            # line, so a mismatch **misaligns** them -- the count can still be right
            # while one entry carries **another command's output**. The visible
            # signal of misalignment is exactly "a block does not end with a bare
            # OK/KO" (`_finish_block` then gives `ok=None`).
            #
            # Measured (2026-09-30): after such an `ok=None` row was handed over as
            # a response, `if not resp.get("ok")` in `run()` judged it **KO** and
            # reported "the command failed" -- while its output **plainly said OK**.
            # That is the worst kind of error: a **false failure** (the user assumes
            # the command is unsupported). Tool rule: **slow is acceptable,
            # misaligned is not** -> drop the batch and send one by one.
            if any(x.get("ok") not in (True, False) for x in parsed):
                return
            for cmd, row in zip(cmds, parsed):
                # One raw response per command, shaped like `client.run()`, so the
                # check and parse in `run()` need no change at all.
                self.rows.setdefault(cmd, []).append({
                    "ok": bool(row["ok"]),
                    "output": row.get("output") or "",
                    "elapsed_ms": row.get("elapsed_ms"),
                    "from_prefetch": True,
                })
            self.used = True
        except Exception:
            return

    def take(self, command: str):
        """Take the raw response of this command; returns None when unavailable (or
        when it answered KO).

        **`ok` must be a real boolean**: anything `is not True` counts as
        unavailable. Why not `is False`: `parse_script` gives `None` when it cannot
        decide, and `None` would be judged **KO** by `not resp.get("ok")` in
        `run()` -- a **false failure** (the output says OK and the report says the
        command failed). This was hit once.
        """
        if not self.used:
            return None
        q = self.rows.get(command)
        if not q:
            return None
        row = q.pop(0)
        if row.get("ok") is not True:
            return None
        self.served += 1
        return row

    def clear(self):
        """Drop the whole batch (used when a write command appears inside the block)."""
        self.rows = {}
        self.used = False

    def __enter__(self):
        global _PREFETCH
        self._outer = _PREFETCH
        _PREFETCH = self
        return self

    def __exit__(self, *exc):
        global _PREFETCH
        _PREFETCH = self._outer
        return False


#: Command prefixes that **replace the current project**. **Only these** invalidate
#: the per-session refname memo (see :func:`ipxml.project_refname`): the device name
#: depends only on which project is loaded, and configuration commands such as
#: `set mode` / `clock set` never change the chip, so they do not invalidate it --
#: that is exactly what brought the `get mcu name` calls of one `apply` run down
#: from 69 to a single digit.
_SWITCHES_PROJECT = ("config load", "load ", "loadboard", "tinyload",
                     "project create-from", "import ", "MultiConfig")


def _switches_project(command: str) -> bool:
    return command.strip().startswith(_SWITCHES_PROJECT)


def drop_refname_memo(client) -> None:
    """Drop the "device name of this session" memo. Must be called after switching
    project (`run()` does it automatically)."""
    for attr in ("_cubemx1_refname",):
        try:
            if hasattr(client, attr):
                delattr(client, attr)
        except Exception:
            pass


#: A frame line of a Java stack trace (`\tat <package>.<class>...`). When CubeMX
#: throws, it prints the whole stack on standard output (measured: a common output
#: shape when a command fails).
#:
#: Note: **the class part must allow `/`**: JDK frames are **module qualified**
#: (measured shape: `at java.base/java.lang.Thread.run(...:1583)`), which
#: `[\w.$]+` alone cannot match, and such a line would leak into "it also said".
_STACK_FRAME_RE = re.compile(r"^\s*at\s+[\w.$/]+\([\w.]*:\d+\)\s*$")

#: Recognises an "exception first line": `java.lang.NullPointerException: ...`.
_EXCEPTION_LINE_RE = re.compile(
    r"^\s*(?:[\w.$]+\.)?[A-Za-z_]*(?:Exception|Error)\b.*$")


def _failed_message(label: str, output: str) -> str:
    """Build the error text for "the command failed", **folding a Java stack trace
    when one is embedded**.

    ## Why fold it (measured 2026-09-30)

    Running `peripherals describe <instance>` with no project loaded makes CubeMX
    answer with its own **null-pointer stack trace** (dozens of `at ...` lines).
    Copying it verbatim has two problems:

    1. **useless to the caller**: the sentence that matters is "**no project is
       loaded**" and the trace never says it. One measured run wasted two commands
       (about 40 s) before guessing that a project had to be created first;
    2. those dozens of lines are **internal implementation detail** of CubeMX; they
       do not help diagnosis and they bury the one line that matters.

    So: **keep only the exception first line**, fold the rest into "omitted", and
    state the **most likely cause**. The full text stays in the structured output
    (the `output` field of `--json` is not folded).
    """
    text = (output or "").strip()
    if not text:
        return (label + " failed (CubeMX answered KO)." + chr(10)
                + "it said: (nothing)"
                + chr(10) + "common causes: wrong argument count (CubeMX prints the "
                "usage and answers KO), or the project has no such peripheral or "
                "parameter.")
    kept, head, frames = [], None, 0
    for line in text.splitlines():
        if _STACK_FRAME_RE.match(line):
            frames += 1
            continue
        if head is None and frames == 0 and _EXCEPTION_LINE_RE.match(line):
            head = line.strip()
            continue
        kept.append(line)
    if frames < 2:                      # not a stack-trace shape -> report as is
        return (label + " failed (CubeMX answered KO)." + chr(10) + "it said:" + chr(10)
                + text + chr(10)
                + "common causes: wrong argument count (CubeMX prints the usage and "
                "answers KO), or the project has no such peripheral or parameter.")
    body = chr(10).join(kept).strip()
    # **Even the exception first line keeps only the exception type**: that line
    # usually holds package, class and method names, internal identifiers that do
    # not help diagnosis. **Runtime output and file wording must agree**, so the
    # class name is dropped too and only the type such as `NullPointerException`
    # stays.
    kind = head or ""
    m = re.search(r"([A-Za-z_]+(?:Exception|Error))", kind)
    kind = m.group(1) if m else "(it did not say which exception)"
    # ---- causes are stated **from evidence**, never as a guess dressed up as fact
    # ----
    #
    # This used to say **unconditionally** "**the most common cause is: no project is
    # loaded**". That was hit in practice: when `project save` collides with a target
    # file held by another process, CubeMX throws and the text mentions the
    # `Save Project` dialog, while the "no project loaded" sentence is **wrong** --
    # the caller then searches in the wrong direction.
    #
    # Now: **that direction is only suggested when the text really holds a clue**;
    # without a clue only the possibilities are listed, clearly marked as "no
    # evidence". The exception type is a clue too (a null pointer most often means no
    # project loaded, still worded as "most often", not "this is it").
    # The two non-ASCII literals below are the localised words for "popup" and
    # "dialog"; they are written as escapes so this source file stays ASCII while the
    # match itself is unchanged.
    hints = []
    if ("Save Project" in body) or ("\u5f39\u7a97" in body) \
            or ("\u5bf9\u8bdd\u6846" in body) \
            or ("dialog" in body.lower()):
        hints.append(
            "the text mentions **a blocked popup or dialog**, which usually means the "
            "target file is held by another process or the project has unsaved "
            "changes. Check whether another program has that file open.")
    if kind.endswith("NullPointerException"):
        hints.append(
            "the **most common cause** of a null pointer here is that no project is "
            "loaded: load one with `start --project <project.ioc>`, or create one "
            "with `project create-from-mcu`. (This is **inferred from the exception "
            "type**, not read from the text.)")
    if not hints:
        hints.append(
            "**no cause could be read from the text**. Possible causes (possibilities "
            "only, no evidence): wrong argument count, no such peripheral or "
            "parameter in the project, or no project loaded.")
    return (label + " failed (CubeMX answered KO)." + chr(10)
            + "it threw `%s` internally (%d more stack frames omitted;"
              " they are internal implementation detail and do not help diagnosis)."
              % (kind, frames)
            + chr(10) + chr(10).join(hints)
            + ((chr(10) + "it also said:" + chr(10) + body) if body else ""))


def run(client, command: str, parser: Callable | None = None,
        capture_log: bool = False, what: str = "",
        allow_fail: bool = False):
    """Send one command and parse the result when asked.

    :param capture_log: set True when this command writes its output to the log (the
        daemon then captures it through the redirection). Which commands need it is
        described in :mod:`cubemx1.protocol`.
    :param parser: parse function taking the raw text and returning structured data.
        Without it the raw text is returned.
    :param what: human-readable label used in error messages.
    :param allow_fail: tolerate a KO from CubeMX.

        **When it is needed**: CubeMX answers KO to commands that only print their
        usage (measured: it prints the usage and then answers KO). So for a command
        such as `clock set`, which is used as "take its usage table as data",
        seeing KO is normal and the content is in the output.

        For the same reason **KO does not mean "the command does not exist"**. The
        two cases look different: too few arguments print a block starting with
        `Usage:`, an unknown command prints the top-level command table.
    """
    label = what or command

    # ---- scoped cache for read-only commands (see `cache_queries`) ----
    # A hit reuses the **raw response** and then runs the same post-processing below
    # (the check and parse logic is unchanged, so "raise on KO" behaves exactly as
    # without a cache). Not caching the parsed result is deliberate: the same command
    # may use a different parse function at another call site.
    resp = None
    # ---- prefetch buffer for "already known" commands (see `prefetch`) ----
    # Only "read-only and not captured through redirection" commands may come from the
    # buffer: a `capture_log` command uses a different temporary file every time, so
    # the buffered copy is **empty standard output** and cannot be the result.
    if _PREFETCH is not None:
        if _is_readonly(command) and not capture_log:
            resp = _PREFETCH.take(command)
        else:
            _PREFETCH.clear()          # write command -> state changed, drop the batch
    if resp is None and _CACHE is not None:
        if _is_readonly(command):
            resp = _CACHE.get(command)
            if resp is not None and _CACHE_STATS is not None:
                _CACHE_STATS["hit"] += 1
        else:
            # **write command -> state changed, earlier answers are all unusable**
            _CACHE.clear()
            if _MEMO is not None:
                _MEMO.clear()          # state changed -> the snapshot is unusable too
            if _CACHE_STATS is not None:
                _CACHE_STATS["cleared"] += 1
    if resp is None:
        resp = client.run(command, capture_log=capture_log)
        if _CACHE is not None and _is_readonly(command):
            _CACHE[command] = resp
            if _CACHE_STATS is not None:
                _CACHE_STATS["miss"] += 1
    # A project switch changes the device name, so drop the per-session copy (see
    # `ipxml.project_refname`; only those prefixes switch project, configuration
    # commands do not)
    if resp.get("ok") and _switches_project(command):
        drop_refname_memo(client)

    if resp.get("fatal"):
        raise CommandFailed(
            f"the session is unusable, {label} was not done:\n{resp.get('output') or ''}")
    if not resp.get("ok") and not allow_fail:
        raise CommandFailed(_failed_message(label, resp.get("output") or ""))

    text = resp.get("output") or ""
    parsed = parser(text) if parser else text
    return parsed, resp


def run_or_empty(client, command: str, parser: Callable | None = None,
                 **kw):
    """Send one command; **return None when it cannot be parsed or answered KO**
    (instead of raising).

    ## Why it is needed (the root cause of six dead branches)

    Some callers mean "**treat an unavailable value as absent**" and write:

        x, _ = run(client, cmd, P.single_value, ...)
        if not x:                       # <- meant to catch "empty output"
            ...report "no project is loaded"...

    But parsers such as `P.single_value` / `P.pinout` **raise on empty output**
    (`parse.single_value`: no line at all means `raise FormatError`), so the
    `if not x:` **never runs** and the user sees "empty output could not be parsed"
    instead of the intended "no project is loaded".

    This function turns "empty/failed" into `None` and **hands the decision back to
    the caller**.

    Note: it swallows only the **two expected kinds**, `FormatError` (cannot parse)
    and `CommandFailed` (answered KO / session unusable). Anything else is raised --
    otherwise a real bug would be swallowed too.
    """
    from .errors import CubemxError
    from .parse import FormatError
    try:
        parsed, _resp = run(client, command, parser, **kw)
    except (FormatError, CommandFailed):
        return None
    except CubemxError:
        return None
    return parsed


def path_arg(p) -> str:
    """The path form to use when splicing into a CubeMX command: **absolute path plus
    double quotes**.

    ## Why it must be done this way

    The CubeMX working directory is **its own installation directory**. A relative
    path is resolved against that directory.

    Measured: a relative path of the form `..` plus a subdirectory put the output
    **next to the installation directory** (because `..` walks up from there) while
    our target directory stayed empty, and **the command still reported success**.

    The same class of problem occurred once before: a start-up script used a relative
    path, CubeMX could not find the file, startup timed out, and the error pointed
    nowhere near the cause.

    ## Why quotes are needed as well

    CubeMX **splits the command line on whitespace first**. A path with a space is cut
    apart and only the first half arrives. Measured: `project path` with a path
    containing a space answers KO with **no explanation at all** -- an empty error is
    the hardest to diagnose.

    This function **returns the quotes itself**, so call sites must not add them:
    write `"project path %s" % path_arg(d)`, never
    `'project path "%s"' % path_arg(d)` (that gives two layers of quotes).

    **The rule is: every path spliced into a CubeMX command goes through this
    function.** Do not decide case by case at the call site.
    """
    return '"%s"' % Path(p).resolve()


def ok_result(**kw) -> dict:
    """The shared result envelope."""
    return {"ok": True, **kw}


#: Sessions that already have the popup suppression on (keyed by client id).
#: `popupwrapper set` is cheap (about 1 second) but sending it before every command is
#: pointless.
_POPUP_WRAPPED: set = set()


def enable_popup_wrapper(client, force: bool = False) -> dict:
    """Switch on the **popup suppression CubeMX provides** (`popupwrapper set`).

    ## Why this is the right answer (and the SendKeys watchdog is not)

    The CubeMX GUI and command line are **the same process**, so a modal dialog
    blocks the command line. We used to enumerate windows, call `AppActivate` and
    press Enter with `SendKeys`. That cannot dismiss anything on a **non-interactive
    window station** (`AppActivate` simply fails), and early versions suffered from
    it several times.

    Measured (2026-09-27): CubeMX has a `popupwrapper` of its own:

    ::

        popupwrapper set            # on: every popup returns the "default answer"
        popupwrapper unset
        popupwrapper record_answer <title> <answer>
            # the answer is one of OK / YES / NO / CANCEL / CLOSE / BUTTON_1..9

    With it on, the `Memory Protection Unit for Cortex-M7` dialog raised by
    `load STM32H750VBTx` is answered automatically and the command returns by itself:
    ``... has been wrapped, default Answer was returned`` (measured). **That is far
    more reliable than key presses and needs no person or script standing by.**

    ## Where it is used

    **Only on the actions that are known to raise a dialog** (`create_from_mcu` /
    `create_from_board` / `generate`), **never globally**. Reason:
    `popupwrapper` answers **every** popup with the default answer, including
    `Warning: Load Project` (which asks whether to save unsaved changes, and the
    default is usually "save") -- that would **write the user's project file**.
    Project switching always goes through `write.safe_load` (clear the dirty flag
    first, so that dialog never appears), so it must not get this opening.

    The dangerous "may I download packages" dialog
    (`Downloading selected software packages`) is covered by a **pre-check**
    (`ideproject._ensure_firmware_package`) instead, not by this fallback.
    """
    key = id(client)
    if not force and key in _POPUP_WRAPPED:
        return {"ok": True, "already": True}
    run(client, "popupwrapper set", what="enable popup suppression")
    _POPUP_WRAPPED.add(key)
    return {"ok": True, "already": False}


def disable_popup_wrapper(client) -> dict:
    """Switch the popup suppression off (`popupwrapper unset`); the counterpart of
    :func:`enable_popup_wrapper`.

    Its use is **temporary**: when the firmware package path of the target `.ioc` is
    broken, `write.safe_load` would raise `Firmware Location Error.` (measured: 20
    seconds blocked), so it is switched on there and off right after the load.
    **It must not stay on**; the reason is in :func:`enable_popup_wrapper`.
    """
    run(client, "popupwrapper unset", what="disable popup suppression")
    _POPUP_WRAPPED.discard(id(client))
    return {"ok": True}
