"""IOC encoding types: one codec per class (decode / encode / owned keys).

This layer only converts keys <-> structured fragments in both directions; it knows nothing about the target contract (v1/v2) and does no merging. Upper layers (iocc/build.py writing, iocc/import_ioc.py reading) all use the same rules here.

Design constraints:
- each codec handles decode / encode / owned keys together, so deleting and adding share one ownership table;
- data is never deleted by broad string prefixes; ownership is decided by the full shape of a key plus an explicit position;
- unrecognized keys go to extensions and are never dropped silently.
"""

from __future__ import annotations

from . import props

# ---------------------------------------------------------------- constants and tables

HEADER_KEYS = ("File.Version", "MxCube.Version", "MxDb.Version", "KeepUserPlacement")

DEVICE_KEYS = {
    "Mcu.Name", "Mcu.UserName", "Mcu.CPN", "Mcu.Family", "Mcu.Package",
    "Mcu.ThirdPartyNb", "Mcu.TrustZone",
}

BOARD_KEYS = {"board", "boardIOC", "BSP_IP_NAME"}

CONSTANTS_KEY = "Mcu.UserConstants"

#: ProjectManager option table: key name suffix -> (type, default). Basis: measured key values and defaults.
PROJECT_OPTIONS = {
    "KeepUserCode": ("bool", True),
    "DeletePrevious": ("bool", True),
    "BackupPrevious": ("bool", False),
    "CoupleFile": ("bool", False),
    "NoMain": ("bool", False),
    "HalAssertFull": ("bool", False),
    "FirmwarePackage": ("str", ""),
}

#: kind names of the RTOS object tables (the hidden parameter rows the plugin creates at runtime).
RTOS_KINDS = ("Tasks", "Queues", "Timers", "Mutexes",
              "RecursiveMutexes", "BinarySemaphores",
              "CountingSemaphores", "Events")

#: keys that are not parameters (pin/IP metadata).
NOT_A_PARAMETER = {
    "IPParameters", "VirtualMode", "Mode", "Signal", "Locked",
    "GPIO_Label", "GPIO_Pin", "GPIOParameters", "PinState",
    "GPIO_ModeDefaultEXTI",
}


class CodecError(ValueError):
    """Codec semantic error (the format is valid but the structure is not what is expected)."""


def _split(key: str):
    inst, sep, rest = key.partition(".")
    return inst, (rest if sep else None)


def _bool(value: str) -> bool:
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise CodecError("not a boolean value: %r" % value)


# ---------------------------------------------------------------- codecs

class HeaderCodec:
    name = "header"

    def decode(self, doc):
        return {key: doc[key] for key in HEADER_KEYS if key in doc}

    def encode(self, payload):
        return {key: str(payload[key]) for key in HEADER_KEYS if key in payload}

    def owned(self, doc):
        return {key: {"kind": "header"} for key in HEADER_KEYS if key in doc}


class DeviceCodec:
    name = "device"

    def decode(self, doc):
        return {key: doc[key] for key in DEVICE_KEYS if key in doc}

    def encode(self, payload):
        return {key: str(payload[key]) for key in DEVICE_KEYS if key in payload}

    def owned(self, doc):
        return {key: {"kind": "device"} for key in DEVICE_KEYS if key in doc}


class BoardCodec:
    name = "board"

    def decode(self, doc):
        payload = {}
        if "board" in doc:
            payload["id"] = doc["board"]
        if "boardIOC" in doc:
            payload["loaded"] = _bool(doc["boardIOC"])
        if "BSP_IP_NAME" in doc:
            payload["bsp_ip"] = doc["BSP_IP_NAME"]
        return payload

    def encode(self, payload):
        out = {}
        if "id" in payload:
            out["board"] = str(payload["id"])
        if "loaded" in payload:
            out["boardIOC"] = "true" if payload["loaded"] else "false"
        if "bsp_ip" in payload:
            out["BSP_IP_NAME"] = str(payload["bsp_ip"])
        return out

    def owned(self, doc):
        return {key: {"kind": "board"} for key in BOARD_KEYS if key in doc}


