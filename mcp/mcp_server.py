"""MCP shell: wraps the offline IOC compiler as MCP tools.

## How thin this layer is

**No business logic at all.** Each tool just takes arguments, hands them to the
kernel, and turns the result into JSON. Compilation rules, validation and error
semantics all live in `cubemx1/iocc/` (that is, the `cubemx1 ioc ...` command
line).

## Tool list (the release surface is three tools + status)

| tool | what it does | needs CubeMX |
|---|---|---|
| `cubemx1_ioc_build` | target JSON -> canonical `.ioc` (offline compile) | **no** |
| `cubemx1_ioc_generate` | native CMake project generation + artifact checks | yes (long-lived session + firmware pack) |
| `cubemx1_status` | background process and session status | no (query only) |

**The release path does no round-trip**: load / save as / compare keys runs on
the development side only and does not ship in this package.
The only write entry point is `cubemx1_ioc_build`; the "target state" of a
configuration lives in that JSON, not in a command sequence.

## Also a minimal hand-written implementation

No MCP SDK, just to save one dependency -- the standard library only.
Transport is stdio, one JSON-RPC message per line; stdout carries protocol
messages only, logs go to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# make the script runnable directly (works without installing the package)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cubemx1 import __version__  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "cubemx1"

#: Documents the model can pull into its context through MCP resources.
#: Tool descriptions are the primary surface; these resources carry what does not
#: fit there (the full target JSON contract and the tool/transport contract). A
#: file under docs/ is not read by anyone automatically, so it has to be exposed
#: here (or routed by the Skill) to be reachable at all.
RESOURCES = [
    {
        "uri": "cubemx1://protocol/target",
        "name": "Target JSON protocol",
        "description": "Full contract of the target JSON that cubemx1_ioc_build "
                       "compiles: every section, required/optional fields, the "
                       "cubemx1.ioc/1 vs cubemx1.ioc/2 difference, error kinds.",
        "mimeType": "text/markdown",
        "path": "docs/PROTOCOL.md",
    },
    {
        "uri": "cubemx1://protocol/mcp",
        "name": "MCP transport and tool contract",
        "description": "How the host talks to this server (stdio JSON-RPC, "
                       "methods, failure semantics) and what each tool promises.",
        "mimeType": "text/markdown",
        "path": "mcp/PROTOCOL.md",
    },
]

#: Sent in the initialize result: the smallest useful map of the server. A client
#: may show it to the model, so it says what to do, not what the code looks like.
INSTRUCTIONS = (
    "cubemx1 compiles a target JSON into a canonical STM32CubeMX 1.x .ioc offline, "
    "then CubeMX does the native generation.\n"
    "Flow: read resource cubemx1://protocol/target for the JSON contract, call "
    "cubemx1_ioc_build with the target, then cubemx1_ioc_generate on the written .ioc.\n"
    "build / import / probe_describe run offline; generate / check need a local "
    "CubeMX 1.x installation. ioc_check is for development and acceptance, not a "
    "required step for release use.\n"
    "Tool/transport contract: cubemx1://protocol/mcp."
)


def _resource_path(rel: str) -> Path:
    """Where a shipped document lives: next to the package root, never the cwd."""
    return (Path(__file__).resolve().parents[1] / rel).resolve()


def resources_list() -> dict:
    """resources/list payload (uri, name, description, mimeType; no file paths)."""
    return {"resources": [{k: r[k] for k in ("uri", "name", "description", "mimeType")}
                          for r in RESOURCES]}


def resources_read(uri) -> dict:
    """resources/read payload: the document text, or an error when it is absent."""
    item = next((r for r in RESOURCES if r["uri"] == uri), None)
    if item is None:
        raise ValueError("no such resource: %s; available: %s"
                         % (uri, ", ".join(r["uri"] for r in RESOURCES)))
    path = _resource_path(item["path"])
    if not path.is_file():
        raise ValueError("resource %s is missing from this installation: %s"
                         % (uri, path))
    return {"contents": [{"uri": uri, "mimeType": item["mimeType"],
                          "text": path.read_text(encoding="utf-8")}]}


# --------------------------------------------------------------------------
# Tool implementations: each one only "passes through the kernel"
# --------------------------------------------------------------------------

def _client():
    from cubemx1.client import Client
    return Client(ensure_running=True)


def _target_data(raw):
    """Target: either an object, or JSON text, or a file path."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("target must be a JSON object, JSON text or a file path")
    path = Path(raw.strip()).expanduser()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return json.loads(raw)


