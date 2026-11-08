"""Read the IP Modes XML: mode tree, RefMode, parameter variants, possible values,
signals, GPIO and DMA.

Read only. Every variant is kept; possible values are never merged.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import (
    Condition, IpModel, Mode, Parameter, ParamVariant, RefMode, Signal, Value,
)
from .xmlio import attr, bool_attr, children, first, local, parse, text

TOKEN_SPLIT = re.compile(r"[^\w:$#]+", re.UNICODE)
# The right-hand side of a comparison is a value, not a configuration item.
# Strip it before splitting tokens, so values are not mistaken for dependencies.
COMPARISON = re.compile(r"(?:>=|<=|!=|=|>|<)\s*[^&|)\s]+")
MATH_WORDS = {
    "AND", "OR", "NOT", "XOR", "MIN", "MAX", "ABS", "TRUE", "FALSE",
    "IF", "THEN", "ELSE",
}


def tokens_of(raw: str | None) -> list[str]:
    if not raw:
        return []
    out = []
    for tok in TOKEN_SPLIT.split(COMPARISON.sub(" ", raw)):
        if not tok or not re.search(r"[A-Za-z]", tok):
            continue
        if tok.upper() in MATH_WORDS:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _cond(el) -> Condition | None:
    if el is None:
        return None
    raw = attr(el, "Expression")
    if raw is None:
        raw = text(el) or None
    if raw is None and attr(el, "Diagnostic") is None:
        return None
    return Condition(raw=raw, diagnostic=attr(el, "Diagnostic"),
                     action=attr(el, "Action"), tokens=tokens_of(raw))


def _cond_list(el) -> list[Condition]:
    out = []
    for c in children(el, "Condition"):
        got = _cond(c)
        if got:
            out.append(got)
    for c in children(el, "ContextCondition"):
        got = _cond(c)
        if got:
            out.append(got)
    return out


def _value(el) -> Value | None:
    v = attr(el, "Value")
    if v is None:
        v = attr(el, "Value2")
    if v is None:
        v = text(el) or None
    if v is None:
        return None
    c = _cond(first(el, "Condition"))
    sem = attr(el, "Semaphore")
    sems = [x for x in (sem or "").split(",") if x.strip()]
    return Value(
        value=v,
        comment=attr(el, "Comment"),
        condition=c,
        action=attr(el, "Action"),
        semaphores=[x.strip() for x in sems],
    )


def _expand_signal(name: str, instance: str) -> str:
    if "$SingleInstance" in name:
        name = name.replace("$SingleInstance", "")
        name = name.replace("$IpInstance", instance)
        return name
    if "$IpInstance" in name:
        return name.replace("$IpInstance", instance)
    return instance + "_" + name


def _read_parameters(root, model: IpModel):
    by_name: dict[str, Parameter] = {}
    for el in children(root, "RefParameter"):
        name = attr(el, "Name")
        if not name:
            continue
        param_desc = text(first(el, "Description")) or None
        variant = ParamVariant(
            refmode=None,
            type=attr(el, "Type"),
            default=attr(el, "DefaultValue"),
            min=attr(el, "Min"),
            max=attr(el, "Max"),
            unit=attr(el, "Unit"),
            display=attr(el, "Display"),
            visible=bool_attr(el, "Visible"),
            conditions=_cond_list(el),
            values=[],
            group=attr(el, "Group"),
            tab=attr(el, "TabName"),
            source=model.source,
        )
        for pv in children(el, "PossibleValue"):
            got = _value(pv)
            if got:
                variant.values.append(got)
        param = by_name.get(name)
        if param is None:
            param = Parameter(name=name, description=param_desc)
            by_name[name] = param
        param.variants.append(variant)
    model.parameters = list(by_name.values())


def _read_refmodes(root, model: IpModel):
    out = []
    for el in children(root, "RefMode"):
        name = attr(el, "Name")
        if not name:
            continue
        ref = RefMode(
            name=name,
            base=attr(el, "BaseMode"),
            abstract=bool(bool_attr(el, "Abstract")),
            conditions=_cond_list(el),
            group=attr(el, "Group"),
            tab=attr(el, "TabName"),
            description=text(first(el, "Description")) or None,
            source=model.source,
        )
        for p in children(el, "Parameter"):
            pname = attr(p, "Name")
            if not pname:
                continue
            vals = []
            for pv in children(p, "PossibleValue"):
                got = _value(pv)
                if got:
                    vals.append(got)
            ref.parameters.append({
                "name": pname,
                "ref_parameter": attr(p, "RefParameter"),
                "condition": _cond(first(p, "Condition")),
                "values": vals,
                "group": attr(p, "Group"),
                "tab": attr(p, "TabName"),
            })
        for bsp in children(el, "BspDependency"):
            ref.bsp_dependencies.append({
                "name": attr(bsp, "Name"),
                "comment": attr(bsp, "Comment"),
                "bsp_ip_name": attr(bsp, "BspIpName"),
                "bsp_mode_name": attr(bsp, "BspModeName"),
                "api": attr(bsp, "Api"),
                "api_version": attr(bsp, "ApiVersion"),
                "component": attr(bsp, "Component"),
                "group_name": attr(bsp, "GroupName"),
                "semaphore": attr(bsp, "Semaphore"),
                "dma_request": attr(bsp, "DmaRequest"),
                "dma_mode": attr(bsp, "DmaMode"),
                "condition": _cond(first(bsp, "Condition")),
            })
        for cfg in children(el, "ConfigForMode"):
            ref.configs.append(text(cfg))
        out.append(ref)
    model.refmodes = out


def _read_signals(root, model: IpModel):
    out: dict[str, Signal] = {}
    for el in children(root, "RefSignal"):
        raw_name = attr(el, "Name")
        if not raw_name:
            continue
        name = _expand_signal(raw_name, model.instance)
        sig = out.get(name)
        if sig is None:
            sig = Signal(name=name, source=model.source)
            out[name] = sig
        sig.io_modes = [x.strip() for x in (attr(el, "IOMode") or "").split(",")
                        if x.strip()]
        sig.direction = attr(el, "Direction")
        sig.virtual = bool(bool_attr(el, "Virtual"))
        sig.shareable_group = attr(el, "ShareableGroupName")
        sig.exclusive_group = attr(el, "ExclusiveGroupName")
    model.signals = list(out.values())


def _read_modes(root, model: IpModel):
    """Read the ModeLogicOperator/Mode tree recursively."""
    out: list[Mode] = []
    seen: dict[str, int] = {}

    def add(mode: Mode):
        base = mode.id
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            mode.id = base + "#%d" % (n + 1)
            mode.path = mode.path[:-1] + [mode.path[-1] + "#%d" % (n + 1)]
        out.append(mode)

    def walk_op(op_el, parent: Mode | None, path: list[str]):
        op_name = attr(op_el, "Name")
        for m_el in children(op_el, "Mode"):
            name = attr(m_el, "Name") or ""
            internal = name
            mpath = path + [name]
            mid = model.instance + "/" + "/".join(mpath)
            signals = []
            for slo in children(m_el, "SignalLogicalOp"):
                for s_el in children(slo, "Signal"):
                    raw = attr(s_el, "Name")
                    if raw:
                        signals.append(_expand_signal(raw, model.instance))
            conds = _cond_list(m_el)
            remove = first(m_el, "RemoveCondition")
            mode = Mode(
                id=mid,
                name=name,
                internal_name=internal,
                parent=parent.id if parent else None,
                operator=op_name if parent is not None else op_name,
                is_leaf=False,
                signals=signals,
                conditions=conds,
                remove_condition=text(remove) if remove is not None else None,
                remove_disable=bool(bool_attr(m_el, "RemoveDisable")),
                semaphores=[text(s) for s in children(m_el, "Semaphore")
                            if text(s)],
                refmode=None,
                user_name=attr(m_el, "UserName"),
                description=text(first(m_el, "Description")) or None,
                path=mpath,
                source=model.source,
            )
            child_ops = children(m_el, "ModeLogicOperator")
            mode.is_leaf = not child_ops
            add(mode)
            for cop in child_ops:
                walk_op(cop, mode, mpath)

    for op_el in children(root, "ModeLogicOperator"):
        walk_op(op_el, None, [])

    refmode_names = {r.name for r in model.refmodes}
    for m in out:
        if m.internal_name in refmode_names:
            m.refmode = m.internal_name
        parent = next((x for x in out if x.id == m.parent), None)
        if parent is not None and m.id not in parent.children:
            parent.children.append(m.id)
    model.modes = out


def _read_gpio(root, model: IpModel):
    for el in children(root, "GPIO_Port"):
        model.gpio_ports.append({
            "name": attr(el, "Name"),
            "clock_enable_mode": attr(el, "ClockEnableMode"),
            "specific": [dict(x.attrib) for x in children(el, "SpecificParameter")],
        })
    for el in children(root, "GPIO_Pin"):
        name = attr(el, "Name")
        if not name:
            continue
        item = {
            "name": name,
            "port": attr(el, "PortName"),
            "signals": [],
            "specific": [dict(x.attrib) for x in children(el, "SpecificParameter")],
        }
        for ps in children(el, "PinSignal"):
            sig = {
                "name": attr(ps, "Name"),
                "specific": [dict(x.attrib) for x in children(ps, "SpecificParameter")],
                "remap_blocks": [],
            }
            for rb in children(ps, "RemapBlock"):
                sig["remap_blocks"].append({
                    "name": attr(rb, "Name"),
                    "default": bool_attr(rb, "DefaultRemap"),
                    "specific": [dict(x.attrib) for x in children(rb, "SpecificParameter")],
                })
            item["signals"].append(sig)
        model.gpio_pins.append(item)


def _read_dma(root, model: IpModel):
    flows = []
    requests = []
    for flow in children(root, "DMA_Flow"):
        fname = attr(flow, "Name")
        entry = {"name": fname, "requests": []}
        for req in children(flow, "DMA_Request"):
            r = {
                "name": attr(req, "Name"),
                "flow": fname,
                "master_ip_name": attr(req, "MasterIpName"),
                "section": attr(req, "Section"),
                "insertion": attr(req, "Insertion"),
                "scope": attr(req, "Scope"),
            }
            entry["requests"].append(r)
            requests.append(r)
        flows.append(entry)
    for req in children(root, "DMA_Request"):
        r = {
            "name": attr(req, "Name"),
            "flow": None,
            "master_ip_name": attr(req, "MasterIpName"),
            "section": attr(req, "Section"),
            "insertion": attr(req, "Insertion"),
            "scope": attr(req, "Scope"),
        }
        requests.append(r)
    model.dma_flows = flows
    model.dma_requests = requests


def _read_root_extras(root, model: IpModel):
    """Read the semaphores, conditions, dependencies and description under the root.

    A <Semaphore> on the root is an IP-level semaphore, for example
    $IpInstanceUsed_ForRCC. The mode-level readers never see it.
    """
    for el in children(root, "Semaphore"):
        name = text(el)
        if not name:
            continue
        model.semaphores.append({
            "name": name,
            "comment": attr(el, "Comment"),
            "condition": _cond(first(el, "Condition")),
        })
    for el in children(root, "Condition"):
        got = _cond(el)
        if got:
            model.root_conditions.append(got)
    for el in children(root, "IPDependencies"):
        raw = text(el) or ""
        for name in raw.replace(";", ",").split(","):
            name = name.strip()
            if name and name not in model.ip_dependencies:
                model.ip_dependencies.append(name)
    model.description = text(first(root, "Description")) or None


KNOWN_IP_ROOT = {
    "About", "RefParameter", "RefMode", "ModeLogicOperator", "RefSignal",
    "GPIO_Pin", "GPIO_Port", "DMA_Flow", "DMA_Request", "SpecificParameter",
    "Condition", "Semaphore", "IPDependencies", "Dependency", "Description",
}


def read_modes(path: Path, instance: str, name: str, version: str) -> IpModel:
    root = parse(path)
    ip_type = (attr(root, "IPType") or "").lower() or None
    model = IpModel(
        instance=instance,
        name=name,
        version=version,
        ip_type=ip_type,
        group=attr(root, "IpGroup"),
        db_version=attr(root, "DBVersion"),
        source=str(path),
    )
    _read_parameters(root, model)
    _read_refmodes(root, model)
    _read_signals(root, model)
    _read_modes(root, model)
    _read_gpio(root, model)
    _read_dma(root, model)
    _read_root_extras(root, model)
    model.specific_parameters = [dict(x.attrib) for x in
                                 children(root, "SpecificParameter")]
    for el in children(root):
        tag = local(el.tag)
        if tag not in KNOWN_IP_ROOT:
            model.unhandled.append({"tag": tag, "source": model.source})
    return model