# STM32CubeMX Config Skill & MCP

[中文](README.md) ｜ **English**

An STM32CubeMX configuration toolkit for AI coding assistants. The assistant can query a device's definitions and
parameter ranges, write one JSON describing the target configuration, and let the tool compile it
**offline** into an `.ioc` that STM32CubeMX 1.x loads directly. When code is needed, CubeMX generates
the project.

This toolkit targets **STM32CubeMX 1**. STM32CubeMX2 uses a new software design and ships reliable CLI
commands, so it can be used directly; see ST's
[STM32Cube for Agentic AI](https://community.st.com/stm32cube-for-agentic-ai-186) tool stack for details.

> This English text is a translation snapshot, synced at release time. The Chinese version
> ([README.md](README.md)) is authoritative.

## Why not drive the CubeMX CLI directly

The CubeMX batch-script mechanism has several problems. They do not scale with project size.

- **Slow**: each batch that starts a new CubeMX process incurs a startup cost of ten-odd seconds.
- **Commands depend on state, so order becomes a dependency**: the work of a command is decided by the
  state the previous command changed. That dependency chain is hard to model explicitly.
- **Failures do not fail**: some commands fail while the process still exits with code 0.
- **Configuration is silently dropped**: when saving `.ioc`, CubeMX rewrites the whole file from its
  in-memory model and drops keys it does not recognise.

**Case: the same parameter write fails until the step before it is done.**

```text
set ip parameters USART1 BaudRate 115200
set mode USART1 Asynchronous
set ip parameters USART1 BaudRate 115200
```

The first line fails: USART1 has no working mode yet, so BaudRate is not in its parameter table.
The second line selects the mode; the third line then writes the same parameter with the same value.
A first-time user reads the failure as "the command is wrong" when the problem is the order.

This toolkit takes a different route: the target state goes into JSON. The tool computes the keys
offline and compiles the `.ioc` in one step. CubeMX only loads that file and generates code.

## What is in the package

```text
cubemx1.cmd / .sh   launcher (Windows / Linux, macOS, Git Bash)
skills/             instructions for AI assistants (SKILL.md + references/)
mcp/                the program: offline compiler + query handler + MCP shell
docs/PROTOCOL.md    target JSON protocol (how to write that JSON)
README.md / LICENSE
```

## Requirements

- Python 3.8 or newer (standard library only, nothing to pip install);
- STM32CubeMX 1.x installed locally;
- One machine configuration per computer, `~/.cubemx1/machine.json`:

```json
{
  "cubemx_home": "<CubeMX install directory>",
  "repository": "<firmware package repository>"
}
```

`repository` is only needed to generate code. A missing value is reported explicitly; paths are
never guessed.

## Quick start

Probe first, write second, compile last. Copy parameter names, pins and values from what the probe prints,
instead of typing them from memory.

### 1. See which peripherals the chip has

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

The `instance` column is exactly what goes into `ips[].instance` later.

### 2. Look up the peripheral: parameters, values, candidate pins

```bash
cubemx1 probe describe --ip USART1 --device "STM32F103C(8-B)Tx"
```

```text
instance USART1 (USART)
type     peripheral
source   <CubeMX install dir>/db/mcu/IP/USART-sci2_v1_1_Cube_Modes.xml
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

The `source` line points at this machine's CubeMX install; it is replaced with a placeholder here.
For parameters, `integer` means check `range`, `list` means pick one of `values`,
and `not applicable` means the current mode does not use that definition.

Next, the pins:

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

USART1 in asynchronous mode needs TX and RX, and each signal has two candidate pins, so `pins[]`
has to make the choice. The chosen pins look like this:

### 3. Write the JSON

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

### 4. Compile

```bash
cubemx1 ioc build target.json
```

No CubeMX starts here; it takes seconds. The output tells you which `.ioc` was written, how many keys
it holds, what was filled in as defaults, and what was clamped.

### 5. Generate the project

Needs CubeMX and the matching firmware package on this machine:

```bash
cubemx1 ioc generate project.ioc
```

It runs a real generation, then checks that the startup file exists, that the project references it,
and that the key values made it into the code.

More complete cases (peripheral inventory and probing, clocks, boards, DMA, RTOS, editing an existing
project, clock topology probing):
[workflows](skills/stm32cubemx1-config/references/workflows.md).

## Inspect the clock tree before writing clock parameters

If a target frequency does not solve, or you want to write RCC parameters directly, inspect the
topology first. The query handler is read-only: it solves nothing and starts no CubeMX.

```bash
cubemx1 probe clock --device "STM32H750VBTx" --view upstream --output HCLKFreq_Value
```

The answer carries every element's type and parameter binding, all upstream paths, every MUX branch,
and the fractional input of a fractional multiplier. Write `clock.params` after reading it.
On MCP, use `cubemx1_probe_describe` with `kind=clock`.

The solver can also run on its own and print the result without writing a file:

```bash
cubemx1 clock solve --device "STM32F407Z(E-G)Tx" --target HCLKFreq_Value=168
```

It prints the final RCC parameters, a check recomputed from those parameters, and a note per target.
Write them into the target JSON only after confirming them.

## For AI assistants (MCP)

`mcp/mcp.json` is the MCP packaging manifest. The tool surface is six tools:

| Tool                       | What it does                                                                                                                                                                                                         |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cubemx1_ioc_build`      | Target JSON → `.ioc` (offline, no CubeMX)                                                                                                                                                                         |
| `cubemx1_ioc_check`      | Dev/acceptance comparison: load + save-as + compare keys (**not needed for release use**)                                                                                                                      |
| `cubemx1_ioc_generate`   | Native CMake generation plus artifact checks                                                                                                                                                                         |
| `cubemx1_ioc_import`     | Dev/acceptance: reverse a `.ioc` into target JSON (editing)                                                                                                                                                        |
| `cubemx1_probe_describe` | Offline queries: definitions (`kind=describe`), device peripheral inventory (`kind=peripherals`), signals and candidate pins (`kind=pins`), ST boards (`kind=boards/board`), clock topology (`kind=clock`) |
| `cubemx1_status`         | Background process and session status                                                                                                                                                                                |

## Supported Configuration Scope

Thanks to its XML-driven architecture, the toolkit can discover, configure, and edit any IP defined in the STM32CubeMX XML database, without relying on a hardcoded peripheral whitelist.

The following configuration areas have been tested across multiple MCUs from multiple STM32 families:

- Pins and GPIO
- USART, TIM, SPI, I2C, ADC, FMC
- DMA, clock configuration, and NVIC
- FreeRTOS
- USB device classes
- User constants
- Middleware: LWIP, FATFS, and USB HOST

### Board Support

The `board` section supports both official ST board configuration seeds and previously expanded configurations.

`probe board` can inspect a board to determine:

- Which MCU it uses
- What is configured in its seed
- Whether a native BSP definition is available

For boards with BSP support, the `ips` section can also specify board-level instances and component parameters, such as `LD1`, `VCP`, and `BUTTON`.

STM32CubeMX expands these board-level components into MCU configurations when loading the generated `.ioc` file.

### Coming Soon

- dual core / TrustZone / memory map
- X-CUBE expansion packages
- BSP-based registration of custom board information
- BSP-based custom drivers for external modules (peripherals and code initialization)


## License

See [LICENSE](LICENSE).
