"""Three-valued evaluation state for the whole design: merges the choices of all IPs for conditional evaluation.

Why it is needed: whether a DMA request is usable depends on semaphores of other peripherals (for example, whether USART1_TX can go through DMA depends on S_USART1_TX_RX|S_USART1_TX). A single-IP ModelState only sees its own modes, so this merges the states of all IPs: any IP saying true means true; if none says true but someone is undecided, it is unknown; otherwise false.
"""

from __future__ import annotations

from ..probe import cond
from ..probe.resolve import ModelState


class DesignState(cond.CondState):
    """State of the whole target project (modes/params/pins of all IPs merged)."""

    def __init__(self, model, target):
        target = target or {}
        signals = [str(item.get("signal")) for item in target.get("pins") or []
                   if item.get("signal")]
        pins = {str(item.get("pin")): bool(item.get("locked"))
                for item in target.get("pins") or []}
        params_by_instance = {}
        for item in target.get("ips") or []:
            instance = str(item.get("instance") or "")
            params_by_instance.setdefault(instance, {}).update(
                item.get("params") or {})
        self.ip_instances = {str(item.get("instance") or "").upper()
                             for item in target.get("ips") or []}
        self.states = []
        for ip in model.ips:
            params = {(ip.instance, name): value
                      for name, value in (params_by_instance.get(ip.instance)
                                          or {}).items()}
            self.states.append(ModelState(
                ip=ip, device=model.device, instance=ip.instance,
                selection={"signals": signals, "pins": pins,
                           "params": params}))
        self._providers = self._build_providers(model, params_by_instance)

    def _expand(self, name: str, instance: str) -> str:
        text = str(name or "")
        text = text.replace("$IpInstanceForLPBAM", instance.replace("LPBAM", ""))
        text = text.replace("$IpInstance", instance)
        text = text.replace("$IpNumber", cond.instance_number(instance))
        text = text.replace("$Index", cond.instance_number(instance))
        return text

    def _build_providers(self, model, params_by_instance):
        """Register the semaphores provided by parameter values.

        Basis: the Semaphore attribute of PossibleValue in the db: 'this parameter provides that semaphore when it equals this value' (true only when the value matches). The current value is the one given by the target, else the default of the applicable definition.
        """
        from ..probe.resolve import ModelState, select_variant

        provided = set()
        for ip in model.ips:
            params = params_by_instance.get(ip.instance) or {}
            local = ModelState(ip=ip, device=model.device, instance=ip.instance)
            for param in ip.parameters:
                param_name = param.name
                explicit = params.get(param_name)
                picked = select_variant(ip, param_name, local)
                variant = picked.get("variant")
                if variant is None:
                    continue
                value = explicit if explicit is not None else variant.default
                if value is None or str(value) == "":
                    continue
                for item in (variant.values or []):
                    if item.value is None or str(item.value) != str(value):
                        continue
                    for semaphore in (item.semaphores or []):
                        provided.add(self._expand(semaphore, ip.instance))
        return provided
    def semaphore(self, name: str):
        if name in self._providers:
            return 1.0
        unknown = False
        for state in self.states:
            value = state.semaphore(name)
            if value == 1.0:
                return 1.0
            if value is None:
                unknown = True
        return None if unknown else 0.0

    def parameter(self, instance: str, name: str):
        for state in self.states:
            value = state.parameter(instance, name)
            if value is not None:
                return value
        return None

    def mode_exists(self, instance: str, mode: str):
        for state in self.states:
            value = state.mode_exists(instance, mode)
            if value is not None:
                return value
        return None

    def pin_locked(self, pin: str):
        for state in self.states:
            value = state.pin_locked(pin)
            if value is not None:
                return value
        return None

    def pin_usage(self, token: str):
        for state in self.states:
            value = state.pin_usage(token)
            if value is not None:
                return value
        return None

    def device_fact(self, name: str):
        for state in self.states:
            value = state.device_fact(name)
            if value is not None:
                return value
        return None

    def usage_flag(self, name: str):
        token = str(name or "")
        upper = token.upper()
        if upper.startswith("USE_"):
            token = token[4:]
        elif upper.endswith("_USED"):
            token = token[:-5]
        else:
            return None
        return 1.0 if token.upper() in self.ip_instances else 0.0

    def resolve_name(self, name: str):
        for getter in (self.semaphore, self.usage_flag, self.device_fact):
            value = getter(name)
            if value is not None:
                return float(value)
        return None
