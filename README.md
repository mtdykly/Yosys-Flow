# Yosys-Flow

目标是通过一次插桩全量综合记录**原始组合 RTL → 默认 `synth` 最终通用门级网表**的对应关系，生成区域契约，为后续局部综合与替换提供依据。

目前支持普通组合子模块的多层实例。默认 `synth -top` **保留层次**；显式 `--flatten` 才请求展平，仍尊重 `keep_hierarchy`。时序、memory、generate、循环、函数/任务等不在当前支持范围。

## 目录

| 路径 | 内容 |
| --- | --- |
| [provenance/](provenance/README.md) | 已有来源记录、原生 Yosys/ABC 钩子、查询、导出、区域契约及合并；含原来的源码、测试、例子和阶段文档 |
| [incremental/](incremental/README.md) | 从旧记录和完整新版 RTL 自动推导修改实例、局部代码及绑定，进行局部综合、替换和验证；保留显式输入接口 |
| [benchmarks/anubis/](benchmarks/anubis/README.md) | 唯一新增的外部测试集：固定版本的 ANUBIS DLX 宏修改、哈希和适配说明 |
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

只需提供旧综合运行目录和完整新版 RTL（多个文件可重复 `--new-source`）。顶层、参数、宏和综合模式继承旧记录；无需填写修改行号、实例、契约、局部代码或连接表。工具比较归档的 `frontend.il` 与新版展开后的 `proc` 前表示，自动选择变化的模块实例，从新版提取相应局部表示，利用旧记录的实例端口和实际信号绑定生成 patch 与契约。旧源码文件可以已被修改或删除。

首轮粒度是**变化的模块实例，必要时扩大到父实例**。单模块内的修改可能重综合整个模块，尚不保证语句级最小区域。新增输入依赖必须找到旧网表中的实际外部驱动；证据不足或无法确定连接时报告阻塞。候选生成只综合局部 patch，之后完整综合新版作为全输出 SAT 验证参考；全部检查通过才发布 `incremental.v/json`。

报告同时保存差异、区域扩展原因、自动生成的 `patch.il/patch.v`、契约和绑定。详细步骤、边界限制和 ANUBIS 复现命令见 [自动增量入口](incremental/automatic.md)。

## 局部综合与替换实验

```bash
python3 -m incremental demo --out runs/incremental-demo
```

默认验证保留层次和显式展平两种模式。例子为四条 8 位组合通道，共 **152 个最终门、128 个输入位、72 个输出位**；只修改 `chip/u1/core` 的 `(a & b) ^ c` 为 `(a | b) ^ c`。

实验先从基准记录选择并合并两条语句的区域契约，再独立综合 [patch.v](incremental/examples/four_lanes/patch.v)，替换其中 **16 个门，保留其余 136 个门及引脚连接**。边界输出用 8 个缓冲门接回旧信号；当前例子得到 160 个门，未对整份拼接设计再优化。最后用全部顶层输出的 SAT miter 与 [new.v](incremental/examples/four_lanes/new.v) 的完整默认综合结果比较，同时验证输出 Verilog。

这是显式输入接口的已验证实验；新版局部 RTL、修改实例及边界功能由例子提供。上面的 `auto` 入口已自动推导模块实例级修改与绑定，但尚未自动提取最小语句区域，或拆分“同一个旧输出、修改后不同消费者需要不同功能”的情况。候选来源、缺失边界、组合环或整体等价失败会拒绝交付。来源记录仍为部分覆盖，ABC 完整因果历史尚未补齐。

具体步骤、产物和限制见 [incremental/README.md](incremental/README.md)。完整回归运行 `make test`，任何跳过均视为失败。

## ANUBIS 真实修改实验

```bash
python3 -m incremental anubis --out runs/anubis-demo
# 仅运行真实功能修改，保留层次
python3 -m incremental anubis --cases compare_local9 --mode retained --out runs/anubis-local9
# 自动模式：不给算法修改位置、局部规范或绑定
python3 -m incremental anubis --automatic --out runs/anubis-automatic
```

测试使用 ANUBIS 的条件改写、比较表达式改写、符号位条件修改、译码选择修改和缺省赋值删除；再用其 ALU 的 32 位加法分支作为独立算术邻居。每项测试实例化两份组合模块，只修改 `u_target`，保留 `u_other` 和顶层汇总。新旧默认 `x` 输出统一适配为 0，`casex` 通配位保持原样，修改和行号映射全部归档。

显式和自动模式各有四项保留层次实验完成替换并通过全输出 SAT 验证；删除默认赋值的两种模式正确拒绝锁存行为。另外六项模式组合仍因来源或连接证据不足而阻塞，报告为 `blocked`，不计为成功。自动模式自行发现目标叶实例并提取代码，尚未自动提取最小修改区域。详见[用例、结果及限制](benchmarks/anubis/README.md)。

本轮同时增加 `fraig` 清理的实际节点复制/删除记录、按位交错的 BDD 输入排序，以及阶段见证缺失等诊断。查询、记录重放、契约、绑定、局部综合、拼接和验证均单独计时；尚未证明增量流程具有时间收益。
