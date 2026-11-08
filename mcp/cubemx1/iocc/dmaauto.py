"""Candidate channels/streams for DMA requests and automatic assignment (read from the mode tree + conditions; no hardcoded peripheral map).

Data sources (all in the db of the install directory):
- mode tree: the controller root Mode has channel/stream modes below it, and each channel has modes for the requests it can carry;
- request definition: parameters on the RefMode chain (Direction/Mode/...); a few requests also declare Channel;
- conditions: the Condition on the request Mode (references semaphores of other peripherals), evaluated three-valued through DesignState.

Implementation notes: DMA parameters are written the way CubeMX saves them (write visible parameters only, skip empty values); whether a request is usable comes from the mode tree + Mode conditions, not from hardcoded peripheral names.
"""

from __future__ import annotations

from ..probe import cond
from . import derive
from .errors import IocUnknown


def controllers(model) -> list:
    """All IPs in the device that declare DMA request definitions (no guessing DMA/BDMA by name)."""
    return [ip for ip in model.ips if derive.dma_request_refmodes(ip)]


def _mode_truth(mode, state, instance) -> int | None:
    """Condition tristate of a mode: all true -> 1; any false -> 0; otherwise None (unknown)."""
    conditions = list(getattr(mode, "conditions", None) or [])
    if not conditions:
        return 1
    unknown = False
    for condition in conditions:
        raw = getattr(condition, "raw", None)
        if not raw or not str(raw).strip():
            continue
        result = cond.evaluate_condition(raw, state, instance)
        truth = result.get("truth")
        if truth == 0:
            return 0
        if truth is None:
            unknown = True
    return None if unknown else 1


def request_candidates(model, request: str, state) -> list:
    """Which channels/streams this request can use: with tristate conditions, in db declaration order."""
    out = []
    for ip in controllers(model):
        try:
            flows = derive.dma_flows(ip, request)
        except IocUnknown:
            continue
        for flow in flows:
            truth = _mode_truth(flow["request_mode"], state, ip.instance)
            channel = (flow.get("channel_hint")
                       or derive.dma_request_channel_default(ip, request))
            # one wildcard Mode may map to several concrete streams (the Instance parameter on H7);
            # each concrete stream is its own candidate; conflicts are judged by concrete stream name.
            for instance in flow.get("instances") or [flow["instance"]]:
                out.append({"ip": ip,
                            "controller": flow["controller"],
                            "instance": instance,
                            "channel": channel,
                            "truth": truth,
                            "conditions": [getattr(item, "raw", "")
                                           for item in (flow["request_mode"].conditions
                                                        or [])]})
    return out


def solve(requests, candidates_by_request) -> tuple:
    """Pick one channel/stream per request: MRV + backtracking, no channel reuse.

    Return (assignment, conflicts). assignment: {request: candidate}; when there is no solution assignment is None and conflicts lists the candidates and current occupants of each request.
    """
    domains = {}
    for request in requests:
        usable = [candidate for candidate in candidates_by_request.get(request) or []
                  if candidate["truth"] == 1]
        domains[request] = usable
    order = list(requests)
    assignment = {}
    used = {}

    def search():
        if len(assignment) == len(order):
            return True
        remaining = [item for item in order if item not in assignment]
        best = None
        best_domain = None
        for request in remaining:
            free = [candidate for candidate in domains[request]
                    if candidate["instance"] not in used]
            if best is None or len(free) < len(best_domain):
                best, best_domain = request, free
                if not free:
                    break
        if not best_domain:
            return False
        for candidate in best_domain:
            assignment[best] = candidate
            used[candidate["instance"]] = best
            if search():
                return True
            used.pop(candidate["instance"], None)
            assignment.pop(best, None)
        return False

    if search():
        return assignment, []
    conflicts = []
    for request in requests:
        candidates = candidates_by_request.get(request) or []
        conflicts.append({
            "request": request,
            "candidates": [{"instance": item["instance"],
                            "controller": item["controller"],
                            "truth": item["truth"]} for item in candidates],
            "blocked_by": sorted(used),
        })
    return None, conflicts
