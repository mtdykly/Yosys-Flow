# EPFL hyp：结果与复现

输入来自 `lsils/benchmarks`，固定提交 `82d8cc6910419298e713a46644ed59fd3df53038` 的 `arithmetic/hyp.v`。本地路径为 `build/epfl-large-screen/sources/hyp.v`，单模块顶层 `top`，239751 行。输入 SHA-256：`ca578a27ce121d7f4ae9f56b02f071661118871534d314e187635b1b2309e653`。

固定修改把第 239742 行 `n214713` 的 AND 改为 OR。它不是上游原有 ECO；修改有效性与完整输出验证均需单独检查。历史上按已知目标准备的已证明区域为 6 门、5 个输入、2 个输出，来自旧来源记录与独立边界证明。不能将其等同于完整 ABC 因果历史或任意修改的自动最小选区。

2026-11-24 差分优化：同一份已保存的 hyp 前端表示，单核串行测得修改前 **244.81 s**、修改后 **53.90 s**；`benchmarks/testcases` 对应的 `stress_3000` 为 **1.68 → 1.29 s**，保留有界匹配，未恢复无界 SequenceMatcher。差异内容和新旧指纹一致，52 项相关回归通过。此次没有重跑完整 hyp 增量；旧建档和通用证据库无需重建。测量及限制见[自动流程说明](../../incremental/automatic.md)，原始结果在 `runs/rtlil-diff-optimization-20261124-052843/summary.json`。

## 2026-11-23：补齐通用准备中的中间对应

**最新边界验收通过。** 在全部旧设计函数关系准备完成后，才查询原失败位置；得到 6 个最终门、5 个独立输入和 2 个对外输出，全部输出通过 BDD 边界校验。报告为 `runs/hyp-boundary-generic-20261123-063517/gate-form-acceptance.json`。准备没有输入新版 RTL、修改位置或历史候选对，验收也没有把这个目标的结果写入区域缓存。

这次验收从已保存并核对摘要的旧检查点材料重新构造函数类，安装通用旧事件证据并扫描全部相邻阶段，约 1227.208 s；不包含重新捕获、完整原始日志重放、来源索引构建及正式函数库发布，不能当作完整离线建档时间。该文件是算法验收报告，不是可交给在线命令的已发布函数库。当前实现的正式函数库需按下方命令在新目录生成。此次尚未执行完整新版增量网表生成或生成后的整网验证，因而没有新的完整在线耗时。

11/22 的 `runs/hyp-evidence-integrated-20261122-144239/base` 已完成重放和来源索引，可继续复用。其基础库支持的首次修改在 `first-20261122-164429` 阻塞：所需旧边界没有对应，4096 节点的局部窗口展开超限，随后来源遍历触及 65536 边上限。没有启动旧区域 SAT，也没有生成候选网表；这不是 60 s SAT 超时。

本轮从原始捕获发现 **29 条 FRAIG 成功合并已记录但未导入函数库**。另有 `read_blif/strash` 表达式转换的函数关系没有统一传播。实现现在先复用这些已采用的事实，再对全部相邻旧检查点做有预算的局部扫描；不以 `n214713`、历史候选对或新版 RTL 为输入。进一步检查旧重放报告发现 **8 次 `dc2` 替换留为未知**：实际快照已保存，独立补证全部通过。现在通用准备也会处理这份由旧日志产生的未知清单，保留事件身份、成功证明及未解决项，不需要重新插桩综合。

中间版本的通用准备实测位于 `runs/hyp-boundary-generic-20261123-063517/`，只使用旧归档，2955.52 s 完成（约 49 分 16 秒）。它包含 FRAIG 导入和相邻检查点扫描，尚不包含后加的未知改写补证；只恢复了历史局部区域五个输入边界中的一个，验收仍阻塞。随后仅把 8 条补证关系加入既有函数类的诊断也仍触及 4096 节点上限。两次均未发布目标区域缓存、未生成新网表，不能作为首次增量成功结果，也不能把这个中间版本的耗时当作最终版本性能。

