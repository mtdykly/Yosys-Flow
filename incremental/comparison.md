# 全量综合与来源驱动增量的对比实验

本页的默认套件比较 **ANUBIS 中有功能修改的组合逻辑适配案例**：从完整新版 RTL 生成新版网表，来源驱动的局部综合与替换相对于全量综合节省多少时间，最终网表增加或减少多少通用门，以及统一工艺映射后的标准单元面积。功能不变修改、锁存器负例和人工控制用于独立诊断，不进入这项性能结论。

较大规模的扩展套件使用 `--suite epfl-functional`，在 EPFL 原始单模块电路上构造六对功能修改，默认比较 F 与 P-fine。固定变异、共享旧建档和实测结果见 [EPFL 实验](epfl-comparison.md)；与本页 ANUBIS 结果分别报告。

主计时终点是**整份新版网表的 JSON、RTLIL 和 Verilog 已写出**，包含把局部新门接回旧网表。生成后的正确性验证不计入这项时间，但仍执行；验证失败的结果不参与成功样本的加速比和网表规模比较。

## 比较方法

| 方法 | 实际执行 | 用途 |
| --- | --- | --- |
| `F` | 原版 Yosys 从完整新版 RTL 开始全量综合 | 独立全量基线 |
| `P-module` | 使用旧来源归档，跳过语句切片，以变化模块为起点生成替换计划、局部实现及绑定 | 同一实现的选区粒度对照 |
| `P-fine` | 使用同一旧归档，从变化赋值及依赖切片起步，必要时扩大到模块或父实例 | 当前细粒度实现 |

`P-module` 与 `P-fine` 共用前端差分、旧索引/记录、替换计划、局部综合、拼接和生成后的验证。`P-module` 不是一个独立的模块级增量综合系统；本轮不实现或测量独立方法 `M`。它仍可能因旧边界无法证明而扩区或拒绝。

算法的输入只有旧归档与完整新版 RTL。案例清单中的修改类别、宏或预期结果仅用于分组和检查，不作为修改位置、patch 或连接提示交给自动算法。完整新版综合产生的门不参与选区或拼接。

`F` 必须重新读取完整新版源文件、执行相同的顶层选择与综合配置，不能以增量路径已生成的 `new.frontend.il` 为起点来冒充完整全量成本。替换器内部的新版参考综合仅用于验证，应单独计时。

## 主要案例与诊断范围

清单固定于 [comparison-cases.json](../benchmarks/anubis/comparison-cases.json)，由 [experiment_cases.py](experiment_cases.py) 生成完整 base/new 和哈希清单。选定案例的失败、拒绝和超时全部保留。

默认 `--suite anubis-functional` 仅包含以下两项；功能不变预检查默认关闭，避免把证明后直接复用混入局部修改的综合比较。

| 主要案例 | 上游修改 | 当前已知限制 |
| --- | --- | --- |
| `compare_local9` | `quick_compare.v` 的 `ANUBIS_LOCAL_9`：符号位条件 0→1 | 保留层次可替换；展平仍可能因来源证据不足而阻塞 |
| `control_local2` | `alu_control.v` 的 `ANUBIS_LOCAL_2`：SLTIU 运算选择修改 | 来源候选及连接消费者义务仍可阻塞选区 |

两项沿用固定上游提交与原有适配器。新旧版的默认 X 输出均显式改为 0，并进行模块重命名和实例包装；所有适配与输入哈希归档。因此它们是 ANUBIS 派生的组合案例，不是原始完整 CPU。范围和上游来源见 [ANUBIS 说明](../benchmarks/anubis/README.md)。

以下仅通过 `--suite diagnostic` 显式选择，报告与主实验分开：

| 诊断分组 | 案例 | 用途 |
| --- | --- | --- |
| ANUBIS 功能不变适配版 | `bypass_noc1`、`compare_noc2`、`bypass_with_adder` | 检查等价预判和整网表复用；最后一项附带独立 32 位加法邻居 |
| ANUBIS 负例 | `writeback_local10` | 缺省赋值删除引入锁存器，组合增量必须拒绝 |
| 人工规模控制 | `scale_2`、`scale_8`、`scale_32` | 固定变化区域，增加独立不变邻居 |
| 人工共享控制 | `shared_dependency` | 展平后共享输出分化与下游常量化失效 |

诊断套件也允许复查主要案例，但诊断报告不进入主套件汇总。`--cases` 必须属于选定套件，防止误把 NOC 或人工控制作为主要性能样本。原始 30 个 NOC 宏的等价性调查仍使用独立命令，不与这里混算。

