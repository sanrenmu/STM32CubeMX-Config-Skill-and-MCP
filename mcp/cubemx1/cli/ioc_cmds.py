"""Thin adapters for the ioc commands: output only."""

from __future__ import annotations

from .base import EXIT_FAIL, EXIT_OK, Out, _join, _need_client, machine


def cmd_ioc_build(args, out: Out) -> int:
    from ..cmds import ioc
    res = ioc.build(machine(), args.target, args.output)
    payload = {key: value for key, value in res.items() if key != "text"}
    lines = []
    if res["path"]:
        lines.append("written  %s" % res["path"])
    lines.append("keys     %d" % res["keys"])
    for note in res["notes"]:
        lines.append("note     %s" % note)
    out.emit(payload, _join(lines))
    return EXIT_OK




def cmd_ioc_edit(args, out: Out) -> int:
    """Apply an incremental edit (cubemx1.ioc-edit/1) to a target JSON."""
    from ..cmds import ioc
    try:
        res = ioc.apply_edit_file(args.base, args.edit, args.output)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "edit failed: %s" % exc)
        return EXIT_FAIL
    out.emit({"ok": True, "json": res["json"]}, _join(["written  %s" % res["json"]]))
    return EXIT_OK


def cmd_ioc_import(args, out: Out) -> int:
    """Import a .ioc into an advanced target JSON (does not start CubeMX)."""
    from ..cmds import ioc
    try:
        res = ioc.import_target(machine(), args.ioc, args.output)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "import failed: %s" % exc)
        return EXIT_FAIL
    report = res["report"]
    lines = ["written  %s" % res["json"],
             "keys     %d (semantic %d, extensions %d)"
             % (report["keys_total"], report["keys_semantic"], report["keys_extensions"]),
             "objects  pins %d / IP %d / IRQ %d / DMA %d"
             % (report["counts"]["pins"], report["counts"]["ips"],
                report["counts"]["nvic"], report["counts"]["dma"])]
    if report["unsemanticized"]:
        lines.append("unsemanticized %d keys (kept in extensions.ioc)"
                     % len(report["unsemanticized"]))
    for note in report["notes"]:
        lines.append("note     %s" % note)
    out.emit({"ok": True, "json": res["json"], "report": report}, _join(lines))
    return EXIT_OK


def cmd_ioc_check(args, out: Out) -> int:
    """Development/acceptance round-trip: load + save as + compare keys.

    **Not a required step for release use.**
    """
    from ..cmds import ioc
    try:
        res = ioc.check(_need_client(args), args.ioc, args.save_dir)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "check failed: %s" % exc)
        return EXIT_FAIL
    lines = ["result   %s" % ("PASS" if res["ok"] else "FAIL")]
    if res.get("roundtrip"):
        lines.append("saveas   %s" % res["roundtrip"])
    if res.get("keys_checked"):
        lines.append("checked  %d" % res["keys_checked"])
    for problem in res.get("problems") or []:
        lines.append("problem  %s" % problem)
    for note in res.get("normalized") or []:
        lines.append("normalized %s" % note)
    for line in res.get("log_errors") or []:
        lines.append("log      %s" % line[:160])
    out.emit(res, _join(lines))
    return EXIT_OK if res["ok"] else EXIT_FAIL

def cmd_ioc_generate(args, out: Out) -> int:
    from ..cmds import ioc
    try:
        res = ioc.generate(_need_client(args), args.ioc)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "generate failed: %s" % exc)
        return EXIT_FAIL
    lines = ["result   %s"
             % ("generation complete" if res["ok"] else "generation failed")]
    if res.get("output_dir"):
        lines.append("dir      %s" % res["output_dir"])
    check = res.get("check") or {}
    if check.get("startup"):
        lines.append("startup  %s" % ", ".join(check["startup"]))
    if check.get("values_found"):
        lines.append("found %d target values in the generated code (for example %s)"
                     % (len(check["values_found"]), check["values_found"][0]))
    if check.get("values_missing"):
        # A hint only: some parameters (for example the RTOS heap size) appear
        # in the generated code in a different form.
        lines.append("another %d numeric parameters did not appear as "
                     "\"parameter=value\" in the generated code (verify them "
                     "yourself): %s"
                     % (len(check["values_missing"]),
                        ", ".join(check["values_missing"][:5])))
    for problem in check.get("problems") or []:
        lines.append("check    %s" % problem)
    if res.get("output"):
        lines.append(res["output"])
    for line in res.get("log_errors") or []:
        lines.append("log      %s" % line[:160])
    out.emit(res, _join(lines))
    return EXIT_OK if res["ok"] else EXIT_FAIL
