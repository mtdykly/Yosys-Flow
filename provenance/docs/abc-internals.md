# ABC 内部证据与局部切点验证

默认 synth 使用固定 ABC 提交 `e026ed5380f3bdc3beea2ff9ffc23236fc549d5b`，它也是当前固定 Yosys 的 ABC 子模块版本。`make` 会校验归档摘要、构建内部插桩 ABC，再把它接入原生 Yosys。默认综合脚本和优化参数保持不变，独立原版 Yosys 仍用于网表对照和等价检查。

## 实际钩子

钩子由 `provenance/tools/build_abc.py` 插入，记录器位于 `provenance/native/abc/flow.c`。

| ABC 源码位置 | 捕获内容 |
| --- | --- |
| `CmdCommandDispatch` | 实际命令开始/结束、返回状态、命令完成时的 ABC 网络或 GIA 网络 |
| `Abc_NtkStrash` / `Abc_CommandAbc9Get` | 入口逻辑网到 strash 的实际 `pCopy`、清理后存活对象、NTK→AIG 的实际 `pCopy` 和 AIG→GIA 的 `iData`；保留 literal 极性 |
| `Aig_ObjReplace` | 真正替换之前的旧根、新根及极性和当时的 AIG；没有改写 ABC 的决策 |
| `Cec4_ManPerformSweeping`（默认 `&fraig -x`） | 结构合并、SAT 已证明且实际采用的代表替代；清理前的真实 `Value` 复制表 |
| `Dch_ManSweepNode` | 已证明的结构/SAT 等价代表关系；不把仿真候选当成证明 |
| `Dch_DeriveChoiceAigNode` / `Dch_DeriveChoiceAigInt` | 实际设置的代表、实际加入的 choice 链接、真实 `pData` 复制表 |
| `Nf_ManDeriveMapping` | 最终选中的映射根、输入叶、各自极性与库门编号；不把所有枚举过的 cut 当成实际结果 |
| `Abc_NtkFromCellMappedGia` | 实际 GIA literal 到 ABC 门对象的复制表 |
| `Abc_NtkLogicMakeSimpleCos` | `&put` 中为多个输出实际复制的同极性映射门；副本继承被选中 cut 的对应 |
| 逻辑网络转换到 netlist | 实际 ABC 门对象到导出网络对象的复制表 |
| `Io_WriteBlif` | 正常写出时的网络、门函数与实际信号名称 |

记录器仅在 `YOSYS_FLOW_ABC_TRACE` 开启时写文件；运行器只为插桩运行开启。它不修改节点的 `pCopy`、`Value`、遍历标记或名字，不增加 keep。每个网络快照有自己的编号，节点 ID 只能在对应快照中解释，不能跨网络按相同整数 ID 认定身份。

当前 ABC 内部日志为 schema 3；读取器仍支持原 schema 1/2。新日志中每条替代/等价关系引用自己所属的网络快照，复制事件明确区分两个网络和 literal 极性。schema 3 增加 `entry_copy` 和 `mapped_clone`，命令结束分别记录数量。读取器检查事件计数、孤立快照、实际函数、输入对应、入口复制链连续性和已采用合并；旧格式没有入口链时，不推断或补造这类证据。

每次调用归档 `input.blif.flow.jsonl`，连同输入/输出 BLIF 和原执行脚本一起加入运行产物哈希。内部日志缺失、截断、非法引用、接口或函数不一致会拒绝验证。命令快照是审计证据；它本身不是实际节点替换历史。

## 当前可证明的关联

`provenance/yosys_flow/abc_internal.py` 实现 `abc_internal_cut`，表示**经过内部证据校验的局部功能切点对应**。这是一种明确的关联规则，尚不能等同于完整的内部因果来源历史。

验证依次检查：

1. 实际命令与归档脚本一致，内部网络对象和引用合法。
2. 入口节点与输入 BLIF 的局部函数和连接一致，阶段接口及输出函数一致。
   schema 3 另重放入口的实际复制表，将原逻辑根绑定到 fraig 输入节点；成功等价关系还须在真实 sweep 复制表中汇合。仅函数相同但未采用的临时节点不能替代该关系。
