# 目标 JSON 协议（cubemx1.ioc/1，扩展版 cubemx1.ioc/2）

这份文件说清"要什么配置"怎么写。工具把它离线编译成 STM32CubeMX 1.x 能直接加载的 `.ioc`。

- 版本钉死：CubeMX 6.17.0-RC5 / DB 6.0.170 / 文件版本 6。
- 大小写敏感；**不认识的字段一律拒**（不会"先忽略"）。
- 值都是**字符串**（数字也写成字符串），因为 .ioc 里就是文本。

契约有两个版本号：

| `contract` | 说明 |
|---|---|
| `cubemx1.ioc/1` | 较早的实现，只有本文件"新增段"之前那些字段 |
| `cubemx1.ioc/2` | 在较早的实现上增加 `board` / `allocation` / `connections` / `extensions`，
以及 `project.options|extra`、`pins[].shared`、`ips[].modes`、`nvic.flags`、`nvic.vectors[].raw` |

`/1` 的行为不变：新字段只写在 `/2` 里；写 `/1` 时带新字段会报字段类型错。

## 顶层结构

`contract` 必填（`"cubemx1.ioc/1"` 或 `"cubemx1.ioc/2"`）。其余段：

| 段 | 必填 | 说明 |
|---|---|---|
| `device` | 板卡模式下可省 | 器件 |
| `project` | 是 | 工程名、输出目录、工具链、堆栈、工程选项 |
| `board` | 否（v2） | ST 板卡：套板卡默认配置或只保留板卡身份 |
| `allocation` | 否（v2） | 自动引脚 / DMA 分配的开关与策略 |
| `connections` | 否（v2） | 只给信号、让工具挑引脚的接线表（可限制候选端点） |
| `extensions` | 否（v2） | 反解时没语义化的 `.ioc` 键，原样写回 |
| `pins` | 否 | 引脚与信号（不写就不配引脚） |
| `ips` | 否 | 外设 / 中间件实例、模式、参数 |
| `clock` | 否 | 显式时钟参数，或目标频率 |
| `nvic` | 否 | 优先级组与中断向量 |
| `dma` | 否 | DMA 请求与通道 |
| `rtos` | 否 | RTOS 对象表（任务/队列/定时器/信号量/事件组） |
| `constants` | 否 | 用户常量（写进 `Mcu.UserConstants`） |

## device

| 字段 | 必填 | 说明 |
|---|---|---|
| `refname` | 给了 `board` 时可省 | CubeMX 认得的引用名，例如 `STM32F103C(8-B)Tx`；省了取板卡的 `mountedDevice` |
| `cpn` | 否 | 料号（写进 `Mcu.CPN`，一般不用） |

引用名不是丝印型号。写错会让启动文件缺失，而生成仍返回成功——所以别猜，从
CubeMX 器件选择器里抄那个名字。

## project

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 工程名（决定 .ioc 文件名和 `ProjectManager.ProjectName`） |
| `output_dir` | 是 | 输出目录；编译产物 `<output_dir>/<name>.ioc`，生成目录也是它 |
| `toolchain` | 否 | `CMake`（默认） / `Makefile` / `STM32CubeIDE`；目前只验过 CMake |
| `heap` / `stack` | 否 | 栈堆大小，例如 `"0x400"` |
| `library_copy` | 否 | 固件库拷贝方式 |
| `options` | 否（v2） | 7 个工程选项：`KeepUserCode`、`DeletePrevious`、`BackupPrevious`、`CoupleFile`、`NoMain`、`HalAssertFull`（这 6 个是布尔，必须写 JSON 布尔）、`FirmwarePackage`（字符串，固件包目录名） |
| `extra` | 否（v2） | 其它 `ProjectManager.*` 键（字符串），原样写回；和工具自己写的键重名会报冲突 |

## pins

每个元素：

