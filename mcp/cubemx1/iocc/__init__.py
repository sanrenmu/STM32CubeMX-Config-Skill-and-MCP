"""Offline IOC compiler (iocc).

Input: target JSON (states what is wanted). Output: canonical .ioc text.
It starts no session and runs no command; CubeMX is only used during development for cross-checks and code generation.

Design and acceptance basis: docs/PROTOCOL.md inside the package.
"""

from .build import BuildResult, build  # noqa: F401
from .target import TargetError, parse as parse_target  # noqa: F401