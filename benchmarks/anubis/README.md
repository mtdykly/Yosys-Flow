# ANUBIS 修改与等价基准

新增的外部 RTL 和修改均来自 [masc-ucsc/anubis](https://github.com/masc-ucsc/anubis/tree/93088bd3c05407ccd871e8d5067d024f812aeeaa)，固定提交 `93088bd3c05407ccd871e8d5067d024f812aeeaa`。`upstream/` 保留原始文件和许可/免责声明；`lock.json` 逐文件固定 SHA-256，每次运行先校验。正常运行不需要联网。上游通过宏启用修改，这些是现成基准变体，**不声称每项都对应两个历史提交**。

本命令在仓库检出中运行，使用随仓库保存的语料；不是完整 ANUBIS 设计集或官方评分流程。没有接入 YSYX、EPFL 或其他外部测试库。原有内部回归测试不删除。

`incremental compare` 的主要时间与网表规模比较仅使用下文有实际功能变化的组合适配案例 `compare_local9`、`control_local2`，默认关闭功能不变预检查。主计时覆盖从输入到整份新版网表写出，拼接后的正确性验证单列；通过验证才纳入加速比和规模统计。其余 NOC、锁存器负例和人工控制可显式选作诊断，不进入主要性能结论，也不与原始 30 个 NOC 宏调查混算。配置和计时口径见 [对比实验](../../incremental/comparison.md)，固定目录见 [comparison-cases.json](comparison-cases.json)。

## 按上游顶层与配置验证 NOC

判断原始 NOC 是否影响实际设计，使用这个入口：

```bash
python3 -m incremental anubis-nochange --upstream-top \
  --repeats 1 --case-timeout 600 --frontend-timeout 300 \
  --out runs/anubis-noc-upstream-final
```

`--upstream-top` 与 `--with-sequential` 模块上下文调查互斥。固定上游 `scripts/yosys/*_files.tcl` 的文件顺序、语言选项和 include 设置，从 `Makefile.config` 读取真正顶层：ALPHA `pipeline`、DLX `cpu_bug`、FPU `fpu`、MOR1KX `mor1kx`、OR1200 `or1200_top`。不添加 `Fast=0`、`Left=1` 或额外功能开关，实际参数来自原始实例。上游 Yosys Makefile 未使用 `DEFS_MOR1KX`，本入口也不自行添加。文件清单、Makefile 和基准脚本原文纳入 `nochange-lock.json`。

每个宏仅一个上游配置，共 30 项。首先通过 `hierarchy -check` 比较完整活动层次，保留子模块类型、参数与连接。仍有差异则使用 `proc`、`opt_expr`、`memory_collect`、清理和内部名称规范化；保留状态、初始化、内存内容及全部顶层端口。然后对变化叶模块尝试原有全输出 SAT，**局部反例仅作诊断**；再展开完整上下文检查是否被上游常量或下游未使用关系消除。仍不同则尝试完整顶层 SAT/EQY，只有此处真实输出反例才能记为设计 `different`。不支持的时钟、未驱动、X、未证明或超时仍为 `unknown`，不额外假设启动复位。

本入口不执行完整 `synth`/ABC，不生成复用网表。`structural_same` 是结构一致性证据，不是 SAT/EQY 成功，也不证明全部输出有定义；所有条目保持 `reuse_authorized=false`。局部二值等价不在尚未验证的时序/X 上下文中自动升级为顶层等价。

### 顶层诊断的已知限制

| 用例 | 待处理范围 |
| --- | --- |
| `dlx_noc2` | 完整 `cpu_bug` 同时包含正沿和负沿状态；现有后端仅接受单个正沿时钟。局部比较器保留原始 X 默认输出 |
| `fpu_noc5` | 顶层 `rco[4:0]` 未驱动；不能删除这些输出或补零来通过定义性检查 |
| `mor1kx_noc3` | 顶层未驱动信号需要独立分析；局部等价不能自动授权完整设计复用 |
| `or1200_noc3` | 实际状态使用 `clk_i`、`iwb_clk_i`、`dwb_clk_i`，且有异步复位；不能直接采用上游脚本的 `clk` 标签或强制同步 |

实施顺序、X/未驱动语义、反例要求和验收条件统一维护在 [时序验证后续计划](../../incremental/sequential.md#后续补齐计划)。每次调查重新输出逐项结论，不用历史汇总代替本次证明。

FPU 的独立模块配置与原始顶层不同：`fp_lead0 Fast=0` 在真实实例中为 `Fast=1`；`fp_lead0_p0`、`fp_shift0`、`fp_lead0_p1` 未进入原始 FPU 活动层次。因此独立模块反例只用于局部诊断，不能直接判定上游 NOC 失败；同样不能仅凭 NOC 标签预设等价。

产物含 `configurations.json`、每版 `frontend.il`、规范化表示、局部证明、顶层尝试及诊断、`report.json`、`timings.csv` 和证据哈希。输入、工具、实现哈希在结束时核对；运行结果仍不提交。结构一致性不是求解器耗时样本；模块上下文的时间只代表对应边界和配置，不用于推定完整顶层成本。

`incremental.tests.test_nochange_top` 验收原始 FPU 上下文、子模块类型/参数/初始化差异、不可达局部反例、真实顶层反例，以及顶层未知时禁止提升局部结论。错误注入测试不计入 30 个原始宏。

## 组合叶模块诊断（非上游顶层）

这是独立于下文六项 DLX 替换实验的入口。`nochange-lock.json` 记录固定提交完整归档的哈希、五个设计目录的 HDL 扫描清单、每个 NOC 宏的全部位置，以及本地保存的原文件/头文件/许可证的 SHA-256。`nochange-cases.json` 登记包含修改的模块、支持范围和参数配置。原始源码逐字节保留，正常运行不需要联网。

```bash
# 默认：全部清单，单次证明保护上限 600 秒，无共享总预算
# 成功证明串行重复 3 次；输出目录必须不存在
python3 -m incremental anubis-nochange --out runs/anubis-nochange

# 只盘点，不启动 Yosys
python3 -m incremental anubis-nochange --inventory-only --out runs/nochange-inventory

# 单独复查；只改变评测保护上限，不改变 auto 默认值
python3 -m incremental anubis-nochange --cases dlx_noc1 fpu_noc0 \
  --case-timeout 1200 --frontend-timeout 120 --repeats 5 --out runs/nochange-selected
```

源码中共 30 个不同的 NOC 宏：ALPHA 6、DLX 4、FPU 7、MOR1KX 7、OR1200 6。同一宏出现多处只算一项。上游脚本使用闭区间 `seq 0 NOC_*`，还会枚举五个源码中不存在的宏：`alpha_noc6`、`dlx_noc4`、`fpu_noc7`、`or1200_noc6`、`or1200_noc7`。这些缺失项仅在清单中披露，不伪造为等价通过。

30 项全部在报告中交代去向。19 项在模块范围审查时排除：17 项涉及状态、时序上下文或头文件配置，另 2 项为 ALPHA `ex_stage` 的组合非叶模块，当前生产预检查不处理这种模块对。这些项保留源码位置、理由和可用的状态语法证据，没有执行功能证明，也不把组合片段从状态上下文中裁出。其余 11 个叶模块变体配置为 15 个参数组合；`fp_lead0` 同时测 `Fast=0/1`（`Bits=64`），`fp_shift0/1` 同时测 `Left=0/1`。这不是 CPU/FPU 顶层全部可达行为的证明，也没有穷举所有参数。

评测使用固定版本的**原版 Yosys 前端和当前 `incremental.equivalence.prove` 后端**。先读取原文件，旧版不启用 NOC，新版只启用目标 NOC；保留预处理输出与 diff、`proc` 前 RTLIL 和粗粒度检查结果。接口相同、没有状态的叶模块进入同一个 `miter + sat`，比较所有输出并要求输出有定义。结构相同的配置单列 `structural_same`，不消耗 SAT、不计入 SAT 成功数。每例独立截止时间；未知、错误、超时均不能算证明成功。这里不执行 `synth`、ABC、来源查询或替换，stock 前端能接受某种结构不代表原生来源钩子已覆盖它。

报告分别统计 `equivalent`、`different`、`structural_same`、`unknown` 和 `out_of_scope`，并保留参数配置与唯一宏两种分母。`completed_with_findings` 表示调查完成，不表示全部等价；真实反例是调查发现，输入损坏等实验错误则返回 `failed`。

`dlx_noc2` 保留原始 `default: Result = dc`，需要检查输出定义性；`fpu_noc3/4 Left=1` 可能在预处理后保留锁存器，不能进入组合证明。下文适配的 `compare_noc2` 显式将新旧 X 默认值都改为 0，两种入口不混算。

每次运行保存：

- `inventory.json`：全部宏位置、逐文件哈希、范围审查、上游多枚举的空宏。
- `inputs/`：经哈希校验的原始输入副本；结束前再次核对，损坏则报告失败。
- `<case>/profile-N/`：参数、预处理 diff、原始前端表示、粗粒度检查与逐例 `report.json`。
- `proof-N/`：每次独立证明的 miter、SAT 脚本、完整日志和反例；没有使用跨重复缓存。
- 根目录 `report.json`、`timings.csv`：全部去向和时间；包含源码准备、miter/检查、SAT 命令阶段的分项统计。`sat_command_seconds` 含导入和建模，不是纯求解器 CPU 时间。

预算统计只采纳**原始叶模块、前端结构确实不同、证明成功**的配置，保存首轮和重复测量的最小/中位/最大值。首次源码前端处理另计；证明后端耗时不是整条增量流程总成本。

报告给出各保护阈值对成功样本的观测覆盖数，不把未知、结构相同和未执行项纳入成功分母。独立案例不能确定多模块共享的总预算，也不能凭少量样本估计普遍的 P95/P99。`auto` 仍使用每次 5 秒、共享 15 秒的默认值；这里只调节评测上限，不自动更改生产参数。

专项回归位于 `incremental/tests/test_nochange.py`。完整项目回归会将这套结果保存到 `nochange/survey/`；运行结果仍由 `.gitignore` 排除，源码、目录清单和验收代码可以提交。

## 包含时序的模块上下文诊断（非上游顶层）

```bash
python3 -m incremental anubis-nochange --with-sequential \
  --case-timeout 600 --frontend-timeout 300 --repeats 3 \
  --out runs/anubis-noc-combined
```

该模式重新检查之前排除的 19 个宏，仍只使用同一固定 ANUBIS 提交。`nochange-contexts.json` 固定顶层、时钟、启动复位及功能开关；原始依赖文件均按完整源码扫描的 SHA-256 核对并保存。读入各设计的 RTL 后由 `hierarchy -check` 选择包含修改的顶层，不读 testbench，不把状态边界切成无约束输入，也不将异步复位改成同步复位。

统计口径为 **30 个宏、35 个参数/功能配置**：保留原有四个 FPU 宏的双参数配置，另对 `or1200_noc5` 分别检查默认禁用和显式启用 `OR1200_PM_IMPLEMENTED` 的配置。`or1200_noc2` 是头文件修改，使用真正消费该配置的 `or1200_dc_top`；不能把头文件本身当作可证明模块。这些是指定边界和配置下的实验，不是完整 CPU/FPU 所有参数与可达行为的认证。

- 原来的组合叶模块使用相同 SAT 后端。新增组合非叶上下文先展平证明表示再调用该后端，报告为 `flattened_comb_diagnostic`；这不扩大 `auto` 的叶模块预检查范围。
- 符合单时钟、初始化/同步启动复位约定的时序设计，实际调用 `seq-baseline` 和 `seq-reuse` 的实现。先综合并证明旧网表，再以完整新版为规范检查归档门级网表；成功才逐字节复制旧网表。新版不执行完整综合。
- 有同步启动复位的上下文使用显式两周期启动协议，复位释放后观察全部输出；随后输入和复位任意变化。无启动复位的上下文依赖原始已知初始化。完整条件写入每例 `config`，不作无条件等价声明。
- 时序后端保留原有的全输出、状态定义性、初始状态、时钟和证据完整性检查。旧基准本身未能证明时记 `old_baseline_not_proven`，不把它称作 NOC 修改不等价，也不继续交付复用网表。

所有配置先完成范围检查，再串行执行证明。成功重复三次，不使用跨重复的证明缓存；结构相同、宏未生效、未证明和不等价分别报告。每次证明的 600 秒是防挂死上限，没有跨案例共享的 15 秒预算；触及上限的时间是截断观测，不是完成证明所需时间。

`report.json` 保留逐宏与逐配置两个统计口径。`timings.csv` 分开保存前端、旧网表建档、证明的首轮/中位/最大时间，以及时序新版检查与复用的总时间。`baseline/`、`reuse-N/` 中保留 EQY/SBY 分区、策略、完整日志、状态、原始输入快照及哈希；根报告绑定工具、输入、配置和实现版本，实验中变化则不能报告正常完成。

预算分析分开统计 SAT 和 EQY。只有前端表示确实改变且证明等价的原始配置进入正样本；注释/未生效改写不用于推算真实修改的证明预算。独立单设计实验不能确定多个变化模块共享的总预算，当前也没有据此自动修改生产默认值。该调查不属于每次提交必跑的短回归；其清单、过滤、拒绝条件、证据损坏和统计逻辑由 `incremental/tests/test_nochange_full.py` 验收。

未证明原因须单列：旧基准未完成无界证明、原始 X 输出、前端未驱动信号和求解超时不是同一种结果。不得删除观察输出、补零或用有限步检查替代无界证明来提高通过率。

时序入口目前一次检查一份完整设计，没有跨变化模块共享的总预算；组合的 15 秒不能直接套在 EQY 上。当前时序 `--proof-timeout` 默认 120 秒，调查命令可显式提高保护上限；超时是截断观测，不能作为完成证明耗时。需要重新生成报告后，分别分析前端表示变化的成功样本、注释/未生效修改、失败和超时，再讨论预算。

`incremental/tests/test_nochange.py`、`test_nochange_full.py` 和 `test_nochange_top.py` 检查清单、配置、证据损坏、范围过滤与统计。它们是本仓库测试，不是额外 ANUBIS 宏。

## 运行与产物

```bash
make NATIVE_JOBS=4
python3 -m incremental anubis --out runs/anubis-demo
python3 -m incremental anubis --cases compare_local9 --mode retained --out runs/anubis-local9
python3 -m incremental anubis --cases bypass_with_adder --out runs/anubis-arithmetic
# 自动差分、提取与绑定：算法仅收到旧记录和完整新版
python3 -m incremental anubis --automatic --out runs/anubis-automatic
```

默认运行六项用例，各执行保留层次和显式展平两种模式。结果目录必须不存在。输出 `report.json` 区分 `passed`、`expected_rejection`、`blocked`、`failed`；有阻塞而无异常时汇总为 `completed_with_limitations`，命令正常退出，但这不表示所有实验通过。完整回归对已知阻塞检查具体原因，并要求零跳过。

每项用例保存：

- `fixture/`：原版、完整新版、两个叶模块、宏定义、顶层包装和 `fixture.json`。清单记录来源提交、输入哈希、实际启用宏、适配及行号映射；前后核对这些输入。
- `<mode>/base-run/`：旧设计的一次插桩全量综合，以及原版对照验证；含内部 ABC 记录。
- `edit-query.json`：宏修改对应原源码范围的初始查询。`region.contract.json` **明确扩大到整个 `benchmark/u_target` 叶模块**，不宣称最小修改区域。
- 通过旧契约验证后才生成 `patch.v`、`bindings.json`、`patch-specification.json`。patch 明确实例化完整修改叶模块；若某些旧输入位不在边界，显式接 0 并记录，随后必须以完整新版全输出 SAT 验证它们确实不是新依赖。
- `replacement/`：只综合局部 patch、拼接、验证独立边界输出、所有顶层输出和 Verilog 重读。所有未选中门及引脚连接逐项不变。完整新设计仅作验证参考，不复制其门。
- `case-report.json`：状态、选区大小、外部使用者检查、证明资源、记录字节数及各阶段耗时。失败/阻塞不发布增量网表。

上面的 patch 构造步骤描述显式模式。`--automatic` 改为调用 [auto](../../incremental/automatic.md)，不把 `fixture.json` 中的修改行号、目标实例或局部规范交给算法。它先比较旧 `frontend.il` 与完整新版，对接口与实例上下文未变的变化叶模块做有预算的 `miter + sat`。全部修改证明等价则直接复用旧网表；其余从变化赋值和依赖切片生成局部表示、绑定和 `replacement-plan.json`，整模块作为后备。产物保存到每种模式的 `automatic/`，逐项报告实际完成、拒绝或证据阻塞原因。

## 显式替换模式的适配与验收

以下是固定案例的验收条件及已知阻塞，不代替本次实际运行报告。

| 用例 | 上游修改/压力来源 | 保留层次 | 显式展平 |
| --- | --- | --- | --- |
| `bypass_noc1` | `bypass_ex.v` / `ANUBIS_NOC_1`：反转条件并交换分支 | 要求替换后新旧全部输出等价 | `dc2` 阶段函数见证缺失，阻塞 |
| `compare_noc2` | `quick_compare.v` / `ANUBIS_NOC_2`：`!=` 改为 `~(==)` | 要求替换后新旧全部输出等价 | `dc2` 阶段函数见证缺失，阻塞 |
| `compare_local9` | `quick_compare.v` / `ANUBIS_LOCAL_9`：符号位条件 0→1 | 要求增量结果等价于完整新版，并保存新旧不同的 SAT 见证 | `dc2` 阶段函数见证缺失，阻塞 |
| `control_local2` | `alu_control.v` / `ANUBIS_LOCAL_2`：无符号选择改成有符号 | 来源候选、连接消费者未完全绑定，阻塞 | 同类证据不足，阻塞 |
| `writeback_local10` | `wb_control.v` / `ANUBIS_LOCAL_10`：删除默认赋值 | 要求拒绝新增锁存行为 | 要求拒绝新增锁存行为 |
| `bypass_with_adder` | `NOC_1` 旁放 `alu.v` 的原始 32 位 ADD 分支 | 要求成功替换且保留独立加法器及另一实例 | 无 BDD 预算回退；仍因阶段见证缺失阻塞 |

两份实例使用独立顶层输入，目标输出和另一实例输出都参与额外的异或汇总。全输出 miter 因此覆盖未修改实例及汇总；保留层次模式的最终替换交付仍为展平网表。

`quick_compare` 的 `default: Result = dc` 与 `alu_control` 的 `default: ALUsel = dc8` 在新旧两版中统一改为 0。保留 `casex` 模式中的 `dc6` 和原始宏定义。宏分支选择把未启用行留为空行，不移动原始行号。模块重命名和包装是显式适配，不能把结果称为未经修改的原始基准。

压力例只提取 `alu.v` 的 `ALUout = RSbus + Y`，保留原行号、32 位宽度和原文件的免责声明；包装提供独立操作数。验收要求查询旁路修改不选入独立加法器门，并单独报告符号资源及预算回退。未运行整份 ALU，也未解除锁存、未定义输出等输入限制。

## 反馈对应的改进与保留限制

1. BDD 输入按真实总线位位置交错排序，PI 身份继续由归档绑定确认；契约和局部输出证明采用相同策略。32 位比较器原分组顺序耗尽预算，交错后旧区域全部输出证明通过。复杂函数仍可能整体回退，尚无按锥分区或 SAT 替代后端。
2. schema 4 增加 `fraig_cleanup_copy`：读取清理真实 `Value` 映射、literal 极性及删除集合，检查所有对象的去向、独立扇入函数和最终命令网络。验收覆盖实际删除以及损坏映射、极性、常量、删除列表或链路时的拒绝。**这不等于 `dc2/dch` 的全部历史已连接。**
3. 诊断分开报告来源转换缺失、阶段函数见证缺失、独立切点无法证明和预算耗尽。阶段见证记录命令和快照；它是当前匹配策略的限制，不能直接解释成电路错误或缺少钩子。整体 SAT 不等价只给出可能原因，不武断断言是共享消费者拆分。
4. 没有消费者的内部观察别名保留原驱动并验证原值；真正旧直连变新逻辑仍拒绝。错误绑定、位序、极性、遗漏输出、新依赖、组合环及共享消费者拆分反例继续验收。
5. 报告分开记录旧基准成本、查询/重放/契约、绑定与局部综合/拼接、完整新版验证及总成本。当前重放和证明成本可能大于原版全量综合，**没有加速结论**。

性能数据统一保留在 [v4 对比实验](../../incremental/comparison.md)，只将功能修改的组合案例纳入主要时间和规模比较。重新生成完整回归可运行 `python3 provenance/tools/run_regression.py --out runs/anubis-regression`；它保存逐用例输入、脚本、网表、日志和 `anubis/report.json`。输出目录需新建，历史运行产物不是测试依赖。

模块内自动切片的选择数量由 `incremental/tests/test_statements.py` 验收；当前功能修改的实际规模见 v4 对比报告。默认开启预检查后，`bypass_noc1` 的反转条件并交换分支、`compare_noc2` 的 `!=` 改 `~(==)` 都可证明功能不变，直接复用全部旧门；`compare_local9` 留下反例并进入原局部路径。没有先综合完整新版去判断是否等价。

等价预检查验收位于 `incremental/tests/test_equivalence.py`。同一固定输入运行开/关预检查，保存总耗时与分项成本，并检查共享定义缓存、混合改动、未知/超时回退、接线变化不跳过，以及展平记录可直接复用但仍保留原 ABC 候选状态。专项测试另使用 ANUBIS 接线、参数和共享定义包装变体，明确区分它们与上游宏修改。

新增 `incremental/tests/test_statements.py` 的小例子从 `alu.v` 的 AND/XOR/OR 表达式与操作数选择构造：明确缩为 4 位、重新连接为链或改写为 `if/case`，提供独立邻居；不是完整原始 ALU，也不冒充上游宏版本对。链式例只替换 4/12 个门，单过程条件编辑只替换 4/8 个门；端点缺失通过解析器故障注入验收扩区，原归档不修改。`bypass_id.v` 的原 `ANUBIS_LOCAL_3` 编辑实际涉及常量（`r31` 为 6 位、原比较字段为 5 位），作为明确拒绝项；另声明将字段扩成 6 位的活跃比较器适配，目前仍因 ABC 候选而阻塞。每项保留来源提交、完整 base/new、`adaptation.json`、证明与脚本，完整回归保存到 `statements/`。

下一步应分别处理展平后见证消失的来源关联、译码器连接义务、共享消费者拆分和更多模块内编辑形态。没有以放宽契约或跳过全输出证明来让基准变成“全通过”。

## 独立时序预检查

`python3 -m incremental anubis-sequential --proof-timeout 180 --out runs/anubis-sequential` 使用同一锁定版本中原始 `alpha/regfile.v`，保留完整 32×64 位数组与初始化。先做一次旧版默认综合并证明，再分别验证 `ANUBIS_NOC_5` 初始化循环反转、`ANUBIS_LOCAL_0` 写条件修改、`ANUBIS_LOCAL_2` 旁路修改。仅证明等价的版本获得整份旧网表复制；其余保留输出反例或未证明状态。输入、EQY 配置、分区、SBY 日志、证明耗时和文件哈希均归档。

这是一份时序模块的三项原始宏实验，不是全部时序 NOC 的覆盖统计；NOC_5 的改写在展开时已可消除。`test_sequential.py` 另使用明确记录的 2 位数据宽度适配和同步复位/故障控制，不能计作额外的上游原始案例。单时钟、初始状态、全输出覆盖和整网表复用的条件见 [时序入口说明](../../incremental/sequential.md)。不带 `--with-sequential` 的组合调查口径保持不变；全部 NOC 的时序上下文调查见上文。
