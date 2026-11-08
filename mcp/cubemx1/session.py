"""The long-lived session: start CubeMX once and keep using it.

This is the core of the whole tool. Why it must stay resident:

- one cold start of CubeMX takes about 9-15 seconds; loading a project 20-40 seconds.
- restarting for every command would mean waiting a minute to query three
  peripherals.
- when resident, startup and load are paid once and each later command costs 0-2 ms.

## Which start mode (measured conclusion)

=================  ============  ============  ============================
Start mode         Window        Resident      CubeMX end marker looks like
=================  ============  ============  ============================
``-i``             yes           yes           ``<ms> OK`` / ``KO``
``-q <script>``    **none**      yes           script lines give a bare ``OK``,
                                                 later commands carry ms
=================  ============  ============  ============================

So **use ``-q <script>``**: give a start-up script that only warms up, then keep
feeding commands. No window, and still the speed of a resident session. See
:mod:`cubemx1.protocol`.

Note that ``-q`` **must** be followed by a script file. Without a script CubeMX
falls back to graphics mode (measured: without a script the window handle is
non-zero, with one it is 0).

About the window
----------------
On Windows ``STARTUPINFO`` + ``SW_HIDE`` + ``CREATE_NO_WINDOW`` are added as well
(inherited from an early version). That only hides the console shell; what really
keeps the main window away is the ``-q`` mode itself.

Note that CubeMX allows a single instance: when the user already has the GUI open,
check before starting, otherwise the failure is very hard to read.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import protocol
from .errors import BusyError, CubeMxCommandError, SessionError
from .machine import Machine

# These arguments come from STM32CubeMX.l4j.ini in the CubeMX installation, plus two
# entries from the manual. --add-opens / --add-exports are copied from the official
# launcher configuration; without them the JVM does not start.
JVM_ARGS = [
    "-Djavax.net.ssl.trustStoreType=WINDOWS-ROOT",
    "-Dsun.java2d.d3d=false",
    "-Dfile.encoding=UTF8",
    "--add-exports", "java.desktop/sun.awt=ALL-UNNAMED",
    "--add-opens", "java.desktop/java.awt=ALL-UNNAMED",
]

# The start-up script runs one line by default, to confirm the session is alive.
# Measured cold start: about 9-15 seconds.
DEFAULT_STARTER = ["get version"]

STARTUP_TIMEOUT = 180.0

# Hard limit for one command. Loading a project can take 40 seconds and generating
# code longer, so it is generous. Note: completion is decided by receiving the end
# marker, not by "how long there was no output". See Session.run.
COMMAND_TIMEOUT = 600.0

# Polling interval. Keep it small: sleeping too long turns "the command already
# finished" into "it looks stuck".
POLL_INTERVAL = 0.02

# Past this time without an end marker, start suspecting a stall (a modal dialog, for
# example). Loading a project takes over ten seconds with no output, so this threshold
# is clearly larger than a normal command.
STALL_HINT_AFTER = 25.0

# Once suspicious, how often to check that process for popup windows.
STALL_RECHECK_EVERY = 5.0


def _window_options(hide: bool) -> dict:
    """Additionally hide the console shell on Windows.

    Measured approach. Note it **cannot** hide the CubeMX main window, which ignores
    the start-up information. What really keeps the main window away is ``-q`` mode.
    """
    if os.name != "nt" or not hide:
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startup,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def visible_windows(pid: int) -> list:
    """List the titles of the **visible** top-level windows of one process.

    **Why it has to be this involved**: `MainWindowTitle` cannot see dialogs.
    Measured: when CubeMX is started with `-q` its main window is hidden
    (`MainWindowHandle` is 0) and `MainWindowTitle` is empty, while a modal dialog is
    a visible top-level window that `MainWindowTitle` does not report.

    Measured (enumeration while a dialog was up)::

        3281042 vis=True  title=Warning: Load Project    <- the one to catch
        4789394 vis=False title=User Preferences
        6230182 vis=False title= cubemx1_stall_t.ioc*: STM32H750VBTx
        1577032 vis=False title=theAwtToolkitWindow

    So the rule is: **the main window is normally hidden, so any visible top-level
    window means a dialog is blocking the command line.**

    (Also: in the hidden main window's title a `*` after the file name means unsaved
    changes. That is another useful signal, returned here as well.)
    """
    if os.name != "nt":
        return []

    # PowerShell plus a little C#: EnumWindows is a Win32 API that PowerShell lacks.
    script = r"""
