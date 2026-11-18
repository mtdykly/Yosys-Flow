# EPFL hyp：结果与复现

输入来自 `lsils/benchmarks`，固定提交 `82d8cc6910419298e713a46644ed59fd3df53038` 的 `arithmetic/hyp.v`。本地路径为 `build/epfl-large-screen/sources/hyp.v`，单模块顶层 `top`，239751 行。输入 SHA-256：`ca578a27ce121d7f4ae9f56b02f071661118871534d314e187635b1b2309e653`。

固定修改把第 239742 行 `n214713` 的 AND 改为 OR。它不是上游原有 ECO；修改有效性与完整输出验证均需单独检查。当前已证明区域为 6 门、5 个输入、2 个输出，来自旧来源记录与独立边界证明。不能将其等同于完整 ABC 因果历史或任意修改的自动最小选区。

## 完整流程结果（2026-11-15）

保留目录：`runs/hyp-rerun-20261115-060717/`，汇总 `summary.json` 的状态为 `passed`。

| 部分 | 秒 | 计时含义 |
|---|---:|---|
| 旧捕获、验证及原始索引建档 | 7479.140 | `archive` 命令墙钟，包含后台重放及后处理 |
| 功能区域细化与独立归档发布 | 3110.040 | `region-proof` 命令墙钟，重新证明候选切点 |
| 新版完整全量 F | 506.610 | 读取完整新版 RTL、默认 synth、三种网表写出 |
| P-fine 新版输入至候选网表 | 455.210 | 应用内在线生成计时，约比 F 缩短 10.1% |
| 生成后的全部验证 | 1212.803 | 包括新版参考综合、整网证明与导出验证 |
| 其中整网等价证明 | 593.222 | 已证切点用于分区及后续求解 |
| 增量命令总墙钟 | 1678.820 | 包含生成与后验验证，不再与上述包含项相加 |

F 包含进程启动/退出，P 生成是应用内计时。整网与 Verilog 重读均为 `equivalent`；这个成功样本不能证明所有 ECO 都可局部替换。区域细化使用已有候选对并重新证明，是固定已知修改的复现。

主要产物相对于该保留目录：

| 路径 | 内容 |
|---|---|
| `base-raw/` | 原始建档及原始索引 |
| `base-functional/` | 已发布、可直接用于此次增量的细化索引 |
| `region-proof/` | 边界、候选对与证明 |
| `full-new/netlist.v` | 独立全量综合的新版 Verilog |
| `incremental/replacement/` | 候选网表、新版参考与完整验证产物；具体名称以 `report.json` 为准 |
| `replay-inputs/` | 本轮清理时保留的新版 RTL 与需重新证明的候选对 |

## 捕获优化后的单独测量（2026-11-16）

| 模式 | 墙钟 s | user CPU s | 保留目录（runs/ 下） |
|---|---:|---:|---|
| 普通旧版全量综合 | 501.370 | 498.500 | `hyp-stock-only-20261116-020745/` |
| 插桩捕获 | 1017.800 | 1003.990 | `hyp-capture-optimized-20261116-085314/` |

读取各目录的 `time.txt` 可核对。两者均包含各自脚本中的前端、综合与写出；插桩捕获还写来源记录并开启细计时，不运行 Python 重放、证明或建索引。1017.800 s 约为普通版本的 2.03 倍。此前“1003 s”是 user CPU，并非墙钟。

捕获缓存、检查点与接口共享的实现及审计开关见[存储说明](../../provenance/docs/storage.md)。这次单独捕获属于后续优化版本，不能把它替换进上一节的旧建档总时间后声称完成了新的整轮实测。

## 复现命令

在仓库根目录运行。脚本会检查固定输入与修改，输出目录必须不存在；先用 `--dry-run` 查看将执行的命令。

```bash
# 复用保留的已验证归档，跑独立 F、自动增量和完整验证
bash experiments/tools/rerun_hyp.sh --dry-run
bash experiments/tools/rerun_hyp.sh

# 从旧 RTL 开始重新建档、重证候选对，再完成同一流程
bash experiments/tools/rerun_hyp.sh --fresh-archive --dry-run
bash experiments/tools/rerun_hyp.sh --fresh-archive
```

`--fresh-archive` 实际从旧 RTL 重跑插桩综合；不是复用旧捕获的 `resume`。旧捕获中断后续建档的方法见[续建档](../../provenance/docs/storage.md#捕获完成后的续建档)。

脚本默认绑定 CPU 0；可通过 `HYP_CPU`、`HYP_OUT` 指定允许的 CPU 与新目录。`HYP_BASE_RUN`、`HYP_NEW_RTL`、`HYP_PAIRS` 可覆盖本地保留路径，但仍要求相同固定电路与修改。候选对只作为证明输入，绝不当作未经核验的证书。

脚本分阶段上限：旧建档 21600 s、区域证明 21600 s、F 3600 s、整个增量命令 7200 s；增量内部整网证明共享 1200 s。任何阶段失败即停止后续阶段，不能把超时或只有候选输出记为通过。

## 历史诊断的保留方式

此前解决过重复全网快照、大来源数组重复解码、递归深度、整网候选、结构差异证明和后端重复求解。现行机制分别记在[存储](../../provenance/docs/storage.md)、[功能区域细化](../../provenance/docs/abc-partitions.md)与[整网验证](../../incremental/netlist-equivalence.md)。不再保留逐次失败尝试的大量重复网表；旧汇总归入 `runs/retained-summaries/`，主线状态见[当前进展](../../docs/status.md)。
