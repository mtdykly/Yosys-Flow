# 组合逻辑的来源选择验收

目标保持为“原始组合 RTL → 完整默认 synth → 原始语句关联的最终逻辑 → 子网表导出”。所有验收使用固定工具与默认 ABC，不添加 keep。时序实现与正向时序用例已移除；输入拒绝检查另行保留。

## 当前验收范围

| 方向 | 已实现及测试的子集 | 未完成 |
| --- | --- | --- |
| 来源选择 | 独立输出、相同输入不同运算、共享表达式、宽组合算术、表达式列选择、组合过程的分支/覆盖/裁剪、同一输出内保留切点的组合链、部分三层重结合及分配律因式分解的门组与边界排除 | 任意组合 RTL 的精确选择；同一 ABC 输出内部的一般语句区分 |
| 过程与优化 | 实际动作位和分支、无状态直连、合并/替代、子归约合并、mux 树裁剪 | 全部跨对象常量/控制依赖、位宽与共享变换 |
| techmap | 原单元到实际派生单元、输出位对应 | 算术内部门的完整位来源 |
| ABC | 内部阶段网络、实际替换、选中映射和对象转换、输出门副本，入口到 fraig 的复制链及已采用合并绑定、dch 等价/choice、有界单门/门组证明 | 全部内部重写因果历史；组内的逐门归属、没有已记录等价根或无法证明的共同改写 |
| 导出 | 参数/连接/常量复制、集合边界、共享说明、Verilog 重读等价 | 每种新来源规则还需独立选择验收 |

## 可复现例子

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run --source examples/selection.v --top demo --out runs/selection-demo
python3 -m yosys_flow query runs/selection-demo --source examples/selection.v --line 2
python3 -m yosys_flow extract runs/selection-demo --source examples/selection.v --line 2 --out runs/selection-demo/and.v
python3 -m yosys_flow extract runs/selection-demo --source examples/selection.v --line 3 --out runs/selection-demo/xor.v
```

第 2、3 行的独立 AND/XOR 输出分别选择自己的最终门；共享表达式仍保留多个来源。它们即使在同一次 ABC 调用中，也不因区域并集而互相混入。

`examples/comb.v` 演示组合 always、直连和常量；`examples/process_precision.v` 演示嵌套分支、覆盖赋值和部分位覆盖；`examples/chain.v` 演示同一输出锥内两条语句的局部切点分离。运行目录和导出文件不可覆盖，重复实验请换目录。

## ABC 证据与候选含义

提取时捕获真实信号编号、输入节点 fanin、节点对应的退休 cell 和输出边界。导入时记录 BLIF cell 的类型、端口、映射到用户设计的实际端口，以及边界连接。读取器与归档的输入/输出 BLIF、首次对象状态交叉核对。

细化为每个保留输出求映射前的输入成员锥，以及映射后服务该输出的门。共享门保留多个输出的来源并集。该算法在 `yosys_flow/abc_provenance.py`，结果为 `abc_output_cone_candidate`，证据放入 `abc_associations`。

现在还读取固定 ABC 的内部日志。单门切点验证通过时使用 `abc_internal_cut`；多门整体证明使用 `abc_internal_joint_cut` 并保留组内来源并集，不能将其当成逐门独占归属；未证明的门继续使用上述候选。实际 AIG 替换和选中映射已有钩子，完整内部重写历史尚未建立，因此 `internal_rewrite_provenance=false`、`end_to_end_exact=false` 保留。未知门或无法解析的输出回退 `abc_region_candidate`，原因位于 `abc_refinement_fallbacks`；内部验证的规模/接口限制另见 `abc_internal`。详见 [abc-internals.md](abc-internals.md)。

原始 JSONL 先按真实区域来源并集重放，再建立查询投影；细化不改写原始记录。多来源关系不能无依据地提高精度。

## 组合优化的实际事件

`cell_contribution` 表示存活供体实际贡献到目标新版本，供体不因此自动退休。当前保留 `opt_reduce_import`，在实际成功合并子归约的位置取来源并集。`opt_muxtree_prune` 记录实际裁剪目标与替代连接；全部条件分析的跨对象依据仍未覆盖。

`techmap_bit_map` 保存原输出位和实际生成对象输出位及双方信号，用于核对拆分边界；它不自动证明算术内部门的位级来源。组合过程细节见 [process-provenance.md](process-provenance.md)。

## 验收方法

`tests/test_selection.py` 使用最终输出端点、独立逻辑锥和应排除的对象定义预期，不用记录器自己的来源并集生成答案。检查共享、候选精度、原始记录不可变、ABC 节点/归档损坏，以及实际导出。

`tests/test_combinational.py` 验证组合 always 被保留，时钟过程、初始化、推导锁存器和组合环路不能发布成功结果。`tests/test_native.py`、`tests/test_process.py` 保留组合过程和开发检查点测试；`tests/test_synth.py` 验证完整默认终点，`tests/test_export.py` 验证边界、常量、参数和共享。

三层验收分别是记录一致性、来源选择正确性、导出正确性。运行还对照独立原版的完整 JSON modules 并验证功能等价；这些检查不能把未捕获转换认定为已覆盖。

`make test` 通过 `tools/run_regression.py` 保存完整报告和日志，记录工具版本、提交/工作区身份、测试清单及失败/跳过详情。验收层与测试模块的对应单独列出，某个模块通过不意味着其所属层的全部来源场景已覆盖。完整回归要求零跳过。

`tests/test_abc_internal.py` 专门检查同输出组合链、共享中间逻辑、极性、实际替换记录、内部证据损坏，多级重结合的组级包含/排除、共享边界、嵌套条件、实际 fraig/dch 等价/choice、损坏极性/复制/计数，以及缺少证明依据和超出证明规模后的候选回退。

`tests/test_abc_factoring.py` 为新的等价表达式门组定义以下独立预期；不以记录器自己的来源集合生成期望值。

| 用例 | 必须包含 | 必须排除或拒绝 |
| --- | --- | --- |
| `(a&b)|(a&c)` 与 `a&(b|c)` | 任意一行均选择最终共享 AND、OR，两行来源并集 | 独立输出 XOR；不允许漏掉共享 OR |
| 公共上游 `a=s&t` 作为输出保留 | 两种等价写法均选择下游两门，导出以 `a,b,c` 为输入 | 上游 `s&t` 的门及独立输出 |
| 正反相等价输出 | 实际 AND、NAND、OR 共享组，保留原根与复制链极性 | 独立输出 XOR |
| 输出门复制后再合并 | 所有副本都有真实复制证据，最终共享来源和精度保留 | 因未记录副本而退回候选 |
| 仅函数相同、没有已记录合并 | 不产生新的等价门组 | 不得仅凭函数匹配提升精度 |
| 损坏入口或副本证据 | 拒绝非法极性、缺项、断链、绑定、计数及不完整副本表 | 函数相同但未采用的 sweep 目标也必须拒绝 |
| ABC 将整个区域化为 0/1 | 零 cell 模块和实际常量输出，综合/导出验证通过 | 不得引用已退休常量对象或无依据地提升连接精度 |

导出另检查两门集合、实际边界位及 Verilog 重读等价，反查检查两行共享来源。结果继续报告 `end_to_end_exact=false`；门组证明不计作组内逐门独占归属或完整内部历史。示例及命令见 [ABC 因式分解门组](abc-internals.md#等价表达式与因式分解门组)。
