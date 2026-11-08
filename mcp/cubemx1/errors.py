"""Unified error types.

Rule: an error message must read plainly and must say what to do next.
"""


class CubemxError(Exception):
    """Base class for every error raised by this tool."""


class MachineError(CubemxError):
    """The local path configuration is wrong.

    For example CubeMX is not installed, a path is misspelled, or Java cannot be
    found. Such errors must be caught before CubeMX starts, otherwise the error
    that comes out is very hard to read.
    """


class SessionError(CubemxError):
    """A problem with the long-lived session.

    For example a startup timeout, the session died half way, or the process was
    already gone when a command was sent.
    """


class CubeMxCommandError(CubemxError):
    """CubeMX failed to run a command (it answered KO).

    :param command: the raw command sent to CubeMX
    :param elapsed_ms: execution time in milliseconds reported by CubeMX
    :param output: what the command produced (log noise removed)
    """

    def __init__(self, command: str, elapsed_ms: int | None, output: str):
        self.command = command
        self.elapsed_ms = elapsed_ms
        self.output = output
        detail = f" (CubeMX reported {elapsed_ms} ms)" if elapsed_ms is not None else ""
        msg = f"CubeMX failed to run: {command}{detail}"
        if output.strip():
            msg += f"\nits output was:\n{output.strip()}"
        msg += "\nnext: check the argument count (CubeMX prints the usage and answers KO when it is wrong)."
        super().__init__(msg)


class BusyError(CubemxError):
    """A CubeMX instance is already running.

    CubeMX allows a single instance: while the GUI is open, the command line is
    blocked.
    """


class NoStatusMarkerError(CubemxError):
    """The command was sent but no end marker arrived.

    Normally CubeMX prints `<milliseconds> OK/KO` at the end of every command.
    If it never arrives, the session is probably unhealthy.
    """
