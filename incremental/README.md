# 自动推导与验证局部替换

本目录验证已有来源记录能否用于一次真实的局部修改：旧 RTL 只做一次插桩全量综合，之后依靠旧记录选择区域；新版局部 RTL 独立综合后接回旧网表。新版完整综合仅作为验证参考，不用于寻找或复制新门。

新增外部实验只使用 [ANUBIS](../benchmarks/anubis/README.md)。运行 `python3 -m incremental anubis --out runs/anubis-demo` 可保留原版、宏修改后的完整版、局部 patch、绑定、源码查询、契约和逐用例报告。原有四通道及错误绑定等回归继续保留。

原始 NOC 验证使用 `python3 -m incremental anubis-nochange --upstream-top --repeats 1 --frontend-timeout 300 --out runs/anubis-noc-upstream-final`。按上游文件清单与真实顶层展开；未使用的模块/分支不触发功能变化判定，局部反例不提升为顶层反例。本轮 30 个宏为 26 个结构一致、4 个未知、0 个明确顶层反例。输出表示、证明尝试与证据哈希；不交付复用网表，不替代旧基准验证。`--inventory-only` 只盘点，`--cases` 可选择宏。

无 `--upstream-top` 的原入口保留为独立模块诊断，单次保护上限默认 600 秒、无共享预算；不代表原始 CPU/FPU 的全部行为。说明见 [ANUBIS 评测](../benchmarks/anubis/README.md)。

