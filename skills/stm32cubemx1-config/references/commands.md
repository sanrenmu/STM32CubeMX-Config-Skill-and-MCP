# 命令参考

主路径：**目标 JSON → 离线编译 .ioc → 原生生成**。另有离线查询（IP / 板卡 / 时钟拓扑）、
时钟求解（只读看结果）与反解（改现有工程）三个辅助入口。

> 加载时机：要查具体命令、参数、退出码或 `probe` 用法时。默认路径（build → generate）跑通时不必读本文件。
>
> 段索引：一、编译；二、对拍；三、时钟求解；四、生成；五、离线查询；六、反解；七、本机配置；八、MCP 工具。
> 直接跳到对应段。
>
> 写法：每条命令先给**一句话**说清它会干什么，再列参数。

所有命令都支持全局参数 `--json`：输出机器可读 JSON，给人看的文字行换成 JSON 对象。

退出码：`0` 成功；`1` 失败。失败时带 `kind`（target / unknown / unsupported / conflict）与字段路径。

---

## 一、编译

把目标 JSON 离线编成 `.ioc`。不启动 CubeMX，不跑命令序列。

```bash
python -m cubemx1 ioc build <目标.json> [-o 输出.ioc]
```

| 参数          | 含义                                                                   |
| ------------- | ---------------------------------------------------------------------- |
| `目标.json` | 必填。字段见 [目标 JSON 协议](../../../docs/PROTOCOL.md)                |
| `-o`        | 输出路径。不给就落到目标里的 `project.output_dir/<project.name>.ioc` |

输出三行：写出的文件路径、键数、说明（补了哪些默认、钳了哪些值）。
失败时按语义给出 target / unknown / unsupported / conflict 之一，并指到目标里的字段路径。

---

## 二、对拍（开发 / 验收用；发布使用不需要）

加载 `.ioc`、另存、比键值，确认写进去的还在。开发/验收用，不是发布必需步骤。

```bash
python -m cubemx1 ioc check <工程.ioc> [--save-dir 目录]
```

- 在 `--save-dir`（默认 `.ioc` 所在目录）写一份 `<同名>_roundtrip.ioc`。
- 需要 CubeMX 会话。
- 两类差异允许：板卡 seed 的旧 `PCC.*`；`PLLSourceVirtual` 与 `PLLSource` 二选一。

---

## 三、时钟求解

把目标频率解成 RCC 参数并打印结果（含独立核算）。**只读**：不写 `.ioc`、不启动 CubeMX。
它和 `ioc build` 共用求解器（多目标联合搜索 + 最终参数独立重算）。

时钟求解器如有问题可以自行通过clock离线查询获取时钟拓扑，然后自行反算。

```bash
python -m cubemx1 clock solve --device <器件> --target <读数>=<MHz> [--target ...] [--param <参数>=<值> ...]
```

| 参数         | 必填 | 说明                                                                     |
| ------------ | ---- | ------------------------------------------------------------------------ |
| `--device` | 是   | 器件 RefName，例如 `STM32F407Z(E-G)Tx`                                 |
| `--target` | 是   | `读出名=MHz`，例如 `HCLKFreq_Value=168`。可重复；多个目标联合求解    |
| `--param`  | 否   | 显式 RCC 参数当约束，例如 `PLLSourceVirtual=RCC_PLLSOURCE_HSI`。可重复 |

例：F407 解 168 MHz：

```bash
python -m cubemx1 clock solve --device "STM32F407Z(E-G)Tx" --target HCLKFreq_Value=168
```

输出：

```text
device   STM32F407Z(E-G)Tx
target   HCLKFreq_Value = 168.0 MHz
status   solved (writes nothing; run ioc build to write the .ioc)
rcc      AHBCLKDivider=1
rcc      PLLM=8
rcc      PLLN=168
rcc      PLLP=2
rcc      PLLSourceVirtual=RCC_PLLSOURCE_HSI
rcc      SYSCLKSource=RCC_SYSCLKSOURCE_PLLCLK
check    HCLKFreq_Value: final RCC gives 1.68e+08 Hz
note     clock HCLKFreq_Value = 168.0 MHz: solved 6 parameters (expected 168000000 Hz)
```

读输出：

- `rcc` 是最终要写进 `.ioc` 的参数。把它抄进 `clock.params`，或交给 `ioc build` 的 `clock.targets` 重算。
- `check` 是用最终 RCC 参数**独立重算**的结果，不是求解器自报的数。
- `note` 写每个目标怎么解的（解了几个参数、是否零写入、被什么固定值限制）。

失败时退出码 1，`kind` 区分 `exhausted` / `truncated` / `unsupported` / `conflict`。
两点注意：只读命令不写盘；真写 `.ioc` 仍走 `ioc build`。

---

## 四、生成

把编好的 `.ioc` 交给 CubeMX 原生生成 CMake 工程，再检查产物。

```bash
python -m cubemx1 ioc generate <工程.ioc>
```

工具链由目标里的 `project.toolchain` 决定，目前只验过 CMake。生成后检查三件事：

1. 启动文件在工程根目录。
2. 工程文件（CMakeLists / .cmake / Makefile）引用了启动文件。
3. 目标里的数值参数（例如波特率、预分频、堆大小）出现在生成的代码里。

生成目录由 `.ioc` 里的工程设置决定（`project.output_dir`），不是当前目录。

---

## 五、离线查询

**一句话**：查器件定义、外设清单与引脚、板卡和时钟拓扑；只读，不求解频率，不写盘，只读 db 里的 XML，不启动 CubeMX。

