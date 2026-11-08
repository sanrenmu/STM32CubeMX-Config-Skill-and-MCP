# 工作流程与案例

按任务挑一段。每个案例都写清四件事：目标、命令、看什么、边界。
每节命令先用一句话说清作用；参数细节看[命令参考](commands.md)。

> 加载时机：第一次建工程、要照抄完整案例、写目标 JSON 前要查外设清单/参数/引脚、要配时钟、
> 要查时钟拓扑、按板卡/BSP 建工程时。只读需要的那一段，不要从头读到尾。
>
> 段索引：A 新建；B 外设清单与探测（写 JSON 前先做）；C 配时钟；D 查时钟拓扑；
> E 板卡/BSP；F DMA；G RTOS；H 改现有工程；I 对拍与生成；J 报错定位。

---

## A. 新建一个工程（最小可用）

### A1. F103 + USART1 + 115200

**目标**：PA9/PA10 接 USART1，波特率 115200，开中断。

1. 写目标 JSON：

```json
{
  "contract": "cubemx1.ioc/1",
  "device": {"refname": "STM32F103C(8-B)Tx"},
  "project": {"name": "demo", "output_dir": "D:/projects/demo", "toolchain": "CMake"},
  "pins": [{"pin": "PA9", "signal": "USART1_TX"},
           {"pin": "PA10", "signal": "USART1_RX"}],
  "ips": [{"instance": "USART1", "mode": "Asynchronous",
           "params": {"BaudRate": "115200"}}],
  "nvic": {"vectors": [{"irq": "USART1_IRQn", "priority": 1}]}
}
```

2. 编译：

```bash
python -m cubemx1 ioc build demo.json
```

3. 看输出：写出的 `.ioc` 路径、键数（60 上下），以及"补了哪些默认、钳了哪些值"。
   补默认是正常的。被钳的项要确认是你要的。

**边界**：异步模式必须同时给 TX 和 RX。只给 TX，整个 USART1 会在加载时被丢掉，而且不报错。

### A2. 目标 JSON 从哪查字段

1. 先读 [目标 JSON 协议](../../../docs/PROTOCOL.md)。
2. 字段不确定时，先做 B 段（外设清单、参数与引脚探测），再写 JSON。
3. 不要猜参数名。查不到就按报错里列出的可用项改（B4 会给出所在域的建议）。

---

## B. 外设清单与探测（写目标 JSON 之前）

**这一节解决的问题**：这颗芯片有哪些外设？某个外设支持哪些参数、什么取值？它的信号能落哪些引脚？
名字或取值写偏了会不会被猜？四个问题对应四条只读命令，都不启动 CubeMX。

先说结论：**探测只回答"有什么"，不替你做选择**。名字写错会报错；取值写错不会报错（B5）；
引脚有歧义时必须显式挑（B3）。

### B1. 芯片支持哪些外设

**一句话**：给一颗芯片，列出它声明的全部外设/中间件实例，作为写 `ips[].instance` 的清单。

```bash
python -m cubemx1 probe peripherals --device STM32H750XBHx
```

输出（本机实测）：

```text
device     STM32H750XBHx
peripherals 102
  FMC                    FMC-fmc_v3_0   peripheral
  ...
```

1. `instance` 是目标 JSON 的 `ips[].instance` 要写的名字；不要自己拼（`USART1` 不是 `USART`）。
2. `type` 来自该实例 Modes 文件的 `IPType`：`peripheral` / `middleware` / `service`。
3. `missing_modes_file` 列出"器件 XML 声明了实例、但 Modes 文件找不到"的项：这些只能确认存在，
   参数清单查不到，不能当成"没有参数"。
4. 也可以 `--board <板名>`，器件从板卡记录取。F103 同一命令列出 27 项（含 USART1/2/3、ADC1/2、
   FREERTOS、FATFS），H750XB 是 102 项。

### B2. 一个外设支持哪些参数、什么类型、什么取值

**一句话**：查这个实例的模式、参数名、类型、默认值、取值范围或候选值，以及当前上下文下哪份定义生效。

1. 查：

```bash
python -m cubemx1 probe describe --ip USART1 --device "STM32F103C(8-B)Tx"
```

2. 输出摘录（本机实测）：