| 字段 | 必填 | 说明 |
|---|---|---|
| `pin` | 是 | 引脚名，例如 `PA9`；短名（`PA0`）会自动匹配到完整名（`PA0-WKUP`） |
| `signal` | 是* | 该引脚支持的信号名，例如 `USART1_TX`。*v2 里允许空串：这时必须 `locked: true`，表示"保留脚"（不写 `.Signal`，只写 `.Locked`；BSP 展开的未启用引脚就是这种形态） |
| `mode` | 否 | 信号在多个模式里都能用时要显式给（例如 USB_OTG_FS_DM 有 3 个模式）；不给且唯一时会推出来 |
| `locked` | 否 | 锁定该引脚（写 `<pin>.Locked=true`） |
| `gpio` | 否 | 该引脚的 GPIO 参数（键名由器件定义决定），例如 `{"GPIO_Label": "LED"}` |
| `shared` | 否（v2） | 共享组的多接法：`[{signal, mode?}, ...]`；一般由反解自动生成，手写场景很少 |

**模式要用的引脚要写全。** 实测：USART 异步模式只写 TX 不写 RX 时，加载会把整个 USART 实例丢掉
（连同它的参数和中断）。工具不会替你补脚；少给会被 CubeMX 丢掉，`Mcu.IP` 清单就对不上。

**共享信号**（可 remap 的定时器通道、ADC 共用通道）不需要特殊写法：
给引脚 + 信号，工具会按模式定义里的共享组写成
`<pin>.Signal=<组名>` + `SH.<组名>.0=<信号>,<模式名>`。

## ips

每个元素：

| 字段 | 必填 | 说明 |
|---|---|---|
| `instance` | 是 | 器件里的实例名，例如 `USART1` / `FREERTOS` / `USB_DEVICE` |
| `mode` | 否 | 模式的**内部名**（不是界面显示名），例如 `Asynchronous` / `CMSIS_V2` / `CDC_FS` |
| `params` | 否 | 参数名 → 值；参数必须属于当前模式 |
| `modes` | 否（v2） | 这个 IP 的多个独立模式组：`[{name, selected?}]`，工具为每组生成自己的虚拟脚 |

参数等于默认值可以不写（加载后模型会补）。写了但不属于当前模式会报 `unknown`，
报错里会给这个模式可用的参数名。

**RCC 的参数不要写在 `ips` 里**：时钟统一走 `clock` 段。写在 `ips` 里会和时钟求解的结果
抢同一个键，工具直接报 `conflict` 并指到 `clock`。

**中间件**也是 `ips` 里的一项（USB_DEVICE、FREERTOS 等）。注意：
中间件的**宿主外设要自己写**（例：USB_DEVICE 要同时给 `USB` 设 `Device`）。

**板级实例（BSP）**也算 `ips` 里的一项，但只在目标带 `board` 且该板有原生 BSP 定义时可用：

- `instance` 就是板名（例如 `NUCLEO-H723ZG`），`mode` 用原生叶模式内部名（例如 `COMMON`），
  `params` 用原生 Modes 里的组件参数（例如 `LD1`/`VCP`/`BUTTON`）。
- 工具会写 `Mcu.IP*`（BSP 在器件 IP 之后）、`<板名>.IPParameters`、`<板名>.<参数>`、
  模式虚拟脚 `<板名>_VS_BSP_COMMON`，以及 `BSP_IP_NAME`。
- 只写板卡标记（不声明 BSP 实例）时组件全关，和以前一样；BSP 组件展开成 MCU 配置
  （LED→GPIO、VCP→USART）由 CubeMX 加载时完成，不在本工具里手写。

**RTOS**：任务、队列、定时器、互斥量、信号量、事件组都支持（见 `rtos` 一节）。
任务表第一行是默认任务；没写时工具补一行并说明。事件组只在 `CMSIS_V2` 下支持。

## clock

两种表达，任选：

`params`：显式 RCC 参数 → 值（键名就是 .ioc 里的 RCC 参数名）。

`targets`：目标频率数组，每项二选一：

| 字段 | 说明 |
|---|---|
| `parameter` | 时钟输出名，例如 `HCLKFreq_Value` / `48MHZClocksFreq_Value` |
| `peripheral` | 或者按外设要频率，例如 `SPI1` |
| `mhz` | 目标频率（数字），必须 > 0 |

