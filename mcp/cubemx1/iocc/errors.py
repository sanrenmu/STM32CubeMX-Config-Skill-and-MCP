"""Compiler failure semantics (three kinds, do not conflate).

- unsupported: this field/mechanism is not implemented yet; refuse to compile.
- unknown: not found in the definition data and not given by the target; cannot derive.
- conflict: two conflicting inputs in the target (for example one pin with two signals).
"""


class IocError(Exception):
    kind = "error"

    def __init__(self, message: str, path: str = ""):
        self.path = path
        super().__init__((path + ": " if path else "") + message)


class IocUnsupported(IocError):
    kind = "unsupported"


class IocUnknown(IocError):
    kind = "unknown"


class IocConflict(IocError):
    kind = "conflict"


class TargetError(IocError):
    """Shape error in the target JSON itself (missing fields, unknown fields)."""

    kind = "target"