# 默认 synth 来源选择验收：ABC 对应、优化吸收和位关系

本轮保持 RTL 支持范围与默认 `synth -top <top>` 不变，围绕三个方向推进：独立的最终对象选择验收、ABC 节点交接与候选细化、影响已有结构的优化和过程来源。仍使用同一个原生记录器，未改变默认 ABC 脚本、添加 keep 或跳过优化。

## 本轮实际达到的范围

| 方向 | 已实现并验证 | 尚未完成 |
| --- | --- | --- |
| 最终来源选择验收 | 独立输出、相同输入不同运算、共享表达式、宽算术、重写、使能/复位吸收、时钟反相、寄存器拆位；每例对照原版综合并导出验证 | 任意 RTL 或每种优化的完整验收 |
| ABC | 原 cell → 实际输入节点、输出 BLIF 节点 → 导回 cell/连接；按保留输出边界收缩候选，独立 AND/XOR 可分开 | ABC 内部节点重写、选择替代实现和映射的事件历史；同一输出锥内的逐语句精确区分 |
| 内部来源 | 实际 mux 吸收、反相器旁路、子归约合并、mux 树裁剪、复位值求解和 techmap 输出位对应 | 全部跨对象优化依赖、全部算术内部位关系；复杂情形继续保留缺口 |

这张表不将第二、三项的阶段成果计为整个默认 synth 精确追踪已经完成。后续继续补来源精度，层次、memory、generate 等输入支持范围尚未扩大。

## 可复现例子

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source examples/selection.v --top demo --out runs/selection-final
python3 -m yosys_flow query runs/selection-final \
  --source examples/selection.v --line 2
python3 -m yosys_flow extract runs/selection-final \
  --source examples/selection.v --line 2 --out runs/selection-final/and.v
python3 -m yosys_flow extract runs/selection-final \
  --source examples/selection.v --line 5 --out runs/selection-final/enable.v
python3 -m yosys_flow extract runs/selection-final \
  --source examples/selection.v --line 10 --out runs/selection-final/state-bit.v