目标频率交给离线时钟求解器倒推上游参数。求解器按**路径形状**分派，不按家族：
有 PLL 的走 M/N 搜索（含小数 PLL），没有 PLL 的纯分频路径走区间剪枝。
求解器给的元件名会映射成 RCC 参数名；**有 `*Virtual` 变体时优先用 Virtual 变体**。

**先查拓扑再写参数**：目标解不出来，或要直接写 `params` 时，先运行
`probe clock --view upstream --output <读数>`。它是只读查询，给出全部上游路径、MUX 每个分支、
小数倍频的分数输入、每个元件的参数绑定。看懂了再写。

**只看求解结果**：`python -m cubemx1 clock solve --device <器件> --target <读数>=<MHz>`
只打印最终 RCC 参数、独立核算与每个目标的说明，不写文件、不启动 CubeMX；
要和编译用完全一致的联合搜索时就用它。

结果语义（都带在 `kind`/`reason` 里）：

| 状态 | 含义 |
|---|---|
| `solved` | 解出精确解。已经满足的目标返回空 steps（零写入），不是失败 |
| `exhausted` | 支持范围内搜完，没有精确解（含超上限） |
| `truncated` | 搜索被预算截断，**没有证明**解不出来 |
| `unsupported` | 这种结构没建模，不是"硬件不行" |
| `fixed_conflict` | 显式值或已解值冲突，报错会点名参数 |

小数 PLL（H5/H7）：`N + FRACN/div`，`div` 取自器件定义（缺省 8192）。整数解优先。
分辨率达不到时报 `exhausted`，理由写"分数分辨率"。别的 PLL 的 FRACN 不参与本次求解。

四条边界：

- **显式 `params` 优先**：同一个参数既写了显式值、又被目标频率算出差值时，报 `conflict`，不静默覆盖。
- **多目标一致**：多个 `targets` 联合求解。每个解带完整 settings（它依赖的全部元件值），
  后续目标按 settings 固定，不推翻已解结果；解不出来才回溯。目标顺序不影响结论。
- **最终独立核算**：全部目标解完后，工具用最终 RCC 参数沿时钟树**独立重算每个目标**，
  不是只信求解器自己算的数。重算不过报 `conflict`。
- **总线分频自动补**：给了 `HCLKFreq_Value` 目标时，工具按器件定义里声明的上限
  （例如 APB1 的 Max）取"满足上限的最小分频"，写进 `APB*Divider`。
  显式给了分频就按显式值写，越界只在说明里提示。没有 HCLK 目标时不自动补。

**实例自己需要的固定频率时钟**（例如 USB 全速要 48 MHz）由工具按器件定义里的
"这个读数被谁消费"关系找到读数名，不需要你在 `targets` 里重复写。

## nvic

| 字段 | 说明 |
|---|---|
| `priority_group` | 例如 `NVIC_PRIORITYGROUP_4`；用了 RTOS 会被强制成 4 |
| `vectors` | 数组：每项 `{irq, priority, subpriority?, enabled?, raw?}`；`raw`（v2）是反解拿到的原值，工具只改前 3 段（启用/抢占/子优先级），后面几段是 CubeMX 自己补的 |
| `flags` | v2：`NVIC` 的标量开关，键名 → 值（反解保留，原样写回） |

`irq` 用 CubeMX 的向量名（例如 `USART1_IRQn`）。
用了 RTOS 时，抢占优先级低于 `configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY`
（默认 5）的会被抬到那条下限——这是 CubeMX 的规则，编译输出里会写"钳了哪一个"。

## dma

| 字段 | 必填 | 说明 |
|---|---|---|
| `request` | 是 | DMA 请求名，例如 `USART1_TX` |
| `instance` | v1 必填；v2 `allocation.dma=auto` 时可省略 | 通道/流，例如 `DMA1_Channel4`。v1 里工具不替你挑；v2 自动挑时按 db 的模式树求解 |
| `params` | 否 | 覆盖固定参数；请求里只有一个候选的参数会自动取那一个 |


## rtos

