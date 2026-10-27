# 区域契约与在线替换计划

完整 v1 契约是来源查询结果面向独立审查和导出的交付形式。它绑定一次插桩默认 `synth` 的输入、日志和最终网表，保存区域成员、全部边界输出、共享使用者、来源证据和使用条件。生成、验证和合并均读取同一次归档记录，不调用 Yosys/ABC，不重新综合原始设计。

本阶段支持单模块及普通组合子模块的层次，包含默认保留层次和显式 flatten；契约保留实例身份，并可合并不同实例的区域，见 [hierarchy.md](hierarchy.md)。独立的 [incremental 实验](../../incremental/README.md) 已消费这些契约进行局部综合、拼接和整体验证；`auto` 从归档旧表示与完整新版分析变化赋值及必要依赖，提取局部功能和绑定，并以模块实例作为后备范围。尚未实现任意语句的最小增量区域，也没有把现有 ABC 局部功能对应升级为完整因果历史。

## 完整审查与在线计划的分工

`incremental auto` 使用 v2 `replacement_plan`，默认写入 `replacement-plan.json`。它仍描述选区、边界、共享义务和旧区域验证结果，但用旧对象、事件和归档身份引用证据，避免将相同来源和父契约逐层复制。替换阶段复用这一计划，不再另写完整契约副本；新旧边界分析、全部输出义务和最终验证仍执行。

首次建档的 `verified-index.sqlite3` 供后续按需加载已验证来源状态，旧 gzip 索引保留兼容读取。区域选择依赖本次修改，因此具体计划在新版到来后生成；索引和与旧设计相关的来源整理可前置。完整契约并非自动增量的必需文件，但边界和共享分析仍不可省略。

以下 `contract` / `contract-merge` 命令继续默认输出完整 v1 契约，适用于审查、导出及兼容已有显式实验。`incremental run --contract` 接受完整契约或轻量计划，都会对照其旧归档验证。新格式不会把 `verified_old_region` 升级成未来任意修改都安全，也不改变来源覆盖声明。

## 使用方法

以下命令使用仓库已有的两级组合例子；每次使用新的运行目录和输出文件：

```bash
python3 -m yosys_flow run --source provenance/examples/chain.v --top demo --out runs/contract-chain

python3 -m yosys_flow contract runs/contract-chain \
  --source provenance/examples/chain.v --line 3 --out runs/contract-chain/upstream.contract.json
python3 -m yosys_flow contract runs/contract-chain \
  --source provenance/examples/chain.v --line 4 --out runs/contract-chain/downstream.contract.json

python3 -m yosys_flow contract-validate runs/contract-chain \
  runs/contract-chain/downstream.contract.json

python3 -m yosys_flow contract-merge runs/contract-chain \
  runs/contract-chain/upstream.contract.json \
  runs/contract-chain/downstream.contract.json \
  --out runs/contract-chain/union.contract.json

python3 -m yosys_flow extract runs/contract-chain \
  --contract runs/contract-chain/union.contract.json \
  --out runs/contract-chain/union.v
```

`contract` 接受与源码查询相同的行列范围或 `--object`。`contract-merge` 至少需要两份不同契约，要求完全相同的基准运行；支持重叠、包含关系和已经合并的契约。`extract --contract` 验证契约后导出其全部成员，仍执行原有的网表检查和 Verilog 重读等价；这一步需要原版 Yosys，但不会重新综合原始 RTL。来源选择参数不能与 `--contract` 混用。

文件不覆盖已有路径。契约正文包含父契约，父文件移动后仍可验证合并结果，但完整原始运行目录仍是证据来源。内容哈希用于身份和完整性校验，不是签名或安全证明；即使重新计算哈希，修改边界、函数、使用者或来源证据也会被重放验证拒绝。

## 保存的信息

| 字段 | 含义 |
| --- | --- |
| `baseline` | 运行 ID、顶层、终点、manifest 摘要、全部产物及原始输入摘要；禁止跨基准混合 |
| `derivation` / `request` | 原查询条件，或父契约；保留原查询命中的 cell、连接和来源 ID |
| `region` | 实际契约成员，以及从最终对象记录重建的门类型、参数和连接 |
| `expansion` | 为补齐已记录共同来源门组而增加的对象；显式列出，不递归加入扇入锥 |
| `boundary` | 实际单比特信号、别名、驱动、使用者及直连端点身份；位序和极性按真实网表绑定 |
| `outputs` | 每一个边界输出的布尔函数、全部外部使用者、区内贡献对象、记录来源及证据引用 |
| `other_recorded_sources` | 原查询范围之外的已记录来源，提示共享功能义务 |
| `provenance` | 支持成员的来源边、事件序号、ABC 局部证明/候选和覆盖缺口；不重新编造来源链 |
| `conditions` | 等价实现替换需要保持的接口条件、功能修改需要另验的条件、区域外返回路径 |
| `verification` | 旧区域功能验证结果、门组闭合、候选对象和未绑定的使用者 |
| `union` | 父区域重叠对象、每个旧端口在新边界中的去向、复用证据引用及重新验证标记 |