继续检查发现映射检查点使用 SOP、最终 BLIF 使用 NAND/XNOR 等门类型，同一函数没有被统一表达。增加完整局部门真值表的 DAG 规范化后，该导出转换段的 164055 个逻辑节点与 256 个 PI（共 164311 项）均建立对应；这项检查扫描整个旧网络，不指定修改或候选边界。生产代码的这项专项检查耗时 26.800 s（含读图、函数构造和合并），没有运行 SAT；报告为同目录的 `export-normalization.json`。它解决的是表达转换中的结构对应遗漏，不等于证明了完整 ABC 因果历史，也不是完整建档耗时。

现在已有当前来源索引时，使用以下命令另建证据库即可，不必重复下面旧版的 `reindex` 步骤：

```bash
cd /workspace/Yosys-Flow
hyp_base="$PWD/runs/hyp-evidence-integrated-20261122-144239/base"
hyp_job="$PWD/runs/hyp-boundary-$(date +%Y%m%d-%H%M%S)"
hyp_evidence="$hyp_job/proofs"
mkdir -p "$hyp_job"
set -o pipefail
/usr/bin/time -v -o "$hyp_job/offline.time.txt" \
  timeout --kill-after=30s 7200s taskset -c 0 \
  python3 -u -m yosys_flow prepare-evidence "$hyp_base" \
    --out "$hyp_evidence" --boundary-budget 600 \
    2>&1 | tee "$hyp_job/offline.console.log"
```

本轮证明实现变化使旧函数库失效，应使用新目录；**来源索引仍可复用**。若报告为 `base_ready_partial` 且原因是扫描预算，保持变量并运行 `python3 -u -m yosys_flow prepare-evidence "$hyp_base" --out "$hyp_evidence" --resume --boundary-budget 600`。这仍然只使用旧设计。小型日志在每个检查点提交，恢复不会把未知对应作为已证事实。

通用准备结束后，再使用本页的 `hyp_trial first` 命令提供完整新版 RTL。务必使用上述新的 `hyp_job/hyp_evidence`，初始区域缓存应为 0；本轮不运行历史上按已知目标的 `refine_partitions.py --input-wire ...`。

已完成最新代码的仓库完整回归 **614 项通过、零失败、零错误、零跳过**，耗时 729.251 s，报告为 `runs/boundary-forms-regression-20261123/report.json`。新增 19 项测试覆盖相邻检查点传播、SOP/映射门统一表达、反相关系、门组、随机电路穷举核对、误候选拒绝、预算恢复、材料损坏、FRAIG 采用关系和未知改写补证，已包含在这 614 项中。FRAIG 阶段独立检查显示，导入 29 条事实后，214592 个逻辑节点均能在该阶段找到函数对应；这不是对整个 ABC 或首次增量已成功的声明。必要的旧区域证明仍计入在线时间。

## 较早的重放升级与完整复现步骤

`runs/hyp-proof-evidence-20261122-062930/` 的旧版命令在 7200 s 被终止，未进入增量综合。它复用了 11/21 的旧建档，没有重跑插桩。最后仍处于 `checkpoint_pair`，在跨 ABC 检查点细化函数对应；CPU 约 99%，峰值 RSS 约 19.01 GiB。旧实现等全部输出细化结束才写函数库，目录中的 `functions.sqlite3` 只有 49152 字节空表结构，未发布有效 manifest；约 438.02 MiB 的检查点材料不能等同于已发布证据。这一轮不是 SQLite 写入超时。

现在改为共享重放数据、复用已确认关系、基础证据先发布及增强按批保存；旧 RTL 组合表示也离线建立查询索引。以下为 11/22 重放升级流程的背景，当前已有来源索引时优先使用上一节命令。旧超时目录没有认证状态，不能直接恢复。此次 Python 重放实现升级需重建来源索引，仍可复用 `base-raw`，无需重跑插桩或原版完整综合。

