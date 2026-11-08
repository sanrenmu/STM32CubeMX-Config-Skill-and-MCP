"""Clock solver inspection: solve target frequencies without writing an .ioc.

This is the same solver the compiler uses (joint search over all targets, then an
independent recomputation of every target from the final RCC values). It writes
no file and does not start CubeMX, so it is safe to call while designing a
target JSON.

Basis: the compiler's own entry point is `iocc.buildclock.clock_targets`; this
command is a thin adapter over it, exactly like the other `cmd_*` functions.
"""

from __future__ import annotations

from types import SimpleNamespace

from ..errors import CubemxError
from .base import EXIT_FAIL, EXIT_OK, Out, _join, machine


def _split_pair(text: str, what: str) -> tuple[str, str]:
    if "=" not in str(text):
        raise CubemxError("%s must look like NAME=VALUE (got %r)" % (what, text))
    name, value = str(text).split("=", 1)
    name, value = name.strip(), value.strip()
    if not name or not value:
        raise CubemxError("%s must look like NAME=VALUE (got %r)" % (what, text))
    return name, value


def _rcc_stub(machine_handle, refname: str):
    """The RCC parameter names of one device: enough for name mapping, no process."""
    from ..clockvalues import value_model

    names = set()
    for item in value_model(machine_handle, refname)["elements"]:
        for name in str(item.get("ref_parameter") or "").split(","):
            if name.strip():
                names.add(name.strip())
    return SimpleNamespace(
        parameters=[SimpleNamespace(name=name) for name in sorted(names)])


def _parse_targets(raw_targets) -> list:
    wanted = []
    for text in raw_targets or []:
        name, mhz = _split_pair(text, "--target")
        try:
            value = float(mhz)
        except ValueError:
            raise CubemxError("--target %r has a non-numeric frequency" % text)
        if value <= 0:
            raise CubemxError("--target %r: the frequency must be > 0" % text)
        wanted.append({"parameter": name, "peripheral": None, "mhz": value})
    if not wanted:
        raise CubemxError("pass at least one --target NAME=MHZ")
    return wanted


def _checks(machine_handle, refname: str, wanted: list, params: dict) -> list:
    """Independent recomputation of every target from the final RCC values."""
    from .. import clocksolve

    out = []
    for item in wanted:
        got = clocksolve.forward_from_rcc(machine_handle, refname,
                                          item["parameter"], params)
        want = item["mhz"] * 1e6
        final = got.get("hz")
        out.append({
            "parameter": item["parameter"],
            "requested_hz": want,
            "final_hz": final,
            "headroom_hz": None if final is None else final - want,
            "ok": final is not None and abs(final - want) <= max(1.0, want * 1e-9),
            "reason": got.get("reason"),
        })
    return out


def cmd_clock_solve(args, out: Out) -> int:
    from ..iocc import buildclock

    try:
        machine_handle = machine()
        refname = args.device
        wanted = _parse_targets(args.target)
        explicit = dict(_split_pair(item, "--param") for item in (args.param or []))
        device = SimpleNamespace(refname=refname)
        rcc = _rcc_stub(machine_handle, refname)
        params, notes = buildclock.clock_targets(
            machine_handle, device, rcc, wanted, explicit=explicit or None)
        checks = _checks(machine_handle, refname, wanted, params)
    except CubemxError as exc:
        payload = {"ok": False, "kind": getattr(exc, "kind", None),
                   "path": getattr(exc, "path", None), "error": str(exc)}
        out.emit_error(payload, "clock solve failed: %s" % exc)
        return EXIT_FAIL
    payload = {
        "ok": all(row["ok"] for row in checks),
        "device": refname,
        "targets": wanted,
        "explicit": explicit,
        "rcc": params,
        "notes": notes,
        "checks": checks,
    }
    lines = ["device   %s" % refname]
    for item in wanted:
        lines.append("target   %s = %s MHz" % (item["parameter"], item["mhz"]))
    lines.append("status   solved (writes nothing; run ioc build to write the .ioc)")
    for name, value in sorted(params.items()):
        lines.append("rcc      %s=%s" % (name, value))
    for row in checks:
        got = "unreadable" if row["final_hz"] is None else "%.6g Hz" % row["final_hz"]
        lines.append("check    %s: final RCC gives %s%s"
                     % (row["parameter"], got,
                        "" if row["ok"] else " (MISMATCH)"))
    for note in notes:
        lines.append("note     %s" % note)
    out.emit(payload, _join(lines))
    return EXIT_OK if payload["ok"] else EXIT_FAIL