保留层次与显式展平分别统计。保留层次综合允许各模块独立优化；成功局部替换交付平坦网表。展平模式显式启用 `synth -flatten`，可能出现跨实例共享。两者优化空间不同，不能混合计算一个加速比。

## 运行

```bash
# 默认主要套件：两个 ANUBIS 功能变化组合案例，关闭等价预检查
# 输出目录必须不存在
python3 -m incremental compare --mode retained --repeats 3 \
  --out runs/comparison-functional-retained

# 显式展平独立测量
python3 -m incremental compare --mode flatten --repeats 3 \
  --out runs/comparison-functional-flatten

# 只复测其中一个主要案例
python3 -m incremental compare --cases compare_local9 --repeats 3 \
  --out runs/comparison-functional-local9

# v4 加上相同 Liberty 的面积评估；库获取方法见 benchmarks/libraries/README.md
python3 -m incremental compare --mode both --repeats 3 \
  --liberty build/technology/nangate45/lib/NangateOpenCellLibrary_typical.lib \
  --out runs/comparison-anubis-functional-v4/technology-study

# 可选诊断：不进入主要时间与规模结论
python3 -m incremental compare --suite diagnostic \
  --cases bypass_noc1 shared_dependency --precheck both --repeats 3 \
  --out runs/comparison-diagnostic
```

默认 `--suite anubis-functional`、`--repeats 5`、`--mode retained`、`--precheck off`、`--seed 20261024`、`--job-timeout 600`。每个方法至少重复 3 次。`--cpu` 默认选进程允许的最低编号 CPU；实际机器、亲和性和工具身份写入报告。测量串行执行，以固定随机种子安排方法顺序，保留逐次耗时；不清操作系统缓存，不把重复次数称为独立设计数。

显式开启预检查的诊断路径可能直接复用旧网表。此路径的计时包含功能证明与复用，未单独测量网表生成：`netlist_generation_seconds` 为 `null`，保留 `candidate_seconds` 与完整耗时作诊断。`--precheck both` 对 NOC 分别测开/关，其余诊断案例固定开启。

默认报告增量失败，不自动改成成功。显式 `--fallback-full` 才测失败后全量综合，仍保留失败尝试、后备结果及两者总成本。主要加速比不把后备全量记为增量成功。整项超时保护也不等于功能不等价或一个完整的生成耗时。

## 时间：到整份新版网表生成

| 字段 | 计时范围 | 是否用于主要速度比较 |
| --- | --- | --- |
| `netlist_generation_seconds` | 输入开始到整份新版 JSON/IL/Verilog 写出 | 是 |
| `post_generation_validation_seconds` | 写出后的局部输出证明、候选检查、完整参考及最终验证 | 否，单列 |
| `candidate_seconds` | 输入开始到带检查的候选就绪 | 否，仅作诊断 |
| `end_to_end_seconds` | 外层 worker 启动到退出，含验证和产物处理 | 否，保留运行总成本 |
| `mapping_seconds` | 冻结成功交付的 generic 网表、核对库摘要、展平、ABC 工艺映射及写出映射网表 | 单列 |
| `mapped_netlist_generation_seconds` | 同一样本的 `netlist_generation_seconds + mapping_seconds` | 可选终点：包含统一的全网表工艺映射 |
| `mapping_validation_seconds` | 映射后面积核对、全输出 SAT 与 Verilog 读回证明 | 否，单列 |

对增量而言，主时间包含旧归档及验证索引加载（旧归档缺索引时重放）、新 RTL 前端、差分、选区与扩区、边界分析、patch 提取与导出、局部综合、绑定、拼接以及整份新网表写出。旧记录中的来源证据用于确定选区和连接，这部分分析是算法生成新网表的实际工作，不能只拿 `local_synth_seconds` 与全量综合比较。

局部全部输出证明、候选 `check`/组合环检查、完整新版参考综合、全设计等价检查、Verilog 重读验证及最终归档复核在主计时终点后执行。`F` 也先综合并写出相同三种网表文件，随后单列检查时间。两种方法的主计时都不包含外层测量 worker 的解释器启动时间。

`candidate_seconds` 也包含替换和拼接，但同时包含部分证明/检查，终点不等同于三种完整网表写出。主要指标使用独立的实际计时点；阶段可能嵌套，不从总时间中减去若干阶段来推算生成耗时。