class ConstantsCodec:
    name = "constants"

    def decode(self, doc):
        if CONSTANTS_KEY not in doc:
            return {"set": False, "values": {}}
        raw = doc[CONSTANTS_KEY]
        out = {}
        for item in raw.split(";"):
            item = item.strip()
            if not item:
                continue
            name, sep, value = item.partition(",")
            if not sep:
                raise CodecError("user constant is missing a comma: %r" % item)
            out[name.strip()] = value.strip()
        return {"set": True, "values": out}

    def encode(self, payload):
        values = (payload or {}).get("values") or {}
        if not values and not (payload or {}).get("set"):
            return {}
        rows = ["%s,%s" % (name, values[name]) for name in values]
        return {CONSTANTS_KEY: ";".join(rows)}

    def owned(self, doc):
        if CONSTANTS_KEY in doc:
            return {CONSTANTS_KEY: {"kind": "constants"}}
        return {}


class ProjectCodec:
    name = "project"

    def decode(self, doc):
        options = {}
        other = {}
        for key, value in doc.pairs():
            inst, rest = _split(key)
            if inst != "ProjectManager" or rest is None:
                continue
            if rest in PROJECT_OPTIONS:
                kind, _default = PROJECT_OPTIONS[rest]
                if kind == "bool":
                    options[rest] = _bool(value)
                else:
                    options[rest] = value
            else:
                other[rest] = value
        return {"options": options, "other": other}

    def encode(self, payload):
        out = {}
        for name, value in (payload.get("options") or {}).items():
            kind, _default = PROJECT_OPTIONS.get(name, ("str", ""))
            if kind == "bool":
                if not isinstance(value, bool):
                    raise CodecError("option %s requires a boolean, got %r" % (name, value))
                out["ProjectManager." + name] = "true" if value else "false"
            else:
                out["ProjectManager." + name] = str(value)
        for name, value in (payload.get("other") or {}).items():
            out["ProjectManager." + name] = str(value)
        return out

    def owned(self, doc):
        out = {}
        for key in doc.keys():
            if key.startswith("ProjectManager."):
                out[key] = {"kind": "project", "name": key[len("ProjectManager."):]}
        return out


class PinsCodec:
    name = "pins"

    def _listed_pins(self, doc):
        pins = []
        count = doc.get("Mcu.PinsNb")
        if count is None:
            return pins
        try:
            total = int(count)
        except ValueError:
            raise CodecError("Mcu.PinsNb is not an integer: %r" % count)
        for index in range(total):
            key = "Mcu.Pin%d" % index
            if key not in doc:
                raise CodecError("missing entries in the list: %s (PinsNb=%d)" % (key, total))
            pins.append(doc[key])
        return pins

    def decode(self, doc):
        pins = []
        for name in self._listed_pins(doc):
            item = {"pin": name}
            if name + ".Signal" in doc:
                item["signal"] = doc[name + ".Signal"]
            if name + ".Mode" in doc:
                item["mode"] = doc[name + ".Mode"]
            if name + ".Locked" in doc:
                item["locked"] = _bool(doc[name + ".Locked"])
            if name + ".GPIOParameters" in doc:
                gpio = {}
                for field in doc[name + ".GPIOParameters"].split(","):
                    field = field.strip()
                    if not field:
                        continue
                    key = name + "." + field
                    if key in doc:
                        gpio[field] = doc[key]
                item["gpio"] = gpio
            if name.startswith("VP_"):
                item["virtual"] = True
            pins.append(item)
        shared = {}
        for key, value in doc.pairs():
            if not key.startswith("SH."):
                continue
            rest = key[3:]
            group, sep, index = rest.rpartition(".")
            if not sep:
                continue
            if index == "ConfNb":
                shared.setdefault(group, {})["count"] = value
            else:
                shared.setdefault(group, {})["slots"] = shared.setdefault(group, {}).get("slots", {})
                shared[group]["slots"][index] = value
        return {"pins": pins, "shared": shared}

    def encode(self, payload):
        pins = payload.get("pins") or []
        out = {}
        out["Mcu.PinsNb"] = str(len(pins))
        for index, item in enumerate(pins):
            out["Mcu.Pin%d" % index] = item["pin"]
        for item in pins:
            name = item["pin"]
            if "signal" in item:
                out[name + ".Signal"] = str(item["signal"])
            if "mode" in item:
                out[name + ".Mode"] = str(item["mode"])
            if item.get("locked"):
                out[name + ".Locked"] = "true"
            gpio = item.get("gpio") or {}
            if gpio:
                out[name + ".GPIOParameters"] = ",".join(sorted(gpio))
                for field in sorted(gpio):
                    out[name + "." + field] = str(gpio[field])
        for group, data in (payload.get("shared") or {}).items():
            if "count" in data:
                out["SH.%s.ConfNb" % group] = str(data["count"])
            for index, value in (data.get("slots") or {}).items():
                out["SH.%s.%s" % (group, index)] = str(value)
        return out

    def owned(self, doc):
        listed = set(self._listed_pins(doc))
        out = {}
        if "Mcu.PinsNb" in doc:
            out["Mcu.PinsNb"] = {"kind": "pins.count"}
        for index in range(len(listed)):
            key = "Mcu.Pin%d" % index
            out[key] = {"kind": "pins.list"}
        for key in doc.keys():
            if key.startswith("SH."):
                out[key] = {"kind": "pins.shared"}
                continue
            inst, rest = _split(key)
            if inst in listed and rest is not None:
                out[key] = {"kind": "pins.attr", "pin": inst, "field": rest}
        return out


