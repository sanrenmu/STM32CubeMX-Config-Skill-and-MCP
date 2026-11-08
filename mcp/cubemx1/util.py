"""Small helpers shared across modules: only judgements that must exist exactly once.

Two implementations of the same rule disagree between entry points (for example
"does an enable parameter count as true"), so such semantics are defined here once.
"""

from __future__ import annotations


def truthy(value) -> bool:
    """The db / .ioc spellings of "true": true / 1 (case, whitespace and quotes are irrelevant)."""
    if value is None:
        return False
    text = str(value).strip().strip('"').lower()
    return text in ("true", "1")

def suggest_clause(domain: str, matches: list) -> str:
    """The ". Did you mean (...)" clause appended to a name error.

    The searched domain is always named (`parameters of USART1`,
    `instances of STM32F103C(8-B)Tx`, `clock readouts of ...`), so a reader of
    the error knows what set was searched and a suggestion is never mistaken for
    a value from another domain. A suggestion is only ever a hint.
    """
    if not matches:
        return ""
    return ". Did you mean (%s): %s" % (domain, ", ".join(matches))


def suggest(name, candidates, limit: int = 5) -> list:
    """Close candidates for a mistyped name: a suggestion only, never auto-applied.

    Matching is case-insensitive. A candidate that starts with the typed text, or
    contains it, comes first (that is how "UART" finds "UART1"); otherwise
    close matches come from `difflib`. An empty list means nothing is close
    enough: a wrong guess is worse than no guess.
    """
    import difflib

    want = str(name or "").strip().lower()
    if not want:
        return []
    texts = [str(candidate) for candidate in candidates if candidate]
    looked = set()
    out: list = []
    for text in texts:
        low = text.lower()
        if (low == want and text == str(name)) or low in looked:
            continue
        if low.startswith(want) or want in low:
            out.append(text)
            looked.add(low)
    if not out:
        for text in difflib.get_close_matches(str(name), texts, n=limit, cutoff=0.6):
            low = text.lower()
            if (low != want or text == str(name)) and low not in looked:
                out.append(text)
                looked.add(low)
    out.sort(key=lambda text: (not text.lower().startswith(want), len(text), text.lower()))
    return out[:limit]
