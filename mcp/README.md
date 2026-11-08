# mcp/ —— 程序本体（离线内核 + MCP 外壳）

**中文** ｜ [English](README.en.md)

这个目录是程序：一个**离线内核**（目标 JSON → `.ioc`、查询、反解、对拍）+ 一个**很薄的 MCP 外壳**。

```text
mcp/
├── cubemx1/                 内核（唯一有业务逻辑的地方）
│   ├── cli/                 命令行入口：参数装配 + 转交（不写规则）
│   ├── cmds/                命令实现（ioc build / check / generate / import / edit）
│   ├── iocc/                目标 -> .ioc：契约、编译、反解、编辑、对拍、codec
│   ├── probe/               静态模型、三值条件求值、时钟拓扑查询（probe；不启动 CubeMX）
│   ├── clocksolve*.py       时钟求解（PLL 路径、固定值、独立正向核算）
│   ├── clockplain.py        无 PLL 的纯分频路径求解（与 PLL 路径按形状分派）
│   ├── machine / session / daemon / invoke
│   │                        本机配置与会话运行时（离线操作读配置；check / generate 启动会话）
│   └── ...                  其余基础件（IOC 字节格式、XML/输出解析等）
├── mcp_server.py            MCP 外壳（stdio；只做参数转发，无第二套逻辑）
├── mcp.json                 机器可读清单：启动方式、依赖、工具面、mcpServers 配置块
├── PROTOCOL.md              MCP 工具契约（给人看）
└── README.md                本文件
```

## 六个工具（按用途）

| 工具 | 什么时候用 | 要 CubeMX 吗 |
|---|---|---|
| `cubemx1_ioc_build` | 写入口：目标 JSON -> `.ioc`（离线，主路径） | 不要 |
| `cubemx1_ioc_generate` | 有了 `.ioc`，要生成 CMake 工程并检查产物 | 要 |
| `cubemx1_ioc_check` | 开发/验收对拍：加载 + 另存 + 比键值（**发布使用不需要**） | 要 |
| `cubemx1_ioc_import` | 把现有 `.ioc` 反解成可改的目标 JSON | 不要 |
| `cubemx1_probe_describe` | 查定义/参数/三值条件（`kind=describe`）、外设清单（`kind=peripherals`）、信号↔候选引脚（`kind=pins`）、ST 板卡（`kind=boards/board`）、时钟拓扑（`kind=clock`） | 不要 |
| `cubemx1_status` | 看后台进程与会话状态 | 不要 |

## 当 MCP 服务（stdio）

```bash
python mcp/mcp_server.py      # 每行一条 JSON-RPC；stdout 只放协议消息，日志走 stderr
```

客户端注册（把 `<包根>` 换成解包后的绝对路径）：

```json
{
  "mcpServers": {
    "cubemx1": {
      "command": "python",
      "args": ["<包根>/mcp/mcp_server.py"],
      "cwd": "<包根>"
    }
  }
}
```

## 让文档进上下文（resources + instructions）

工具描述是主入口。长文档通过 **MCP 资源**给出去，不靠"放在仓库里就会有人读"。

```text
initialize -> capabilities: {tools:{}, resources:{}} + instructions
resources/list  -> cubemx1://protocol/target   （目标 JSON 完整契约，docs/PROTOCOL.md）
                   cubemx1://protocol/mcp      （这份工具契约，mcp/PROTOCOL.md）
resources/read  -> {uri} 取全文（缺文件 / 未知 uri 会明确报错）
```

`instructions` 里写最短路径：先读 `cubemx1://protocol/target` → `cubemx1_ioc_build` →
`cubemx1_ioc_generate`。Skill 那边也把这两个 uri 列进了路由表，两条路都能到。

## 用之前要有两样东西

1. 本机装有 STM32CubeMX 1.x（自带 JRE 的那一版）。`build` 读取安装目录中的 DB 数据，但不启动 CubeMX；`check` / `generate` 需要启动 CubeMX。
2. 本机配置 `~/.cubemx1/machine.json`（在包外，不进版本库），至少含 `cubemx_home`。
   也可用 `CUBEMX1_MACHINE` 指向别处。缺了会直接说该填什么。

生成代码还要求对应家族的固件包已下到本机。没装时生成会被提前拦住并说明。

## 三条纪律

1. **一个内核**：所有逻辑在 `cubemx1/`。MCP 外壳只转发，不许有第二套逻辑。
2. **进程只在一个地方碰**：CubeMX 的启动、命令、弹框处理集中在会话运行时，别处不直接起进程。
3. **命令行与 MCP 行为一致**：MCP 工具就是命令内核的另一种入口。发布路径只有 `ioc build` → `ioc generate`。
