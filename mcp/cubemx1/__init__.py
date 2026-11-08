"""cubemx1 -- offline configuration compiler for STM32CubeMX 1.x.

Offline, it compiles one JSON file that describes the target configuration into
a `.ioc` that CubeMX loads directly; CubeMX only loads and generates, it is no
longer driven command by command.

Package layers:

- `iocc/`: validate, derive, render and compare the target JSON (pure Python, no CubeMX)
- `cmds/`: command implementations (`ioc build` / `ioc check` / `ioc generate`)
- `cli/`: command-line parsing and output
- `session.py` / `invoke.py` / `daemon.py`: the long-lived session, used only when CubeMX is needed
- `ipxml.py` / `devicedb.py` / `clocksolve.py`: derive keys and clocks from the installed data

Only `check` and `generate` need CubeMX; `build` is fully offline.
"""

__version__ = "1.0.0"