核对发现当前插桩二进制与 `11/21 base-raw` 保存的摘要不同，所以本轮使用 `reindex`，不使用要求旧工具身份一致的捕获 `resume`。`reindex` 重新核对已有原版结果、复制并验证旧记录，再生成新索引和基础证据。其间会为旧源表示运行一次 `proc/techmap` 转换，但不会执行完整 `synth`。新的证据库和恢复状态占额外磁盘；没有设置体积上限。

第一步只根据旧设计准备基础函数关系，不指定修改信号，也不读历史目标候选对：

```bash
cd /workspace/Yosys-Flow
hyp_origin="$PWD/runs/hyp-provenance-20261121-081128/base-raw"
hyp_job="$PWD/runs/hyp-evidence-integrated-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$hyp_job"
hyp_base="$hyp_job/base"
hyp_evidence="$hyp_job/base.proofs"
set -o pipefail
echo "开始准备旧设计函数证据：$hyp_evidence"
/usr/bin/time -v -o "$hyp_job/offline.time.txt" \
  timeout --kill-after=15s 7200s taskset -c 0 \
  python3 -u -m yosys_flow reindex "$hyp_origin" \
    --out "$hyp_base" --index-backend sqlite --prepare-evidence \
    --evidence-out "$hyp_evidence" --source-timeout 600 \
    2>&1 | tee "$hyp_job/offline.console.log"
```

基础库发布后，第二步在同一终端运行增强；这一步仍只使用旧设计：

```bash
hyp_attempt="$(date +%Y%m%d-%H%M%S)"
/usr/bin/time -v -o "${hyp_evidence}.enhance-${hyp_attempt}.time.txt" \
  timeout --kill-after=15s 7200s taskset -c 0 \
  python3 -u -m yosys_flow prepare-evidence "$hyp_base" \
    --out "$hyp_evidence" --resume --refine-outputs \
    --enhance-budget 3600 --batch-outputs 8 --cache-engines 1 \
    2>&1 | tee "${hyp_evidence}.enhance-${hyp_attempt}.console.log"
```

`3600` 是本次增强阶段预算，外层 `7200s` 是整条命令上限。预算协作式检查，单次求解或保存可能使增强墙钟超出预算；`--enhance-budget 0` 才是不限制增强预算。增强中断后重复第二条命令恢复。若第一步中断，先看 `base.reindex.json`：只有 `archive_status=complete` 且证据目录已有认证 `work/state.json` 时，才可用 `prepare-evidence --resume` 继续证据准备；索引尚未完成时，应从原归档在新目录重新 `reindex`。换终端需恢复实际 `hyp_job`、`hyp_base`、`hyp_evidence` 路径，不要对已有目录重复首次创建命令。

重建启动输出复制/重验证阶段，可看 `base.progress.json`；交接证据时输出 `reuse_validated_archive`，之后看 `<证据目录>/progress.json` 和 `progress.jsonl`。`base.reindex.json` 给出重建总时间；证据 `report.json` 和每次 `report-<id>.json` 给出基础、旧源表示、增强、发布及状态读写的分项。发布和状态读写属于相应阶段的子项，不重复相加。

`ready` 不表示任意内部边界均已证明；`base_ready_partial` 表示增强或基础准备未全部完成，但已有一版可查询库。此时可以继续增强，也可以使用已发布关系尝试在线流程；缺少证据时仍会回退或拒绝。发布失败不会覆盖上一版，函数库各版本及恢复状态会额外占用磁盘。没有有效签名 manifest 的目录不能用于在线作业。

基础准备及所选增强轮次结束后，才给出完整新版 RTL。下面定义运行函数并执行首次修改；其中未提供修改行号或目标信号：

