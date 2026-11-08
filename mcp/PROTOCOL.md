# MCP 协议与工具契约

这份讲**把 `cubemx1` 注册成 MCP 服务之后，宿主和它之间怎么说话**。
启动方式、依赖、工具清单的机器可读版本在 [`mcp.json`](mcp.json)；这里是给人看的契约。

## 一、传输

- **stdio**，一行一条 JSON-RPC 2.0 消息。
- **stdout 只放协议消息**；日志走 stderr。所以调用方可以直接按行读。
- 没有鉴权、没有网络端口：它是本机进程，权限就是当前用户的权限。

## 二、方法（服务器实现的）

| 方法 | 说明 |
|---|---|
| `initialize` | 回协议版本、能力（`tools` + `resources`）、服务器名与版本、**`instructions`**（怎么用这台服务的几句话） |
| `notifications/initialized` | 通知，**不回** |
| `tools/list` | 回工具清单（`name` / `description` / `inputSchema`） |
| `tools/call` | 调一个工具，`params = {name, arguments}` |
| `resources/list` | 回可读文档清单（`uri` / `name` / `description` / `mimeType`） |
| `resources/read` | 读一份文档，`params = {uri}`；没有这个 uri 回 `-32602` |
| `resources/templates/list` | 回空清单（保留位，服务端没有参数化资源） |
| `ping` / `server/discover` | 回空对象（保活/探测） |
| 其它 | 有 `id` 就回 `-32601 不支持的方法`；没有 `id` 就静默丢弃 |

- 协议版本：`2024-11-05`。
- 服务器名：`cubemx1`。

### `tools/call` 的返回形状

```json
{
  "content": [{"type": "text", "text": "<工具结果的 JSON 字符串>"}],
  "isError": false
}
```

- `isError` 由**工具结果里的 `ok` 字段**决定（`ok=false` → `isError=true`）。
- 工具内部抛异常时**不**变成 JSON-RPC 错误，而是转成
  `{"ok": false, "error": "<异常类型>: <消息>"}`，照上面的形状回。
- 只有"工具名不存在"才回 JSON-RPC 错误（`-32602`），并在消息里列出可用工具。

## 三、资源（resources）

工具描述是主入口；放不进描述的长文档走资源。**`docs/PROTOCOL.md` 这类文件不会被自动读进上下文**，只有登记成资源（或由 Skill 路由）才到得了模型那边。

| uri | 内容 |
|---|---|
| `cubemx1://protocol/target` | 目标 JSON 的完整契约（`docs/PROTOCOL.md`）：各段字段、`/1` 与 `/2` 的差别、错误 kind |
| `cubemx1://protocol/mcp` | 这份文件：传输与方法、工具契约、失败语义 |

`initialize` 的 `instructions` 指向这两个 uri，并给出"build → generate"的最短路径。

## 四、工具契约

六个工具。**每个都是"接参数 → 交给内核 → 把结果转成 JSON"，没有业务逻辑。**
配置的"目标状态"全写在 `cubemx1_ioc_build` 的那份 JSON 里（契约 `cubemx1.ioc/1` 或 `cubemx1.ioc/2`）。
反解与增量修改在命令行入口（`ioc import` / `ioc edit`），不单独开放成 MCP 工具。
（旧的 `apply` / `query` / `save` 已经下线；发布路径不做对拍，加载/另存那套在开发侧。）

### `cubemx1_ioc_build`

把目标 JSON 离线编译成 canonical `.ioc`。**不启动 CubeMX**，所以不需要会话。

| 入参 | 必填 | 说明 |
|---|---|---|
| `target` | 是 | 目标：JSON 对象，或 JSON 文本，或 `.json` 文件路径。顶层必须有 `contract: "cubemx1.ioc/1"` 或 `"cubemx1.ioc/2"`；字段见 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md) |
| `output` | 否 | 输出 `.ioc` 路径；不给就落到目标里的 `project.output_dir/<project.name>.ioc` |

返回：`{"ok": true, "ioc": "<路径>", "keys": <键数>, "notes": ["补了哪些默认/钳了哪些值"]}`。

失败时 `ok=false`，并给 `stage` 和 `kind`：

| `kind` | 含义 |
|---|---|
| `target` | 目标 JSON 形状不对（缺字段、多字段、类型错） |
| `unknown` | 定义里查不到、目标也没写（实例不存在、引脚没这个信号、参数不属于当前模式、时钟解不出） |
| `unsupported` | 这个机制不在支持范围内（例如双核/TZ、X-CUBE 包） |
| `conflict` | 目标里两处冲突（同一根脚两个信号之类） |

`error` 里带字段路径（例如 `ips.USART1.params`），直接指到写错的那一行。

### `cubemx1_ioc_check`

**【开发 / 验收用；不是发布使用的必需步骤】** 加载 .ioc → 另存 → 比对
"目标要求的键都在且值一致"，并扫加载日志。

| 入参 | 必填 | 说明 |
|---|---|---|
| `ioc` | 是 | 要检查的 .ioc 路径 |
| `save_dir` | 否 | 另存到哪；默认 .ioc 所在目录（会在那里写 `<同名>_roundtrip.ioc`） |

返回：`ok`、`problems`、`log_errors`、`roundtrip`、`keys_checked`。需要 CubeMX 会话。
用户正常只用 build + generate，不需要调用它。

### `cubemx1_ioc_generate`

把编译好的 `.ioc` 交给 CubeMX 原生生成，并检查产物。