3. 真实的 GIA → ABC 门 → netlist 对象转换在函数和极性上保持一致。
4. 待匹配的原逻辑根在各命令结果中有功能见证；见证仅说明函数存在，不将两个节点认定为同一对象。
5. 最终门确实来自记录的 Nf 选中 cut；它的实际输入对应原网络切点。
6. 将切点输入作为**彼此独立的变量**，用局部真值表或 ROBDD 验证原逻辑与最终门的局部函数相同，包含反相。

只有切点内部被覆盖的原 cell 贡献来源；边界输入只是电气依赖，不递归贡献全部上游来源。共享来源保留并集。结果附带阶段见证、选中映射事件和对象转换事件编号。

区域输入数不再是证明的硬限制。至多 10 输入使用完整真值表；更大的区域使用 ROBDD 校验阶段函数、极性及实际复制关系。局部边界单独选择真值表或 ROBDD，保留输入独立性。预算耗尽、不支持的脚本/网络、内部切点消失或无法证明的门，继续使用 `abc_output_cone_candidate`，必要时回退 `abc_region_candidate`。允许同一查询同时包含已证明切点和候选对象。[区域诊断与基线](abc-diagnostics.md) 包含大区域中的小目标及成本测量。

`cut_proven_refs` 列出单门局部切点证明通过的 cell，`joint_cut_proven_refs` 列出门组整体对应的 cell，`candidate_cell_refs` 列出仍未证明的候选 cell；三者不混用。若前序 pass 已有候选精度，不因 ABC 证明而消除。`abc_internal` 汇报实际替换数、`equivalence_events`、已验证等价与复制数、映射数、单门/门组证明数量及回退原因。schema 3 还报告 `entry_lineage`、`verified_entry_copies`、`mapped_clones`、`equivalence_groups`。`coverage=partial` 和 `end_to_end_exact=false` 仍保留。

## 验收例子

```verilog
wire t;
assign t = a & b;
assign y = t ^ c;
```

