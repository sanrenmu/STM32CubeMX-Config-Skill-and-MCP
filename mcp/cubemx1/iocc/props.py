"""IOC file (Java Properties format) parsing and writing back.

Basis (measured behaviour):
- `.ioc` is read and written with Java Properties semantics: ISO-8859-1 byte decoding, backslash-u escapes, trailing-backslash line continuation, `=` / `:` / whitespace as separators, later duplicate keys win, comments start with `#` / `!`;
- on save it always writes `File.Version` and adds no timestamp (measured).

This module does two things only: parse (faithful, no strip) and write back (sorted + escaped, deterministic). Upper layers decide which key belongs to whom and what type its value has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BS = chr(92)                 # backslash
BLANKS = " " + chr(9) + chr(12)
SEPARATORS = "=:"
CTRL = {"t": chr(9), "n": chr(10), "r": chr(13), "f": chr(12)}


class IocFormatError(ValueError):
    """IOC format error. Carries a 1-based line number to point at the source location."""

    def __init__(self, line_no: int, message: str, key: str | None = None):
        self.line_no = line_no
        self.key = key
        where = "line %d" % line_no
        if key:
            where += " (key %r)" % key
        super().__init__("%s: %s" % (where, message))


@dataclass
class IocDoc:
    """Parse result: ordered key/value pairs, source line of each key, raw text (optional)."""

    items: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)
    duplicates: list = field(default_factory=list)
    source: str | None = None
    raw: str | None = None

    def __getitem__(self, key):
        return self.items[key]

    def __contains__(self, key) -> bool:
        return key in self.items

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def get(self, key, default=None):
        return self.items.get(key, default)

    def keys(self):
        return self.items.keys()

    def values(self):
        return self.items.values()

    def pairs(self):
        return self.items.items()

    def line_of(self, key) -> int | None:
        return self.lines.get(key)

    def as_dict(self) -> dict:
        return dict(self.items)


def _logical_lines(text: str):
    """Split into logical lines by Java Properties rules; yield (first physical line number, content)."""
    physical = text.splitlines()
    index = 0
    total = len(physical)
    while index < total:
        start = index
        current = physical[index]
        index += 1
        while True:
            trailing = 0
            for ch in reversed(current):
                if ch == BS:
                    trailing += 1
                else:
                    break
            if trailing % 2 == 0 or index >= total:
                break
            current = current[:-1] + physical[index].lstrip(BLANKS)
            index += 1
        yield start + 1, current


def _unescape(raw: str, line_no: int, what: str, key: str | None) -> str:
    """Handle escapes in .ioc (faithful, no normalization)."""
    if BS not in raw:
        return raw
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != BS:
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            raise IocFormatError(line_no, "lone trailing backslash (%s)" % what, key)
        nxt = raw[i]
        if nxt == "u":
            hexed = raw[i + 1:i + 5]
            if len(hexed) != 4 or any(c not in "0123456789abcdefABCDEF" for c in hexed):
                raise IocFormatError(line_no, "backslash-u must be followed by 4 hex digits (%s)" % what, key)
            out.append(chr(int(hexed, 16)))
            i += 5
            continue
        if nxt in CTRL:
            out.append(CTRL[nxt])
            i += 1
            continue
        out.append(nxt)
        i += 1
    result = "".join(out)
    if any(chr(0xD800) <= ch <= chr(0xDFFF) for ch in result):
        try:
            return result.encode("utf-16", "surrogatepass").decode("utf-16")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return result
    return result


def _split_key_value(line: str, line_no: int):
    """Split into (raw key, raw value) by Java Properties rules."""
    n = len(line)
    i = 0
    escaped = False
    key_end = None
    while i < n:
        ch = line[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == BS:
            escaped = True
            i += 1
            continue
        if ch in SEPARATORS:
            key_end = i
            break
        if ch in BLANKS:
            key_end = i
            break
        i += 1
    if key_end is None:
        return line, ""
    j = key_end
    while j < n and line[j] in BLANKS:
        j += 1
    if j < n and line[j] in SEPARATORS:
        j += 1
    while j < n and line[j] in BLANKS:
        j += 1
    return line[:key_end], line[j:]


def loads(data, source: str | None = None) -> IocDoc:
    """Parse IOC text/bytes. bytes are decoded as ISO-8859-1 (same as Java)."""
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("iso-8859-1")
    else:
        text = str(data)
    doc = IocDoc(source=source, raw=text)
    for line_no, logical in _logical_lines(text):
        stripped = logical.lstrip(BLANKS)
        if not stripped:
            continue
        if stripped[0] in "#!":
            continue
        # Java skips whitespace before a key but must not eat whitespace inside the value: so lstrip only here.
        raw_key, raw_value = _split_key_value(logical.lstrip(BLANKS), line_no)
        key = _unescape(raw_key, line_no, "key", None)
        value = _unescape(raw_value, line_no, "value", key)
        if key in doc.items:
            doc.duplicates.append((key, doc.lines.get(key), line_no))
        doc.items[key] = value
        doc.lines.setdefault(key, line_no)
    return doc


def unescape_value(raw: str, line_no: int = 1) -> str:
    """Unescape a single-line value/key (reused by callers that only split one token)."""
    return _unescape(raw, line_no, "value", None)


def loads_file(path, source: str | None = None) -> IocDoc:
    with open(str(path), "rb") as handle:
        return loads(handle.read(), source=source or str(path))


def _escape_value(raw: str) -> str:
    out = []
    for index, ch in enumerate(raw):
        code = ord(ch)
        if ch == " " and index == 0:
            # leading spaces of a value must be escaped, otherwise reading it back eats them as separator whitespace.
            out.append(BS + " ")
            continue
        if ch == BS:
            out.append(BS + BS)
        elif ch == chr(9):
            out.append(BS + "t")
        elif ch == chr(10):
            out.append(BS + "n")
        elif ch == chr(13):
            out.append(BS + "r")
        elif ch == chr(12):
            out.append(BS + "f")
        elif code < 0x20 or code > 0x7E:
            out.append(_unicode_escape(code))
        else:
            out.append(ch)
    return "".join(out)


def _unicode_escape(code: int) -> str:
    if code > 0xFFFF:
        code -= 0x10000
        high = 0xD800 + (code >> 10)
        low = 0xDC00 + (code & 0x3FF)
        return BS + "u%04X" % high + BS + "u%04X" % low
    return BS + "u%04X" % code


def _escape_key(raw: str) -> str:
    out = []
    for index, ch in enumerate(raw):
        # every space inside a key must be escaped: an unescaped one becomes the key/value separator (JDK saveConvert passes escapeSpace=true for keys).
        if ch in "=:#!" or ch == " ":
            out.append(BS + ch)
        else:
            out.append(_escape_value(ch))
    return "".join(out)


def dumps(mapping, comments=()) -> str:
    """Output text. Keys in byte order (same as CubeMX saves), newline fixed to LF."""
    lines = []
    for comment in comments:
        lines.append("#" + comment)
    if comments:
        lines.append("#")
    for key in sorted(mapping.keys(), key=lambda item: str(item).encode("utf-8")):
        lines.append("%s=%s" % (_escape_key(str(key)), _escape_value(str(mapping[key]))))
    return chr(10).join(lines) + chr(10)


def dumps_bytes(mapping, comments=()) -> bytes:
    return dumps(mapping, comments=comments).encode("iso-8859-1", "strict")


def format_error_note(exc: IocFormatError, doc: IocDoc | None) -> str:
    """Human readable error description: line number + a short excerpt of that line."""
    line = ""
    if doc is not None and doc.raw:
        physical = doc.raw.splitlines()
        if 0 <= exc.line_no - 1 < len(physical):
            line = physical[exc.line_no - 1].strip()
    if line:
        return "%s (line: %s)" % (exc, line[:120])
    return str(exc)