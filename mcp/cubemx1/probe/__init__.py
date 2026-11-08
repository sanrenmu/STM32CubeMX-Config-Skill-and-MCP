"""Static configuration probing.

Reads XML only: no .ioc file, no CubeMX process, no evaluation of current state.

Public entry points:

- load_model(machine, refname) -> ProbeModel
- device_summary(model) / item_list(model) / item_detail(model, id) / graph(model)
"""

from .model import (
    Condition, Device, Edge, IpEntry, IpModel, Item, Parameter, ParamVariant,
    Pin, ProbeModel, RefMode, Signal, Value,
)
from .catalog import load_model, build_catalog, summary
from .device import read_device
from .modes import read_modes
from .clock import read_clock

__all__ = [
    "Condition", "Device", "Edge", "IpEntry", "IpModel", "Item", "Parameter",
    "ParamVariant", "Pin", "ProbeModel", "RefMode", "Signal", "Value",
    "load_model", "build_catalog", "summary",
    "read_device", "read_modes", "read_clock",
]
