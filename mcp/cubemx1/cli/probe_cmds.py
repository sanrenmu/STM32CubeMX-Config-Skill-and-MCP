"""Thin adapters for the probe commands: argument assembly and output only
(the logic lives in probe/query.py)."""

from __future__ import annotations

from .. import boardservice
from ..probe import query
from .base import EXIT_FAIL, EXIT_OK, Out, _join, machine


def cmd_probe_clock(args, out: Out) -> int:
    try:
        payload = query.clock(machine(), device=args.device, target=args.target,
                              output=args.output, view=args.view, size=args.size,
                              seq=args.seq, tree=args.tree)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    summary = payload["summary"]
    lines = ["device    %s" % payload["device"],
             "tree      %s" % payload["clock_tree"],
             "elements  %d  connections %d  raw records %d"
             % (summary["nodes"], summary["edges"], summary["raw_elements"]),
             "types     %s" % ", ".join("%s=%d" % pair
                                        for pair in summary["node_types"].items())]
    if summary.get("unknown_types"):
        lines.append("unknown types %s" % ", ".join(summary["unknown_types"]))
    if args.view == "graph":
        for node in payload["nodes"]:
            lines.append("  %-34s %-16s %s"
                         % (node["id"], node["type"] or "?",
                            node["parameters"]["raw"] or ""))
            for item in node["inputs"]:
                lines.append("      <- %-8s %-24s %s"
                             % (item["role"], item["from"], item["signal_id"] or ""))
            for item in node["outputs"]:
                lines.append("      -> %-24s %s"
                             % (item["to"], item["signal_id"] or ""))
    elif args.view == "upstream":
        lines.append("output    %s" % args.output)
        lines.append("targets   %s" % ", ".join(payload["target_ids"]))
        for node in payload["nodes"]:
            lines.append("  %-34s %-16s %s"
                         % (node["id"], node["type"] or "?", node["role"] or ""))
        lines.append("paths     %d%s"
                     % (payload["path_total"],
                        " (truncated)" if payload.get("paths_truncated") else ""))
    else:
        raw = payload["raw"]
        lines.append("raw       %s%s"
                     % (raw["source"],
                        " (truncated)" if raw.get("truncated") else ""))
        lines.append(raw.get("xml") or raw.get("fragment") or "")
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_probe_peripherals(args, out: Out) -> int:
    try:
        payload = query.peripherals(machine(), device=args.device,
                                    target=args.target, board=args.board)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    lines = ["device     %s" % payload["device"],
             "peripherals %d" % payload["count"]]
    for row in payload["peripherals"]:
        lines.append("  %-22s %-14s %-18s %s"
                     % (row["instance"], "%s-%s" % (row["name"], row["version"]),
                        row["type"] or "?", ""))
    if payload.get("missing_modes_file"):
        lines.append("no modes file: %s" % ", ".join(payload["missing_modes_file"]))
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_probe_pins(args, out: Out) -> int:
    try:
        payload = query.pins(machine(), device=args.device, target=args.target,
                             ip=args.ip, mode=args.mode,
                             board=getattr(args, "board", None),
                             limit=args.limit)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    peripheral = payload["pins"]
    lines = ["device     %s" % payload["device"],
             "instance   %s (%s)" % (payload["instance"], payload["ip"]),
             "pins       %d  signals %d"
             % (peripheral["pin_count"], peripheral["signal_count"])]
    many = peripheral["pin_with_many_signals"]
    lines.append("one pin, many signals %d" % len(many))
    for item in many[:10]:
        lines.append("  %-22s %s" % (item["pin"], " / ".join(item["signals"])))
    multi = peripheral["signal_on_many_pins"]
    lines.append("signal, many pins %d" % len(multi))
    for item in multi[:10]:
        lines.append("  %-22s %s" % (item["signal"], ", ".join(item["pins"])))
    block = payload.get("block")
    if block is not None:
        lines.append("mode       %s  required %d"
                     % (payload["mode"], block["required_count"]))
        for row in block["signals"]:
            note = "ambiguous" if row["ambiguous"] else ""
            lines.append("  %-26s %-32s %s"
                         % (row["signal"], ", ".join(row["pins"]), note))
        for conflict in block["conflicts"]:
            lines.append("conflict   %s: %s"
                         % (conflict["pin"], ", ".join(conflict["signals"])))
        if block["missing"]:
            lines.append("missing    %s" % ", ".join(block["missing"]))
    children = payload.get("children")
    if children is not None:
        lines.append("group      %s  leaf modes %d"
                     % (payload["mode"], payload["children_total"]))
        for row in children:
            lines.append("  %-30s required %s ambiguous %s conflicts %s missing %s"
                         % (row["mode"], row.get("required"),
                            row.get("ambiguous"), row.get("conflicts"),
                            row.get("missing")))
    summary = payload.get("mode_summary")
    if summary is not None:
        lines.append("modes      %d%s" % (payload["modes_total"],
                                           " (truncated)"
                                           if payload.get("modes_truncated") else ""))
        for row in summary:
            if not row.get("decidable"):
                lines.append("  %-30s offline-undecidable" % row["mode"])
                continue
            lines.append("  %-30s required %-3s ambiguous %-3s conflicts %-3s missing %s"
                         % (row["mode"], row["required"], row["ambiguous"],
                            row["conflicts"], row["missing"]))
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_probe_describe(args, out: Out) -> int:
    try:
        payload = query.describe(machine(), device=args.device,
                                    target=args.target, ip=args.ip,
                                    board=getattr(args, "board", None))
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    lines = ["instance %s (%s)" % (payload["instance"], payload["ip"])]
    if payload.get("ip_type"):
        lines.append("type     %s" % payload["ip_type"])
    if payload.get("board"):
        lines.append("board    %s" % payload["board"])
    lines.append("source   %s" % (payload.get("source") or ""))
    lines.append("modes    enabled %s; possible %s"
                 % (payload.get("enabled_modes"), payload.get("possible_modes")))
    for item in payload.get("parameters") or []:
        picked = item.get("selected")
        if picked is None:
            state = "undecided" if item.get("truth") is None else "not applicable"
            lines.append("param    %s  %s" % (item["name"], state))
            continue
        domain = item.get("domain") or {}
        if domain.get("values"):
            detail = "values %s" % (", ".join(domain["values"][:6]))
        elif domain.get("min") is not None:
            detail = "range %s..%s" % (domain.get("min"), domain.get("max"))
        else:
            detail = "type %s" % (picked.get("type") or "?")
        lines.append("param    %-28s %-8s default %-10s %s"
                     % (item["name"], picked.get("type") or "?",
                        picked.get("default"), detail))
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_probe_boards(args, out: Out) -> int:
    try:
        payload = query.boards(machine(), args.keyword, args.limit)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    lines = ["boards %d (keyword %s)" % (payload["count"], payload["keyword"])]
    for item in payload["boards"]:
        lines.append("  %-46s %-16s seed=%d bsp=%s"
                     % (item["id"], item.get("mounted_device") or "?",
                        len(item.get("seeds") or []), item.get("has_bsp")))
    out.emit(payload, _join(lines))
    return EXIT_OK