```text
param    BaudRate     integer  default 115200     range 110..10500000
param    WordLength   list     default WORDLENGTH_8B values WORDLENGTH_8B, WORDLENGTH_9B
param    StopBits     list     default STOPBITS_1 values STOPBITS_1, STOPBITS_2, STOPBITS_0_5, STOPBITS_1_5
param    Parity       list     default PARITY_EVEN values PARITY_EVEN, PARITY_ODD
param    Mode         list     default MODE_TX_RX values MODE_TX_RX, MODE_RX, MODE_TX
param    UsartParity  not applicable
```

3. 读法：
   - `integer` + `range`：写数字，落在区间内。
   - `list` + `values`：**写列出的枚举名**（不要自己缩写，见 B5）。
   - 默认值可以省略不写；写默认值不报错，只是多一个键。
   - `not applicable`：当前模式下这条定义不生效。换模式后再查。
4. **同一个参数可能按模式/条件分多份定义**：没给决定条件时 `domain.values` 可能为空。
   这不是"没有候选值"，是"当前上下文决定不了"。FMC 的 SDRAM 参数（`SDBank1`、`SDMemoryDataWidth1`、
   `ColumnBitsNumber1` 等）在没选 SDRAM 引脚/模式时就会这样；把 `--target` 或模式引脚给进去再查。
   条件求值见 B7。

### B3. 外设支持哪些引脚组合

**一句话**：查一个外设的信号↔候选脚两个方向，以及某个叶子模式需要哪些信号、每个信号能落哪些脚。

相同pin可能支持相同外设不同引脚，相同引脚可能支持多个pin。

先看整颗外设（本机实测，数字与 CLI 阶段的记录一致）：

```bash
python -m cubemx1 probe pins --device STM32H750VBTx --ip FMC
```

```text
device     STM32H750VBTx
instance   FMC (FMC)
pins       43  signals 57
one pin, many signals 19
  PC8                    FMC_NCE / FMC_NE2
  PD0                    FMC_D2 / FMC_DA2
  ...
signal, many pins 5
  FMC_NE1                PC7, PD7
  FMC_NWAIT              PC6, PD6
  ...
```

1. `one pin, many signals`：一个脚上挂着同一外设的多个信号，写目标时要连"用哪个信号"一起写清
   （`FMC_D2` 还是 `FMC_DA2`）。
2. `signal, many pins`：一个信号有多个候选脚，**必须显式挑一个**；工具不会替你挑。
3. 数字来自器件 XML，同一器件不同封装不同：H750XB（LQFP144）是 94 脚 / 101 信号。
4. 一个 TIM 也可能很多脚：F103 的 TIM2 是 8 脚 / 5 信号，`TIM2_CH1` 在 `PA0-WKUP` 和 `PA15` 上都有。

再看某个模式要什么（模式名不确定就先给组名）：

```bash
python -m cubemx1 probe pins --device STM32H750XBHx --ip FMC --mode Sdram1
python -m cubemx1 probe pins --device STM32H750XBHx --ip FMC --mode sd-16b-d1
```

```text
group      Sdram1  leaf modes 12
  SdramChipSelect1_1             required 2 ambiguous 2 conflicts 0 missing 0
  sd-16b-d1                      required 16 ambiguous 0 conflicts 0 missing 0
  ...
mode       sd-16b-d1  required 16
  FMC_D0                     PD14
  FMC_D1                     PD15
  ...
```

5. `required` 是这个叶子模式需要的信号数；`pins` 是每个信号的候选脚。
6. `ambiguous` = 这个信号有多个候选脚；`conflicts` = 两个必需信号只有同一个候选脚；
   `missing` = 器件 XML 没给这个信号候选脚（不是"不用接"，是数据上判不了）。
7. 组名（如 `Sdram1`）返回它的叶子模式和计数；挑一个叶子再查完整信号表。

**边界**：查询只给候选。写目标时 `pins[]` 必须显式给脚——模式信号没接全，整个实例会在 CubeMX 加载时
被静默丢掉（A1）。不想手挑可以用目标的 `allocation.pins=auto`，但分配结果要按本命令复核。
板卡工程可以直接读 seed 里现成的引脚：`probe board --id ...` 的 `config.pins`（H750B-DK 的 FMC SDRAM
就是 37 根脚，见 E 段）。

### B4. 名字不完全匹配：建议只在一个域里找，且不自动改

