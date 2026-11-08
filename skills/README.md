# skills/ —— 给 AI 助手读的说明书

**中文** ｜ [English](README.en.md)

这个目录里只有文字，没有程序。程序在 [`../mcp/`](../mcp/)。

## 这是什么

`stm32cubemx1-config`：**写一份描述目标配置的 JSON，离线编译成 STM32CubeMX 1.x 能直接加载的
`.ioc`；需要时再让它生成工程。** CubeMX 只做两件事：加载编出来的 `.ioc`、生成代码。

## 渐进式路由（文件怎么被加载）

`SKILL.md` 是**唯一入口**。YAML frontmatter 的 `name`/`description` 供客户端发现；
正文只放默认路径、路由表和硬边界。**默认读完 `SKILL.md` 就能完成 build → generate，不需要打开 references。**

只有出现对应处境时才加载其中一份：

| 处境 | 加载 |
|---|---|
| 第一次建工程、要照抄完整案例、按板卡/BSP 建、配 DMA 或 RTOS | [references/workflows.md](stm32cubemx1-config/references/workflows.md) 的 A / E / F / G 段 |
| 写 JSON 前要查外设清单、参数名/类型/取值、引脚候选，或名字不完全匹配 | workflows.md 的 B 段 |
| 要配时钟目标（多目标、无 PLL、小数 PLL） | workflows.md 的 C 段 |
| 要先查时钟拓扑、看某个频率的上游 | workflows.md 的 D 段 |
| 要查命令、参数、退出码、`probe` 用法 | [references/commands.md](stm32cubemx1-config/references/commands.md) |
| 有报错、生成结果不对、加载异常、时钟解不出来 | [references/handbook.md](stm32cubemx1-config/references/handbook.md) |
| 要写/改目标 JSON 的字段 | [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md)（随包单独发） |

**不是从上到下全读**。每份 reference 顶部都写了加载时机，解决当前问题就停。

```text
skills/stm32cubemx1-config/
├── SKILL.md                    入口路由器：默认路径 + 路由表 + 边界（先读这一份，通常只读这一份）
└── references/
    ├── workflows.md            案例库：新建 / 外设清单与探测 / 时钟 / 时钟拓扑 / 板卡 / DMA / RTOS / 改现有工程 / 对拍生成
    ├── commands.md             命令参考：build / check / generate / import / edit / probe
    └── handbook.md             现象 → 解法（编译报错 / 对拍 / 生成 / RTOS / 时钟 / 家族差异）
```

`references/handbook.md` 只有中文：它是给操作者查现象用的手册，不再随包提供英文快照。
英文读者用 `SKILL.md` 与 `workflows.md`。

`build` 的目标 JSON 协议在包的 `docs/PROTOCOL.md`。它单独随包发，因为调用方必须照着它写。

## 装到 AI 助手那边

1. 推荐让助手直接指向本包的 `skills/` 目录。这样 Skill 里的相对链接都还通。
2. 如果助手只认固定的 skills 目录，就把 `stm32cubemx1-config/` 整个目录复制过去。
   这时 `../../docs/PROTOCOL.md` 会失效：把协议文件一起放进去，并改链接。
3. MCP 是另一条独立入口。注册方式在 [`../mcp/mcp.json`](../mcp/mcp.json) 的 `mcpServers` 块。