只有目标里启用了 FreeRTOS（`ips` 里有 `FREERTOS` 且给了 `mode`）才能用这一段。
对象表**不是** `rtos.*` 键，它是 FREERTOS 的隐藏参数行；工具负责拼行、拼清单。

| 字段 | 说明 |
|---|---|
| `instance` | 默认 `FREERTOS` |
| `tasks` | 任务表；第一行是默认任务 |
| `queues` / `timers` / `mutexes` / `recursive_mutexes` / `binary_semaphores` / `counting_semaphores` | 各自的对象表 |
| `events` | 事件组；**只在 `CMSIS_V2` 下收**，其它 API 模式报 unsupported（CMSIS_V1 / 原生没有事件组） |

### 行字段

| 对象 | 必填 | 选填 |
|---|---|---|
| `tasks` | `name`、`priority`、`stack`、`entry` | `option`（默认 `Default`）、`parameters`（默认 `NULL`）、`allocation`（默认 `Dynamic`）、`buffer`、`control_block` |
| `queues` | `name`、`size`、`item_size` | `allocation`、`buffer`、`control_block`；**不要给第 4 列**（工具按 `item_size` 是否数字派生） |
| `timers` | `name`、`callback`、`type` | `option`、`parameters`、`allocation`、`control_block` |
| `mutexes` / `recursive_mutexes` / `binary_semaphores` | `name` | `allocation`、`control_block`、`state` |
| `counting_semaphores` | `name`、`max_count` | `initial_count`（默认 0）、`allocation`、`control_block` |
| `events` | `name` | `allocation`（默认 `Dynamic`）、`control_block`（`Static` 时默认 `<名字>ControlBlock`） |

### 规则（违反会报错，不静默改）

- 优先级：`CMSIS_V2` 只接受 **1 或 8..56**；**2..7 会被 CubeMX 归一成 24**，所以工具直接拒。
  `CMSIS_V1` 是 -3..3；原生 API 是 0..`configMAX_PRIORITIES-1`。也可以写 CMSIS_V2 的名字
  （`osPriorityNormal` 这类）。
- 栈：单位是**字**，必须 ≥ 这颗芯片的 `configMINIMAL_STACK_SIZE`（从定义里取，F1 是 64）。
- 任务表第一行必须是默认任务（名字 `defaultTask`）；不是的话工具在最前面补一行并说明。
- 名字要合法 C 标识符；同一类里不能重名；队列 `size` 必须 > 0；
  计数信号量要求 `max_count ≥ initial_count ≥ 0`。

### 例

```json
"rtos": {
  "tasks": [
    {"name": "defaultTask", "priority": 24, "stack": 128, "entry": "StartDefaultTask"},
    {"name": "ledTask", "priority": 8, "stack": 128, "entry": "StartLedTask"}
  ],
  "queues": [{"name": "ledQueue", "size": 8, "item_size": 4}]
}
```

落到 `.ioc` 里是两行：

```ini
FREERTOS.IPParameters=configTOTAL_HEAP_SIZE,Tasks01,Queues01
FREERTOS.Tasks01=defaultTask,24,128,StartDefaultTask,Default,NULL,Dynamic,NULL,NULL;ledTask,8,128,StartLedTask,Default,NULL,Dynamic,NULL,NULL
FREERTOS.Queues01=ledQueue,8,4,1,Dynamic,NULL,NULL
```

**清单必须列全**：加载只读 `FREERTOS.IPParameters` 里列出的参数名，
不列的整张表读不回来（实测：删掉清单里的 `Tasks01,Queues01`，另存后就少了这两行）。
工具是按参数字典自动拼清单的，不要手写。

## constants

用户常量：名字 → 值（都是字符串）。写进 `Mcu.UserConstants`，格式是 `名字,值`，多条用 `;`。
名字必须是合法的 C 标识符；写错就报错。

```json
"constants": {"BOARD_REV": "B", "APP_VERSION": "1.2.0"}
```

落到 `.ioc` 里是一行：

```ini
Mcu.UserConstants=BOARD_REV,B;APP_VERSION,1.2.0
```