#: management instances are not recognized by hardcoded names: which keys belong to another codec is asked from the codec registry.
#: when a name appears in Mcu.IP* but its keys (such as NVIC./RCC./Dma./FREERTOS./SH.) are
#: claimed by another codec, it is a 'management instance' and IpCodec does not claim its parameters again.
def _instances_of(keys):
    """Take 'instance names' from a batch of keys (the part before the dot in `instance.rest`)."""
    out = set()
    for key in keys:
        inst, rest = _split(key)
        if rest is not None and inst:
            out.add(inst)
    return out


def _reserved_from_doc(doc):
    keys = set()
    for codec in CODECS:
        if codec.name == "ips":
            continue
        keys.update(codec.owned(doc).keys())
    return _instances_of(keys)


def _reserved_from_payload(payloads):
    keys = set()
    for codec in CODECS:
        if codec.name == "ips":
            continue
        section = payloads.get(codec.name)
        if section is None:
            continue
        keys.update(codec.encode(section).keys())
    return _instances_of(keys)


class IpCodec:
    name = "ips"

    def _listed_ips(self, doc):
        """Return (instances in the list, missing indices).

        Natively, when a BSP component is expanded, the peripherals used by the BSP are counted into `Mcu.IPNb` first, but their `Mcu.IPn` rows are not written (measured: NUCLEO-H723ZG with VCP on has `IPNb=6` while the file only holds IP0..IP4). The loader tolerates such gaps, so they are skipped and reported here instead of being treated as a format error.
        """
        ips = []
        gaps = []
        count = doc.get("Mcu.IPNb")
        if count is None:
            return ips, gaps
        try:
            total = int(count)
        except ValueError:
            raise CodecError("Mcu.IPNb is not an integer: %r" % count)
        for index in range(total):
            key = "Mcu.IP%d" % index
            if key not in doc:
                gaps.append(index)
                continue
            ips.append(doc[key])
        return ips, gaps

    def decode(self, doc):
        instances, gaps = self._listed_ips(doc)
        special = _reserved_from_doc(doc)
        out = []
        for name in instances:
            if name in special:
                # NVIC/RCC/Dma/FREERTOS/SH also appear in the Mcu.IP* list, but their keys belong to their own
                # codec (clock/nvic/dma/rtos/pins). Here we only record 'it is in the list' and do not claim the
                # parameters again - otherwise both places would write on rebuild and the last writer wins.
                out.append({"instance": name, "params": {},
                            "parameters_listed": None})
                continue
            item = {"instance": name, "params": {}}
            params = doc.get(name + ".IPParameters")
            if params is None:
                item["parameters_listed"] = None
            else:
                names = [part for part in params.split(",") if part]
                item["parameters_listed"] = names
                for param in names:
                    key = name + "." + param
                    if key in doc:
                        item["params"][param] = doc[key]
            out.append(item)
        return {"instances": instances, "ips": out, "gaps": gaps}

    def encode(self, payload):
        ips = payload.get("ips") or []
        listed = payload.get("instances") or [item["instance"] for item in ips]
        out = {"Mcu.IPNb": str(len(listed))}
        for index, name in enumerate(listed):
            out["Mcu.IP%d" % index] = name
        special = _reserved_from_payload(payload)
        for item in ips:
            name = item["instance"]
            if name in special:
                # the parameters of these instances are written by the clock/nvic/dma/rtos codecs, so no key is written twice.
                continue
            names = item.get("parameters_listed")
            if names is None:
                names = list((item.get("params") or {}).keys())
            if names:
                out[name + ".IPParameters"] = ",".join(names)
            for param in names:
                if param in (item.get("params") or {}):
                    out[name + "." + param] = str(item["params"][param])
        return out

    def owned(self, doc):
        listed = set(self._listed_ips(doc)[0])
        special = _reserved_from_doc(doc)
        out = {}
        if "Mcu.IPNb" in doc:
            out["Mcu.IPNb"] = {"kind": "ips.count"}
        for index in range(len(listed)):
            out["Mcu.IP%d" % index] = {"kind": "ips.list"}
        for key in doc.keys():
            inst, rest = _split(key)
            if rest is None or inst in special:
                continue
            if inst in listed:
                out[key] = {"kind": "ips.param", "instance": inst, "name": rest}
        return out