后续先明确行为等价与输出定义性的不同义务，再补 DLX 正负沿建模、FPU/MOR1KX 未驱动信号分析，以及 OR1200 多时钟和异步复位证明。每项的小型对照、顶层验收、预算测量与复用条件见 [后续补齐计划](sequential.md#后续补齐计划)。这些能力尚未实现，四项未知不计为等价通过。

## 自动入口

另有独立的时序前置检查：`seq-baseline` 归档并证明旧网表，`seq-reuse` 接收旧归档和完整新版 RTL，用 EQY/SBY 完成全输出无界证明后逐字节复用。单时钟、明确初始化或同步启动复位；失败/超时不交付、不进入组合增量。命令、依赖、状态语义及 ANUBIS 时序验收见 [sequential.md](sequential.md)。

历史 `--with-sequential` 调查覆盖 30 个宏、35 个自选模块/参数配置，组合 SAT 和时序 EQY 分别测量。它与 `--upstream-top` 互斥；该模式的成功率、反例和耗时不能视为上游完整设计的结果。两者都不扩大组合增量范围或更改生产预算。

```bash
python3 -m incremental auto \
  --base-run runs/old-run \
  --new-source path/to/new.v \
  --out runs/automatic-edit

# 从 ANUBIS 自动准备完整新旧版本，验证相同的两输入接口
python3 -m incremental anubis --automatic \
  --cases compare_local9 --mode retained --out runs/automatic-anubis
```

用户不需要提供修改位置、目标实例、契约、局部 RTL 或绑定。工具从旧归档 `frontend.il` 与完整新版展开结果识别变化实例，再优先比较其内部赋值目标的表达式和过程动作，生成契约、局部代码和连接。来源缺口、接口歧义及验证失败均拒绝发布。旧源码可已被编辑或删除，旧记录的所有产物必须完整且哈希一致。

当前默认先对接口和实例上下文未变的变化叶模块做 Yosys `miter + sat` 全输出等价预检查。全部修改证明等价且上下文满足二值前提时，直接复制旧网表，状态为 `reused_equivalent`；结构完全相同时为 `unchanged`。这些路径保留旧层次，无 patch、局部综合、拼接或新版全量综合。默认每项墙钟预算 5 秒、共享总预算 15 秒，超时/未知继续原流程；`--no-equivalence-precheck` 可关闭，也适用于希望等价改写仍重新优化 PPA 的情况。

剩余修改先尝试单模块或叶模块内部的赋值与依赖切片；从实际 `proc` 后表示复制必要单元，保留条件与赋值优先级。消失的边界沿旧网表消费者扩区，模块及父实例保留为后备方案。完整新版的前端展开和 `proc` 检查仍是在线成本；发生局部替换的路径才用完整新版 `synth` 作最终参考。不是最小区域算法，复杂共享或证据不足仍可能回退、没有复用收益或拒绝。详细算法、产物和限制见 [automatic.md](automatic.md)。

## 四通道例子

[base.v](examples/four_lanes/base.v) 包含 `chip → lane → mix` 三层模块，四条独立 8 位通道共用 `lane`/`mix` 定义。每条通道先计算 `pre = data ^ key`，核心输出为 `(pre & mask) ^ salt`，另外保留 `side = pre & salt`；顶层还有汇总四条核心输出的 `summary`。

[new.v](examples/four_lanes/new.v) 仅将实例 `chip/u1/core` 的 AND 改成 OR。为只修改一个实例，新版显式提供 `lane_edited` 和 `mix_edited`。其余实例、`side` 和汇总逻辑保持原 RTL 描述。[patch.v](examples/four_lanes/patch.v) 是显式给定的新版局部功能，而非自动从 `new.v` 提取。

```bash
# 在仓库根目录执行；结果目录必须尚不存在
python3 -m incremental demo --out runs/incremental-demo

# 也可单独验证一种模式
python3 -m incremental demo --mode retained --out runs/incremental-retained
python3 -m incremental demo --mode flatten --out runs/incremental-flat
```

两种模式的旧电路均为 152 个门。契约并集选中 `mix` 第 3、4 行在 `chip/u1/core` 中的 16 个门，边界有 24 个输入、8 个输出。每个输出还服务于区域外的汇总门，契约必须保留这部分义务。

1. `base-run/` 保存原有插桩综合及独立原版验收产物。
2. `line-3.contract.json`、`line-4.contract.json` 和 `region.contract.json` 由旧记录生成，不执行新综合。
3. `boundary-proof.json` 验证显式边界功能：`a = data[15:8] ^ key[15:8]`、`b = mask[15:8]`、`c = salt[15:8]`。用旧网表的实际边界位计算函数并核对极性，原线名消失时也不猜测绑定。
4. `replacement/work/local-synth.ys` 只读取 `patch.v`，执行默认 `synth -top patch -flatten`。
5. 替换器仅删除所选旧门，复制新局部门。输出用显式缓冲门连接旧信号；有反相输入时添加极性适配门。所有未选中门的类型、参数、属性及连接必须逐项不变。
6. 独立边界函数检查覆盖每个新输出；单驱动、无环及全部外部消费者检查必须通过。
7. 此后才综合完整 `new.v`，构建全输出 SAT miter。检查所有 72 个输出位，包括未修改通道、共享上游逻辑的 `side` 和区域外的 `summary`；没有按内部同名信号假设等价。
8. Verilog 重读等价和基准归档复核通过后，才发布 `replacement/incremental.json`、`incremental.v` 和成功的 `report.json`。

两种模式当前均复用 136 个门，局部综合产生 16 个门，加 8 个边界输出缓冲门后共 160 个门。此实验不在拼接后运行全局优化，因此不声称门数最优或已有性能收益。报告分别记录局部综合、参考全量综合和整体验证耗时。

保留层次模式以已独立验证的电气实例视图进行实例级替换，**交付网表为展平的通用门网表**。原始基准层次网表不变；尚未实现通过克隆模块定义来保持拼接结果的层次外形。显式展平模式直接采用其最终电气网表。两种路径均不会将一个模块定义的所有实例一起修改。

## 独立运行替换器

```bash
python3 -m incremental run \
  --base-run runs/incremental-demo/retained/base-run \
  --contract runs/incremental-demo/retained/region.contract.json \
  --bindings runs/incremental-demo/retained/bindings.json \
  --patch-rtl incremental/examples/four_lanes/patch.v --patch-top patch \
  --new-source incremental/examples/four_lanes/new.v \
  --out runs/another-replacement
```

`bindings.json` 绑定该契约的内容 ID，按 Yosys JSON 的从低位到高位顺序列出每个局部端口对应的契约端口。输入项为 `{"port":"in_...","inverted":false}`，也接受无反相的字符串简写；输出项为 `"out_..."`。所有输入和输出恰好绑定一次。此 `run` 接口要求调用者提供局部功能与绑定；`auto` 另用新前端表示、旧实例端口和信号关系推导它们，两者均不从完整新版综合网表搜索或复制新门。

显式 `run` 始终重新验证契约，要求 `verified_old_region`、基准输入未变化、替换输出由所选门驱动，以及新的局部端口完全位于契约边界内。局部输入增加而契约未扩展会拒绝。`auto` 用已归档且校验过的前端表示替代读取旧源码；需要替换时增加有真实外部驱动的契约输入，仍经过相同拼接与全输出验证。整份旧网表复用则验证全部修改的功能证明和归档身份，不生成替换契约。失败日志和报告保留在新的实验目录，但不发布 `incremental.v/json`。原运行和已有结果不覆盖。

选区可能包含没有外部消费者的源码连接别名。这类观察输出若对应旧输入或常量，保留旧信号而不添加驱动；仍逐项证明 patch 的观察输出与旧值相同。真正有外部消费者的直连/常量输出不适用此规则。**旧直连变成新门**仍需要消费者重绑定实现，目前明确拒绝。局部综合的 `$scopeinfo` 仅在无电气引脚时作为层次元数据移出拼接输入，不改变原始 `local.json`。

报告的 `replacement_ready_seconds` 包括加载重放、契约核对、局部综合、绑定、拼接和候选检查；不包括调用者查询/生成契约。ANUBIS 的 `incremental_candidate_ready_seconds` 另加入这些调用者成本；`online_checked_total_seconds` 还包含完整新版参考综合与全部验证。不能用 `local_synth_seconds` 与全量综合直接比较后宣称加速。

## 已验证与未完成

测试同时包含成功路径和以下反例：错误位序、错误输入极性、遗漏输出、新增外部依赖、非凸区域产生组合环，以及共享旧门的两个消费者中只允许一个改变功能。后者需要拆分消费者或扩大/重建区域，目前由全部输出验证拒绝，未实现自动修复。

局部拼接函数验证只证明新门正确实现了**给定的局部规范与绑定**。错误绑定仍可能通过这层检查，所以必须再检查与完整新 RTL 的全部输出等价。具体版本对的验证成功，不改变旧契约中的 `safe_for_incremental_reuse=false`，也不把 ABC 局部功能证明升级为完整因果历史。

四通道例子没有遇到需要新增 ABC 内部钩子的证据断点；补充的是优化后边界功能/极性绑定和拼接验收。自动流程已从模块实例细化到变化赋值及必要依赖；ANUBIS 4 位表达式适配例验证 12 个旧门只替换 4 个，同一过程的条件修改保留独立目标。尚未完成任意语法的最小区域推导、消费者拆分、层次形态保持、PPA 优化、时序逻辑和替换后的连续来源归档。新门映射保存在实验报告，旧记录不会冒充新设计的完整来源日志；ANUBIS 展平、部分比较器和嵌套条件的来源缺口仍报告阻塞。
