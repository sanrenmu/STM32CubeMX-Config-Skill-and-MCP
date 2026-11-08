"""Query "what can this IP be configured with" from the db in the CubeMX install dir.

## What lives here

Generic device XML / db lookups, shared by every caller:

- device name (refname, i.e. `Mcu.Name` in the `.ioc`) → `db/mcu/<refname>.xml`
- `<IP Name=".." Version=".."/>` in the device XML → `db/mcu/IP/<Name>-<Version>_Modes.xml`
- `<RefMode>` (mode names) and `<RefParameter>` (parameter names + candidates) in
  `_Modes.xml`

**Feature-specific adapters** live in their own modules: middleware in
:mod:`cubemx1.mwmodes`, DMA in :mod:`cubemx1.cmds.dma`.

## Why the db can be read

The data comes from the read-only db / XML data files under the CubeMX install
dir. The db is CubeMX's own data, so reading it is the same kind of operation as
reading the clock definition file (:mod:`cubemx1.clockxml`), and both are
**data-driven**:

- file names are built from device XML attributes, not from per-chip if branches
- the same middleware may map to different file versions per family
  (H750's LwIP is `LWIP-h7-v2.2.1_Cube_Modes.xml`, F429's is
  `LWIP-v2.1.2_Cube_Modes.xml`); that difference comes from data, not from our code

## Both `<IP>` spellings must be accepted

Device XMLs contain both ``<IP ... Version=".."/>`` and ``<IP ... Version="..">``.
Accepting only the self-closing form makes L552 and others look like "no IP at
all" — **silently returning a wrong answer is far more dangerous than an error**.
Measured.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CubemxError
from .machine import Machine


class IpXmlError(CubemxError):
    """The db has no information for this IP."""


def _local(tag: str) -> str:
    """db XML tags carry a namespace (`{http://mcd.rou.st.com/...}Mode`); return the local name."""
    return tag.rsplit("}", 1)[-1]


# **XML inside comments is not data.** Whole declaration blocks are commented out
# in db `_Modes.xml` files (measured over 1395 local files: comments hold 6332
# `<PossibleValue>`, 2378 `<RefParameter>`, 674 `<Mode>` and 992 `<Signal>`
# elements); regex-scanning raw text treats those **non-existent** modes,
# parameters and candidates as real. Measured: `ref_modes` therefore reported ghost
# modes (`WatchDog`, `WatchdogCopy`…) on 63/1395 files. So every place that scans
# raw text runs this function first.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


# Both spellings are accepted; see the module docstring
_IP_TAG_RE = re.compile(r"<IP\s+([^>]*?)>")
_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')


def device_xml(machine: Machine, refname: str) -> Path:
    """Device name → device XML path.

    refname is `Mcu.Name` in the `.ioc` (also what `get mcu name` returns). When
    no XML with that name exists, the device database's ``MXFILENAME_SDTDB`` maps
    refname to the shared XML file name; the two differ when one XML covers several
    CPNs.

    **Do not pass a part number (CPN) as refname**: CPN→refname goes through the
    device database SQLite (see :mod:`cubemx1.devicedb`), which is a different
    operation.
    """
    p = machine.db / "mcu" / ("%s.xml" % refname)
    if p.is_file():
        return p

    # Many part numbers share one MCU XML; its file name comes from MXFILENAME_SDTDB,
    # not from Mcu.Name. The direct fast path above stays; the read-only device
    # database is consulted only when the same-name file is missing.
    from .devicedb import DeviceDb

    with DeviceDb(machine) as db:
        xml_name = db.xml_name_for_refname(refname)
    if not xml_name:
        raise IpXmlError(
            "device file not found: %s\n"
            "(the device database has no MXFILENAME_SDTDB for refname %r.)"
            % (p, refname))

    resolved = machine.db / "mcu" / ("%s.xml" % xml_name)
    if not resolved.is_file():
        raise IpXmlError(
            "the device database maps refname %r to XML %r, but that file does "
            "not exist: %s"
            % (refname, xml_name, resolved))
    return resolved


@dataclass
class IpEntry:
    """One IP declared in the device XML."""

    instance: str
    name: str
    version: str
    attrs: dict = field(default_factory=dict)

    @property
    def config_file(self) -> str | None:
        return self.attrs.get("ConfigFile")


def ip_entries(machine: Machine, refname: str) -> list:
    """Every IP declared in the device XML (peripherals, middleware, services)."""
    text = device_xml(machine, refname).read_text(encoding="utf-8",
                                                  errors="replace")
    out = []
    for m in _IP_TAG_RE.finditer(text):
        a = dict(_ATTR_RE.findall(m.group(1)))
        inst, name, ver = (a.get("InstanceName"), a.get("Name"),
                           a.get("Version"))
        if not (inst and name and ver):
            continue
        out.append(IpEntry(instance=inst, name=name, version=ver, attrs=a))
    return out


def find_ip(machine: Machine, refname: str, instance: str):
    """Find an IP by instance name (case-insensitive). Returns None when absent.

    ⚠ **`instance` is a data key, not a family specialization**: it is the `Name`
    of `<IP Name="RCC" Version="…"/>` in the device XML, the same on every family.
    So `find_ip(…, "RCC")` / `"GPIO"` / `"DMA"` in the code **reads data**; it is
    not a per-family branch (which this tool forbids). Measured counter-example: DMA
    is named `DMA` in the device XML (one entry covers DMA1/DMA2), so
    `find_ip(…, "DMA1")` finds nothing.
    """
    for e in ip_entries(machine, refname):
        if e.instance.lower() == instance.lower():
            return e
    return None


def modes_xml_path(machine: Machine, entry: IpEntry) -> Path:
    """The `db/mcu/IP/<Name>-<Version>_Modes.xml` file of one IP."""
    return machine.db / "mcu" / "IP" / ("%s-%s_Modes.xml"
                                        % (entry.name, entry.version))


def ip_type(text: str) -> str | None:
    """The `IPType` attribute of the `_Modes.xml` root (middleware / peripheral / ...).

    Based on the declaration in the db: CubeMX uses it to decide whether an IP
    becomes a `Peripheral` or a `Middleware`.
    """
    # Strip comments first: an `IPType="middleware"` inside a comment is not a declaration.
    m = re.search(r'IPType="([^"]+)"', _strip_comments(text)[:1500])
    return m.group(1).lower() if m else None


_IP_KIND_CACHE: dict = {}


def ip_is_middleware(modes_dir: Path, ip_name: str) -> bool:
    """Whether this IP name is **middleware** in the db (`IPType="middleware"`).

    Used to drop "other middleware" from host requirements — a token in a condition
    expression is **not necessarily** a host: LwIP's conditions mention `S_MBEDTLS`
    (= depends on MBEDTLS) and `S_FREERTOS`; those are **dependencies**, not
    **hosts**, and treating them as hosts gives wrong advice.
    The criterion uses the same data as `mwmodes.list_middleware` (`IPType`),
    cached by directory + name.
    """
    key = (str(modes_dir), (ip_name or "").upper())
    got = _IP_KIND_CACHE.get(key)
    if got is not None:
        return got
    val = False
    try:
        for p in modes_dir.glob("%s-*_Modes.xml" % ip_name):
            try:
                head = p.read_bytes()[:2000].decode("latin-1", "replace")
            except OSError:
                continue
            if ip_type(head) == "middleware":
                val = True
                break
    except OSError:
        val = False
    _IP_KIND_CACHE[key] = val
    return val


def ref_modes(path: Path) -> tuple:
    """Mode names in `_Modes.xml`, returned as ``(selectable modes, base modes)``.

    The split is ``Abstract="true"``: that marks an internal base mode that others
    attach to via ``BaseMode=".."`` but cannot select themselves.

    **Read it through the XML structure**, do not regex raw text. Measured
    2026-09-28: scanning raw text also counts **commented-out** `<RefMode>`, so
    **63/1395** files in the local db reported ghost modes
    (`ADC-aditf2_v1_1_Cube_Modes.xml`'s `WatchDogCopy`,
    `External-Trigger-for-Injected-conversion` and others — all commented out).
    """
    modes, abstract = [], []
    for e in ET.parse(str(path)).getroot().iter():
        if _local(e.tag) != "RefMode":
            continue
        name = e.get("Name")
        if not name:
            continue
        if (e.get("Abstract") or "").lower() == "true":
            abstract.append(name)
        else:
            modes.append(name)
    return modes, abstract


# `RemoveCondition="..."` on a mode: **when the expression holds, the mode is removed**.
# That is the host gating itself.
# Expressions mix two kinds of symbols, and only the first kind can become advice:
#   1. `<host IP>:$modeexist_<mode>` — measured: FATFS's
#      `!FMC:$modeexist_Sram1 & !FSMC:$modeexist_Sram1 & ...`,
#      USB_DEVICE's `!(USB_OTG_HS:$modeexist_External_Phy)`
#   2. device/family symbols (`STM32H7`, `STM32H745_755`, `DIE423`) — these say
#      whether the chip has something and give no actionable advice
# So only kind 1 is extracted: **host IP + the mode it must be in**. That way every
# middleware and chip uses the same logic, with no "which middleware needs which
# host" table to maintain.
_MODE_REQ_RE = re.compile(r"([A-Za-z0-9_]+)(?:\$context)?:\$modeexist_([A-Za-z0-9_\-]+)")


def _attr(attrs: str, name: str):
    for k, v in _ATTR_RE.findall(attrs or ""):
        if k == name:
            return v
    return None


def _ip_name_of(path: Path) -> str:
    """`_Modes.xml` file name → IP name (`<Name>-<version>_Modes.xml`, text before the first `-`)."""
    stem = path.name
    if stem.endswith("_Modes.xml"):
        stem = stem[: -len("_Modes.xml")]
    return stem.split("-", 1)[0]


def _mode_frames(path: Path) -> list:
    """Flatten the `<Mode>` elements of `_Modes.xml` by **nesting level**.

    Each item is ``{"index", "parent", "name", "user_name", "remove_condition",
    "conditions": [{expression, diagnostic}], "semaphores": [..], "depth"}``.

    Why the nesting matters: USBX writes its host requirement on the **parent**
    `<Mode Name="Device_Class_FS">` `<Condition>`
    (`USBX-v1.0.0_H5_Cube_Modes.xml:1663`), while the leaf mode
    `UX_Device_CoreStack` only requires `Core_System` in its own `<Condition>`.
    Without the ancestors the host requirement is missed.

    **Read it through the XML structure**, do not regex raw text. Raw-text scanning
    loses three things on this data (measured 2026-09-28 over 1395 local
    `*_Modes.xml` files):

    - **`<Condition>` elements whose attribute values contain `>` are dropped
      whole** — a `Diagnostic` like `…supporting FreeRTOS >` makes the regex
      `[^>]*` stop at that `>`. There are **31007 such conditions across 247
      files**, and they are exactly where host gating comes from;
    - **commented-out `<Mode>`/`<Condition>`/`<Semaphore>` are taken as real**
      (674 modes, 3296 conditions and 2115 semaphores sit inside comments);
    - ET decodes `&amp;` on the way (so the caller's `.replace("&amp;", "&")`
      becomes a no-op and the result is unchanged).
    """
    out: list = []
    stack: list = []

    def walk(el) -> None:
        for child in el:
            nm = _local(child.tag)
            if nm == "Mode":
                frame = {
                    "index": len(out),
                    "parent": stack[-1]["index"] if stack else None,
                    "name": child.get("Name"),
                    "user_name": child.get("UserName"),
                    "remove_condition": child.get("RemoveCondition"),
                    "conditions": [],
                    "semaphores": [],
                    "depth": len(stack),
                }
                out.append(frame)
                stack.append(frame)
                walk(child)
                stack.pop()
            elif nm == "Condition":
                if stack:
                    stack[-1]["conditions"].append({
                        "expression": child.get("Expression") or "",
                        "diagnostic": child.get("Diagnostic"),
                    })
                walk(child)
            elif nm == "Semaphore":
                val = (child.text or "").strip()
                if stack and val:
                    stack[-1]["semaphores"].append(val)
            else:
                walk(child)

    walk(ET.parse(str(path)).getroot())
    return out


def mode_names(path: Path) -> dict:
    """The **leaf mode names** of this peripheral plus their UI display names (both
    from the offline db).

    ## Why it exists (2026-09-30)

    `resolve_mode` used to need two online commands per question
    (`get mode available` + `get mode all`), while "does this name exist in the
    db at all" **can be answered offline**. Measured: the online set is a **subset**
    of the offline leaf set (429 FMC modes match name by name; "online only" is
    always empty), so:

    - the name **is** in the offline leaf set → it is a legal internal name and the
      online query is unnecessary ("can it be set now" is asked separately through
      `not_available_hint` and `available`);
    - the name is **not** in the leaf set and **not** among the display names →
      the db does not have it either, so online cannot have it → report an error
      directly, again without an online query;
    - the name is **only** among the display names → take the online path so the
      "what you passed is a UI display name" message can be produced (display names
      collide, so they must not be guessed silently).

    :return: ``{"leaves": set, "display": set}``; when the peripheral is not in the
        db, ``{"leaves": None, "display": None}`` (the caller falls back to the
        online criterion).
    """
    frames = _mode_frames(path)
    if not frames:
        return {"leaves": set(), "display": set()}
    parent_idx = {f.get("parent") for f in frames}
    leaves, display = set(), set()
    for f in frames:
        if f["index"] in parent_idx:
            continue                       # not a leaf
        nm = f.get("name")
        if not nm:
            continue
        leaves.add(nm)
        if f.get("user_name"):
            display.add(f["user_name"])
    return {"leaves": leaves, "display": display}


def mode_blocks(path: Path) -> dict:
    """The "**block → leaf modes**" grouping of `_Modes.xml`.

    Returns ``{"leaf_to_block": {leaf: block}, "blocks": {block: [leaf, ...]},
    "roots": [block, ...]}``.

    ## Why it exists (measured 2026-09-29)

    Some peripherals have **several parallel blocks on one instance**, each choosing
    its own chip select and timing. FMC is one: NOR/PSRAM has 4 blocks, SDRAM 2,
    NAND 2, CompactFlash 1. But `get mode active <instance>` reports **only one**
    mode (measured: FMC reports `NorPsramChipSelect1_1`), so it cannot answer "what
    state is this instance in".

    The criterion is **not the suffix of the mode name** (that is just a naming
    habit) but the db hierarchy: **a depth-0 `<Mode>` is a block root**, and the
    leaves below it belong to it. Measured on FMC: 9 block roots and 503 leaves,
    106/106/106/106/11/11/21/18/18 per block; `NorPsramChipSelect1_1`, `Lcd1`,
    `A16_1` and `16b-d1` all belong to `NorFlash1`, while
    `NorPsramChipSelect2_2`, `MuxedPsram2` and `16b-da2` belong to `NorFlash2`.

    A "leaf" is a mode that **no other `<Mode>` uses as parent** (those are what
    `set mode` accepts).
    """
    frames = _mode_frames(path)
    if not frames:
        return {"leaf_to_block": {}, "blocks": {}, "roots": []}

    parent_idx = {f.get("parent") for f in frames}
    by_index = {f["index"]: f for f in frames}
    leaf_to_block: dict = {}
    blocks: dict = {}
    roots: list = []
    for f in frames:
        idx = f["index"]
        if idx in parent_idx:
            continue                      # not a leaf
        name = f.get("name")
        if not name:
            continue
        # Walk up to the depth-0 level
        cur = f
        while cur.get("parent") is not None:
            cur = by_index.get(cur["parent"], cur)
            if cur is None:
                break
        block = (cur or {}).get("name")
        if not block:
            continue
        leaf_to_block[name] = block
        blocks.setdefault(block, []).append(name)
        if block not in roots:
            roots.append(block)
    return {"leaf_to_block": leaf_to_block, "blocks": blocks, "roots": roots}


def mode_xor_groups(path: Path) -> dict:
    """The "**exclusive group → leaf modes**" grouping of `_Modes.xml`
    (`ModeLogicOperator` XOR).

    Returns ``{"leaf_to_group": {leaf: group name}, "groups": {group: [leaf, ...]}}``.

    ## Why it exists (Gitea #162)

    SPI's `_Modes.xml` has two groups under the root
    `<ModeLogicOperator Name="OR">`: `Mode` (`Full_Duplex_Master` /
    `Full_Duplex_Slave` / …) and `NSS_Signal` (`NSS_Signal_Hard_Input` /
    `NSS_Signal_Hard_Output`).
    **Inside a group it is XOR (mutually exclusive); across groups they coexist.**
    So `Full_Duplex_Master` + `Full_Duplex_Slave` must be rejected, while
    `Full_Duplex_Master` + `NSS_Signal_Hard_Output` must both be storable.

    ⚠ The criterion is the XML `ModeLogicOperator`, **not the prefix/suffix of the
    mode name** (that is just a naming habit).
    """
    out = {"leaf_to_group": {}, "groups": {}}
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError):
        return out

    def walk(elem, group):
        for child in elem:
            tag = _local(child.tag)
            if tag == "ModeLogicOperator":
                g = group
                if (child.get("Name") or "").upper() == "XOR" and not g:
                    g = "(root XOR)"
                walk(child, g)
            elif tag == "Mode":
                name = child.get("Name")
                if any(_local(c.tag) == "ModeLogicOperator" for c in child):
                    walk(child, name or group)      # this level is a group
                elif group and name:                # this level is a leaf
                    out["leaf_to_group"].setdefault(name, group)
                    bucket = out["groups"].setdefault(group, [])
                    if name not in bucket:
                        bucket.append(name)

    walk(root, None)
    return out


_SEM_INDEX_CACHE: dict = {}

#: **Compact** regex for the "semaphore → which mode" index. Three branches only,
#: much faster than parsing every file in full (measured over 1395 local db files:
#: **1.54 s** vs 24.7 s; the extra second is the cost of stripping comments, see the
#: step inside `_semaphore_index`).
#: `<ModeLogicOperator` is not matched by `<Mode\b` (no word boundary between `e`
#: and `L`).
#:
#: ⚠ The attribute part **must swallow quoted strings as a whole** (`"[^"]*"`), not
#: `[^>]*`: 263 `<Mode>` elements in the local db (66 files, e.g.
#: `OCTOSPI-octospi_v5_2_Cube_Modes.xml`) have `>` inside an attribute value, and
#: `[^>]*` truncates at that `>`, losing `Name`/`UserName` — the display names of
#: those modes would become `None` in the index. The old `<Mode>` spelling (no
#: attributes) is accepted too.
_SEM_SCAN_RE = re.compile(
    r"<Mode\b((?:[^>\"]|\"[^\"]*\")*)>"        # open tag (with or without attrs) → group 1
    r"|</Mode>"                                # close tag
    r"|<Semaphore>([^<]*)</Semaphore>")        # semaphore → group 2


def _semaphore_index(modes_dir: Path) -> dict:
    """``{semaphore token: (IP name, mode internal name, mode display name)}`` —
    scanning all of `db/mcu/IP/`.

    ## Why it must span files

    Some middleware write the host requirement into the **parent `<Mode>`'s
    `<Condition>`**, and the expression references a **semaphore token** rather
    than `<host>:$modeexist_<mode>`::

        <!-- db/mcu/IP/USBX-v1.0.0_H5_Cube_Modes.xml:1663 (parent Mode Device_Class_FS)-->
        <Condition Expression="(SEMAOoUSBOoOTGOoFSOoDEVICEOoFOROoUSBX | OTG_DUAL_ROLE_DEVICE_FS)
                               &amp; !SEM…UXOoDeviceOoCoreStackJjRTOSIiMODE_HS"
                   Diagnostic="Active only when USB IP is configured as Device_Only(FS)" />

    That token is emitted by a mode in **another file**::

        <!-- the USB Modes file under db/mcu/IP -->
        <Mode Name="Device" UserName="Device_Only">
            <Semaphore>MW_USB_MW_DEVICE</Semaphore>
            <Semaphore>SEMAOoUSBOoOTGOoFSOoDEVICEOoFOROoUSBX</Semaphore>
        </Mode>

    So "token → which mode of which IP" requires an index over the whole database.
    About half a second per run, **cached per directory**.
    """
    key = str(modes_dir)
    got = _SEM_INDEX_CACHE.get(key)
    if got is not None:
        return got
    idx = {}
    try:
        files = sorted(modes_dir.glob("*_Modes.xml"))
    except OSError:
        files = []
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"<Semaphore>" not in data:      # quick filter: skip files without semaphores
            continue
        ip = _ip_name_of(p)
        stack = []
        # **Strip comments first**: `<Mode>`/`<Semaphore>` inside comments do not
        # count. Raw-text scanning must do this; it costs one linear replace and the
        # quick filter above stays (a full ET parse takes 24.7 s, so that path is not
        # an option).
        text = _strip_comments(data.decode("latin-1", "replace"))
        for m in _SEM_SCAN_RE.finditer(text):
            tok = m.group(0)
            if tok == "</Mode>":
                if stack:
                    stack.pop()
                continue
            if tok.startswith("<Mode"):
                stack.append(m.group(1) or "")
                continue
            val = (m.group(2) or "").strip()
            if val and stack:
                attrs = stack[-1]
                idx.setdefault(val, (ip, _attr(attrs, "Name"),
                                     _attr(attrs, "UserName")))
    _SEM_INDEX_CACHE[key] = idx
    return idx


#: A token may carry a `$suffix` such as `S_FREERTOS$context` (measured in LORAWAN's
#: conditions). ⚠ The suffix **must be consumed with it**: taking only the head
#: leaves a **stray token** in the stream, the recursive descent fails at once and
#: falls back to "one term per token", losing the AND/OR structure (Gitea #172).
_EXPR_TOKEN_RE = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\$[A-Za-z_][A-Za-z0-9_]*)?|&|\||!|\(|\))")


def _dnf_terms(expr: str, keep) -> list:
    """Boolean expression → **DNF**: ``[[token, ...], ...]`` (outer list is OR, each
    item is AND).

    ## Why the structure must be preserved (Gitea #172 review)

    The old code **flattened** the tokens of an expression into one list and let the
    caller pass when any of them matched — that treats every condition as **OR**.
    Measured counter-example: one branch of `LoRaWAN_Enabled` is
    `S_SUBGHZ & S_IPCC_Enabled` (**AND**); flattened, a single host was enough to
    pass, so a plan missing `SUBGHZ` slipped through.

    ## Rules

    - `&` = and; `|` = or; parentheses as usual;
    - **`!token` is dropped** (that means "must be off", not a host requirement;
      see `mode_requirements`);
    - tokens where `keep(tok)` is false are dropped too (device/family symbols give
      no actionable advice);
    - **unparsable → all recognized tokens collapse into one AND group**
      (Gitea #172 review). Falling back to "one term per token" is forbidden: that
      treats an unknown condition as OR and a plan may miss a required host.
      (Why not reject outright: LWIP and PDM2PCM expressions in the real db are
      genuinely unparsable.)
    """
    toks = _EXPR_TOKEN_RE.findall(expr or "")
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def eat(want=None):
        cur = peek()
        if want is not None and cur != want:
            raise ValueError("expect %r, got %r" % (want, cur))
        pos[0] += 1
        return cur

    def atom():
        t = peek()
        if t == "!":
            eat("!")
            atom()
            return [frozenset()]          # negated token: no constraint added
        if t == "(":
            eat("(")
            v = orexpr()
            eat(")")
            return v
        if t is None or t in ("&", "|", ")"):
            raise ValueError("bad token %r" % t)
        eat()
        # `$suffix` is only a modifier (`S_FREERTOS$context`); the index is keyed by
        # the name **without** the suffix.
        base = t.split("$", 1)[0]
        return [frozenset([base])] if keep(base) else [frozenset()]

    def andexpr():
        out = [frozenset()]
        while True:
            v = atom()
            out = [a | b for a in out for b in v]
            if peek() == "&":
                eat("&")
                continue
            break
        return out

    def orexpr():
        out = andexpr()
        while peek() == "|":
            eat("|")
            out = out + andexpr()
        return out

    try:
        terms = orexpr()
        if peek() is not None:
            raise ValueError("trailing %r" % peek())
    except Exception:                            # noqa: BLE001
        # ⚠ **An unparsable expression must not be silently relaxed into OR**
        # (Gitea #172 review). The old fallback was "one term per token" — that
        # treats every condition as **OR** and a plan may miss a required host (the
        # reviewer's synthetic counter-example: `S_A && S_B` became two groups).
        # The fix (the reviewer's second option, "keep the constraint"): treat the
        # recognized tokens as **one AND group** — undecidable means **stricter**,
        # never looser.
        #
        # Why not reject outright: the real db already contains unparsable forms —
        # `IF(MW_ETH_MW,1,0)` (LWIP) and `CRC & $IpInstance=PDM2PCM_M$Index`
        # (PDM2PCM). Rejecting would wrongly reject both middleware entirely. Each of
        # those forms has exactly one recognizable token, so one AND group is right.
        picked = sorted({t for t in toks
                         if t not in ("&", "|", "!", "(", ")") and keep(t)})
        return [picked]
    return [sorted(t) for t in terms]


def mode_requirements(path: Path) -> dict:
    """``{mode internal name: [requirement, ...]}`` — both kinds of db gating.

    Each requirement is a dict::

        {"source": "RemoveCondition" | "Condition" | "ancestor Condition",
         "ip": "USB", "mode": "Device", "user_name": "Device_Only",
         "same_ip": False,          # is this another mode of the **same** middleware
         "negated": False,          # the expression says `!token` (this must be **off**)
         "diagnostic": "...", "semaphore": "..."}

    The two sources **mean different things; do not mix them**:

    - `<Mode RemoveCondition="…">` = **removal condition**: when the expression holds,
      the mode is taken off the available list (the old shape, written as
      `<host>:$modeexist_<mode>`);
    - `<Mode><Condition Expression="…">` = **enable condition**: every level of the
      parent chain must hold for the mode to be enabled. USBX uses this form, and it
      **sits on an ancestor Mode** and references semaphore tokens.

    ⚠ **`negated` must always be honoured**: `!S_FREERTOS_NATIVE` means "FreeRTOS
    must **not** be in use", not "FreeRTOS is required". Treating it as a positive
    requirement inverts the conclusion (measured in FATFS's `SDIO` condition).
    """
    idx = _semaphore_index(path.parent)
    my_ip = _ip_name_of(path)
    frames = _mode_frames(path)
    by_index = {f["index"]: f for f in frames}
    out: dict = {}

    def add(name, req):
        lst = out.setdefault(name, [])
        # ⚠ **The key must include `group`** (Gitea #172): the same (ip, mode) may
        # appear in two different AND terms; dropping the second loses the AND/OR
        # structure.
        k = (req["source"], req["ip"], req["mode"], req["negated"],
             req.get("group"))
        if k not in {(x["source"], x["ip"], x["mode"], x["negated"],
                      x.get("group")) for x in lst}:
            lst.append(req)

    for f in frames:
        name = f["name"]
        if not name:
            continue

        # (1) the mode's own RemoveCondition (old shape)
        rc = (f["remove_condition"] or "").replace("&amp;", "&")
        # `RemoveCondition` = **removal condition**: when the expression is true the
        # mode is removed. The measured shape is "a run of `!host:$modeexist_mode`
        # joined by `&`" → available when any host is in place. So **each host
        # becomes its own AND term** (= OR), matching the old behaviour.
        for gi, (host, mode) in enumerate(_MODE_REQ_RE.findall(rc)):
            add(name, {"source": "RemoveCondition", "ip": host, "mode": mode,
                       "user_name": None, "negated": False,
                       "same_ip": host.upper() == my_ip.upper(),
                       "diagnostic": None, "semaphore": None,
                       "group": ("RemoveCondition", gi)})

        # (2) the mode's own and **all ancestor** Conditions (new shape: semaphore tokens)
        chain, cur = [], f
        while cur is not None:
            chain.append(cur)
            cur = by_index.get(cur["parent"]) if cur["parent"] is not None else None
        for depth, node in enumerate(chain):
            src = "Condition" if depth == 0 else "ancestor Condition"
            for ci, cond in enumerate(node["conditions"]):
                expr = cond["expression"] or ""
                # ⚠ **Group by DNF** (Gitea #172): inside one expression `&` is AND;
                # across expressions (family branches) it is OR. `group` is the index
                # of that AND term.
                terms = _dnf_terms(
                    expr, lambda t: bool((idx.get(t) or ("",))[0]))
                for ti, term in enumerate(terms):
                    for tok in term:
                        owner = idx.get(tok)
                        if not owner or not owner[0]:
                            continue
                        ip, mode, user = owner
                        add(name, {
                            "source": src, "ip": ip, "mode": mode,
                            "user_name": user,
                            "same_ip": ip.upper() == my_ip.upper(),
                            "negated": False,
                            "diagnostic": cond["diagnostic"],
                            "semaphore": tok,
                            "group": (src, depth, ci, ti),
                        })
    return out


def mode_host_requirements(path: Path, exclude_ips=()) -> dict:
    """``{mode name: [(host IP, required mode), ...]}`` — which IP must be in which
    mode for this mode.

    **Both sources are collected**:

    (1) `<Mode RemoveCondition="host:$modeexist_mode">` (old shape, readable as is);
    (2) semaphores in the `<Condition>` of the mode itself or of an **ancestor**
        `<Mode>` (USBX uses this form; see :func:`_semaphore_index` and
        :func:`mode_requirements`).

    Device/family symbols are ignored (no actionable advice); **`!token` is ignored
    too** (that means "must be off", not a host). One mode may have several
    candidate hosts (FATFS's SDIO can hang off SDMMC or SDIO); duplicates are
    removed and the rest returned in appearance order.

    :param exclude_ips: IP names to exclude (case-insensitive). Tokens in condition
        expressions **also point at other middleware** (e.g. LwIP's `S_MBEDTLS`,
        `FREERTOS$context`); those are dependencies, not hosts. Callers usually pass
        the **middleware list**, leaving only the real peripheral hosts.
    """
    skip = {x.upper() for x in (exclude_ips or ())}
    out: dict = {}
    for name, reqs in mode_requirements(path).items():
        for r in reqs:
            if not r["ip"] or r["ip"].upper() in skip or r.get("negated"):
                continue
            pair = (r["ip"], r["mode"])
            if pair not in out.setdefault(name, []):
                out[name].append(pair)
    return out


def ref_parameters(path: Path) -> dict:
    """All `<RefParameter>` elements of `_Modes.xml`, as ``{parameter name: {...}}``.

    ## Why one parameter name has several variants

    The same parameter name often appears several times in the db, each with its own
    `<Condition>`. **Which one applies depends on the current state**, and reading a
    parameter returns only the first variant that is in use.
    Two measured examples:

    - `NVIC.PriorityGroup`: two variants; the one with `Expression="S_FREERTOS"`
      only declares `NVIC_PRIORITYGROUP_4`, while `Expression="!S_FREERTOS"`
      declares all five 0~4. That is the real mechanism behind "only group 4 is
      settable once RTOS is enabled".
    - `NVIC.PreemptionPriority` / `SubPriority`: five variants each, conditioned on
      the priority group, with `Max` 0/1/3/7/15 and 15/7/3/1/0.

    ## Returned fields

    ===============  =========================================================
    `type`           type of the first variant
    `default`        first non-empty, non-`null` default value
    `values`         union of the candidate values of **all** variants (for humans)
    `list_values`    union of the candidates of the ``Type="list"`` variants only —
                     **this is the set usable for hard validation**, see below
    `strict`         `list_values` is non-empty
    `max` / `min`    range declared by the `integer` variants (NVIC priority, clock
                     divider)
    `unit`           unit declared by the db (often `MHz` on clock parameters)
    `display`        display conversion declared by the db (e.g. `value/1000000`)
    `value_numbers`  enum value → its numeric meaning (taken from `Comment`, e.g.
                     `RCC_SYSCLK_DIV4` → 4). Dividers use it to fold an enum into a
                     number.
    `visible`        the db `Visible` attribute (`false` = **hidden in the UI**, but
                     the value exists; recorded as false only when every variant says
                     false)
    `actions`        `Action` on candidate values (measured: GPDMA's
                     `<PossibleValue … Action="Disable"/>` = this request line is
                     unusable in the current configuration)
    `variants`       per variant ``{type, values, value_numbers, condition,
                     diagnostic, max, min, visible, actions}``
    ===============  =========================================================

    ## Why hard validation can only use `list_values`

    The `possible_value ip parameters` CLI checker **is only strict when the variant
    type is `list`**: it judges `list` / `integer` / `double` / `array` properly,
    but the `uniqueElementList` branch **has no check** and accepts anything.

    Measured: `possible_value ip parameters FREERTOS MEMORY_ALLOCATION 9` returns OK
    (it uses the `uniqueElementList` variant) although `9` is not among the declared
    `0,1,2`, and the value is **stored silently**. So the checker cannot be the gate
    on its own.
    """
    out = {}
    for el in ET.parse(str(path)).getroot().iter():
        if _local(el.tag) != "RefParameter":
            continue
        name = el.get("Name")
        if not name:
            continue
        # **A self-closing `<RefParameter/>` has no candidate values by definition**
        # and ET sees that directly: it has no child nodes. The old regex scan had to
        # test `text[m.end()-2:m.end()] == "/>"` by hand, and **guessed wrong**:
        # parameters whose attribute values contain `>` (829 of them across 46 files in
        # the local db) make `[^>]*` stop early, the self-closing test fails and the
        # body runs on to the `</RefParameter>` of **the next parameter**, so this
        # parameter "inherits" someone else's candidates.
        #
        # [measured] `VoltageScaling` in
        # `db/mcu/IP/PWR-STM32H7_pwr_v1_0_Modes.xml:85` is self-closing with no
        # candidates, yet before the fix it read back the next parameter
        # (`TrimmingMode`)'s `__NULL` / `USER_TRIMMING`. And `list_values` + `strict`
        # is exactly the **pre-write gate** criterion (`cmds/dma.py:323`,
        # `cmds/middleware.py:639`, `clockvalues.py:150`): a parameter with "no
        # candidates" would suddenly become "strictly validated", rejecting legal
        # values and accepting illegal ones.
        #
        # Commented-out `<PossibleValue>`/`<RefParameter>` (6332 / 2378 in the local
        # db) do not count either — ET ignores comments.
        pvs = [x for x in el.iter() if _local(x.tag) == "PossibleValue"]
        # Both candidate spellings are accepted: a `Value` attribute and plain text
        # content. **The order stays as before** (attribute form first, then text
        # form); do not change the order callers see.
        values = [x.get("Value") for x in pvs if x.get("Value")]
        values += [(x.text or "").strip() for x in pvs
                   if not x.get("Value") and (x.text or "").strip()]

        # **An enum value's numeric meaning lives in Comment.** Measured:
        #   <PossibleValue Comment="4" Value="RCC_SYSCLK_DIV4"/>
        #   <PossibleValue Comment="1" Value="RCC_APB2_DIV1"/>
        # For dividers the Comment is the divisor; for frequency parameters the
        # Comment is something else (e.g. "ADC Clksource PLL2P"), and when it is not
        # numeric nothing is recorded. The solver uses it to fold an "enum divider"
        # into a number.
        value_numbers = {}
        # **A `PossibleValue` may also carry a `Semaphore`**: a boolean token meaning
        # "the current value is me". [measured] The db attaches `D2PPRE2_1/_2/_4` to
        # `D2PPRE2 = RCC_APB2_DIV1/2/4`, and `Tim2Mul`'s value condition reads
        # `(D2PPRE2_1)&…`. Parsing it is what makes "what should this
        # condition-determined parameter be now" answerable.
        semaphores = {}
        # **A candidate value may also carry an `Action`**. Measured in
        # `GPDMA-STM32H5_dma3_Cube_Modes.xml:1612`:
        # `<PossibleValue Value="GPDMA1_REQUEST_SPI1_TX"
        #                 Semaphore="SEM_REQ_SPI1TX_$IpInstanceCH6"
        #                 Condition="!(SPI1_Used|I2S1_Used)" Action="Disable"/>`
        # meaning "this request line is unusable in the current configuration" (it is
        # greyed out in the UI). Reporting it tells the caller "this value can be
        # written but may have no effect".
        actions = {}
        for pv in pvs:
            v = pv.get("Value")
            if not v:
                continue
            c = pv.get("Comment")
            if c is not None:
                try:
                    value_numbers[v] = float(c)
                except (TypeError, ValueError):
                    pass
            s = pv.get("Semaphore")
            if s:
                semaphores[v] = s
            act = pv.get("Action")
            if act:
                actions[v] = act

        ptype = el.get("Type")
        dflt = el.get("DefaultValue")
        cmt = el.get("Comment")
        pmin = el.get("Min")
        pmax = el.get("Max")
        punit = el.get("Unit")
        pdisp = el.get("Display")
        # The variant's own `<Condition>`: take the **first** one (as before).
        cond = None
        for c in el.iter():
            if _local(c.tag) == "Condition":
                cond = c
                break
        cond_attrs = dict(cond.attrib) if cond is not None else {}

        # `Visible`: measured, 118 parameters in `RCC-STM32F102_rcc_v1_0_Modes.xml`
        # carry `Visible="false"` — hidden in the UI, but the value exists (it still
        # reads back normally).
        # ⚠ **No word-boundary escape here**: an earlier version was bitten twice —
        # a heredoc patch turned the escape into a **real backspace character 0x08**
        # (`inspect.getsource` printed nothing, so it looked like "the regex is right
        # but matches nothing"). Hence the plain `Visible="`.
        pvisible = el.get("Visible") or None

        # `IP="USB_OTG_HS,USB_OTG_FS"`: **who consumes** this readout. Names differ per
        # family, so this answers "which clock readout does this instance need"
        # instead of hard-coding parameter names.
        consumers = [x.strip() for x in (el.get("IP") or "").split(",") if x.strip()]
        variant = {
            "consumers": consumers,
            "type": ptype,
            "values": values,
            "value_numbers": value_numbers,
            "semaphores": semaphores,
            "actions": actions,
            "visible": (None if pvisible is None
                        else pvisible.lower() != "false"),
            # **The variant's own default** (added 2026-09-30). Why it is needed:
            # the merged `default` above takes "the first non-empty one", which **may
            # not belong to the variant in effect**. Measured: `SPI5.NSS` has 4
            # variants with different defaults (`SPI_NSS_HARD_OUTPUT` /
            # `SPI_NSS_HARD_INPUT` / `SPI_NSS_SOFT`×2); the first two carry a
            # `Condition` (`$IpInstance_NSSHARD_Output` / `_Input`) and the last two
            # are unconditional. The effective one is the **unconditional variant** →
            # default `SPI_NSS_SOFT`. This is the basis for the criterion "a key
            # missing from the `.ioc` means the default is used"
            # (see `cmds/apply._verify_from_iocs`).
            "default": dflt,
            "condition": cond_attrs.get("Expression"),
            "diagnostic": cond_attrs.get("Diagnostic"),
            "min": pmin,
            "max": pmax,
        }

        cur = out.setdefault(name, {
            "type": None, "default": None, "values": [], "comment": None,
            "list_values": [], "strict": False, "min": None, "max": None,
            "unit": None, "display": None, "value_numbers": {},
            "semaphores": {}, "consumers": [], "variants": [],
            # `visible` plus the `action` on candidate values
            "visible": None, "actions": {},
        })
        cur["variants"].append(variant)
        for k, s in semaphores.items():
            cur["semaphores"].setdefault(k, s)
        for k, a in actions.items():
            cur["actions"].setdefault(k, a)
        for consumer in consumers:
            if consumer not in cur["consumers"]:
                cur["consumers"].append(consumer)
        # `visible` takes the **conservative** direction: false only when "every
        # variant that declares Visible says false" (so a variant that omits it is not
        # treated as hidden).
        if pvisible is not None:
            vis = pvisible.lower() != "false"
            cur["visible"] = vis if cur["visible"] is None else (
                cur["visible"] and vis)
        if ptype and not cur["type"]:
            cur["type"] = ptype
        if dflt and dflt != "null" and not cur["default"]:
            cur["default"] = dflt
        if cmt and not cur["comment"]:
            cur["comment"] = cmt
        for v in values:
            if v not in cur["values"]:
                cur["values"].append(v)
        for k, num in value_numbers.items():
            cur["value_numbers"].setdefault(k, num)
        # Only list-type candidates can be used for hard validation; see the docstring
        if ptype and ptype.lower() == "list":
            for v in values:
                if v not in cur["list_values"]:
                    cur["list_values"].append(v)
        # The range is the union over all integer variants (the looser direction),
        # used for hints to callers
        if pmin:
            cur["min"] = _wider(cur["min"], pmin, lower=True)
        if pmax:
            cur["max"] = _wider(cur["max"], pmax, lower=False)
        if punit and not cur["unit"]:
            cur["unit"] = punit
        if pdisp and not cur["display"]:
            cur["display"] = pdisp
    for cur in out.values():
        cur["strict"] = bool(cur["list_values"])
    return out


def _wider(old, new, lower):
    """Take the wider end of two numeric strings (smaller min / larger max)."""
    try:
        a = float(old) if old is not None else None
        b = float(new)
    except (TypeError, ValueError):
        return old
    if a is None:
        return new
    return old if ((a <= b) if lower else (a >= b)) else new


def spec_for(specs: dict, ref_parameter):
    """Look up the definition of an element `refParameter` in the db parameter table.

    **`refParameter` may be several comma-separated names.** [measured] In F429's
    clock tree `PLLSource` is `PLLSourceVirtual,PLLSource` and `RTCClkSource` is
    `RCC_RTC_Clock_Source,RCC_RTC_Clock_SourceVirtual` (H750 has neither). Using the
    whole string as a key finds nothing — the solver then cannot read the mux's
    current value and emits an extra `clock set PLLSource 0` (measured).

    Try them in order and return the first one present in the db; when none is,
    return ``({}, None)``.
    """
    for name in str(ref_parameter or "").split(","):
        name = name.strip()
        if name and name in specs:
            return specs[name], name
    return {}, None


#: The device-name memo lives on the **client object**, not in a module global. Why:
#: each CLI command is a new process → new client → the memo starts empty, so **a
#: stale value can never be read across processes**; within one `apply` the client is
#: the same object, so the same question is asked once.
_REFNAME_ATTR = "_cubemx1_refname"


# --------------------------------------------------------------------------
# The **value-domain table** for pin-level GPIO parameters (2026-09-30, used by
# the "protocol-adaptive" path)
# --------------------------------------------------------------------------

#: GPIO **pseudo-parameters** on the CubeMX side: they are **not in the device db**
#: and CubeMX handles them itself.
#:
#: Basis (two places, both measured from stored files):
#:
#: 1. In CubeMX's **own board `.ioc` files**
#:    (`db/plugins/boardmanager/boards/*.ioc`), `GPIO_Label` appears more than
#:    twenty thousand times, yet it is **not** in any GPIO `_Modes.xml`;
#: 2. this tool's `pinout add-label` writes the same name (`<pin>.GPIO_Label` plus
#:    an entry in the `<pin>.GPIOParameters` list).
#:
#: Its "value domain" is not an enum but a C identifier (it becomes a `#define`),
#: so a synthetic `type=identifier` spec is provided here, keeping validation to a
#: single entry point.
GPIO_PSEUDO_PARAMS = {
    "GPIO_Label": {
        "type": "identifier", "default": None, "values": [], "list_values": [],
        "strict": False, "min": None, "max": None, "unit": None,
        "display": None, "value_numbers": {}, "semaphores": {},
        "variants": [], "visible": None, "actions": {},
        "comment": "user label (becomes a C `#define`)",
    },
}

#: Cache for `gpio_param_specs`, keyed by `(db dir, device name)`.
#: The device table is a **read-only db**, so one device is asked once per `apply`.
_GPIO_SPECS_CACHE: dict = {}

#: Cache for `ref_mode_parameters`, keyed by file path (one `_Modes.xml` parses once).
_REF_MODE_CACHE: dict = {}


def ref_mode_parameters(path: Path) -> dict:
    """**Which parameters each `<RefMode>` declares** in `_Modes.xml`:
    ``{mode name: [parameter names]}``.

    ## Why it exists (2026-09-30)

    `peripherals describe --mode X` used to **return no parameters for X**
    (measured: none when the peripheral is disabled, and when enabled it returned
    the parameters of the **currently active mode**, so `--mode` had no effect) —
    because the only way to ask, `get mode_param_list`, **works for the active mode
    only**. Yet the db declares them:

    .. code-block:: xml

        <RefMode Name="Full_Duplex_Master">
          <Parameter Name="VirtualType"><PossibleValue>VM_MASTER</PossibleValue></Parameter>
          <Parameter Name="Mode">…</Parameter>
          <Parameter Name="DataSize"/>
          <Parameter Name="BaudRatePrescaler"/>
          …
        </RefMode>

    So "which parameters does this mode have" **is readable offline**, without
    switching the project over first.

    ## Shape notes (do not confuse them when reading)

    The ones carrying `<PossibleValue>` (`Mode`/`Direction`/`VirtualType`) are
    **fixed by the mode itself**, not knobs the caller can turn; the ones without
    children (`DataSize`) are user parameters.
    **Both are collected here** — the online path (`get mode_param_list`) also
    returns both, and matching it is safer than filtering on our own initiative
    (a wrong filter makes callers believe a parameter does not exist).

    ⚠ **`<RefMode>` inside comments is not data**: whole blocks are commented out in
    the db (measured: 674 `<Mode>` elements sit inside comments in 1395 local
    `_Modes.xml` files), so :func:`_strip_comments` runs first.
    """
    key = str(path)
    if key in _REF_MODE_CACHE:
        return _REF_MODE_CACHE[key]
    out: dict = {}
    try:
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        _REF_MODE_CACHE[key] = out
        return out
    for m in re.finditer(r"<RefMode\s+([^>]*?)>(.*?)</RefMode>", text, re.S):
        nm = re.search(r'Name="([^"]+)"', m.group(1))
        if not nm:
            continue
        names = re.findall(r'<Parameter\s+Name="([^"]+)"', m.group(2))
        if names:
            out[nm.group(1)] = names
    _REF_MODE_CACHE[key] = out
    return out


def gpio_param_specs(machine: Machine, refname: str) -> dict:
    """The **pin-level GPIO parameter table** of this device: ``{parameter: spec}``
    (offline, read from the db).

    ## Why it exists

    `pins[].params` is "adaptive": **which keys can be written is decided by the
    data**, not by a whitelist we maintain (a new CubeMX release may add or drop
    some). This is that data — the `<RefParameter>` elements of
    `db/mcu/IP/GPIO-<device>_gpio_v*_Modes.xml`.

    ## What is filtered out (all criteria come from the db itself)

    - `Visible="false"` ones (`GPIO_AF` / `GPIOx` / `GPIO_Pin`) — hidden in the UI;
      their values are computed by CubeMX from the pin, they are not knobs;
    - ones with **neither a value domain nor a range** (`strict=False` and no
      `min/max`) — legality cannot be judged, so including them only produces
      "written, but is it right?".

    Then `GPIO_PSEUDO_PARAMS` is layered on top (`GPIO_Label` and other CubeMX
    pseudo-parameters).

    ## Measured shape (F429, 12 parameters)

    `GPIO_Mode` / `GPIO_ModeDefaultEXTI` / `GPIO_ModeDefaultOD` /
    `GPIO_ModeDefaultOutputPP` / `GPIO_ModeDefaultPP` / `GPIO_Mode_For_EXTI_LL` /
    `GPIO_Pu` / `GPIO_PuPd` / `GPIO_PuPdOD` / `GPIO_Speed` /
    `GPIO_Speed_High_Default` / `PinState` + `GPIO_Label`.

    **It must be fetched again per family**: H7 adds `GPIO_FM6~FM9`,
    `PinAttribute` and the `SYSCFG_*` batch (same code, different device name →
    different table).
    """
    key = (str(machine.db), refname)
    if key in _GPIO_SPECS_CACHE:
        return _GPIO_SPECS_CACHE[key]

    out = {}
    try:
        e = find_ip(machine, refname, "GPIO")
        if e is not None:
            for name, spec in ref_parameters(modes_xml_path(machine, e)).items():
                if spec.get("visible") is False:
                    continue
                if not spec.get("strict") and spec.get("min") is None \
                        and spec.get("max") is None:
                    continue
                out[name] = spec
    except Exception:
        out = {}                       # undecidable → empty table; callers only warn
    out.update(GPIO_PSEUDO_PARAMS)
    _GPIO_SPECS_CACHE[key] = out
    return out


def project_refname(client) -> str:
    """Device name of the currently loaded project. Errors when no project is loaded.

    ## Why it is memoized per session (measured 2026-09-30)

    In one measured `apply` run (fixture plan) this command was asked **69 times,
    69 seconds** — **32%** of the 213-second run, far more than the single
    `config load` (8.7 s). Every question was the same: "which chip is the current
    project". The repeats come from deep call sites: once per pin
    (`pinmap.readback_names`), twice per clock item (`_clock_readback_names` runs
    both while collecting commands and while reading results), once per clock value
    (`clockvalues.check_value`)…

    ## Why it is safe

    The device name **changes only when the current project is replaced**. So:
    - the memo lives on the client (one client per `apply`; on the command line each
      command is a new process);
    - `invoke.run` drops it for **project-switching** commands (`config load` /
      `load` / `loadboard` / `project create-from*` …, see
      `invoke._SWITCHES_PROJECT`);
    - configuration commands (`set mode` / `clock set` / `dma add` …) **do not drop
      it** — the device name cannot change because of them, and that is exactly the
      large saving.
    """
    memo = getattr(client, _REFNAME_ATTR, None)
    if memo:
        return memo

    from . import parse as P
    from .invoke import run, run_or_empty

    refname = run_or_empty(client, "get mcu name", P.single_value,
                           what="read device name")
    if not refname:
        raise IpXmlError(
            "cannot read the device name, which means no project is loaded. "
            "Load one first with `cubemx1 start --project <project.ioc>`.")
    try:
        setattr(client, _REFNAME_ATTR, refname)
    except Exception:
        pass                       # a read-only client (stub) cannot memo; unchanged
    return refname
