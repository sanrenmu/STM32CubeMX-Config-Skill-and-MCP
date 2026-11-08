"""Command-line entry: only the offline IOC compilation path is left.

2026-11-06 slimming: the old query / per-setting write / apply-plan commands
are gone (they still exist in git, but no longer ship). What is left:

    cubemx1 ioc build    target JSON -> .ioc (offline, does not start CubeMX)
    cubemx1 ioc generate .ioc -> native CubeMX generation + artifact checks
    cubemx1 version / status / stop   runtime maintenance

The command tree is changed in this file only; each command's implementation
lives in `*_cmds.py`.
"""

from __future__ import annotations

import argparse

from .ioc_cmds import (cmd_ioc_build, cmd_ioc_check, cmd_ioc_edit,
                        cmd_ioc_generate, cmd_ioc_import)
from .clock_cmds import cmd_clock_solve
from .probe_cmds import (cmd_probe_board, cmd_probe_boards, cmd_probe_clock,
                         cmd_probe_describe, cmd_probe_peripherals,
                         cmd_probe_pins)
from .session_cmds import cmd_status, cmd_stop, cmd_version


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cubemx1",
        description="STM32CubeMX 1.x offline IOC compiler: "
                    "write a target JSON, compile it offline into a .ioc that "
                    "loads directly.")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (for programs; humans do not need it)")
    sub = ap.add_subparsers(dest="command")

    # ---- ioc: offline compile / round-trip / generate ----
    s = sub.add_parser("ioc", help="offline IOC compiler: target JSON -> .ioc")
    isub = s.add_subparsers(dest="ioc_command")

    a = isub.add_parser("build", help="compile a target JSON into a .ioc (does not start CubeMX)")
    a.add_argument("target", help="target JSON file")
    a.add_argument("-o", "--output", default=None, help="where to write (the .ioc path)")
    a.set_defaults(func=cmd_ioc_build)

    a = isub.add_parser("edit", help="apply an incremental edit to a target JSON (cubemx1.ioc-edit/1)")
    a.add_argument("base", help="base target JSON")
    a.add_argument("edit", help="edit document JSON (cubemx1.ioc-edit/1)")
    a.add_argument("-o", "--output", default=None, help="where to write; defaults to <base>.edited.json")
    a.set_defaults(func=cmd_ioc_edit)

    a = isub.add_parser("import", help="import a .ioc into a cubemx1.ioc/2 target JSON")
    a.add_argument("ioc", help="the .ioc to import")
    a.add_argument("-o", "--output", default=None, help="which JSON to write; defaults to the same name with .json")
    a.set_defaults(func=cmd_ioc_import)

    a = isub.add_parser("check",
                        help="development/acceptance only: load + save as + compare keys (not needed for release use)")
    a.add_argument("ioc", help="the .ioc to check")
    a.add_argument("--save-dir", default=None,
                   help="where to save as; defaults to the .ioc directory")
    a.set_defaults(func=cmd_ioc_check)

    a = isub.add_parser("generate", help="hand the .ioc to CubeMX for code generation")
    a.add_argument("ioc", help="a compiled .ioc")
    a.set_defaults(func=cmd_ioc_generate)

    # ---- clock: offline solver inspection (writes nothing) ----
    s = sub.add_parser("clock", help="clock solver: solve target frequencies offline (writes nothing)")
    csub = s.add_subparsers(dest="clock_command")

    a = csub.add_parser("solve", help="solve target frequencies and print the RCC parameters")
    a.add_argument("--device", required=True, help="device RefName, for example STM32F407Z(E-G)Tx")
    a.add_argument("--target", action="append", default=[], metavar="PARAM=MHZ",
                   help="target frequency; repeatable, for example HCLKFreq_Value=168. "
                        "Several targets are solved jointly")
    a.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="explicit RCC parameter as a constraint; repeatable")
    a.set_defaults(func=cmd_clock_solve)

    # ---- probe: offline queries (does not start CubeMX) ----
    s = sub.add_parser("probe", help="offline queries: device/IP definitions, peripheral inventory, pins, ST boards, clock topology")
    psub = s.add_subparsers(dest="probe_command")

    a = psub.add_parser("describe", help="show one IP's definition and applicable definitions")
    a.add_argument("--ip", required=True, help="instance name, for example USART1")
    a.add_argument("--device", default=None, help="device RefName (falls back to --target)")
    a.add_argument("--board", default=None,
                   help="board id or board name: board-level instances (BSP) are queried from that board's native Modes")
    a.add_argument("--target", default=None, help="target JSON (optional; conditions are evaluated from its choices)")
    a.set_defaults(func=cmd_probe_describe)

    a = psub.add_parser("peripherals",
                        help="list every peripheral/middleware instance a device declares")
    a.add_argument("--device", default=None, help="device RefName (falls back to --target)")
    a.add_argument("--target", default=None, help="target JSON (optional; only its device is used)")
    a.add_argument("--board", default=None, help="board id or board name (device from the board)")
    a.set_defaults(func=cmd_probe_peripherals)

    a = psub.add_parser("pins",
                        help="one IP's signals and candidate pins; a mode's required pins")
    a.add_argument("--ip", required=True, help="IP instance or name, for example USART1 or FMC")
    a.add_argument("--device", default=None, help="device RefName (falls back to --target)")
    a.add_argument("--board", default=None, help="board id or board name (device from the board)")
    a.add_argument("--target", default=None, help="target JSON (optional; device and selection from it)")
    a.add_argument("--mode", default=None,
                   help="leaf mode name for its full signal table, or a group name")
    a.add_argument("--limit", type=int, default=50,
                   help="maximum mode rows in the summary, defaults to 50")
    a.set_defaults(func=cmd_probe_pins)

    a = psub.add_parser("boards", help="list official ST boards (reads the boardmanager PDSC)")
    a.add_argument("--keyword", default=None, help="board name keyword, for example NUCLEO")
    a.add_argument("--limit", type=int, default=50, help="maximum number of entries")
    a.set_defaults(func=cmd_probe_boards)

    a = psub.add_parser("board", help="show one board's details (including seed and BSP availability)")
    a.add_argument("--id", required=True, help="board id, for example NUCLEO-F429ZI@Rev.B@STMicroelectronics")
    a.set_defaults(func=cmd_probe_board)

    a = psub.add_parser("clock",
                        help="clock topology: graph, the upstream of one readout, or raw XML")
    a.add_argument("--device", default=None, help="device RefName (falls back to --target)")
    a.add_argument("--target", default=None, help="target JSON (optional; only its device is used)")
    a.add_argument("--output", default=None,
                   help="readout parameter name (for example HCLKFreq_Value) or element id (view=upstream)")
    a.add_argument("--view", default="graph", choices=["graph", "upstream", "raw"],
                   help="graph = every node and connection; upstream = everything behind one readout; raw = original XML")
    a.add_argument("--size", type=int, default=None, help="page size (never drops the underlying model)")
    a.add_argument("--seq", type=int, default=None, help="raw record id (view=raw of one node)")
    a.add_argument("--tree", default=None, help="clock tree name override")
    a.set_defaults(func=cmd_probe_clock)

    # ---- runtime maintenance ----
    a = sub.add_parser("version", help="show versions (tool + CubeMX + DB)")
    a.set_defaults(func=cmd_version)

    a = sub.add_parser("status", help="show background process and session status")
    a.set_defaults(func=cmd_status)

    a = sub.add_parser("stop", help="stop the background process (releases CubeMX)")
    a.set_defaults(func=cmd_stop)

    return ap
