"""Runtime maintenance commands: version / status / stop.

After the slimming the CLI has only the offline compilation path plus these
three maintenance commands; a separate start is no longer needed: ioc generate
starts the background process and loads the project by itself.
"""

from __future__ import annotations

from .base import EXIT_OK, Out, _join


def cmd_version(args, out: Out) -> int:
    from .. import __version__
    from ..machine import load

    payload = {"cubemx1": __version__, "cubemx": None, "db": None}
    try:
        machine = load()
        payload["cubemx"] = machine.cubemx_version
        payload["db"] = machine.db_version
    except Exception as exc:  # noqa: BLE001 - report the tool version even if this fails
        payload["machine_error"] = "%s: %s" % (type(exc).__name__, exc)
    lines = ["cubemx1 %s" % payload["cubemx1"],
             "CubeMX  %s" % (payload["cubemx"]
                             or "(not read; check ~/.cubemx1/machine.json)"),
             "DB      %s" % (payload["db"] or "-")]
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_status(args, out: Out) -> int:
    from ..client import daemon_status

    st = daemon_status()
    lines = ["daemon   %s" % ("running" if st.get("running") else "not running")]
    if st.get("port"):
        lines.append("port     %s" % st["port"])
    if st.get("project_path"):
        lines.append("project  %s" % st["project_path"])
    if st.get("detail"):
        lines.append(str(st["detail"]))
    out.emit(st, _join(lines))
    return EXIT_OK


def cmd_stop(args, out: Out) -> int:
    from ..client import Client, clear_state, read_state

    st = read_state()
    if not st:
        out.emit({"ok": True, "stopped": False},
                 "no background process is running; nothing to stop.")
        return EXIT_OK
    try:
        Client(ensure_running=False, port=st.get("port")).stop()
    except Exception as exc:  # noqa: BLE001 - the process may already be gone
        clear_state()
        out.emit({"ok": True, "stopped": True,
                  "note": "not reachable, treated as stopped: %s" % exc},
                 "cannot reach the background process; state cleared "
                 "(it may have exited already).")
        return EXIT_OK
    clear_state()
    out.emit({"ok": True, "stopped": True},
             "background process stopped; CubeMX session released.")
    return EXIT_OK