```bash
hyp_new="$PWD/runs/hyp-rerun-20261115-060717/replay-inputs/new.v"
hyp_trial() {
  local hyp_label="$1"
  echo "开始 hyp ${hyp_label}：$hyp_job/$hyp_label"
  /usr/bin/time -v -o "$hyp_job/${hyp_label}.time.txt" \
  timeout --kill-after=15s 7200s taskset -c 0 \
  python3 -u -m incremental auto \
    --base-run "$hyp_base" --proof-evidence "$hyp_evidence" \
    --new-source "$hyp_new" \
    --native-yosys "$PWD/build/native/install/bin/yosys" \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --yosys-timeout 3600 --whole-equivalence-timeout 1200 \
    --region-proof-budget 60 --region-node-limit 4096 \
    --no-equivalence-precheck --out "$hyp_job/$hyp_label" \
    2>&1 | tee "$hyp_job/${hyp_label}.console.log"
}
hyp_trial first
```

首次成功后，在同一终端执行重复修改，保留相同证据库和新版 RTL：

```bash
hyp_trial repeat
```

第一次使用新库时，`proof_evidence.metrics.initial_region_cache_entries` 应为 0。重复运行应另标缓存条件，检查初始条目数和 `cache_hits`；不能将重复时间当作真实首次修改时间。若返回 blocked，优先读取 `failure.detail.missing_evidence_stage`、`proof_evidence.metrics.misses` 和节点上限。它表示缺口或预算使局部证明未完成，不代表新旧功能不等价。不要将失败样本列为增量成功，也不要仅为绕过缺口盲目放大预算。

汇总这几种时间可运行：

```bash
python3 - "$hyp_job" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
r = json.loads((p/'base.reindex.json').read_text())
sessions = [json.loads(f.read_text()) for f in (p/'base.proofs').glob('report-*.json')]
enhancement = sum(s['timings']['total_seconds'] for s in sessions if s.get('resumed'))
print('通用离线准备秒（含复制/重验证/索引/基础准备及后续恢复会话）:', r['total_seconds'] + enhancement)
for label in ('first', 'repeat'):
    path = p/label/'report.json'
    if not path.exists():
        continue
    report = json.loads(path.read_text())
    t = report['timings']; m = report.get('proof_evidence', {}).get('metrics', {})
    print(label, report['status'], {k: t.get(k) for k in (
        'netlist_generation_seconds', 'required_old_region_proof_seconds',
        'post_generation_validation_seconds')},
        '启动时区域缓存:', m.get('initial_region_cache_entries'), '命中:', m.get('cache_hits'))
PY
```

必要证明是在线生成时间的子项，不能扣除；后验整网验证独立列出。首轮准备和恢复会话失败/部分完成的状态也需保留，不能只有时间而忽略完成情况。完整口径见[持久证明证据](../../provenance/docs/proof-evidence.md)。

## 完整流程结果（2026-11-15）

保留目录：`runs/hyp-rerun-20261115-060717/`，汇总 `summary.json` 的状态为 `passed`。

| 部分 | 秒 | 计时含义 |
|---|---:|---|
| 旧捕获、验证及原始索引建档 | 7479.140 | `archive` 命令墙钟，包含后台重放及后处理 |
| 功能区域细化与独立归档发布 | 3110.040 | `region-proof` 命令墙钟，重新证明候选切点 |
| 新版完整全量 F | 506.610 | 读取完整新版 RTL、默认 synth、三种网表写出 |
| 目标区域证据已准备后的候选网表生成 | 455.210 | 应用内计时；不含前面的目标区域证明，不能单独代表无预知修改的完整增量时间 |
| 生成后的全部验证 | 1212.803 | 包括新版参考综合、整网证明与导出验证 |
| 其中整网等价证明 | 593.222 | 已证切点用于分区及后续求解 |
| 增量命令总墙钟 | 1678.820 | 包含生成与后验验证，不再与上述包含项相加 |

F 包含进程启动/退出，生成是应用内计时。整网与 Verilog 重读均为 `equivalent`；这个成功样本不能证明所有 ECO 都可局部替换。区域细化使用已有候选对并重新证明，是固定已知修改的复现。脚本提前指定了本次修改的 `n214713`，因此该目标区域的准备不能直接当作与修改无关的离线工作；区域准备加生成合计为 3565.250 s，仍不含最初发现候选切点的成本。455.210 s 仅描述已经具有这份区域证据的运行条件。

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

## 本次来源修补的近期捕获基线（2026-11-21 核对）

