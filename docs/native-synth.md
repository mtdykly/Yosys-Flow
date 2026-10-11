# 默认 synth 原生追踪试验

`run --flow synth` **实际执行固定版本默认 `synth -top <top>`，包含 ABC 和其后的优化**。输出 `after.v/.il/.json` 已是与工艺库无关的细粒度通用门和触发器网表。没有增加 `keep`、改为 `-noabc`、提前结束或暗中 flatten。

这次打通的是默认终点的**部分来源追踪和候选导出**，不是 M4 精确定位验收完成。`coverage=partial`、`end_to_end_exact=false` 继续保留；普通 `proc` 的结果是目标信号候选，ABC 是实际调用区域的候选。

## 使用

构建依赖、固定源码摘要及本地安装方法见 [native-m1.md](native-m1.md)。需要独立原版 Yosys `0e82bbefe` 做对照，原生版使用构建时指定的外部 `yosys-abc`。

```bash
make native NATIVE_JOBS=4
python3 -m yosys_flow run --flow synth \
  --source examples/proc.v --top demo --out runs/my-synth-run

# 寄存器更新对应最终 FF，保留时钟、复位、使能和初始化
python3 -m yosys_flow query runs/my-synth-run --source examples/proc.v --line 13
python3 -m yosys_flow extract runs/my-synth-run \
  --source examples/proc.v --line 13 --out runs/my-synth-run/state.v

# ABC 区域候选：报告中明确列出候选精度和来源
python3 -m yosys_flow extract runs/my-synth-run \
  --source examples/proc.v --line 5 --out runs/my-synth-run/logic.v

# 直接连接和常量也可独立导出
python3 -m yosys_flow extract runs/my-synth-run \
  --source examples/proc.v --line 15 --out runs/my-synth-run/direct.v
python3 -m yosys_flow extract runs/my-synth-run \
  --source examples/proc.v --line 16 --out runs/my-synth-run/zero.v
python3 -m yosys_flow validate runs/my-synth-run
make test-native
```

`--flow m1` 继续保留原来的 `proc -noopt; opt_merge` 检查点。新增 `--flow m1-opt` 执行 `proc; opt_expr; opt_clean; opt_merge; opt_clean`，便于独立检查常量替代和删线；它不是整个 coarse 阶段。未指定 `--flow` 仍运行 M0 插件实验。

## 新增变换证据

| 实际位置 | 记录的关系 | 限制 |
| --- | --- | --- |
| `opt_expr` 的目标 cell 处理循环 | 原 cell 版本 → 新建 cell 或原输出的替代连接；实际退休及原因 | 跨 cell 常量、极性和控制依赖未完整接入，报告 `partial_pass:opt_expr` |
| `opt_clean` 的实际别名代表选择点 | 在清空旧连接、删除 wire 前，迁移已记录连接的左右端点 | wire 删除后无法保留的位显式退休；不按名字猜测新来源 |
| `opt_dff` 的寄存器处理循环 | 原 FF → 改写或拆分的 FF/输出连接 | 吸收的 mux/控制来源和寄存器位合并尚不完整 |
| `alumacc` 的模型合并、替换位置 | 实际合并的算术模型来源；目标模型 → ALU/MACC 及辅助单元 | 多输入 ALU 组为 `cell_group_candidate`；不表示逐位依赖精确 |
| `techmap` 的目标 cell 映射循环 | 原 cell → 在原模块内实际生成的单元；覆盖直接 simplemap 和模板展开 | 保留父对象候选精度，不证明每个拆分位的精确来源；辅助模板不混入原设计 |
| ABC 的实际 cell 提取和重新导入 | 实际移走的输入成员 → 本次调用重新生成的门和输出连接 | `abc_region_candidate`；未接入 ABC 内部逐节点改写 |

对象仍使用显式 ID/版本和退休事件。cell 结构检查点用于审计；结构观察不能单独建立新的来源。原生记录限定在被追踪的设计中，前端尚未挂入 design 的临时模块显式登记，技术映射/ABC 的辅助 design 被排除。

`connection_state` 保留原始 LHS、原位偏移和连续修订号。最终解析使用仍存在的信号代表，导出报告仍能展示原信号。`retired_cells` 和 `retired_connections` 给出历史退休证据；**退休可能意味着被替换，并不等于整条 RTL 无影响**。没有最终来源绑定的空结果继续保留未知含义。

## ABC 区域及复现证据

每次真实 ABC 运行都有独立 `invocation_id`，保存：

- 从原设计实际提取的 cell 版本集合，及其已记录来源并集；未提取的 FF 不加入该集合。
- `ys__n<id>` 对应的原信号和真实输入/输出边界；边界与送入 ABC 的 BLIF 端口逐项比对。
- `yosys.jsonl.abc_<n>.input.blif`、`.output.blif`、`.script`，归档于运行目录，加入产物 SHA256 清单。
- 实际 ABC 可执行文件路径、SHA256 和版本输出；映射资源随 Yosys 真实输入文件清单保存摘要。
- 重新导入的 cell/连接引用该调用和输入成员；继续记录之后的优化和最终网表。

例如两条独立语句 `assign y=a&b; assign z=c^d;` 可能同属一次 ABC 调用。当前查询任意一条会返回两个门作为区域候选，并报告另一条来源。这个案例专门加入精度回归，**不得把区域全部门的返回升级为精确语句定位**。导出器按这个已声明的候选集合复制最终网表，不自动向上游扩张扇入锥。

## 验收和支持范围

`tests/test_synth.py` 检查实际执行默认命令与 ABC 后优化、细粒度终点、真实区域成员/边界、寄存器排除/包含、逐位初始化、直接连接/常量、常量替代、无用表达式删除、宽加法的 `alumacc → techmap → ABC` 路径，以及错误来源/精度/连接修订的拒绝。

运行时逐项比较原生与独立原版的最终 JSON modules（包括属性），再做等价检查；检查器重放生命周期、来源并集和连接修订并核对最终网表。导出另做参数/位连接/边界检查和 Verilog 重读等价；状态导出沿用独立的初始化核对。**这些检查不代替源码选择的独立预期断言，也不证明尚未插桩的变换。**

已验证单模块基本组合逻辑、宽加法、组合条件过程、普通/带常量复位和使能的寄存器、正负沿、初始化、直接连接/常量。最终状态白名单为通用 DFF/DFFE/SDFF/SDFFE/SDFFCE 门族；其他状态形式拒绝发布结果。层次实例、memory、generate、循环、函数/任务和手工实例化仍由前端明确拒绝。其余可解析结构不自动纳入已验收范围。

后续优先补齐 process 的动作/分支精度，以及 `opt_expr`、`opt_dff`、`opt_reduce`、`opt_muxtree`、位宽/共享等跨对象来源；随后扩展 FSM、memory、实例上下文和 ABC 内部变换。没有实际语义钩子的 pass 即使恰好不改变本例，也仍报告 `uncovered_pass`，不会因最终等价就认定来源覆盖完整。
