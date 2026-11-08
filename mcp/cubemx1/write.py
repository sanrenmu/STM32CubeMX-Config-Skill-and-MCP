"""Shared parts of the write commands.

## Why every write is "save + reload + read back"

This is not excess caution; CubeMX behaviour forces it:

**When it saves an `.ioc`, CubeMX rebuilds the whole file from its in-memory
model.** Configuration it does not recognise is dropped, with no prompt and no
error. So:

- a command answering `OK` is not enough: OK only means the command was accepted
- `config saveas` must write the file and `config load` must read it back, to see
  whether the value is still there
- without reading back there is no way to know what was dropped silently

This belongs to the "checks that must stay" category: skipping it produces results
that look successful and are wrong.

## Where it saves

By default it saves to a temporary file and does not overwrite the user's project.
To overwrite the original file, pass `--to` explicitly.

After the reload, **the current project of the session is the file that was
saved** -- deliberately, so that the session state and the file on disk agree and a
later save cannot write to the wrong place.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

from . import parse as P
from .errors import CubemxError
from .invoke import path_arg, run

#: The two keys in an `.ioc` that carry the firmware package location.
_FW_LOC_KEYS = ("ProjectManager.DefaultFWLocation",
                "ProjectManager.CustomerFirmwarePackage")


def check_firmware_location(path) -> dict | None:
    """Can the firmware package path in an `.ioc` make `config load` raise a modal
    dialog?

    ## Evidence (**measured**; "not measured" became a measurement on 2026-09-28)

    Measured behaviour (the behaviour alone settles this check)::

        - the `.ioc` has `DefaultFWLocation=false` and the directory that
          `CustomerFirmwarePackage` points at **does not exist on this machine**
          -> loading the project raises a modal dialog titled
          `Firmware Location Error.` (body: "The folder '...' does not exists or
          does not contains a valid firmware ! ... Default Firmware location
          Settings will be used.");
        - that dialog **blocks the command line** (measured 20.2 seconds);
        - after it is dismissed `CustomerFirmwarePackage` is cleared and
          `DefaultFWLocation` goes back to `true`.
            this.m_sCustomerFirmwarePackage = null;
            this.defaultFWLocation = true;   // <- CubeMX resets both keys itself
        }

    Measured reproduction: build an `.ioc` with `DefaultFWLocation=false` and
    `CustomerFirmwarePackage` pointing at a **missing directory**, then send a bare
    `config load` -> **the `Firmware Location Error.` dialog appears and the command
    is blocked for 20.2 s** (it continues only after dismissal); once the load
    finishes both keys are **reset to their defaults** (read back:
    `DefaultFWLocation=true`, `CustomerFirmwarePackage=` empty).

    Returns ``None`` when there is no risk; otherwise a description dict the caller
    uses to warn and fall back.

    Note: **only "directory missing" and "value empty" are checked**. The case
    "the directory exists but holds no valid firmware package" is decided inside
    CubeMX by an availability check that cannot be reproduced here, so **that branch
    is not guessed** (`safe_load` falls back to the popup wrapper and that path
    still cannot hang).
    """
    p = Path(str(path))
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    def _val(key):
        m = re.search(r"^%s=(.*)$" % re.escape(key), text, re.M)
        return m.group(1).strip() if m else None

    dfl, cfp = _val(_FW_LOC_KEYS[0]), _val(_FW_LOC_KEYS[1])
    # `DefaultFWLocation` defaults to true, so do not block the normal true case.
    if dfl is None or dfl.lower() != "false":
        return None
    if not cfp:
        return {"default_fw_location": False, "customer_firmware_package": cfp,
                "reason": "empty",
                "detail": "`ProjectManager.DefaultFWLocation=false` but "
                          "`CustomerFirmwarePackage` is empty"}
    if Path(cfp).exists():
        return None
    return {"default_fw_location": False, "customer_firmware_package": cfp,
            "reason": "missing_dir",
            "detail": "`ProjectManager.DefaultFWLocation=false` and the directory "
                      "`CustomerFirmwarePackage=%s` **does not exist on this "
                      "machine**" % cfp}


def apply_mutations(client, mutations, save_to: str | Path | None = None,
                    work_dir: Path | None = None) -> dict:
    """Run a series of mutations, then save and reload.

    :param mutations: the commands to send, for example
        ``["set mode SPI1 Full_Duplex_Master"]``
    :param save_to: where to save. Without it a temporary file is used (**the user's
        project is not overwritten**)
    :returns: ``{"saved_to": path, "executed": [commands...]}``
    """
    executed = []
    for cmd in mutations:
        run(client, cmd, what="run mutation")
        executed.append(cmd)

    if save_to:
        target = Path(save_to)
    else:
        d = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
        d.mkdir(parents=True, exist_ok=True)
        target = d / ("cubemx1_write_%d.ioc" % time.time_ns())

    # The path must be quoted: the config commands parse their arguments, so a space
    # would split it. Note this is the opposite of the `>` redirection rule, where
    # quoting breaks things.
    run(client, 'config saveas %s' % path_arg(target), what="save project")

    # Reload: both to read the values back and to keep the session consistent with
    # the file on disk
    run(client, 'config load %s' % path_arg(target), what="reload project")

    return {"saved_to": str(target), "executed": executed}


def read_back(client, command: str, parser=None, what: str = "read back"):
    """Read a value back after a write. Wraps :func:`invoke.run` so call sites read
    clearly."""
    got, _ = run(client, command, parser, what=what)
    return got


def safe_load(client, path, work_dir: Path | None = None) -> None:
    """Load a project **without triggering the "save first?" dialog**.

    ## Why a bare `config load` is not allowed

    Measured (the dialog was captured as well): when the current project **has
    unsaved changes**, `config load` raises a modal dialog::

        Warning: Load Project
        Project xxx.ioc has been modified would you like to save the project?

    The modal dialog **blocks the command line**: the command does not return and no
    end marker appears, which looks like "hangs for hundreds of seconds, then KO
    with no message", and the project is lost.

    ## What it does

    Run `config saveas` to an unrelated temporary file first. That counts as
    "already saved", the dirty flag is cleared, and the later `load` does not ask.

    **`config save` on the target file itself is not allowed**: when the target is
    the current file, that writes the dirty state back and destroys what is about to
    be loaded.

    ## When to use it

    **Every place that switches project uses this**; never send a bare
    `config load`. A file that was just written and is loaded right away (for
    example `saveas` then `load` of the same file) is not dirty and never pops, so
    either way works, but using this uniformly is simpler.

    ## The second dialog: `Firmware Location Error.` (added 2026-09-28)

    When the target `.ioc` has `DefaultFWLocation=false` and the directory that
    `CustomerFirmwarePackage` points at does not exist (or the value is empty),
    **loading the project raises `Firmware Location Error.`**: measured to block
    for 20.2 s, and after the load both keys are reset by CubeMX. See
    :func:`check_firmware_location`.

    Handling: **only when that risk is detected**, switch on the CubeMX popup
    suppression for the duration (`popupwrapper set`, which answers with the
    default answer) and `unset` it right after the load -- never leave it on; the
    reason is in :func:`cubemx1.invoke.enable_popup_wrapper`.

    :returns: ``{"firmware_location": <result of check_firmware_location or None>}``
        (it used to return None; a caller that ignores the value is unaffected)
    """
    # With no project loaded there is nothing to save, so skip the clear: a forced
    # saveas reports an NPE.
    try:
        got = run(client, "get mcu name", P.single_value,
                  what="check whether a project is loaded")
        loaded = (got[0] if isinstance(got, tuple) else got) or ""
    except Exception:
        loaded = ""

    if loaded:
        d = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
        d.mkdir(parents=True, exist_ok=True)
        throwaway = d / ("cubemx1_cleardirty_%d.ioc" % time.time_ns())
        # Clear the dirty flag. This step must be saveas to another file; the reason
        # is above.
        run(client, 'config saveas %s' % path_arg(throwaway), what="clear dirty flag")

    fw = check_firmware_location(path)
    if not fw:
        # Note: **the "current" project matters too**. Measured: the
        # `DefaultFWLocation=false` plus custom firmware path of the previous project
        # **leaks into the load of the next one** -- the target file itself was clean
        # (it did not even have the `DefaultFWLocation` key) and the
        # `Firmware Location Error.` dialog still appeared and locked the session up.
        # Use the path the daemon remembers (cheap, no query to CubeMX); skip it when
        # unavailable.
        try:
            cur = client.current_project_path()
        except Exception:
            cur = None
        if cur and str(cur) != str(path):
            fw = check_firmware_location(cur)
    if fw:
        # **Refuse to load instead of gambling on the popup suppression.**
        #
        # Why refuse: measured that `popupwrapper set` **does not dismiss** this
        # dialog (it still hangs), so "suppress then load" is a gamble while refusing
        # is certain: the command line does not hang and the caller is told what to
        # fix.
        #
        # Why this dialog must be avoided: it blocks the command line **permanently**
        # (a modal dialog in the same process), and after the load CubeMX resets both
        # keys anyway (measured).
        raise CubemxError(chr(10).join([
            "this project has a bad firmware package location: **loading it raises "
            "the `Firmware Location Error.` dialog**"
            " and locks the command line up, so it is **refused here**.",
            "criterion: %s" % fw["detail"],
            "evidence: measured -- that dialog blocked for 20 seconds"
            " and after the load CubeMX reset both keys.",
            "how to fix it (pick one):",
            "  1. set `ProjectManager.DefaultFWLocation` to `true`"
            " (= use the default firmware package from the ST repository);"
            " the `.ioc` is text, edit that line;",
            "  2. or point `ProjectManager.CustomerFirmwarePackage` at a directory"
            " that **holds a valid firmware package**.",
            "(`cubemx1 global-services set DefaultFWLocation true` also does step 1,"
            " but it loads a project itself, so the first time has to be a manual"
            " edit.)",
        ]))
    run(client, 'config load %s' % path_arg(path), what="load project")
    # Remember the current project (CubeMX offers no query for it; see `set-project`
    # in the daemon). Skip it when unavailable; it does not affect the load.
    try:
        client.set_current_project(path)
    except Exception:
        pass
    return {"firmware_location": None}
