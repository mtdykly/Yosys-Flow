# Yosys-Flow

输入**仅包含组合逻辑的 RTL**，执行固定版本 Yosys `0e82bbefe` 的完整默认 `synth -top <top>`，随后按原始语句/表达式查询最终通用门级网表，并导出带边界端口的子网表。也支持最终对象到源码的反查。

流程保留默认 `techmap`、ABC 和其后的优化，不增加 `keep`、不跳过 ABC。来源覆盖仍为 `partial`：组合过程已有动作/分支证据，固定 ABC 已接入内部网络、实际 AIG 替换、`&fraig` / `&dch` 的成功等价/choice 关系及选中映射 cut。对于至多 10 个区域输入的可证明切点，支持同一输出内的局部语句区分；部分多级重结合可证明为共享来源的多门整体。现在还记录入口到 `&fraig` 的实际对象复制链，将部分跨表达式因式分解关联为共享门组，并保留输出门副本的来源。未证明对象继续返回候选。**一般的完整内部来源追踪尚未完成**。

## 快速运行

依赖 Python 3.10+、Make、C++20、CMake ≥ 3.28、Ninja、Bison、Flex、m4、zlib 开发文件、固定版本的独立原版 Yosys（含其 ABC）。详见[构建说明](docs/native-m1.md#构建)。

```bash
make NATIVE_JOBS=4
python3 -m yosys_flow run \
  --source examples/comb.v --top demo --out runs/comb-demo
python3 -m yosys_flow query runs/comb-demo \
  --source examples/comb.v --line 4
python3 -m yosys_flow extract runs/comb-demo \
  --source examples/comb.v --line 4 --out runs/comb-demo/selected.v
python3 -m yosys_flow validate runs/comb-demo
make test
```

`make` 构建固定插桩 ABC 到 `build/abc/install`，再构建原生插桩 Yosys 到 `build/native/install`。`--native-yosys` 指向该工具，`--yosys` 指向独立原版，二者不能是同一文件。每次 `run` 使用新目录，`extract` 也不会覆盖已有文件。`make demo` 使用 `examples/comb.v`，可通过 `DEMO_OUT` 指定新目录。

## 输入范围

支持单模块组合表达式、算术、比较、选择、直接连接/常量、完整赋值的组合 `always @*` / `always_comb`、`if/else` 和 `case`。`reg` / `logic` 声明本身不代表时序，组合过程中的非阻塞赋值也不会仅因使用 `<=` 被拒绝。

前端拒绝敏感沿、`initial` 和初始化属性；推导出锁存器或其他状态单元时拒绝发布结果。`check -assert` 检查组合环路及无效连接。层次实例、memory、generate、循环、函数/任务、手工实例化仍不受支持；更复杂语法即使可以解析，也不自动计入已验收范围。

时序专用的追踪、逐位 FF 来源、初始化和时序导出代码已移除；旧状态运行记录不再支持。原版 `synth` 自身的 pass 列表保持不变，例如 `proc_dff` / `proc_dlatch` 仍可能处理组合过程中的直连路径；这不代表支持状态电路。

## 查询与导出

`query` / `extract` 支持 `--line`、`--end-line`、`--column`、`--end-column` 或 `--object`。`--instance` 当前仅接受顶层模块名。查询使用本次运行的原始源码坐标，源码修改后通过 `changed_or_missing_base_inputs` 提示，行号不会自动迁移。

默认选择的是**按转换记录或已验证局部切点关联的对象**，不会沿连接自动加入完整扇入锥。共享门保留全部已记录来源和外部使用关系。直接连接和常量也可生成零 cell 的有效子模块；空结果不能直接解释为语句没有影响。

导出生成 `.v`、`.il`、`.json`、`.provenance.json` 及验证脚本/日志。来源说明包含最终网表身份、所选 cell/连接、边界端口、共享使用者、过程证据、techmap 输出位对应、ABC 关联及精度/覆盖缺口。文件来自同一次运行的最终网表。

`coverage=partial`、`end_to_end_exact=false`、`safe_for_incremental_reuse=false` 继续保留。`abc_internal_cut` 表示内部证据支持的局部功能切点证明，不表示完整因果历史；`abc_internal_joint_cut` 表示多门整体对应，组内不能逐门独占归给某条语句；`abc_output_cone_candidate` 和 `abc_region_candidate` 均为候选；导出格式等价不证明语句选择准确，也不证明可安全替换逻辑。

`examples/multilevel.v` 演示多级重结合：第 3 行单独选择 AND，第 4/5 行共同选择两个 XNOR，第 6 行单独选择 OR。导出第 4 行时不会加入上游 AND 或独立 OR，说明中会保留第 4/5 行共享来源。具体命令与验收见 [ABC 精度里程碑](docs/abc-internals.md#多级重结合的精度里程碑)。

`examples/factoring.v` 演示等价表达式共享：第 2 行 `(a & b) | (a & c)` 和第 3 行 `a & (b | c)` 经默认 ABC 合并。查询任意一行均返回最终共享的 AND、OR，排除第 4 行的独立 XOR；不会遗漏只有另一种写法中显式出现的因子。证明包含真实复制链、已采用的 fraig 合并及最终门组的独立边界验证，见 [等价表达式门组](docs/abc-internals.md#等价表达式与因式分解门组)。

## 开发与验收

默认入口为 `synth`。`--flow m1` 执行 `proc -noopt; opt_merge`，`--flow m1-opt` 执行 `proc; opt_expr; opt_clean; opt_merge; opt_clean`。两者共用原生记录器，仅用于隔离过程和优化问题，不能替代最终 synth 验收。旧 M0 插件及 JSONL v1 已退役。

`make test` 在 `runs/regression-<id>/` 保存 `report.json` 和 `tests.log`，记录工具、提交和执行结果；任何跳过都会使完整回归失败。需要指定新的报告目录时可运行 `python3 tools/run_regression.py --out runs/my-report`。

验收分别检查转换记录、来源选择和子网表导出。运行器比较原版与插桩版的完整 JSON modules，并做整体等价检查；导出另做边界与 Verilog 重读等价检查。`tests/test_combinational.py` 检查输入范围，其他测试保留组合过程、来源选择、共享、参数、常量、边界和损坏记录的回归。

- [默认 synth 的实现与限制](docs/native-synth.md)
- [当前计划与后续优先级](docs/default-synth-plan.md)
- [ABC 内部钩子与切点证明](docs/abc-internals.md)
- [组合过程来源](docs/process-provenance.md)
- [来源选择验收](docs/selection-acceptance.md)
- [JSONL 格式](docs/schema.md)
