# JSONL v1 格式与不变量

这是本项目自己的实验格式，不是 Yosys 原生事件格式。每行含 `schema_version=1`、`run_id`、连续递增的 `sequence` 和 `record_type`。只有结束标记、清单状态、结构与产物摘要都通过检查的运行才能查询。

## 记录类别

| record_type | 主要字段 | 语义 |
| --- | --- | --- |
| `run_begin` | `capture`, `step_snapshot_ids` | 开始一次独立记录；新日志用 `current` 声明前后快照均标记当前步骤 |
| `object` | `object_id`, `version`, `ref`, `kind`, `module`, `name`, `state`, `source_ids` | 不可变的对象版本定义 |
| `snapshot` | `phase`, `step_id`, `objects` | 从实际 RTLIL 重新读取的存活对象集合和状态 |
| `step_begin` | `step_id`, `pass`, `command`, `coverage` | 一次 pass 调用开始 |
| `transform` | `event_id`, `step_id`, `operation`, `inputs`, `outputs`, `retired`, `connection_changes` | 一次真实变换 |
| `step_end` | `step_id`, `coverage` | 该次调用结束且结构匹配 |
| `run_end` | `status`, `coverage` | 记录正常结束；不能单独代表整个实验已验证 |

`object_id` 为运行内分配的编号，`ref` 是 `object_id@version`。名称、源码位置、实例信息仅作描述，不能充当跨运行身份。当前记录器内部用受控阶段中未被重命名的对象键定位句柄；对重命名、删除后同名再创建、跨阶段身份继承的支持需新增生命周期接口，不应直接复用当前键表。

对象类别：

- `source_span`：某个实例上下文里的 `src` 字符串，`capture=src_attribute`，不是原始 AST 节点。
- `module` / `wire`：模块属性、位宽、端口方向等定义；属性和参数采用 RTLIL 常量的 MSB-first 位串，保留原始值。
- `signal_bit`：wire 名称和 RTLIL 内部 offset。对 `[7:4]`、`upto` 等声明用 wire 元数据解释，不能把 offset 当源代码下标。
- `cell`：类型、参数、属性、各端口方向和 LSB-first 位向量。
- `connection`：有序的 `lhs` / `rhs` 位向量。原始连接方向保留，查询使用它建立电气别名等价类。

位向量元素是 `signal_bit` 的版本引用，或 `{"constant":"0"}` 这样的四态常量。所有参与事件的对象必须先声明；删除保留原定义、连接和来源记录。

## 合并

概念示例：

```json
{
  "record_type": "transform",
  "event_id": "event_1",
  "step_id": "step_1",
  "operation": "merge",
  "capture": "instrumented",
  "granularity": "cell",
  "inputs": ["yosys:object_21@0", "yosys:object_20@0"],
  "outputs": ["yosys:object_20@1", "yosys:object_24@0"],
  "retired": ["yosys:object_21@0", "yosys:object_20@0"],
  "connection_changes": [{
    "connection": "yosys:object_24@0",
    "operation": "add_alias",
    "before": {"driver": "yosys:object_21@0"},
    "after": {"driver": "yosys:object_20@1"}
  }],
  "dependencies": []
}
```

示例省略公共信封字段。`inputs` 顺序固定为“移除者、保留者”；后者即使端口结构未变，也因承载新的合并历史产生新版本。所有输出端口的改接都通过新增 connection 对象记录，其 `state` 给出完整左右端点。

`dependencies=[]` 表示当前事件没有额外记录的条件/参数依赖，不能据此推断设计不依赖参数。`instrumented` 仅表示来自转换实现中的主动钩子，不代表来源关系已被形式证明。形式检查验证本次受控组合逻辑变换前后的功能。

## 重放与检查

1. 检查运行编号、格式版本、记录顺序、对象与事件编号、引用存在及对象版本连续。
2. 初始 snapshot 定义存活集合。事件输入必须存活；旧版本退役；输出只能被生成一次；新版本必须退役其前一版本。
3. 检查合并只有两个旧 cell、一个保留 cell 新版本；保留状态不发生无法解释的修改；每个输出端口都有对应 alias 和驱动变化记录。
4. 按事件重放存活集合，与每个实际 snapshot 比较状态和成员。没有事件解释的对象新增/删除不能通过。
5. 用 Yosys 独立输出的 `before.json` / `after.json` 检查单元集合、类型、参数、端口连接、wire 集合/位宽及 alias。
6. 与原版 `opt_merge` 的 JSON modules 内容严格比较，并独立做前后形式等价检查。

