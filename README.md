# Yosys-Flow

按照[分享对话中的方案](https://chatgpt.com/share/6aca0247-c794-83e8-a1e1-31c0c9792df2)实现的第一阶段原型：**在 Base 综合时主动记录对象演化，以 JSONL 保存，并支持 RTL → 合并后网表、网表 → RTL 的双向查询。**

目前实现统一记录器、实际插桩的 `opt_merge`、独立查询程序、检查报告，以及按源码位置导出带边界端口的子网表。后续完整流程已确定以默认 `synth -top <top>` 为目标，见 [默认 synth 接入计划](docs/default-synth-plan.md)。当前追踪终点仍是 `opt_merge`，尚未覆盖完整 synth、局部重综合或网表拼接。

## 快速运行

依赖 Python 3.10+、GNU Make、支持 C++20 的 g++，以及带开发头文件的 **Yosys 0.67+24，提交 `0e82bbefe`**。当前工作环境已具备这些依赖。Python 程序只用标准库，不需要安装数据库或 pip 依赖。

```bash
cd /workspace/Yosys-Flow
make
python3 -m yosys_flow run --source examples/merge.v --top demo --out runs/my-first-run
python3 -m yosys_flow query runs/my-first-run --source examples/merge.v --line 2
python3 -m yosys_flow query runs/my-first-run --source examples/merge.v --line 3
python3 -m yosys_flow extract runs/my-first-run --source examples/merge.v --line 2 --out runs/exports/selected.v
python3 -m yosys_flow validate runs/my-first-run
make test
```

每次 `run` 都必须使用一个不存在的目录，避免覆盖 Base 历史。`make demo` 等价于构建后运行 `examples/merge.v` 到 `runs/merge` 并查询第 2 行；再次运行请改用新的 `--out`。

示例 RTL：

```verilog
module demo(input a, b, output y1, y2);
    assign y1 = a & b;
    assign y2 = a & b;
endmodule
```

两次源码查询会找到同一个 `$and`，`all_origins` 包含两处表达式位置，`consumers` 包含 `y1` 和 `y2`。复制查询结果里的完整 `ref` 可反查来源，例如：

```bash
python3 -m yosys_flow query runs/my-first-run --object 'yosys:object_20@1'
```

编号在运行内分配，这个例子里的数字不是固定 API；不同设计、版本和运行应使用各自结果中的 `ref`。`run_id + ref` 才能唯一定位一个运行中的对象。

`examples/merge_new.v` 将第一处 AND 改成 OR。单独运行它会得到两个门，用来检验为什么修改共享逻辑时不能直接替换旧 AND。这里对 New 的完整运行仅是测试用对照，不是已经实现的增量算法。

## 从源码导出子网表

```bash
python3 -m yosys_flow extract runs/my-first-run \
  --source examples/merge.v --line 2 \
  --out runs/exports/selected.v
```

此命令只复制所选来源关联的 cell，**不递归加入上游驱动 cell**。子模块边界由同一次运行的 `after.json` 实际电气连接确定：外部驱动成为输入、外部使用成为输出，内部连线和常量保留。未被任何对象使用的选中输出也会暴露为观察端口，并在说明中标为 `unconsumed_selected_output`。

示例导出的逻辑如下：

```verilog
module selected(input in_2, input in_3, output out_4);
    assign out_4 = in_2 & in_3;
endmodule
```

端口按信号位命名；数字是原 JSON 的电气信号 ID，不是源码变量下标。`out_4` 对应原设计中的两个别名 `y1`、`y2`，不会因为有两处源码来源而复制两遍共享门。对于总线，每一位使用独立边界端口，cell 的位宽、符号参数和端口位序均保留。

一次导出会生成：

| 文件 | 内容 |
| --- | --- |
| `selected.v` | 可独立读取的 Verilog 子模块，内建 cell 由 Yosys 转成表达式 |
| `selected.il` | 保留原 cell 名称、类型、参数的 RTLIL 子网表 |
| `selected.json` | 从 `after.json` 直接复制的 cell 及边界连接 |
| `selected.provenance.json` | 所选 cell 的全部已记录来源、共享使用者、端口和原信号的对应、覆盖缺口及验证结果 |
| `selected.ys` / `selected.log` | 独立子网表检查和导出格式等价检查的脚本、日志 |

导出先在临时目录中通过 `check -assert`，再重新读取生成的 Verilog，与直接复制的 cell 子网表做形式等价检查；全部通过后才发布产物。该检查证明导出格式保持功能，不证明来源覆盖完整或这些门可被单独替换。任一同名产物已存在时拒绝覆盖。脚本使用相对文件名，可在导出目录内用 `yosys -s selected.ys` 复查。

`query` 和 `extract` 都支持 `--column` / `--end-column`（从 1 开始，查询末列包含在范围内），可缩小同一行多个表达式的选择范围。例如 `--line 2 --column 17 --end-column 21`。仅给 `--column` 时选择该位置；给 `--end-line` 时和末列共同构成连续范围。匹配依然基于 `src` 范围，不是独立 AST 语句身份。

也可用 `extract --object '<最终 cell 的 ref>'` 或 `--module-name my_slice`。目前不支持 inout 或多驱动的边界，也不导出缺少语句来源证据的纯连接子网表。

空查询现在返回明确的 `resolution`：

| 值 | 含义 |
| --- | --- |
| `cells_found` | 找到来源关联的最终 cell，可尝试导出 |
| `non_cell_objects_only` | 只找到 wire/connection 等对象，见 `final_non_cell_objects` |
| `source_not_mapped` | 该源码位置没有已记录的来源映射 |
| `no_final_objects` | 找到了起点，但查询方向上没有可达的最终对象 |

以上后三项均不表示“这条语句没有影响”或“已经被消除”。纯 `assign y=a` / 常量赋值的语句来源尚不完整，导出会给出说明并拒绝生成空模块。只有将来有明确删除事件时，才能报告“被消除”及其原因。

## 运行流程

```text
源文件、include、宏、顶层参数
    │ read_verilog -sv -defer; hierarchy -check
    ▼
frontend.il + before.json + 实际输入文件清单
    ├─ trace_begin → trace_opt_merge → trace_end → after.il / after.json
    └─ 原版 opt_merge → baseline.json

事件重放及快照检查 + 与原版网表严格比较
    + equiv_make / equiv_simple / equiv_status -assert
    ▼
validation.json 通过后，manifest.status 才设为 complete
```

前端只执行一次，两个分支读取同一份 RTLIL，从而避免前端重复生成的命名差异干扰对照。`trace_opt_merge` 使用固定版本的上游实现，仅在实际变换处加入观察钩子；没有重写合并算法，没有添加 `keep` 或来源属性来改变综合决策，也没有覆盖内置 `opt_merge` 命令。

独立插件适合当前小实验。若以后要直接记录 `opt`/`synth` 内部调用，需把同一组钩子接入所用 Yosys 源码。当前执行内置 `opt` 不会自动获得本插件的覆盖。

## 配置

```bash
python3 -m yosys_flow run \
  --source path/to/top.sv \
  --source path/to/definitions.sv \
  --top demo \
  -I path/to/include \
  -D WIDTH=8 \
  --param N=8 \
  --steps 2 \
  --out runs/configured
```

- `--source`、`-I`、`-D`、`--param` 可以重复。参数目前支持数值形式；宏值支持常用数字、标识符和运算符，不支持带空白的复杂宏定义。
- `--steps` 可验证同名 pass 多次调用时的顺序和独立步骤编号；`--nomux` 对应原版的选项。
- `--yosys` 和 `--plugin` 可指定路径，但二进制必须匹配上述固定版本。编译器可用 `make CXX=...` 指定。
- 源文件和运行目录可含空格。当前固定版本前端的 `-I` 路径不处理引号，因此本程序明确拒绝包含空白的 include 目录。
- `query --line A --end-line B` 查询 Base 的源码范围，`--instance demo` 限定实例；当前阶段只支持一个展开后的组合模块，因此实例就是顶层模块。

## 产物

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 运行编号、状态、覆盖缺口、工具/插件摘要、实际源码及 include 摘要、宏、顶层参数、脚本和产物哈希、时间与可用时的峰值 RSS |
| `yosys.jsonl` | 对象定义、版本、步骤、真实合并事件、连接重定向和结构快照 |
| `inputs.json` / `frontend.d` | Yosys 实际读取的输入清单；JSON 用于处理含空格的路径，Make 依赖文件保留供检查 |
| `frontend.il` / `before.json` | 追踪开始前的 RTLIL 和独立 JSON 网表 |
| `after.il` / `after.json` / `after.v` | 追踪结束时的网表 |
| `baseline.json` | 原版 `opt_merge` 对照结果 |
| `*.ys` / `*.log` | 实际命令和工具日志，包括形式等价检查 |
| `validation.json` | 引用、版本、结构重放、原版对照和等价检查结果 |

`status=complete` 与 `coverage=partial` 可以同时成立：前者表示这次实验执行和验证成功，后者表示它不是全流程来源覆盖。所有查询都输出 `safe_for_incremental_reuse=false`，并附带具体缺口。

日志为缓冲顺序写入，每行一个 JSON 对象。记录器不逐事件刷盘；中断可能丢失末尾缓冲，因此读取必须检查 `run_end`、运行清单和产物哈希。运行失败会保留目录及日志，写入 `failed`；即使进程被强杀而来不及写 `failed`，遗留的 `running` 也不会被查询程序接受。完成清单采用临时文件加原子替换，不承诺断电事务持久性。

计时包含进程启动、校验及额外产物写入，不等于仅记录器自身的开销。Linux 下存在 `/usr/bin/time` 时会记录各进程峰值 RSS，日志大小可直接从产物清单读取。小例子的开销比例不适合外推到大设计。

## 覆盖边界

**已经覆盖：** `opt_merge` 运行内的单元身份、保留对象的新版本、被删除对象的完整旧状态、新增信号别名、合并的多来源关系、重复步骤、前后实际结构校验。wire、signal bit、module 和 connection 均有独立记录，常量与直接连线不会因为没有 cell 而从结构快照中消失。

**尚未覆盖：** 宏展开、AST 节点演化、条件/参数依赖、generate 未选分支、`proc`、其他优化、`flatten`、`techmap`、ABC、时序状态对应、区域切割、局部综合和拼接。前端来源只是 `src` 属性观察，不能解释已经被前端删除的对象。直接赋值的连接被保留，但不承诺恢复没有 `src` 的赋值语句来源。

当前拒绝 process、memory、触发器、模块实例及多模块设计，避免将这些设计误报为已覆盖。阶段间实际新增、删除或改变 RTLIL 对象而没有记录的情况会被检测并使运行失败。不过，快照检测不能发现某个外部 pass 曾修改又完全恢复的内部历史，因此应用应使用受控 CLI 流程，不能把任意自定义脚本视为获得了全覆盖。

查询遍历的只有来源关系；电路连接单独用于报告驱动和使用者，不会沿连接无限扩大“来源”。`edges` 是可达事件子图，保留多条路径，不是只给一个最短路径。来源位置信息不是 AST 精确身份，信号与连接粒度也不意味着已经得到位级逻辑依赖证明。

Base 的源码后来修改或移走时，历史仍可查询；结果中的 `changed_or_missing_base_inputs` 会提示对应文件，行号仍然指向 Base 坐标。新增语句不具备旧来源，需要后续 New RTL 解析与差分算法处理。

## 代码入口与后续扩展

| 路径 | 作用 |
| --- | --- |
| `plugin/recorder.h` | 统一对象/步骤/事件记录器，以及 `trace_begin`、`trace_end`、`trace_inputs` 命令 |
| `vendor/opt_merge.cc` | 固定版本上游源码，保留原始 ISC 版权许可 |
| `tools/instrument.py` | 校验源码 SHA-256，再在明确的转换位置加入钩子；锚点变化时拒绝生成 |
| `yosys_flow/runner.py` | 运行、配置摘要、原版对照、等价检查、完成状态 |
| `yosys_flow/trace.py` | JSONL 检查、事件重放、双向来源索引、独立连接索引 |
| `yosys_flow/extract.py` | 复制来源关联的 cell、计算电气边界、导出并验证子模块 |
| `tests/test_flow.py` | 真实 Yosys 集成测试和损坏日志测试 |
| [docs/schema.md](docs/schema.md) | 格式约定与一致性规则 |
| [docs/roadmap.md](docs/roadmap.md) | 后续前端、其他 pass、ABC 与增量综合的接入顺序 |
| [docs/default-synth-plan.md](docs/default-synth-plan.md) | 用户已选定的默认 synth 目标、阶段清单和验收标准 |

上游依据：[固定版本 opt_merge 源码](https://github.com/YosysHQ/yosys/blob/0e82bbefe/passes/opt/opt_merge.cc)、[Yosys 扩展文档](https://yosyshq.readthedocs.io/projects/yosys/en/latest/yosys_internals/extending_yosys/extensions.html)。