例如，按对象查询只选中 `multilevel.v` 门组中的一个门时，契约会补入该已记录组的其他最终成员。`request` 保留最初对象，`expansion.added_cell_refs` 列出扩展；原 `query` 和 `diagnose` 的选择规则不变。无法找到最终成员的组继续标记未闭合，不凭电气锥猜测补齐。

## v2 轻量替换计划

自动计划使用 `schema_version=2`、`report_type=replacement_plan`、`proof_rule=recorded_replacement_plan_v2`。保留 `contract_id` 作为内容身份，绑定格式不变。

| 与完整 v1 的差别 | v2 保存方式 |
| --- | --- |
| 父契约反复嵌套 | 父节点仅保存 `contract_id` 和推导条件；验证时据此重建 |
| 每个门复制完整来源说明 | 保留来源 ID、对象引用、类型、实例、精度及消费者 |
| 完整来源边和事件逐层复制 | 引用旧视图的 `source_ids`、`supporting_edge_indices`、`record_event_ids` |
| ABC 相同证明在多个位置重复 | 以调用身份和证据索引引用；需要审查时解析归档 |
| 全部输出函数重复保存 | ROBDD 或结构化布尔 DAG 放在 `outputs` 一次，验证摘要指向该证据 |

`boundary`、全部 `outputs`、`conditions`、电气切割、请求/门组闭合及重放门函数仍保留，候选和覆盖缺口不删除。`contract-validate` 和 `extract --contract` 均接受 v2；需要完整导出说明时才展开证据。校验会对照同一次旧归档重建成员、函数、边界及引用，逐字段核对；重新封装内容哈希不能掩盖删门、删消费者或替换证据。

## “全部输出验证”具体证明什么

验证器独立列举所选门的跨边界连接，包括中间结果被区域外门使用的情况；函数验证覆盖每一个输出端口，而不只检查某个门组根或顶层输出。直连和常量也有函数说明，常量的外部端点按已记录 LHS 绑定，不能把相同常量值误认为同一根电气信号。

`region_functions.py` 用固定 Yosys `simcells.v` 的二值通用组合门语义，把实际边界输入抽象成彼此独立变量。它分别计算**重放得到的最终对象状态**和**最终网表所选门**的各个输出函数，并比较。小区域保存可重放的 ROBDD：终端 0/1，其他节点含输入端口及 low/high 分支；较大区域或 BDD 超限时改用下述结构化布尔 DAG。不支持的门、活跃 x/z 或未完成的函数检查仍产生 `not_proven` 或拒绝，不能仅凭图结构不同宣称功能不等价。

这个复制证明的范围是旧区域的实际功能及接口。**它不证明该函数就是某条原始 RTL 语句的全部语义，也不证明来源记录已经覆盖所有变换。** `source_completeness_proven=false` 和 `complete_causal_history=false` 继续保留；通常的记录契约还保留每个输出的 `rtl_function_of_this_output_proven=false`。只有下述独立 `source_cut` 证明通过后，才把该输出对应源码切割函数的证明标记设为真，仍不声称某条语句独占这些门。输出复制检查不能替代来源选择验收。

`verified_old_region` 表示全部旧边界输出通过函数检查、已记录门组闭合、已绑定相关连接使用者，并具有足够的来源依据：成员原有精度满足要求，或者附加的 `source_cut` 证明已完成。候选成员仍保留原精度和候选列表；该功能证明不补造 ABC 因果历史。没有完成这种附加证明的候选、未证明输出或缺失成员仍保存 `partial_old_region`。`contract-validate` 的 `valid=true` 只表示文件中的声明与原记录及当前验证规则一致，一份诚实报告缺口的 partial 契约也可以验证有效。

### 候选区域的独立旧源码证明

`derivation.kind=source_cut` 引用父计划，从同一归档的 `frontend.il` 重建旧源码函数，使用已记录的实例端口、展平复制及 wire 身份绑定实际边界。旧表示经过 `proc`、`opt_expr`、`techmap`、清理后供证明使用，不执行 ABC 或完整 `synth`，也不读取新版全量参考网表。所有实际输出及其对应源码锚点都进入 miter，SAT 同时证明相等和二值有定义；输入以记录的实际绑定为依据并作为独立变量，不把新旧内部信号仅凭同名当作等价前提。一个输入的多个旧锚点只有经结构化函数证明等价后才共同绑定。锚点缺失、反例、X、超时和不支持的结构不构成成功证明。

`verification.source_region_function_proof` 保存归档哈希、工具哈希、锚点、被比较电路的摘要和结论。验证计划时从归档重新构造并证明，不能仅修改 `verified` 字段或重新封装内容哈希。单条工具命令墙钟上限 60 秒、SAT 求解上限 50 秒；外层性能实验还受每作业 360 秒总上限约束。这些证明调用属于在线选区成本。