```

运行目录和导出文件不能覆盖已有结果，重复实验请换目录。

- 第 2、3 行分别对应 AND 与 XOR 的最终对象，不因为同属一次 ABC 调用而互相混入。
- 第 5 行 mux 被 `opt_dff` 吸收到最终使能 FF；查询仍能返回该 FF。
- 第 8 行复位关联两个状态位；第 10、11 行数据赋值各关联对应的单个 FF。

## ABC 候选细化的证据与含义

提取时记录实际信号编号、输入节点 fanin、节点对应的退休 cell，以及规范化输出信号。导入时记录实际 BLIF cell 的类型、端口和映射到用户设计的端口；创建对象引用该节点。边界连接另记录实际输出编号。读取器检查这些引用，并与归档的输入/输出 BLIF、首次导回 cell 状态交叉核对。

细化按每个保留输出边界进行：求出映射前关联到该输出的输入成员，以及映射后服务该输出的门。一个共享门服务多个输出时，保留这些输出对应的来源并集。该算法在 `yosys_flow/abc_provenance.py`，结果为 `abc_output_cone_candidate`，证据放入 `abc_associations`。

它利用结构来收缩已有 ABC 区域候选，没有取得 ABC 内部实际重写历史。因此 `internal_rewrite_provenance=false`、`end_to_end_exact=false` 继续保留；同一输出的多个来源仍可能混合。遇到未知门或未解析输出时保留 `abc_region_candidate`，并通过 `abc_refinement_fallbacks` 解释回退。它不将所有原始 RTL 上游门加入默认导出集合，也不改变 `source_association` 的导出模式。

原始 JSONL 仍记录实际事件及区域来源并集，重放首先校验这些事实，再建立用于查询的细化关联。细化不会改写 `trace.records` 或原始日志。后续 cell 版本、派生及合并继续传播细化结果；多来源关系不能无依据地升级候选精度。

## 优化、复位与 techmap

`cell_contribution` 表示一个存活对象实际贡献到另一个对象的新版本，供体不因此自动退休。已接入 `opt_dff_mux_absorb`、`opt_dff_feedback`、`opt_expr_polarity`、`opt_reduce_import`。目标来源与供体来源取并集；事件在成功变换的位置捕获，不按事后的名字相似度匹配，也不递归收集任意扇入。

`opt_muxtree_prune` 的作用域记录实际被裁剪对象及替代连接。条件分析的全部跨对象依据尚未覆盖，因此 `opt_muxtree` 与 `opt_reduce` 仍声明部分覆盖。

`proc_arst` 在实际求复位值的递归遍历中传递动作证据，`reset_value` 节点把存活的复位赋值接到对应状态位。找不到记录的复位路由或遇到不完整证据时仍回退 `process_signal_candidate`。敏感事件继续标明 `clock_or_reset`，没有声称拆分全部前端控制依赖。

`process_binding.output_proofs` 保存普通/已捕获复位 FF 的逐 Q 位过程证据。`techmap_bit_map` 记录原输出端口位和生成对象输出端口位的真实对应，以及双方信号。当前只对有完整逐位证据、Q 对应直接一致且 D 位一致的单比特 DFF 派生应用来源细化；相关结构或来源发生未解释变化时放弃该细化。宽算术的输出位对应可查看，但不等于每个内部门的位级来源已经解决。

## 第三步续补：寄存器优化中的逐位来源

宽 FF 在 `opt_dff` 中可能按不同使能或复位拆成多个 FF。此前仅把成功吸收的 mux 来源并入整个原 FF，会导致查询 `q[0]` 的赋值也选中 `q[1]`。本次在实际成功旁路处记录 `ff_mux_absorb`：原 FF 版本、Q 位、供体 mux 的输出位、替换前后的 D 位，以及 `ce` / `srst` 模式。在同次实际重写结束时，`ff_bit_map` 记录存活 FF 的 Q/D 位与原位的对应及吸收证据编号。

回放检查连续 D 替换链、mux 端口、反馈位或复位常量、Q 位、位宽和真实派生关系。只有时钟及保留控制一致、新增使能/复位直接对应已记录 mux 选择端、极性和值一致时，才应用逐位来源细化。过程的逐位证据与该位实际吸收的供体一起传递给新 FF，再传到直接 D/Q 对应的通用 DFF/SDFF 门族。没有完整逐位过程证据时保留原来源；不会凭一个 Q 信号匹配推断全部优化语义。

`opt_clean` 使用实际 `assign_map` 生成 `ff_alias_map`，在后续观察与该映射、类型和参数完全一致时保留逐位证据。别名映射在并行改写前记录，不在并行 worker 中写日志。原始来源并集记录不变，细化仍是经过校验的查询投影。

查询及 `.provenance.json` 新增 `ff_mux_absorptions`、`ff_bit_maps`、`ff_alias_maps`，同时保留 `techmap_bit_maps`。`ff_bit_maps.applied` 说明是否采用细化；复杂控制不满足条件时为 `false`，并给出 `unsupported_ff_control_rewrite`。递归反馈树等尚未捕获逐位控制证据的路径不生成可应用的切片证据，继续保留原来源与整体缺口。旧 v2 日志仍可读，但缺少新证据的历史结果不会自动取得这项细化。

可复现示例包含两个有各自同步复位、使能和初始化的状态位：

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source examples/register_bits.v --top demo --out runs/register-bits-demo
python3 -m yosys_flow query runs/register-bits-demo \
  --source examples/register_bits.v --line 4
python3 -m yosys_flow extract runs/register-bits-demo \
  --source examples/register_bits.v --line 4 --out runs/register-bits-demo/q0.v
```

第 4 行只关联驱动 `q[0]` 的最终 FF，第 5 行只关联 `q[1]`；第 2 行初始化关联两个状态位。导出保留所选位的时钟、使能、复位和初始化。重复运行请换输出目录。

新增验收覆盖独立使能、独立同步复位、独立 mux 语句、共享使能、部分位拆分、异步复位叠加负使能、连续复位/使能重写和逐位初始化。还检查错误位号、缺失吸收证据的拒绝，以及复杂使能的保守回退。这仍是第三步中寄存器优化的一个已验证子集；常量传播、复杂反馈控制、更多跨对象优化和算术内部门的位关系尚未完成，RTL 支持范围保持不变。

## 验收方式

`tests/test_selection.py` 用最终模块的输出端点、独立逻辑锥和明确应排除的对象定义预期集合，不以记录器自己报告的来源集合生成预期。逐项检查选择结果、共享、精度和导出。还检查节点对应篡改、techmap 位交换、原始记录不被细化修改，以及旧 v2 日志保留旧区域精度。

原版与插桩版的完整 JSON modules 对照、整体等价检查及导出重读等价继续执行。通过这些检查不会将未捕获的转换认定为已覆盖。
