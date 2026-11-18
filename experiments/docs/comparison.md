# 全量综合与来源驱动增量的对比实验

本页的默认套件比较 **ANUBIS 中有功能修改的组合逻辑适配案例**：从完整新版 RTL 生成新版网表，来源驱动的局部综合与替换相对于全量综合节省多少时间，最终网表增加或减少多少通用门，以及统一工艺映射后的标准单元面积。功能不变修改、锁存器负例和人工控制用于独立诊断，不进入这项性能结论。

较大规模的扩展套件使用 `--suite epfl-functional`，在六个 EPFL 原始单模块电路上构造十二对功能修改，默认比较 F 与 P-fine。固定变异、共享旧建档和实测结果见 [EPFL 实验](epfl.md)；与本页 ANUBIS 结果分别报告。

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

清单固定于 [comparison-cases.json](../../benchmarks/anubis/comparison-cases.json)，由 [experiment_cases.py](../experiment_cases.py) 生成完整 base/new 和哈希清单。选定案例的失败、拒绝和超时全部保留。

默认 `--suite anubis-functional` 仅包含以下两项；功能不变预检查默认关闭，避免把证明后直接复用混入局部修改的综合比较。

| 主要案例 | 上游修改 | 最新保留测量 |
| --- | --- | --- |
| `compare_local9` | `quick_compare.v` 的 `ANUBIS_LOCAL_9`：符号位条件 0→1 | 保留/展平两种模式已通过指定修改的验收 |
| `control_local2` | `alu_control.v` 的 `ANUBIS_LOCAL_2`：SLTIU 运算选择修改 | 指定修改已通过；实际复用粒度见后端结果 |

两项沿用固定上游提交与原有适配器。新旧版的默认 X 输出均显式改为 0，并进行模块重命名和实例包装；所有适配与输入哈希归档。因此它们是 ANUBIS 派生的组合案例，不是原始完整 CPU。范围和上游来源见 [ANUBIS 说明](../../benchmarks/anubis/README.md)。

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
python3 -m experiments compare --mode retained --repeats 3 \
  --out runs/comparison-functional-retained

# 显式展平独立测量
python3 -m experiments compare --mode flatten --repeats 3 \
  --out runs/comparison-functional-flatten

# 只复测其中一个主要案例
python3 -m experiments compare --cases compare_local9 --repeats 3 \
  --out runs/comparison-functional-local9

# 相同 Liberty 的面积评估；库获取方法见 benchmarks/libraries/README.md
python3 -m experiments compare --mode both --repeats 3 \
  --liberty build/technology/nangate45/lib/NangateOpenCellLibrary_typical.lib \
  --out runs/comparison-technology

# 可选诊断：不进入主要时间与规模结论
python3 -m experiments compare --suite diagnostic \
  --cases bypass_noc1 shared_dependency --precheck both --repeats 3 \
  --out runs/comparison-diagnostic
```

默认 `--suite anubis-functional`、`--repeats 5`、`--mode retained`、`--precheck off`、`--seed 20261024`、`--job-timeout 600`。支持每个方法重复 1 次；单次不代表稳定均值，正式比较建议至少重复 2 次。同时输出 `successful_means` 与 `successful_medians`。`--index-backend sqlite|gzip` 默认 SQLite，`--baseline-repeats` 默认 1；设为 2 时独立建档两次，在线重复与归档轮次配对。`--cpu` 默认选进程允许的最低编号 CPU；实际机器、亲和性和工具身份写入报告。测量串行执行，以固定随机种子安排方法顺序，保留逐次耗时；不清操作系统缓存，不把重复次数称为独立设计数。

最新两后端、单次复测、360 秒且不限体积的 ANUBIS/EPFL 比较见[后端结果](backend-results.md)。先前两次均值测量的摘要保存在 `runs/retained-summaries/backend-suite-360/`，不与单次结果混算。

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

指定 `--liberty` 后，所有成功交付的完整 generic 网表使用相同 `flatten`、`abc -liberty` 条件映射。当前固定库为 Nangate45 typical（1.1 V、25 °C），来源、哈希、单位依据和获取命令见 [工艺库说明](../../benchmarks/libraries/README.md)。面积是映射后各单元的实例数乘以 Liberty `area` 之和；与 Yosys `stat -liberty` 交叉核对。已知库的单位经 LEF 尺寸核对为 µm²，未知库只报告 `library_area_units`，不猜单位。

保留层次与展平两组在面积阶段均对完整交付结果展平；分组仍保留，因为前面的 generic 优化空间不同。保留 `keep` 和 `keep_hierarchy` 约束，若后者阻止完全展开，则本次面积测量拒绝。不配置额外时序目标、驱动单元或输出负载，也不插入 tie cell；常量直接保留。这个数值不包含布局空白、布线、时钟树或物理填充面积。

这是**交付结果的统一全局工艺映射评估**。当前增量算法仍交付 generic 网表，尚未实现旧标准单元的增量保留和替换；全局映射可能重新组织原本复用的门。`mapped_netlist_generation_seconds` 是两个生成阶段的逐样本求和，排除中间及之后的验证，并非一段无间断墙钟测量。原有主指标 `netlist_generation_seconds` 保持不变，首次旧设计综合建档仍不计入在线时间。

映射后的 JSON、Verilog 读回均与输入 generic 网表做全输出等价和输出有定义性检查，允许任意二值主输入。未知单元、漏映射、缺少面积、面积统计不一致、证明失败或超时均不报告有效面积；不会将部分映射的面积之和冒充完整面积。映射测量失败与 generic 生成结果分别报告；只有完整成功的重复组、相同库及相同单位才计算面积变化和映射后生成时间比。

按实际电气实例计数，不能只数模块定义一次。边界缓冲、极性适配和没有重新做全局优化都可能增加最终门数。规模比较只覆盖主要套件中验证通过的样本；NOC 直接复用、人工控制和拒绝项均不纳入主要面积结论。未知值保留 `null`，不填 0。

内存同时记录约 20 ms 采样的进程树 RSS 总和峰值、单进程高水位最大值。前者可能重复计共享页或漏短峰值；后者不是所有进程同时使用的内存。这两个指标不相加，也不当作精确物理占用。

报告保留每次状态与有效样本数。来源阻塞、范围拒绝、验证失败、异常和超时不会从分母消失，也不以缺失时间计算加速。所有增量成功样本必须通过现有的全部输出、边界、未选中门及产物检查；`F` 成功表示原版全量综合及自身检查完成。

`--repeats 1 --baseline-repeats 1` 可用于一次支持范围复测；这种结果是单次观察，没有方差或稳定均值的含义。自动扩区后的 `selection_strategy`、删除/复用门数仍应一起报告：`statement_dependency_slice` 也可能最终覆盖整个旧电路，不能只根据方法名称声称细粒度复用。新加入的旧源码区域 SAT 证明属于选区所需证据，其耗时计入在线生成时间。

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

## 保留的结果与当前状态

SQLite/gzip 在 ANUBIS 功能修改和六个 EPFL 电路上的最新完整对比见 [后端结果](backend-results.md)，数据位于 `runs/backend-suite-supported-360/`。后续多案例热点归因见 [耗时归因](cost-attribution.md)。旧 v4 的逐次失败过程已从本文移除，Git 历史保留原记录。

较大 `hyp` 已完成一个固定修改的整套交付：新版全量约 506.61 秒，增量生成约 455.21 秒；这不是任意修改的保证，见 [hyp](hyp.md)。最新 `stress_3000` 仍在来源边界绑定阶段阻塞，见 [当前状态](../../docs/status.md)。
