# 原生开发检查点（proc 与基础优化）

本文说明保留的 `--flow m1` 检查点。新增基础优化检查点 `m1-opt` 和实际默认综合入口 `synth`，见 [native-synth.md](native-synth.md)；它们仍保留部分覆盖和候选精度。

这是 `default-synth-plan.md` 中 M1 的**部分实现**，用于验证原生前端、process 与导出接口。真实终点锁定为：

```yosys
read_verilog -sv -defer ...
hierarchy -check -top <top> ...
proc -noopt
opt_merge
```

`-noopt` 暂时跳过 `proc` 最后的 `opt_expr`，便于隔离尚未覆盖的变换。没有执行 `opt_clean`、完整 `opt`、coarse、techmap 或 ABC。这个终点不是默认 `synth` 最终网表，也不表示 M1 的全部验收已经完成。

## 构建

保留原版固定版本 Yosys（提交 `0e82bbefe`）用于独立对照。原生版安装在仓库的 `build/native/install`，不覆盖系统安装。默认 `make` 与 `make native` 都构建这套原生版。

构建需要 Python 3.10+、C++20 编译器、CMake ≥ 3.28、Ninja、Bison ≥ 3.6、Flex ≥ 2.6、m4、zlib 开发文件，以及现有 `yosys-abc`。Slang、Tcl、readline/libedit 和 libffi 在本构建中关闭。ABC 只是链接配置中的外部工具，此试验流程不调用它。

```bash
make native NATIVE_JOBS=4
make test-native
make test
```

`tools/build_native.py` 按 `native/lock.json` 下载完整提交固定的 Yosys 源码和必要子模块，先验证归档 SHA256，再应用固定锚点插桩；锚点或哈希不匹配即失败。`build/native/prepared.json` 保存插桩输入摘要和修改文件摘要。通过 `#line` 保持上游位置，避免 `NEW_ID` 中的 C++ 行号因插桩改变。已有未修改的源码文件保持时间戳，支持增量编译。

系统 CMake 较旧时可本地安装：

```bash
python3 -m pip install --target build/native/python cmake==3.31.6
```

脚本优先使用这个本地 CMake，也可直接调用 `python3 tools/build_native.py --cmake /path/to/cmake --abc /path/to/yosys-abc`。Ubuntu 环境缺少生成器且不希望安装到系统时，可用 `apt-get download bison flex m4 libsigsegv2` 下载本机发行版的包，然后以 `dpkg-deb -x <包> build/native/toolchain` 解压。脚本识别这里的工具和运行库；这些宿主构建工具不属于固定的 Yosys/子模块源码锁。

## 运行和导出

```bash
python3 -m yosys_flow run --flow m1 \
  --source examples/proc.v --top demo --out runs/proc-demo

# 寄存器更新：返回带使能/复位相关逻辑的状态候选集合
python3 -m yosys_flow query runs/proc-demo --source examples/proc.v --line 13
python3 -m yosys_flow extract runs/proc-demo \
  --source examples/proc.v --line 13 --out runs/proc-demo/state.v

# 纯连接、常量赋值：允许零 cell 的有效模块
python3 -m yosys_flow extract runs/proc-demo \
  --source examples/proc.v --line 15 --out runs/proc-demo/direct.v
python3 -m yosys_flow extract runs/proc-demo \
  --source examples/proc.v --line 16 --out runs/proc-demo/zero.v
python3 -m yosys_flow validate runs/proc-demo
```

`--native-yosys` 指定插桩版；`--yosys` 指定独立原版，两者不能是同一文件。日常运行省略 `--flow` 即执行 `synth`；检查点须显式选择。`query` / `extract` / `validate` 统一读取原生 JSONL v2，旧 v1 会明确提示已退役。

查询使用运行时的旧源码坐标，报告已改变或缺失的输入文件。列查询优先选择覆盖指定位置/范围的最小运算表达式；仅按行查询则选择该范围内记录的语句与表达式。来源选择不沿电气连接递归扩张完整扇入锥。

## 已接入的位置与精度

