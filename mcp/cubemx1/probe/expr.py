"""Condition expression evaluation.

Arithmetic and booleans only. How a name turns into a value is supplied by the
caller (see the resolve argument of evaluate).

## Why this is written here instead of calling the original library

The library CubeMX uses is commercial (com.bestcode.mathparser:jbcparser:5.4).
It may be called, but its code cannot go into the release package.

## Where the syntax comes from

The syntax is **entirely measured**:

- 27 functions, with the argument counts measured;
- operators: & | ! = > < >= <= + - * / % ^ << >> work;
  !=, **, the ?: ternary and words such as AND/NOT do not;
- % is an integer division, not a remainder; ^ is a power and left-associative;
- an unresolved variable counts as 0 in the original library. **We do not count
  it as 0**, see below.

## Three-valued results

A name the caller cannot resolve makes the result unknown.
Measured: an unresolved name counts as 0, and the result keeps a "source unknown"
marker so the caller can tell the difference.
So 0 and unknown must stay apart: 0 is "false", unknown is "do not know".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ExprError(Exception):
    """The expression cannot be used. Syntax errors, unsupported operators and wrong
    argument counts all come through here."""


class Unknown:
    """A value that cannot be computed. It is not the same thing as 0."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "unknown"

    def __bool__(self) -> bool:
        raise TypeError("unknown cannot be used as a boolean; test it first")


UNKNOWN = Unknown()

# Measured function table: name -> allowed argument counts.
FUNCTIONS = {
    "ABS": (1,), "ATAN": (1,), "CEIL": (1,), "COS": (1,), "COSH": (1,),
    "COTAN": (1,), "EXP": (1,), "FLOOR": (1,), "LN": (1,), "LOG": (1,),
    "RANDOM": (1,), "RND": (1,), "SIGN": (1,), "SIN": (1,), "SINH": (1,),
    "SQR": (1,), "SQRT": (1,), "TAN": (1,), "TRUNC": (1,),
    "INTPOW": (2,), "LOGN": (2,), "MAX": (2,), "MIN": (2,), "MOD": (2,),
    "POW": (2,),
    "IF": (3,),
    "SUM": (0, 1, 2, 3),
}

# Binary operators: symbol -> (precedence, associativity). Higher binds tighter.
# Note: this table is **subject to review**, associativity is still being measured
# pair by pair.
# Measured: 9 levels, loosest to tightest:
#   | < & < unary ! < = < comparison and shift < binary +- < unary - < * / % < ^
# Apart from & and |, every binary operator associates to the left (the
# associativity of & and | cannot be observed, bitwise operations are associative).
BINARY = {
    "|": (1, "left"),
    "&": (2, "left"),
    "=": (3, "left"),
    ">": (4, "left"), "<": (4, "left"),
    ">=": (4, "left"), "<=": (4, "left"), "<>": (4, "left"),
    "<<": (4, "left"), ">>": (4, "left"),
    "+": (5, "left"), "-": (5, "left"),
    "*": (6, "left"), "/": (6, "left"), "%": (6, "left"),
    "^": (7, "left"),
}
UNARY = {"!": 8, "-": 8, "+": 8}

