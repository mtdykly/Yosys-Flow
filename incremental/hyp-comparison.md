# EPFL hyp 首次建档与局部修改实验（2026-11-09）

## 共享集合索引续建档（2026-11-10）

本轮开始检查时，原 PID 182781 已不存在，没有存活任务需要再终止。旧 `.progress.json` 停在 `verified_index_build`，不能据此判断仍在执行；旧目录保留作失败证据。

已修改反向索引为源码→共享集合→最终对象，避免在 SQL 写入前生成所有源码/门组合；两种后端共用 schema 3。原版与插桩网表改用下述结构匹配。当前再次复用 `runs/hyp-resumed-20261109-163735/` 捕获，输出到 `runs/hyp-shared-index-20261110/`。同时实现的记录器优化仅影响未来新捕获，不要求重跑这份旧记录。

为重编译优化记录器而保留了旧二进制 `build/native/capture-tool-20261109/bin/yosys`，其摘要必须等于捕获中的 native 摘要。新旧工具不能混用来伪装原捕获身份。续跑命令如下；已存在的输出目录需更换名称。

```bash
hyp_index="runs/hyp-shared-index-$(date +%Y%m%d-%H%M%S)"
/usr/bin/time -v -o "${hyp_index}.time.txt" \
  timeout --kill-after=15s 7200s \
  taskset -c 0 python3 -u -m yosys_flow resume \
    runs/hyp-resumed-20261109-163735 \
    --native-yosys build/native/capture-tool-20261109/bin/yosys \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --index-backend sqlite --out "$hyp_index" \
    > "${hyp_index}.console.log" 2>&1
```

不重跑插桩综合；仍离线恢复已有记录、验证 ABC 证据、跑普通综合对照，再执行结构匹配和索引发布。构建进度新增 `index_source_groups`、`index_electrical_lookup`、`index_write_rows`，写入阶段报告当前字段与已处理行数。本次实际结果如下。

### 本轮实际结果

`runs/hyp-shared-index-20261110/manifest.json` 已为 `complete`，进程退出码 0。没有再次执行插桩综合；复用旧记录完成全部声明检查，未使用 `--skip-stock-equivalence`。

| 阶段 | 秒 |
|---|---:|
| 捕获核对与独立复制 | 65.417 |
| 记录、ABC 证据及实例视图验证 | 848.817 |
| 普通 Yosys 全量综合对照 | 520.768 |
| 原版/插桩网表结构匹配 | 9.795 |
| 索引构建，包括准备、写入、完整性检查及签名 | 2044.778 |
| 发布完成时的进度计时 | 3600.451 |
| 整个命令墙钟，含退出释放内存 | 3641（60 分 41 秒） |

索引内部的共享来源集合准备约 31.1 秒，之后开始实际 SQL 数据写入；这不是整个索引构建时间。上表子阶段不覆盖全部哈希、准备及退出开销，不应强求相加等于命令墙钟。峰值 RSS 为 **38.266 GiB**，无 swap；索引写入时观察到约 26–28 GiB。此前旧展开尝试曾观察到超过 111 GiB，最终未发布，不是一个可计算完整加速比的成功基线。当前单次测量同时存在其他 CPU 上的回归任务。

| 产物 | 字节 | GiB |
|---|---:|---:|
| 原生 JSONL | 4525528721 | 4.215 |
| ABC 内部 JSONL | 1910736277 | 1.780 |
| SQLite 索引 | 7285231616 | 6.785 |
| 整份归档，包含上述文件及其他产物 | 14554524345 | 13.555 |

独立加载验证通过，强制禁止原始重放时命中 SQLite。加载总计 45.589 秒：产物哈希 31.002 秒、索引恢复 3.857 秒、保存的参考网表/电气视图检查 10.721 秒，其余为少量外围开销。这个加载总时间不是纯 SQLite 打开时间。

这份实际来源数据中，698017 个来源与 163799 个最终门共享同一个来源集合；旧反向表会展开 **114334486583 对**关系。新表示只需 698017 项源码到组和 163799 项组到门，即 **861816 项成员关系**。后者只描述反向关联部分，不是整个索引的行数。所有组成员的并集与独立保存的最终门集合完全一致，且无重复成员。

**本次解决了建档可完成性，没有提高这份旧捕获的来源精度。** 对第 239742 行的反向关联仍返回全部 163799 个门，关系查找约 0.208 秒；这个时间不含加载、完整证据路径检查、区域边界/契约验证或 patch 提取，不能当作增量选区时间。此次检查的是压缩前后关联语义、归档完整性及可读取性，不能宣称该行已有精确的小区域，也尚未运行 `hyp` 的 P-fine 替换。

