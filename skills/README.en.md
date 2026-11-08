# skills/ — instructions for AI assistants

[中文](README.md) ｜ **English**

This directory is text only — no program. The program lives in [`../mcp/`](../mcp/).

> This English text is a translation snapshot, synced at release time. The Chinese version
> ([README.md](README.md)) is authoritative.

## What this is

`stm32cubemx1-config`: **write one JSON describing the target configuration, compile it offline
into a `.ioc` that STM32CubeMX 1.x loads directly, then let CubeMX generate the project.** CubeMX only
loads the `.ioc` and generates code.

## Progressive routing (how files get loaded)

`SKILL.md` is the **single entry point**. The YAML frontmatter `name`/`description` lets clients
discover it; the body only holds the default path, the routing table and hard boundaries. **Reading
`SKILL.md` alone is enough for build → generate; references stay closed until a matching situation
appears.**

Load exactly one of these when the situation matches:

| Situation | Load |
|---|---|
| First project, copying a complete case, board/BSP project, DMA or RTOS | [references/workflows.md](stm32cubemx1-config/references/workflows.md) sections A / E / F / G |
| Looking up the device peripheral list, a parameter name/type/value, or a pin choice; a name that does not match exactly | workflows.md, section B |
| Clock targets (multiple targets, no-PLL C0, fractional PLL) | workflows.md, section C |
| Inspect clock topology / upstream of one frequency | workflows.md, section D |
| Looking up a command, argument, exit code, `probe` usage | [references/commands.md](stm32cubemx1-config/references/commands.md) |
| Compile error, wrong generation, load problem, clock not solving | [references/handbook.md](stm32cubemx1-config/references/handbook.md) |
| Writing/editing target JSON fields | [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md) (shipped separately) |

**Do not read top-to-bottom.** Each reference states its loading condition at the top; stop once the
current problem is solved.

```text
skills/stm32cubemx1-config/
├── SKILL.md                    entry router: default path + routing table + boundaries (usually the only file read)
└── references/
    ├── workflows.md            case library: new project / peripheral inventory and probing / clocks / clock topology / boards / DMA / RTOS / editing
    ├── commands.md             command reference: build / check / generate / import / edit / probe
    └── handbook.md             symptom → fix (compile / comparison / generation / RTOS / clocks / families)
```

`references/handbook.md` is Chinese only: it is an operator's symptom-to-fix handbook, and the English
snapshot is no longer shipped. English readers use `SKILL.md` and `workflows.md`.

The target JSON protocol lives in the package's `docs/PROTOCOL.md`. It ships separately because
callers must write against it.

## Installing into an AI assistant

1. Recommended: point the assistant at this package's `skills/` directory, so relative links keep working.
2. If your assistant insists on a fixed skills directory, copy `stm32cubemx1-config/` there.
   Then `../../docs/PROTOCOL.md` breaks: copy the protocol next to it and fix the links.
3. MCP is a separate entry. Registration is in the `mcpServers` block of [`../mcp/mcp.json`](../mcp/mcp.json).