| 位置 | 实际记录 | 当前限制 |
| --- | --- | --- |
| `AST::process`、`cloneInto` | 简化前的运行内语句/表达式 ID、文件、行列、模块及复制保留 | 不是完整 AST 简化事件链；预处理、参数依赖仍有缺口 |
| `AstNode::genRTLIL` | 当前表达式/语句作用域内实际新建的 cell | `precision=cell` 表示该生成位置的关联粒度，不是完整端到端正确性证明 |
| 连续赋值、过程赋值生成点 | LHS/RHS 位连接；过程动作到原始目标信号及显式临时信号映射 | 不能将随后无产物的源对象推断成“已消除” |
| `proc_mux`、`proc_dff` | 生成对象到实际参与的赋值位、分支和临时路由 | 普通路径与已捕获的异步复位路由为 `process_action`；未知路径回退候选，完整过程语义仍有缺口 |
| `proc_init`、敏感沿生成 | 初始化动作及时钟/复位沿的来源参与状态候选集合 | 不是时钟、初始化语义的完整依赖分析 |
| `proc_dlatch` 的无锁存器路径 | 组合过程生成的直接连接 | 锁存器本身拒绝接入 |
| 普通 `opt_merge` | 实际合并位置记录双方来源并退休旧对象 | 不自动覆盖其他 `opt` 子 pass |
| `Pass::call`、cell 创建/删除 | 父子步骤、cell 生命周期、版本与结构检查点 | 检查点标为 `structural_observation`，不能代替语义转换证据 |

源码身份保存在编译器内部侧表/AST 字段中，没有添加 RTLIL `keep` 或其他优化约束。对象退休时清除句柄，避免地址复用继承旧身份。所有入口共用 JSONL v2；结构观察不代表完整的语义变换证明。

每条结果同时报告 `coverage=partial`、对象的 `precision`、`cell_level_refs`、`candidate_cell_refs`、`end_to_end_exact=false`、来源边和覆盖缺口。来源可按最终 cell/连接的 `ref` 反查。空结果 `no_final_object_with_recorded_binding` 表示没有足够的最终绑定证据，**不表示没有影响或已被删除**。

## 已验证与拒绝的输入

已用原生 Yosys 集成实验验证：单模块连续组合表达式、同一行/同一语句的表达式选择、直接连接和常量、完整赋值的组合 `always` 与 `if/else`、同一过程不同目标信号、正/负沿 `$dff`、常量异步复位 `$adff`、通过反馈 mux 表达的使能、常量 `initial`、多位初始化位序，以及相同表达式的来源合并。

层次实例、memory、generate、循环、函数/任务、手工实例化在前端明确拒绝。最终状态单元只接受 `$dff` / `$adff`；latch、复杂置位/复位、非恒定异步装载等状态形式拒绝发布运行。更复杂组合语法即使 Yosys 接受，也不因此纳入已验收输入清单。`--instance` 只允许顶层模块，不能混合实例上下文。

## 三层验证与时序边界

1. **记录检查：** 校验步骤嵌套、对象版本/退休、源 ID 引用、合并来源并集、cell 生命周期重放；独立 `after.json` 核对 cell 参数、方向、位连接和记录的连接。逐项比较原版相同流程结果，包括属性和初始化，再做原版/插桩版功能检查。这里尚未证明 process 的每一处内部修改均有语义事件。
2. **来源检查：** `tests/test_native.py` 预先约定操作符和原 RTL 输出端点，检查应包含与应排除的对象。例如选 AND 分支不加入 OR 运算，同一 process 的 `y` 不混入 `z`，选寄存器更新不递归加入其数据输入的组合逻辑。候选集合通过这些断言仍不等于复杂 process 的精确定位已经完成。
3. **导出检查：** 直接复制所选 cell 的参数、端口和连接；确定真实边界；导出的 Verilog 重读后检查。连接可独立导出，不依赖存在 cell。选中 FF 时保留其状态定义、极性、复位值、时钟/使能连接及相关 Q 位的 `init`；未选中 FF 的 Q 作为边界输入，报告 `external_state_drivers`，不复制或初始化外部状态。

时序格式检查在验证副本上运行 `clk2fflogic; equiv_simple -seq 5; equiv_induct -seq 5`，不改动发布的 FF 网表。`clk2fflogic` 使用采样时钟事件，对异步输入采用 negative hold time；`equiv_induct` 证明可观察状态相同一段时间后的不分歧，不能独自证明初始状态一致。因此另行逐位比较 Verilog 重读后的初始化，并保留 `<名称>.roundtrip.json` 作为验证产物。报告写明这些语义与限制；不能将导出等价检查替代来源选择检查。

动作身份、覆盖/裁剪证据和分支选择的新增实现与验收见 [process-provenance.md](process-provenance.md)。

## 后续 M1 必做项

继续细化异步复位路径及尚未覆盖的过程变换；补齐覆盖/删除/常量化原因及完整的 AST 简化路径；接入 `opt_expr`、`opt_clean` 和锁定的基础 `opt` 子 pass。每项都需要独立预期来源集合和完整导出回归，然后才可升级 M1 的完成状态。M2–M4 继续按计划推进到默认 `synth` 的细粒度通用门级终点。