完整结果在 `runs/hyp-shared-index-20261110.verification.json`；资源计时、进度和控制台日志使用相同前缀的 `.time.txt`、`.progress.json`、`.console.log`。最终实现的项目回归为 **416 项通过，零失败、零跳过**，见 `runs/archive-scaling-final-regression/report.json`。新版记录器的状态缓存、SigMap 复用与分批重放只影响新捕获；小例子验证及准确边界见[存储说明](../provenance/docs/storage.md)。

## 使用完成的归档运行增量（手工执行）

现在可以直接复用 `runs/hyp-shared-index-20261110/`，无需再建档。新版使用已经固定且已确认存在功能差异的 `fixture/new.v`，仍然只输入旧归档和完整新版，不手工指定修改位置。本段是运行说明，**尚未执行本次增量测量**；上文的全网候选关联意味着它可能扩为整块电路或阻塞，不能预设有局部复用收益。

```bash
cd /workspace/Yosys-Flow
mkdir -p runs
hyp_inc="runs/hyp-incremental-$(date +%Y%m%d-%H%M%S)"
set -o pipefail
echo "开始 hyp 增量实验，输出目录：$hyp_inc"
/usr/bin/time -v -o "${hyp_inc}.time.txt" \
  timeout --kill-after=15s 7200s \
  taskset -c 0 python3 -u -m incremental auto \
    --base-run runs/hyp-shared-index-20261110 \
    --new-source runs/hyp-incremental-20261109/fixture/new.v \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --native-yosys /workspace/Yosys-Flow/build/native/install/bin/yosys \
    --selection-mode fine --no-equivalence-precheck \
    --yosys-timeout 3600 \
    --out "$hyp_inc" 2>&1 | tee "${hyp_inc}.console.log"
```

外层 7200 秒覆盖整个在线作业；单次前端、局部综合、参考综合及后验检查的 Yosys 调用可用 3600 秒，避免原来写死的 120 秒上限截断已知需要约 9 分钟的参考综合。其他独立来源证明预算及所有正确性检查保持不变。关闭功能不变预检查，是因为这份固定修改已有具体功能差异反例。新版前端使用当前原生工具，不重跑旧版插桩综合，也不改变旧捕获的工具身份。原版/插桩归档检查改为结构匹配，并不意味着可把新版增量网表与独立参考的 SAT 检查也省略。

第一次手工尝试 `runs/hyp-incremental-20261110-094714/` 成功命中旧索引，旧归档加载与前端表示读取共 48.471 秒，随后因工具相对路径在子进程工作目录下失效而停止；没有开始新版前端或局部综合。已修复公共 Yosys 调用入口，在切换工作目录之前将可执行文件位置转为绝对路径；上方命令也显式使用绝对路径。旧归档与索引无需重建，重跑使用新的输出目录。

程序不会逐行打印内部 Yosys 日志。运行时查看输出目录的 `progress.json`；进入替换阶段后查看 `replacement/generation.json`，它在完整候选网表写出时就保存生成时间，即使随后验证耗时或失败，也能区别“尚未生成”与“已生成但未验收”。此检查点的 `netlist_generation_seconds` 仅计替换阶段，需要加上外层 `progress.json` 中 `completed_timings.automatic_preparation_seconds` 才是完整在线生成时间；最终自动报告已经相加，不要重复计数。以实际报告为准，不能把候选写出当作验证通过。

完成后查看 `report.json`：

- `timings.netlist_generation_seconds`：新 RTL 到完整候选网表生成的在线时间，包含索引加载、前端、差分、选区及必要证据、patch、局部综合和拼接；不含旧建档和后验验证。
- `timings.post_generation_validation_seconds`：生成后验证时间。
- `timings.total_seconds`：整个自动流程时间；旁边 `.time.txt` 还包含进程启动和退出开销。
- `splice.removed_cells`、`splice.reused_cells`：实际替换与复用规模。仅 `status=passed` 的样本可作为已验证成功结果；阻塞或超时不是成功增量样本。

已测的完整新版全量生成时间为 568.034 秒，可先作历史参照；严格同机对比仍需控制负载并重测 F。这里不重新运行 F 或旧建档。

## 在线选区内存膨胀与修复验证（2026-11-10）

手工运行 `runs/hyp-incremental-20261110-095357/` 的旧归档加载 47.978 秒、新版前端与组合检查 109.579 秒、差分 88.658 秒，随后进入 `region_and_binding`。整个作业在 74 分 39 秒被 SIGKILL，峰值 RSS 254161760 KiB（242.388 GiB），未达到外层 7200 秒上限；没有 patch、replacement 或增量网表。系统记录有 OOM 杀进程事件，但无权限读取内核日志直接对应 PID，因此退出原因标为疑似 OOM。