只对验证通过的完整重复组计算 `generation_speedup = F 生成中位数 / 增量生成中位数`、`generation_seconds_saved` 和网表规模差。失败样本保留阶段耗时及失败原因；生成过文件但验证失败，不表示获得了一个可参与收益统计的正确实现。验证不计入主要运行时间，不等于已经证明实现对所有输入设计都正确。

## 网表规模、内存和统计

未指定 `--liberty` 时，仅使用**最终 generic 网表的通用门数量**作为结构代理，另列门类型、旧门数、删除/复用门数、局部新门和接入开销。每种门按一个单元计数，没有 Liberty 面积权重，因此不是标准单元面积。单位门逻辑深度同样不包含负载、线延迟或实际单元延迟，不作为物理时序结果。

指定 `--liberty` 后，所有成功交付的完整 generic 网表使用相同 `flatten`、`abc -liberty` 条件映射。当前固定库为 Nangate45 typical（1.1 V、25 °C），来源、哈希、单位依据和获取命令见 [工艺库说明](../benchmarks/libraries/README.md)。面积是映射后各单元的实例数乘以 Liberty `area` 之和；与 Yosys `stat -liberty` 交叉核对。已知库的单位经 LEF 尺寸核对为 µm²，未知库只报告 `library_area_units`，不猜单位。

保留层次与展平两组在面积阶段均对完整交付结果展平；分组仍保留，因为前面的 generic 优化空间不同。保留 `keep` 和 `keep_hierarchy` 约束，若后者阻止完全展开，则本次面积测量拒绝。不配置额外时序目标、驱动单元或输出负载，也不插入 tie cell；常量直接保留。这个数值不包含布局空白、布线、时钟树或物理填充面积。

这是**交付结果的统一全局工艺映射评估**。当前增量算法仍交付 generic 网表，尚未实现旧标准单元的增量保留和替换；全局映射可能重新组织原本复用的门。`mapped_netlist_generation_seconds` 是两个生成阶段的逐样本求和，排除中间及之后的验证，并非一段无间断墙钟测量。原有主指标 `netlist_generation_seconds` 保持不变，首次旧设计综合建档仍不计入在线时间。

映射后的 JSON、Verilog 读回均与输入 generic 网表做全输出等价和输出有定义性检查，允许任意二值主输入。未知单元、漏映射、缺少面积、面积统计不一致、证明失败或超时均不报告有效面积；不会将部分映射的面积之和冒充完整面积。映射测量失败与 generic 生成结果分别报告；只有完整成功的重复组、相同库及相同单位才计算面积变化和映射后生成时间比。

按实际电气实例计数，不能只数模块定义一次。边界缓冲、极性适配和没有重新做全局优化都可能增加最终门数。规模比较只覆盖主要套件中验证通过的样本；NOC 直接复用、人工控制和拒绝项均不纳入主要面积结论。未知值保留 `null`，不填 0。

内存同时记录约 20 ms 采样的进程树 RSS 总和峰值、单进程高水位最大值。前者可能重复计共享页或漏短峰值；后者不是所有进程同时使用的内存。这两个指标不相加，也不当作精确物理占用。

报告保留每次状态与有效样本数。来源阻塞、范围拒绝、验证失败、异常和超时不会从分母消失，也不以缺失时间计算加速。所有增量成功样本必须通过现有的全部输出、边界、未选中门及产物检查；`F` 成功表示原版全量综合及自身检查完成。

## 一次性成本和收支平衡

旧设计的插桩全量综合与记录建档只执行一次，并供同一案例的两种增量选区共用。报告同时保存普通旧设计全量综合成本、完整建档成本、记录与归档字节数。建档流程还包含验证，因此不能把它的总耗时全称为纯钩子开销。

`validated_archive_overhead_seconds` 以两种隔离 worker 的完整墙钟差计算一次性额外成本 `H = 建档成本 - 普通旧综合成本`，同时保留两项原始值。`traced_tool_overhead_seconds` 另比较建档 manifest 中的插桩工具阶段与普通旧 RTL 全量工具阶段，两者不可混为同一个 H。

若某条在线策略每次平均节省 `Δ = F - 增量在线成本 > 0`，才有形式上的收支平衡条件 `N × Δ >= H`；`Δ <= 0` 时没有由增加修改次数得到的正向摊销结论。报告的 `break_even_edits` 按一次测得的 H 与新版网表生成时间中位数节省估算，依据为 `netlist_generation_seconds`，不包含生成后的独立验证。