def tool_ioc_build(args: dict) -> dict:
    """Offline compile: target JSON -> canonical .ioc.

    **Does not start CubeMX**, so it needs no background process and does not
    consume a session.

    :param target: target JSON (an object, or JSON text, or a .json file path)
    :param output: output .ioc path; defaults to project.output_dir/<name>.ioc
    """
    from cubemx1 import iocc
    from cubemx1.iocc.errors import IocError
    from cubemx1.machine import load as load_machine

    args = args or {}
    try:
        data = _target_data(args.get("target"))
    except (ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "stage": "target JSON",
                "error": "cannot parse the target: %s" % exc}
    try:
        machine = load_machine()
    except Exception as exc:  # noqa: BLE001 - a missing machine config is an environment problem
        return {"ok": False, "stage": "machine config",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        result = iocc.build(machine, data)
    except IocError as exc:
        # Report unsupported / unknown / conflict separately: the caller has to
        # know whether the feature is "not supported yet" or the target is
        # "written wrong".
        return {"ok": False, "stage": "compile", "kind": exc.kind,
                "path": exc.path, "error": str(exc)}

    out = args.get("output")
    if not out:
        project = data.get("project") or {}
        if project.get("output_dir") and project.get("name"):
            out = str(Path(project["output_dir"]).expanduser()
                      / (project["name"] + ".ioc"))
    written = str(result.write(out)) if out else ""
    return {"ok": True, "ioc": written, "keys": len(result.keys),
            "notes": result.notes}




def tool_ioc_check(args: dict) -> dict:
    """Development/acceptance round-trip: load .ioc -> save as -> compare keys + scan the log.

    **Not a required step for release use** (normal users only run build +
    generate). It needs a CubeMX session and writes <same name>_roundtrip.ioc
    into save_dir.
    """
    from cubemx1.cmds import ioc as ioc_cmd
    args = args or {}
    ioc = args.get("ioc")
    if not ioc:
        return {"ok": False, "error": "missing 'ioc': path of the .ioc to check"}
    return ioc_cmd.check(_client(), ioc, args.get("save_dir"))

def tool_ioc_generate(args: dict) -> dict:
    """Generate the native CMake project and check the artifacts (startup file,
    project references, key values).

    Needs a CubeMX session and the firmware pack of the matching family. The
    output directory comes from the project settings inside the .ioc.

    :param ioc: path of a compiled .ioc
    """
    from cubemx1.cmds import ioc as ioc_cmd
    args = args or {}
    ioc = args.get("ioc")
    if not ioc:
        return {"ok": False,
                "error": "missing 'ioc': path of the .ioc to generate"}
    return ioc_cmd.generate(_client(), ioc)


def tool_status(args: dict) -> dict:
    """Show background process and session status.

    **This is a query, and "the query ran" counts as success** -- hence the
    fixed ok: True. "The background process is not running" is a query
    **result** (in the running field), not a tool failure.
    """
    from cubemx1.client import daemon_status
    st = daemon_status()
    st["cubemx1_version"] = __version__
    return {"ok": True, **st}


def tool_probe_describe(args: dict) -> dict:
    """Offline queries: device/IP definitions, applicable-definition selection,
    three-valued conditions; or ST board lookups.

    **Does not start CubeMX**. Queries and compilation share the same
    "applicable definition selection" (see cubemx1/probe/resolve.py).

    :param kind: describe / boards / board / clock, defaults to describe
    :param device: device RefName (for describe/clock; can also come from target)
    :param target: target JSON (optional; conditions use its choices)
    :param ip: instance name, for example USART1; a board-level instance is the
        board name (give board as well)
    :param keyword: keyword for boards
    :param board: board id (name@revision@vendor) or board name; for
        kind=describe it loads that board's BSP definitions
    :param output: clock readout name or element id (kind=clock, view=upstream)
    :param view: clock view: graph / upstream / raw (kind=clock)
    :param size: page size of a clock view; it never drops the underlying model
    :param seq: raw record id of one clock element (kind=clock, view=raw)
    """
    from cubemx1 import boardservice
    from cubemx1.machine import load as load_machine
    from cubemx1.probe import query

    args = args or {}
    kind = str(args.get("kind") or "describe").lower()
    try:
        machine = load_machine()
        if kind == "boards":
            return query.boards(machine, args.get("keyword"),
                                int(args.get("limit") or 50))
        if kind == "board":
            wanted = args.get("board") or args.get("board_id")
            if not wanted:
                return {"ok": False, "error": "board requires a board id"}
            return boardservice.board(machine, wanted)
        if kind == "peripherals":
            return query.peripherals(machine, device=args.get("device"),
                                     target=args.get("target"),
                                     board=args.get("board"))
        if kind == "pins":
            return query.pins(machine, device=args.get("device"),
                              target=args.get("target"),
                              ip=args.get("ip") or args.get("instance"),
                              mode=args.get("mode"),
                              board=args.get("board"),
                              limit=int(args.get("limit") or 50))
        if kind == "clock":
            size = args.get("size")
            seq = args.get("seq")
            return query.clock(machine, device=args.get("device"),
                               target=args.get("target"),
                               output=args.get("output"),
                               view=str(args.get("view") or "graph"),
                               size=int(size) if size is not None else None,
                               seq=int(seq) if seq is not None else None,
                               tree=args.get("tree"))
        return query.describe(machine, device=args.get("device"),
                              target=args.get("target"),
                              ip=args.get("ip") or args.get("instance"),
                              board=args.get("board"))
    except Exception as exc:  # noqa: BLE001 - query failures must be reported as they are
        return {"ok": False, "stage": "query",
                "error": "%s: %s" % (type(exc).__name__, exc)}


def tool_ioc_import(args: dict) -> dict:
    """Import a .ioc into an advanced target JSON (cubemx1.ioc/2).

    **Does not start CubeMX**. Objects it understands go into advanced fields;
    keys it does not understand go into extensions.ioc and are reported one by
    one.

    :param ioc: path of the .ioc to import
    :param output: which JSON to write; defaults to the .json with the same name
    """
    from cubemx1.cmds import ioc as ioc_cmd
    from cubemx1.machine import load as load_machine

    args = args or {}
    ioc = args.get("ioc")
    if not ioc:
        return {"ok": False, "error": "missing 'ioc': path of the .ioc to import"}
    try:
        machine = load_machine()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "machine config",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        result = ioc_cmd.import_target(machine, ioc, args.get("output"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "import",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    return result


TOOLS = [
    {
        "name": "cubemx1_ioc_build",
        "description": "Compile a **target JSON** offline into a canonical .ioc "
                       "that CubeMX loads directly. Does not start CubeMX and "
                       "runs no command sequence. The target describes: device, "
                       "pins and signals, peripheral modes and parameters, clock "
                       "target frequencies or explicit RCC parameters, "
                       "interrupts, DMA, middleware, RTOS scalar parameters, "
                       "project name / output directory / toolchain. "
                       "Unsupported or malformed fields are reported as "
                       "unsupported/unknown/conflict and are never silently "
                       "ignored.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": ["object", "string"],
                           "description": "Target: a JSON object, or JSON text, "
                                          "or a .json file path. The top level "
                                          "must carry contract: cubemx1.ioc/1 or "
                                          "cubemx1.ioc/2"},
                "output": {"type": "string",
                           "description": "Output .ioc path; defaults to "
                                          "project.output_dir/<project.name>.ioc "
                                          "from the target"},
            },
            "required": ["target"],
        },
        "fn": tool_ioc_build,
    },
    {
        "name": "cubemx1_ioc_check",
        "description": "[development/acceptance only, not a required step for "
                       "release use] Hand the .ioc to CubeMX: load -> save as -> "
                       "verify that every key the target requires is present "
                       "with the same value, and scan the load log. Needs a "
                       "CubeMX session; writes <same name>_roundtrip.ioc into "
                       "save_dir. Normal project generation does not call it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ioc": {"type": "string", "description": "path of the .ioc to check"},
                "save_dir": {"type": "string", "description": "where to save as; defaults to the .ioc directory"},
            },
            "required": ["ioc"],
        },
        "fn": tool_ioc_check,
    },
    {
        "name": "cubemx1_ioc_generate",
        "description": "Hand the compiled .ioc to native CubeMX generation of a "
                       "CMake project and check the artifacts: the startup file "
                       "exists and is referenced by the project, and the target's "
                       "numeric parameters appear in the generated code. "
                       "Successful generation does not mean the project compiles, "
                       "let alone runs on the board. Needs a CubeMX session and "
                       "the firmware pack of the matching family.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ioc": {"type": "string", "description": "path of a compiled .ioc"},
            },
            "required": ["ioc"],
        },
        "fn": tool_ioc_generate,
    },
    {
        "name": "cubemx1_ioc_import",
        "description": "Import a .ioc into an advanced target JSON "
                       "(cubemx1.ioc/2): understood objects go into advanced "
                       "fields, unknown keys go into extensions.ioc and are "
                       "reported one by one. Does not start CubeMX.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ioc": {"type": "string", "description": "path of the .ioc to import"},
                "output": {"type": "string", "description": "which JSON to write; defaults to the .json with the same name"},
            },
            "required": ["ioc"],
        },
        "fn": tool_ioc_import,
    },
    {
        "name": "cubemx1_probe_describe",
        "description": "Offline queries. kind=describe: device/IP definitions (modes, parameters, types and ranges, candidate values, owning RefMode and conditions, true/false/unknown under the current target), pin candidates. kind=boards/board: official ST boards (details include the seed base configuration and native BSP definitions); with board, queries and compilation share the same board context and board-level instances (instance name = board name) are queryable. kind=peripherals: every peripheral/middleware instance the device declares (with the IP type from its modes file). kind=pins: one IP's signals and their candidate pins in both directions, and with mode the leaf mode's required signals, candidate pins, must-be-explicit signals and conflicts; without mode it gives a per-mode summary. kind=clock: the device's clock topology as a graph, the upstream of one readout (view=upstream, all MUX branches and the fractional input kept), or the original clock-definition XML / one element fragment (view=raw). The clock views keep RAW facts separate from interpretation: every attribute, every condition, document order, all candidates of an ambiguous reference. Does not start CubeMX and does not solve any frequency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["describe", "peripherals", "pins", "boards", "board", "clock"], "description": "query kind, defaults to describe"},
                "device": {"type": "string", "description": "device RefName, for example STM32F103C(8-B)Tx"},
                "target": {"type": ["object", "string"], "description": "target JSON (optional)"},
                "ip": {"type": "string", "description": "instance name, for example USART1; a board-level instance is the board name (with board)"},
                "keyword": {"type": "string", "description": "keyword for boards, for example NUCLEO"},
                "board": {"type": "string", "description": "board id (name@revision@vendor) or board name; for describe it is the board context / BSP definition source"},
                "limit": {"type": "integer", "description": "maximum number of entries for boards, defaults to 50"},
                "output": {"type": "string", "description": "clock readout parameter name (for example HCLKFreq_Value) or element id; required for kind=clock, view=upstream"},
                "view": {"type": "string", "enum": ["graph", "upstream", "raw"], "description": "clock view, defaults to graph"},
                "size": {"type": "integer", "description": "page size of a clock view; the payload reports what was left out"},
                "seq": {"type": "integer", "description": "raw record id (kind=clock, view=raw of one element)"},
                "tree": {"type": "string", "description": "clock tree name override (kind=clock)"},
                "mode": {"type": "string", "description": "leaf mode name (kind=pins) or a group name; leave out for a per-mode summary"},
            },
        },
        "fn": tool_probe_describe,
    },
    {
        "name": "cubemx1_status",
        "description": "Show the cubemx1 background process and session status. "
                       "ioc_build does not need it; take a look before "
                       "ioc_generate.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_status,
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

def handle(req: dict):
    """Handle one request. Returning None means no reply is needed."""
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }}

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                      for t in TOOLS],
        }}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        tool = BY_NAME.get(name)
        if tool is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602,
                              "message": "no such tool: %s; available: %s"
                                         % (name, ", ".join(BY_NAME))}}
        try:
            result = tool["fn"](params.get("arguments") or {})
        except SystemExit as e:
            # **One bad argument must not pull the plug on the control plane**
            # (measured: a read-only command with an illegal argument made the
            # whole service exit with code 2, and later `ping`s went
            # unanswered).
            # `SystemExit` is not an `Exception`, so it must be caught
            # **separately**; catching it turns it into a normal tool error and
            # the service keeps running.
            result = {"ok": False,
                      "error": "invalid arguments for this tool call; the "
                               "process exited during parsing (exit code %s). "
                               "Check the arguments and retry." % (e.code,)}
        except Exception as e:
            result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False,
                                            indent=2)}],
            # **Only an explicit `ok: false` counts as tool failure.**
            # A missing `ok` field is not a failure -- otherwise tools whose
            # response shape carries no ok would be reported as failing on every
            # call (`cubemx1_status` hit this once).
            "isError": result.get("ok") is False,
        }}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": resources_list()}

    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": []}}

    if method == "resources/read":
        params = req.get("params") or {}
        try:
            result = resources_read(params.get("uri"))
        except (ValueError, OSError) as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method in ("ping", "server/discover"):
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "unsupported method: %s" % method}}
    return None


