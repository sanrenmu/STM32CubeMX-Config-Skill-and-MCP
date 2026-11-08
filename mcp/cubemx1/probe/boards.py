"""ST official board query: reads the boardmanager PDSC files and the seed
directory of the installation.

Rules (measured):
- Data directory = `<db>/plugins/boardmanager` (PDSC files and the boards subdirectory);
- Board identity = the PDSC `board name` + `revision` (+ vendor), **never the list position**;
- A seed is a plain `.ioc` (`boards/<prefix>_<board>_<device>_Board.ioc`);
- A BSP component exists only when `boards/bsp/<board>_Modes.xml` exists.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


def _plugin_dir(machine) -> Path:
    home = Path(str(machine.home))
    return home / "db" / "plugins" / "boardmanager"


def _tag(element) -> str:
    tag = element.tag or ""
    return tag.rsplit("}", 1)[-1]


def board_id(name: str, revision: str | None, vendor: str | None = None) -> str:
    parts = [name]
    if revision:
        parts.append(revision)
    if vendor:
        parts.append(vendor)
    return "@".join(parts)


def _iter_boards(pdsc: Path):
    try:
        tree = ET.parse(str(pdsc))
    except (ET.ParseError, OSError):
        return
    for element in tree.getroot().iter():
        if _tag(element) != "board":
            continue
        name = element.get("name")
        if not name:
            continue
        mounted = None
        features = []
        for child in element.iter():
            kind = _tag(child)
            if kind == "mountedDevice" and mounted is None:
                mounted = child.get("Dname")
            elif kind == "feature":
                features.append(child.get("type") or child.get("name"))
        yield {
            "name": name,
            "revision": element.get("revision"),
            "vendor": element.get("vendor"),
            "type": element.get("type"),
            "description": element.get("description") or "",
            "mounted_device": mounted,
            "features": [item for item in features if item],
            "source": pdsc.name,
        }


def seed_paths(machine) -> list:
    """Every seed file in the boards directory (plain .ioc)."""
    boards = _plugin_dir(machine) / "boards"
    if not boards.is_dir():
        return []
    return sorted(boards.glob("*.ioc"))


def _seed_files(machine, names) -> dict:
    """Board name -> list of seed files.

    Seed file names look like <prefix>_<board>_<device>_Board.ioc. This does not
    take "segment number N"; it matches the PDSC board name as the longest match
    (the board name must appear in the file name). A changed prefix or segment
    count therefore cannot attach a seed to the wrong board, and a changed
    directory layout shows up as "matches no board" instead of a silent mismatch.
    """
    out = {name: [] for name in names}
    for path in seed_paths(machine):
        matches = [name for name in names if name in path.name]
        if not matches:
            continue
        name = max(matches, key=len)
        out[name].append(path)
    return out


def _bsp_files(machine) -> set:
    bsp = _plugin_dir(machine) / "boards" / "bsp"
    if not bsp.is_dir():
        return set()
    return {path.name[: -len("_Modes.xml")] for path in bsp.glob("*_Modes.xml")}


def _records(machine):
    root = _plugin_dir(machine)
    if not root.is_dir():
        raise FileNotFoundError("boardmanager directory not found: %s" % root)
    raw = []
    for pdsc in sorted(root.glob("*.pdsc")):
        raw.extend(_iter_boards(pdsc))
    seeds = _seed_files(machine, [item["name"] for item in raw])
    bsp = _bsp_files(machine)
    out = []
    for item in raw:
        name = item["name"]
        variants = list(seeds.get(name) or [])
        # Plain variant first, _AllConfig second; neither present means no seed.
        plain = [path for path in variants if "_AllConfig" not in path.name]
        rest = [path for path in variants if "_AllConfig" in path.name]
        variants = plain + rest
        item["seeds"] = [path.name for path in variants]
        item["seed_all_config"] = any("_AllConfig" in path.name for path in variants)
        item["seed_file"] = variants[0].name if variants else None
        item["has_bsp"] = name in bsp
        item["id"] = board_id(name, item.get("revision"), item.get("vendor"))
        out.append(item)
    return out


def seed_overview(machine) -> dict:
    """How seed files map to PDSC board names (a layout change shows up here).

    The installation may hold stale seeds whose board is no longer in the PDSC, so
    "a file exists" is not treated as "the board exists". This reports the total
    and the file names that match no board.
    """
    all_paths = seed_paths(machine)
    records = _records(machine)
    claimed = {name for item in records for name in (item.get("seeds") or [])}
    unmatched = [path.name for path in all_paths if path.name not in claimed]
    return {"total": len(all_paths), "claimed": len(all_paths) - len(unmatched),
            "unmatched": unmatched}


def list_boards(machine, keyword: str | None = None, limit: int = 50) -> list:
    keyword = (keyword or "").strip().lower()
    out = []
    for item in _records(machine):
        if keyword and keyword not in item["name"].lower():
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def bsp_paths(machine, name: str) -> dict:
    """Board name -> native BSP definition files (Modes / Configs; either may be
    absent).

    File names use the board name only, without revision or vendor (installation
    layout). This matches the real file names in the directory instead of building
    a path: a changed layout returns None rather than pointing at a missing file.
    """
    bsp = _plugin_dir(machine) / "boards" / "bsp"
    found = {"modes": None, "configs": None}
    if not bsp.is_dir():
        return found
    for path in bsp.glob(name + "_*.xml"):
        if path.stem == name + "_Modes":
            found["modes"] = path
        elif path.stem == name + "_Configs":
            found["configs"] = path
    return found


def board_context(machine, wanted: str) -> dict:
    """Board identity -> shared context (MCU, seed, BSP definitions).

    `wanted` may be the full id or just the board name (the `board` key in an
    .ioc file is the board name). When only the name is given and the
    installation has several revisions: nothing is guessed, `detail` is None and
    `ambiguous` is True. A caller that needs a seed or defaults must ask for the
    full id; materialized builds and BSP definition lookups do not need a
    revision.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        raise ValueError("missing board id")
    name = wanted.split("@")[0]
    detail = None
    ambiguous = False
    mounted = None
    try:
        detail = board_detail(machine, wanted)
        name = detail["name"]
        mounted = detail.get("mounted_device")
    except ValueError:
        ambiguous = True
        same = [item for item in _records(machine) if item["name"] == name]
        mounted = same[0].get("mounted_device") if same else None
    except KeyError:
        if "@" in wanted:
            raise
    paths = bsp_paths(machine, name)
    seed = None
    if detail is not None:
        chosen = detail.get("seed_file")
        if chosen is not None:
            seed = _plugin_dir(machine) / "boards" / chosen
        else:
            seeds = detail.get("seeds") or []
            plain = [item for item in seeds if "_AllConfig" not in item]
            picked = plain[0] if plain else (seeds[0] if seeds else None)
            seed = _plugin_dir(machine) / "boards" / picked if picked else None
    return {"id": wanted,
            "name": name,
            "detail": detail,
            "ambiguous": ambiguous,
            "seed": seed,
            "seeds": list(detail.get("seeds") or []) if detail else [],
            "mounted_device": mounted,
            "has_bsp": paths["modes"] is not None,
            "bsp_modes": paths["modes"],
            "bsp_configs": paths["configs"]}


def board_detail(machine, wanted: str) -> dict:
    wanted = (wanted or "").strip()
    records = _records(machine)
    exact = [item for item in records if item["id"] == wanted]
    if exact:
        return exact[0]
    by_name = [item for item in records if item["name"] == wanted]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise ValueError("board %s has %d revisions, pass the full id (name@revision): %s"
                         % (wanted, len(by_name), ", ".join(item["id"] for item in by_name)))
    raise KeyError("no such board: %s" % wanted)