这些常量会进生成代码（原生生成案例里核对过再次出现的值）。要确认具体落在哪个文件，生成后搜一下常量名。


## board（v2）

ST 官方板卡的默认配置来自安装目录里的板卡 seed（一份普通 `.ioc`）。

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `name@revision@vendor`，例如 `NUCLEO-F429ZI@Rev.B@STMicroelectronics`。`defaults` 必须完整 id；`materialized` 可以只写板名（`ioc import` 的产物就是板名——`.ioc` 里没有 revision/vendor，不编造） |
| `initialize` | 否 | `defaults`（默认）= 先反解 seed 再用你的显式段覆盖；`materialized` = 不重新套 seed，只保留板卡身份 |

套 seed 时写 `board=<板名>` 与 `boardIOC=true`，引脚、时钟、IP 都来自 seed；
你的显式 `pins` / `ips` / `clock` / `project` 覆盖在 seed 之上。
`device.refname` 可以不给（取板卡的 `mountedDevice`）；给了但与板卡不一致会报两处。

- 板卡 seed 里没有任何 `ProjectManager.*` 键，所以**工程选项不随板卡继承**。
- 板卡有原生 BSP 定义（`boards/bsp/<板名>_Modes.xml`）时，可以用 `ips` 声明板级实例和组件参数
  （见 `ips` 一节）；没有 BSP 定义的板卡仍能正常查、正常编，只是没有板级组件。
- `materialized` 用于"反解出来的真实工程"：不套 seed、不要求唯一 revision，保留已展开的配置。
- 板卡查询会给出 seed 的基础配置和 BSP 定义：`probe board --id <id>` 的 `config`/`bsp` 字段。

```json
"board": {"id": "NUCLEO-F429ZI@Rev.B@STMicroelectronics", "initialize": "defaults"},
"clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 168}]}
```

离线查板卡：`python -m cubemx1 probe boards --keyword NUCLEO`、
`python -m cubemx1 probe board --id NUCLEO-F429ZI@Rev.B@STMicroelectronics`。

## allocation 与 connections（v2）

`allocation` 控制"工具替你挑资源"：

| 字段 | 说明 |
|---|---|
| `pins` | `auto` = 允许工具分配引脚；不给或不写 = 只按你给的引脚做，缺的报错 |
| `dma` | `auto` = 允许工具按 db 的模式树挑通道/流（候选按"请求能接哪些通道"的声明过滤，多请求竞争时回溯求解） |
| `tie_break` | 多解时的取舍（当前实现：引脚按器件顺序，结果与书写顺序无关） |
| `release` | 点名可以重新分配的已占用资源 |

`connections` 是"只给信号、让工具挑引脚"的接线表：

| 字段 | 必填 | 说明 |
|---|---|---|
| `signals` | 是 | 信号名数组，例如 `["USART2_TX", "TIM2_CH3"]` |
| `endpoint_pins` | 否 | 限定候选引脚；只在这个集合里挑 |

规则：已给的固定引脚与板卡已有引脚**一个不动**；一引脚一信号；无解时给结构化冲突
（哪个信号、候选有哪些、被谁占着），不返回"差不多能用"的解。

```json
"allocation": {"pins": "auto"},
"connections": [{"signals": ["SPI2_SCK", "I2C2_SCL"], "endpoint_pins": ["PD3", "PF1"]}]
```

## extensions（v2）

反解 `.ioc` 时，模型里没有语义的键（例如 GUI 状态、旧版本残留）进 `extensions.ioc`：
键名 → 值，重建时**原样写回**，不静默丢。

- 与工具自己写的键重名 → 报 `conflict`（不做"最后一行赢"）。
- 这些键是否被 CubeMX 采纳不由我们保证；它们的存在是为了"反解再编译不丢东西"。

```json
"extensions": {"ioc": {"PCC.Checker": "false", "GPIO.groupedBy": "false"}}
```

## 增量修改（cubemx1.ioc-edit/1）

`ioc edit` 吃的不是完整目标，是"在已有目标上改几处"：

