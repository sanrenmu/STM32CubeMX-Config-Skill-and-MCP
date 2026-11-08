"""Shared base of the CLI layer: output shell + client lookup + small helpers.

Every `cmd_*` adapter depends only on the helpers here, and **they never call
each other** (verified with AST when `cli/` was split out: zero references
between the 78 `cmd_*` functions)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..client import Client
from ..errors import CubemxError

EXIT_OK = 0
EXIT_FAIL = 1


def machine():
    """Machine config handle (does not start CubeMX). Shared by all cmd_*."""
    from ..machine import load
    return load()


def _abs_project(p):
    """Normalize `--project` to an **absolute path** and **check the file exists first**.

    ## Why this is required (measured 2026-09-30)

    CubeMX's **working directory is its own install directory**, so a relative
    path is resolved against that directory. Measured with
    `cubemx1 start --project <relative>.ioc`:

    - it does not find the file relative to the install directory;
    - it then **opens a `Load Project` dialog** and blocks the command line
      for good -- `start` can only report "the command never produced an end
      marker and CubeMX opened a dialog";
    - the **dirty-marker cleanup does not stop it** either (`safe_load`
      guards against "project has unsaved changes"; "the file does not exist"
      is a different problem).

    So the path is normalized **the moment it enters the CLI**; do not rely on
    every downstream step remembering it. A missing file is reported **right
    there** (the "a bad argument fails fast" rule) instead of being sent over
    and gambled on.
    """
    if not p:
        return p
    path = Path(p).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    if not path.is_file():
        raise CubemxError(
            "project file not found: %s\n"
            "(`--project` must point at an **existing** `.ioc`. The tool "
            "normalizes it to an absolute path before handing it to CubeMX -- "
            "a relative path is resolved against **CubeMX's own install "
            "directory**, which was measured to open a `Load Project` dialog "
            "and hang the session.)"
            % path)
    return str(path)


class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, payload: dict, text: str) -> None:
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(text)

    def emit_error(self, payload: dict, text: str) -> None:
        """Emit a command error using the selected output contract.

        JSON mode keeps the machine-readable response on stdout. Human mode
        preserves the CLI convention of sending diagnostics to stderr.
        """
        if self.as_json:
            self.emit(payload, text)
        else:
            print(text, file=sys.stderr)


def _join(items) -> str:
    """Join several lines into one block of text."""
    return "\n".join(items)


def _fmt_result(resp: dict, show_log: bool = False) -> str:
    parts: list[str] = []
    body = (resp.get("output") or "").rstrip()
    if body:
        parts.append(body)
    elif resp.get("ok"):
        # Success with no output is normal, e.g. asking for the MCU name while
        # no project is loaded. Say so, otherwise it looks like the command
        # had no effect.
        parts.append("(the command succeeded but produced no output. "
                     "Common cause: no project is loaded, so there is nothing "
                     "to report.)")
    else:
        parts.append("(the command failed but CubeMX gave no specific output.)")

    timing = []
    if resp.get("elapsed_ms") is not None:
        timing.append("CubeMX reported %d ms" % resp["elapsed_ms"])
    if resp.get("total_ms") is not None:
        timing.append("%d ms from send to reply" % resp["total_ms"])
    if resp.get("queue_ahead"):
        timing.append("%d commands ahead in the queue" % resp["queue_ahead"])
    if timing:
        parts.append("(" + ", ".join(timing) + ")")

    if show_log and resp.get("log_lines"):
        parts.append("")
        parts.append("Log lines (CubeMX log, not command output):")
        parts.extend("  " + l for l in resp["log_lines"])
    return "\n".join(parts)


def _starter_of(args) -> list:
    """Startup script.

    **The project does not go here.** Measured: a config load performed from
    the startup script does not register the "project-only commands"
    (`clock set` and the whole `dma` group are missing), so the project has to
    be loaded interactively. The project is passed to the daemon through
    `Client(project=...)`, and the daemon loads it interactively after the
    startup script finishes. See the notes in cubemx1.daemon.
    """
    return None


def _need_client(args) -> Client:
    """Get a usable client. Starts a background process when none is running."""
    return Client(ensure_running=True,
                  show_window=getattr(args, "show_window", False),
                  require_exclusive=getattr(args, "require_exclusive", False),
                  port=getattr(args, "port", None),
                  starter=_starter_of(args),
                  project=_abs_project(getattr(args, "project", None)))


def _run_command(args, command: str):
    """Send one command; self-heal once when the infrastructure is broken.

    Returns (response, healed). Healing covers **infrastructure** failures such
    as a dead session or an unreachable port, not a command that failed on its
    own. A command that fails (CubeMX answers KO) is not retried.
    """
    from ..daemon import clear_state
    try:
        c = _need_client(args)
        resp = c.run(command)
        if resp.get("fatal"):
            raise RuntimeError(resp.get("output") or "the session is no longer usable")
        return resp, False
    except RuntimeError:
        clear_state()
        c = _need_client(args)
        return c.run(command), True


def _save_to_of(args):
    return getattr(args, "save_to", None)
