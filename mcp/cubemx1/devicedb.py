"""Device database access.

## Where the data comes from

CubeMX ships a SQLite device database called CubeFinder that holds marketing and
spec data for every ST part. **It can be queried without starting CubeMX**, so
`finder`-style commands are the fastest ones.

Key tables:

=======  ======  ====================================================
table    rows    purpose
=======  ======  ====================================================
`cpn`    17587   part number (CPN) → device name (refname)
`cpn_has_attribute`  650318  part attributes (pin count, core, peripheral
                  count, MX2 support…)
`rpn`    14560   RPN (orderable part number)
=======  ======  ====================================================

## There are three names; do not mix them up

One part can map to three different names inside CubeMX, and mixing them fails:

| name | example | where it is used |
|---|---|---|
| CPN (part number) | `STM32F031C6T6` | ordering, `--cpn` argument |
| refname | `STM32F031C6Tx` | `Mcu.Name` written into `.ioc` |
| XML file name | `STM32F031C(4-6)Tx` | `db/mcu/<this>.xml` |

**refname and the XML file name often differ.** Measured over 5035 recorded
parts: **2585 (51.3%) differ** — because the XML is organized as "one file covers
several parts", with temperature/size ranges in parentheses, e.g.
`STM32F031C(4-6)Tx` covers both C4 and C6.

The XML file name comes from the `MXFILENAME_SDTDB` attribute, **not** from
`cpn.refname`. Building the path from refname makes half the chips report
"device file missing" for no visible reason.

## Full path

::

    CPN  ──query cpn table──▶  refname       ──written into .ioc as Mcu.Name
     └──query MXFILENAME_SDTDB──▶  XML file name
                                  ──build path──▶  <cubemx_home>/db/mcu/<xml>.xml
                                                  device XML: pin table, IP list, Flash/RAM …
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CubemxError
from .machine import Machine

# The XML file name lives in this attribute
XML_NAME_ATTR = "MXFILENAME_SDTDB"


class DeviceDbError(CubemxError):
    """Problem with the device database (missing file, cannot open, …)."""


# Searched in priority order: first inside the install dir, then the runtime cache.
CANDIDATES = [
    Path("db/plugins/mcufinder/mcu/cube-finder-db.db"),
    Path.home() / ".stmcufinder" / "plugins" / "mcufinder" / "mcu" / "cube-finder-db.db",
]


def find_db_file(machine: Machine) -> Path:
    """Locate the device database file."""
    for c in CANDIDATES:
        p = (machine.home / c) if not c.is_absolute() else c
        if p.is_file():
            return p
    raise DeviceDbError(
        "CubeMX device database (cube-finder-db.db) not found. Looked in:\n"
        + "\n".join("  - " + str((machine.home / c) if not c.is_absolute() else c)
                    for c in CANDIDATES)
        + "\nIf CubeMX is installed in an unusual place, check that the install dir "
          "contains db/plugins/mcufinder/"
    )


@dataclass
class Device:
    """Summary of one part."""

    cpn: str
    refname: str
    cpn_id: int
    attributes: dict = field(default_factory=dict)

    @property
    def xml_name(self) -> str | None:
        """Device XML file name (without extension), or None when absent.

        Comes from the ``MXFILENAME_SDTDB`` attribute. Measured: 51.3% of parts have
        a value different from refname (the XML is organized as "one file covers
        several parts", with ranges in parentheses), so refname cannot be used.

        **No "fall back to refname" path.** Measured over 12552 parts without this
        attribute: refname never happened to match a real file (0/12552 hits).
        Those parts are marketing part numbers of older products and have no device
        file at all. A fallback would only produce a path-like value that does not
        exist, and make people think the file was lost.
        """
        v = self.attributes.get(XML_NAME_ATTR)
        if v in (None, ""):
            return None
        return str(v).strip()

    @property
    def has_device_file(self) -> bool:
        """Whether this part has a device file (pin table and IP list live there)."""
        return self.xml_name is not None

    @property
    def mx2_supported(self) -> bool | None:
        """Whether CubeMX2 supports this part."""
        v = self.attributes.get("MX2_Supported")
        if v is None:
            return None
        return str(v).strip().lower() == "true"


class DeviceDb:
    """Read-only access to the device database."""

    def __init__(self, machine: Machine):
        self.machine = machine
        self.path = find_db_file(machine)
        # Open read-only; never modify ST's data
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "DeviceDb":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- queries ----

    def series(self) -> list[str]:
        """List all series, e.g. STM32F1 / STM32H7."""
        cur = self._conn.execute(
            "SELECT DISTINCT substr(cpn,1,7) AS s FROM cpn "
            "WHERE cpn LIKE 'STM32%' ORDER BY s"
        )
        # Some prefixes are truncated (e.g. STM32L-); return the first 7 chars and
        # let the caller filter.
        return [r["s"] for r in cur.fetchall()]

    def list_devices(self, series: str | None = None,
                     limit: int | None = None) -> list[tuple[str, str]]:
        """List part numbers with their device names.

        :param series: series prefix, e.g. ``STM32H7``. Omit to list everything.
        """
        sql = "SELECT cpn, refname FROM cpn"
        args: list = []
        if series:
            sql += " WHERE cpn LIKE ?"
            args.append(series + "%")
        sql += " ORDER BY cpn"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r["cpn"], r["refname"]) for r in self._conn.execute(sql, args)]

    def by_cpn(self, cpn: str, with_attributes: bool = True) -> Device | None:
        """Look up by part number. The full number is required; case-insensitive."""
        cur = self._conn.execute(
            "SELECT id, cpn, refname FROM cpn WHERE upper(cpn)=upper(?)", (cpn,))
        r = cur.fetchone()
        if r is None:
            return None
        dev = Device(cpn=r["cpn"], refname=r["refname"], cpn_id=r["id"])
        if with_attributes:
            dev.attributes = self.attributes(dev.cpn_id)
        return dev

    def attributes(self, cpn_id: int) -> dict:
        """Fetch all attributes of one part."""
        cur = self._conn.execute(
            "SELECT a.name AS n, ca.strValue AS s, ca.numValue AS v "
            "FROM cpn_has_attribute ca JOIN attribute a ON a.id=ca.attribute_id "
            "WHERE ca.cpn_id=?", (cpn_id,))
        out: dict = {}
        for r in cur.fetchall():
            # Prefer the string value; fall back to the numeric one
            out[r["n"]] = r["s"] if r["s"] not in (None, "") else r["v"]
        return out

    def search(self, keyword: str, limit: int = 40) -> list[tuple[str, str]]:
        """Fuzzy part-number search by keyword."""
        cur = self._conn.execute(
            "SELECT cpn, refname FROM cpn WHERE cpn LIKE ? ORDER BY cpn LIMIT ?",
            (f"%{keyword}%", limit))
        return [(r["cpn"], r["refname"]) for r in cur.fetchall()]

    # ---- match against the device XML in the install dir ----

    def mcu_xml_path(self, xml_name: str) -> Path:
        """Full path of the device XML.

        Pass :attr:`Device.xml_name` (that is, MXFILENAME_SDTDB), **not refname**.
        """
        return self.machine.db / "mcu" / f"{xml_name}.xml"

    def has_mcu_xml(self, xml_name: str) -> bool:
        return self.mcu_xml_path(xml_name).is_file()

    def xml_name_for_refname(self, refname: str) -> str | None:
        """Resolve an IOC ``Mcu.Name`` to its MCU XML filename.

        A refname can cover several CPN rows, so collect their
        ``MXFILENAME_SDTDB`` values and require one unambiguous XML name.
        Missing attributes stay missing; never guess that refname is a filename.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT ca.strValue AS xml_name "
            "FROM cpn c "
            "JOIN cpn_has_attribute ca ON ca.cpn_id = c.id "
            "JOIN attribute a ON a.id = ca.attribute_id "
            "WHERE upper(c.refname) = upper(?) AND a.name = ? "
            "AND ca.strValue IS NOT NULL AND trim(ca.strValue) <> ''",
            (refname, XML_NAME_ATTR),
        ).fetchall()
        names = {str(row["xml_name"]).strip() for row in rows
                 if row["xml_name"] and str(row["xml_name"]).strip()}
        if len(names) > 1:
            raise DeviceDbError(
                "refname %r maps to several MCU XML file names: %s" %
                (refname, ", ".join(sorted(names))))
        return next(iter(names), None)

    def resolve_xml(self, dev: Device) -> Path | None:
        """Given a part, return the path of its device XML.

        Returns None both when the part has no device file (older marketing part
        numbers) and when the file is missing. Callers must tell these two apart,
        hence :meth:`Device.has_device_file`.
        """
        if dev.xml_name is None:
            return None
        p = self.mcu_xml_path(dev.xml_name)
        return p if p.is_file() else None
