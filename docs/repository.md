# 目录与产物维护

代码分为来源、增量和实验三部分。`provenance/` 负责原生事件与证据；`incremental/` 负责消费旧归档生成新版网表；`experiments/` 负责基准输入、调度、测量和诊断。主算法不依赖实验数据目录。三个目录中的回归由 `provenance/tools/run_regression.py` 统一发现。

`yosys_flow/` 是仓库内兼容入口。原生中间检查点 `m1/m1-opt` 用于定位来源缺口；默认产品入口为 `synth`。旧 M0 不恢复。实验命令从 `python3 -m incremental compare` 等转到 `python3 -m experiments compare`，原命令保留转发。Python 模块路径统一为 `experiments.*`。

## 运行产物

`runs/` 是忽略目录，运行结果不进入 Git。此次清理保留：

| 目录（均在 runs/ 下） | 保留理由 |
|---|---|
| `backend-suite-supported-360/` | 双后端适配复测及归档、测量依据 |
| `hyp-rerun-20261115-060717/` | 完整成功流程：旧归档、区域证明、全量与增量网表、终检 |
| `hyp-capture-optimized-20261116-085314/`、`hyp-stock-only-20261116-020745/` | 最新单独捕获与普通综合对照 |
| `capture-repair-20261116/` | 捕获优化对照、语义事件一致性及回归 |
| `testcases-20261116-133941/` | stress_3000 当前可复用旧归档 |
| `testcases-diff-fix-20261117/` | 差分修复测量与专项回归 |
| `testcases-incremental-20261117-121739/` | 最新阻塞现场及扩区信息 |
| `hyp-incremental-20261109/fixture/` | 保留报告仍绑定的原始修改输入 |
| `retained-summaries/` | 已清理实验的摘要，不能作为可复用建档 |
| `workspace-cleanup-20261118/` | 本轮删除清单、体积统计、校验和回归 |

旧的重复归档、未完成尝试、一次性诊断脚本和重复回归目录已清理。摘要可能包含当时绝对路径；这些字段是历史记录，不能保证原始大文件仍存在。正文只把上述保留目录作为当前运行依据。

删除以完整运行目录为单位，先确认没有后续归档依赖其中的源文件、输入绑定或证明素材。不要从保留归档内部删 JSONL、网表、索引或签名字段，也不要手改 manifest。归档哈希与认证失败会拒绝加载。

`build/` 保留工具链和下载缓存；`build/.verified-index-auth-key` 是本机索引认证密钥，不能提交或当作临时文件删除。`benchmarks/` 保留固定清单、来源许可及仍被测试使用的输入。

## 文档入口

当前结论统一写在[状态页](status.md)。来源与算法约束分别放在各自模块文档；测量协议和有日期的结果放在 `experiments/docs/`。过时的过程日志与重复结论交给 Git 历史，不再为每轮调试新增一份主线文档。

## 本次整理验收（2026-11-18）

删除清单包含 208 项旧目录/文件，删除前后 `runs/` 的磁盘占用约从 203 GiB 降至 50 GiB，释放约 153 GiB；保留 66 份历史汇总。明细位于 `runs/workspace-cleanup-20261118/cleanup.json`。本轮回归新增产物也保留在该目录，体积会稍有增长。

完整回归 **545 项通过，零失败、零错误、零跳过**（682.44 s）。hyp 和 testcase 当前旧归档均通过只读认证加载、命中 SQLite，显式禁止原始重放。新旧实验 CLI 的 5 个子命令参数一致，所有文档文件链接及章节锚点检查通过；hyp 复现脚本完成 dry-run，没有重跑大型实验。

保留测试材料中有 4 个早已不存在的输入文件，来自“旧源文件已移除仍可使用归档”的测试夹具；清理没有新增缺失引用。检查范围与逐项结果见同目录 `plan.json`、`archive-load.json`、`cli-check.json`、`document-links.json` 和 `regression/report.json`。
