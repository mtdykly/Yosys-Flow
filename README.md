# Yosys-Flow

目标是通过一次插桩全量综合记录**原始组合 RTL → 默认 `synth` 最终通用门级网表**的对应关系，生成区域契约，为后续局部综合与替换提供依据。

来源记录和局部替换目前支持普通组合子模块的多层实例。默认 `synth -top` **保留层次**；显式 `--flatten` 才请求展平，仍尊重 `keep_hierarchy`。时序、memory、generate、循环、函数/任务等不在这条记录与替换流程的支持范围。另有独立的 [EQY 时序等价预检查](incremental/sequential.md)：在明确的单时钟、初始化/同步启动复位条件下，证明完整新版 RTL 可以复用整份旧网表；不提供时序局部替换。

## 目录

| 路径 | 内容 |
| --- | --- |
| [provenance/](provenance/README.md) | 已有来源记录、原生 Yosys/ABC 钩子、查询、导出、区域契约及合并；含原来的源码、测试、例子和阶段文档 |
| [incremental/](incremental/README.md) | 从旧记录和完整新版 RTL 自动发现变化赋值，提取局部功能、推导边界并验证替换；模块实例作为后备选区 |
| [benchmarks/anubis/](benchmarks/anubis/README.md) | 唯一新增的外部测试集：DLX 替换实验，以及五个设计的完整 NOC 宏清单、原始组合模块证明评测及独立时序预检查 |
| `yosys_flow/__init__.py` | 仓库内命令兼容入口；实际实现位于 `provenance/yosys_flow/` |
| `build/`、`runs/` | 原位置保留的工具链缓存与运行产物，均不提交 |

所有命令在仓库根目录执行。原来的 `python3 -m yosys_flow` 接口保持可用，脚本与例子路径增加 `provenance/` 前缀。旧运行中的源码坐标仍指向当时的路径；移动例子不会重写历史 manifest，验证会如实报告旧源码路径已移动。

## 构建与来源查询

