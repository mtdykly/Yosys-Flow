# ANUBIS 组合修改基准

新增的外部 RTL 和修改均来自 [masc-ucsc/anubis](https://github.com/masc-ucsc/anubis/tree/93088bd3c05407ccd871e8d5067d024f812aeeaa)，固定提交 `93088bd3c05407ccd871e8d5067d024f812aeeaa`。`upstream/` 保留原始文件和许可/免责声明；`lock.json` 逐文件固定 SHA-256，每次运行先校验。正常运行不需要联网。上游通过宏启用修改，这些是现成基准变体，**不声称每项都对应两个历史提交**。

本命令在仓库检出中运行，使用随仓库保存的语料；不是完整 ANUBIS 设计集或官方评分流程。没有接入 YSYX、EPFL 或其他外部测试库。原有内部回归测试不删除。

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

上面的 patch 构造步骤描述显式模式。`--automatic` 改为调用 [auto](../../incremental/automatic.md)，不把 `fixture.json` 中的修改行号、目标实例或局部规范交给算法。它从旧 `frontend.il` 与完整新版比较得到目标，自动提取局部表示、绑定和契约，产物保存到每种模式的 `automatic/`；报告通过 `automatic_report` 链接详细记录。已实测自动模式同样为 4 项成功、2 项锁存拒绝、6 项证据阻塞，未将扩大选区当作绕过来源要求的方式。

## 适配与当前结果

| 用例 | 上游修改/压力来源 | 保留层次 | 显式展平 |
| --- | --- | --- | --- |
| `bypass_noc1` | `bypass_ex.v` / `ANUBIS_NOC_1`：反转条件并交换分支 | 替换成功，新旧全部输出等价 | `dc2` 阶段函数见证缺失，阻塞 |
| `compare_noc2` | `quick_compare.v` / `ANUBIS_NOC_2`：`!=` 改为 `~(==)` | 替换成功，新旧全部输出等价 | `dc2` 阶段函数见证缺失，阻塞 |
| `compare_local9` | `quick_compare.v` / `ANUBIS_LOCAL_9`：符号位条件 0→1 | 替换成功，增量结果等价于完整新版；另有新旧不同的 SAT 见证 | `dc2` 阶段函数见证缺失，阻塞 |
| `control_local2` | `alu_control.v` / `ANUBIS_LOCAL_2`：无符号选择改成有符号 | 来源候选、连接消费者未完全绑定，阻塞 | 同类证据不足，阻塞 |
| `writeback_local10` | `wb_control.v` / `ANUBIS_LOCAL_10`：删除默认赋值 | 正确拒绝新增锁存行为 | 正确拒绝新增锁存行为 |
| `bypass_with_adder` | `NOC_1` 旁放 `alu.v` 的原始 32 位 ADD 分支 | 替换成功，独立加法器及另一实例保留 | 无 BDD 预算回退；仍因阶段见证缺失阻塞 |

两份实例使用独立顶层输入，目标输出和另一实例输出都参与额外的异或汇总。全输出 miter 因此覆盖未修改实例及汇总；保留层次模式的最终替换交付仍为展平网表。

`quick_compare` 的 `default: Result = dc` 与 `alu_control` 的 `default: ALUsel = dc8` 在新旧两版中统一改为 0。保留 `casex` 模式中的 `dc6` 和原始宏定义。宏分支选择把未启用行留为空行，不移动原始行号。模块重命名和包装是显式适配，不能把结果称为未经修改的原始基准。

压力例只提取 `alu.v` 的 `ALUout = RSbus + Y`，保留原行号、32 位宽度和原文件的免责声明；包装提供独立操作数。未运行整份 ALU，也未因此解除锁存、未定义输出等输入限制。在一次实测中，64 输入加法区域用了 21,344 个 BDD 节点；与两个旁路实例一起展平后，108 输入区域用了 23,838 个节点，均低于 100,000 预算。查询旁路修改未选入加法器门。数字是这组固定用例的结果，不是任意算术电路的规模保证。

## 反馈对应的改进与保留限制

1. BDD 输入按真实总线位位置交错排序，PI 身份继续由归档绑定确认；契约和局部输出证明采用相同策略。32 位比较器原分组顺序耗尽预算，交错后旧区域全部输出证明通过。复杂函数仍可能整体回退，尚无按锥分区或 SAT 替代后端。
2. schema 4 增加 `fraig_cleanup_copy`：读取清理真实 `Value` 映射、literal 极性及删除集合，检查所有对象的去向、独立扇入函数和最终命令网络。比较器记录了 6 个删除节点，译码器记录了 18 个；损坏映射、极性、常量、删除列表或链路均拒绝。**这不等于 `dc2/dch` 的全部历史已连接。**
3. 诊断分开报告来源转换缺失、阶段函数见证缺失、独立切点无法证明和预算耗尽。阶段见证记录命令和快照；它是当前匹配策略的限制，不能直接解释成电路错误或缺少钩子。整体 SAT 不等价只给出可能原因，不武断断言是共享消费者拆分。
4. 没有消费者的内部观察别名保留原驱动并验证原值；真正旧直连变新逻辑仍拒绝。错误绑定、位序、极性、遗漏输出、新依赖、组合环及共享消费者拆分反例继续验收。
5. 报告分开记录旧基准成本、查询/重放/契约、绑定与局部综合/拼接、完整新版验证及总成本。当前重放和证明成本可能大于原版全量综合，**没有加速结论**。

一次本地完整回归中的保留层次结果如下。候选生成耗时包含查询、重放、契约、绑定和拼接，尚不包含完整新版等价验证；参考全量耗时为独立原版 Yosys 综合。时间依赖机器和缓存，只用于暴露当前成本。

| 用例 | 旧门数 / 替换 / 复用 | 候选生成 | 参考全量综合 | 旧 JSONL 记录大小 |
| --- | --- | --- | --- | --- |
| `bypass_noc1` | 63 / 31 / 32 | 约 0.99 s | 约 0.25 s | 约 0.70 MB |
| `compare_local9` | 231 / 115 / 116 | 约 9.39 s | 约 0.40 s | 约 8.06 MB |
| `bypass_with_adder` | 225 / 31 / 194 | 约 8.27 s | 约 0.34 s | 约 11.35 MB |

这些结果证明了有限范围内的复用正确性，同时说明当前实现还没有时间优势。完整回归命令 `python3 provenance/tools/run_regression.py --out runs/anubis-regression` 会把逐用例输入、脚本、网表、日志和 `anubis/report.json` 一起留在报告目录，便于复核。

模块实例级局部规范与绑定已能自动推导；专项测试另外使用 ANUBIS 的接线、参数和共享定义包装变体，明确区分它们与上游宏修改。下一步应针对这些真实失败分别处理：展平后见证消失的来源关联、译码器连接义务，以及模块内部更细的区域提取。没有以放宽契约或跳过全输出证明来让基准变成“全通过”。
