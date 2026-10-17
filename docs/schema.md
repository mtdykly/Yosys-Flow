# 原生 JSONL v2 格式与不变量

主流程 `synth` 和开发检查点 `m1`、`m1-opt` 共用 `schema_version=2`，由 `NativeTrace` 读取。这是项目自定义格式，不是 Yosys 标准事件格式。旧 M0 的 v1 读取支持已退役，历史格式说明和读取器由 Git 保存。当前 profile 为 `native-combinational`，features 包含 `combinational_only`；旧时序来源记录不再支持。

每行含连续 `seq`、当前 `step_id` 和 `record_type`。`run_begin` 声明 schema 与 `run_id`；`run_end` 必须是最后一条。步骤具有父子关系，读入前还要检查 manifest 状态、运行 ID 和产物摘要。只有完整、通过验证的运行允许查询和导出。

## 记录类别

| record_type | 主要内容 |
| --- | --- |
| `run_begin` | schema、运行身份及记录器 profile |
| `source` | 简化前分配的 AST 语句/表达式 ID、文件、行列范围、模块和种类 |
| `step_begin` / `step_end` | 实际 pass、参数、步骤和父步骤 |
| `cell_create` | 从 0 开始的 cell 版本、来源、精度、可选派生输入及操作 |
| `cell_state` | 前一版本、递增版本、实际结构状态、来源及观察时机 |
| `cell_contribution` | 实际供体贡献到目标的新版本；供体仍可存活 |
| `techmap_bit_map` | 原 cell 输出位与实际生成对象输出位对应、双方信号 |
| `abc_import_node` | 实际输出 BLIF 节点与导回端口映射 |
| `merge` | 两个实际输入版本与保留对象的新版本、来源并集 |
| `cell_remove` | 退休的实际 cell 版本及原因；ABC 提取时附调用身份 |
| `assignment` | 稳定连接身份、左右位向量、来源、精度和可选派生输入或 `cloned_from` |
| `connection_state` | 迁移后的位向量、原始偏移、递增修订号及原因 |
| `process_node` / `process_action_detail` | 带来源角色的动作位、条件、临时路由证据与具体赋值身份 |
| `process_binding` / `process_connection_binding` | 实际生成的 cell/连接到过程证据的绑定 |
| `process_discard` | 覆盖、裁剪或不可达分支的目标位及原因 |
| `process_action` | 实际组合过程动作、目标信号及来源 |
| `abc_region` | 实际调用成员、边界、来源、归档网络和工具路径 |
| `coverage_gap` | 未覆盖或部分覆盖的阶段、生成位置 |
| `checkpoint` / `run_end` | 实际存活 cell 引用及连接修订号；结束记录另含真实输入文件清单 |

cell 引用形如 `native:cell_17@2`，连接引用形如 `native:connection_18`。编号仅在一次运行内有效，必须与 `run_id` 一起解释。cell 状态包含 module/name/type、参数、端口方向及连接；完整属性另通过独立最终 JSON 网表对照检查。信号向量是 LSB-first 的 `{"wire":"\\name","offset":0}` 或 `0/1/x/z` 常量字符串，内部 offset 不等于源码总线下标。

## 来源、生命周期与精度

来源 ID 必须先声明。cell 创建引用不可重复，版本连续递增；变更和合并输入必须存活，旧版本退休，删除不能重复。合并后的来源为输入并集；候选精度不能在随后的结构观察中提升为精确精度。新记录头声明 `process_action_proofs` 与 `conservative_precision_merge`，合并取更保守的精度；读取旧 v2 仍按其旧合并约定校验。

新建对象和连接可带 `inputs` / `operation`，指向实际目标处理位置捕获的输入版本。替换可能先退休原对象再创建新对象，因此这些派生输入可以是历史版本；读取器检查引用存在、来源并集和精度继承。

`cell_state.capture=structural_observation` 仅声明结构观察，不是新的来源因果证据。`before_instrumented_rewrite`、`instrumented_rewrite`、`before_removal` 说明观察时机，也不能独自证明跨对象来源完整。