只读抽样确认同一数据库来源文档在内存中被逐门重复解码：大于 1 MiB 的共享数组不进入普通缓存，而选区结果持续持有每份列表。当前已补齐大数组弱引用共享、来源集合交并去重、按相同来源集合批量检查每个门的实际证据路径，以及大替换计划的共享来源编码。修改没有提高旧捕获的定位精度，也没有放宽来源、边界或功能证明。

复用原归档、强制禁止原始重放的受控查询测试通过：加载 54.140 秒，完整紧凑源码查询及其路径检查 50.205 秒，全部 163799 门的批量来源证据检查 40.773 秒，门信息编码 1.137 秒，总计 146.366 秒，峰值 RSS 4.454 GiB。这些门的 698017 项来源只保留一份数组。详见 `runs/hyp-query-sharing-20261110/report.json` 和[存储说明](../provenance/docs/storage.md#在线选区的大集合共享与批量证据检查2026-11-10)。

本轮只运行上述受控查询与项目回归，**没有启动完整 hyp 增量综合**，也未执行这个大区域的完整边界函数证明。原 `hyp-shared-index-20261110/` 的 SQLite、manifest 和签名均未改写；新版只对审查过的完整实现指纹组合放行读取，仍验证原签名和所有哈希。可以继续使用上方手工命令和新的输出目录进行完整增量实验，最终是否通过及实际复用规模仍需观察。

完整项目回归最终为 **429 项通过，零失败、零错误、零跳过**，532.564 秒，见 `runs/shared-query-regression-20261110/report.json`。这不是 429 个 EPFL 大案例，也不替代上述尚未运行的完整 `hyp` 增量验收。

## 后续完整增量的递归深度失败（2026-11-10）

`runs/hyp-incremental-20261110-114506/` 成功命中旧索引，归档加载 47.277 秒、新版前端与组合检查 107.357 秒、差分 88.088 秒。源码修改种子为 `n214713` 的 AND→OR，区域计划已返回 163799 个门；随后在 `region_and_binding` 抛出 `maximum recursion depth exceeded`。总命令墙钟 46 分 4 秒，峰值 RSS 44.503 GiB，退出码 1；不是 7200 秒超时或 SIGKILL，没有生成 patch 或增量网表。进度最后显示 `region_plan_encoding` 是最近一次检查点，不是异常调用栈；`statement-plan.json` 已保存 `seed_cell_count`，说明流程已经越过计划构造。

原失败报告没有保存 traceback。随后只读取旧归档 `frontend.il`，构造 `ir_graph.Graph`，对实际旧输出 `\\hypotenuse[0]` 执行 `value(...,root=True)`，30.514 秒内独立复现递归异常，峰值 RSS 3.038 GiB。调用栈到达默认递归上限 1000，其中 431 帧为 `Graph.value`、566 帧为其列表推导。链路从 `hypotenuse[0]` 经过修改目标 `n214713`，继续沿上游逻辑递归。复现产物在 `runs/hyp-recursion-diagnosis-20261110/`，包含报告、traceback、脚本与资源计时；没有重跑综合或完整选区。

触发路径是 `statements.Slice.__init__` 为判断输入切点是否可复用，调用 `Graph.value` 比较新旧信号的完整上游结构指纹。当时实现通过 Python 函数递归遍历深组合依赖；电路虽无组合环，仍可能超过解释器调用深度。这是实现规模限制，不是发现功能不等价。本段记录当时诊断，修复与后续复查见下节。

## 深依赖显式栈修复与复查（2026-11-11）

已将前端指纹与过程临时量之间的相互调用改成显式栈调度，保留原 `(bit, local, root)` 缓存键、过程内部缓存、赋值及分支顺序和环检测。异常时按逆序退出活动帧，避免失败查询残留活动标记。`Slice.build`、模块输入依赖、`source_regions.make_cut` 和 `Circuit.value` 的依赖遍历也改为显式栈；源码展开中的别名并查集改为迭代路径压缩。没有提高 `sys.setrecursionlimit`，BDD 运算及函数序列化原本就采用迭代实现。

新增测试覆盖 3500 层门、别名、过程临时量和模块依赖，以及共享子图、缓存命中、过程优先级、切点、环路、无驱动及 X。小型随机 DAG 的完整/局部指纹逐项与旧递归算法对比；错误输入仍拒绝，未把缺失证据转为证明成功。自动流程新增选区内部进度标记及 `failure-traceback.txt`，后续异常可直接定位调用栈。

实际 `hyp` 复查只读取旧归档和上次失败目录留下的新前端，对 `hypotenuse[0]` 计算指纹，再读取旧最终网表计算同一输出的结构函数。没有重新综合、创建索引、生成契约、运行 SAT 或启动完整增量。报告为 `runs/hyp-stack-verification-20261111/report.json`，状态 `passed`。

| 检查 | 读取及建图秒 | 依赖计算秒 | 结果 |
|---|---:|---:|---|
| 旧 `frontend.il` 输出完整指纹 | 30.914 | 6.248 | 698017 个缓存条目，活动栈清空 |
| 已保存新版 `new.frontend.il` 输出完整指纹 | 32.349 | 6.246 | 698017 个缓存条目，新旧指纹不同 |
| 旧最终 163799 门网表的输出函数 | 1.504 | 1.491 | 结构 Boolean DAG 计算完成 |

进程总墙钟 **80.91 秒**，峰值 RSS **3.110 GiB**，退出码 0，递归上限仍为 1000；重复读取同一根使用缓存。固定 CPU 0，同期完整回归在 CPU 1，因此这里只是修复后的可完成性检查，不是严格性能对比，也不是完整增量生成时间。

最终完整回归 **440 项通过，零失败、零错误、零跳过**，报告总耗时 533.227 秒，见 `runs/deep-traversal-regression-20261111/report.json`。其中新增 11 项深依赖与错误诊断测试；其余覆盖原有来源记录、契约、层次、组合增量、导出及独立等价预检查。这不是 440 个大规模 EPFL 案例。

已有 `runs/hyp-shared-index-20261110/` 的记录和 SQLite 索引无需重建，索引读取实现摘要保持不变。可以使用上文命令和新的输出目录重跑完整增量。此次修复不改变旧来源仍关联全部 163799 门的事实，也不取消旧源码边界证明或最终全输出验证；完整 `hyp` 增量是否成功仍需单独验收。

## 最终网表等价检查的结构匹配实验（2026-11-10）

旧脚本读取 `baseline.il` 与 `after.il`，使用 `equiv_make gold gate equiv; hierarchy -top equiv; equiv_simple; equiv_status -assert`。默认 `equiv_make` 不匹配 `$...` 内部名字；旧日志仅匹配 384 个公开信号，未匹配内部 cell。这使现有内部结构对应未被充分利用。日志只能确定任务停在 `equiv_simple`，没有细分计时，不能把此前约 88 分钟全部归因于 SAT 求解本身。

对同一份已有 `hyp` 网表做独立、有 60 秒上限的实验，将后半段改为：

```yosys
equiv_make -inames gold gate equiv
hierarchy -top equiv
opt_clean
equiv_status -assert
```

实测 **10.446 秒**完成，退出码 0：163799 个 `$equiv` 全部 proven，0 个 unproven。此次没有调用 SAT；内部 cell/wire 匹配以及连接归一化已使等价点两侧一致。这是当前结构对齐的组合网表的结构等价检查，不代表任意新旧 RTL 都能如此处理。

负例在独立 `after.il` 副本中把一个 `$_AND_` 改成 `$_NAND_`，保持 cell 名称和接线不变。同一脚本 **10.069 秒**后以退出码 1 拒绝：163798 个 proven，1 个 unproven，说明没有仅凭同名直接接受。该负例是结构检查拒绝，不是 SAT 反例报告。

证据目录 `runs/hyp-equivalence-probe-20261110/` 包含正负例脚本、日志、正例资源计时和绑定输入哈希的 `report.json`。原版工具未变，没有重新综合或修改原捕获；该实验固定 CPU 1，后台索引仍在 CPU 0，只有一次测量，不能作为严格受控的加速比测试。生产脚本已接入结构匹配，当前新的续跑默认执行该检查，不再传入跳过选项。

## 跳过最终网表等价检查，生成 SQLite 索引（2026-11-10）

用户执行的 `runs/hyp-resumed-20261109-163735/` 已在 7200 秒超时退出。捕获检查及复制 54.949 秒，记录重放与 ABC 证据检查 1222.513 秒，原版综合 606.912 秒；进入 `equivalence` 时总计约 1916.849 秒。日志停在 `Executing EQUIV_SIMPLE pass`，其后直到超时仍未完成，SQLite 创建尚未开始。峰值内存约 39.70 GiB，退出码 124；manifest 的 `running` 是外层超时终止留下的状态，不代表进程还在运行。

按用户要求新增并使用显式 `--skip-stock-equivalence`。本次目录为 `runs/hyp-index-20261110-035756/`，复用上一份已保存 `capture.json` 的捕获，不重跑插桩综合；需要重建进程退出时丢失的内存状态。仍重放记录、检查 ABC 证据、执行原版综合并比较最终结构，然后跳过这项形式等价检查创建 SQLite。记录策略见[存储说明](../provenance/docs/storage.md#捕获完成后的续建档)。以下命令用于复现；执行结果以目录内 manifest 与旁边进度、计时文件为准。

本次已实际进入 `verified_index_build`：捕获检查及复制 55.723 秒，记录验证 1212.453 秒，原版综合 488.749 秒。`validation.json` 中 `stock_flow_exact_match=true`、`stock_to_traced_equivalence=null`，`skipped_checks` 写明显式跳过，不存在 `equivalence.log`。约在总计 1804.892 秒进入索引阶段；已创建 `verified-index.sqlite3.tmp`，但查询关系展开未完成，**这次旧尝试未发布索引**；随后检查进程已经退出。阶段进入时间不等于索引耗时，最终耗时和体积待完成后填写。

```bash
hyp_index="runs/hyp-index-$(date +%Y%m%d-%H%M%S)"
/usr/bin/time -v -o "${hyp_index}.time.txt" \
  timeout --kill-after=15s 7200s \
  taskset -c 0 python3 -u -m yosys_flow resume \
    runs/hyp-resumed-20261109-163735 \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --index-backend sqlite --skip-stock-equivalence \
    --out "$hyp_index" > "${hyp_index}.console.log" 2>&1
```

该选项通过 13 项续建档专项测试，包括 SQLite/gzip 索引加载、原始重放、默认恢复证明、保留层次视图证明，以及 ABC 证据失败、原版结构不同和伪造证明状态的拒绝测试。另有 80 项原生加载、CLI、双后端索引和层次回归全部通过，零跳过。日志分别为 `runs/hyp-skip-equivalence-tests/resume.log`、`runs/hyp-skip-equivalence-tests/index-regression.log`。

## 继续现有捕获

现已提供 `resume` 入口。下面的命令复用 `runs/hyp-optimized-20261109-104148/` 的捕获，在新目录完成重放、ABC 证据验证、原版对照、等价检查和 SQLite 索引发布；不会重新执行插桩 `synth` 或插桩 ABC 优化。原目录保持不变。原版对照仍会运行一次普通 `synth`（包括其普通 ABC），这是验收步骤。

这份旧记录没有 `capture.json`，会采用旧版时间戳保护并核对原插桩工具哈希。请保持源码、包含文件、Yosys 数据文件和原捕获工具不变；不要为了续跑先重建工具。入口实现时的轻量检查中，`hyp.v`、原生 Yosys、ABC 和 `techmap.v` 均存在且时间早于捕获，原生二进制哈希匹配。随后用户执行结果见上节。

```bash
cd /workspace/Yosys-Flow
mkdir -p runs
hyp_resume="runs/hyp-resumed-$(date +%Y%m%d-%H%M%S)"
set -o pipefail
/usr/bin/time -v -o "${hyp_resume}.time.txt" \
  timeout --kill-after=15s 7200s \
  taskset -c 0 python3 -u -m yosys_flow resume \
    runs/hyp-optimized-20261109-104148 \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --index-backend sqlite \
    --out "$hyp_resume" 2>&1 | tee "${hyp_resume}.console.log"
```

启动后会显示 `capture_inventory`、`capture_binding`、`capture_copy`，然后进入重放和证明阶段。实时进度保存在 `${hyp_resume}.progress.json`；成功标准为新目录 `manifest.json` 的 `status=complete`、全部检查通过且生成 `verified-index.sqlite3`。`capture.json` 只是捕获字节及输入绑定检查点，不表示证明已经通过。

续跑会重新恢复 Python 中的记录状态，上次 425.5 秒的诊断报告不能代替它。复制采用独立文件，不使用硬链接，因此新目录需要容纳另一份捕获；原有原始日志、脚本和源码坐标不改写。若续跑在检查点发布后再次中断，可将这个新目录传给下一次 `resume --out <另一个新目录>`；若在复制或检查点发布时中断，则从原始捕获目录重试。仍会重做验证，不信任中断时留下的索引或验证报告。原有工具时间记在 `resume.source_timings_seconds` 中，不伪装成此次执行的时间。

入口验证：完整回归 405 项通过、零失败、零跳过；补充复制中断保护后，9 项续建档专项测试全部通过。报告和日志分别在 `runs/resume-archive-tests/regression/report.json`、`runs/resume-archive-tests/resume-final.log`。这是入口初版的验证记录，早于本页新增的跳过选项与实际续跑。

## 结果

最新用户手工复测 `runs/hyp-optimized-20261109-104148/` 在 **7200 秒**退出，退出码 124：完整插桩 `synth`、ABC 和最终网表写出已完成，后处理未完成；原版对照和索引尚未开始。原生记录有 `run_end`，ABC 记录有成功 `end`，但这不等于归档已经验证成功。下面先保留首次 3600 秒实验，后文另记后处理优化和重放诊断。

首次建档在 **3600 秒预算内未完成**，停止于 ABC `&dch -f`。局部修改及新版全量综合已完成，但没有可用旧归档，**本轮无法测量 P-fine 生成时间或验证后总时间**。

旧版原版全量综合为用户提供的同日实测：488.600 秒，随后结构检查 3.971 秒。它不是新版局部修改后的全量时间，也不是来源记录或形式等价验证成功的证明。

| 流程 | 完整网表生成 s | 生成后验证 s | 作业总时间 s | 状态 |
|---|---:|---:|---:|---|
| 旧版原版全量（用户已有样本） | 488.600 | 3.971 | 492.571 | 结构检查通过 |
| 新版原版全量 F | 568.034 | 3.956 | 571.992 | 结构检查通过 |
| 新版 P-fine | 未执行 | 未执行 | 未执行 | 受旧建档阻塞 |

首次建档独立统计，实际作业墙钟 3600.845 秒；它不计入在线增量时间。未执行/未完成的数据不填 0，也不计算加速比。

## 案例与修改

- 上游：lsils/benchmarks 固定提交 `82d8cc6910419298e713a46644ed59fd3df53038` 的 `arithmetic/hyp.v`；沿用本地筛选包，不重新下载或更换版本。
- 原源码 SHA-256：`ca578a27ce121d7f4ae9f56b02f071661118871534d314e187635b1b2309e653`。
- 单模块 `top`，239,751 行，214,335 条连续赋值，256 个输入位、128 个输出位。
- 用户已有原版全量网表为 163,799 个通用门（按原版 `full.log` 的最终 `stat`）；本次不进行工艺库面积比较。
- 沿用此前 EPFL local 规则：只依据源码拓扑，在到达输出的内部 AND 中按输出数量、到输出距离和源码顺序选择；在建档结果出来前固定。没有根据综合或增量收益重新挑位置。
- 第 239742 行 `n214713` 的 AND 改为 OR，源码上仅到达一个顶层输出；接口、其他源码、综合约束保持不变，没有新增 keep 或包装模块。
- 新源码 SHA-256：`f8b4cc04db4df1b6ca363be527e80b1fb44e59ee97d9f2f4eef89c48f7f34725`。

```verilog
  assign n214713 = ~\hypotenuse[127]  & ~n214712;
// 新版：
  assign n214713 = ~\hypotenuse[127]  | ~n214712;
```

找到了使 `hypotenuse[0]` 从 0 变为 1 的具体二值输入。先经位并行源码模拟找到，再经独立标量源码模拟重放；原版 Yosys 对这组输入重放所有输出的结果为 **passed**。这只证明该修改确实改变功能，不证明未来增量网表正确。预定的增量输入只有旧归档和完整新版 RTL，不提供修改行号或手工 patch；本轮尚未调用增量规划器。

## 首次建档阶段

| 阶段 | 墙钟 s | 状态 |
|---|---:|---|
| 插桩综合及原始记录 | 3600.410 | 未完成（已耗时间） |
| 原生记录重放 | — | 未到达 |
| ABC 证据验证 | — | 未到达 |
| 独立原版综合对照 | — | 未到达 |
| 旧归档等价检查 | — | 未到达 |
| 查询索引构建 | — | 未到达 |

包含关系的时间不可重复相加。每作业预算 3600 秒，超时后的清理/报告可能有额外时间；未完成阶段只能报告已耗时。固定 CPU 0，默认 SQLite，普通 `synth -top top`，未展平、未修改 pass 列表或 ABC 脚本，不限产物体积。

- ABC 开始的日志首次观测约在 834.4 秒；这是每 5 秒采样日志的观测时间，不是精确 pass 边界计时。
- 未完成的归档目录约 4.026 GiB；停止后的 ABC 临时 JSONL 约 46.266 GiB。ABC 文件完成后才复制到正式归档，二者存在生命周期差异，复制完成时不能重复相加为原始记录大小。
- 采样的进程树 RSS 峰值约 7.019 GiB；共享页可能重复计数。
- 实现、工具及旧源码的哈希与运行前一致。

## ABC 记录体积归因

所有综合计时结束后，顺序扫描冻结的临时 JSONL，按记录头中的 `kind`/`encoding` 统计实际字节量：

| 记录类别 | 条数 | GiB | ABC JSONL 占比 |
|---|---:|---:|---:|
| 编码为 checkpoint 的全量网络 | 3,591 | 45.176 | 97.65% |
| 编码为 delta 的变化网络 | 454,265 | 0.895 | 1.93% |
| 其他网络、复制、替换及命令事件 | 457,887 | 0.195 | 0.42% |

这是文件体积归因，不是各类别的 CPU 时间归因。扫描只做记录头和字节统计，没有重放或证明来源关系。最后一行完整，不代表流程完整：记录包含第 12 条 `&dch -f` 的开始事件，缺少它的结束事件及后续映射记录。Yosys 原生 JSONL 另有 3.692 GiB，两份 JSONL 合计约 49.958 GiB；临时 ABC 文件还没有并入归档目录。

## 计时与验证口径

在线生成时间沿用项目现有定义，包含新版前端、差分、选区/必要证据、patch、局部综合、绑定拼接及完整网表写出。旧建档不计入。完成验证后的作业总时间包含生成后验证、报告和产物整理。

每项只有一次测量，不估计运行方差。旧版表格总时间为用户提供的生成与检查时间之和；新版为 worker 实测总墙钟。

F 的后检查为结构检查；P-fine 还会执行本项目的局部输出、完整新版参考综合、全网表等价、Verilog 往返等检查。两者的后验验证范围不同，总时间需据此解释。本轮因旧归档未完成，没有执行增量 worker，也没有修改任何已执行综合的内部超时或证明规则。所有执行的作业整体预算均为 3600 秒。

## 超时样本的阻塞

这次停止在默认 ABC 脚本的 `&dch -f`（command 12），并未到达映射完成、原始记录重放、ABC 证据验证、独立原版对照或 SQLite 建索引。`index_bytes=0` 表示索引尚未生成，不能解释成索引不占空间。

这次超时使用的旧版 `provenance/native/abc/flow.c` 在每次观测时扫描网络，并且每 128 次观测重新写一个全量检查点。当时的测量没有中途修改实现。下面记录了随后针对重复全网扫描及检查点的优化；其大案例效果仍需用同一源码和修改重测。只有旧归档完成后，才能衡量 P-fine 是否省时。

## 原始证据

产物保存在 `runs/hyp-incremental-20261109/`，旧筛选目录未改动：

- `plan.json`、`archive-result.json`、`report.json`：输入/工具/实现绑定、建档结论、对比状态。
- `archive/`：实际脚本、日志、原始记录、阶段 profile、每 10 秒资源采样。
- `stage-observations.jsonl`、`abc-resources.jsonl`：阶段与临时 ABC 体积观测。
- `fixture/`、`witness-base/`、`witness-new/`、`witness-replay.json`：固定修改、具体差异输入和全部输出重放。
- `final-storage.json`、`abc-record-size-breakdown.json`：最终体积与逐类别统计，诊断扫描耗时不计入综合测量。
- `full/full/`：新版原版网表、综合脚本及结构检查日志。
- `abc-temporary/origin.json`：本次 ABC 临时证据保存位置；它不属于经过验证的归档。
- `archive.py`、`prepare_fixture.py`、`continue_after_archive.py` 等：这次实验的执行脚本，输出目录需新建，不能覆盖当前结果。

本轮属于扩大规模的可行性实验，没有在执行途中优化记录器，也没有声称任意组合 RTL 已支持或获得增量加速。

## 后续记录优化与手工复测

上面的数字属于优化前的实测，保留不变。随后针对它修改了记录与重放：取消每 128 次观察的默认全网检查点；`dc2/dch` 根据实际 AIG 结构变更生成差量；入口复制使用存活对象地址索引；重放共享每个 ID 的历史版本，并用引用计数检查删除是否合法。函数证明、原版对照和完整归档的验收条件未放宽。实现范围、诊断开关见 [storage.md](../provenance/docs/storage.md)。

按用户要求，代理修改后**没有重新启动 hyp 建档**；用户随后自行运行了文首的 7200 秒样本。小案例与项目回归用于检查正确性，不能替代大案例完整建档验收。

插桩 ABC 已重新编译，完整回归 **389 项通过，零失败、零跳过**；报告为 `runs/abc-recording-optimization/regression/report.json`。

在自己的终端执行以下命令。每次使用新目录，保留旧实验；固定 CPU 0，时间上限为用户选择的 7200 秒，不设文件体积上限：

```bash
cd /workspace/Yosys-Flow
make native NATIVE_JOBS=4
mkdir -p runs
hyp_run="runs/hyp-optimized-$(date +%Y%m%d-%H%M%S)"
unset YOSYS_FLOW_ABC_FULL_SNAPSHOTS YOSYS_FLOW_ABC_SCAN_SNAPSHOTS
unset YOSYS_FLOW_ABC_VERIFY_DIRTY YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL
set -o pipefail
/usr/bin/time -v -o "${hyp_run}.time.txt" \
  timeout --kill-after=15s 7200s \
  taskset -c 0 python3 -u -m yosys_flow run \
    --flow synth \
    --source build/epfl-large-screen/sources/hyp.v \
    --top top \
    --yosys /opt/oss-cad-suite/bin/yosys \
    --native-yosys build/native/install/bin/yosys \
    --index-backend sqlite \
    --out "$hyp_run" 2>&1 | tee "${hyp_run}.console.log"
```

GNU `timeout` 负责整个命令进程组的超时清理；达到上限后先发送 TERM，15 秒后仍未退出则发送 KILL。`pipefail` 防止 `tee` 隐藏失败状态。`time.txt` 保存整个建档命令的墙钟和资源数据；其最大 RSS 是进程高水位口径，不能当作同时存活进程树的内存总和。

运行时可在另一终端查看新目录下的 `traced.log`。ABC 临时目录名会出现在日志中；尚未完成时其记录还没有复制进正式归档。成功标准是命令退出成功、`manifest.json` 的 `status` 为 `complete` 且生成 `verified-index.sqlite3`，不是仅看到 `after.v`。成功 manifest 还包含各建档阶段的计时。若超时，保留控制台日志、`time.txt` 和未完成目录，不能拿部分记录进入增量流程。

## 7200 秒样本的后处理诊断

用户样本的最大 RSS 为 54,801,680 KiB（约 52.3 GiB），原生 JSONL 4.215 GiB、6,036,480 条，ABC JSONL 1.780 GiB。最终网表为 163,799 个门。按产物时间戳估算，综合与写出约 66 分钟，随后约 54 分钟处于后处理；没有细分计时，不能反推出超时瞬间的调用栈。最初曾有重复任务竞争同一 CPU，这不是与原版全量的严格速度对照。

后处理代码发现的开销包括：420,512 个输入 cell 的线性列表成员检查；对同一份巨大共享数组重复执行元素比较；相同输出锥逐门重复合并、排序和保留成员键；同时常驻完整原始 JSON 解码树和重放结果。已分别改为集合校验、身份优先比较、按边界缓存并共享候选，以及按文件偏移流式读取。详见 [存储说明](../provenance/docs/storage.md)。校验规则、来源精度、综合流程及证明预算没有放宽。

现在 `run` 会立即输出 `[yosys-flow] traced: running`，后续输出当前后处理阶段，并在目录旁保存 `${hyp_run}.progress.json`。强制终止可能留下最后一次进度；需同时看进程和更新时间。进度文件不是归档成功标志，也不参与证据签名。

已有原始记录可以单独诊断，无需再运行整次综合。下列命令默认验证原生生命周期、候选细化和最终网表一致性；如需继续检查 ABC 内部证据，再添加 `--with-abc`。它不会写入原运行目录、生成索引或发布完整归档：

```bash
python3 -u provenance/tools/replay_native.py \
  runs/hyp-optimized-20261109-104148 \
  --out "runs/hyp-replay-$(date +%Y%m%d-%H%M%S)" \
  --timeout 1800 --memory-gib 64
```

`--timeout` 是该诊断的总秒数，`--memory-gib` 是诊断进程的地址空间上限；它们不修改生产建档或功能证明预算。诊断遇到超时会输出栈和失败阶段，不把未验证的部分记为通过。工具输出的峰值 RSS 是进程高水位，不是总地址空间。

## 后处理优化实测

最终实现只读取用户的同一份旧记录，在 600 秒诊断预算、48 GiB 地址空间上限内完成原生重放、候选细化和最终网表核对，结果为 **passed**。没有重新运行 `hyp` 综合。

| 阶段 | 实测 s |
|---|---:|
| 原始记录偏移扫描 | 4.591 |
| 原生生命周期重放 | 189.689 |
| ABC 输入锥 | 34.009 |
| ABC 输出锥 | 60.207 |
| 候选集合生成 | 41.208 |
| 来源关系构建与传播 | 89.042 |
| 最终网表核对 | 6.742 |
| 上述处理合计 | **425.490** |

峰值 RSS 为 **26.163 GiB**；核对 6,036,480 条原生记录和 163,799 个最终 cell。163,927 个关联对象共享 128 组不同成员集合。报告和阶段明细为 `runs/hyp-postprocess-optimization/replay-final/{report,progress}.json`。

这是已有记录的诊断，不包含 ABC 内部证据函数证明、原版对照、归档等价检查或索引生成；原始运行目录仍是未完成归档，不能用于在线增量。该诊断没有绑定 CPU 0，完整回归同时运行；上述秒数用于确认处理能完成和定位后续开销，不能与原 7200 秒作业直接计算整体加速比。52.3 GiB 是旧整次作业的峰值，26.2 GiB 是本次诊断的峰值，两者测量范围也不同。

中间诊断 `replay-1` 在确认共享数组的逐元素比较热点后主动停止；`replay-2` 在候选并集阶段达到 360 秒预算，促成了后续位集合与成员块共享优化。它们都不是通过样本。初始候选缓存微基准另存于 `candidate-microbenchmark.json`，明确标为位集合/成员块优化前的局部测量，不当作最终实现的完整性能数据。

最终完整回归 **397 项通过，零失败、零跳过**，耗时 614.139 秒，报告在 `runs/hyp-postprocess-optimization/regression-final/report.json`。它覆盖两种索引、来源选择、证据损坏拒绝、层次、契约及增量流程；回归总时间不计入上表的 `hyp` 诊断时间。