默认 ABC 可以把它映射成 NAND→XNOR。现在可分别选择第一条语句的 NAND 和第二条语句的 XNOR，导出第二条时以 NAND 的输出作为边界输入，并保留实际极性；不会为了得到表面上的 AND→XOR 修改综合结果。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run --source provenance/examples/chain.v --top demo --out runs/abc-chain
python3 -m yosys_flow query runs/abc-chain --source provenance/examples/chain.v --line 4
python3 -m yosys_flow extract runs/abc-chain --source provenance/examples/chain.v --line 4 --out runs/abc-chain/y.v
```

`provenance/tests/test_abc_internal.py` 还检查共享中间表达式、实际 AIG 替换、反查、边界导出、多级整体证明、未证明重写的保守回退和损坏证据。`provenance/tests/test_abc_scale.py` 验收 12/16/32 输入区域的小目标、预算回退及大区域中的证据损坏；`provenance/tests/test_boolean.py` 用穷举对照校验符号后端及独立切点。预期选择由 RTL 的输出端点及独立逻辑结构定义，不使用记录器自己的来源集合产生预期答案。

## 符号功能校验与独立切点预算

`provenance/yosys_flow/boolean.py` 实现约简有序二叉决策图（ROBDD），以规范化布尔函数判断相等和反相，不采用随机仿真或相似度。超过 10 输入时，不生成 `2^N` 位真值向量。跨阶段变量仍按归档的 PI 名称、实际转换/复制表绑定；对象 ID 相同不构成输入身份依据。历史快照内的关系使用其独立 PI 顺序，复制双方通过真实链接对齐。

门组边界超过 10 输入时也改用符号计算。其变量代表**相互独立的边界信号**，与区域主输入函数分属不同的 BDD 管理器。原逻辑必须在该独立域内与导出门组相等；例如互斥信号的 OR/XOR 虽然在原设计可达输入下相同，独立边界下不等，仍会拒绝该切点。保留内部边界、实际映射证据和阶段见证等原有要求不变。

每个符号管理器最多创建 100,000 个非终端节点，最多执行 1,000,000 次未缓存的 Shannon 展开；这是确定性的资源预算，不是运行时间承诺。`function_proof` 报告区域后端、状态、节点、计算量和预算；`local_function_resources` 报告独立切点尝试的资源，`local_proof_failures` 记录未完成的目标和原因。

- 区域级校验耗尽预算时，`fallback_reason` 为 `bdd_node_limit_exceeded` 或 `bdd_work_limit_exceeded`，本区域保留候选，不能发布依赖未完成校验的证明。
- 单个局部切点耗尽预算时，记录该尝试并跳过它，保留其他已完成证明；未证明对象的保守候选仍可能使某些查询扩大。
- 找到函数不一致或非法证据时仍拒绝验证，与资源不足的“未证明”分开处理。

本轮解决的是全区域输入数的硬门槛及宽门组边界的枚举门槛，**仍需校验整个区域的阶段函数**。BDD 可能随函数和变量顺序指数增长，复杂区域耗尽预算时仍会整体回退；尚未实现让小目标完全独立于其他复杂逻辑的验证分解或 SAT 后端。无需新增依赖、重新插桩或改变默认 ABC 脚本；已有完整记录可以离线重新查询。

## 多级重结合的精度里程碑

`provenance/examples/multilevel.v` 同时含三层链和独立输出：

```verilog
assign t = a & b;  // 第 3 行
assign u = t ^ c;  // 第 4 行
assign y = u ^ d;  // 第 5 行
assign z = e | f;  // 第 6 行
```

固定版本默认 synth 的结果是 AND、两个 XNOR 和独立 OR。当前验收如下：

| 查询 | 返回集合 | 精度 |
| --- | --- | --- |
| 第 3 行 | AND | `abc_internal_cut` |
| 第 4 行或第 5 行 | 两个 XNOR；排除 AND 和 OR | `abc_internal_joint_cut` |
| 第 6 行 | OR | `abc_internal_cut` |

第 4、5 行的中间切点已被重结合消除，系统返回**经过独立边界验证的多门整体对应**。两个门都保留这两条语句的共享来源，`attribution_scope=joint_group`、`per_gate_source_exact=false`；不能把组内每个门分别独占归给某条语句。

算法位于 `provenance/yosys_flow/abc_joint.py`。这类重结合证明从尚未证明的根出发，在已证明的内部门或区域输入处停止，要求至少存在一个已证明的内部边界。对最终组内每个门检查真实 Nf 映射和复制链；对原网络与最终局部网络，将边界抽象为相互独立变量后比较函数。原网络只取边界以内的来源，不收集边界上游。最终根还必须在各命令网络中有功能见证。下节的等价门组另以实际合并根作为依据，不能与本规则混为一谈。

`joint_groups` 保存组内门、原始成员、边界、根、映射事件和阶段见证。重叠组保留来源并集。真实等价/choice 历史另行校验和报告：这些历史记录尚未连成完整因果图，多门证明仍属于明确声明的局部功能对应规则，不能据此声称全部来源精确。

```bash
python3 -m yosys_flow run --source provenance/examples/multilevel.v --top demo --out runs/multilevel-demo
python3 -m yosys_flow query runs/multilevel-demo --source provenance/examples/multilevel.v --line 4
python3 -m yosys_flow extract runs/multilevel-demo --source provenance/examples/multilevel.v --line 4 --out runs/multilevel-demo/u.v
```

导出的两个门以 `t` 对应的最终电气信号、`c`、`d` 为输入，以 `y` 为输出。它是两条语句共同改写后的片段，不能作为原始 `u` 的独立实现。共享中间节点、嵌套条件、反查、边界导出均有独立包含/排除验收。

没有保留内部边界且没有下述已记录等价根依据、函数对应无法证明或证明预算不足时，继续返回候选。损坏等价关系的极性、choice、复制表或事件计数会拒绝验证。

## 等价表达式与因式分解门组

`provenance/examples/factoring.v` 使用两种等价写法和一个独立输出：

```verilog
assign y = (a & b) | (a & c);  // 第 2 行
assign z = a & (b | c);        // 第 3 行
assign independent = d ^ e;   // 第 4 行
```

固定版本默认 synth 最终保留共享的 AND、OR，以及独立 XOR。查询第 2 行或第 3 行，均选择 AND、OR，保留两行共享来源并排除 XOR。此前展开写法可能只关联到最终根门、遗漏共享 OR；现在通过已记录等价根建立整个门组的关联。查询第 4 行仍只选择 XOR。

新增证明分为两段，明确区分证据强度：

1. `abc_lineage.py` 重放 BLIF → strash → 清理后存活对象 → AIG → GIA 的真实复制表，组合所有反相标记。把原表达式根绑定到实际 `fraig_sat_merge` / `fraig_structural_merge`，并要求真实 `fraig_sweep_copy` 将两者映射到同一个 literal（含极性）。这一段是连续对象关系，报告 `scope=entry_to_fraig`。
2. `abc_joint.py` 验证这些原根到最终门组的局部功能对应。边界只允许区域输入，或两个原始锥中都存在且已证明保留的内部函数。只在其中一种写法出现的因子留在组内；每个最终门必须有实际选中 cut 和转换证据，两个原根均须通过独立边界函数及阶段见证检查。此段仍不是完整下游因果历史。

门组的 `basis=recorded_fraig_equivalence`，`entry_lineage` 保存原始根、四步复制路径、等价事件、sweep 事件及汇合 literal。共享来源取并集，使用 `abc_internal_joint_cut`、`attribution_scope=joint_group`、`per_gate_source_exact=false`。没有实际合并证据时，不能仅因为两个根函数相同就使用这条规则。

`&put` 为输出命名复制的门通过实际 `mapped_clone` 钩子继承映射对应；随后 Yosys `opt_merge` 合并副本时保留双方证据。如果没有这条复制关系，其中一个副本会退回候选，并影响合并后的结果。

`MakeSimpleCos` 还可能重建常量输出并删除旧常量节点。转换日志不把失效的常量复制表条目当作存活对象，也不据此推断新常量身份。整个区域化为 0/1 时仍可查询、导出零 cell 模块，ABC 连接保留候选精度。

```bash
python3 -m yosys_flow run --source provenance/examples/factoring.v --top demo --out runs/factoring-demo
python3 -m yosys_flow query runs/factoring-demo --source provenance/examples/factoring.v --line 2
python3 -m yosys_flow extract runs/factoring-demo --source provenance/examples/factoring.v --line 2 --out runs/factoring-demo/y.v
python3 -m yosys_flow extract runs/factoring-demo --source provenance/examples/factoring.v --line 3 --out runs/factoring-demo/z.v
```

`provenance/tests/test_abc_factoring.py` 验收两种写法、反相等价根、保留公共上游边界、独立输出排除、对象反查、实际输出副本和后续合并、导出重读等价。损坏极性、入口复制链、合并绑定、副本记录或计数均拒绝验证；还测试将已采用复制目标改成另一个函数相同的临时节点时必须拒绝。仅函数相等、没有记录合并的构造用例不能产生等价门组。

上游切点若被默认优化吸收，不能继续假设它存在，也不能保证单独导出其下游原语句；通过整体证明时报告共同来源，否则保留候选。本能力不覆盖所有因式分解；符号后端移除了区域输入数的硬门槛，但没有增加完整因果覆盖。

## 仍需补充的内部历史

当前不是完整 ABC 重写记录器。入口到 `&fraig -x` 的复制链及 Cec4 成功合并已连接，`&dch` 的成功代表与 choice 构造也有实际记录；fraig 后的清理、`dc2`、`&dch` 等重构及网络转换还没有组成从入口到出口的完整因果边。固定默认脚本调用的是 `dc2`；具体命令序列由归档脚本和读取器共同校验。`Aig_ObjReplace` 捕获的实际替换是其中一类，不能用它代表全部优化。

因此 `internal_rewrite_provenance=false` / `complete_rewrite_history=false` 继续表示完整历史未建立。近期以可靠的区域对应为验收目标：先复现失败案例或不能连续校验的断点，再按需连接清理/转换关系；每个新增钩子都须有相应选择或证据验收。允许门组整体对应，完整历史与组内逐门独占归属不作为层次扩展的前置门槛。证明规模和日志成本同步评估，候选要保留范围依据及原因。不能仅因为导出等价或局部切点成立，就宣称原 RTL 到最终网表全路径精确，更不能推导修改后可安全替换。