**一句话**：名字写偏时报错会附"最接近的几个"，并写明是从哪个域里找的；它不会替你改写。

1. 参数名小写（`baudrate`）：

```text
unknown: ips.USART1.params: parameter 'baudrate' does not belong to the current mode of USART1: 'Asynchronous'.
  Did you mean (parameters of USART1 in mode 'Asynchronous'): BaudRate
```

2. 参数名少一截（`Baud`）：

```text
unknown: peripheral USART1 has no parameter 'Baud'.
  Did you mean (parameters of USART1 in mode 'Asynchronous'): BaudRate
```

3. 实例名写错（`USART`）：

```text
unknown: this device has no instance 'USART'; available: ...
  Did you mean (instances of STM32F103C(8-B)Tx): USART1, USART2, USART3
```

4. 其它入口同样规则：

```text
no clock element matches 'HCLKFreq_Valu' ... Did you mean (clock readouts of STM32C031C(4-6)Tx): HCLKFreq_Value
'FMCX is not defined on ... Did you mean (IP instances and names declared by ...): FMC
no mode 'Sdram9' on FMC ... Did you mean (leaf modes of FMC): SdramChipSelect1_1, ...
```

5. **查询域是明确的**：参数名只在"这个实例+这个模式"的参数里找；实例名只在"这颗器件"的实例里找；
   读数只在"这棵时钟树"里找；模式名只在"这个外设的叶子模式"里找。不会把参数名建议成外设名。
6. **建议只是建议**：工具不会采用建议项；写错名字仍然报错。

**边界**：这是有意设计。静默改写成"看着像"的名字，会把配置写到别的键上。

### B5. 取值不完全匹配：别猜，build 不校验值

**一句话**：参数值写错不会报错，会原样写进 `.ioc`；写值前先用 B2 的 `values` / `range` 对照。

1. 写 `WordLength: "9"`（真实候选是 `WORDLENGTH_9B`）。编译**成功**，产物里是：

```text
USART1.WordLength=9
```

2. 写 `Parity: "X"`（候选只有 `PARITY_EVEN` / `PARITY_ODD`）。编译**成功**，产物里是：

```text
USART1.Parity=X
```

3. 结论：**名字错会报错，值错不会报错**。值原样写进 `.ioc`；加载时 CubeMX 是否接受是另一回事。
4. 做法：枚举参数写枚举名，数字参数写数字，并且落在 `range` 内。

### B6. 只有这两类"规范化"，不是模糊匹配

**一句话**：工具会做的名字归一化只有两类（引脚短名/全名、RCC 的 `*Virtual`）；其它名字和取值都不归一化。

| 情况                                                                            | 行为                                          | 依据                                                                     |
| ------------------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------ |
| 引脚短名 vs 全名（`PA0` vs `PA0-WKUP`、`PB3` vs `PB3 (JTDO/TRACESWO)`） | 目标可写短名；解析时短名/全名都认，写盘用全名 | `mcp/cubemx1/iocc/derive.py` 的 `pin_by_name` / `_pin_short_names` |
| RCC 参数有 `*Virtual` 变体（例如 `PLLSourceVirtual`）                       | 写盘时优先 Virtual 变体                       | `mcp/cubemx1/iocc/buildclock.py` 的 `rcc_param_name`                 |

这两类是"同一个东西的两个合法写法"，不是"猜你想写什么"。B4 的建议只出现在报错文本里，
不会参与解析；参数名、模式名、取值都按定义里的写法。

### B7. 同一个参数有多份定义（条件不同）

**一句话**：同一参数在不同条件下类型/候选不同，先看哪份生效，别猜。

1. `probe describe` 的输出会标 `selected` / `true` / `false` / `unknown`。
2. `unknown` = 当前目标信息不足，不能确定用哪份。工具会**停下报 unknown**，
   不会跳到后面那份无条件定义（那等于猜）。
3. 做法：把决定条件的东西写进目标（模式、外设实例、引脚），再查一次。

**边界**：条件求值是三值的；不要因为"看起来像默认那份"就按那份写。

---

## C. 配时钟（目标频率）

规则先看这一节，后面几个案例直接用。

