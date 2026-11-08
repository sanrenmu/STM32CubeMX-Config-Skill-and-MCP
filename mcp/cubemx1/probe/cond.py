"""Substitution layer for condition evaluation: names are replaced by values, and
whatever cannot be computed is handed to the three-valued evaluator.

## Where the rules come from

Reproduced from measured behaviour, not from the tool's internals. Rule sources:

| Line | Rule |
|---|---|
| 320 | $context becomes the context suffix |
| 330 | a known semaphore becomes 1 or 0 |
| 343-351 | $modeexist_X becomes 1 when that mode exists |
| 353-368 | $pinlocked_X becomes 1 when that pin is locked |
| 335-339 | IP:parameter is split into the IP and the parameter name |
| 370-382 | a parameter value becomes a number; #parameter too |
| 389 | **an unresolved name becomes 0** <- we do not do that; it stays three-valued |
| 391 | : $ # become underscores before the maths library sees them |
| 56-63 | $RamSize / $FlashSize (only used for RefParameter defaults) |
| variable table | $IpInstance / $IpNumber / $Index / $IpInstanceForLPBAM |

## Difference from the tool's default behaviour

When the tool parses a condition it treats an unknown name as 0, so "do not know"
turns into "false". We **keep that name** and let it become unknown: a deliberate
choice here, because reporting unknown at compile time is safer than quietly
assuming false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .expr import UNKNOWN, ExprError, evaluate

# Rule for splitting names (measured):
SPLIT_RE = re.compile(r"[^\w:$#]+", re.UNICODE)
# Symbol substitution before the maths library sees it (measured):
SANITIZE_RE = re.compile(r"[:$#]")
MODE_EXIST = "$modeexist_"
PIN_LOCKED = "$pinlocked_"


@dataclass
class CondState:
    """External information that evaluation may ask for. Returns None when the answer
    is not known.

    The default implementation knows nothing; subclasses override what they can.
    """

    instance: str | None = None

    def semaphore(self, name: str):
        return None

    def parameter(self, instance: str, name: str):
        return None

    def mode_exists(self, instance: str, mode: str):
        return None

    def pin_locked(self, pin: str):
        return None

    def context_suffix(self, instance: str) -> str:
        return ""

    def device_fact(self, name: str):
        """A static device fact, for example DIE450 or STM32H7. Returns
        true / false / unknown."""
        return None

    def semaphore_or_usage(self, name: str):
        """A semaphore, or "is this instance used".

        The default falls back to the plain semaphore lookup; only subclasses that
        know the project state add the "enabled flag" layer.
        """
        return self.semaphore(name)

    def pin_usage(self, token: str):
        """Pin usage. Shaped like PA15 or PA15_S_TIM2_ETR. Returns 1 / 0 / None."""
        return None


@dataclass
class Substitution:
    """Result of a substitution."""

    text: str = ""
    unknown: list = field(default_factory=list)


def instance_number(instance: str) -> str:
    """Instance number: the trailing digits of the name; 1 when there are none (measured)."""
    m = re.search(r"([0-9]+)$", instance or "")
    return m.group(1) if m else "1"


def expand_instance_vars(expr: str, instance: str | None) -> str:
    """Replace $IpInstance and friends with the concrete names (measured rules)."""
    if not instance:
        return expr
    number = instance_number(instance)
    out = expr.replace("$IpInstanceForLPBAM", instance.replace("LPBAM", ""))
    out = out.replace("$IpInstance", instance)
    out = out.replace("$IpNumber", number)
    out = out.replace("$Index", number)
    return out


def names_of(expr: str) -> list:
    """Split out the names. Same regular expression as CubeMX."""
    out = []
    for tok in SPLIT_RE.split(expr or ""):
        if not tok or not re.search(r"[a-zA-Z]", tok):
            continue
        if tok not in out:
            out.append(tok)
    return out


def _number_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def substitute(raw: str, state: CondState, instance: str | None = None) -> Substitution:
    """Replace the names that can be decided by numbers. The undecidable ones stay
    in place and are recorded."""
    inst = instance or state.instance
    text = expand_instance_vars(raw or "", inst)
    notes = []
    if "$context" in text:
        suffix = state.context_suffix(inst or "") or ""
        if suffix and not suffix.startswith("_"):
            suffix = "_" + suffix
        text = text.replace("$context", suffix)
    for name in names_of(text):
        value = None
        reason = ""
        if name.startswith(MODE_EXIST):
            got = state.mode_exists(inst or "", name[len(MODE_EXIST):])
            if got is None:
                reason = "mode existence is not known"
            else:
                value = 1.0 if got else 0.0
        elif name.startswith(PIN_LOCKED):
            got = state.pin_locked(name[len(PIN_LOCKED):])
            if got is None:
                reason = "pin lock state is not known"
            else:
                value = 1.0 if got else 0.0
        elif ":" in name:
            left, right = name.split(":", 1)
            got = state.parameter(left, right)
            if got is None:
                reason = "parameter value is not known"
            else:
                value = float(got)
        else:
            got = state.semaphore_or_usage(name)
            if got is not None:
                value = 1.0 if float(got) != 0 else 0.0
            else:
                # A name starting with # drops the # for the parameter lookup
                # (measured).
                plain = name[1:] if name.startswith("#") else name
                got = state.parameter(inst or "", plain)
                if got is not None:
                    value = float(got)
                else:
                    fact = state.device_fact(name)
                    if fact is not None:
                        value = 1.0 if fact else 0.0
                    else:
                        used = state.pin_usage(name)
                        if used is not None:
                            value = 1.0 if float(used) != 0 else 0.0
        if value is None:
            notes.append({"name": name, "reason": reason or "no source for this name"})
            continue
        pattern = re.compile(r"(?<![\w:$#])" + re.escape(name) + r"(?![\w:$#])")
        text = pattern.sub(_number_text(value), text)
    return Substitution(text=SANITIZE_RE.sub("_", text), unknown=notes)


def evaluate_condition(raw: str, state: CondState | None = None,
                       instance: str | None = None) -> dict:
    """Evaluate one condition. Returns the three-valued result.

    truth is 1 / 0 / None; None means it cannot be computed.
    """
    state = state or CondState()
    sub = substitute(raw, state, instance)
    if not raw or not raw.strip():
        return {"truth": None, "text": sub.text, "unknown": sub.unknown,
                "error": "empty condition"}
    try:
        # The remaining names go to the state's pseudo-number table (measured
        # rule).
        value = evaluate(sub.text, getattr(state, "resolve_name", None))
    except ExprError as exc:
        return {"truth": None, "text": sub.text, "unknown": sub.unknown,
                "error": str(exc)}
    if value is UNKNOWN:
        return {"truth": None, "text": sub.text, "unknown": sub.unknown,
                "error": ""}
    return {"truth": 0 if float(value) == 0 else 1, "text": sub.text,
            "unknown": sub.unknown, "error": ""}