class NvicCodec:
    name = "nvic"

    def decode(self, doc):
        group = doc.get("NVIC.PriorityGroup")
        vectors = {}
        flags = {}
        for key, value in doc.pairs():
            if not key.startswith("NVIC.") or key == "NVIC.PriorityGroup":
                continue
            name = key[len("NVIC."):]
            # a vector value looks like 'true:15:0:false...'; the other values are NVIC scalar switches.
            if key.endswith("_IRQn") or str(value).count(":") >= 3:
                vectors[name] = value
            else:
                flags[name] = value
        return {"priority_group": group, "vectors": vectors, "flags": flags}

    def encode(self, payload):
        out = {}
        if payload.get("priority_group") is not None:
            out["NVIC.PriorityGroup"] = str(payload["priority_group"])
        for name, value in (payload.get("vectors") or {}).items():
            out["NVIC." + name] = str(value)
        for name, value in (payload.get("flags") or {}).items():
            out["NVIC." + name] = str(value)
        return out

    def owned(self, doc):
        out = {}
        for key in doc.keys():
            if key == "NVIC.PriorityGroup":
                out[key] = {"kind": "nvic.group"}
            elif key.startswith("NVIC."):
                name = key[len("NVIC."):]
                kind = "nvic.vector" if (key.endswith("_IRQn")
                                          or str(doc[key]).count(":") >= 3) else "nvic.flag"
                out[key] = {"kind": kind, "name": name}
        return out


class RccCodec:
    name = "clock"

    def decode(self, doc):
        names = doc.get("RCC.IPParameters")
        listed = [part for part in names.split(",") if part] if names else None
        params = {}
        for key, value in doc.pairs():
            if not key.startswith("RCC.") or key == "RCC.IPParameters":
                continue
            params[key[len("RCC."):]] = value
        return {"listed": listed, "params": params}

    def encode(self, payload):
        out = {}
        listed = payload.get("listed")
        params = payload.get("params") or {}
        if listed is None:
            listed = list(params.keys())
        if listed:
            out["RCC.IPParameters"] = ",".join(listed)
        for name in listed:
            if name in params:
                out["RCC." + name] = str(params[name])
        return out

    def owned(self, doc):
        return {key: {"kind": "clock"} for key in doc.keys() if key.startswith("RCC.")}


class DmaCodec:
    name = "dma"

    def decode(self, doc):
        requests = []
        count = doc.get("Dma.RequestsNb")
        if count is not None:
            try:
                total = int(count)
            except ValueError:
                raise CodecError("Dma.RequestsNb is not an integer: %r" % count)
            for index in range(total):
                key = "Dma.Request%d" % index
                if key not in doc:
                    raise CodecError("missing entries in the list: %s (RequestsNb=%d)" % (key, total))
                requests.append(doc[key])
        entries = []
        for key, value in doc.pairs():
            inst, rest = _split(key)
            if inst != "Dma" or rest is None or rest.startswith("Request"):
                continue
            request, sep, tail = rest.partition(".")
            if not sep:
                continue
            index, sep2, param = tail.partition(".")
            if not sep2:
                continue
            entries.append({"request": request, "slot": index, "param": param, "value": value})
        return {"requests": requests, "entries": entries}

    def encode(self, payload):
        requests = payload.get("requests") or []
        out = {}
        if requests:
            out["Dma.RequestsNb"] = str(len(requests))
            for index, name in enumerate(requests):
                out["Dma.Request%d" % index] = str(name)
        for entry in payload.get("entries") or []:
            key = "Dma.%s.%s.%s" % (entry["request"], entry["slot"], entry["param"])
            out[key] = str(entry["value"])
        return out

    def owned(self, doc):
        out = {}
        for key in doc.keys():
            if key.startswith("Dma."):
                out[key] = {"kind": "dma"}
        return out


