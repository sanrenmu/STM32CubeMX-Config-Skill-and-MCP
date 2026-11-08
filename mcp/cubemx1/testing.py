"""Client for tests and probe scripts.

## Why this file exists

The `cmds/` layer needs something that can `run(command)`, and the command line
passes a `Client` (one that talks to the daemon). Probe scripts and tests, however,
**cannot easily go through the daemon**: they often need their own session, or
exact control over which project is loaded.

So three probe scripts each wrote the same glue that wraps `Session` in something
shaped like `Client`, and the three copies were almost identical. This file keeps
that glue once.

**The interface does not go beyond what the `cmds` layer really uses; not one
method more**, otherwise it becomes "maybe useful later".

## Usage

::

    from cubemx1.testing import LocalClient
    from cubemx1.session import Session

    sess = Session(machine, starter=["get version"])
    sess.wait_ready()
    client = LocalClient(sess)
    from cubemx1.cmds import peripherals
    res = peripherals.describe(client, "SPI1")

That exercises the **real command implementation**, not a second copy of the
probing logic; otherwise what was tested and what is used are not the same thing,
which is the same as not testing at all.
"""

from __future__ import annotations

import time
from pathlib import Path

# The `cmds` layer calls Client.run without a timeout; this default is the fallback
DEFAULT_TIMEOUT = 620.0

# Commands that are captured through redirection; the daemon has its own directory,
# so this one is separate too
CAPTURE_SUBDIR = "test_capture"


class LocalClient:
    """Wrap :class:`~cubemx1.session.Session` in the shape of `Client`.

    Implements the methods the `cmds` layer really uses: `run(...)`,
    `set_current_project(...)` / `current_project_path()` (same shape as the daemon
    `Client`) and `load_project(...)`.

    :param session: a session that is already ready
    :param work_dir: where the redirection files go. Defaults to the temp directory.
    """

    def __init__(self, session, work_dir: Path | None = None):
        import tempfile
        self.session = session
        base = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
        self.capture_dir = base / CAPTURE_SUBDIR
        self._n = 0

    def run(self, command: str, capture_log: bool = False,
            timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Run one command and return a dict shaped like `Client.run`.

        **A failure does not raise**; it returns `{"ok": False, ...}`, because the
        `cmds` layer reads the ok field (`invoke.run` turns ok=False into an
        exception).

        The one exception is **the session itself being broken** (the process died,
        or a CubeMX modal dialog blocks the command line): then `Session.run` raises
        `SessionError` and this returns ``{"ok": False, "fatal": True}``, the same
        convention as the daemon. On `fatal` the caller must **stop there**: the
        dialog is still up and every later command just blocks in turn.
        (This happened: a probe script swallowed the exception as an ordinary
        failure and sent a dozen more commands, each blocking until timeout, so the
        whole probe run was wasted.)
        """
        from .errors import SessionError

        try:
            if capture_log:
                text = self._run_captured(command, timeout)
                return {"ok": True, "output": text}
            r = self.session.run(command, timeout=timeout)
            return {"ok": True, "output": r.output, "elapsed_ms": r.elapsed_ms,
                    # The raw text (with each bare OK/KO): the per-command verdict
                    # of the fast path depends on it. Kept identical to the daemon
                    # response in `daemon.py`.
                    "raw": r.raw, "log_lines": r.log_lines}
        except SessionError as e:
            return {"ok": False, "fatal": True, "output": str(e)}
        except Exception as e:
            return {"ok": False,
                    "output": getattr(e, "output", None) or str(e),
                    "elapsed_ms": getattr(e, "elapsed_ms", None)}

    def set_current_project(self, path) -> dict:
        """Record which `.ioc` is currently held.

        **Why this had to be added here** (2026-10-01, Gitea #18): `Client` (the
        daemon path) had this method while `LocalClient` **did not`. `apply` calls
        it after saving, so the call raised `AttributeError` -- and that call sat
        inside `except Exception: pass`, so **the exception was swallowed** and the
        two clients stayed inconsistent without anyone noticing.

        `apply` no longer swallows exceptions, so the gap has to be closed: without
        it every online probe that uses `LocalClient` (`probelib.connect` returns
        exactly that) failed as soon as it ran `apply`.

        This process has no daemon cache, so the path is only stored on the object;
        the return value keeps the shape of the daemon `set-project` response
        (`{"ok": True, "project_path": ...}`).
        """
        self.project_path = str(path)
        return {"ok": True, "project_path": self.project_path}

    def current_project_path(self):
        """The current project path this process remembers (None when it has none),
        same shape as `Client`."""
        return getattr(self, "project_path", None)

    def load_project(self, path) -> dict:
        """Switch project. **Never send a bare `config load`**; always go through here.

        A bare `config load` on a project with unsaved changes raises the
        `Warning: Load Project` modal dialog and blocks the command line for good.
        `write.safe_load` first runs `config saveas` to an unrelated file, which
        clears the dirty flag so no dialog appears. The reasoning is in that
        module.
        """
        from .errors import SessionError
        from . import write

        try:
            write.safe_load(self, str(path), self.capture_dir.parent)
            return {"ok": True, "output": ""}
        except SessionError as e:
            return {"ok": False, "fatal": True, "output": str(e)}
        except Exception as e:
            return {"ok": False,
                    "output": getattr(e, "output", None) or str(e)}

    def _run_captured(self, command: str, timeout: float) -> str:
        """Use the `command > file` redirection to get the log content back.

        The two redirection pitfalls (the path must not be quoted, a busy file needs
        a fresh name) are handled once here so every script does not repeat them.
        """
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self._n += 1
        f = self.capture_dir / ("cap_%d_%d.txt" % (id(self), time.time_ns()))
        # The path is **not quoted**: CubeMX takes whatever follows `>` as the file
        # name.
        self.session.run("%s > %s" % (command, f), timeout=timeout)
        # the file may need a moment to appear on disk
        for _ in range(20):
            if f.is_file():
                try:
                    t = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    t = ""
                if t:
                    return t
            time.sleep(0.1)
        return ""
