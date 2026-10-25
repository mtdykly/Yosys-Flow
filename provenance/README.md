# 来源记录、索引与区域导出

本目录集中保存来源追踪的 Python 实现、原生钩子、构建工具、测试和文档。以下命令均在仓库根目录执行。局部综合与替换实验位于独立的 [incremental/](../incremental/README.md)。

输入**仅包含组合逻辑的 RTL**，执行固定版本 Yosys `0e82bbefe` 的完整默认 `synth -top <top>`，随后按原始语句/表达式查询最终通用门级网表，并导出带边界端口的子网表。也支持最终对象到源码的反查。

流程保留默认 `techmap`、ABC 和其后的优化，不增加 `keep`、不跳过 ABC。来源覆盖仍为 `partial`：组合过程已有动作/分支证据，固定 ABC 已接入内部网络、实际 AIG 替换、`&fraig` / `&dch` 的成功等价/choice 关系及选中映射 cut。可证明切点支持同一输出内的局部语句区分；超过 10 输入的区域及门组边界使用有资源预算的符号布尔证明，不再仅因区域输入数而放弃小目标。部分多级重结合可证明为共享来源的多门整体。现在还记录入口到 `&fraig` 的实际对象复制链，将部分跨表达式因式分解关联为共享门组，并保留输出门副本的来源。未证明对象继续返回候选。**一般的完整内部来源追踪尚未完成**。

## 快速运行