- 目标频率写在 `clock.targets`。每项是 `parameter`（读出名）或 `peripheral`（外设名），加 `mhz`。
- 也可以写 `clock.params` 显式给 RCC 参数。两种不要对同一个参数给两个值，冲突会报 `conflict`。
- 多个目标联合求解。解出来的每个目标，最后都会用**最终 RCC 参数独立重算**，不是只信求解器自己的数。
- 已经满足的目标零写入，不算失败。
- 解不出来时的状态要分开读：`exhausted`（搜完了）/ `truncated`（预算截断，未证明）/
  `unsupported`（结构没建模）/ `fixed_conflict`（显式值冲突，会点名）。

### C1. 先单独跑求解器看结果（只读）

只想先知道会解出什么参数，不写文件：

```bash
python -m cubemx1 clock solve --device "STM32F407Z(E-G)Tx" --target HCLKFreq_Value=168
```

输出三段：`rcc`（最终参数）、`check`（用最终参数独立重算的结果）、`note`（每个目标怎么解）。
确认没问题，再写进目标 JSON，或直接交给 `ioc build`。
这条命令只读：不写 `.ioc`、不启动 CubeMX。

### C2. F4：HCLK = 168 MHz，再加 USB 48 MHz

**目标**：系统跑 168 MHz，USB 全速要 48 MHz。

```json
"clock": {
  "targets": [
    {"parameter": "HCLKFreq_Value", "mhz": 168},
    {"parameter": "48MHZClocksFreq_Value", "mhz": 48}
  ]
}
```

1. 编译后看说明：第一条目标解出 PLL 的 M/N/P 与 `SYSCLKSource=PLLCLK`。
2. 第二条目标只解剩余的输出分频（USB 那条通常落在 `PLLQ`）。
   已解的 M/N 不再重枚举，所以第一条目标不会被推翻。
3. 如果第二条解不出来，报 `fixed_conflict`。读理由里点名的参数。

**边界**：USB 全速需要 48 MHz。器件定义里能查到"这个读数被谁消费"，工具会自己找读数名。

### C3. C0：没有 PLL，纯分频

**目标**：C031 要 48 MHz，再要 24 MHz。

```json
"clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 48}]}
```

1. 48 MHz 解出 `HSISYSCLKDivider=1`。
2. 24 MHz 解出分频器组合（`HSISYSCLKDivider` / `AHBCLKDivider` 之一或其乘积）。
3. 两个目标一起写时，求解器会回溯。两个顺序都通过。

**边界**：C0 没有 PLL，路径按"纯分频"处理。这是**按路径形状**分派，不是按家族写分支。

### C4. H7：小数 PLL

**目标**：H750 的 HCLK = 13.0001 MHz。整数 N 到不了这个频率。

```json
"clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 13.0001}]}
```

1. 编译输出会带小数解：本机实测是 `DIVM1=25`、`DIVN1=253`、`DIVP1=50`、`PLLFRACN=7440`。
2. 独立重算：`64000000/25 × (253 + 7440/8192) / 50 = 13000100 Hz`。
3. 显式固定 `PLLFRACN` 时，求解器按固定值解。冲突会点名 `PLLFRACN`。
4. 分辨率不够（例如需要 `F = 8192/3`）时报 `exhausted`，理由写"分数分辨率"。

**边界**：别的 PLL 的 FRACN 与目标无关，不参与求解，也不会让目标失败。

---

## D. 先查时钟拓扑，再写时钟参数

查询器是只读的：不求解频率，不启动 CubeMX。它给你原始事实和索引，由你判断怎么写。

三个视图：

| 视图         | 命令                                                            | 看什么                                   |
| ------------ | --------------------------------------------------------------- | ---------------------------------------- |
| `graph`    | `probe clock --device <器件> --view graph`                    | 全部节点与连接、参数绑定、条件、歧义候选 |
| `upstream` | `probe clock --device <器件> --view upstream --output <读数>` | 某个频率的全部上游路径                   |
| `raw`      | `probe clock --device <器件> --view raw [--seq N]`            | 原始 XML，或一个元素的原始片段           |

### D1. H750：HCLK 由谁算出来

1. 查上游：

```bash
python -m cubemx1 probe clock --device STM32H750VBTx --view upstream --output HCLKFreq_Value
```

2. 读返回：节点 14 个，路径 14 条。`DIVN1` 是 `multiplicatorFrac`，两个端口标成
   `input`（来自 `DIVM1`）和 `fractional`（来自 `PLLFRACN`）。
   `PLLSource` 一个节点、三条下游声明；`SysClkSource` 的输入标成 `branch`。
