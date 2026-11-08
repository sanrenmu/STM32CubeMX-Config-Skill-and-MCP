"""Render key/value pairs as .ioc text.

Format facts (measured + CubeMX's own saved output):

- first line is a fixed comment;
- keys are sorted alphabetically (it uses sorted Properties);
- values follow Java properties syntax: backslash, colon, equals and newline are escaped, non-ASCII becomes an escape sequence.
"""

from __future__ import annotations

HEADER = "#MicroXplorer Configuration settings - do not modify"


def escape_key(key: str) -> str:
    out = []
    for index, ch in enumerate(str(key)):
        if ch in "\\:=#!":
            out.append("\\" + ch)
        elif ch == " ":
            out.append("\\ ")
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def escape_value(value: str) -> str:
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch in ":=":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


class Ioc:
    """Key/value pairs -> text. Setting the same key twice overwrites it."""

    def __init__(self) -> None:
        self._kv: dict = {}

    def set(self, key: str, value) -> None:
        key = str(key)
        if "=" in key:
            raise ValueError("key must not contain '=': %r" % key)
        self._kv[key] = "" if value is None else str(value)

    def update(self, mapping: dict) -> None:
        for key, value in mapping.items():
            self.set(key, value)

    def get(self, key: str, default=None):
        return self._kv.get(key, default)

    @property
    def keys(self) -> dict:
        return dict(self._kv)

    def __len__(self) -> int:
        return len(self._kv)

    def render(self) -> str:
        lines = [HEADER]
        for key in sorted(self._kv):
            lines.append("%s=%s" % (escape_key(key), escape_value(self._kv[key])))
        return "\n".join(lines) + "\n"