"""Small XML helpers.

Every function looks at the local name only (the namespace is dropped).
ET ignores comments by default.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(path: Path):
    return ET.parse(str(path)).getroot()


def children(el, name: str | None = None):
    if el is None:
        return []
    if name is None:
        return list(el)
    return [c for c in list(el) if local(c.tag) == name]


def first(el, name: str):
    for c in children(el, name):
        return c
    return None


def attr(el, name: str, default=None):
    if el is None:
        return default
    return el.get(name, default)


def text(el) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def bool_attr(el, name: str):
    v = attr(el, name)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes")


def int_attr(el, name: str):
    v = attr(el, name)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None