NUMBER_RE = re.compile(r"0[xX][0-9a-fA-F]+|(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class Token:
    kind: str          # num / name / op / lparen / rparen / comma / end
    text: str
    value: float = 0.0


def tokenize(text: str) -> list:
    """Split into tokens. A character that cannot be tokenised raises; it is never
    skipped."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        m = NUMBER_RE.match(text, i)
        if m:
            raw = m.group(0)
            if raw[:2].lower() == "0x":
                out.append(Token("num", raw, float(int(raw, 16))))
            else:
                out.append(Token("num", raw, float(raw)))
            i = m.end()
            continue
        m = NAME_RE.match(text, i)
        if m:
            out.append(Token("name", m.group(0)))
            i = m.end()
            continue
        two = text[i:i + 2]
        if two in ("<=", ">=", "<<", ">>", "!=", "<>"):
            out.append(Token("op", two))
            i += 2
            continue
        if ch in "&|=><!+-*/%^":
            out.append(Token("op", ch))
            i += 1
            continue
        if ch == "(":
            out.append(Token("lparen", ch))
            i += 1
            continue
        if ch == ")":
            out.append(Token("rparen", ch))
            i += 1
            continue
        if ch == ",":
            out.append(Token("comma", ch))
            i += 1
            continue
        raise ExprError("unrecognised character: %r (position %d)" % (ch, i))
    out.append(Token("end", ""))
    return out


def _truth(value):
    """Truth rule: non-zero is true (measured)."""
    if value is UNKNOWN:
        return UNKNOWN
    return 0 if value == 0 else 1


def _need_number(value):
    if value is UNKNOWN:
        return UNKNOWN
    if not isinstance(value, (int, float)):
        raise ExprError("a number is required here, got %r" % (value,))
    return float(value)


class Parser:
    """Precedence climbing. Returns UNKNOWN where a value cannot be computed instead
    of raising."""

    def __init__(self, tokens: list, resolve):
        self.tokens = tokens
        self.pos = 0
        self.resolve = resolve

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self):
        value = self.expression(0)
        if self.peek().kind != "end":
            raise ExprError("expression was not fully parsed: %r" % self.peek().text)
        return value

    def expression(self, min_prec: int):
        left = self.unary()
        while True:
            tok = self.peek()
            if tok.kind != "op" or tok.text == "!":
                break
            info = BINARY.get(tok.text)
            if info is None:
                if tok.text == "!=":
                    raise ExprError("the original library does not support !=; use <> for not equal")
                raise ExprError("unsupported operator: %s" % tok.text)
            prec, _assoc = info
            if prec < min_prec:
                break
            self.next()
            right = self.expression(prec + 1)
            left = self.apply(tok.text, left, right)
        return left

    # Unary operators are consumed differently; this follows the measurements:
    #   -2^2  = -4   -> minus binds looser than ^, so 2^2 is computed first
    #   -2*3  = -6   -> minus binds tighter than *
    #   !0=3  = 1    -> not binds looser than =, so 0=3 is computed first, then negated
    #   !0&0  = 0    -> not binds tighter than &
    UNARY_OPERAND = {"-": 6, "+": 6, "!": 3}

    def unary(self):
        tok = self.peek()
        if tok.kind == "op" and tok.text in UNARY:
            self.next()
            value = self.expression(self.UNARY_OPERAND[tok.text])
            if tok.text == "-":
                return UNKNOWN if value is UNKNOWN else -float(value)
            if tok.text == "+":
                return value
            flag = _truth(value)
            if flag is UNKNOWN:
                return UNKNOWN
            return 1.0 if flag == 0 else 0.0
        return self.primary()

    def primary(self):
        tok = self.next()
        if tok.kind == "num":
            return tok.value
        if tok.kind == "lparen":
            value = self.expression(0)
            if self.next().kind != "rparen":
                raise ExprError("unbalanced parentheses")
            return value
        if tok.kind == "name":
            if self.peek().kind == "lparen":
                return self.call(tok.text)
            return self.resolve(tok.text)
        raise ExprError("expected a value here, got %r" % tok.text)

    def call(self, name: str):
        allowed = FUNCTIONS.get(name)
        if allowed is None:
            raise ExprError("the original library has no such function: %s" % name)
        self.next()                     # consume the (
        args = []
        if self.peek().kind != "rparen":
            args.append(self.expression(0))
            while self.peek().kind == "comma":
                self.next()
                args.append(self.expression(0))
        if self.next().kind != "rparen":
            raise ExprError("unbalanced parentheses in the call to %s" % name)
        if len(args) not in allowed:
            raise ExprError("%s takes %s argument(s), got %d"
                            % (name, "/".join(str(x) for x in allowed),
                               len(args)))
        return self.call_function(name, args)

    def call_function(self, name: str, args: list):
        ctx = {"name": name, "args": args}
        return _FUNCS[name](ctx)

    def apply(self, op: str, left, right):
        return _apply(op, left, right)


def _div(a: float, b: float) -> float:
    """Double-precision division as in Java: never raises. 1/0 = Infinity,
    0/0 = NaN."""
    if b != 0:
        return a / b
    if a == 0 or a != a:
        return float("nan")
    return float("inf") if a > 0 else float("-inf")


def _floor(value: float) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        return value
    return float(_math.floor(value))


def _binary_numeric(op, left, right):
    """Arithmetic and comparison. If either side is unknown the result is unknown."""
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    a, b = float(left), float(right)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return _div(a, b)
    if op == "%":
        # Measured: % is an integer division **rounding towards -infinity**, not a
        # remainder.
        #   7%2 = 3   7.5%2 = 3   (-7)%2 = -4
        # Note: -7%2 looks like -3 because it parses as -(7%2), not because of the
        # rounding rule.
        if b == 0:
            return _div(a, b)
        return _floor(_div(a, b))
    if op == "^":
        # math.pow instead of **: a negative base with a fractional exponent gives
        # NaN in Java but a complex number in Python.
        try:
            return _math.pow(a, b)
        except (OverflowError, ValueError):
            return float("nan")
    if op == "<<":
        return float(int(a) << int(b))
    if op == ">>":
        return float(int(a) >> int(b))
    if op == "=":
        return 1.0 if a == b else 0.0
    if op == "<>":
        return 1.0 if a != b else 0.0
    if op == ">":
        return 1.0 if a > b else 0.0
    if op == "<":
        return 1.0 if a < b else 0.0
    if op == ">=":
        return 1.0 if a >= b else 0.0
    if op == "<=":
        return 1.0 if a <= b else 0.0
    raise ExprError("unsupported operator: %s" % op)


def _apply(op: str, left, right):
    """Note: & and | are **bitwise** operations, not logical ones.

    Measured: 2 & 1 = 0, 2 | 0 | 2 = 2, 1 | 2 = 3.
    A condition is true when the result is != 0, so bitwise results take part
    directly.
    """
    if op in ("&", "|"):
        if left is UNKNOWN or right is UNKNOWN:
            # Only one case can be decided early: 0 AND anything is 0.
            if op == "&":
                known = right if left is UNKNOWN else left
                if known is not UNKNOWN and float(known) == 0:
                    return 0.0
            return UNKNOWN
        a, b = int(float(left)), int(float(right))
        return float(a & b) if op == "&" else float(a | b)
    return _binary_numeric(op, left, right)


def _math1(fn):
    def run(ctx):
        v = ctx["args"][0]
        if v is UNKNOWN:
            return UNKNOWN
        return fn(float(v))
    return run


def _math2(fn):
    def run(ctx):
        a, b = ctx["args"]
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        return fn(float(a), float(b))
    return run


def _if(ctx):
    cond, yes, no = ctx["args"]
    c = _truth(cond)
    if c is UNKNOWN:
        # Decidable only when both branches are equal; otherwise unknown.
        if yes is not UNKNOWN and no is not UNKNOWN and yes == no:
            return yes
        return UNKNOWN
    return yes if c == 1 else no


import math as _math

_FUNCS = {
    "ABS": _math1(abs), "CEIL": _math1(_math.ceil), "FLOOR": _math1(_math.floor),
    "SIGN": _math1(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)),
    "SQRT": _math1(_math.sqrt), "SQR": _math1(lambda x: x * x),
    "EXP": _math1(_math.exp), "LN": _math1(_math.log),
    "LOG": _math1(lambda x: _math.log10(x) if x > 0 else 0.0),
    "SIN": _math1(_math.sin), "COS": _math1(_math.cos), "TAN": _math1(_math.tan),
    "ATAN": _math1(_math.atan), "SINH": _math1(_math.sinh),
    "COSH": _math1(_math.cosh),
    "COTAN": _math1(lambda x: 1.0 / _math.tan(x)),
    "TRUNC": _math1(lambda x: float(int(x))),
    "RND": _math1(lambda x: float(round(x))),
    # RANDOM differs on every call. It cannot decide a condition, so it is unknown.
    "RANDOM": lambda ctx: UNKNOWN,
    "INTPOW": _math2(lambda a, b: float(int(a) ** int(b))),
    "POW": _math2(lambda a, b: a ** b),
    "LOGN": _math2(lambda a, b: _math.log(a, b)),
    "MOD": _math2(lambda a, b: _math.fmod(a, b)),
    "MIN": _math2(min), "MAX": _math2(max),
    "IF": _if,
    "SUM": lambda ctx: _sum(ctx["args"]),
}


def _sum(args):
    total = 0.0
    for v in args:
        if v is UNKNOWN:
            return UNKNOWN
        total += float(v)
    return total


def evaluate(text: str, resolve=None):
    """Evaluate one expression.

    resolve(name) returns a number or UNKNOWN. Without resolve every name counts
    as UNKNOWN (not 0).
    """
    if text is None or not text.strip():
        raise ExprError("empty expression")
    if resolve is None:
        resolve = lambda name: UNKNOWN       # noqa: E731
    raw = resolve

    def safe(name):
        """A None from the caller counts as unknown: None is not a number."""
        got = raw(name)
        return UNKNOWN if got is None else got

    return Parser(tokenize(text), safe).parse()


def truth_of(value):
    """For callers: turn the result into the three-valued 1 / 0 / None."""
    if value is UNKNOWN:
        return None
    return 0 if value == 0 else 1
