# 组合过程动作和分支来源

默认入口仍是完整 `synth`。组合过程转换的来源依据是：为每个生成的过程赋值建立独立动作身份和逐位证据，沿实际临时赋值、分支和 mux 生成路径传递来源。`m1`、`m1-opt` 作为检查点，便于检查 ABC 前的集合。

## 使用与预期

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run --flow m1 \
  --source provenance/examples/process_precision.v --top demo --out runs/process-checkpoint
python3 -m yosys_flow query runs/process-checkpoint \
  --source provenance/examples/process_precision.v --line 10
python3 -m yosys_flow extract runs/process-checkpoint \
  --source provenance/examples/process_precision.v --line 10 --out runs/process-checkpoint/outer.v

python3 -m yosys_flow run \
  --source provenance/examples/process_precision.v --top demo --out runs/process-synth
python3 -m yosys_flow query runs/process-synth \
  --source provenance/examples/process_precision.v --line 13
python3 -m yosys_flow extract runs/process-synth \
  --source provenance/examples/process_precision.v --line 14 --out runs/process-synth/overwritten.v
```

第 10 行在 proc 检查点只选中输出端的外层 mux，不混入 `t` 控制的内部 mux。该固定版本的 `proc -noopt` 还会保留 `s ? inner : x` 的中间 mux，第 6、8 行关联它和最终外层 mux；测试使用实际端口关系确认集合。

第 13 行的目标赋值被第 14 行覆盖。完整 synth 后查询它没有最终对象，并返回 `frontend_overwritten` 证据；第 14 行不会继承它的来源。第 17 行只关联 `partial[3:2]` 的连接，第 18 行关联低两位常量。

## 记录方式

- `process_action_detail` 保存动作身份、实际 LHS/RHS、原始目标和逐位证据引用。
- `process_node` 记录动作位、分支条件、编译器生成的临时信号路由；输入引用及角色来源的并集必须与节点来源一致。
- `process_binding` 把实际生成的 cell 版本绑定到过程证据；创建时暂为 `process_pending`，完成绑定后才允许发布最终结果。
- `process_connection_binding` 为直接连接和常量提供同样的过程证据。
- `process_discard` 记录前端覆盖、`proc_prune` 裁剪和不可达 case 的目标位及原因。

动作位保存 `statement`、`data`、`target` 角色。外围条件保存在 `enclosing_controls` 中作为上下文，不自动注入每个内部 cell；控制来源在实际比较器和 mux 生成位置加入。

临时路由可能先被引用、后被生成，记录器在完成相应过程转换后解析证据；`captured_step_id` 保存捕获路由的实际步骤。只跟随前端明确生成并登记的过程临时信号，不沿任意 RTL 网线扩张上游逻辑。模块顶层参数复制、CaseRule 移动和删除也维护这些元数据。

查询新增 `process_actions`、`process_proof_nodes`、`process_discards`、`action_level_refs`；导出说明包含动作和完整支撑过程证据，包括所选 cell 的其他共享来源。正查返回的对象必须存在从所选源码起点到对象的连续证据路径。

## 精度和限制

`precision=process_action` 表示该对象具有动作/分支生成关系，粒度仍是 cell 或连接位。这不表示后续每个最终门已经获得精确的位级来源。

前端简化、复杂过程转换和跨对象优化仍有缺口，`end_to_end_exact=false`、整体 `coverage=partial` 保留。遇到未绑定的过程临时信号或未知动作会产生缺口，不能升级为动作级证据。读取器沿证据输入传播候选精度，拒绝把含候选来源的绑定标成 `process_action`。

进入 ABC 后，已有输入输出节点证据的映射使用 `abc_output_cone_candidate`，未知映射回退 `abc_region_candidate`。即使 proc 检查点排除了内部 mux，完整 synth 的同一区域候选仍可能包含它；查询同时保留上游过程证据和 ABC 区域信息。这次没有完成 ABC 内部精确定位。

`process_discard` 的含义是某次目标赋值被删除，不能单独证明整条语句或它计算的值没有其他使用者。例如阻塞赋值的值可能已用于另一个目标。没有最终对象时仍保留 `no_final_object_with_recorded_binding` 和 `empty_result_means_no_effect=false`，不把局部裁剪升级为全局消除证明。

## pmux 子网表的边界语义

更细的查询可以只选中 `$pmux`，把比较器输出作为边界输入。外部输入不保证互斥：多个选择位同时为 1 时，`$pmux` 的结果未定义。导出这种切片使用 `write_verilog -noparallelcase` 显式保留该情况，并在等价验证中启用 `-undef`，报告 `pmux_semantics`。验证按 Yosys 的未定义值模型检查已定义行为，不附加选择信号互斥假设，也不为通过验证而扩大所选集合。

## 验证范围

`provenance/tests/test_process.py` 检查嵌套分支、条件与数据角色、同目标覆盖、部分位覆盖、默认赋值、独立位条件、重复 case 分支退休、共享 pmux 与比较器排除、完整 synth 精度继承，techmap 辅助设计的隔离，以及损坏证据与错误精度升级的拒绝。原生和独立原版最终 JSON 对照、等价验证和导出重读检查仍然执行。

仅组合逻辑的原生 JSONL v2 继续使用，旧时序来源支持已退役。优化事件、techmap 位关系和 ABC 节点交接见 [来源选择验收](selection-acceptance.md)。