依赖 Python 3.10+、Make、C++20、CMake ≥ 3.28、Ninja、Bison、Flex、m4、zlib 开发文件，以及固定版本 `0e82bbefe` 的独立原版 Yosys/ABC。详见[构建说明](provenance/docs/native-m1.md#构建)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source provenance/examples/hierarchy.v --top demo --out runs/hierarchy-example
python3 -m yosys_flow query runs/hierarchy-example \
  --source provenance/examples/hierarchy.v --line 4 --instance demo/u0/inner
```

[来源记录使用说明](provenance/README.md)包含查询、诊断、导出、契约生成与合并命令。

## 旧记录 + 完整新版 RTL

```bash
python3 -m incremental auto \
  --base-run runs/old-run \
  --new-source path/to/new.v \
  --out runs/automatic-edit
```

只需提供旧综合运行目录和完整新版 RTL（多个文件可重复 `--new-source`）。顶层、参数、宏和综合模式继承旧记录；无需填写修改行号、实例、契约、局部代码或连接表。工具比较归档的 `frontend.il` 与新版 `proc` 前表示，在发生变化的单模块或叶模块中找出赋值目标及表达式变化，再查询旧来源并生成区域契约。旧源码文件可以已被修改或删除。

默认先用 **Yosys `miter + sat` 检查变化叶模块是否功能不变**：接口与实例上下文未变时，比较全部输出，并检查输出有定义。全部修改证明等价后，直接逐字节复用旧网表，不生成 patch，也不执行新版 `synth`。只证明部分等价、找到反例或超时时，其余修改继续走**变化赋值 → 旧网表区域 → 新版依赖切片**。证明默认每项 5 秒、共享总预算 15 秒；可用 `--no-equivalence-precheck` 关闭。

过程体先由 Yosys `proc` 正确展开，提取时保留目标所需的条件、覆盖优先级与数据依赖；能够复用的旧信号作为输入边界。无法绑定的输出沿真实外部消费者扩区，再尝试模块及父实例后备方案。不是每改一行都重综合整个模块，也不承诺数学上最小区域。

ANUBIS 的 `bypass_noc1`、`compare_noc2` 等价改写已验证可直接复用全部旧门；`compare_local9` 的真实修改会留下 SAT 反例并继续局部综合。ALU 表达式的 4 位组合适配例中，XOR 改 OR 只替换 4/12 个门；同一 `always` 的 `if/case` 分支修改也能保留独立输出。完整新版的前端与 `proc` 仍是在线成本；实际拼接的路径保留完整新版 `synth` 和全部输出 SAT 验收，直接复用路径采用结构上下文及叶模块证明。

报告同时保存差异、区域扩展原因、自动生成的 `patch.il/patch.v`、契约和绑定。详细步骤、边界限制和 ANUBIS 复现命令见 [自动增量入口](incremental/automatic.md)。

## 局部综合与替换实验

```bash
python3 -m incremental demo --out runs/incremental-demo
```

默认验证保留层次和显式展平两种模式。例子为四条 8 位组合通道，共 **152 个最终门、128 个输入位、72 个输出位**；只修改 `chip/u1/core` 的 `(a & b) ^ c` 为 `(a | b) ^ c`。

实验先从基准记录选择并合并两条语句的区域契约，再独立综合 [patch.v](incremental/examples/four_lanes/patch.v)，替换其中 **16 个门，保留其余 136 个门及引脚连接**。边界输出用 8 个缓冲门接回旧信号；当前例子得到 160 个门，未对整份拼接设计再优化。最后用全部顶层输出的 SAT miter 与 [new.v](incremental/examples/four_lanes/new.v) 的完整默认综合结果比较，同时验证输出 Verilog。

这是显式输入接口的已验证实验；新版局部 RTL、修改实例及边界功能由例子提供。上面的 `auto` 入口另行自动推导变化赋值、局部功能与绑定；仍未实现共享消费者自动拆分。实际替换时，候选来源、缺失边界、组合环或整体等价失败会拒绝交付。整份旧网表的等价复用不需要从候选中选门，但仍不改变来源精度；ABC 完整因果历史尚未补齐。

具体步骤、产物和限制见 [incremental/README.md](incremental/README.md)。完整回归运行 `make test`，任何跳过均视为失败。

## ANUBIS 真实修改实验

判断原始 NOC 是否影响实际设计，使用 `anubis-nochange --upstream-top`：按上游文件清单、真正顶层和实际实例参数展开，先比较完整有效设计，再做局部诊断及必要的顶层证明。局部反例不能冒充顶层反例。本轮 30 个宏中 26 个结构一致、4 个未知、0 个确认的顶层反例；这不是全部证明通过。原始 X、初始化、异步复位语义保持不变，生产预算不变。旧的无标志模式和 `--with-sequential` 保留为模块边界诊断，其 35 配置及五个 FPU 局部反例不代表原始 CPU/FPU 顶层结果。详见 [范围与结果](benchmarks/anubis/README.md)。

```bash
# 按上游实际顶层与配置复核全部 NOC
python3 -m incremental anubis-nochange --upstream-top --repeats 1 \
  --frontend-timeout 300 --out runs/anubis-noc-upstream-final

python3 -m incremental anubis --out runs/anubis-demo
# 仅运行真实功能修改，保留层次
python3 -m incremental anubis --cases compare_local9 --mode retained --out runs/anubis-local9
# 自动模式：不给算法修改位置、局部规范或绑定
python3 -m incremental anubis --automatic --out runs/anubis-automatic
```

测试使用 ANUBIS 的条件改写、比较表达式改写、符号位条件修改、译码选择修改和缺省赋值删除；再用其 ALU 的 32 位加法分支作为独立算术邻居。每项测试实例化两份组合模块，只修改 `u_target`，保留 `u_other` 和顶层汇总。新旧默认 `x` 输出统一适配为 0，`casex` 通配位保持原样，修改和行号映射全部归档。

显式模式六项用例 × 两种模式中，四项完成替换，两项正确拒绝锁存，六项来源或连接证据不足而阻塞。自动模式增加模块内切片验收，包括完整 `compare_local9`（231 个旧门替换 110 个、复用 121 个）及明确标注的表达式、过程和位宽适配例。部分比较器和嵌套条件仍受 ABC 候选证据限制。详见[用例、结果及限制](benchmarks/anubis/README.md)。

已有 `fraig` 清理的实际节点复制/删除记录、按位交错的 BDD 输入排序，以及阶段见证缺失等诊断。查询、记录重放、证明、契约、绑定、局部综合、拼接和验证均单独计时。等价预检查的开关成本比较保存在完整回归的 `equivalence/comparison.json`；等价改写可省去综合工作，但不保留其潜在 PPA 改善，也没有对任意电路的加速承诺。