3. 结论：要动 HCLK，先决定走 PLL1 还是直供 HSI。走 PLL1 时，`DIVM1`/`DIVN1`/`DIVP1` 一起动。

**边界**：`branch` 是备选，不是同时输入。

### D2. C031：两个目标为什么能成立

1. 查 SYSCLK 的上游：

```bash
python -m cubemx1 probe clock --device "STM32C031C(4-6)Tx" --view upstream --output SYSCLKFreq_VALUE
```

2. `SysClkSource` 四个分支按文档顺序是 `LSIRC`、`HSEOSC`、`HSISYS`、`LSEOSC`。
   `HSISYS` 的参数绑定是 `HSISYSCLKDivider`。
3. 结论：SYSCLK = HSI 48 MHz ÷ `HSISYSCLKDivider`；HCLK 再除以 `AHBCLKDivider`。
   两个目标一起要，就是 `HSISYSCLKDivider=1` + `AHBCLKDivider=4`。

### D3. 原始 XML：查一个元素的原始属性

1. 先 `--view graph --size 5`，从 `node_ref`（形如 `raw:12`）拿记录号。
2. 再取片段：

```bash
python -m cubemx1 probe clock --device "STM32C031C(4-6)Tx" --view raw --seq 12
```

3. 片段里的 `tag`、全部属性、`text` 与子节点来自原文件。命名空间与排版不保证逐字节还原。

### D4. 有 target 时看条件真假

1. 目标里列出 `FDCAN1` 实例，再查图：

```bash
python -m cubemx1 probe clock --device STM32C051C6Tx --view graph --target c051.json
```

2. 条件 `FDCAN1_Exist` 的 `truth` 是 1；目标里不列 `FDCAN1` 时是 `null`（unknown），不是 0。
3. 不给 target 时，条件只给定义，不假定成立。

**边界**：查询器不判断"这组参数能不能配出目标"。它只给拓扑和绑定。

---

## E. 按官方板卡建工程（含 BSP）

### E1. NUCLEO-H723ZG + LED + VCP

1. 先查板卡：

```bash
python -m cubemx1 probe board --id NUCLEO-H723ZG@Rev.E@STMicroelectronics
```

   看 `mounted_device`、`config`（seed 的实例/引脚/时钟输入）与 `bsp`（组件参数与候选值）。

2. 写目标：

```json
{
  "contract": "cubemx1.ioc/2",
  "board": {"id": "NUCLEO-H723ZG@Rev.E@STMicroelectronics", "initialize": "defaults"},
  "project": {"name": "nucleo", "output_dir": "D:/projects/nucleo", "toolchain": "CMake"},
  "ips": [{"instance": "NUCLEO-H723ZG", "mode": "COMMON",
           "params": {"LD1": "true", "VCP": "true"}}]
}
```

3. 编译，然后生成。
4. 生成后看 `.ioc` 回写：板卡实例、`BSP_IP_NAME`、组件参数都在。

**边界**：组件展开成 MCU 配置（LED → GPIO、VCP → USART）由 CubeMX 加载时完成。

### E2. 反解出来的真实板卡工程

1. 用 `ioc import` 反解。产物里 `board.initialize` 是 `materialized`。
2. `materialized` 不重新套 seed，只保留板卡身份。已展开的配置原样保留。
3. 改完再编，不要回去套 `defaults`，否则会覆盖已展开的部分。

---

## F. DMA（含自动挑通道）

### F1. F1：USART1_TX 用 DMA1_Channel4

```json
"dma": [{"request": "USART1_TX", "instance": "DMA1_Channel4"}]
```

1. `instance` 显式给时，工具按 db 的模式树校核：这个请求能不能接这条通道。
2. 接不上就报错，不会静默换一条。

### F2. v2：让工具挑通道

```json
"allocation": {"dma": "auto"},
"dma": [{"request": "USART1_TX"}]
```

1. 候选来自模式树：控制器 → 通道/流 → 请求。
2. 多个请求竞争同一通道时，工具回溯求解。
3. 无解时给结构化冲突：哪个请求、候选有哪些、被谁占着。

**边界**：v1 里 `instance` 必填。自动挑只在 v2 的 `allocation.dma=auto` 下生效。