def cmd_probe_board(args, out: Out) -> int:
    try:
        payload = boardservice.board(machine(), args.id)
    except Exception as exc:
        out.emit_error({"ok": False, "error": str(exc)}, "query failed: %s" % exc)
        return EXIT_FAIL
    item = payload["board"]
    lines = ["board    %s" % item["id"],
             "identity %s" % ("resolved to a directory" if item.get("resolved")
                              else "name only (revision unresolved)"),
             "device   %s" % (item.get("mounted_device") or "?"),
             "type     %s" % (item.get("type") or "?"),
             "seed     %s" % (", ".join(item.get("seeds") or []) or "(none)"),
             "BSP      %s" % ("defined" if item.get("has_bsp")
                              else "not defined (no BSP component is generated)"),
             "source   %s" % item.get("source")]
    config = item.get("config") or {}
    if config.get("ips"):
        lines.append("config   instances %s"
                     % ", ".join(entry["instance"] for entry in config["ips"]))
    if config.get("pins"):
        lines.append("pins     %d" % len(config["pins"]))
    if config.get("clock_inputs"):
        lines.append("clock inputs %s" % ", ".join(
            "%s=%s" % pair for pair in config["clock_inputs"].items()))
    bsp = item.get("bsp") or {}
    if bsp.get("instance"):
        leaves = [mode["internal_name"] for mode in (bsp.get("modes") or [])
                  if mode.get("is_leaf")]
        lines.append("BSP instance %s  leaf modes %s"
                     % (bsp["instance"], ", ".join(leaves)))
        for param in (bsp.get("parameters") or [])[:12]:
            picked = param.get("selected") or {}
            domain = param.get("domain") or {}
            detail = picked.get("default")
            if domain.get("values"):
                detail = "%s (options %s)" % (detail, ", ".join(domain["values"][:4]))
            lines.append("  BSP param %-24s %s" % (param["name"], detail))
    elif bsp.get("error"):
        lines.append("BSP lookup failed %s" % bsp["error"])
    out.emit(payload, _join(lines))
    return EXIT_OK
