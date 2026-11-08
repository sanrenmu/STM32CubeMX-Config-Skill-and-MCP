"""Client: connects to the background daemon.

Core rule: **the caller is not expected to start the daemon first.**
If none is running, one is started and then used. The caller only sends commands.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .daemon import HOST, STATE_FILE, clear_state, read_state

# Starting the daemon waits for a cold CubeMX start, measured 8-15 seconds.
# Leave plenty of margin.
DAEMON_BOOT_TIMEOUT = 180.0


def _spawn_daemon(starter: list[str] | None, show_window: bool,
                  require_exclusive: bool, project: str | None = None
                  ) -> subprocess.Popen:
    """Start a daemon that is detached from this process.

    Once detached, the CLI process exiting does not take it down, which is exactly
    what we want: the next command reaches the same session.

    Returns the Popen object so the caller notices an early exit at once instead of
    waiting for the full timeout.
    """
    # `-B`: do not write `__pycache__`. The launchers (`cubemx1.cmd` / `cubemx1.sh`)
    # both pass `-B`, and the background process is **spawned from the CLI** --
    # without it an unpack-and-run package collects cache files in its own directory
    # after a single run (measured: one `version` run creates one).
    args = [sys.executable, "-B", "-m", "cubemx1.daemon"]
    for line in (starter or []):
        args += ["--starter", line]
    if show_window:
        args.append("--show-window")
    if require_exclusive:
        args.append("--require-exclusive")
    if project:
        args += ["--project", project]

    env = os.environ.copy()
    # so the child process can import cubemx1
    pkg_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")

    kwargs: dict = {"env": env, "stdin": subprocess.DEVNULL}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = daemon_log_path()
    if os.name == "nt":
        # DETACHED_PROCESS detaches it from the console; CREATE_NO_WINDOW avoids a
        # window flash
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        kwargs["stdout"] = open(log, "ab")
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["start_new_session"] = True
        kwargs["stdout"] = open(log, "ab")
        kwargs["stderr"] = subprocess.STDOUT

    return subprocess.Popen(args, **kwargs)


def daemon_log_path() -> Path:
    return STATE_FILE.parent / "daemon.log"


def _log_tail(n: int = 1500) -> str:
    p = daemon_log_path()
    if not p.is_file():
        return "(no log file)"
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-n:]
    except Exception as e:
        return f"(cannot read the log: {e})"


def _wait_ready(proc: subprocess.Popen | None = None,
                timeout: float = DAEMON_BOOT_TIMEOUT) -> dict:
    """Wait for the daemon to write its state file.

    Both failure modes must show up at once instead of waiting for the timeout:
    - the daemon died (for example CubeMX did not start): report its real reason
    - the state file exists but the port refuses connections: keep waiting
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"the background process exited right after start (exit code {proc.returncode}).\n"
                f"log tail:\n{_log_tail()}"
            )
        st = read_state()
        if st:
            try:
                with socket.create_connection((HOST, st["port"]), timeout=5):
                    return st
            except OSError:
                pass
        time.sleep(0.3)
    raise TimeoutError(
        f"waited {timeout:.0f} seconds and the background process is still not ready.\n"
        f"log tail:\n{_log_tail()}"
    )


class Client:
    """Connect to the background process and send commands."""

    def __init__(self, ensure_running: bool = True, starter: list[str] | None = None,
                 show_window: bool = False, require_exclusive: bool = False,
                 port: int | None = None, project: str | None = None):
        self.port = port
        st = read_state()
        if st is None and ensure_running:
            clear_state()
            proc = _spawn_daemon(starter, show_window, require_exclusive, project)
            st = _wait_ready(proc)
            self.port = st["port"]
        if self.port is None:
            if st is None:
                raise RuntimeError(
                    "no background process is running.\n"
                    "run `cubemx1 start`, or just send a command and one starts."
                )
            self.port = st["port"]
        self.state = st or {}

    def request(self, payload: dict, timeout: float = 620.0) -> dict:
        try:
            with socket.create_connection((HOST, self.port), timeout=15) as s:
                s.settimeout(timeout)
                f = s.makefile("rw", encoding="utf-8", newline="\n")
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                f.flush()
                line = f.readline()
                if not line:
                    raise RuntimeError("the background process closed the connection without answering")
                return json.loads(line)
        except (ConnectionRefusedError, ConnectionResetError, socket.timeout) as e:
            raise RuntimeError(
                f"cannot connect to the background process (127.0.0.1:{self.port}): {e}\n"
                f"it may have died. Run `cubemx1 status`,"
                f" or `cubemx1 stop` to clear the state and start again."
            ) from e

    # ---- common actions ----

    def run(self, command: str, timeout: float = 620.0,
            capture_log: bool = False) -> dict:
        """Send one command.

        :param capture_log: set True when this command writes its output to the log
            instead of standard output. The daemon then adds the redirection and
            returns the file content as the output. Which commands need it is
            described in :mod:`cubemx1.protocol`.
        """
        return self.request({"command": command, "capture_log": capture_log},
                            timeout=timeout)

    def ping(self) -> dict:
        return self.request({"op": "ping"}, timeout=30)

    def stop(self) -> dict:
        return self.request({"op": "stop"}, timeout=30)

    def set_current_project(self, path) -> dict:
        """Tell the daemon which .ioc is currently held (CubeMX cannot be queried for
        it, only the loader can report it)."""
        return self.request({"op": "set-project", "path": str(path)}, timeout=30)

    def current_project_path(self):
        """The current project path the daemon remembers (None when it has none)."""
        try:
            return self.request({"op": "current-project"},
                                timeout=30).get("project_path")
        except Exception:
            return None


def daemon_status() -> dict:
    """Query the background process status without starting one."""
    st = read_state()
    if not st:
        return {"running": False,
                "detail": "no state file, so no background process is running"}
    try:
        with socket.create_connection((HOST, st["port"]), timeout=5) as s:
            s.settimeout(15)
            f = s.makefile("rw", encoding="utf-8", newline="\n")
            f.write(json.dumps({"op": "ping"}) + "\n")
            f.flush()
            resp = json.loads(f.readline() or "{}")
        return {"running": True, "state": st, "daemon": resp}
    except Exception as e:
        return {"running": False, "state": st,
                "detail": f"the state file exists but the connection fails ({e})."
                          f" this is stale state; run `cubemx1 stop` to clear it."}
