# Yosys-Flow

目标是通过一次插桩全量综合记录**原始组合 RTL → 默认 `synth` 最终通用门级网表**的对应关系，生成区域契约，为后续局部综合与替换提供依据。

目前支持普通组合子模块的多层实例。默认 `synth -top` **保留层次**；显式 `--flatten` 才请求展平，仍尊重 `keep_hierarchy`。时序、memory、generate、循环、函数/任务等不在当前支持范围。

## 目录

| 路径 | 内容 |
| --- | --- |
| [provenance/](provenance/README.md) | 已有来源记录、原生 Yosys/ABC 钩子、查询、导出、区域契约及合并；含原来的源码、测试、例子和阶段文档 |
| [incremental/](incremental/README.md) | 使用旧契约、显式新版局部 RTL 和边界绑定的局部综合／替换实验 |
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

## 局部综合与替换实验

```bash
python3 -m incremental demo --out runs/incremental-demo
```

默认验证保留层次和显式展平两种模式。例子为四条 8 位组合通道，共 **152 个最终门、128 个输入位、72 个输出位**；只修改 `chip/u1/core` 的 `(a & b) ^ c` 为 `(a | b) ^ c`。

实验先从基准记录选择并合并两条语句的区域契约，再独立综合 [patch.v](incremental/examples/four_lanes/patch.v)，替换其中 **16 个门，保留其余 136 个门及引脚连接**。边界输出用 8 个缓冲门接回旧信号；当前例子得到 160 个门，未对整份拼接设计再优化。最后用全部顶层输出的 SAT miter 与 [new.v](incremental/examples/four_lanes/new.v) 的完整默认综合结果比较，同时验证输出 Verilog。

这是明确修改和绑定下的已验证实验。新版局部 RTL、修改实例及边界功能由例子明确提供；尚未自动提取 RTL 差分、生成任意新版区域，或拆分“同一个旧输出、修改后不同消费者需要不同功能”的情况。候选来源、缺失边界、组合环或整体等价失败会拒绝交付。来源记录仍为部分覆盖，ABC 完整因果历史尚未补齐。

具体步骤、产物和限制见 [incremental/README.md](incremental/README.md)。完整回归运行 `make test`，任何跳过均视为失败。