评估本次来源修补的捕获性能，应优先使用已经完成性能优化的 **1017.800 s**，不能使用更早的 3885.093 s 得出“本次修补加快了捕获”的结论。1017.800 s 的原始计时保存在 `runs/hyp-capture-optimized-20261116-085314/time.txt`。

| 捕获运行 | 墙钟 s | 相对近期基线 | 运行条件 |
|---|---:|---:|---|
| 修补前优化版，单独捕获 | 1017.800 | 基线 | 无 Python 重放，开启内部细计时 |
| 修补后 `20261121-071036` | 1247.294 | +229.494 s，+22.55% | 建档内捕获，伴随后台重放 |
| 修补后 `20261121-081128` | 1883.671 | +865.871 s，+85.07% | 建档内捕获，伴随后台重放，时间区间与另一建档重叠 |

现有修补后捕获实测都更慢，应作为性能回退调查；运行条件不同只限制因果归因，不能据此宣称没有回退。脚本将整个建档命令绑定到 CPU 0，Python 重放线程与派生的 Yosys/ABC 继承该限制；与无重放的单独捕获相比，存在额外竞争。现有记录没有修补后同条件的单独捕获，也没有其内部细计时，不能将差额全部归于钩子或全部归于竞争。

两份原始 `yosys.jsonl` 的 `run_end.recorder_stats` 已能确认新增计算，而非仅新增文件写出：

| 记录器指标 | 修补前优化版 | 修补后 `081128` |
|---|---:|---:|
| 对象观察调用 `observe_calls` | 65356069 | 72509206 |
| 实际状态构造 `state_builds` | 1589108 | 1589108 |
| 检查点数 | 100 | 100 |
| 原始 Yosys JSONL 字节 | 2804606382 | 2822917510 |

对象观察增加 7153137 次（10.94%），日志只增加 17.463 MiB（0.65%），ABC 日志体积几乎不变。流式核对新日志仅发现 2 条 `optimization_rewrite`，均为 `opt_expr_mux_invert`。代码中新增的 `wreduce` 逐 cell `RewriteScope`、取反前提登记和连接证据维护在最终没有改写时仍有计算。应优先减少这些空操作路径上的作用域构造、对象观察和集合处理，保留实际改写的完整证据；新增调用的具体耗时仍需同条件细计时确认。

## 历史完整运行对照（2026-11-21 核对）

完整流程比较使用 `runs/hyp-rerun-20261115-060717/` 和 `runs/hyp-provenance-20261121-081128/`。两次新旧 RTL 内容摘要、原版 Yosys 二进制摘要和综合配置一致，均成功替换 6 个旧门，复用 163793 个门，生成 4 个局部门及 2 个输出绑定缓冲；全部顶层输出与 Verilog 重读验证通过。

这不是仅改变来源钩子的受控 A/B：两次之间还包含捕获性能优化和 IR 差分算法改造，11/21 还存在时间区间重叠的另一份建档。下表只能说明保留运行的时间变化，不能将变化全部归因于新增钩子。单位为秒，变化为修补后减去修补前。

| 部分 | 11/15 修补前完整运行 | 11/21 修补后完整运行 | 变化 |
|---|---:|---:|---:|
| 旧版插桩捕获、ABC 及网表/日志写出 | 3885.093 | 1883.671 | -2001.422 |
| 捕获后的记录验证、ABC 证据检查及网表核对 | 748.701 | 595.779 | -152.922 |
| 旧版普通全量对照 | 607.267 | 500.701 | -106.566 |
| 原版与插桩网表结构匹配 | 13.858 | 9.850 | -4.009 |
| 索引构建，含来源关系整理与写入 | 2038.568 | 1944.041 | -94.527 |
| **旧版建档总墙钟，包含以上阶段及其他管理开销** | **7479.140** | **5071.460** | **-2407.680** |
| 针对本次修改的区域证明与发布 | 3110.040 | 2978.980 | -131.060 |
| 该区域证据已准备后的增量生成 | 455.210 | 576.769 | +121.559 |
| **针对性区域准备＋增量生成** | **3565.250** | **3555.749** | **-9.501** |
| 新版全量综合 F | 506.610 | 502.620 | -3.990 |
| 整网等价验证 | 593.222 | 596.999 | +3.777 |
| 生成后全部验证，含新版参考综合、整网及导出检查 | 1212.803 | 1180.880 | -31.923 |
| 区域证据已准备后的增量命令总墙钟 | 1678.820 | 1768.310 | +89.490 |

