# 时序等价预检查与整份旧网表复用

这是独立于组合来源记录和局部替换的前置处理。用户提供已验证的旧基准和完整新版 RTL；EQY 证明全部输出满足同一时钟、初始化和复位约定后，逐字节复制旧网表。没有修改位置、局部 patch 或连接表，也不执行新版完整 `synth`。

依赖 Linux、固定 Yosys `0e82bbefe`、EQY、SBY、Yices（`yices-smt2`）、`yosys-smtbmc` 和 `yosys-abc`，均需在 PATH 中。开发验证使用 OSS CAD Suite 的 EQY/SBY v0.67。Linux `/proc` 用于超时后清理 EQY/SBY 自行建立的多个进程组。原生插桩工具不是本入口的依赖；`auto`、来源查询、契约和局部替换仍只接受组合逻辑。

## 使用

旧版本先独立归档一次；所有结果目录必须不存在：

```bash
python3 -m incremental seq-baseline \
  --source path/to/old.v --top chip --clock clk \
  --out runs/seq-old

python3 -m incremental seq-reuse \
  --base-run runs/seq-old --new-source path/to/new.v \
  --out runs/seq-new
```

可重复 `--source` / `--new-source` 传入完整设计的多个文件，`--include` 指定头文件目录，`--define NAME[=VALUE]` 指定**本版**的宏。新旧宏可以不同，报告保存各自配置及预处理后的文本。旧基准用 `--param NAME=INTEGER` 固定顶层数值参数；新检查继承同一顶层、参数、时钟及状态约定。

不指定 `--reset` 时，要求所有保留状态位有显式的 0/1 初始化。工具不猜测初始化，也不补零。若使用同步复位，可在基准阶段明确约定：

```bash
python3 -m incremental seq-baseline \
  --source path/to/old.v --top chip --clock clk \
  --reset rst --reset-active 1 --reset-cycles 2 \
  --out runs/seq-reset-old
```

此时证明适用条件是：**启动时先施加两个有效时钟沿的同步复位，释放后才观察输出**。证明包装器在启动阶段强制复位并屏蔽输出，之后复位由原输入任意控制；数据输入每周期任意。包装器只用于证明，交付网表没有添加复位逻辑。使用者必须满足记录的启动条件。没有承诺复位之前的输出相等，也没有承诺在运行中直接热切换状态。

首版支持单个直接输入的上升沿时钟、同步 FF、可被 Yosys 展开并映射到寄存器的有限数组，以及接口保持不变的完整设计。异步复位、下降沿、多时钟、门控/派生时钟、把时钟当数据、锁存器、inout、黑盒和用户形式化假设暂不支持。证明时会展平层次；归档及交付仍采用默认 `synth -top` 保留的层次形态。这不扩大原生来源前端的语法支持。

## 证明规则与交付

1. 基准保存精确的预处理源码、`proc` 前表示、时序检查表示、工具信息及文件哈希。旧版只做一次普通默认 `synth`，并将输出 Verilog 重新读入，证明它符合旧 RTL。
2. 新版只进行前端展开、`proc`、证明所需展平和 `memory_map`。时钟检查在 EQY 抽象时钟之前执行。接口、初始状态及配置不满足条件时拒绝交付。
3. **gold 是新版 RTL 规范，gate 是归档旧网表实现。** EQY 对 X 的语义具有方向性，因此先单独用 EQY/SBY 的 ABC PDR 证明：所有观察输出及内部状态在约定的观察阶段都有定义。遇到未定义输出时不能凭 EQY 的 don't-care 规则直接复用。
4. 功能关系使用 EQY/SBY 的 SMTBMC + Yices `mode prove`。同名、同宽的状态可作为匹配提示；必须证明所有相关分区，并检查每个顶层输出位与分区依赖的覆盖。内部名字相同不是等价证据。启动复位条件仍由完整设计的定义性证明验证，不能仅凭分区内的状态匹配跳过启动条件。
5. 内部分区失败不直接宣称设计不等价。预算允许时改做只匹配顶层端口的整体检查；只有真实初态出发的 base-case 反例才报告 `different`。归纳步骤失败、工具错误、超时、缺失证明或分区证据均为 `unknown`，不能复用。

