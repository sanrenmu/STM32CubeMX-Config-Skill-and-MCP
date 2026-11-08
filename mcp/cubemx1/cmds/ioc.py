"""The ioc command group: offline compile / development round-trip / generate.

The round-trip runs in development/CI only (in a long-lived session); the
release path does not do it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import iocc
from ..errors import CubemxError
from ..invoke import (disable_popup_wrapper, enable_popup_wrapper,
                      path_arg)
from ..iocc.check import compare, errors_in_log, parse_ioc
from ..iocc.errors import IocError


def _target(path):
    src = Path(path).expanduser()
    if not src.is_file():
        raise CubemxError("target JSON not found: %s" % src)
    try:
        return json.loads(src.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CubemxError("target JSON is not valid JSON: %s\n  %s" % (src, exc))


def apply_edit_file(base_path, edit_path, out_path=None) -> dict:
    """Apply cubemx1.ioc-edit/1 to a target JSON and write the new target."""
    from ..iocc import edit as edit_mod

    base = _target(base_path)
    if edit_path:
        edit_doc = _target(edit_path)
    else:
        raise CubemxError("an edit document is required (cubemx1.ioc-edit/1)")
    result = edit_mod.apply_edit(base, edit_doc, base.get("contract"))
    out = Path(out_path).expanduser() if out_path else Path(str(base_path)).with_suffix(".edited.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"ok": True, "json": str(out), "target": result}


def import_target(machine, ioc_path, out_path=None) -> dict:
    """Import a .ioc into a cubemx1.ioc/2 target and write it (does not start CubeMX)."""
    from ..iocc import import_ioc

    src = Path(ioc_path).expanduser()
    if not src.is_file():
        raise CubemxError(".ioc not found: %s" % src)
    result = import_ioc.import_ioc(machine, src)
    target = result["target"]
    report = result["report"]
    out = Path(out_path).expanduser() if out_path else src.with_suffix(".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"ok": True, "json": str(out), "target": target, "report": report}


def build(machine, target_path, out_path=None) -> dict:
    """Offline compile: target JSON -> .ioc text (does not start CubeMX)."""
    data = _target(target_path)
    try:
        result = iocc.build(machine, data)
    except IocError as exc:
        raise CubemxError("%s: %s" % (exc.kind, exc))
    if not out_path:
        # Without an output path, land in the project directory from the
        # target: <output_dir>/<project name>.ioc
        project = data.get("project") or {}
        if project.get("output_dir") and project.get("name"):
            out_path = str(Path(project["output_dir"]).expanduser()
                           / (project["name"] + ".ioc"))
    written = str(result.write(out_path)) if out_path else ""
    return {"keys": len(result.keys), "notes": result.notes,
            "path": written, "text": result.text}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")




def check(client, ioc_path, save_dir=None) -> dict:
    """Development/acceptance round-trip: load -> save as -> compare keys + scan the log.

    **Not a required step for release use**: normal users only run build +
    generate; this one is for development and acceptance.
    """
    src = Path(ioc_path).expanduser().resolve()
    if not src.is_file():
        raise CubemxError("the .ioc to check does not exist: %s" % src)
    target_dir = (Path(save_dir).expanduser().resolve() if save_dir else src.parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    dump = target_dir / (src.stem + "_roundtrip.ioc")
    if dump.exists():
        dump.unlink()
    logs = []
    load = client.run("config load " + path_arg(str(src)))
    logs.extend(load.get("log_lines") or [])
    if not load.get("ok"):
        return {"ok": False, "stage": "load", "problems": ["load failed"],
                "output": (load.get("output") or "")[:500],
                "log_errors": errors_in_log("\n".join(logs))}
    save = client.run("config saveas " + path_arg(str(dump)))
    logs.extend(save.get("log_lines") or [])
    if not save.get("ok") or not dump.exists():
        return {"ok": False, "stage": "saveas",
                "problems": ["save as failed or produced no file"],
                "output": (save.get("output") or "")[:500],
                "log_errors": errors_in_log("\n".join(logs))}
    expected = parse_ioc(_read(src))
    saved = parse_ioc(_read(dump))
    normalized = []
    problems = compare(expected, saved, normalized)
    log_errors = errors_in_log("\n".join(logs))
    return {"ok": not problems and not log_errors, "stage": "compare",
            "problems": problems, "normalized": normalized,
            "log_errors": log_errors,
            "roundtrip": str(dump), "keys_checked": len(expected)}


_STARTUP = "startup_stm32"


def _walk(root: Path):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def verify_generated(root: Path, keys: dict) -> dict:
    """Hard checks after generation: startup file present + referenced by the
    project file + key values present in the generated code.

    Why this step exists: it was measured that generation can return success
    while the startup file is missing and the project does not build. Checking
    the return code alone is no check at all.
    """
    problems = []
    root = Path(root)
    startup = [p for p in _walk(root)
               if p.name.lower().startswith(_STARTUP)
               and p.parent == root]
    if not startup:
        problems.append("no startup file in the root directory (%s*.s)" % _STARTUP)
    referenced = False
    for path in _walk(root):
        if path.suffix.lower() not in (".txt", ".cmake", "") and path.name != "Makefile":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _STARTUP in text.lower():
            referenced = True
            break
    if startup and not referenced:
        problems.append("the startup file exists but no project file references it")
    hits = []
    # Only look at "numeric parameters of the IPs the target uses".
    # Why not a prefix blacklist: project-level constants leak in too
    # (measured: PCC.Temperature=25 was taken for a parameter, so every case
    # reported "none appeared in the generated code" - a false alarm, not a
    # generation defect).
    instances = set()
    try:
        count = int(keys.get("Mcu.IPNb", "0"))
    except (TypeError, ValueError):
        count = 0
    for index in range(count):
        name = keys.get("Mcu.IP%d" % index)
        if name:
            instances.add(name)
    # RCC values are frequency names (USBFreq_Value and the like) and never
    # appear in the generated code as "parameter=value", so they are left out;
    # clock correctness is covered by the save-as round-trip.
    numeric = {
        key: value for key, value in keys.items()
        if value.isdigit() and len(value) >= 2 and key.count(".") == 1
        and key.split(".", 1)[0] in instances
        and not key.startswith("RCC.")
    }
    if numeric:
        blob = []
        for path in _walk(root):
            if path.suffix.lower() not in (".c", ".h"):
                continue
            try:
                blob.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        text = "\n".join(blob)
        for key, value in numeric.items():
            param = key.rsplit(".", 1)[-1]
            pattern = r"\b%s\s*=\s*%s\b" % (re.escape(param), re.escape(value))
            if re.search(pattern, text):
                hits.append(key)
        if not hits:
            problems.append("none of the target's numeric parameters appeared "
                            "in the generated code")
        missing = sorted(set(numeric) - set(hits))
    else:
        missing = []
    return {"ok": not problems, "problems": problems,
            "startup": [p.name for p in startup],
            "values_found": hits, "values_missing": missing}


def generate(client, ioc_path) -> dict:
    """Hand the compiled .ioc to the native generation entry and check the artifacts."""
    src = Path(ioc_path).expanduser().resolve()
    if not src.is_file():
        raise CubemxError("the .ioc to generate does not exist: %s" % src)
    load = client.run("config load " + path_arg(str(src)))
    if not load.get("ok"):
        raise CubemxError("load failed, cannot generate: %s"
                          % (load.get("output") or "")[:300])
    # During generation CubeMX may raise "Warning: Code Generation" (for
    # example a FREERTOS warning) and the modal dialog blocks the command line
    # for good. Use CubeMX's own popup suppressor to answer with the default
    # answer (the same approach as the IDE-project generation path; not left
    # enabled, see the notes in invoke).
    enable_popup_wrapper(client)
    try:
        res = client.run("project generate", timeout=3600)
    finally:
        try:
            disable_popup_wrapper(client)
        except Exception:  # noqa: BLE001 - if the session is broken, so be it
            pass
    lines = res.get("log_lines") or []
    # Generation directory: CubeMX's rule is the .ioc directory plus the
    # project name; measured: right next to the .ioc.
    outdir = src.parent
    expected = parse_ioc(_read(src))
    verified = verify_generated(outdir, expected)
    log_errors = errors_in_log("\n".join(lines))
    return {"ok": bool(res.get("ok")) and verified["ok"] and not log_errors,
            "output": (res.get("output") or "").strip()[-800:],
            "output_dir": str(outdir),
            "generate_ok": bool(res.get("ok")),
            "check": verified,
            "log_errors": log_errors,
            "fatal": bool(res.get("fatal"))}