#: all keys of the FREERTOS instance belong to the RTOS codec: object table rows (Tasks01 etc.), ordinary parameters
#: (configTOTAL_HEAP_SIZE etc.) and the parameter name list IPParameters. The IP parameter codec must not claim them again,
#: otherwise the same key is written from two places on rebuild (the last writer wins).
class RtosCodec:
    name = "rtos"

    @staticmethod
    def _row_name(rest: str):
        for kind in RTOS_KINDS:
            if rest.startswith(kind) and rest[len(kind):].isdigit():
                return rest
        return None

    def decode(self, doc):
        listed = doc.get("FREERTOS.IPParameters")
        listed = [part for part in listed.split(",") if part] if listed else None
        rows = {}
        params = {}
        for key, value in doc.pairs():
            inst, rest = _split(key)
            if inst != "FREERTOS" or rest is None or rest == "IPParameters":
                continue
            if self._row_name(rest) is not None:
                rows[rest] = [part for part in value.split(";")]
            else:
                params[rest] = value
        objects = {}
        for name, parts in rows.items():
            kind = "".join(ch for ch in name if not ch.isdigit())
            objects.setdefault(kind, {})[name] = parts
        return {"listed": listed, "rows": rows, "objects": objects,
                "params": params}

    def encode(self, payload):
        rows = payload.get("rows") or {}
        params = payload.get("params") or {}
        listed = payload.get("listed")
        if listed is None:
            listed = list(rows) + list(params)
        out = {}
        if listed:
            out["FREERTOS.IPParameters"] = ",".join(listed)
        for name, value in rows.items():
            if isinstance(value, list):
                out["FREERTOS." + name] = ";".join(value)
            else:
                out["FREERTOS." + name] = str(value)
        for name, value in params.items():
            out["FREERTOS." + name] = str(value)
        return out

    def owned(self, doc):
        out = {}
        for key in doc.keys():
            inst, rest = _split(key)
            if inst != "FREERTOS" or rest is None:
                continue
            out[key] = {"kind": "rtos.param", "name": rest}
        return out


CODECS = (HeaderCodec(), DeviceCodec(), BoardCodec(), ConstantsCodec(),
          ProjectCodec(), RtosCodec(), DmaCodec(), NvicCodec(), RccCodec(),
          PinsCodec(), IpCodec())


def decode(doc):
    """IOC -> {codec name: payload} + extensions (keys that were not recognized)."""
    owned = {}
    payloads = {}
    for codec in CODECS:
        claimed = codec.owned(doc)
        duplicated = set(claimed) & set(owned)
        if duplicated:
            raise CodecError("the same batch of keys is claimed by two codecs: %s" % sorted(duplicated)[:5])
        owned.update(claimed)
        payloads[codec.name] = codec.decode(doc)
    leftovers = {key: value for key, value in doc.pairs() if key not in owned}
    payloads["extensions"] = {"ioc": leftovers}
    return {"payloads": payloads, "owned": owned, "extensions": leftovers}


def encode(payloads):
    """{codec name: payload} -> {key: value}. extensions are written back as is."""
    out = {}
    for codec in CODECS:
        name = codec.name
        if name in payloads:
            out.update(codec.encode(payloads[name]))
    out.update((payloads.get("extensions") or {}).get("ioc") or {})
    return out


def roundtrip(doc):
    """Difference list of decode->encode (semantic comparison: whether key/value pairs agree)."""
    decoded = decode(doc)
    back = encode(decoded["payloads"])
    problems = []
    for key, want in doc.pairs():
        if key not in back:
            problems.append(("dropped key", key, want, None))
        elif str(back[key]) != str(want):
            problems.append(("changed value", key, want, back[key]))
    for key, got in back.items():
        if key not in doc:
            problems.append(("extra key", key, None, got))
    return problems, decoded


def classify(doc):
    """Key -> owner (used by the import report and the deletion rules)."""
    result = decode(doc)
    return result["owned"]


def load_pair(path):
    """Convenience function: read a file and decode it."""
    return decode(props.loads_file(path))