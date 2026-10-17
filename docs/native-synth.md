# 组合逻辑的默认 synth 原生追踪

默认 `run` 实际执行固定版本 Yosys `synth -top <top>`，包含 techmap、ABC 及后续优化，输出为与工艺库无关的细粒度组合门网表。没有添加 keep、跳过 ABC 或修改综合策略。

## 运行

构建见 [native-m1.md](native-m1.md)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run --source examples/comb.v --top demo --out runs/comb-demo
python3 -m yosys_flow query runs/comb-demo --source examples/comb.v --line 4
python3 -m yosys_flow extract runs/comb-demo --source examples/comb.v --line 4 --out runs/comb-demo/selected.v
python3 -m yosys_flow extract runs/comb-demo --source examples/comb.v --line 8 --out runs/comb-demo/direct.v
python3 -m yosys_flow extract runs/comb-demo --source examples/comb.v --line 9 --out runs/comb-demo/zero.v
python3 -m yosys_flow validate runs/comb-demo
```

已有目录和导出文件不覆盖；重复实验请换路径。默认不必写 `--flow synth`，`m1` / `m1-opt` 仅用于组合过程与基础优化检查。

## 范围与记录

支持单模块连续组合表达式、算术、比较、选择、直接连接/常量、完整赋值的 `always @*` / `always_comb` 和条件过程。敏感沿、初始化和推导出的状态单元被拒绝；组合环路由 `check -assert` 检查。层次、memory、generate、循环、函数/任务和手工实例化仍不支持。`--instance` 只支持顶层。

| 位置 | 实际记录 | 限制 |
| --- | --- | --- |
| 前端、proc_mux、过程清理 | 语句/表达式身份、动作位、条件、覆盖、裁剪及临时路由 | 完整前端简化与过程语义仍不完整 |
| proc_dff/proc_dlatch 的无状态路径 | 组合过程直接连接及其动作来源 | 不记录或支持 FF/latch，推导出状态即拒绝 |
| opt_merge、opt_expr、opt_clean | 合并、替代/删除、连接别名迁移 | 跨对象常量与控制来源仍有缺口 |
| opt_reduce、opt_muxtree、alumacc | 子归约合并、mux 树裁剪、部分模型合并/替换 | 部分覆盖，多来源组可能仍为候选 |
| techmap | 实际派生和输出位对应 | 算术内部位关系未完整建立 |
| ABC | 内部命令网络、入口到 fraig 的实际复制链及合并绑定、AIG 替换、dch 等价/choice、输出门副本、Nf cut；单门/多门及部分因式分解门组证明 | 至多 10 个区域输入；完整重写历史尚未建立，门组不代表逐门独占归属，未证明对象返回候选 |

原生记录限定在用户设计；技术映射/ABC 辅助 design 被隔离。对象显式退休，连接在删线前迁移；结构观察不能建立新的语义来源。已删除时序专用模块与证据字段，旧状态日志不再支持。默认 synth 自身的 pass 仍照常执行。

## 验证与结果

运行器检查生命周期、来源引用、最终 cell/连接与 JSON 的一致性，再比较独立原版同流程的完整 JSON modules，并做组合等价检查。导出复制实际所选集合，生成边界端口，另做 Verilog 重读等价；包含 `$pmux` 的开发检查点保留其未定义值语义。

`retired_cells` / `retired_connections` 说明历史对象退休，不等于整条语句无影响。共享与候选来源保持可见；当前结果继续报告部分覆盖和 `end_to_end_exact=false`。来源选择的独立验收见 [selection-acceptance.md](selection-acceptance.md)，后续工作见 [default-synth-plan.md](default-synth-plan.md)。

固定 ABC 的构建、内部钩子、精度规则和验收见 [abc-internals.md](abc-internals.md)。