当前实验反复使用同一旧归档来测量独立修改，不产生每次替换后可继续追踪的新版来源归档。因此上述 `N` 是成本估计，不是已完成连续 N 次增量迭代的验证；后续修改分布、失败回退及重新建档会改变它。

## 产物与结果解释

运行目录主要产物如下：

- `fixtures/<case>/`：完整旧/新版及 `fixture.json`，包含适配和输入哈希。
- `preparation/<case>/<mode>/stock-old/` 与 `traced-old/`：普通旧综合及一次性建档，各有 `measurement.json`。
- `samples/<order>-.../`：每次独立作业的配置、日志与 `measurement.json`；增量报告在 `auto/report.json`，全量脚本、日志及 `.json/.il/.v` 网表在 `full/`。
- `technology/`：启用 `--liberty` 时冻结的库、面积目录、来源及单位元数据；已知库同时保存锁文件和缓存中的上游说明。
- `samples/<order>-.../library-area/`：成功 generic 结果的映射网表、面积分项、映射脚本、全输出证明及读回日志。失败或来源阻塞的 generic 样本不进入映射。
- `report.json`：全部原始测量、分类摘要、配置、工具/输入/实现哈希及一致性检查结果；`evidence_files` 绑定运行证据文件的哈希。
- `implementation/`、`catalog.json`：本轮实际 Python 实现及固定案例清单的副本，便于后续代码改变后复查。
- `timings.csv`、`stages.csv`、`schedule.json`：逐次指标、阶段耗时及实际随机执行顺序。

结果位于 `runs/`，按仓库规则不提交；实验代码、固定清单和文档可提交。

这些文件用于复查某个样本实际运行了什么、何时回退、哪些证明未完成，以及是否把预处理成本排除在外。发现具体来源缺口时再补对应钩子，并保持同一清单重新测量，避免只报告后来能通过的子集。

## 当前实测：v4

最新时间与工艺面积结果为 [technology-study/report.json](../runs/comparison-anubis-functional-v4/technology-study/report.json)，逐次数据在 [timings.csv](../runs/comparison-anubis-functional-v4/technology-study/timings.csv)。原始 v4 generic 报告及证据保留在父目录；本次没有更改来源、选区、局部综合或拼接算法，只扩展了测量器和库映射评估。

```bash
python3 -m incremental compare --mode both --repeats 3 \
  --liberty build/technology/nangate45/lib/NangateOpenCellLibrary_typical.lib \
  --out runs/comparison-anubis-functional-v4/technology-study
```

复测需使用尚不存在的输出目录。固定种子 `20261024`、CPU 0、Intel Xeon Gold 5220R，串行随机次序运行，关闭等价预检查。两项功能修改案例 × 两种层次模式 × 三种方法 × 三次重复，共 **36 个样本**：12 次 `F` 成功、6 次增量成功、18 次增量来源阻塞。没有自动全量回退，也没有混入 NOC、人工控制或锁存器负例。

18 份成功交付网表的映射、面积核对、JSON 全输出证明及 Verilog 读回证明全部通过。阻塞的 18 次没有可交付的新版网表，不执行映射、不报告有效面积。

### 能够比较的组：compare_local9 保留层次

以下均为三次中位数，时间单位为秒。生成时间截止完整网表写出，排除首次旧建档和生成后的验证。最后一项时间先逐样本相加，再取中位数，因此不一定等于前两列中位数之和。

| 方法 | generic 生成 | 统一工艺映射 | 生成＋映射 | 通用门数 | 映射单元数 | 单元面积（µm²） | 面积相对 F |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `F` | 0.300 | 0.188 | 0.491 | 232 | 154 | 190.456 | — |
| `P-module` | 1.818 | 0.189 | 2.007 | 235 | 148 | 188.860 | −0.84% |
| `P-fine` | 3.476 | 0.189 | 3.665 | 238 | 153 | 191.254 | +0.42% |

**本例 P-fine 的 generic 生成耗时约为 F 的 11.6 倍；包含统一工艺映射后约为 7.5 倍。没有测出时间收益。** P-module 也更慢，所有增量策略的 `break_even_edits` 为空。三次重复中，每种方法的面积完全一致。面积差别较小，不能据此推断其他设计上有同样趋势。