---

## G. RTOS 对象表

### G1. F4：两个任务 + 一个队列

```json
"ips": [{"instance": "FREERTOS", "mode": "CMSIS_V2",
         "params": {"configTOTAL_HEAP_SIZE": "15360"}}],
"rtos": {
  "tasks": [
    {"name": "defaultTask", "priority": 24, "stack": 128, "entry": "StartDefaultTask"},
    {"name": "ledTask", "priority": 8, "stack": 128, "entry": "StartLedTask"}
  ],
  "queues": [{"name": "ledQueue", "size": 8, "item_size": 4}]
}
```

1. 编译。工具把对象表拼成 `FREERTOS.Tasks01` / `FREERTOS.Queues01` 行，并补全 `IPParameters` 清单。
2. 校验规则自动生效：CMSIS_V2 优先级只收 1 或 8..56；栈不小于 `configMINIMAL_STACK_SIZE`；
   第一行必须是默认任务，不是就补一行并说明。
3. 生成后，初始化在 `Src/main.c`，`Src/freertos.c` 是空壳。

**边界**：不要手写 `FREERTOS.*` 行。清单漏一项，加载时整张表读不回来。

### G2. 事件组（只在 CMSIS_V2）

1. `rtos.events` 写事件组。
2. 加载时会打一行 `Invalid parameter (Events01)`。这是已知噪音：参数由插件运行时创建。
3. CMSIS_V1 或原生 API 下写事件组会报 `unsupported`。

---

## H. 改现有工程（import + edit）

**目标**：把波特率从 115200 改成 921600，并加一个中断。

1. 反解：

```bash
python -m cubemx1 ioc import project.ioc -o base.json
```

   产物是 `cubemx1.ioc/2`。理解的对象进高级字段，未理解的键进 `extensions.ioc`，一条都不丢。

2. 写修改：

```json
{
  "contract": "cubemx1.ioc-edit/1",
  "set": {
    "ips": [{"instance": "USART1", "params": {"BaudRate": "921600"}}],
    "nvic": {"vectors": [{"irq": "USART1_IRQn", "priority": 1}]}
  }
}
```

3. 应用修改：

```bash
python -m cubemx1 ioc edit base.json change.json -o edited.json
```

4. 整份重编：`python -m cubemx1 ioc build edited.json`。

**边界**：

- 按稳定身份合并：引脚=完整引脚名、IP=实例名、参数=实例+参数名、NVIC=IRQ、DMA=请求名、RTOS=类别+名字、常量=名字。
- 省略的段保持原值；空数组不表示清空。
- 同一对象同时出现在 `set` 与 `remove` 会报冲突。
- 同一份修改连续应用两次，结果不变。

---

## I. 对拍与生成（开发 / 验收用）

### I1. 对拍（发布不需要）

```bash
python -m cubemx1 ioc check 工程.ioc --save-dir out
```

1. 加载 `.ioc`，另存，再比对"目标要求的键都在、值一致"。
2. 能看出三类问题：丢键、值被归一化、日志报错。
3. 两类差异允许：板卡 seed 的旧 `PCC.*`；`PLLSourceVirtual` 与 `PLLSource` 二选一。
4. 有噪音行是正常的：DMA 的 `Parameter ... currently not set`、事件组的 `Invalid parameter (Events01)`。

### I2. 生成并检查产物

```bash
python -m cubemx1 ioc generate 工程.ioc
```

1. 工具检查三件事：启动文件在根目录、工程文件引用了它、关键数值在生成的代码里。
2. 生成成功不等于编译通过，更不等于上板跑通。

---

## J. 报错定位

| 你看到的                                    | 去哪                                         |
| ------------------------------------------- | -------------------------------------------- |
| 外设清单/参数名/取值/引脚不确定，或名字写偏 | 本文件 B 段；命令参考第五节                  |
| build 报 unknown / unsupported / conflict   | [行为手册](handbook.md) 第一节                |
| 对拍变红                                    | [行为手册](handbook.md) 第二节                |
| 生成失败或缺产物                            | [行为手册](handbook.md) 第三节                |
| 时钟目标解不出来                            | [行为手册](handbook.md) 第五节                |
| 想单独看求解器给出的参数与理由              | [命令参考](commands.md) 第三节（clock solve） |