Add-Type @'
using System;using System.Text;using System.Runtime.InteropServices;
public class MxWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  public static string Visible(uint want) {
    var sb = new StringBuilder();
    EnumWindows((h,l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (pid == want && IsWindowVisible(h)) {
        var t = new StringBuilder(400); GetWindowText(h, t, 400);
        if (t.Length > 0) sb.AppendLine(t.ToString());
      }
      return true;
    }, IntPtr.Zero);
    return sb.ToString();
  }
}
'@
[MxWin]::Visible(%d)
""" % pid

    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip()
        return [x for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def find_running_cubemx() -> list[int]:
    """Find the process ids of the CubeMX instances currently running.

    CubeMX is single-instance: while the GUI is open our session is blocked, and the
    failure message is very hard to read. So check before starting and say it plainly.
    """
    if os.name != "nt":
        try:
            r = subprocess.run(["pgrep", "-f", "STM32CubeMX"],
                               capture_output=True, text=True, timeout=15)
            return [int(x) for x in r.stdout.split() if x.isdigit()]
        except Exception:
            return []
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe' or Name='javaw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*STM32CubeMX*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=40)
        return [int(x) for x in (r.stdout or "").split() if x.strip().isdigit()]
    except Exception:
        return []


#: Known dialog title -> hint for the caller.
#:
#: Keys are matched **as substrings** (not for equality): a window title may carry a
#: file name, a `*` (unsaved marker) and similar affixes, and measured titles of the
#: same dialog are not always word for word identical.
DIALOG_HINTS = {
    "Save Project":
        "CubeMX is showing the \"save project\" dialog. The most common measured cause "
        "is that **the target .ioc is held by another process, or that directory is "
        "not writable** (another program having the file open, or a directory without "
        "write permission, both do this).\n"
        "  Look at the target path in the \"command sent\" above: is the file open in "
        "another program? Does the directory exist, and is it writable?\n"
        "  Another possibility is that the save path points at a missing directory.",
    "Warning: Load Project":
        "CubeMX is asking \"this project changed, save it first?\": the project was "
        "probably dirty before some config load. Code should call "
        "cubemx1.write.safe_load instead of a bare load.",
}


def dialog_hint(titles: list) -> str:
    """Turn a dialog title into "what this dialog says and where to look".

    **Why it is a pure function**: this logic used to be a hardcoded **exact** match
    (`known.get(titles[0], ...)`) that recognised one dialog only. Measured:
    `project save --to <a file locked exclusively>` raises `Save Project` and fell
    through to the generic "this is a CubeMX dialog", so the caller **could not see
    that the target file was in use** and had to guess.

    Extracted like this, the "title -> hint" logic can be verified **without really
    raising a dialog** (the check no longer needs to start CubeMX).

    :param titles: the title list from `visible_windows()` (in enumeration order).
    :returns: the matching hint; when nothing matches, a generic sentence (it
        **never guesses**).
    """
    for t in titles or []:
        low = str(t).lower()
        for key, hint in DIALOG_HINTS.items():
            if key.lower() in low:
                return hint
    return ("this is a CubeMX dialog and it is blocking the command line. The title "
            "is not recognised, so what it wants cannot be said.")


class Session:
    """One long-lived CubeMX session.

    Usage::

        with Session(machine) as s:
            s.run("config load my.ioc")
            r = s.run("get version")
            print(r.output, r.elapsed_ms)

    A session is stateful and the CubeMX command table depends on the project state
    (measured: project-specific commands disappear from the table once the project is
    closed). So **never cache the command table**; ask for it when it is needed.
    """

    def __init__(self, machine: Machine, starter: list[str] | None = None,
                 hide_window: bool = True, require_exclusive: bool = False,
                 work_dir: Path | None = None):
        machine.require_ok()

        # About "single instance": an early version said "only one CubeMX at a time",
        # but that was about `STM32CubeMX.exe`, which is only a launcher shell. We
        # start `java -jar` directly with the bundled JRE, and measured: **two
        # sessions run at the same time and both work**.
        # So the check is off by default; it would misread "the previous session is
        # still shutting down" as a conflict. Only a caller that really needs
        # exclusivity passes require_exclusive=True.
        if require_exclusive:
            busy = find_running_cubemx()
            if busy:
                raise BusyError(
                    "a CubeMX instance is already running (pid "
                    + ", ".join(str(p) for p in busy)
                    + ").\nexclusivity was requested, so close it first."
                )

        self.machine = machine
        self.starter = list(starter if starter is not None else DEFAULT_STARTER)
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._closed = False
        # How far the buffer was already consumed. The next command reads from here,
        # so a leftover marker of the previous command is not taken as its own.
        self._consumed = 0

        # The start-up script is written to a temporary file with \n line ends and no
        # BOM: CubeMX is sensitive to a BOM.
        tmpdir = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
        tmpdir.mkdir(parents=True, exist_ok=True)
        # It must be an absolute path: the CubeMX working directory is its own
        # installation directory, so a relative path makes it miss the start-up
        # script and the startup times out -- and the error it reports (no MX> or no
        # marker) points nowhere near the cause.
        self._starter_file = (tmpdir / f"cubemx1_starter_{os.getpid()}.txt").resolve()
        self._starter_file.write_text(
            "".join(line + "\n" for line in self.starter),
            encoding="utf-8", newline="\n",
        )

        argv = ([str(machine.java)] + JVM_ARGS
                + ["-jar", str(machine.mx_exe), "-q", str(self._starter_file)])
        self._argv = argv

        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=str(machine.home),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_window_options(hide_window),
            )
        except OSError as e:
            raise SessionError(
                f"failed to start CubeMX: {e}\n"
                f"the command was:\n  {' '.join(argv[:2])} ... -jar STM32CubeMX.exe -q <script>"
            ) from e

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---- internals ----

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                with self._lock:
                    self._buf.append(line.rstrip("\n"))
        except Exception:
            pass

    def _text_from(self, index: int) -> str:
        with self._lock:
            return "\n".join(self._buf[index:])

    def _size(self) -> int:
        with self._lock:
            return len(self._buf)

    def _safe_lines(self) -> list[str]:
        with self._lock:
            return list(self._buf)

    def _check_alive(self) -> None:
        if self._closed:
            raise SessionError("the session is already closed")
        if self._proc.poll() is not None:
            raise SessionError(
                f"the CubeMX process has exited (exit code {self._proc.returncode}).\n"
                f"last output received:\n{self._text_from(0)[-1500:]}"
            )

    # ---- public ----

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT) -> float:
        """Wait for the start-up script to finish. Returns how long it waited (s).

        The criterion is one end marker per line of the start-up script. Measured cold
        start: about 9-15 seconds. Note that ``-q`` mode **prints no MX> prompt**, so
        the prompt cannot be used here.
        """
        want = len(self.starter)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._proc.poll() is not None:
                raise SessionError(
                    "CubeMX exited half way through the start-up script.\n"
                    f"exit code {self._proc.returncode}\n"
                    f"output:\n{self._text_from(0)[-2000:]}"
                )
            text = self._text_from(0)
            if len(protocol.STATUS_RE.findall(text)) >= want:
                # The startup output is fully read here, so move the read position
                # past it; each later command then reads only its own slice.
                #
                # Without this the first user command would re-read all startup output,
                # which holds CubeMX's own startup messages (log4j configuration notes,
                # Java warnings -- not in log4j format, so they cannot be filtered) plus
                # the bare OK of the start-up script -- so the first command would
                # immediately "see" that old marker and return startup noise.
                self._consumed = self._size()
                return time.time() - t0
            time.sleep(0.1)
        raise SessionError(
            f"waited {timeout:.0f} seconds and the start-up script has not finished; "
            f"CubeMX may be stuck.\n"
            f"output received so far:\n{self._text_from(0)[-2000:]}"
        )

    def _drain(self, quiet: float = 1.0, hard: float = 60.0) -> None:
        """Wait for the output to go quiet and realign the buffer.

        When it is needed: a command failed to parse, which means our view and the
        CubeMX output are misaligned. The next command must not be sent right away, or
        it would take the leftover output of the previous one as its own. Wait for
        quiet first, then continue.
        """
        t0 = time.time()
        last_size, last_change = -1, time.time()
        while time.time() - t0 < hard:
            size = self._size()
            if size != last_size:
                last_size, last_change = size, time.time()
            if time.time() - last_change >= quiet:
                return
            time.sleep(0.05)

    def run(self, command: str, timeout: float = COMMAND_TIMEOUT) -> protocol.Result:
        """Send one command and wait until it finishes.

        There is one completion criterion: **the end marker arrives**
        (``<ms> OK/KO``).

        Never judge completion by "how long there was no output": CubeMX stays quiet
        for a long time while running a long command (loading a project prints almost
        nothing for 20-40 seconds). An early version got this wrong and declared a
        command that was still loading a failure after 4 seconds.

        :param command: the CubeMX command as is, for example ``get version``
        :raises CubeMxCommandError: CubeMX answered KO
        :raises NoStatusMarkerError: the timeout expired without an end marker
        """
        self._check_alive()

        # Read from here. The current end of the buffer cannot be used, because the
        # leftover output of the previous command (its end marker in particular) may
        # not have arrived yet. If that marker falls into this read window, this
        # command "sees" the old marker immediately and returns the previous output --
        # measured: asking for SPI1 parameters returned another command's result.
        # So the start is the position consumed last time.
        start = self._consumed

        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(command + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise SessionError(f"failed to write to CubeMX; the session may be dead: {e}") from e

        t0 = time.time()
        text = ""
        marker_seen_at = None
        last_stall_check = t0
        # **Script mode waits for the numbered total marker** (measured 2026-09-29):
        # inside `script <file>` **every command** prints a **bare** `OK`/`KO`, and
        # the run ends with one **numbered** total marker. A measured raw output (5
        # commands) ended like this::
        #
        #     9| get mcu package
        #    10| LQFP176
        #    11| OK            <- this command's own marker
        #    12| 28 OK         <- the **total** one (still OK even if a command said KO)
        #
        # This used to "wait 0.25 s after any marker and return", so a script with one
        # **slow command** (for example `config saveas`) **returned half way and
        # truncated the output silently**. Measured: a script of that shape broke at
        # command 4 (the raw text stopped at the log lines of `config saveas`).
        want_numbered = command.strip().startswith("script ")
        while True:
            text = self._text_from(start)
            hit = protocol.STATUS_RE.search(text)
            if hit is not None and want_numbered:
                # script: only the numbered one counts (per-command markers are bare,
                # they do not mean "finished")
                hit = next((m for m in protocol.STATUS_RE.finditer(text)
                            if m.group(1) is not None), None)
            if hit is not None:
                # Seeing a marker **does not** mean we may leave: the marker of the
                # previous command may arrive late and land in this window. So wait a
                # little longer; if another marker shows up, the last one wins (the
                # parser takes the last one anyway).
                # This extra wait is short (0.25 s) and only matters when output really
                # is late.
                if marker_seen_at is None:
                    marker_seen_at = time.time()
                elif time.time() - marker_seen_at > 0.25:
                    break
            else:
                marker_seen_at = None

            now = time.time()
            elapsed = now - t0
            if elapsed > timeout:
                break

            # When the marker does not come, first check for a dialog instead of
            # waiting out the timeout.
            #
            # Why: the CubeMX GUI and command line are the same process, so a modal
            # dialog blocks the command line: the command does not return and no marker
            # appears. Measured (config load with unsaved changes) this looks like
            # "stuck for hundreds of seconds, then KO with no message", which leaves no
            # way to guess the cause. Checking the windows here and reporting the dialog
            # title turns guessing into reading.
            if (elapsed > STALL_HINT_AFTER
                    and now - last_stall_check > STALL_RECHECK_EVERY):
                last_stall_check = now
                titles = visible_windows(self._proc.pid)
                if titles:
                    hint = dialog_hint(titles)
                    raise SessionError(
                        "no end marker arrived and CubeMX has a dialog open:\n"
                        "  %s\n"
                        "\n"
                        "%s\n"
                        "\n"
                        "the dialog blocks the command line: until it is dismissed the "
                        "command never returns.\n"
                        "next: look at the desktop and dismiss that dialog"
                        " (take a screenshot when it cannot be seen),"
                        " then restart the session with `cubemx1 stop`.\n"
                        "\n"
                        "command sent: %s\n"
                        "content received (last 300 characters):\n%s"
                        % (titles[0], hint, command, text[-300:]))

            self._check_alive()
            time.sleep(POLL_INTERVAL)

        try:
            result = protocol.parse(command, text)
        except Exception:
            # Misaligned. Realign the buffer first so the next command is clean.
            self._drain()
            raise

        # Where this read stopped is where the next one starts; leftover output stays
        # out of it
        self._consumed = self._size()

        if not result.ok:
            raise CubeMxCommandError(command, result.elapsed_ms, result.output)
        return result

    def try_run(self, command: str, timeout: float = COMMAND_TIMEOUT) -> protocol.Result:
        """Like :meth:`run` but does not raise on failure; a KO is returned as a
        result.

        For cases where "the command may not exist", such as probing which commands a
        machine offers.
        """
        try:
            return self.run(command, timeout=timeout)
        except CubeMxCommandError as e:
            return protocol.Result(
                command=command, ok=False, elapsed_ms=e.elapsed_ms,
                output=e.output, log_lines=[], raw=e.output,
            )

    def close(self, timeout: float = 30.0) -> None:
        """Close the session. Send exit and let it go; kill it when it does not."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None and self._proc.stdin:
                self._proc.stdin.write("exit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=timeout)
        except Exception:
            pass
        finally:
            if self._proc.poll() is None:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=10)
                except Exception:
                    pass
            try:
                self._starter_file.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self) -> "Session":
        self.wait_ready()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
