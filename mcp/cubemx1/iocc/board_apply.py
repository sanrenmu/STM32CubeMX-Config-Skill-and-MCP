"""board section: apply the seed by board id (defaults) or keep it as is (materialized).

Data sources: the PDSC in the install dir boardmanager (board name/device/revision) and boards/*.ioc (seed).
A seed is an ordinary .ioc, so 'apply defaults' = decode the seed -> then override with the user's explicit fields (the same edit merge semantics).

Boundaries (measured):
- board identity is name@revision@vendor, not the list position;
- board seeds have no ProjectManager keys, so project options are not inherited from the board;
- BSP components are only generated when the install dir has bsp definitions for that board; this tool does not generate on-board device drivers and only says so in the report.
"""

from __future__ import annotations

from ..probe import boards as board_query
from . import edit, import_ioc, options
from .errors import IocConflict, IocUnknown

#: sections not used when applying a seed (the caller owns these in the user target).
MERGE_SECTIONS = ("device", "project", "pins", "ips", "clock", "nvic", "dma",
                  "rtos", "constants")


def _user_sections(target: dict) -> dict:
    section = {}
    for name in MERGE_SECTIONS:
        value = target.get(name)
        if value in (None, {}, []):
            continue
        section[name] = value
    return section


def apply(machine, target: dict) -> tuple:
    """Return (new target, notes). target must be a normalized target (with a board section)."""
    board = target.get("board")
    if not board:
        return target, []
    ctx = board_query.board_context(machine, board["id"])
    notes = []
    name = ctx["name"]
    detail = ctx["detail"]
    mounted = ctx["mounted_device"]
    refname = (target.get("device") or {}).get("refname")
    if refname and mounted and refname != mounted:
        raise IocConflict(
            "board %s carries %s, but the target names device %s; the two disagree"
            % (ctx["id"], mounted, refname), "board.id")
    if ctx.get("has_bsp"):
        notes.append(
            "board %s has native BSP definitions: BSP instances and parameters are written to ips from Modes;"
            "on-board driver assembly is not part of this tool" % name)
    else:
        notes.append("board %s has no BSP definition; no BSP component is generated" % name)

    if board["initialize"] == "materialized":
        # importing an already expanded project: the seed is not needed, and a unique revision must not be forced just to pick a seed.
        notes.append("materialized: do not re-apply the seed; keep the restored actual configuration")
        return target, notes

    if detail is None:
        raise IocUnknown(
            "board %s has several revisions; applying the seed needs the full id (name@revision@vendor)"
            % name, "board.id")
    seed_file = ctx.get("seed")
    if seed_file is None:
        raise IocUnknown(
            "board %s has no seed file (boards/<board>_Board.ioc)" % ctx["id"],
            "board.id")
    seed_target = import_ioc.import_ioc(machine, seed_file)["target"]
    merged = edit.apply_edit(seed_target, {"contract": "cubemx1.ioc-edit/1",
                                           "set": _user_sections(target)},
                             seed_target.get("contract"))
    merged["board"] = {"id": detail["id"], "initialize": "defaults"}
    # BSP instance in the seed (its name is the board name): keep it when the user declared it explicitly or the seed really carries params/modes,
    # so the build recognizes it through the board context; otherwise (everything off by default) do not write it - the loader adds an empty instance.
    if ctx.get("has_bsp"):
        bsp_ips = [ip for ip in merged.get("ips") or [] if ip["instance"] == name]
        if bsp_ips:
            bsp_ip = bsp_ips[0]
            user_gave = any(ip["instance"] == name for ip in target.get("ips") or [])
            meaningful = bool(bsp_ip.get("params") or bsp_ip.get("mode")
                              or bsp_ip.get("modes"))
            if not (user_gave or meaningful):
                merged["ips"] = [ip for ip in merged.get("ips") or []
                                 if ip["instance"] != name]
    # mark which clock parameters come from the board seed: when there are target frequencies, drop the derived readings among them,
    # and take only source values such as the crystal as input (otherwise stale readings mix with the solver result).
    seed_clock = dict(((seed_target.get("clock") or {}).get("params")) or {})
    user_clock = dict(((target.get("clock") or {}).get("params")) or {})
    inherited = [name for name in seed_clock if name not in user_clock]
    merged_clock = dict(merged.get("clock") or {})
    merged_clock["board_defaults"] = inherited
    merged["clock"] = merged_clock
    # these sections are not seed content and must be carried as is (otherwise the user's allocation policy is swallowed by the board merge).
    for name in ("allocation", "connections"):
        value = target.get(name)
        if value not in (None, {}, []):
            merged[name] = value
    user_extensions = ((target.get("extensions") or {}).get("ioc") or {})
    if user_extensions:
        merged_ext = dict((merged.get("extensions") or {}).get("ioc") or {})
        merged_ext.update(user_extensions)
        merged["extensions"] = {"ioc": merged_ext}
    merged["device"] = dict(merged.get("device") or {})
    if mounted:
        merged["device"]["refname"] = mounted
    notes.append("applied the seed of board %s: %s" % (name, seed_file.name))
    return merged, notes


def project_options(target: dict) -> dict:
    """Default project options a board can provide (none in local seeds, so always empty)."""
    return {}