# STM32CubeMX Config Skill & MCP

**中文** ｜ [English](README.en.md)

给 AI 编程助手用的 STM32CubeMX 配置工具包。助手可以查询器件的定义与参数可行域，写一份描述目标配置的
JSON，由工具**离线**编译成 STM32CubeMX 1.x 能直接加载的 `.ioc`；需要代码时，再让 CubeMX 生成工程。

本工具集面向 **STM32CubeMX 1**。STM32CubeMX2 使用新的软件编排，
并提供可靠的 CLI 指令，可以直接使用；可以前往 ST 的 [STM32Cube for Agentic AI](https://community.st.com/stm32cube-for-agentic-ai-186) 工具栈了解。

## 为什么不采用直接编排 CubeMX CLI 指令的方案

CubeMX 的批处理脚本机制有几个问题。工程一大就压不住。

- **慢**：每次重新启动 CubeMX 的批处理都有启动开销，十几秒起步。
- **命令之间有状态依赖，顺序也成了依赖**：后一条命令的工作由前一条命令改变的状态决定。
  这条依赖链很难显式建模。
- **失败不报失败**：有些命令失败了，进程退出码仍是 0。
- **会悄悄丢配置**：保存 `.ioc` 时，CubeMX 按内存模型重写整个文件，不认识的键直接丢。

**案例：同一个参数，前一步没做，这一步就写不进去。**

```text
set ip parameters USART1 BaudRate 115200
set mode USART1 Asynchronous
set ip parameters USART1 BaudRate 115200
```

第一条直接运行会失败：USART1 还没选工作模式，这时候它的参数表里没有 BaudRate 这一项。而CLI不会在设置ip参数时自动启动UART1的模式。
第二条把模式定下来，第三条同样的参数、同样的值就写进去了。
第一次用的人很容易以为"命令写错了"，其实错的是顺序。

本工具换一个做法：配置的"目标状态"全部写进 JSON。工具离线算出该写哪些键，一次编出 `.ioc`。
CubeMX CLI只做两件事：加载这份文件、生成代码。

## 包里有什么

```text
cubemx1.cmd / .sh   启动器（Windows / Linux、macOS、Git Bash）
skills/             给 AI 助手读的说明书（SKILL.md + references/）
mcp/                程序：离线编译器 + 查询器 + MCP 外壳
docs/PROTOCOL.md    目标 JSON 协议（怎么写那份 JSON）
README.md / LICENSE
```

## 装之前需要什么

- Python 3.8 以上（只用标准库，不用 pip 装东西）；
- 本机已安装 STM32CubeMX 1.x；
- 一台电脑一份的本机配置 `~/.cubemx1/machine.json`：

```json
{
  "cubemx_home": "<CubeMX 安装目录>",
  "repository": "<固件包仓库目录>"
}
```

`repository` 只在生成代码时需要。缺配置时会直接报"该填什么"，不会猜路径。

## 快速开始

先查，再写，再编译。参数名、引脚、取值都从探测结果里抄，别凭记忆写。

### 1. 查这颗芯片有哪些外设

```bash
cubemx1 probe peripherals --device "STM32F103C(8-B)Tx"
```

```text
device     STM32F103C(8-B)Tx
peripherals 27
  ADC1                   ADC-aditf_v2_5F1_Cube peripheral
  ADC2                   ADC-aditf_v2_5F1_Cube peripheral
  CAN                    CAN-bxcan1_v1_1_F1_Cube peripheral
  CRC                    CRC-integtest1_v1_0 service
  DMA                    DMA-STM32F103G_dma_v1_0 service
  FATFS                  FATFS-v0.1.0_Cube middleware
  FREERTOS               FREERTOS-v8.0.0_Cube middleware
  GPIO                   GPIO-STM32F103x8_gpio_v1_0 service
  I2C1                   I2C-i2c1_v1_5_Cube peripheral
  I2C2                   I2C-i2c1_v1_5_Cube peripheral
  IWDG                   IWDG-iwdg1_v1_1 service
  NVIC                   NVIC-STM32F103G service
  RCC                    RCC-STM32F102_rcc_v1_0 peripheral
  RTC                    RTC-rtc1_v1_1_Cube peripheral
  SPI1                   SPI-spi2_v1_4_Cube peripheral
  SPI2                   SPI-spi2_v1_4_Cube peripheral
  SYS                    SYS-STM32F10x_sys_v1_0 peripheral
  TIM1                   TIM1_8F1-gptimer2_v1_x_Cube peripheral
  TIM2                   TIM1_8F1-gptimer2_v1_x_Cube peripheral
  TIM3                   TIM1_8F1-gptimer2_v1_x_Cube peripheral
  TIM4                   TIM1_8F1-gptimer2_v1_x_Cube peripheral
  USART1                 USART-sci2_v1_1_Cube peripheral
  USART2                 USART-sci2_v1_1_Cube peripheral
  USART3                 USART-sci2_v1_1_Cube peripheral
  USB                    USB-usb1_v1_1_Cube peripheral
  USB_DEVICE             USB_DEVICE-v2.0_Cube middleware
  WWDG                   WWDG-wwdg1_v1_0 service
```

`instance` 一列就是 JSON 里 `ips[].instance` 要写的东西。

### 2. 查要用的外设：参数、取值、候选引脚

```bash
cubemx1 probe describe --ip USART1 --device "STM32F103C(8-B)Tx"
```

```text
instance USART1 (USART)
type     peripheral
source   <CubeMX 安装目录>/db/mcu/IP/USART-sci2_v1_1_Cube_Modes.xml
modes    enabled []; possible []
param    Instance                     ?        default USART$Index type ?
param    VirtualMode                  list     default VM_ASYNC   values VM_ASYNC, VM_SYNC, VM_IRDA, VM_SMARTCARD
param    VirtualClockMode             list     default VM_SYNC    values VM_SYNC, VM_SMARTCARD
param    BaudRate                     integer  default 115200     range 110..10500000
param    WordLength                   list     default WORDLENGTH_8B values WORDLENGTH_8B, WORDLENGTH_9B
param    UsartWordLength              list     default USART_WORDLENGTH_8B values USART_WORDLENGTH_8B
param    IrdaWordLength               list     default IRDA_WORDLENGTH_8B values IRDA_WORDLENGTH_8B
param    SmartCardWordLength          list     default SMARTCARD_WORDLENGTH_9B values SMARTCARD_WORDLENGTH_9B
param    UartWordLength               list     default UART_WORDLENGTH_8B values UART_WORDLENGTH_8B
param    StopBits                     list     default STOPBITS_1 values STOPBITS_1, STOPBITS_2, STOPBITS_0_5, STOPBITS_1_5
param    UsartStopBits                list     default USART_STOPBITS_2 values USART_STOPBITS_2
param    UartStopBits                 list     default UART_STOPBITS_1 values UART_STOPBITS_1
param    SmartCardStopBits            list     default SMARTCARD_STOPBITS_1_5 values SMARTCARD_STOPBITS_1_5
param    Parity                       list     default PARITY_EVEN values PARITY_EVEN, PARITY_ODD
param    UsartParity  not applicable
param    IrdaParity  not applicable
param    SmartCardParity              list     default SMARTCARD_PARITY_EVEN type list
param    UartParity  not applicable
param    Mode                         list     default MODE_TX_RX values MODE_TX_RX, MODE_RX, MODE_TX
param    UsartMode  not applicable
param    SmartCardMode  not applicable
param    IrdaMode  not applicable
param    UartMode  not applicable
param    HwFlowCtl                    list     default UART_HWCONTROL_NONE values UART_HWCONTROL_NONE
param    OverSampling                 list     default null       values null
param    CLKPolarity                  list     default SMARTCARD_POLARITY_LOW values SMARTCARD_POLARITY_LOW, SMARTCARD_POLARITY_HIGH
param    CLKPhase                     list     default SMARTCARD_PHASE_1EDGE values SMARTCARD_PHASE_1EDGE, SMARTCARD_PHASE_2EDGE
param    CLKLastBit                   list     default SMARTCARD_LASTBIT_DISABLE values SMARTCARD_LASTBIT_DISABLE, SMARTCARD_LASTBIT_ENABLE
param    IrDAMode                     list     default IRDA_POWERMODE_NORMAL values IRDA_POWERMODE_LOWPOWER, IRDA_POWERMODE_NORMAL
param    Prescaler                    integer  default 1          range 1..1
param    CalculateSpeed               string   default 5000000    type string
param    NACKState                    list     default SMARTCARD_NACK_DISABLE values SMARTCARD_NACK_DISABLE, SMARTCARD_NACK_ENABLE
param    GuardTime                    integer  default 0          range 0..255
param    WakeUpMethode                list     default UART_WAKEUPMETHOD_IDLELINE values UART_WAKEUPMETHOD_IDLELINE, UART_WAKEUPMETHOD_ADDRESSMARK
param    Address                      integer  default 0          range 0..15
param    BreakDetectLength            list     default UART_LINBREAKDETECTLENGTH_10B values UART_LINBREAKDETECTLENGTH_10B, UART_LINBREAKDETECTLENGTH_11B
```

`source` 那行本来是这台机器的安装路径，这里换成了占位符。`integer` 看 `range`，
`list` 看 `values`；写 `not applicable` 说明当前模式用不到这条。

再看引脚：

```bash
cubemx1 probe pins --ip USART1 --device "STM32F103C(8-B)Tx"
```

```text
device     STM32F103C(8-B)Tx
instance   USART1 (USART)
pins       7  signals 5
one pin, many signals 0
signal, many pins 2
  USART1_RX              PA10, PB7
  USART1_TX              PA9, PB6
modes      11
  Asynchronous                   required 2   ambiguous 2   conflicts 0   missing 0
  Synchronous                    required 3   ambiguous 2   conflicts 0   missing 0
  Half_duplex(single_wire_mode)  required 1   ambiguous 1   conflicts 0   missing 0
  Multiprocessor_communication   required 2   ambiguous 2   conflicts 0   missing 0
  IrDA                           required 2   ambiguous 2   conflicts 0   missing 0
  LIN                            required 2   ambiguous 2   conflicts 0   missing 0
  SmartCard                      required 1   ambiguous 1   conflicts 0   missing 0
  SmartCard_With_Clock           required 2   ambiguous 1   conflicts 0   missing 0
  CTS_Only                       required 1   ambiguous 0   conflicts 0   missing 0
  RTS_Only                       required 1   ambiguous 0   conflicts 0   missing 0
  CTS_RTS                        required 2   ambiguous 0   conflicts 0   missing 0
```

USART1 异步要 TX 和 RX 两根脚，两个信号都各有两根候选脚，所以 `pins[]` 里得自己挑好。
挑完是这样：

### 3. 写 JSON

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

### 4. 编译

```bash
cubemx1 ioc build target.json
```

这一步不启动 CubeMX，几秒钟。输出里会写清楚编出了哪个 `.ioc`、一共多少键、
补了哪些默认值、钳了哪些值。

### 5. 生成工程

需要本机装着 CubeMX 和对应家族的固件包：

```bash
cubemx1 ioc generate 工程.ioc
```

它会真的跑一次生成，然后检查启动文件在不在、工程有没有引用它、关键数值有没有进代码。

更多完整案例（外设清单与探测、时钟、板卡、DMA、RTOS、改现有工程、时钟拓扑探测）见
[工作流程](skills/stm32cubemx1-config/references/workflows.md)。

## 先看清时钟树，再写时钟参数

目标频率解不出来，或要在 JSON 里直接写 RCC 参数时，先查拓扑。查询器只读：不求解频率，不启动 CubeMX。

```bash
cubemx1 probe clock --device "STM32H750VBTx" --view upstream --output HCLKFreq_Value
```

返回里有：每个元件的类型与参数绑定、全部上游路径、MUX 的每个分支、小数倍频的分数输入。
看懂了再写 `clock.params`。MCP 里用 `cubemx1_probe_describe` 的 `kind=clock`。

求解器也能单独跑，只打印结果、不写文件：

```bash
cubemx1 clock solve --device "STM32F407Z(E-G)Tx" --target HCLKFreq_Value=168
```

输出最终 RCC 参数、用这些参数独立重算的校验值，以及每个目标的求解说明。确认后再写进目标 JSON。

## 给 AI 助手用（MCP）

`mcp/mcp.json` 是 MCP 服务的打包清单。工具面是六条：

| 工具                       | 干什么                                                                                                                                                                           |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cubemx1_ioc_build`      | 目标 JSON → `.ioc`（离线，不启动 CubeMX）                                                                                                                                     |
| `cubemx1_ioc_check`      | 开发/验收用对拍：加载 + 另存 + 比键值（**发布使用不需要**）                                                                                                                |
| `cubemx1_ioc_generate`   | 原生生成 CMake 工程并检查产物                                                                                                                                                    |
| `cubemx1_ioc_import`     | 开发/验收用：把现有 `.ioc` 反解成目标 JSON（改工程用）                                                                                                                         |
| `cubemx1_probe_describe` | 离线查询：IP 定义/适用定义（`kind=describe`）、外设清单（`kind=peripherals`）、信号↔候选引脚（`kind=pins`）、ST 板卡（`kind=boards/board`）、时钟拓扑（`kind=clock`） |
| `cubemx1_status`         | 看后台进程与会话状态                                                                                                                                                             |

## 支持范围

外设支持不看名单，看 CubeMX 的器件数据：器件 XML 和 Modes XML 里有的外设，工具就能查、能写，
引脚、模式、参数、中断、DMA、时钟都是同一套规则。下面这些是已经用 CubeMX 真机加载验证过的代表，
别的外设照样能写：

引脚与 GPIO、USART、TIM、SPI、I2C、ADC、DMA、时钟、NVIC、FreeRTOS、USB 设备类、
用户常量、LWIP / FATFS / USB HOST。

板卡方面，`board` 段可以直接套 ST 官方板卡的 seed，也可以用已经展开的配置；
`probe board` 能查板卡用的哪颗 MCU、seed 里配了什么、有没有原生 BSP 定义。
板卡带 BSP 时，`ips` 里可以写板级实例和组件参数（比如 `LD1`、`VCP`、`BUTTON`），
这些组件由 CubeMX 加载时展开成 MCU 配置。

还没做的，现在会明确报错，不会悄悄忽略（coming soon）：

- 双核 / TrustZone / 内存映射
- X-CUBE 扩展包
- 基于 BSP 的自定义板卡信息注册
- 基于 BSP 的自定义驱动外部模块（外设与代码初始化）

F1/F4/H7/C0 做过原生加载验收，L4 只验了一部分；四个家族的时钟目标都覆盖（C0 没有 PLL，走纯分频；
H5/H7 支持小数 PLL）；时钟拓扑查询是只读的，有时钟定义的器件都能查。工具链只验过 CMake。
发布路径不做对拍，正确性靠规则和 CI 门禁。

## 许可

见 [LICENSE](LICENSE)。
