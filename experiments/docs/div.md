# EPFL div：第二个大组合电路的首次增量实验

上游为 [lsils/benchmarks 的 arithmetic/div.v](https://github.com/lsils/benchmarks/blob/82d8cc6910419298e713a46644ed59fd3df53038/arithmetic/div.v)，与 hyp 使用同一固定提交。它是单模块、已经展开为逐位逻辑的组合除法器，没有时钟或状态；并不是直接用 `/` 运算符写成的行为级 RTL。

| 原始源码 | div | hyp（规模对照） |
|---|---:|---:|
| 物理行数（含注释、空行） | 63,574 | 239,751 |
| 连续赋值数 | 57,247 | 214,335 |
| 输入位数 | 128（a、b 各 64） | 256 |
| 输出位数 | 128（quotient、remainder 各 64） | 128 |

赋值数不等于默认 synth 后的门数。div 小于 hyp，但明显大于此前的 ctrl、router、priority、adder、bar、arbiter。它用于检验另一种深算术网络，不能预先保证耗时、选区成功或增量加速。

## 固定修改及资格检查

[div-local.json](../../benchmarks/epfl/div-local.json) 固定上游提交、输入和新版 SHA-256。新版只把第 34986 行改为：

```verilog
// base.v
assign n28918 = ~\b[63]  & ~n28917;
// new.v
assign n28918 = ~\b[63]  | ~n28917;
```

修改按既有 EPFL local 规则从源码 DAG 选择：可达输出数最少、最长输出距离最短、源码位置最早的内部 AND。本例拓扑上可达一个输出，距离为一条连接。它是人工构造的局部 ECO，不代表任意内部修改，也不是上游的历史版本对。不加 keep、wrapper 或人工模块边界，不根据综合结果重新挑选一个更容易成功的修改。

2026-11-26 已完成源码模拟及 Yosys `eval` 的独立全顶层向量回放：`a=64'h0004e768a7d834b8`、`b=64'h03e51eceb8d1f37b` 时，旧 `quotient[0]=0`，新 `quotient[0]=1`；两种求值方式的全部 128 个输出一致。一个具体差异足以说明这次修改改变功能；它不证明来源选区或增量网表正确。随后首次建档在 FRAIG 证据整理阶段触发 7200 秒上限，尚未进入增量；诊断及续跑入口见下文。

## 本次超时修复与续跑（2026-11-26）

`runs/div-first-20261126-063254/` 的插桩综合已在 **133.356 s** 完成，并保存完整 `run_end` 和 12 项捕获产物；旧网表有 12,722 个 cell。总建档在 7200.530 s 超时，未创建来源索引和正式函数库，也未运行新版全量和增量。最后的 `replay_local_rewrite_functions` 是未及时更新的进度：改写证据和检查点文件实际上已经写完，接着执行的 FRAIG 导出没有进度提示。

原 FRAIG 导出对 27,884 条关系逐条扫描事件、重建两份 57,504 节点的结构并创建复制映射。现改为一次事件索引、每次 ABC 调用一个复制映射；相同不可变节点存储直接比较身份，不同不可变版本的完整结构比较在该调用中复用。每条关系的命令、快照类型、端口次序、事件先后、节点存在性及采用极性仍逐条检查；新版本不能冒用旧版本的比较结果。

同一捕获的分段复测（`build/div-fraig-optimization/report.json`）：

| 测量 | 修改前 | 修改后 |
|---|---:|---:|
| 同样的 32 条 FRAIG 关系，包含导出 | 9.917 s | 0.782 s |
| 全部 27,884 条关系，包含导出 | 原整轮超时，未取得本段完成时间 | 1.168 s |

32 条样本的证据描述符及文件逐字节相同。全量导出只建立一次复制映射，校验一次 cleanup，逐条核对全部关系，未重复构造共享网络。单独解码 FRAIG 日志前缀另耗时 11.189 s，不包含在表中；集成建档复用已解码数据。**1.168 s 只表示 FRAIG 导出，不代表完整重放、索引或通用证据准备的时间。** 后续阶段仍待实际续跑。

相关回归 60 项通过、零失败、零跳过，包含共享结构下的损坏极性、端口/命令变化、不可变版本变化、缺失或重复复制证据、集成/独立准备一致性以及不重跑插桩的续建档。报告为 `build/div-fraig-optimization/regression.json`。

现在可直接复用已完成捕获，在新目录继续全部后续步骤：

```bash
cd /workspace/Yosys-Flow
div_run="runs/div-resumed-$(date +%Y%m%d-%H%M%S)"
python3 -u -m experiments.div run \
  --resume-capture runs/div-first-20261126-063254/base \
  --out "$div_run" --cpu 0 --timeout 7200
```

该命令第一阶段调用已有 `yosys_flow resume`，核验并复制旧捕获，重新执行重放、证据整理、普通旧版对照、结构检查和建索引；不重新运行插桩综合，不编译 Yosys/ABC。原超时目录保持不变。随后按原协议继续通用旧证据、新版全量和真实首次增量及验证。只想先续建档时增加 `--stage archive`；之后的 `--stage evidence|full|incremental` 可从新目录的 `experiment.json` 自动恢复捕获来源，不必重复提供 `--resume-capture`。

汇总新增 `instrumented_synthesis_reused=true`，本次插桩成本为 0，原来的 133.356 s 作为历史捕获时间保留；本次续建档耗时记在 `archive_after_reused_capture`，不冒充重新测量的完整旧建档时间。FRAIG 导出会报告 `fraig_evidence_load/index/validate/write/complete`，统计保存于临时见证目录以及随后发布的 `proofs/checkpoints-*/checkpoints.json` 的 `fraig_export_statistics`。

## 运行

当前工作区已准备好 `build/epfl-div/base.v`、`new.v`、`fixture.json` 和 `witness.json`。从干净检出首次准备时运行以下命令；工具优先使用本地校验通过的缓存，缺少时下载固定版本，现有目录拒绝覆盖：

```bash
cd /workspace/Yosys-Flow
python3 -m experiments.div prepare
```

完整流程只需一条实验命令。默认绑定当前允许的最小 CPU，串行运行一次，无文件体积上限：

```bash
cd /workspace/Yosys-Flow
div_run="runs/div-first-$(date +%Y%m%d-%H%M%S)"
python3 -u -m experiments.div run --out "$div_run" --cpu 0 --timeout 7200
```

命令开始打印输出目录和阶段，每 30 秒报告仍在运行。`--timeout 7200` 是**每个阶段**的墙钟上限，不是整个流程两小时；在线 Yosys 子进程上限为 3600 秒，最终整网等价上限为 1200 秒。只预览可加 `--dry-run`，不会创建输出目录或启动综合。

四个阶段调用已有主流程，没有另外实现一套增量算法：

1. **archive**：仅输入旧 RTL，执行完整插桩 `synth -top top`、记录写出、重放和网表核对、普通旧版综合与结构对照、SQLite 索引及基础证据准备。关闭后台重放，让 `manifest.json` 的 `timings_seconds.traced` 单独表示插桩综合和网表写出时间；重放期间的 ABC 分析仍共享给证据写入器。
2. **evidence**：如果集成准备已经 `ready`，直接复用，不再次加载归档；否则只使用旧归档继续通用边界扫描，复用已经持久化的关系。要求得到 `ready` 才进入首次增量；`ready` 不等于完整因果链或所有未来修改都已可证明。
3. **full**：从完整新版 RTL 执行原版 `synth -top top`，写出 JSON、RTLIL、Verilog，测量全量基线 F。
4. **incremental**：只向 `incremental auto` 提供旧归档、通用旧证据库和完整新版 RTL。自动差分、选区、必要局部证明、patch 综合、拼接并写出候选；随后执行完整设计验证。修改行、信号名及资格向量均不传入选区器。独立 F 的产物也不作为增量输入。

已有区域缓存或增量输出时，入口拒绝把重跑标为首次修改。报告额外核对 `initial_region_cache_entries=0`、`cache_hits=0`。页缓存未清理，因此这是“没有本次修改区域缓存”的首次增量，并不声称操作系统磁盘冷缓存。

## 看结果与继续中断的阶段

```bash
python3 -m experiments.div summary "$div_run"
# 另一个终端看当前阶段的日志，例如：
tail -f "$div_run/archive.log"
```

- `summary.json`：分阶段时间、失败阶段、首次缓存条件；只有增量验证通过才计算有效加速比。
- `base/manifest.json`：旧建档详细时间；`base.progress.json`、`proofs/progress.json`：准备进度。
- `incremental/progress.json`、`incremental/report.json`：在线进度和最终结果。
- `full/netlist.v`：独立新版全量网表；验证通过后发布 `incremental/incremental.v`。
- `*.stage.json`、`*.time.txt`、`*.log`：各阶段命令、退出码、墙钟、CPU、峰值内存和日志。超时、阻塞均保留，不能算综合成功。

若旧归档已完成、证据准备因预算中断，可以复用同一目录继续：

```bash
python3 -u -m experiments.div run --out "$div_run" --cpu 0 --stage evidence
python3 -u -m experiments.div run --out "$div_run" --cpu 0 --stage full
python3 -u -m experiments.div run --out "$div_run" --cpu 0 --stage incremental
```

不要重复执行已经完成的 full 或 incremental。如果中断发生在旧归档完成之前，先检查捕获是否完整，再按已有 `yosys_flow resume` 机制处理；不要把不完整归档交给增量流程。

**时间口径**：旧建档总时间包含插桩综合和基础证据，内部的重放、索引等是子项，不能再次相加。额外通用证据准备单列。P 是完整新版 RTL 到候选网表的在线时间，包含差分、选区和必要的旧区域证明；必要证明另列但不扣除。生成后的新版参考综合、整网等价、重读验证另列，其中整网等价是后验验证的子项。F 使用外层命令墙钟，P 使用主流程应用计时，进程启动退出差异保留说明。所有原始结果留在 Git 忽略的 `runs/`，源文件缓存留在 `build/`。