较大的复制区域或 BDD 耗尽预算时，全部输出检查使用 `boolean_dag_v1`。相同结构化布尔项可证明函数相等；不同项只返回未证明，不宣称功能不等价。这项检查仅验证重放对象与复制电路，不能取代 `source_cut` 的源码对应证明。通常的查询仍按原有来源精度报告对象。

对于保持原功能的替换，未来的新实现必须满足所有已认证输出函数、外部端点和输入极性绑定，并检查单驱动和无组合环。对于改变功能的 RTL 编辑，还必须给出新功能要求、处理其他共享行为并检查新增依赖。当前所有契约继续 `safe_for_incremental_reuse=false`、`end_to_end_exact=false`，不会对未提供的修改签发安全保证。

## 合并规则

合并首先验证全部父契约和基准身份，再取成员并集，去重重叠对象，并复核已记录门组闭合。以并集在原始最终网表上的真实连接重新切割：

- 父区域之间的连接变成内部连接时，旧输入端口可消失。
- 中间结果仍被任何区域外对象使用时，必须保留输出义务。
- 直连输出与新纳入的驱动合并时，保留它在新输出端口上的原连接身份。
- 来源边和实际 ABC 证明可复用；父契约的函数证书属于旧边界，新边界的全部输出必须重新计算和验证。
- 未覆盖来源继续保留，扩大区域不会自动把候选升级成完整因果证明。

如果区域存在“输出经过外部逻辑又返回某个输入”的路径，`conditions.outside_return_paths` 给出具体外部门。这种区域可以记录和导出，但未来替换必须检查依赖变化是否引入组合环。将中间逻辑并入区域后，会重新计算这些路径，不能沿用旧条件。

契约绑定的是原始版本。原始源码被修改或移走时，旧记录仍可验证，CLI 会报告 `changed_or_missing_base_inputs`；这不表示新源码已经满足旧契约。合并不接受另一轮综合的契约，即使源码文本或门函数看起来相同。

## 自动流程的门选区与额外输入

`derivation.kind=source_cells` 的字段为 `kind`、`selector`。它重放原源码查询，选取其中的 `final_cells` 并补齐已记录门组，保留查询来源 ID；连接对象不作为选区成员。默认 `selection` 的含义不变，仍包含查询选中的连接。

这项区分是为了请求“来源关联的门区域”：源码查询可能同时命中内部常量填充、观察别名等连接，它们不等于门选区的电气输出。`source_cells` 的实际输入、**所有门输出及其外部消费者**仍由最终网表重新切割、独立审计并验证函数；不能通过该类型删掉某个真实门输出的使用者。它不声明覆盖该源码语句的全部连接语义。自动流程对没有门关联或旧常量变更的修改种子转入模块后备，真正直连/常量变门仍需消费者重绑定，当前不能借此绕过。

自动增量流程可能发现新版使用了旧区域原本未使用的信号。`derivation.kind=extend_inputs` 内嵌已验证的父契约和排序去重的 `input_bits`；只接受旧网表中有唯一实际外部驱动的整数信号位，不增加旧门或来源声明。新边界行标记 `reason=new_edit_dependency`，保存实际驱动、别名和使用者，重新验证所有旧输出函数。新输入对旧功能可以无影响，但新 patch 必须明确绑定它并通过完整新版验收。

验证器会从父契约和旧记录重算扩展，不能靠修改哈希伪造端点。`extract --contract` 保留这些输入；合并纳入其驱动后重新内部化，不保留已变成内部连接的输入端口。外部返回路径与组合环仍需检查，扩展不会把候选来源升级为已证明。该操作是自动流程的可重放派生记录，尚无单独 CLI 修改任意契约的入口。

## 验收与下一阶段

`provenance/tests/test_contracts.py` 覆盖两级链、共享分支、`factoring.v`、`multilevel.v`、32 输入共享链、直连、相同值不同端点的常量、候选算术来源、外部返回路径、重叠/嵌套合并和实际 Verilog 导出。负例包括遗漏输出/使用者、错误函数/极性、伪造扩展或来源证据、跨运行合并及损坏父契约；重新计算文件哈希也不能绕过验证。离线测试禁止外部进程并核对原运行目录不变。

`provenance/tests/test_plans.py` 另验收轻量计划与完整契约的一致性、引用证据和篡改拒绝；`make test` 生成本次完整回归报告。v1/v2 是仍受支持的数据格式版本，与性能实验目录的 v4 编号无关。

契约和普通层次案例复用现有 ABC 实际关系与局部证明；新增的是 hierarchy/flatten 的实例、端口及真实复制证据，已有 ABC 来源缺口继续显式报告。后续发现某个明确输出关联被内部转换断点阻挡时，再补相应 ABC 钩子。层次契约的 `hierarchy` 字段声明实例视图及原始实例上下文；边界别名可反查原模块局部信号。共用模块定义不意味着共用物理门。
