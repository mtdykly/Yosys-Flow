# Yosys-Flow

通过一次插桩全量综合保存**原始组合 RTL → 默认 `synth` 最终通用门级网表**的来源关系。新版 RTL 到来后，自动差分、查询旧记录、确定局部边界、局部综合并拼接成完整新版网表。

首次建档保存原始事件、最终网表和可复用的验证索引。默认 SQLite 按需读取，`run --index-backend gzip` 可选择整包加载的去重 gzip；后续查询与增量自动识别后端。两者共用去重结构与来源查询规则。ABC 的 AIG/GIA 历史网络采用检查点与差量记录，原生事件共享大来源/成员集合；原始证据与完整性检查继续保留，详见[记录与查询存储](provenance/docs/storage.md)。在线增量生成轻量替换计划，引用旧证据；完整区域契约保留为独立的审查、验证及导出接口。来源覆盖仍为 `partial`，候选或无法绑定的区域可能阻塞，不能将查询结果直接视为任意修改都可安全替换。

支持普通组合子模块的多层实例。默认 `synth -top` **保留层次**，显式 `--flatten` 请求展平并尊重 `keep_hierarchy`；当前局部替换交付平坦网表。时序、memory、generate、循环、函数/任务不在来源记录与局部替换范围。独立的 [EQY 时序等价预检查](incremental/sequential.md) 可以在已声明的状态条件下证明并复用整份旧网表，不提供时序局部替换。

## 构建与首次建档

所有命令在仓库根目录执行，运行目录须不存在。依赖 Python 3.10+、Make、C++20、CMake ≥ 3.28、Ninja、Bison、Flex、m4、zlib 开发文件和固定版本 `0e82bbefe` 的独立原版 Yosys/ABC，详见[构建说明](provenance/docs/native-m1.md#构建)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source provenance/examples/hierarchy.v --top demo --out runs/old-run
python3 -m yosys_flow query runs/old-run \
  --source provenance/examples/hierarchy.v --line 4 --instance demo/u0/inner
```

`provenance/` 保存原生 Yosys/ABC 钩子、事件重放、索引、查询、区域导出与独立契约工具。现有 `python3 -m yosys_flow` 命令通过仓库兼容入口加载该目录。命令和支持范围见 [来源子系统](provenance/README.md)。

## 完整新版 RTL 到增量网表

```bash
python3 -m incremental auto \
  --base-run runs/old-run --new-source path/to/new.v --out runs/edit
```

输入只有旧归档与完整新版 RTL，多个文件重复 `--new-source`。顶层、参数、宏及综合模式继承旧记录；无需提供修改行号、目标实例、局部代码或连接。工具从变化赋值及依赖起步，必要时扩区或退到模块范围；旧源码可已被修改或删除。

默认先尝试有预算的叶模块 `miter + sat` 等价检查。全部变化证明功能不变且上下文符合前提时，直接复用旧网表；其余变化进入局部替换。实际拼接仍经过全部输出验证，失败不发布网表。替换后的结果尚不能直接作为下一轮来源归档。算法、轻量计划、证据校验和限制见 [自动增量流程](incremental/automatic.md)。

## 测试与对比

```bash
# 固定 ANUBIS 功能修改组合案例，比较到整份新版网表写出的时间
python3 -m incremental compare --repeats 3 --out runs/comparison
# 完整回归；跳过视为失败
make test
```

默认性能套件包含 ANUBIS 派生的功能修改组合案例，比较全量 `F`、同框架模块选区 `P-module` 与默认细粒度 `P-fine`。首次建档与生成后的正确性验证分别计时，不混入在线网表生成成本。默认报告通用门数；通过 `--liberty` 可对交付网表统一工艺映射、估算标准单元面积，映射耗时另列。固定库及获取方法见 [工艺库说明](benchmarks/libraries/README.md)。独立模块级增量方法 `M` 不在本项目中。方法与实测见 [对比实验](incremental/comparison.md)。

较大规模的 [EPFL 扩展实验](incremental/epfl-comparison.md) 使用 `--suite epfl-functional`：保留 `adder`、`bar`、`arbiter` 的原始单模块结构，每个固定一处局部和一处共享逻辑修改，默认比较 F 与 P-fine。修改先通过完整顶层的反例资格检查，结果与 ANUBIS 分开报告。

- [增量命令与显式四通道实验](incremental/README.md)
- [ANUBIS 来源、适配与 NOC 调查](benchmarks/anubis/README.md)
- [时序等价预检查及待补证明](incremental/sequential.md)
- [来源覆盖与后续计划](provenance/docs/default-synth-plan.md)

NOC 调查、时序等价预检查和人工诊断用于独立验收，结果不混入功能修改的性能结论。`build/` 是工具缓存，`runs/` 是运行产物，均不提交。本地仅保留 `runs/comparison-anubis-functional-v4/` 的当前实验及其 `validation/` 回归摘要；旧实验目录和阅读摘录已清理。重新运行命令会生成新的结果目录。
