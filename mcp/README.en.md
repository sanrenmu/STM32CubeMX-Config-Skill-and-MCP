# mcp/ — the program (offline kernel + MCP shell)

[中文](README.md) ｜ **English**

This directory holds the program: an **offline kernel** (target JSON → `.ioc`, queries, import,
comparison) plus a **very thin MCP shell**.

> This English text is a translation snapshot, synced at release time. The Chinese version
> ([README.md](README.md)) is authoritative.

```text
mcp/
├── cubemx1/                 kernel (the only place with business logic)
│   ├── cli/                 CLI entry: argument assembly + dispatch (no rules)
│   ├── cmds/                command implementations (ioc build / check / generate / import / edit)
│   ├── iocc/                target -> .ioc: contract, compile, import, edit, compare, codecs
│   ├── probe/               static model, three-valued conditions, clock topology query (no CubeMX)
│   ├── clocksolve*.py       clock solving (PLL paths, fixed values, independent forward check)
│   ├── clockplain.py        paths without a PLL (pure divider chains), dispatched by shape
│   ├── machine / session / daemon / invoke
│   │                        local config and session runtime (offline operations read config; check / generate start a session)
│   └── ...                  other building blocks (IOC byte format, XML/output parsing)
├── mcp_server.py            MCP shell (stdio; parameter forwarding only, no second logic)
├── mcp.json                 machine-readable manifest: launch, requirements, tools, mcpServers block
├── PROTOCOL.md              MCP tool contract (for humans)
└── README.md                this file
```

## Six tools (by purpose)

| Tool | When to use | Needs CubeMX |
|---|---|---|
| `cubemx1_ioc_build` | The write path: target JSON -> `.ioc` (offline, main path) | no |
| `cubemx1_ioc_generate` | Generate a CMake project from a `.ioc` and check artifacts | yes |
| `cubemx1_ioc_check` | Dev/acceptance comparison: load + save-as + compare keys (**not needed for release use**) | yes |
| `cubemx1_ioc_import` | Reverse a `.ioc` into an editable target JSON | no |
| `cubemx1_probe_describe` | Definitions/parameters/conditions (`kind=describe`), device peripheral inventory (`kind=peripherals`), signals and candidate pins (`kind=pins`), ST boards (`kind=boards/board`), clock topology (`kind=clock`) | no |
| `cubemx1_status` | Background process / session status | no |

## As an MCP server (stdio)

```bash
python mcp/mcp_server.py      # one JSON-RPC message per line; stdout is protocol only, logs on stderr
```

Client registration (replace `<package-root>` with the absolute path of the unpacked package):

```json
{
  "mcpServers": {
    "cubemx1": {
      "command": "python",
      "args": ["<package-root>/mcp/mcp_server.py"],
      "cwd": "<package-root>"
    }
  }
}
```

## Getting the documents into context (resources + instructions)

Tool descriptions are the primary surface. Long documents are handed out as **MCP resources**, not
left lying in the repository hoping someone reads them.

```text
initialize -> capabilities: {tools:{}, resources:{}} + instructions
resources/list  -> cubemx1://protocol/target   (full target JSON contract, docs/PROTOCOL.md)
                   cubemx1://protocol/mcp      (this tool contract, mcp/PROTOCOL.md)
resources/read  -> {uri} returns the text (unknown uri / missing file is an explicit error)
```

`instructions` carries the shortest path: read `cubemx1://protocol/target` →
`cubemx1_ioc_build` → `cubemx1_ioc_generate`. The Skill routes the same two URIs, so either entry
reaches them.

## Before you run it

1. STM32CubeMX 1.x is installed (the build that ships its own JRE). `build` reads the installed DB without starting CubeMX; `check` / `generate` start CubeMX.
2. Local config `~/.cubemx1/machine.json` (outside the package, not versioned), at least
   `cubemx_home`; `CUBEMX1_MACHINE` can point elsewhere. Missing values are reported explicitly.

Generating code also needs the firmware package for that family; otherwise generation is blocked early
with a clear message.

## Three rules

1. **One kernel**: all logic lives in `cubemx1/`; the MCP shell only forwards parameters.
2. **Process handling in one place**: starting CubeMX, sending commands and dialog handling are
   centralized; nothing else spawns it.
3. **CLI and MCP behave the same**: MCP tools are another entry into the same kernel. The release path
   is `ioc build` → `ioc generate`.