该检查器不重新实现上游优化的所有规范化判定，也不试图证明任意恶意重写的日志真实发生过。产物 SHA-256 用于发现意外修改，不是签名或信任认证。`validate` 重算哈希和结构检查，读取已保存的原版/形式检查报告；它不重新启动形式工具。

来源索引 `对象 → 使用事件`、`对象 → 生成事件` 与电气连接索引相互独立。新格式加入条件依赖、删除、拆分、位精度或 ABC invocation 时，应同步升级写入、校验、查询和测试，不能把未知记录静默忽略。

## 步骤快照兼容

当前 `begin_step()` 先分配步骤 ID，再写 `before` 快照。因此 `step_2` 的前后快照都使用 `step_2`。校验器要求声明 `step_snapshot_ids=current` 的日志在 `step_begin` 前紧邻一个同 ID 的 `before` 快照，并检查 `after` 的 ID。

早期 v1 日志没有此标记，其 `before` ID 可能为空或沿用上一轮。读取器继续允许这些已存在的日志，但不能把其中的 `before.step_id` 作为准确分组依据；不会静默修改历史文件。

## 子网表说明文件

`extract` 的 `.provenance.json` 是导出报告，与原始 JSONL 分开保存。它记录原运行 ID、`after.json` 摘要、选择条件、原模块、导出模块、所选 cell 及来源事件子图、实际边界、导出产物摘要和检查结果。

`boundary` 的每项对应一个一位端口：`original_bit` 是原 `after.json` 的电气信号 ID；`aliases` 包括原 wire 名、LSB-first offset 和按原声明方向换算的 `source_index`；`drivers/users` 记录完整端点，`external_drivers/external_users` 只列选中集合外部端点。这些 bit ID 与日志中的 `signal_bit` 对象 ID 是不同编号空间。

`mode=source_association`、`upstream_cone_included=false` 表示导出范围由来源关系选择，没有递归加入上游逻辑。`last_recorded_pass=opt_merge` 和 `coverage=partial` 防止把当前末端误认为完整 synth 末端。`safe_for_incremental_reuse` 仍然为 false。

## 原生试验格式 v2

原生 `m1`、`m1-opt` 和 `synth` 的 manifest 和 JSONL 使用 `schema_version=2`，由独立 `NativeTrace` 读取；以上原有说明适用于 M0/v1。v2 的范围、精度和验证见 [native-m1.md](native-m1.md) 和 [native-synth.md](native-synth.md)。v2 行带连续 `seq` 和当前 `step_id`，步骤记录带父步骤；`source` 在 AST 简化前分配来源 ID；`cell_create`、`cell_state`、`merge`、`cell_remove` 表达 cell 生命周期；`assignment`、`process_action`、`initialization` 保存实际生成位置的绑定。`run_end` 保存最终 cell 引用和真实输入文件清单。

`cell_state.capture=structural_observation` 仅声明观察到的结构版本，不能当作变换原因。`precision=process_signal_candidate` 不能升级为精确 cell 来源。v2 始终保留 `coverage=partial` 和 `end_to_end_exact=false`，尚未达到 v1 对受控 opt_merge 的完整语义事件重放保证。

新建对象和连接可带 `inputs` / `operation`，指向实际目标处理位置捕获的输入版本。替换可能先退休原对象再创建新对象，因此这些输入可以是历史版本；读取器验证引用存在和来源并集。`cell_state.capture=before_instrumented_rewrite/instrumented_rewrite/before_removal` 标明观察时机，不单独证明跨对象语义。

`connection_state` 使用稳定连接 `ref` 和从 1 连续递增的 `revision`，记录迁移后的 `lhs` / `rhs`、原位 `offsets` 和原因（`opt_clean_alias_representative` / `signal_removed`）。位只能保留或退休；原始 LHS 仍由创建事件保存。新检查点额外保存每条连接的修订号，读取器支持旧 v2 中缺少此字段的检查点。最终活动连接必须与 `after.json` 的实际别名一致。

`abc_region` 使用唯一 `invocation_id`，保存真实提取的 `inputs`、来源并集、原信号 `signals` 及其 `input_ports` / `output_ports` 索引、ABC 可执行路径和归档文件名。成员必须与同一步骤带该调用 ID 的退休事件一致；重新导入的对象带 `abc_invocation`，输入集合必须与该区域相同，精度只能是 `abc_region_candidate`。归档 BLIF/脚本列入 manifest 摘要，输入边界还要与实际 BLIF 一致。`cell_group_candidate` 表示实际多输入模型的候选集合。所有候选均不升级为端到端精确定位。
