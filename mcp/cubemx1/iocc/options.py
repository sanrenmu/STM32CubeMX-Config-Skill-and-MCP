"""Project options (the 7 ProjectManager keys): type validation + precedence.

Precedence (highest first):
1. the value explicitly set this time;
2. the value imported from an IOC;
3. the value inherited from the board;
4. the device/toolchain default (see DEFAULT).

Basis (measured): these 7 keys are read and written by the project plugin in the installed version, defaults are in the table below; 'board seeds contain no ProjectManager keys', so the third level is in practice empty - it is kept so that a seed carrying options later needs no structural change.
"""

from __future__ import annotations

from .errors import IocUnknown

OPTIONS = {
    "KeepUserCode": ("bool", True),
    "DeletePrevious": ("bool", True),
    "BackupPrevious": ("bool", False),
    "CoupleFile": ("bool", False),
    "NoMain": ("bool", False),
    "HalAssertFull": ("bool", False),
    "FirmwarePackage": ("str", ""),
}


def validate(section) -> dict:
    """Validate one project.options section; return {name: value} (bool stays bool)."""
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise IocUnknown("project.options must be an object", "project.options")
    unknown = sorted(set(section) - set(OPTIONS))
    if unknown:
        raise IocUnknown(
            "unsupported project option: %s (currently only %s)"
            % (", ".join(unknown), ", ".join(sorted(OPTIONS))), "project.options")
    out = {}
    for name, value in section.items():
        kind, _default = OPTIONS[name]
        where = "project.options." + name
        if kind == "bool":
            if not isinstance(value, bool):
                raise IocUnknown(
                    "%s must be a JSON boolean (true/false), not %r" % (name, value), where)
            out[name] = value
        else:
            if not isinstance(value, str):
                raise IocUnknown("%s must be a string, not %r" % (name, value), where)
            out[name] = value
    return out


def resolve(explicit=None, imported=None, inherited=None, defaults=None) -> dict:
    """Compute the final values by precedence; absent options fall back to the default."""
    layers = [defaults or {}, inherited or {}, imported or {}, explicit or {}]
    merged = {}
    for layer in layers:
        merged.update(layer)
    out = {}
    for name, (kind, default) in OPTIONS.items():
        value = merged.get(name, default)
        if kind == "bool":
            if not isinstance(value, bool):
                raise IocUnknown("project option %s has a non-boolean value %r" % (name, value),
                                 "project.options." + name)
        out[name] = value
    return out