# Yosys-Flow

在原生 Yosys 综合过程中记录 RTL 来源与对象演化，支持“源码位置 → 最终网表对象 → 带边界端口的子网表”，也支持对象到源码的反查。

**默认主流程是固定版本 Yosys 的 `synth -top <top>`**，包含 `techmap`、ABC 和其后优化，终点为与工艺库无关的细粒度通用门及触发器网表。来源覆盖仍为 `partial`：过程和已捕获的异步复位路径已有动作/分支证据，ABC 已按保留输出边界细化候选，尚未完成逐语句精确定位。支持范围和验收目标见 [默认 synth 说明](docs/native-synth.md) 与 [开发计划](docs/default-synth-plan.md)。

## 快速运行

依赖 Python 3.10+、GNU Make、C++20 编译器、CMake ≥ 3.28、Ninja、Bison、Flex、m4、zlib 开发文件及外部 `yosys-abc`。还需提交 `0e82bbefe` 的**独立原版 Yosys**，用于网表对照和等价验证。完整依赖和本地安装方法见[构建说明](docs/native-m1.md#构建)。

```bash
make NATIVE_JOBS=4

# 省略 --flow 即执行默认 synth
python3 -m yosys_flow run \
  --source examples/proc.v --top demo --out runs/my-synth-run
python3 -m yosys_flow query runs/my-synth-run \
  --source examples/proc.v --line 13
python3 -m yosys_flow extract runs/my-synth-run \
  --source examples/proc.v --line 13 --out runs/my-synth-run/selected.v
python3 -m yosys_flow validate runs/my-synth-run
make test
```

`make` 构建插桩原生版到 `build/native/install`；`make native` 是同一构建入口。`make demo` 构建后综合 `examples/proc.v`，查询第 13 行并导出寄存器子网表到 `runs/synth-demo/selected.v`。每次运行必须使用新的目录；重复演示可指定 `make demo DEMO_OUT=runs/another-demo`。已有结果不会被覆盖。

`--native-yosys` 指定插桩版，`--yosys` 指定独立原版。两者用途不同，不能指向同一个可执行文件。原版依然用于检查原生记录是否改变了综合结果。

## 查询、精度与导出

`query` 和 `extract` 使用运行时的源码坐标，支持 `--line` / `--end-line` 和从 1 开始的 `--column` / `--end-column`。列查询优先选择指定范围内最小的运算表达式；仅按行查询会包含该范围中记录的语句及表达式。源码后来修改时，结果会列出 `changed_or_missing_base_inputs`，行号仍指向本次运行的原始版本。

返回的 `ref` 只在当前运行内有效，可以反查或按对象导出：

```bash
# 使用实际 query 结果中的 ref 替换示例编号
python3 -m yosys_flow query runs/my-synth-run --object 'native:cell_17@1'
python3 -m yosys_flow extract runs/my-synth-run \
  --object 'native:cell_17@1' --module-name my_slice --out runs/exports/my_slice.v
```

默认选择沿**已记录的来源关系**传播，不自动把上游扇入锥全部加入。共享门保留全部已记录来源及使用者。边界由同一次运行的 `after.json` 计算：外部驱动成为输入，外部使用成为输出；选中集合内部的驱动和连接保持内部连接。边界总线逐位导出，并保留原声明下标、位序及参数。

ABC 记录实际输入/输出节点对应，并按保留输出边界细化候选。例如 `assign y=a&b; assign z=c^d;` 即使进入同一次调用，也能分别选择 AND 与 XOR。结果仍为 `abc_output_cone_candidate`；同一输出锥内部尚未精确区分全部来源，未知映射回退 `abc_region_candidate`。**候选细化不代表 ABC 内部重写追踪已经完成**。`coverage`、对象 `precision` 和 `end_to_end_exact=false` 会同时保留在查询及导出报告中。

| resolution | 含义 |
| --- | --- |
| `cells_found` | 找到来源关联的最终 cell |
| `connections_found` | 找到有记录的连接或常量，可独立导出零 cell 模块 |
| `source_not_mapped` | 该位置没有已记录的来源起点 |
| `no_final_object_with_recorded_binding` | 有起点但没有最终来源绑定，需检查覆盖缺口及退休证据 |

`retired_cells` / `retired_connections` 表示历史对象退休，可能是替换或删除；空结果不等于原语句没有影响。所有结果保留 `safe_for_incremental_reuse=false`，不提供安全替换证明。

对于 `examples/proc.v`，第 13 行查询寄存器更新，第 5 行查询组合逻辑候选，第 15、16 行分别查询直接连接与常量。选中 FF 时保留时钟、复位、使能和相关 Q 位初始化；未选中 FF 的 Q 可以作为边界输入，标记外部状态驱动。

一次导出生成：

| 文件 | 内容 |
| --- | --- |
| `selected.v` | 可独立读取的 Verilog 子模块 |
| `selected.il` / `selected.json` | 保留所选 cell 类型、参数、连接和位序的子网表 |
| `selected.provenance.json` | 来源、候选精度、共享关系、边界、ABC 证据摘要和覆盖缺口 |
| `selected.ys` / `selected.log` | 子网表检查与格式等价验证脚本、日志 |
| `selected.roundtrip.json` | 含状态时保存的 Verilog 重读结果，用于逐位初始化核对 |

产物先在临时目录中完成检查和等价验证，成功后发布；任一同名产物已存在时拒绝覆盖。导出等价检查证明格式转换保持所选网表功能，不能代替来源选择正确性验收。inout 和多驱动边界目前拒绝导出。

新一轮完整 synth 验收、ABC 对应和优化吸收示例见 [来源选择验收](docs/selection-acceptance.md)。

过程动作细化的使用示例、精度边界与 pmux 导出语义见 [过程来源说明](docs/process-provenance.md)。

## 配置与产物

```bash
python3 -m yosys_flow run \
  --source path/to/top.sv --source path/to/definitions.sv \
  --top demo -I path/to/include -D WIDTH=8 --param N=8 \
  --out runs/configured
```

`--source`、`-I`、`-D`、`--param` 可重复；参数限数值，宏值不支持包含空白的复杂定义。源文件、运行和导出路径支持空格；当前前端的 include 目录不支持空白。当前限定单模块，`--instance` 只接受顶层模块名，尚未支持多实例上下文。

| 运行产物 | 内容 |
| --- | --- |
| `manifest.json` | 状态、真实终点、配置、输入与产物哈希、工具摘要、耗时和覆盖缺口 |
| `yosys.jsonl` | 原生 JSONL v2 来源、对象版本、步骤、变换、连接迁移与检查点 |
| `frontend.il` / `frontend.d` | 前端 RTLIL 和依赖清单；真实输入清单也保存在 `run_end` |
| `after.il` / `after.json` / `after.v` | 默认 synth 或显式开发检查点的最终网表 |
| `baseline.il` / `baseline.json` | 独立原版执行相同配置、相同流程的结果 |
| `yosys.jsonl.abc_*.input.blif` / `.output.blif` / `.script` | 实际执行的 ABC 网络及脚本 |
| `*.ys` / `*.log` / `validation.json` | 实际命令、运行日志及验证结论 |

运行器重放生命周期和来源引用，核对最终对象与连接，与原版 JSON modules 严格比较，再执行等价检查。`validate` 重算产物哈希和记录检查，读取已保存的等价结果，不重新运行形式工具。`status=complete` 表示运行及验证完成，`coverage=partial` 表示来源链仍有缺口，两者独立。

## 开发检查点和测试

所有入口共用同一原生记录器与 JSONL v2 读取器。日常使用默认 `synth`；以下检查点仅用于定位来源在哪个阶段丢失：

| 显式选项 | 真实终点 |
| --- | --- |
| `--flow m1`（proc 检查点） | `proc -noopt; opt_merge` |
| `--flow m1-opt`（基础优化检查点） | `proc; opt_expr; opt_clean; opt_merge; opt_clean` |

检查点不是默认 synth 的替代终点。构建与详细范围见 [proc 检查点说明](docs/native-m1.md)。`make test` 构建后运行全部测试；`make test-native` 为同一测试入口。

`tests/test_selection.py` 检查最终选择集合、优化吸收和 ABC 候选隔离；`tests/test_process.py` 检查逐动作、分支、覆盖及位范围来源；`tests/test_native.py` 检查过程来源、状态、连接和原生合并；`tests/test_synth.py` 检查默认综合、ABC 精度及最终导出；`tests/test_export.py` 保留迁移后的共享、切割、总线、参数、路径和覆盖保护测试；`tests/test_cli.py` 检查默认入口及退役格式的明确拒绝。

## 代码入口

| 路径 | 作用 |
| --- | --- |
| `native/` | 原生记录器、过程证据实现、前端身份和固定源码依赖锁 |
| `tools/build_native.py` / `tools/instrument_native.py` | 构建与固定锚点插桩，保留上游自动命名行号 |
| `yosys_flow/common.py` | 公共错误、校验、哈希和参数处理 |
| `yosys_flow/runner.py` | 统一运行、读取和查询入口 |
| `yosys_flow/native.py` | 原生运行、JSONL v2 重放、来源查询和精度报告 |
| `yosys_flow/extract.py` | 子网表切割、状态保留、导出与验证 |
| [docs/schema.md](docs/schema.md) | 原生格式及不变量 |
| [docs/default-synth-plan.md](docs/default-synth-plan.md) | 阶段计划和三层验收 |
| [docs/roadmap.md](docs/roadmap.md) | 后续完整来源及增量综合研究 |

## 旧版本退役

M0 独立插件、`--flow opt-merge`、`--plugin`、`--steps`、`--nomux` 及 JSONL v1 读取器已移除。历史实现由 Git 保存；旧结果文件不会自动删除或转换，当前 CLI 读取 v1 时会明确提示退役，需要历史 Git 版本才能读取。

这次退役不删除 Yosys 的普通 `opt_merge` 或其原生记录钩子，也不取消独立原版 Yosys 对照。M0–M4 仅在开发计划中表示验收里程碑，不表示多套需要并行维护的产品实现。