包含项不能重复相加。两次建档都启用了后台流式重放，其运行区间与捕获重叠，不能把 `capture_replay_overlapped` 再作为独立阶段叠加；捕获后的等待均接近零。`verified_index_build` 使用 manifest 中覆盖整个索引构建的计时，不取 progress 中仅 10～15 秒的同名初始子阶段。完整 `archive`/`region-proof`/命令总时间取外部墙钟，其余分项取应用内计时。

区域证据已准备后的生成耗时分解如下，前五项对应生成总时间，存在取整和少量计时管理差异：

| 生成环节 | 修补前 | 修补后 | 变化 |
|---|---:|---:|---:|
| 旧档读取与校验、旧前端载入 | 49.158 | 42.812 | -6.346 |
| 新版前端与范围检查 | 115.232 | 110.077 | -5.155 |
| 新旧 IR 差分 | 89.816 | 234.397 | +144.581 |
| 选区、证据、patch 与绑定准备及导出 | 137.022 | 134.194 | -2.828 |
| 后续局部综合、拼接与完整候选写出等生成工作 | 63.981 | 55.289 | -8.692 |
| **生成合计** | **455.210** | **576.769** | **+121.559** |

最后一个分项内，局部综合为 0.051139 → 0.051047 s，绑定与拼接为 9.251 → 7.360 s，候选写出为 4.099 → 4.240 s。其余生成步骤合计减少约 23.021 s，差分增加 144.581 s；因此该次在线生成变慢主要体现在差分阶段，不能归因于局部综合。

两份 `difference.json` 也记录了算法变化：旧运行是 `instance_path_and_alpha_normalized_pre_proc_rtlil_v1`，新运行是 `instance_path_and_structured_pre_proc_rtlil_v2`。新差分处理新旧各 4941389 行 RTLIL、1396292 个结构对象，进行 3 轮临时名称对齐；未触发保守差分回退。尚无这些子操作分别耗时的实测，不能把 234.397 s 全部归到某一个子操作。

单独捕获需另外看：11/16 修补前为 1017.800 s，不运行 Python 重放；11/21 两份建档的捕获分别为 1247.294 s（`071036`）和 1883.671 s（`081128`），都伴随后台重放。`071036` 只保留完整建档结果，没有后续流程汇总，索引构建记录为 12623.333 s；其记录时间区间与 `081128` 重叠，无法从现有日志区分并发、暂停及其他资源影响。不能从两份新运行中各选较小值拼成一轮，也不能直接用 1017.800 与 1883.671 推断钩子的额外开销。

准确隔离来源修补本身的成本，需要固定前后工具版本和全部选项，在无其他任务竞争时串行重测同一种单独捕获命令。此次只核对已有结果，未启动新的 hyp 运行。区域证明时间的归属取决于是否在不知道本次修改的条件下预生成；当前脚本按已知目标准备区域并复用候选切点，合计 3555.749 s 仍不是无预知、无候选缓存的完整冷启动测量。

## 减少重复准备后的局部验收（2026-11-21）

本轮改动为：wreduce 首次实际修改前激活记录；嵌套作用域复用经过实际状态核对的旧观察；来源集合共享、上下文移动；常量链按需查询并缓存，连接和 pass 变化时失效；增加按 pass/调用位置汇总的诊断计数。审计模式仍提供提前准备和无缓存命中的对照，独立检查点未删减。

用保存的修补版工具与本轮工具，在 CPU 0 上串行测试 4096 个独立一位 AND 门、三轮 `opt_expr; wreduce`，两边均开启诊断，每边两次：