`process_action` 表示有动作/分支生成证据的关联，不是完整端到端或最终位级精度。`process_pending` 仅允许出现在等待绑定的中间 cell，不能出现在最终对象中。`process_signal_candidate` 表示过程目标信号候选，`cell_group_candidate` 表示实际多输入模型候选，`abc_region_candidate` 表示 ABC 调用区域候选，`abc_output_cone_candidate` 表示按保留输出边界细化后的候选。`abc_internal_cut` 表示有内部映射证据的局部功能切点证明，并非完整重写因果历史；`abc_internal_joint_cut` 表示通过独立边界验证的多门整体对应，`attribution_scope=joint_group`、`per_gate_source_exact=false`。`cut_proven_refs`、`joint_cut_proven_refs`、`candidate_cell_refs` 分别列出单门证明、多门整体证明和未证明的候选对象，组内保留来源并集。`cell` 表示已记录的 cell 级关联粒度，不是整个端到端来源的正确性证明。查询和导出继续报告 `coverage=partial`、`end_to_end_exact=false` 及具体缺口。

过程证据引用必须已声明；节点来源等于输入节点及角色来源并集，绑定必须引用存活的待绑定 cell 并递增版本。`captured_step_id` 标记实际捕获步骤，角色上下文与因果输入分开。查询逐对象检查连续来源路径。具体字段、范围及未定义值导出语义见 [process-provenance.md](process-provenance.md)。

## 连接及模块生命周期

`connection_state` 使用稳定连接 `ref` 和从 1 连续递增的 `revision`，记录当前 `lhs` / `rhs` 与原位 `offsets`。位只能保留或退休；原始 LHS 由创建事件保存。原生记录器在清除旧连接和删线前按实际别名代表迁移端点，不在查询时按名字猜测。

原因包括 `opt_clean_alias_representative`、`signal_removed`、`module_removed`、`module_renamed`。参数重新派生时，被丢弃模块中的 cell、过程和连接显式退休，重命名更新连接的模块名，避免句柄重用继承旧元数据。这些记录不表示完整参数依赖追踪。

`hierarchy` 把参数派生模块复制为实际顶层时，在复制位置传递 cell、连接及过程目标来源。cell 使用 `operation=hierarchy_parameter_clone` 与原对象版本建立关系；连接通过 `cloned_from` 指向原连接，读取器检查其来源、精度及位宽一致。复制对应关系来自实际模块复制操作，不是在最终网表中匹配名字猜测来源。

新检查点保存所有连接的修订号；读取器仍支持旧原生 v2 检查点缺少此字段的情形。最终活动连接必须能在 `after.json` 中解析且两端别名相同，退休连接不再解析已删除模块或 wire。

## ABC 证据

`abc_region` 使用唯一 `invocation_id`，保存实际提取的 `inputs`、来源并集、原信号 `signals` 及 `input_ports` / `output_ports` 索引、可执行路径和归档文件名。成员必须与同一步骤带该调用 ID 的退休事件一致；重新导入的 cell/连接带 `abc_invocation`，输入必须与区域相同，精度只能为 `abc_region_candidate`。

新日志通过 `input_nodes`、退休事件的 `abc_outputs`、`abc_import_node` 和生成对象的 `abc_node` / `abc_boundary` 保存节点对应。读取器先校验原始区域记录，再建立查询用的 `abc_associations`；原始日志不被修改。节点 fanin、门类型/端口及导回端口须与归档网络和首次对象状态一致。旧日志没有这些证据时保持旧精度。

`techmap_bit_map` 引用真实派生父对象及子对象，端口位范围和双方信号必须与捕获状态一致。组合单元的输出位对应不等于算术内部门的位级来源，查询仍保留实际父对象来源。

新日志声明 `abc_internal_v3`、`abc_region.internal_trace_schema=3`，额外归档 `input.blif.flow.jsonl`。这是 ABC 内部格式的版本；外层原生 JSONL 仍为 v2。内部日志头必须与声明一致；旧 `abc_internal_v1` / schema 1、`abc_internal_v2` / schema 2 仍可读取。事件包含命令、网络快照、AIG 替换、Nf 选中 cut 和实际对象转换；schema 2 增加 `proved_equivalence`、`history_copy`、独立作用域的 `history_network` 和每命令事件计数。等价只记录成功采用的结构/SAT 合并及代表/choice，仿真候选不算证明。

