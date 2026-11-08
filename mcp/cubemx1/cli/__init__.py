"""CLI entry point (package).

Usage is in each subcommand's `--help`; the command tree lives in
:mod:`cubemx1.cli.parser`, and each command's implementation is grouped by
facet in the `*_cmds.py` modules.

This package does one thing: hand the command-line arguments to the
implementations in `cmds/`, then pick the output shape from `--json`.
**No business logic lives here.**"""

from __future__ import annotations

import json
import sys

from .base import EXIT_FAIL, EXIT_OK, Out
from .parser import build_parser

__all__ = ["main", "build_parser", "Out", "EXIT_OK", "EXIT_FAIL",
           "EXIT_INTERRUPTED"]

#: Interrupt code for Ctrl-C (128 + SIGINT). Shell convention.
EXIT_INTERRUPTED = 130


def main(argv: list[str] | None = None) -> int:
    # **The Windows console defaults to GBK**: if the output contains a
    # character GBK cannot encode (for example the warning glyph inside a
    # `clock solve` shared-component message), `print` raises
    # `UnicodeEncodeError` -- the command already did its work and then dies
    # on the last step, which looks like a broken feature.
    #
    # Only `errors` is changed to `replace`: **the encoding stays as it is**
    # (a GBK console stays GBK), the one character that cannot be printed
    # becomes `?`, and nothing crashes.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    out = Out(args.json)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return EXIT_OK

    try:
        return args.func(args, out)
    except KeyboardInterrupt:
        # 130 = 128 + SIGINT(2), the shell convention (Ctrl-C interrupt code).
        # Written this way instead of a bare 130 so the origin stays visible.
        return EXIT_INTERRUPTED
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False,
                             indent=2))
        else:
            print("error: %s" % e, file=sys.stderr)
        return EXIT_FAIL