```json
{
  "contract": "cubemx1.ioc-edit/1",
  "set": {"clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 168}]}},
  "remove": {"pins": [{"pin": "PA9"}]}
}
```

- 按**稳定身份**合并：引脚=完整引脚名、IP=实例名、参数=实例+参数名、NVIC=IRQ、
  DMA=请求名（多路要显式实例）、RTOS=类别+名字、常量=名字。
- 省略的段 = 保持原值；**空数组也不表示清空**。
- 删不存在的对象 = 无操作；同一对象同时出现在 `set` 与 `remove`、或同一修改里身份重复 → 报冲突。
- 删 IP 但引脚还在用它的信号 → 关系冲突。
- 幂等：同一份修改连续应用两次，结果不变（`Apply(Apply(B,P),P) = Apply(B,P)`）。
## 错误语义

| kind | 含义 | 典型修法 |
|---|---|---|
| `target` | 目标 JSON 本身的形状不对（缺字段、多字段、类型错） | 按报错改字段 |
| `unknown` | 定义里查不到、目标也没写（实例不存在、引脚没这个信号、参数不属于当前模式、时钟解不出） | 按报错列的可用项改 |
| `unsupported` | 这个机制没实现或没建模（双核 / TrustZone / 内存映射；X-CUBE 包；非 CMSIS_V2 的事件组；未建模的时钟结构） | 别硬凑，找替代方案 |
| `conflict` | 目标两处冲突（同一根脚两个信号、时钟两处给值不一致） | 改目标 |

报错都会带 **JSON 路径**（例如 `ips.USART1.params`），直接指到你写的那一行。

## 完整示例：F103 + USART + 36 MHz + 中断

```json
{
  "contract": "cubemx1.ioc/1",
  "device": {"refname": "STM32F103C(8-B)Tx"},
  "project": {"name": "demo", "output_dir": "D:/projects/demo", "toolchain": "CMake"},
  "pins": [
    {"pin": "PA9",  "signal": "USART1_TX"},
    {"pin": "PA10", "signal": "USART1_RX"}
  ],
  "ips": [
    {"instance": "USART1", "mode": "Asynchronous",
     "params": {"BaudRate": "115200"}}
  ],
  "clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 36}]},
  "nvic": {"vectors": [{"irq": "USART1_IRQn", "priority": 1}]}
}
```

## 完整示例：DMA + RTOS

```json
{
  "contract": "cubemx1.ioc/1",
  "device": {"refname": "STM32F103C(8-B)Tx"},
  "project": {"name": "rtos_dma", "output_dir": "D:/projects/rtos_dma"},
  "pins": [
    {"pin": "PA9",  "signal": "USART1_TX"},
    {"pin": "PA10", "signal": "USART1_RX"}
  ],
  "ips": [
    {"instance": "USART1", "mode": "Asynchronous", "params": {"BaudRate": "115200"}},
    {"instance": "FREERTOS", "mode": "CMSIS_V2",
     "params": {"configTOTAL_HEAP_SIZE": "24576"}}
  ],
  "nvic": {"vectors": [{"irq": "USART1_IRQn", "priority": 1}]},
  "dma": [{"request": "USART1_TX", "instance": "DMA1_Channel4"}]
}
```

## 编译出来的东西长什么样

工具写一份 canonical `.ioc`：清单（`Mcu.IPNb/IP<i>`、`Mcu.PinsNb/Pin<i>`、
`<IP>.IPParameters`、`<pin>.GPIOParameters`）+ 各键值 + 工程设置。

只写**和默认值不同**的键（CubeMX 保存时也这样），所以"另存后某个键不在"不等于丢——
先确认它是不是等于默认值。

加载后 CubeMX 会补一些它自己算的键（例如 `VirtualMode`、`rtos.<序号>.ip`、
各种 `*Freq_Value`）。这些不用写。

## 支持范围与边界

已支持：引脚/GPIO、USART、TIM、SPI、I2C、ADC、DMA、时钟、NVIC、FreeRTOS 标量参数、
RTOS 对象表与事件组（事件组限 CMSIS_V2）、USB 设备类（要宿主 + 48 MHz）。

