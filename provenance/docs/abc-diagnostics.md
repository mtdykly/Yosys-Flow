# 从一次综合记录诊断区域与测量 ABC 基线

当前目标是组合 RTL 的来源定位与导出，并为后续确定替换范围保留约束。已提供只读区域诊断、可重复基线及大区域符号证明，并接入普通组合层次的实例视图。独立的 [incremental 实验](../../incremental/README.md) 已消费这些记录进行局部替换，支持显式输入及自动差分/依赖切片两种入口；自动模式优先选变化赋值，模块作为后备，来源候选不会因此升级。`fraig` sweep 后的清理映射已记录和校验，后续 `dc2/dch` 的全部因果链仍未补齐。层次与展平的区别见 [hierarchy.md](hierarchy.md)。

[ANUBIS 基准](../../benchmarks/anubis/README.md)新增了真实宏修改、32 位比较器及独立加法器邻居。BDD 位交错排序缓解已复现的预算问题；展平用例中仍存在阶段函数见证缺失，报告具体命令/快照而不归咎于预算或缺少钩子。完整回归保留这些阻塞及拒绝原因，不把它们当成替换成功。

## 读取已有记录

```bash
python3 -m yosys_flow run --source provenance/examples/factoring.v --top demo --out runs/factor-record
python3 -m yosys_flow diagnose runs/factor-record --source provenance/examples/factoring.v --line 2 > runs/factor-diagnostic.json
python3 -m yosys_flow extract runs/factor-record --source provenance/examples/factoring.v --line 2 --out runs/factor-record/y.v
```

`run` 执行一次带记录的完整默认 synth，以及独立原版对照和等价检查。后续 `query/diagnose` 只读本次日志及网表，不启动 Yosys/ABC；导出需要 Yosys 做格式转换与验证，但不会重新综合原始设计。多个查询可复用同一份综合记录。

`diagnose` 接受与 `query` 相同的源码行列或 `--object` 选择条件，JSON 写到标准输出。它验证产物哈希、记录和 ABC 证据，然后复用导出器的电气边界计算；不会改变记录、来源规则或所选集合。查询仍使用旧版本源码坐标，源码变更会提示，不能自动按新行号迁移。

| 字段 | 含义与限制 |
| --- | --- |
| `selected_cells` / `selected_connections` | 实际来源选择，含共享来源和候选精度 |
| `boundary` | 与导出一致的实际信号位、别名、驱动、使用者；常量输出保留具体原始连接身份 |
| `electrical_cut` | 独立枚举所选 cell 的跨边界连接，核对输入/输出和所有外部使用者；不证明源码选择完整 |
| `other_recorded_sources` | 所选 cell 上查询范围之外的已记录来源，不表示它们可以被忽略 |
| `recorded_group_closure` | 从实际 ABC 门组成员沿记录的版本/合并关系找到最终对象；检查是否选全，列出缺失成员；不自动扩大选择 |
| `candidate_*_refs` / `abc_associations` | 候选对象及其输出边界/输入成员依据，或局部证明信息 |
| `abc_internal` / `inventory` | 证明与回退原因、实际区域输入数、事件/快照数量和字节数 |
| `replacement_assessment` | 尚未证明的条件和未来必须检查的约束；当前始终 `not_proven` |
| `synthesis_invocations` | 诊断始终为 0 |

`factoring.v` 第 2 行返回共享 AND/OR，输出使用者同时包括 `y` 和 `z`。诊断因此能说明未来改变这一片逻辑还需保留 `z` 的功能。若用 `--object` 仅查询其中一个门，`additional_required_cell_refs` 会列出同一已记录门组的其他最终成员；`selection_expanded=false` 表明没有修改原选择。

门组追踪只涵盖已经记录的组和后续关系。成员没有可解释的最终 cell 时列入 `members_without_final_cell`，不能据此断言它已无影响。候选或未覆盖来源仍显式保留。`safe_for_incremental_reuse=false`、`end_to_end_exact=false` 不因电气边界完整或门组齐全而改变：新 RTL 可能新增依赖、改变输出功能或需要拆分旧共享信号，后续仍须检查。

## 运行基线

```bash
python3 provenance/tools/benchmark_abc.py --out runs/abc-baseline --repeats 3
# 较小的定向运行；每次使用新目录
python3 provenance/tools/benchmark_abc.py --out runs/abc-scale-check --repeats 1 --sizes 10 12 --case scale_10 --case scale_12
```

