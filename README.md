# Yosys-Flow

通过一次插桩的默认 Yosys `synth` 保存**组合 RTL 到最终通用门网表的来源关系**。新版 RTL 到来后，工具消费旧归档，自动差分、确定局部边界、提取 patch、局部综合并拼接完整新版网表。

支持单模块及普通组合子模块层次。默认 `synth -top` 保留层次，`--flatten` 显式展平；局部替换交付平坦网表。来源覆盖仍为 `partial`，候选区域必须满足证据与全部输出检查。时序局部替换、memory、generate、循环和函数/任务不在当前支持范围。独立的 EQY 等价预检查可以证明并复用整份时序旧网表。

## 使用

所有命令在仓库根目录执行，输出目录须不存在。依赖 Python 3.10+ 和固定版本 Yosys/ABC；编译工具链及版本要求见[构建说明](provenance/docs/build.md)。

```bash
make NATIVE_JOBS=4

# 一次插桩全量综合、验证和建索引
python3 -m yosys_flow run \
  --source provenance/examples/hierarchy.v --top demo --out runs/old-run

# 查询原始语句关联的最终逻辑
python3 -m yosys_flow query runs/old-run \
  --source provenance/examples/hierarchy.v --line 4 --instance demo/u0/inner

# 只提供旧归档与完整新版 RTL
python3 -m incremental auto \
  --base-run runs/old-run --new-source path/to/new.v --out runs/edit
```

首次建档保存原始 JSONL、前端表示、最终网表及验证索引。默认 SQLite 按需查询，`run --index-backend gzip` 使用同样去重结构的内存后端。捕获完成后若后处理被中断，可以 `python3 -m yosys_flow resume OLD_CAPTURE --out NEW_ARCHIVE` 续建档，见[存储与恢复](provenance/docs/storage.md)。

可用 `python3 -m yosys_flow prepare-evidence OLD_ARCHIVE` 提前整理旧设计已证函数关系，在线按需组合和缓存区域，避免重复恢复全局证明状态。它默认复用已记录的 FRAIG/DCH 成功关系、对旧日志中的未知改写做有预算的补证，并扫描相邻旧检查点的局部函数边界，不需要提前指定修改；命中证据与缺失证明的耗时分别报告，详见[持久证明证据](provenance/docs/proof-evidence.md)。

增量入口继承旧顶层、参数、宏和综合模式；默认从变化赋值及依赖起步，必要时扩区。叶模块等价预检查可直接复用功能不变的旧网表。候选生成后执行整网与导出验证，通过才发布交付结果。新网表尚不能直接作为下一轮来源归档。

## 目录与文档

| 目录 | 职责 | 文档 |
|---|---|---|
| `provenance/` | 原生 Yosys/ABC 记录、重放、索引、查询、导出与契约 | [来源子系统](provenance/README.md) |
| `incremental/` | 差分、选区、patch、局部综合、拼接与验证 | [增量入口](incremental/README.md) |
| `experiments/` | 基准适配、性能对比、耗时归因和独立 NOC 调查 | [实验入口](experiments/README.md) |
| `benchmarks/` | 固定上游输入、修改清单、许可与工艺库配置 | [ANUBIS](benchmarks/anubis/README.md)、[EPFL](benchmarks/epfl/README.md) |
| `docs/` | 当前进展与仓库维护约定 | [当前状态](docs/status.md)、[目录与产物](docs/repository.md) |

已验证的固定 hyp 修改可以完成增量生成与整网验证；最新 `stress_3000` 仍在直连输出边界检查处阻塞。具体时间和限制集中在[当前状态](docs/status.md)，避免把历史实验结果当作现版本的普遍保证。

## 验证与性能实验

```bash
# 完整项目回归，集成测试跳过也视为失败
make test

# 比较 F、同框架 P-module 与 P-fine
python3 -m experiments compare --repeats 2 --out runs/comparison
```

首次建档、在线新版网表生成、生成后的正确性验证分别计时。`--liberty` 可统一映射后求和估算面积，映射时间另列。性能协议见[对比实验](experiments/docs/comparison.md)。历史 `python3 -m incremental compare` 等实验命令保留转发；新文档统一使用 `experiments`。

`build/` 与 `runs/` 不提交。保留的归档及历史汇总见[产物保留规则](docs/repository.md#运行产物)。
