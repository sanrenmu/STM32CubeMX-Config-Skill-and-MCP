"""Local path configuration.

One file per machine, never committed. It only records "where things are installed
on this machine" and holds no chip or project information.

Lookup order:
1. the file named by the ``CUBEMX1_MACHINE`` environment variable
2. ``~/.cubemx1/machine.json``
3. if neither exists, raise and show how to write one

The file looks like this (everything except cubemx_home may be omitted):

    {
      "cubemx_home": "C:/ST/STM32CubeMX",
      "repository": "C:/STM32Cube/Repository",
      "firmware_packages": {"H7": "STM32Cube FW_H7 V1.11.1"}
    }

``repository`` normally does not need to be written: it is read from the CubeMX
settings first (see below) and this value is only the fallback.
CubeMX keeps the firmware package repository path in
``~/.stm32cubemx/plugins/updater/updater.ini`` as ``[Path] RepositoryPath=...``
(CubeMX also takes the path from that setting when it generates code).
We used to copy it into machine.json, which went stale as soon as the installation
moved; it now follows CubeMX.

``java`` normally does not need to be written either; the copy shipped with CubeMX
(``<home>/jre/bin/java.exe``) is the default. That has two advantages: no dependency
on a system Java, and it bypasses the Windows launcher shell.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import MachineError

ENV_VAR = "CUBEMX1_MACHINE"
DEFAULT_PATH = Path.home() / ".cubemx1" / "machine.json"

#: Where CubeMX stores the firmware package repository path (the updater ini).
CUBEMX_UPDATER_INI = (Path.home() / ".stm32cubemx" / "plugins" / "updater"
                      / "updater.ini")


def _scan_packages(repository: Path | None) -> dict[str, str]:
    """Firmware packages actually installed in the repository:
    ``{family: "STM32Cube FW_<family> V<version>"}``.

    A directory name ``STM32Cube_FW_H7_V1.11.1`` becomes the CubeMX spelling
    ``STM32Cube FW_H7 V1.11.1`` (``_FW_`` becomes `` FW_``, the last ``_V``
    becomes `` V``). When a family has several versions installed the highest one
    wins.
    """
    out: dict[str, str] = {}
    if repository is None or not repository.is_dir():
        return out
    pattern = re.compile(r"^STM32Cube_FW_([A-Za-z0-9+]+)_V(.+)$")
    seen: dict[str, tuple] = {}
    for child in repository.iterdir():
        if not child.is_dir():
            continue
        m = pattern.match(child.name)
        if not m:
            continue
        fam, version = m.group(1), m.group(2)
        key = tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", version))
        if fam not in seen or key > seen[fam]:
            seen[fam] = key
            out[fam] = "STM32Cube FW_%s V%s" % (fam, version)
    return out


def _updater_ini() -> dict[str, str]:
    """Read the updater plugin ini (key -> value, merged across sections).

    Besides the repository path it holds the CubeMX version and the DB version,
    which must be read live when writing File.Version/MxCube.Version/MxDb.Version
    into a .ioc.
    """
    try:
        text = CUBEMX_UPDATER_INI.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        key, sep, value = line.partition("=")
        if sep:
            out.setdefault(key.strip(), value.strip())
    return out


def _repository_from_cubemx() -> Path | None:
    """Read the firmware package repository from the CubeMX settings.

    The setting lives in the updater plugin of the installation, as
    ``[Path] RepositoryPath`` in the ini. Returns None when it cannot be read (no
    error: this machine may never have run CubeMX).
    """
    try:
        text = CUBEMX_UPDATER_INI.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "repositorypath":
            value = value.strip().strip(chr(34))
            return Path(value) if value else None
    return None

TEMPLATE = {
    "cubemx_home": "CubeMX installation directory, for example C:/ST/STM32CubeMX",
    "repository": "STM32Cube firmware package repository, may be left empty",
    "firmware_packages": {"H7": "for example STM32Cube FW_H7 V1.11.1"},
}


class Machine:
    """The set of paths on one machine."""

    def __init__(self, raw: dict[str, Any], source: Path | str):
        self.raw = raw
        self.source = str(source)

        home = raw.get("cubemx_home")
        if not home:
            raise MachineError(
                f"cubemx_home is missing from the configuration (file: {self.source}).\n"
                f"it should hold the CubeMX installation directory, for example:\n"
                f'  {{"cubemx_home": "C:/ST/STM32CubeMX"}}'
            )
        self.home = Path(home)

        # The repository path follows the CubeMX setting (that is what it uses when
        # it generates code); the value in machine.json is only the fallback.
        from_cubemx = _repository_from_cubemx()
        from_config = Path(raw["repository"]) if raw.get("repository") else None
        if from_cubemx is not None:
            self.repository = from_cubemx
            self.repository_source = "CubeMX updater.ini"
        elif from_config is not None:
            self.repository = from_config
            self.repository_source = str(self.source)
        else:
            self.repository = None
            self.repository_source = ""
        # Firmware package versions follow what is really installed; the
        # configuration only adds families the scan did not find. (A copied version
        # number used to point at a package that was no longer installed, which made
        # generation pop a dialog.)
        installed = _scan_packages(self.repository)
        configured = dict(raw.get("firmware_packages") or {})
        for fam, name in configured.items():
            if fam not in installed and name:
                installed[fam] = name
        self.firmware_packages: dict[str, str] = installed

        # The CubeMX version itself: read it live when writing the .ioc header, do
        # not copy it by hand.
        ini = _updater_ini()
        soft = ini.get("SoftVersion", "")
        self.cubemx_version = soft[3:] if soft.startswith("MX.") else soft
        self.db_version = ini.get("DbVersion", "")
        # java may be given explicitly; otherwise the copy shipped with CubeMX is used
        self._java = raw.get("java")

    # ---- key paths ----

    @property
    def java(self) -> Path:
        """The Java used to start CubeMX.

        The JRE shipped with CubeMX is the default. That does not depend on a system
        Java and avoids the Windows launcher shell (the shell shows dialogs and does
        not reliably pass the output through).
        """
        if self._java:
            return Path(self._java)
        name = "java.exe" if os.name == "nt" else "java"
        return self.home / "jre" / "bin" / name

    @property
    def mx_exe(self) -> Path:
        """The CubeMX main program. It is also the jar package, started with
        java -jar."""
        return self.home / "STM32CubeMX.exe"

    @property
    def db(self) -> Path:
        return self.home / "db"

    @property
    def log_file(self) -> Path:
        """The CubeMX log file.

        Important: the output of parameter queries (for example
        ``get mode_param_list``) goes to the log, not to standard output. Measured.
        """
        return Path.home() / ".stm32cubemx" / "STM32CubeMX.log"

    # ---- checks ----

    def problems(self) -> list[str]:
        """Return the list of configuration and installation problems. An empty list
        means everything is fine."""
        out: list[str] = []
        if not self.home.is_dir():
            out.append(f"CubeMX installation directory does not exist: {self.home}")
            return out
        if not self.mx_exe.is_file():
            out.append(f"cannot find {self.mx_exe.name}; the installation directory is probably wrong")
        if not self.java.is_file():
            out.append(
                f"cannot find Java: {self.java}\n"
                f"  CubeMX normally ships one; with a trimmed installation,"
                f" add \"java\": \"path to your java\" to the configuration"
            )
        if self.repository and not self.repository.is_dir():
            out.append(f"firmware package repository does not exist: {self.repository}"
                       f" (queries still work; only code generation is affected)")
        return out

    def require_ok(self) -> None:
        problems = self.problems()
        if problems:
            raise MachineError(
                "problems with the local configuration (file: %s):\n" % self.source
                + "\n".join("  - " + p for p in problems)
            )


def _config_path() -> Path | None:
    env = os.environ.get(ENV_VAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise MachineError(
                f"the file named by {ENV_VAR} does not exist: {p}"
            )
        return p
    if DEFAULT_PATH.is_file():
        return DEFAULT_PATH
    return None


def load(path: Path | str | None = None) -> Machine:
    """Read the local configuration."""
    src: Path
    if path is not None:
        src = Path(path)
        if not src.is_file():
            raise MachineError(f"the given configuration file does not exist: {src}")
    else:
        found = _config_path()
        if found is None:
            raise MachineError(
                "no local path configuration found.\n"
                f"create {DEFAULT_PATH} with content like:\n"
                f"{json.dumps(TEMPLATE, indent=2, ensure_ascii=False)}\n"
                f"or point {ENV_VAR} at a configuration file elsewhere."
            )
        src = found

    try:
        # utf-8-sig: JSON edited by hand on Windows often carries a BOM, which the
        # json module rejects.
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise MachineError(f"configuration file is not valid JSON: {src}\n  {e}") from e
    if not isinstance(raw, dict):
        raise MachineError(f"the configuration file must hold a JSON object at the top level: {src}")
    return Machine(raw, src)