默认包括链、共享链、多级重结合、等价表达式、保留上游边界、ABC 化为 0/1 共 7 组案例，以及 8/10/12/16/32 输入的共享链。每个样本执行一次插桩 synth 和一次独立原版 synth；同一样本的多个正向/反向查询复用已验证记录，导出另做格式等价。重复样本用于测量，不是定位过程的要求。

每个目录保存 `input.v`、完整运行产物、各查询的 `.query.json`、`.diagnostic.json`、导出 `.v/.json/.il/.provenance.json` 和 `sample.json`。顶层 `report.json` 记录样本、摘要中位数、工具/源码/脚本/实现摘要、提交及工作区、运行配置与机器信息。已有目录不会覆盖。

预期集合位于 `provenance/yosys_flow/abc_cases.py`，依据原 RTL 意图、最终端口驱动及保留边界定义，不读取记录器来源来产生答案。用于验收的电气锥遍历只是测试预期构造方式，不改变产品按来源选择的规则。报告分别保存：

- `missing_cells` / `extra_cells` / `exact_cell_selection`：预期与实际集合差异。
- `expected_boundary_matches` / `origin_lines_match`：独立边界及来源行预期。
- `checks` / `contract_passed`：所有固定案例的精度、反查、边界和导出验收；规模案例还检查实际区域大小及符号功能校验完成。
- `cut_proven_count` / `joint_proven_count` / `candidate_cell_count`：单门、门组及候选分开统计。

当前固定规模案例也要求精确选择，候选扩大或漏选都会使契约失败。仍分别保留 `exact_cell_selection`、差异及候选计数，不能从工具完成推断任意 RTL 已受支持。

12/16/32 输入共享链的验收要求是：单门目标只返回自己的一个已证明门，下游语句排除上游两门。ROBDD 校验大区域函数及宽门组的独立边界；不能仅因区域较大就将整片逻辑作为该目标的可通过结果。

功能校验不凭输入数量跳过，也不提高完整枚举上限。`function_proof` 和 `local_function_resources` 给出后端、节点及计算量；区域/局部预算不足的行为由专门测试覆盖，详见 [符号证明限制](abc-internals.md#符号功能校验与独立切点预算)。

规模选择与预算回退由 `provenance/tests/test_abc_scale.py` 验收。按上面的命令生成本次测量报告；节点数和耗时只代表对应工具、机器和固定案例，不是任意设计的成本上界。

## 时间和内存的解释

`timings_seconds` 分别保存带记录综合 `traced`、原版对照 `baseline`、整体验证 `equivalence`、运行内记录检查 `record_validation`，以及重新读取时的哈希检查、记录重放和 ABC 证明。`run_total_including_validation_and_stock` 包含上述阶段和其他处理，不能当作插桩开销；两份综合脚本的产物写出量也不同，原始时间并非严格的纯记录器成本对照。

每条查询另计已加载记录上的查询、区域诊断、反查，以及包含重新加载/证明和格式等价的导出总时间。加载阶段与其总时间是包含关系，不要重复相加。首次建档保存 `verified-index.sqlite3`；后续核对归档身份和索引适用性后按需读取已验证状态，索引不适用时完整重放。旧 gzip 索引仍有兼容路径。报告应区分索引命中与重放，不能混用两者的加载耗时；原始证据的哈希仍完整计算，详见[存储说明](storage.md)。

测量必须同时查看证明计数和回退原因；省略证明或耗尽预算的结果，不能与完成同等精度验证的结果直接比较速度。

Linux 有 `/usr/bin/time` 时，隔离样本进程记录 `sample_max_rss_kib`；它是 GNU time 报告的进程及已等待子进程的最大 RSS，不是所有并发进程同时占用内存之和，也不是单独 ABC 内存。不可用时记录 null 及原因，不用累计历史进程峰值冒充本样本数据。默认串行运行，各案例重复三次；基线测量应与完整测试回归分开，减少负载干扰。

下一项改进用更复杂布尔函数检验符号节点增长和预算回退。当前仍会评估整个区域的阶段函数，尚未让小目标完全独立于区域内其他复杂逻辑；若这成为实际失败原因，再做验证分解或 SAT。其余因果关系按具体失败/验证断点继续补齐，不能只增加事件数量，也不能把未证明对象包装成安全替换区域。
