---
name: stm32cubemx1-config
description: 用 cubemx1 把一份描述目标配置的 JSON 离线编译成 STM32CubeMX 1.x 能直接加载的 .ioc（不启动 CubeMX、不跑命令序列），需要时再让 CubeMX 生成 CMake 工程；也可以只读查询器件定义、板卡与时钟拓扑。Use when：新建/修改 STM32CubeMX 1.x 工程、要写或改 .ioc、按官方板卡（Nucleo/Discovery）建工程、配置引脚/时钟/DMA/NVIC/FreeRTOS/USB/中间件、要查时钟树拓扑或某个频率的上游，或用户提到 cubemx1、离线 IOC 编译器、目标 JSON、probe clock。不适用于 STM32CubeMX2 的 .ioc2 工程。
---

# 离线 IOC 编译器（STM32CubeMX 1.x）

一句话：**写一份 JSON 说清"要什么配置" → 一条命令编出 `.ioc` → 需要代码时让 CubeMX 生成。**
CubeMX 只做两件事：加载编出来的 `.ioc`、生成工程。它不再是"逐条命令的配置台"。

## 默认路径（80% 的情况，读完这一节就够）

1. **写目标 JSON**：器件或板卡、引脚与信号、外设模式与参数、时钟、中断、DMA、中间件、工程设置。
   字段表见 [目标 JSON 协议](../../docs/PROTOCOL.md)；在 MCP 客户端里读资源
   `cubemx1://protocol/target`。外设清单、参数名/取值、引脚候选不确定时，先按 B 段用 `probe peripherals` / `probe describe` / `probe pins` 查准，不要猜。
2. **编译**（离线、秒级、不启动 CubeMX）：

   ```bash
   python -m cubemx1 ioc build target.json
   ```

   成功会打印 `.ioc` 路径、键数和"补了哪些默认"。
   只看求解结果、不写盘：`python -m cubemx1 clock solve --device <器件> --target <读数>=<MHz>`
   （只读，见[命令参考](references/commands.md)第三节）。
3. **生成**（需要 CubeMX 与对应固件包）：

   ```bash
   python -m cubemx1 ioc generate 工程.ioc
   ```

   它跑一次原生生成，并检查：启动文件在不在、工程有没有引用它、关键数值有没有进生成的代码。

用 MCP 时对应 `cubemx1_ioc_build` / `cubemx1_ioc_generate`。
`cubemx1_ioc_check` 是**开发/验收用的对拍工具**（加载 + 另存 + 比键值），不是发布使用的必需步骤。

## 渐进式路由（按处境只读一份，不要一次读所有 references）

| 你现在的处境 | 读这份 | 什么时候**不需要**读 |
|---|---|---|
| 第一次建工程、要照抄完整案例、按板卡/BSP 建、配 DMA 或 RTOS | [工作流程](references/workflows.md) 的 A / E / F / G 段 | 已经有现成目标 JSON、只想编译 |
| 写 JSON 前要查外设清单、参数名/类型/取值、引脚候选，或名字不完全匹配 | [工作流程](references/workflows.md) 的 B 段 | 外设、参数和引脚都已经确认 |
| 要配时钟目标（多目标、无 PLL 的 C0、H7 小数 PLL） | [工作流程](references/workflows.md) 的 C 段 | 时钟已经写进目标、只关心编译 |
| 要先查时钟拓扑、看某个频率的上游（只读，不求解） | [工作流程](references/workflows.md) 的 D 段；命令见[命令参考](references/commands.md)第五节 | 时钟目标已解出、只关心结果 |
| 要查命令、参数、退出码、`probe` 用法 | [命令参考](references/commands.md) | 只跑上面三步、不查细节 |
| 要单独跑时钟求解器、看解出的参数和理由 | [命令参考](references/commands.md) 第三节 | 目标 JSON 已写好、只想编译 |
| 有报错、生成结果不对、加载异常、时钟解不出来 | [行为手册](references/handbook.md) | 一切正常时 |
| 写目标 JSON 的字段/边界 | [目标 JSON 协议](../../docs/PROTOCOL.md)；**在 MCP 里**读资源 `cubemx1://protocol/target` | 已有可用 JSON 模板 |
| 要 MCP 的工具/传输契约 | 资源 `cubemx1://protocol/mcp`（或 [mcp/PROTOCOL.md](../../mcp/PROTOCOL.md)） | 只用命令行 |