def _force_utf8_stdio() -> None:
    """Pin all three stdio streams to UTF-8 **explicitly**.

    ## Why it must be explicit (measured, and I missed it myself for a while)

    `stdout` is the **protocol channel**, and protocol frames **do carry
    non-ASCII** (tool descriptions, error messages). But on Windows Python
    encodes with the **local code page** by default -- GBK on this machine.
    So when an MCP client starts this process without
    `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` in the environment, the
    JSON-RPC frames go out as **GBK bytes**, and a spec-compliant client
    (reading UTF-8) fails to parse them or gets mojibake.

    This was **masked by environment variables**: the development shell
    happened to inject `PYTHONUTF8=1`, so the same service looked perfectly
    normal locally; **stripping those variables reproduced the GBK output**.
    So nothing here relies on the environment; it is pinned here.

    `stdin` is the same: requests may carry non-ASCII too (paths, labels).
    `stderr` is the log, pinned as well so log text is not mojibake on the
    client side.
    """
    for stream, kw in ((sys.stdin, {"errors": "replace"}),
                       (sys.stdout, {"newline": "\n"}),
                       (sys.stderr, {"errors": "replace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kw)   # 3.7+
        except (AttributeError, ValueError, OSError):
            pass          # redirected to a non-text stream (or old Python) -> keep as is


def main() -> int:
    _force_utf8_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print("skipping a line that is not JSON: %s" % e, file=sys.stderr,
                  flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            # stdout carries protocol messages only; logs go to stderr
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