| 入参 | 必填 | 说明 |
|---|---|---|
| `ioc` | 是 | 已编译好的 `.ioc` 路径 |

返回（关键字段）：`ok`、`output_dir`、`check`（启动文件是否在、工程是否引用、关键数值是否进源码）、
`log_errors`、`generate_ok`。

需要 CubeMX 会话和对应家族的固件包。**生成成功 ≠ 编译通过 ≠ 上板跑通。**

### `cubemx1_ioc_import`

把一个已有 `.ioc` 反解成 `cubemx1.ioc/2` 目标 JSON。**不启动 CubeMX**。

| 入参 | 必填 | 说明 |
|---|---|---|
| `ioc` | 是 | 要反解的 `.ioc` 路径 |
| `output` | 否 | 写到哪个 JSON；不给就写同名 `.json` |

返回：`ok`、`json`（目标文件路径）、`report`（键总数、语义化/未语义化计数、各类对象个数）。
模型里没有语义的键进 `extensions.ioc` 并逐条列在 `report.unsemanticized` 里，不静默丢。

### `cubemx1_probe_describe`

离线查询：IP 的定义与"适用定义"（同名参数有多份定义时哪一份生效、为什么），或 ST 官方板卡。
**不启动 CubeMX**，与编译共用同一套适用定义选择。

| 入参 | 必填 | 说明 |
|---|---|---|
| `kind` | 否 | `describe`（默认）/ `peripherals` / `pins` / `boards` / `board` / `clock` |
| `ip` | describe / pins 必填 | 实例名或 IP 名，例如 `USART1` / `FMC`；板级实例就是板名（配合 `board`） |
| `device` | 否 | 器件 RefName；不给就从 `target` 取 |
| `target` | 否 | 目标 JSON；用它推出当前状态（引脚/参数） |
| `board` | board 必填；describe 可选 | `board` 必填：板卡 id，例如 `NUCLEO-F429ZI@Rev.B@STMicroelectronics`。`describe` 给 `board` 时按该板加载原生 BSP 定义（与编译同一份上下文），可查板级实例 |
| `keyword` | boards 用 | 板卡关键字，例如 `NUCLEO` |
| `limit` | 否 | `boards` 返回条数上限（默认 50）；`pins` 的模式汇总行数上限（默认 50） |
| `output` | clock 的 upstream 必填 | 读数参数名（例如 `HCLKFreq_Value`）或元素 id |
| `view` | 否 | clock 视图：`graph`（默认）/ `upstream` / `raw` |
| `size` | 否 | clock 视图返回分页上限；只限返回，不删底层模型 |
| `seq` | 否 | clock 原始层记录号（`view=raw` 取一个元素的片段） |
| `tree` | 否 | clock 树名覆盖（默认取器件 XML 的 ClockTree） |
| `mode` | pins 可选 | 叶子模式名（例如 FMC 的 `sd-16b-d1`）看完整信号表；给组名（例如 `Sdram1`）返回其叶子模式 |

返回随 `kind` 变：`describe` 给模式、参数（适用定义 / 取值范围 / true|false|unknown）、引脚候选、
`ip_type`（板级实例为 `bspip`），板级模式带 `id`/`is_leaf`（同名父子模式保留身份）；
`peripherals` 给器件声明的全部实例（`instance`/`name`/`version`/`type`）与 `missing_modes_file`；
`pins` 给整颗外设的 `pins`（脚→信号）与 `signals`（信号→候选脚）两个方向、`pin_with_many_signals` /
`signal_on_many_pins`、按模式的 `mode_summary`；带 `mode` 时给 `block`（`signals[].pins`、`ambiguous`、
`conflicts`、`missing`），带组名时给 `children`；
`boards` / `board` 给板名、器件、revision、seed、`has_bsp`，`board` 详情另有 `config`
（seed 的实例/引脚/时钟输入）与 `bsp`（原生 BSP 模式与参数）。

`clock` 是**只读拓扑查询**（不求解频率、不启动 CubeMX）：`graph` 给节点（`node_ref`、原 id、
类型、全部属性、全部条件、端口、参数绑定）与连接（两份声明、方向、解析状态、歧义候选）；
`upstream` 给某个读数的全部上游（MUX 的全部备选分支、小数倍频的分数输入都保留）与路径；
`raw` 给原始 XML 或单元素片段。歧义引用列全部候选，**不替调用方选第一个**。
给了 `target` 时，条件附 `truth`（true / false / unknown）；不给只给定义，不假定成立。

### `cubemx1_status`

看后台进程与会话状态。**它本身不起会话**——后台没在跑就立刻回 `running: false`。

- 入参：无（`{}`）
- 返回：`{"ok": true, ...}`，含是否在跑、端口、当前工程等。

## 五、兼容与版本

- **工具名与入参名**是稳定契约；**新增可选入参**不算破坏性变更。
- 返回里的 `ok` / `warnings` / `checks` 形状稳定；其它字段可能增补。
- 删除或改语义的变更会提升包版本，并在 `mcp.json` 的 `version` 里体现。
- 协议版本（`protocolVersion`）跟 MCP 的传输/方法约定走，与工具契约是两回事。

## 六、依赖

见 [`mcp.json`](mcp.json) 的 `requirements`：Python ≥3.8（只用标准库）、本机装有
STM32CubeMX 1.x、以及**仓库外**的 `~/.cubemx1/machine.json`（记本机路径）。
生成代码还要求对应家族的固件包已经下到本机。