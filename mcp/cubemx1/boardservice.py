"""Application layer for board queries: catalogue identity + seed configuration + native BSP definition.

Layering: probe only owns the static model (device / IP / board catalogue and BSP
Modes) and iocc.import_ioc turns a seed into a target; composing the two is this
layer's job, so that probe never depends on the compiler.
"""

from __future__ import annotations

from pathlib import Path

from .iocc import import_ioc
from .probe import boards as boards_query
from .probe import query as probe_query


def _seed_config(machine, seed: Path) -> dict:
    """Summary of a board seed configuration: instances/modes in use, pins, clock inputs."""
    target = import_ioc.import_ioc(machine, seed)["target"]
    ips = []
    for entry in target.get("ips") or []:
        modes = [item.get("name") for item in (entry.get("modes") or [])
                 if item.get("name")]
        if not modes and entry.get("mode"):
            modes = [entry["mode"]]
        ips.append({"instance": entry["instance"], "modes": modes,
                    "params": dict(entry.get("params") or {})})
    pins = [{"pin": item.get("pin"), "signal": item.get("signal")}
            for item in target.get("pins") or []]
    clock = dict(((target.get("clock") or {}).get("params")) or {})
    clock_inputs = {name: value for name, value in clock.items()
                    if name.endswith("_VALUE") and "Freq" not in name
                    and not value.startswith("=")}
    return {"seed": Path(str(seed)).name,
            "ips": ips,
            "pins": pins,
            "clock_inputs": clock_inputs}


def board(machine, wanted: str) -> dict:
    """Complete view of one board: catalogue identity + seed configuration + native BSP definition."""
    ctx = boards_query.board_context(machine, wanted)
    detail = ctx["detail"]
    item = dict(detail) if detail else {"name": ctx["name"], "revision": None, "vendor": None}
    item["id"] = detail["id"] if detail else ctx["id"]
    item.setdefault("mounted_device", ctx.get("mounted_device"))
    item["resolved"] = detail is not None
    item["ambiguous"] = ctx["ambiguous"]
    item["seeds"] = list(ctx.get("seeds") or [])
    item["has_bsp"] = ctx["has_bsp"]
    if ctx.get("seed") is not None:
        try:
            item["config"] = _seed_config(machine, ctx["seed"])
        except Exception as exc:
            item["config"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    if ctx.get("has_bsp"):
        try:
            item["bsp"] = probe_query.describe(machine, board=ctx["id"], ip=ctx["name"])
        except Exception as exc:
            item["bsp"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "board": item}