| 指标 | 本轮优化前 | 本轮优化后 |
|---|---:|---:|
| 平均捕获墙钟 s | 0.9175 | 0.8674 |
| 平均 user / system CPU s | 0.780 / 0.100 | 0.715 / 0.125 |
| 平均峰值 RSS KiB | 159336 | 159408 |
| 对象观察次数 | 135168 | 86016 |
| 实际状态构造次数 | 8192 | 8192 |
| 检查点数 | 10 | 10 |

四次均完成，前后最终 JSON 网表相同；原始测量在 `runs/capture-lazy-comparison-20261121/report.json`。这个小例子验证空操作路径的成本下降，不代表 hyp 的加速幅度。历史 1017.800 s 的工具比此次保存的修补版更早，不能混为同一个基线。本轮未重新运行 hyp。

完整回归为 564 项通过、零失败、零错误、零跳过，耗时 693.248 s；报告在 `runs/capture-lazy-regression-20261121/report.json`。新增四项覆盖大量不变对象、首次连接和递归裁位、常量缓存与失效、已删除取反前提及作用域恢复，使用提前准备且禁用缓存命中的审计模式核对最终网表与来源。

## 复现命令

先测本轮减少重复准备后的**单独插桩捕获**：

```bash
bash experiments/tools/capture_hyp.sh
```

该命令默认 CPU 0、7200 s 上限，开始即打印目录，实时输出带时间戳的 Yosys 日志。只执行读取、完整 `synth`、来源记录和网表写出；不启动后台重放、证明或索引。输出为新的 `runs/hyp-capture-lazy-<时间>/`，`time.txt` 给出墙钟/CPU/RSS，`measurement.json` 固定输入与工具摘要，JSONL 尾部保存诊断统计。`HYP_CPU`、`HYP_OUT`、`HYP_TIMEOUT` 可覆盖默认值。不要与其他建档同时运行。例：

```bash
HYP_CPU=0 HYP_OUT=runs/hyp-capture-lazy-review bash experiments/tools/capture_hyp.sh
```

若需要与本次优化前的修补版严格对照，串行运行同一脚本并设置 `HYP_NATIVE=build/capture-before-lazy/install/bin/yosys`，使用另一个输出目录；该保存的工具不同于历史 1017.800 s 那次的工具。独立捕获不生成已验证索引，不能直接作为完整增量归档使用。

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

更新记录器后，先 `make native NATIVE_JOBS=4`，再使用 `--fresh-archive`。脚本现在通过新归档中 `top.n214713` 的实际 ABC 输入绑定定位目标，并要求它仍是输出该信号的 `$_AND_`；不再依赖旧的 `native:cell_1118516@1` 编号。找不到、存在歧义或类型改变都会停止。候选切点对仍重新证明，不能直接沿用旧证明结论。新增来源事件可能改变对象编号和记录体积；修补后的完整运行及计时限制见上节。

脚本默认绑定 CPU 0；可通过 `HYP_CPU`、`HYP_OUT` 指定允许的 CPU 与新目录。`HYP_BASE_RUN`、`HYP_NEW_RTL`、`HYP_PAIRS` 可覆盖本地保留路径，但仍要求相同固定电路与修改。候选对只作为证明输入，绝不当作未经核验的证书。

脚本分阶段上限：旧建档 21600 s、区域证明 21600 s、F 3600 s、整个增量命令 7200 s；增量内部整网证明共享 1200 s。任何阶段失败即停止后续阶段，不能把超时或只有候选输出记为通过。

## 历史诊断的保留方式

此前解决过重复全网快照、大来源数组重复解码、递归深度、整网候选、结构差异证明和后端重复求解。现行机制分别记在[存储](../../provenance/docs/storage.md)、[功能区域细化](../../provenance/docs/abc-partitions.md)与[整网验证](../../incremental/netlist-equivalence.md)。不再保留逐次失败尝试的大量重复网表；旧汇总归入 `runs/retained-summaries/`，主线状态见[当前进展](../../docs/status.md)。