P-fine 比 F 少一个映射单元，但面积略大：不同单元的面积权重不同，通用门数或标准单元数均不能直接替代加权面积。P-module 的最终面积略小，也是这份交付网表经过统一全局映射的结果，不表示模块选区算法普遍优于细粒度方法。

此例是两个独立 32 位比较器构成的小案例，不是完整 CPU，也不是大型设计。本轮只出现一个可完整比较的案例/层次组，无法据此给出大规模性能结论。`P-fine` 删除 110 个旧门、复用 121 个；`P-module` 删除 115 个、复用 116 个，复用数量更多并不保证最终面积更小或总耗时更低。

后验成本另列：F 的 generic 结构检查约 0.011 秒，P-module 的 generic 后验验证约 2.104 秒，P-fine 约 2.728 秒；各方法工艺映射后的面积核对和两次全输出证明约 0.646–0.658 秒。这些时间均未计入上表的生成或生成＋映射列。两种 generic 后验步骤的工作量不同，F 没有执行增量路径中的局部对应与旧归档重放验证。

### 在线开销与一次性建档

P-fine 的主要生成阶段中位数为：旧归档/已认证索引加载约 **1.260 秒**，选区、边界/计划和局部功能绑定约 **1.903 秒**，局部 Yosys 综合约 **0.130 秒**。加载与选区/绑定合计约占 generic 生成时间的 91%，局部综合约占 4%。阶段存在嵌套，不应将报告所有计时字段相加。

本轮继续使用已认证索引和轻量 `replacement-plan.json`。原始产物仍按哈希核对，共享来源及 ABC 证据通过旧归档引用，替换目录引用同一计划；来源精度、边界、全部输出及共享义务仍是必要条件。

首次旧建档单列：原版旧设计综合约 **0.196 秒**，插桩工具阶段约 **0.339 秒**；完整建档约 **5.368 秒**，其中索引创建约 **1.319 秒**。完整建档还包含记录验证和独立对照，不能把 5.368 秒全称为钩子开销。这些一次性成本均未进入 3.476 秒在线生成时间；旧建档只测了一次。

### 仍然阻塞的组

| 案例与模式 | F generic 生成 | F 生成＋映射 | F 单元面积（µm²） | P-module / P-fine |
| --- | ---: | ---: | ---: | --- |
| `compare_local9` 展平 | 0.189 | 0.386 | 196.574 | 各 3 次来源候选未证明，阻塞 |
| `control_local2` 保留层次 | 0.254 | 0.440 | 92.834 | 各 3 次来源候选及连接消费者绑定义务未完成，阻塞 |
| `control_local2` 展平 | 0.170 | 0.305 | 101.878 | 各 3 次来源候选及连接消费者绑定义务未完成，阻塞 |

这些增量组没有可比较的完成时间或面积，不填零，也不计算加速或面积差。展平与保留层次之间的全量面积不同，体现不同优化路径，不能混用作同一增量组的基线。

### 验证与证据

本轮测量相关的 **45 项定向测试通过，零失败、零跳过**，覆盖计时隔离、失败分母、跨库/单位拒绝、面积漏计、X 输出、库篡改和读回错误。本轮未重跑整个来源记录与增量系统回归。

新实验的 **1,404 个证据文件**及原始 v4 的 **942 个证据文件**哈希复核一致；18 次面积测量共检查 36 项全输出 SAT 证明。实现及工具在测量期间未变化。核对结果与测试日志见 [technology-study-checks.json](../runs/comparison-anubis-functional-v4/validation/technology-study-checks.json) 和 [technology-tests.log](../runs/comparison-anubis-functional-v4/validation/technology-tests.log)。原有完整回归的状态继续保存在 [followup-checks.json](../runs/comparison-anubis-functional-v4/validation/followup-checks.json)：301 项首次运行中有一个错误消息断言失败，修正后的定向复测通过；不能将其称为本轮全量零失败回归。

## 后续工作

`shared_dependency` 展平后，旧 `target_y` 与 `other_y` 共享信号，`dependent` 被化为 0；新版要求输出分化、下游恢复逻辑。当前拼接未正确处理，整体等价检查拒绝候选，没有发布网表。后续必须在局部综合前识别消费者分化及下游常量化失效，正确扩区或拒绝，不能通过关闭检查将失败计作性能成功。

后续优先继续减少索引加载、选区及绑定的在线成本，并补齐功能修改案例暴露的来源和消费者义务。完整契约继续用于显式审查；进一步优化仍需用同一生成终点复测，同时保留失败分母，不能将减少检查本身视为算法收益。