不支持（会明确报错）：双核/TZ/内存映射、X-CUBE 包。
自动引脚与自动 DMA 通道（v2 `allocation`）都支持：DMA 候选来自 db 的模式树
（控制器 → 通道/流 → 请求），多请求竞争时回溯求解；显式给 `instance` 时同样按这张表校核。
RTOS 事件组只在 `CMSIS_V2` 下支持；换成 CMSIS_V1/原生 API 会直接报 unsupported。
其它中间件（LWIP / FATFS / USB HOST）机制相同，均做过原生加载核对；更细的组合使用前先自测。

家族边界：F1/F4/H7/C0 做过原生加载验收（抽样矩阵），L4 只到 L0/L1；工具链只验过 CMake。
时钟目标四个家族都覆盖：C0 无 PLL 走纯分频路径，H5/H7 支持小数 PLL。
其它家族可能参数名、时钟输出名不同（例：F429 时钟树里的 USB 读数名是
`48MHZClocksFreq_Value`，F1 用的是另一个名字——工具按定义里的归属关系找，不写死名字）。

## 入口
同一个编译器有两个入口，参数语义相同：

| 入口 | 用法 |
|---|---|
| 命令行 | `python -m cubemx1 ioc build <目标.json> [-o 输出.ioc]` |
| | `python -m cubemx1 ioc import <工程.ioc> [-o 目标.json]`（反解，含未知键） |
| | `python -m cubemx1 ioc edit <基础目标.json> <修改.json> [-o 输出.json]`（增量修改） |
| | `python -m cubemx1 ioc check <工程.ioc>`（**开发/验收用**：加载 + 另存 + 比键值） |
| | `python -m cubemx1 ioc generate <工程.ioc>`（原生生成 + 产物检查） |
| | `python -m cubemx1 probe describe --ip <实例> [--device <器件>] [--board <板卡id或板名>]`（查 IP 定义与适用定义；给 board 后可查板级实例） |
| | `python -m cubemx1 probe boards [--keyword K] [--limit N]`、`probe board --id <板卡 id>` |
| | `python -m cubemx1 probe clock --device <器件> [--view graph|upstream|raw] [--output <读数>]`（只读时钟拓扑，不求解） |
| MCP | `cubemx1_ioc_build` / `cubemx1_ioc_import` / `cubemx1_ioc_check` / `cubemx1_ioc_generate` / `cubemx1_probe_describe`（时钟拓扑用 `kind=clock`）/ `cubemx1_status` |

`ioc check` **进发布包，但只用于开发/验收**；正常使用只需要 `build` → `generate`。

对拍会单独列出两类"CubeMX 自己也会产生"的规范化：板卡 seed 的旧 `PCC.*`、
`PLLSourceVirtual` 与 `PLLSource` 二选一（值相同）。它们不算问题，其它丢键/改值仍然算。

## 完整示例：v2 板卡 + 自动引脚 + USB

```json
{
  "contract": "cubemx1.ioc/2",
  "board": {"id": "NUCLEO-F429ZI@Rev.B@STMicroelectronics", "initialize": "defaults"},
  "project": {"name": "board_demo", "output_dir": "D:/projects/board_demo",
              "options": {"KeepUserCode": true}},
  "allocation": {"pins": "auto"},
  "connections": [{"signals": ["SPI2_SCK", "I2C2_SCL"], "endpoint_pins": ["PD3", "PF1"]}],
  "ips": [{"instance": "USB_OTG_FS", "mode": "Device_Only", "params": {}}],
  "pins": [{"pin": "PA11", "signal": "USB_OTG_FS_DM"},
           {"pin": "PA12", "signal": "USB_OTG_FS_DP"}],
  "clock": {"targets": [{"parameter": "HCLKFreq_Value", "mhz": 168}]}
}
```

编译结果是"板卡默认配置 + 上面这些改动"：引脚与时钟继承板卡，
`SPI2_SCK`/`I2C2_SCL` 只落在给定候选里，USB 需要的 48 MHz 由时钟求解器一起满足。