```bash
python -m cubemx1 probe peripherals --device <器件> [--board <板卡 id 或板名>]
python -m cubemx1 probe describe --ip <实例> [--device <器件>] [--board <板卡 id 或板名>] [--target 目标.json]
python -m cubemx1 probe pins --ip <实例或 IP 名> --device <器件> [--mode <叶子模式或组名>] [--limit N]
python -m cubemx1 probe boards [--keyword K] [--limit N]
python -m cubemx1 probe board --id <板卡 id 或板名>
python -m cubemx1 probe clock --device <器件> [--view graph|upstream|raw] [--output <读数或元素 id>] [--size N] [--seq N]
```

- `peripherals`：**一句话**：列这颗芯片声明的全部外设/中间件实例，作为 `ips[].instance` 的清单。
  `type` 来自 Modes 文件的 IPType（`peripheral` / `middleware` / `service`）；`missing_modes_file` 是
  "XML 声明了实例、但找不到 Modes 文件"的项（只能确认存在，参数查不到）。

- `describe`：**一句话**：查一个实例的模式与参数（适用定义 / 范围 / 候选 / true|false|unknown）。
  给 `--board` 时可以查板级实例（实例名 = 板名，返回 `ip_type=bspip`）。
- `pins`：**一句话**：查一个外设的"信号↔候选脚"两个方向，以及某个模式需要哪些信号、每个信号能落哪些脚。
  不给 `--mode` 时给整颗外设的歧义清单和按模式的计数；`--mode` 给叶子模式看完整信号表，
  给组名（例如 FMC 的 `Sdram1`）返回组下叶子模式。`ambiguous`=信号多脚，`conflicts`=两个必需信号抢同一脚，
  `missing`=器件 XML 没给候选脚（判不了，不是不用接）。
- `boards` / `board`：**一句话**：官方板卡列表 / 详情。详情含 `config`（seed 的实例、引脚、时钟输入）
  与 `bsp`（原生 BSP 模式与参数）。
- `clock`：**一句话**：查时钟拓扑。不求解频率，不启动 CubeMX。

`clock` 的三个视图：

| 视图              | 看什么                                                                                                                                          |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `graph`（默认） | 全部节点与连接、端口角色、参数绑定（原始 refParameter + 解释后的名字/分母/允许列表）、条件（给 `--target` 时附 true/false/unknown）、歧义候选 |
| `upstream`      | 从 `--output` 出发的全部上游。MUX 的每个分支、小数倍频的分数输入都在                                                                          |
| `raw`           | 整份 XML，或 `--seq` 指定记录号的一个元素片段                                                                                                 |

例子：

```bash
python -m cubemx1 probe peripherals --device STM32H750XBHx
python -m cubemx1 probe describe --ip FMC --device STM32H750XBHx
python -m cubemx1 probe pins --device STM32H750VBTx --ip FMC
python -m cubemx1 probe pins --device STM32H750XBHx --ip FMC --mode sd-16b-d1
python -m cubemx1 probe pins --device "STM32F103C(8-B)Tx" --ip TIM2
python -m cubemx1 probe clock --device STM32H750VBTx --view upstream --output HCLKFreq_Value
python -m cubemx1 probe clock --device "STM32C031C(4-6)Tx" --view graph --size 40
python -m cubemx1 probe clock --device STM32C051C6Tx --view graph --target c051.json
```

`--size` 只限制返回分页。返回里的 `node_total`、`connections_total`、`not_expanded` 说明底层还有多少。

---

## 六、反解（改已有工程用）

**一句话**：把已有 `.ioc` 反解成可改的目标 JSON，再增量修改、整份重编。

```bash
python -m cubemx1 ioc import <工程.ioc> [-o 目标.json]
```

输出**不是** `.ioc`，是一份 `cubemx1.ioc/2` 目标 JSON：

- 工具理解的对象进高级字段（`pins` / `ips` / `clock` / `nvic` / `dma` / `rtos` / `project.options` 等）。
- 工具不理解的键进 `extensions.ioc`，重建时原样写回，一条都不丢。
- 板卡工程会回填 `board`（板名 + `materialized`），BSP 参数与模式保留。
- 产物字段含义见 [目标 JSON 协议](../../../docs/PROTOCOL.md)。

改几处而不重写整份：

```bash
python -m cubemx1 ioc edit <基础目标.json> <修改.json> [-o 输出.json]
```

修改契约是 `cubemx1.ioc-edit/1`，按稳定身份合并，幂等。字段见协议文档的"增量修改"一节。

---

## 七、本机配置

一台电脑一份，放在 `~/.cubemx1/machine.json`，或用环境变量 `CUBEMX1_MACHINE` 指到别处：

```json
{
  "cubemx_home": "<CubeMX 安装目录>",
  "repository": "<固件包仓库目录>"
}
```

`cubemx_home` 必填。`repository` 只在生成代码时需要。缺了会直接报"该填什么"，不会瞎猜路径。

---

## 八、MCP 工具

`clock solve` 只在命令行提供；MCP 工具面仍是六个。

| 工具                       | 对应命令                                                                  |
| -------------------------- | ------------------------------------------------------------------------- |
| `cubemx1_ioc_build`      | `ioc build`（离线）                                                     |
| `cubemx1_ioc_check`      | `ioc check`（开发 / 验收用）                                            |
| `cubemx1_ioc_generate`   | `ioc generate`                                                          |
| `cubemx1_ioc_import`     | `ioc import`                                                            |
| `cubemx1_probe_describe` | `probe describe` / `peripherals` / `pins` / `boards` / `board` / `clock`（对应 `kind=describe/peripherals/pins/boards/board/clock`） |
| `cubemx1_status`         | 看后台进程与会话状态                                                      |