`depth 3` 是当前 SMT 归纳策略的搜索深度，不是“只检查三个周期就认为等价”。`PASS` 必须包含基例与归纳步骤；结论覆盖任意后续周期。搜不到归纳证明时不会把有限周期的检查通过升级为无界结论。

`seq-baseline` 的 `baseline.json` 只有 `ready` 才能用于复用；`seq-reuse` 输出 `report.json`：

| 状态 | 产物与动作 |
| --- | --- |
| `equivalent`，`reused=true` | `reused.v`、`.il`、`.json` 与旧归档对应文件逐字节相同；保留适用条件和全部证明 |
| `different` | 保存完整输出反例（VCD / Yosys witness）；不交付复用网表 |
| `unknown` | 保存超时、工具错误或未完成的证明；不交付复用网表 |
| `blocked` | 配置/范围不支持、接口变化或归档哈希损坏；不交付复用网表 |

后三种情况报告 `full_synthesis_required`。当前不会将时序设计送入组合局部替换器，也不会在后台自动执行新版完整综合。哈希用于检测意外损坏和运行期间变化，不是对外部不可信归档的密码学签名。

## 预算与验收

默认 `--proof-timeout 120` 秒覆盖本次全部证明义务，包括适配、定义性、分区证明和整体重试；超时终止 EQY、SBY 及求解器进程组。旧版综合另有 `--synthesis-timeout 600`，前端 `--frontend-timeout 120`。这些是可调保护上限，不是性能最优值；组合 `auto` 的 5/15 秒预算保持不变。报告中的定义性和功能证明时间应分别测量，不能用组合测试的耗时直接设定时序预算。

完整 NOC 调查中，三份原始时序设计的成功证明最大值分别约为 69.23、115.44、40.10 秒，因此当前流程可显式使用 `--proof-timeout 180` 留出余量。代码默认尚未调整；其中两例只是注释修改，这些数据不能推导一般时序重构的耗时分布，也不能确定跨模块共享预算。全部结果与具体建议见 [ANUBIS 实测说明](../benchmarks/anubis/README.md#包含时序的本次实测)。

```bash
# 原始 ANUBIS ALPHA 32×64 位寄存器文件：一个等价修改、两个真实修改
python3 -m incremental anubis-sequential \
  --proof-timeout 180 --out runs/anubis-sequential

python3 -m unittest incremental.tests.test_sequential -v
```

原始用例固定在 ANUBIS `93088bd3c05407ccd871e8d5067d024f812aeeaa`：`ANUBIS_NOC_5` 改变初始化循环方向；`ANUBIS_LOCAL_0` 改变写条件；`ANUBIS_LOCAL_2` 改变读口旁路。完整宽度、初始化及源码均保留。NOC_5 会在展开时消除语法差异；仍实际证明归档门级网表满足新版，不能据此声称已验证所有时序重构。

专项回归为速度将同一寄存器文件数据宽度明确缩为 2 位，并另构造同步复位适配及错误注入，验证第二输出、真实状态修改、初始化、复位值、X、时钟、接口、超时、分区不完整和归档损坏。报告区分原始宏和这些适配控制，不把本仓库测试数当作 ANUBIS 原始用例数。当前没有时序修改后的局部重综合能力，也没有时序证明一定节省总耗时的结论。

需要重新调查全部 30 个 NOC 宏时，运行 `python3 -m incremental anubis-nochange --with-sequential --case-timeout 600 --frontend-timeout 300 --repeats 3 --out runs/anubis-noc-combined`。该入口使用相同的时序保护与证明实现，记录旧基准成本、每次新版证明和复用总时间；不支持的异步复位、前端未驱动信号、基准未证明等分别报告。配置清单和统计规则见 [完整 NOC 调查](../benchmarks/anubis/README.md)。这不是把 30 个 `NOC` 标签预设为等价，也不会用组合叶模块的亚秒耗时推定时序预算。

语义依据：[EQY X 传播与替换方向](https://yosyshq.readthedocs.io/projects/eqy/en/latest/xprop.html)、[EQY 证明策略](https://yosyshq.readthedocs.io/projects/eqy/en/latest/strategies.html)。
