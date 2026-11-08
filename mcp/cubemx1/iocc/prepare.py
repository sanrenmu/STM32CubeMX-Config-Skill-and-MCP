"""Preparation before compilation: board inheritance, host derivation, project option resolution.

Order:
1. validate the target shape first;
2. if there is a `board` section, apply the seed by initialize / or keep materialized as is;
3. derive the unique host from the selected pins (when a middleware needs a controller);
4. compute the final project options by 'explicit > imported value > board inheritance > default'.

Build and query (probe) share this result so there is only one set of rules.
"""

from __future__ import annotations

from ..probe.catalog import load_model
from . import board_apply, hosts, options
from .allocate import allocate as run_allocation
from .errors import IocUnknown
from .target import parse as parse_target


def prepare(machine, data, model=None) -> tuple:
    """Return (normalized target, notes). data may be raw JSON or an already normalized target."""
    target = parse_target(data)
    notes = []

    target, board_notes = board_apply.apply(machine, target)
    notes.extend(board_notes)
    if target.get("board"):
        target = parse_target(target)
    if not (target.get("device") or {}).get("refname"):
        raise IocUnknown("device not set: a materialized board target must specify device.refname",
                         "device.refname")

    if model is None:
        model = load_model(machine, target["device"]["refname"])
    target, alloc_notes = run_allocation(model, target)
    if alloc_notes:
        target = parse_target(target)
        notes.extend(alloc_notes)
    added, host_notes = hosts.derive(model, target)
    if added:
        target = dict(target)
        target["ips"] = list(target["ips"]) + added
        target = parse_target(target)
    notes.extend(host_notes)

    explicit = options.validate(target["project"].get("options") or {})
    inherited = board_apply.project_options(target) if target.get("board") else {}
    resolved = options.resolve(explicit=explicit, inherited=inherited)
    target = dict(target)
    target["project"] = dict(target["project"], options=resolved)
    return target, notes