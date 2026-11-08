"""The background daemon: keeps the CubeMX session in a separate process so several
commands can share it.

## Why it is needed

:class:`~cubemx1.session.Session` is an in-process object. Creating a session per CLI
command would cold-start CubeMX for every command (8-15 seconds), which defeats the
point of a resident session.

So a separate process holds the session and every command connects to it. That is
what the ``--port`` form of the start command provides.

## How the connection works

- On start the process picks a free port (listening on 127.0.0.1 only) and writes it
  into the state file ``~/.cubemx1/session.json``.
- A client reads the state file, connects, sends one JSON line and receives one.
- ``--port`` may set the port explicitly, overriding the state file.

The state file looks like this::

    {"port": 51873, "pid": 12345, "started_at": 1758800000.0, "starter": ["get version"]}

## Message format

Request (one JSON object per line)::

    {"command": "get version"}         run one CubeMX command
    {"op": "ping"}                     liveness probe
    {"op": "stop"}                     ask it to exit

Response::

    {"ok": true, "elapsed_ms": 1, "output": "6.17.0", "log_lines": []}

## Why it is not "the user must start it first"

An assistant cannot be relied on to remember a preparation step.
So the client **starts one automatically** when none is running. The ``start`` /
``stop`` / ``status`` commands are for people who want explicit control; they are not
a precondition.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import protocol
from .errors import CubeMxCommandError, SessionError
from .machine import load as load_machine
from .session import Session

STATE_DIR = Path.home() / ".cubemx1"
STATE_FILE = STATE_DIR / "session.json"

# Commands whose output goes to the log are captured through a redirection file.
# CubeMX keeps that file open so it cannot be deleted; they go into a dedicated
# directory that is cleaned up on shutdown.
CAPTURE_DIR = Path(tempfile.gettempdir()) / "cubemx1_capture"

HOST = "127.0.0.1"
# Upper bound for handling one command, including slow operations like a project load
HANDLE_TIMEOUT = 600.0


def _write_state(port: int, pid: int, starter: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "port": port,
        "pid": pid,
        "started_at": time.time(),
        "starter": starter,
    }, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_state() -> dict | None:
    """Read the state file. Returns None when it is missing or incomplete."""
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict) or "port" not in d:
        return None
    return d


def clear_state() -> None:
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Daemon:
    def __init__(self, starter: list[str] | None = None,
                 hide_window: bool = True, require_exclusive: bool = False,
                 project: str | None = None):
        self.machine = load_machine()
        self.starter = list(starter or ["get version"])
        print(f"[cubemx1] starting the CubeMX session (start-up script: {self.starter})...",
              file=sys.stderr, flush=True)
        t0 = time.time()
        self.session = Session(self.machine, starter=self.starter,
                               hide_window=hide_window,
                               require_exclusive=require_exclusive)
        self.session.wait_ready()
        print(f"[cubemx1] session ready after {time.time()-t0:.1f} seconds",
              file=sys.stderr, flush=True)

        # A project must be loaded **interactively**, never from the start-up script.
        #
        # Crucial measurement: a config load run from the start-up script does not
        # register the project-specific commands, while an interactive config load
        # does. The difference:
        #   - loaded from the script: `clock set` and the whole `dma` group are absent
        #   - loaded interactively:   both are present
        # So start --project must take this path, otherwise the user finds clock/dma
        # unusable for no visible reason.
        if project:
            print(f"[cubemx1] loading project {project} ...", file=sys.stderr, flush=True)
            # `Firmware Location Error.`: when the `.ioc` has
            # `DefaultFWLocation=false` and the directory that
            # `CustomerFirmwarePackage` points at is missing (or the value is empty),
            # CubeMX raises this dialog while loading the project -- **measured: 20.2
            # seconds blocked** -- and after the load it **resets both keys** to their
            # defaults (`DefaultFWLocation=true`, `CustomerFirmwarePackage=` empty).
            # Evidence in `cmds/write.check_firmware_location`.
            # Handling: recognise it first (warn), then **temporarily** switch on the
            # popup suppression CubeMX provides, and switch it off right after the load
            # (leaving it on would also answer `Warning: Load Project`).
            fw = None
            try:
                from .write import check_firmware_location
                fw = check_firmware_location(project)
            except Exception:
                fw = None
            wrapped = False
            if fw:
                print("[cubemx1] note: %s. CubeMX will raise a dialog while loading "
                      "and reset both keys to their defaults; the popup suppression is "
                      "on for that (so it cannot hang)." % fw["detail"],
                      file=sys.stderr, flush=True)
                try:
                    self.session.run("popupwrapper set", timeout=60)
                    wrapped = True
                except Exception:
                    wrapped = False
            try:
                self.session.run('config load "%s"' % project, timeout=300)
                # Remember which project is held (needed by the `current-project` op)
                self.project_path = str(project)
            finally:
                if wrapped:
                    try:
                        self.session.run("popupwrapper unset", timeout=60)
                    except Exception:
                        pass
            print("[cubemx1] project loaded (interactively)", file=sys.stderr, flush=True)

        self.port = _free_port()
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((HOST, self.port))
        self._srv.listen(16)

        # Job queue. CubeMX executes one command at a time, so every request is queued
        # and run in order. Callers may send requests concurrently; they queue here and
        # run first come, first served. The queue depth is returned to the caller so a
        # wait is visible.
        self._jobs: queue.Queue = queue.Queue()
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()

        _write_state(self.port, os.getpid(), self.starter)
        print(f"[cubemx1] listening on {HOST}:{self.port}", file=sys.stderr, flush=True)

    # ---- queue ----

    def _work_loop(self) -> None:
        """The only worker thread: take a job from the queue and run it.

        There is a single worker because CubeMX itself handles one command at a time.
        Concurrency is solved by queueing, not by starting more instances.
        """
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            with self._busy_lock:
                self._busy += 1
            try:
                job["result"] = self._execute(job["command"],
                                              job.get("capture_log", False))
            except Exception as e:                      # catch-all: keep the worker alive
                job["result"] = {"ok": False, "output": f"error while running: {e}"}
            finally:
                with self._busy_lock:
                    self._busy -= 1
                job["done"].set()

    def _execute(self, command: str, capture_log: bool = False) -> dict:
        """Run one command.

        :param capture_log: set True when this command writes its output to the
            **log** instead of standard output. The daemon then appends the
            ``> file`` redirection and returns the file content as the output. Which
            commands need this is described by the three cases in
            :mod:`cubemx1.protocol`.
        """
        if capture_log:
            return self._execute_capturing_log(command)
        try:
            r = self.session.run(command, timeout=HANDLE_TIMEOUT)
            return {"ok": True, "elapsed_ms": r.elapsed_ms,
                    "output": r.output, "log_lines": r.log_lines,
                    # **The raw text goes out too** (2026-09-29): `output` is cleaned
                    # (end markers and log lines removed), while the fast path needs the
                    # **per-command** OK/KO, which only the raw text holds. See
                    # `cubemx1.batch.parse_script`.
                    "raw": r.raw}
        except CubeMxCommandError as e:
            return {"ok": False, "elapsed_ms": e.elapsed_ms,
                    "output": e.output, "log_lines": []}
        except SessionError as e:
            return {"ok": False, "fatal": True, "output": str(e)}

    def _execute_capturing_log(self, command: str) -> dict:
        """Run a command whose output goes to the log and bring the log back as the
        output.

        Why a separate path: the content of commands such as `get mode_param_list`
        **goes to the log** (measured), and standard output carries only the end
        marker. The redirection CubeMX provides sends the log to a file, so reading
        that file is enough.

        Two pitfalls (both measured):
        - the path **must not be quoted**: whatever follows `>` is taken as the file
          name
        - CubeMX keeps the file open, so it can be neither deleted nor overwritten;
          every command therefore uses a new file name
        """
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        f = CAPTURE_DIR / f"cap_{os.getpid()}_{time.time_ns()}.txt"
        full = command + protocol.redirect_suffix(f)
        try:
            r = self.session.run(full, timeout=HANDLE_TIMEOUT)
        except CubeMxCommandError as e:
            return {"ok": False, "elapsed_ms": e.elapsed_ms,
                    "output": e.output, "log_lines": [], "from_log": True}
        except SessionError as e:
            return {"ok": False, "fatal": True, "output": str(e)}

        # the file may take a moment to appear on disk
        text = ""
        for _ in range(20):
            if f.is_file():
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                if text:
                    break
            time.sleep(0.1)

        return {"ok": True, "elapsed_ms": r.elapsed_ms, "output": text,
                "log_lines": r.log_lines, "from_log": True}

    def _submit(self, command: str, capture_log: bool = False) -> dict:
        """Queue one command and wait for it to finish."""
        job = {"command": command, "done": threading.Event(), "result": None,
               "capture_log": capture_log}
        t_queued = time.time()
        before = self._jobs.qsize()
        self._jobs.put(job)
        job["done"].wait()
        res = job["result"] or {"ok": False, "output": "no result was produced"}
        # The field meanings must be exact:
        # - elapsed_ms is the command execution time CubeMX itself reports
        # - total_ms is the whole time from submit to answer, mostly spent on the
        #   CubeMX input polling (it polls for input about once per second), so it
        #   stays around 0.5-1 second
        # - queue_ahead is the number of commands queued before this one (only
        #   meaningful under concurrency)
        res["total_ms"] = round((time.time() - t_queued) * 1000)
        res["queue_ahead"] = before
        return res

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            with self._busy_lock:
                busy = self._busy
            return {"ok": True, "pong": True, "pid": os.getpid(),
                    "port": self.port,
                    "alive": self.session._proc.poll() is None,
                    "queued": self._jobs.qsize(), "busy": busy}
        if op == "set-project":
            # Record which .ioc is held. **CubeMX offers no reliable query for it**
            # (measured: `get dest_path` returns null and `project get` cannot read
            # the path), so whoever loads it (`start --project` / `safe_load`) tells
            # the daemon.
            self.project_path = str(req.get("path") or "") or None
            return {"ok": True, "project_path": self.project_path}
        if op == "current-project":
            return {"ok": True, "project_path": getattr(self, "project_path", None)}
        if op == "stop":
            # Delete the state file before answering. That way, when the caller gets
            # the answer, new commands can no longer find this port and nothing is sent
            # into a session that is closing.
            clear_state()
            self._stop.set()
            return {"ok": True, "stopping": True}

        command = req.get("command")
        if not command:
            return {"ok": False, "output": "the request has neither op nor command"}
        return self._submit(command, bool(req.get("capture_log")))

    def serve(self) -> int:
        self._srv.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                # Also watch CubeMX: when it dies, shut down, so no state file is left
                # behind that nothing can connect to
                if self.session._proc.poll() is not None:
                    print("[cubemx1] the CubeMX process exited, the daemon shuts down too",
                          file=sys.stderr, flush=True)
                    break
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn,),
                             daemon=True).start()
        self.shutdown()
        return 0

    def _serve_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(HANDLE_TIMEOUT)
            f = conn.makefile("rw", encoding="utf-8", newline="\n")
            try:
                line = f.readline()
                if not line:
                    return
                req = json.loads(line)
                resp = self.handle(req)
                f.write(json.dumps(resp, ensure_ascii=False) + "\n")
                f.flush()
            except Exception as e:
                try:
                    f.write(json.dumps({"ok": False,
                                        "output": f"daemon error: {e}"}) + "\n")
                    f.flush()
                except Exception:
                    pass

    def shutdown(self) -> None:
        print("[cubemx1] shutting down", file=sys.stderr, flush=True)
        clear_state()
        # CubeMX may still hold the redirection files, so this is best effort
        for f in CAPTURE_DIR.glob("cap_*.txt"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._srv.close()
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cubemx1-daemon",
                                description="cubemx1 background daemon "
                                            "(normally started by the client)")
    p.add_argument("--starter", action="append", default=None,
                   help="commands for the start-up script, repeatable. "
                        "Defaults to one get version line as a warm-up")
    p.add_argument("--show-window", action="store_true",
                   help="do not hide the window (hidden by default; what really keeps "
                        "the main window away is -q mode)")
    p.add_argument("--project", default=None,
                   help="load this project interactively after start-up")
    p.add_argument("--require-exclusive", action="store_true",
                   help="require exclusivity: refuse to start when a CubeMX is "
                        "already running (off by default, because several sessions "
                        "were measured to coexist)")
    a = p.parse_args(argv)

    try:
        d = Daemon(starter=a.starter, hide_window=not a.show_window,
                   require_exclusive=a.require_exclusive, project=a.project)
    except Exception as e:
        print(f"[cubemx1] cannot start: {e}", file=sys.stderr, flush=True)
        return 1
    return d.serve()


if __name__ == "__main__":
    sys.exit(main())