schema 3 新增以下记录和约束：

- `entry_copy`：`before` / `after` 明确引用两个网络，`links` 保存 `[原对象 ID, 新 literal]`。四种 reason 按顺序为 `entry_strash`、`entry_strash_survivors`、`entry_ntk_to_aig`、`entry_aig_to_gia`。复制链必须连续；存活表只能保留未变对象，其余入口转换必须覆盖所有非输出对象。验证输入对应和每个复制函数，组合极性得到入口到 GIA 的实际路径。
- `proved_equivalence.entry_snapshot` 和 `fraig_sweep_copy.entry_snapshot`：只在同一个实际入口 GIA manager 中记录。读取器检查网络结构一致，并要求入口等价根在实际 sweep 复制表中汇合；函数相同但未被采用的临时节点不能替代这条关系。
- `mapped_clone`：保存 `snapshot`、`old`、`new`，记录 `&put` 中实际复制的同极性输出门。`gia_to_ntk.clones` 保存 `[原门 ID, 副本 ID, 复制事件 seq]`，读取器检查两边记录完整、结构相同及转换前没有变化，再将选中 cut 关联到副本。
- `command_end.entry_copies` / `mapped_clones`：分别校验该命令的实际记录数量。

`abc_internal` 报告验证与回退详情，包括 `verified_entry_copies`、`entry_lineage.scope=entry_to_fraig`、`mapped_clones`、`equivalence_groups`；旧格式缺少入口链时 `entry_lineage.available=false`，不补造路径。超过证明规模等早期回退可能尚未建立 `entry_lineage`，应结合 `fallback_reason` 读取。`abc_associations` 附单门证明事件编号或 `joint_groups` 的原始成员、组内门、边界、真实映射事件和阶段功能见证。以实际 fraig 等价根为依据的组另含 `basis=recorded_fraig_equivalence` 和 `entry_lineage`，后者保存两个入口根的复制路径、等价事件、sweep 事件及汇合 literal。它只证明入口到 fraig 的连续关系；最终门组仍按局部功能对应验证，不表示下游完整因果历史。详见 [abc-internals.md](abc-internals.md)。

内部日志、实际输入 BLIF、输出 BLIF 和脚本加入 manifest SHA256 清单，输入边界还要与真实 BLIF 端口一致。工具摘要、实际输入和映射资源摘要保存在 manifest。区域内每条语句关联全部输出门仍只能算候选，不能替代 ABC 内部逐节点变换证据。

## 三层验证与导出报告

1. 记录检查：引用、顺序、步骤嵌套、版本、来源并集、连接修订和实际检查点一致；最终 cell 和活动连接与 `after.json` 一致。
2. 来源选择检查：测试用独立预期的操作符、输出端点、应包含与排除的对象检验选择；候选范围和精确定位分别验收。
3. 导出检查：复制所选 cell 参数、方向、位连接和属性，保留内部连接与边界；Verilog 重读后做组合等价检查。

运行器还比较独立原版相同流程的完整 JSON modules，再执行等价检查。`validate` 重新检查哈希、重放和最终网表，读取已保存的等价结果，不重新启动形式工具。哈希用于发现意外修改，不是签名或任意恶意日志真实性的证明。

`.provenance.json` 记录运行身份、最终网表摘要、选择条件、来源事件、精度、边界、ABC 证据摘要及导出检查。`boundary` 中 `original_bit` 是原 JSON 电气信号 ID；`aliases` 带 wire 名、LSB-first offset 和原声明下标；`drivers/users` 与 `external_drivers/external_users` 区分全体和集合外端点。内部选中 cell 之间的连接不因来源赋值记录被额外提升为边界端口。

`mode=source_association`、`upstream_cone_included=false` 表示没有递归加入上游逻辑。`last_recorded_pass` 保存本次真实终点；`safe_for_incremental_reuse=false` 继续保留。导出格式等价不证明来源精确，也不证明可安全替换逻辑。
