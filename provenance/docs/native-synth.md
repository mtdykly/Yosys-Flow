# 组合逻辑的默认 synth 原生追踪

默认 `run` 实际执行固定版本 Yosys `synth -top <top>`，包含 techmap、ABC 及后续优化，输出为与工艺库无关的细粒度组合门网表。没有添加 keep、跳过 ABC 或修改综合策略。

## 运行

构建见 [build.md](build.md)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run --source provenance/examples/comb.v --top demo --out runs/comb-demo
python3 -m yosys_flow query runs/comb-demo --source provenance/examples/comb.v --line 4
python3 -m yosys_flow extract runs/comb-demo --source provenance/examples/comb.v --line 4 --out runs/comb-demo/selected.v
python3 -m yosys_flow extract runs/comb-demo --source provenance/examples/comb.v --line 8 --out runs/comb-demo/direct.v
python3 -m yosys_flow extract runs/comb-demo --source provenance/examples/comb.v --line 9 --out runs/comb-demo/zero.v
python3 -m yosys_flow validate runs/comb-demo
```

已有目录和导出文件不覆盖；重复实验请换路径。默认不必写 `--flow synth`，`m1` / `m1-opt` 仅用于组合过程与基础优化检查。

## 范围与记录

支持单模块及普通组合子模块的嵌套实例、数值参数派生、连续组合表达式、算术、比较、选择、直接连接/常量、完整赋值的 `always @*` / `always_comb` 和条件过程。敏感沿、初始化和推导出的状态单元被拒绝；组合环路由 `check -assert` 检查。memory、generate、循环、函数/任务、实例数组、门原语、黑盒和 inout 仍不支持。`--instance` 使用原始实例路径，如 `demo/u0/inner`。

默认 synth 保留层次；`run --flatten` 才显式展平。两种模式的来源查询、区域契约与导出均保留实例身份，说明和命令见 [hierarchy.md](hierarchy.md)。保留层次的查询采用只读电气实例视图，并在首次运行中独立验证它与原层次设计等价，原始综合产物保持不变。

| 位置 | 实际记录 | 限制 |
| --- | --- | --- |
| 前端、proc_mux、过程清理 | 语句/表达式身份、动作位、条件、覆盖、裁剪及临时路由 | 完整前端简化与过程语义仍不完整 |
| hierarchy、flatten | 已展开实例及参数/端口绑定；实际复制的对象、来源及逐位 wire map | 普通组合实例；不代表支持 generate 等更复杂展开 |
| proc_dff/proc_dlatch 的无状态路径 | 组合过程直接连接及其动作来源 | 不记录或支持 FF/latch，推导出状态即拒绝 |
| opt_merge、opt_expr、opt_clean | 合并、替代/删除、连接别名迁移；mux 吸收取反、双取反、互补输入和替代连接的实际前提 | 尚未覆盖 opt_expr 全部分支，前提只沿已记录的赋值链获取 |
| opt_share | 实际共享的运算单元、原 mux、新 mux、存活/删除对象和端口关系；共享门组保留全部来源 | 独立的 `share` pass 仍未接入；不声称组内逐门独占归属 |
| wreduce | 实际裁剪前后端口/参数、保留对象与替代连接；包括补零、符号扩展和 mux 位裁剪 | 不据此宣称所有算术内部位关系已完整建立 |
| opt_reduce、opt_muxtree、alumacc | 子归约合并、mux 树裁剪、部分模型合并/替换 | 部分覆盖，多来源组可能仍为候选 |
| techmap | 实际派生和输出位对应 | 算术内部位关系未完整建立 |
| ABC | 内部命令网络、入口到 fraig 的实际复制链及合并绑定、AIG 替换、dch 等价/choice、输出门副本、Nf cut；单门/多门及部分因式分解门组证明；大区域及宽切点使用 ROBDD | 受符号证明预算限制；完整重写历史尚未建立，门组不代表逐门独占归属，未证明对象返回候选 |

原生记录限定在用户设计；技术映射/ABC 辅助 design 被隔离。对象显式退休，连接在删线前迁移；结构观察不能建立新的语义来源。已删除时序专用模块与证据字段，旧状态日志不再支持。默认 synth 自身的 pass 仍照常执行。

## 验证与结果

运行器检查生命周期、来源引用、最终 cell/连接与 JSON 的一致性，再比较独立原版同流程的完整 JSON modules，并做组合等价检查。导出复制实际所选集合，生成边界端口，另做 Verilog 重读等价；包含 `$pmux` 的开发检查点保留其未定义值语义。

`retired_cells` / `retired_connections` 说明历史对象退休，不等于整条语句无影响。共享与候选来源保持可见；当前结果继续报告部分覆盖和 `end_to_end_exact=false`。来源选择的独立验收见 [selection-acceptance.md](selection-acceptance.md)，后续工作见 [roadmap.md](roadmap.md)。

`optimization_rewrites_v1` 日志增加实际规则事件 `optimization_rewrite`：输入对象版本、常量/取反前提、输出对象、保留/退休清单、替代连接以及完整的前后端口与参数。端口数组按 RTLIL 位偏移排列，裁剪后的位位置和补位连接可直接核对。它不记录未实际采用的共享候选，也不把普通电气扇入全部并入来源。重放核对这些清单、版本和来源，并拒绝缺项或损坏；这仍不是任意优化规则的形式证明。

对象缺口另存入 `object_gaps`，带事件序号、具体对象和所在步骤，并沿证据关系传播。查询的 `source_gap_ids` / `object_coverage_gaps` 给出影响当前 cell 或连接的断点；`source_path_status=incomplete` 表示已知缺口，`not_established` 表示尚未建立端到端完整性，不能解读为完整。`abc_internal_cut` 仍只描述局部功能对应，不能抹掉上游缺口。SQLite 和 gzip 都保存这些共享字段，旧格式日志仍可重放，新代码会使旧查询索引失效并要求重建。

固定 ABC 的构建、内部钩子、精度规则和验收见 [abc-internals.md](abc-internals.md)。