依赖 Python 3.10+、Make、C++20、CMake ≥ 3.28、Ninja、Bison、Flex、m4、zlib 开发文件、固定版本的独立原版 Yosys（含其 ABC）。详见[构建说明](docs/native-m1.md#构建)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source provenance/examples/comb.v --top demo --out runs/comb-demo
python3 -m yosys_flow query runs/comb-demo \
  --source provenance/examples/comb.v --line 4
python3 -m yosys_flow diagnose runs/comb-demo \
  --source provenance/examples/comb.v --line 4
python3 -m yosys_flow extract runs/comb-demo \
  --source provenance/examples/comb.v --line 4 --out runs/comb-demo/selected.v
python3 -m yosys_flow validate runs/comb-demo
make test
```

`make` 构建固定插桩 ABC 到 `build/abc/install`，再构建原生插桩 Yosys 到 `build/native/install`。`--native-yosys` 指向该工具，`--yosys` 指向独立原版，二者不能是同一文件。每次 `run` 使用新目录，`extract` 也不会覆盖已有文件。`make demo` 使用 `provenance/examples/comb.v`，可通过 `DEMO_OUT` 指定新目录。

## 首次建档的可复用索引

新建归档在来源、网表和独立对照检查通过后保存 `verified-index.json.gz`，供查询和自动增量复用旧来源状态及连接索引。它绑定原始产物与实现版本，并由本机密钥认证；每次仍核对原始产物哈希。旧归档缺索引、实现变化或异机无认证密钥时回退完整重放，不自动修改归档；内容损坏拒绝。原始 JSONL、ABC 证据和最终网表继续保留，索引只是验证加速副本，不能消除候选或边界问题。`yosys_flow validate` 强制完整重放，`contract-validate` 则可用已认证索引重建区域声明。

自动增量按需生成轻量 `replacement-plan.json`，引用旧证据并重新确定本次边界。完整 v1 契约、合并和导出继续作为独立接口，详见 [区域契约与替换计划](docs/region-contracts.md)；首次与在线分工见 [自动流程](../incremental/automatic.md#首次建档与在线替换计划)。

## 输入范围

支持单模块及普通组合子模块的多层实例、数值参数派生、组合表达式、算术、比较、选择、直接连接/常量、完整赋值的组合 `always @*` / `always_comb`、`if/else` 和 `case`。`reg` / `logic` 声明本身不代表时序，组合过程中的非阻塞赋值也不会仅因使用 `<=` 被拒绝。

前端拒绝敏感沿、`initial` 和初始化属性；推导出锁存器或其他状态单元时拒绝发布结果。`check -assert` 检查组合环路及无效连接，层次设计还检查展开后的整份电路。memory、generate、循环、函数/任务、实例数组、门原语、黑盒及层次 inout 仍不受支持；更复杂语法即使可以解析，也不自动计入已验收范围。

时序专用的追踪、逐位 FF 来源、初始化和时序导出代码已移除；旧状态运行记录不再支持。原版 `synth` 自身的 pass 列表保持不变，例如 `proc_dff` / `proc_dlatch` 仍可能处理组合过程中的直连路径；这不代表支持状态电路。

## 查询与导出

`query` / `extract` 支持 `--line`、`--end-line`、`--column`、`--end-column` 或 `--object`。`--instance demo/u0/inner` 指定原始实例路径；省略时返回该源码位置的所有实例。查询使用本次运行的原始源码坐标，源码修改后通过 `changed_or_missing_base_inputs` 提示，行号不会自动迁移。

**默认 `synth -top` 保留层次，`run --flatten` 才显式执行 `synth -top ... -flatten`。** 两种模式均支持实例查询、契约和导出，并尊重用户的 `keep_hierarchy`。保留层次时，离线查询采用经过整体验证的只读实例视图；原始 `after.json` 不被展平。导出的区域自身是独立的门级子模块。详见[层次模式与实例身份](docs/hierarchy.md)。

```bash
python3 -m yosys_flow run --source provenance/examples/hierarchy.v --top demo --out runs/hierarchy-demo
python3 -m yosys_flow query runs/hierarchy-demo --source provenance/examples/hierarchy.v --line 4 --instance demo/u0/inner
python3 -m yosys_flow contract runs/hierarchy-demo --source provenance/examples/hierarchy.v --line 4 --instance demo/u0/inner --out runs/hierarchy-demo/u0.contract.json
python3 -m yosys_flow extract runs/hierarchy-demo --contract runs/hierarchy-demo/u0.contract.json --out runs/hierarchy-demo/u0.v
# 独立的展平实验，必须使用新的运行目录
python3 -m yosys_flow run --source provenance/examples/hierarchy.v --top demo --flatten --out runs/hierarchy-flat-demo
```

默认选择的是**按转换记录或已验证局部切点关联的对象**，不会沿连接自动加入完整扇入锥。共享门保留全部已记录来源和外部使用关系。直接连接和常量也可生成零 cell 的有效子模块；空结果不能直接解释为语句没有影响。

导出生成 `.v`、`.il`、`.json`、`.provenance.json` 及验证脚本/日志。来源说明包含最终网表身份、所选 cell/连接、边界端口、共享使用者、过程证据、techmap 输出位对应、ABC 关联及精度/覆盖缺口。文件来自同一次运行的最终网表。

`diagnose` 从同一次运行的记录生成旧区域诊断，不启动 Yosys/ABC：列出边界及全部所选门的外部使用者、其他已记录来源、候选依据，并检查是否包含已记录门组的全部最终成员。若只选中门组的一部分，会列出需要补入的对象，保持原选择不变。电气边界完整、记录门组闭合和修改后的安全替换是不同结论，诊断继续报告未证明的条件。详见[区域诊断与基线](docs/abc-diagnostics.md)。

`contract` 是独立的完整来源审查接口，将查询结果交付为绑定原始版本的 v1 区域契约，显式补齐已记录门组，保存所有边界输出的函数、使用者、共享来源和证明缺口。`contract-validate` 对照记录重新验证；`contract-merge` 合并同一次运行的契约，重新计算边界并验证每一个输出，支持重叠和嵌套合并。三者不启动综合；`extract --contract <文件>` 可导出合并后的完整区域。契约的输出函数验证以重放的最终对象和最终网表为依据，不能替代 RTL 来源完整性证明。命令与验收见[区域契约](docs/region-contracts.md)。

`coverage=partial`、`end_to_end_exact=false`、`safe_for_incremental_reuse=false` 继续保留。`abc_internal_cut` 表示内部证据支持的局部功能切点证明，不表示完整因果历史；`abc_internal_joint_cut` 表示多门整体对应，组内不能逐门独占归给某条语句；`abc_output_cone_candidate` 和 `abc_region_candidate` 均为候选；导出格式等价不证明语句选择准确，也不证明可安全替换逻辑。

`provenance/examples/multilevel.v` 演示多级重结合：第 3 行单独选择 AND，第 4/5 行共同选择两个 XNOR，第 6 行单独选择 OR。导出第 4 行时不会加入上游 AND 或独立 OR，说明中会保留第 4/5 行共享来源。具体命令与验收见 [ABC 精度里程碑](docs/abc-internals.md#多级重结合的精度里程碑)。

`provenance/examples/factoring.v` 演示等价表达式共享：第 2 行 `(a & b) | (a & c)` 和第 3 行 `a & (b | c)` 经默认 ABC 合并。查询任意一行均返回最终共享的 AND、OR，排除第 4 行的独立 XOR；不会遗漏只有另一种写法中显式出现的因子。证明包含真实复制链、已采用的 fraig 合并及最终门组的独立边界验证，见 [等价表达式门组](docs/abc-internals.md#等价表达式与因式分解门组)。

## 开发与验收

默认入口为 `synth`。`--flow m1` 执行 `proc -noopt; opt_merge`，`--flow m1-opt` 执行 `proc; opt_expr; opt_clean; opt_merge; opt_clean`。两者共用原生记录器，仅用于隔离过程和优化问题，不能替代最终 synth 验收。旧 M0 插件及 JSONL v1 已退役。

`make test` 在 `runs/regression-<id>/` 保存 `report.json` 和 `tests.log`，记录工具、提交和执行结果；任何跳过都会使完整回归失败。需要指定新的报告目录时可运行 `python3 provenance/tools/run_regression.py --out runs/my-report`。

验收分别检查转换记录、来源选择和子网表导出。运行器比较原版与插桩版的完整 JSON modules，并做整体等价检查；导出另做边界与 Verilog 重读等价检查。`provenance/tests/test_combinational.py` 检查输入范围，其他测试保留组合过程、来源选择、共享、参数、常量、边界和损坏记录的回归。

近期按具体案例完善 **ABC 区域对应、边界和候选解释**，允许共享门组；完整内部历史及逐门独占归属不作为层次扩展的门槛。当前重点是一次插桩全量综合后，靠记录定位并说明区域约束；已有普通组合层次、ROBDD 符号功能校验及独立边界证明，尚未实现 SAT 后端或完整局部验证分解。新增 ABC 钩子由具体证明缺口驱动，独立增量实验见仓库根目录的 `incremental/`。详见[阶段计划](docs/default-synth-plan.md)。

`python3 provenance/tools/benchmark_abc.py --out runs/abc-baseline --repeats 3` 运行 7 组小型案例和 8/10/12/16/32 输入共享链。每个样本只执行一次插桩全量 synth，多个查询复用其记录；独立原版对照另行执行。报告保存漏选/多选、候选原因、边界/反查/导出验收、日志与快照数量、分阶段计时及可用的内存测量。规模案例现在也要求精确符合独立预期：12/16/32 输入区域中的单门目标均只选中该门。重复运行用于成本采样，不是每次定位都要综合。复杂区域耗尽符号证明预算时仍回退，详见[限制与预算](docs/abc-internals.md#符号功能校验与独立切点预算)。

- [默认 synth 的实现与限制](docs/native-synth.md)
- [层次、展平与实例查询](docs/hierarchy.md)
- [当前计划与后续优先级](docs/default-synth-plan.md)
- [ABC 内部钩子与切点证明](docs/abc-internals.md)
- [区域诊断与规模基线](docs/abc-diagnostics.md)
- [区域契约、全部输出验证与合并](docs/region-contracts.md)
- [组合过程来源](docs/process-provenance.md)
- [来源选择验收](docs/selection-acceptance.md)
- [JSONL 格式](docs/schema.md)