路由规则：**先跑默认路径；只有对应处境出现时才加载那一份**。references 之间不要求顺序阅读。

## 硬边界（必须先知道）

- **不许猜**：XML 或 CubeMX 行为解释不了的字段，编译期报 unsupported / unknown / conflict，不静默忽略。
- **看结果，不看口头成功**：build 返回码只说明编完了；生成要 `generate` 的产物检查。
- **版本固定**：CubeMX 6.17.0-RC5 / DB 6.0.170 / 文件版本 6。换版本必须重验。
- **生成成功 ≠ 编译通过 ≠ 上板跑通**：生成检查只覆盖"文件在、值在"。
- **Coming soon**（当前会明确报错，不会静默忽略）：双核 / TrustZone / 内存映射；X-CUBE 扩展包；
  基于 BSP 的自定义板卡信息注册；基于 BSP 的自定义驱动外部模块（外设与代码初始化）。

## 支持范围

**不是外设白名单**：编译器按 db 的 IP 定义、模式树和参数表通用处理。器件 XML 声明了、Modes XML
有定义的外设都能查、能写（引脚/模式/参数/中断/DMA/时钟）。下面列的是已过原生验收的代表，不是上限：

引脚/GPIO、USART、TIM、SPI、I2C、ADC、DMA（可自动挑通道）、时钟（目标频率或显式 RCC 参数）、
NVIC、FreeRTOS（含对象表）、USB 设备类（如 CDC）、用户常量，以及 LWIP / FATFS / USB HOST 等中间件。
没逐项原生验，不等于不支持。

官方板卡：用 `board` 段选板（`defaults` 套 seed / `materialized` 保留已展开配置）。
有 BSP 定义的板卡可以在 `ips` 里声明板级实例与组件参数（如 `LD1` / `VCP`），
工具写清单、参数、虚拟脚。板载驱动装配不归本工具。

家族：F1/F4/H7/C0 做过原生加载验收（抽样矩阵），L4 只到 L0/L1。工具链只验过 CMake。

时钟目标按**路径形状**分派，不按家族：有 PLL 的走 M/N 搜索（含小数 PLL：`N + FRACN/分频`，
分频来自器件定义，H5/H7 默认 8192）；没有 PLL 的纯分频路径（如 C0）从同一份 db 的分频器取值解出来；
已经满足的目标零写入。多目标联合求解后会用**最终 RCC 参数独立重算每个目标**，
不是只信求解器自己算的数。

解不出来时区分状态：`exhausted`（支持范围内搜完）、`truncated`（预算截断，未证明）、
`unsupported`（结构没建模）、`fixed_conflict`（显式值冲突，会点名）。
不把它们说成"硬件不行"。

时钟求解有两个只读入口：`clock solve` 打印最终 RCC 参数、独立核算与每个目标的说明（不写盘）；
`probe clock`（MCP `kind=clock`）给原始属性、连接、端口角色、参数绑定与上游路径。
两者都与编译器共用同一份模型/求解器，都不启动 CubeMX。

外设探测也是只读：`probe peripherals`（MCP `kind=peripherals`）列器件声明的全部实例，
`probe describe`（`kind=describe`）给参数/取值/适用定义，`probe pins`（`kind=pins`）给信号↔候选脚
与某个模式需要的信号。数据来自器件 XML 与 Modes XML，不是写死的表。

## 命令面

发布路径只有两条：

```text
python -m cubemx1 ioc build    <目标.json> [-o 输出.ioc]
python -m cubemx1 ioc generate <工程.ioc>
```

辅助命令（只读，不写盘）：

```text
python -m cubemx1 clock solve --device <器件> --target <读数>=<MHz>
python -m cubemx1 probe clock --device <器件> --view upstream --output <读数>
python -m cubemx1 probe peripherals --device <器件>   # 芯片支持哪些外设
python -m cubemx1 probe pins --ip <实例> --device <器件> [--mode <模式>]   # 信号↔候选引脚
```

> `ioc check` / `ioc import` / `ioc edit` / `probe` 是开发/验收与查配置用，正常出工程不需要。
> 旧的"开后台、逐条 set、apply 计划"入口已下线，别再照老文档敲。
