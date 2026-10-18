# 普通组合层次、展平与实例查询

固定版本 `0e82bbefe` 的 `synth` 将 `flatten` 初始化为 false，只在指定 `-flatten` 时于 `proc` 后调用展平。默认 `run` 因此保留层次；`run --flatten` 使用 `synth -top <top> -flatten`。两种模式均执行默认 techmap、ABC 和后续优化，不加入 keep，也不更改 ABC 脚本。Yosys [synth 命令说明](https://yosyshq.readthedocs.io/projects/yosys/en/v0.58/cmd/index_techlibs_common.html)也将展平列为显式选项。

## 已支持的输入

普通组合模块的嵌套实例、同一模块多次实例化、命名或位置端口、数值参数覆盖、向量切片、连续赋值和完整组合过程。原有组合输入约束适用于所有子模块；时钟沿、初始化、推导状态、黑盒、inout、递归层次、实例数组、门原语、memory、generate、循环和函数/任务不在支持范围。未连接的输出可保留为无外部使用者的区域输出，未连接输入不被当作已定义功能。

## 一次综合中的记录

- `flow_hierarchy` 在前端 `hierarchy -check -top` 后保存已展开的模块、参数值、实例对象版本、端口方向与逐位绑定；它不改变设计。
- `flatten_instance` 位于真实 flatten 复制现场，记录父实例、模板模块和实际 wire map。
- 复制 cell 与连接保留 `cloned_from`、`flatten_event`；连接保存复制时的 `source_offsets`。源码来源复制保留原始文件/行列，并增加相对于目标模块的实例路径。
- 重放检查实例身份、来源映射、复制前后参数/连接及逐位映射。不是通过猜测展平后的 cell 名称来建立来源。
- 叶模块仍经过真实默认 ABC；保留层次时，各模块的 ABC 调用分别校验。不能只校验日志中最后一个调用的输入输出边界。

此次层次案例的阻碍在实例绑定和 flatten 复制，而不在新的 ABC 内部因果断点，因此没有增加 ABC 内部事件类型。已有不完整因果历史、候选关联和预算回退继续保留；以后按失败案例补具体钩子。

## 保留层次时的只读实例视图

`after.json` / `after.il` / `after.v` 仍是原版同流程的层次网表。查询按记录的实例绑定，把模块定义中的门放到具体实例上下文；只做逐位电气连接展开，不做优化或重新综合。

实例视图中的门有 `definition_ref`、`definition_module`、`definition_name` 和 `instance`。例如两份 `leaf` 中同名门使用不同的实例引用，不能把共用模块定义误认为共用物理门。边界别名的 `definition` 保存原模块、wire、局部 bit ID 和实例，便于回到层次网表定位。边界 `original_bit` 在这种模式下是实例视图中的电气 ID，不是未经限定的模块局部 ID。

第一次 `run` 保存 `instance-view.json`，并在独立验证进程中将原层次网表展开，与视图做整份设计等价检查；同时检查跨模块组合环和多驱动。验证副本清除 `keep_hierarchy`，综合产物与原版对照保留用户属性。后续查询、契约生成、合并和验证只读记录，不启动综合。

`--flatten` 尊重原有 `keep_hierarchy`，因此也可能产生混合层次。已记录的源码复制链保留原始实例路径，例如最终实例名含 `u0.inner` 时仍可用原始路径 `demo/u0/inner` 查询。

## 查询、契约与导出

```bash
python3 -m yosys_flow run --source examples/hierarchy.v --top demo --out runs/hier
python3 -m yosys_flow query runs/hier --source examples/hierarchy.v --line 4 --instance demo/u0/inner
python3 -m yosys_flow contract runs/hier --source examples/hierarchy.v --line 4 --instance demo/u0/inner --out runs/hier/u0.contract.json
python3 -m yosys_flow contract runs/hier --source examples/hierarchy.v --line 4 --instance demo/u1/inner --out runs/hier/u1.contract.json
python3 -m yosys_flow contract-merge runs/hier runs/hier/u0.contract.json runs/hier/u1.contract.json --out runs/hier/union.contract.json
python3 -m yosys_flow extract runs/hier --contract runs/hier/union.contract.json --out runs/hier/union.v
```

路径以顶层名开头，`/` 分隔实例；单个名字中的特殊字符采用百分号编码。`--instance` 精确选择该实例中定义的语句，不隐式包含所有后代；省略时查询所有实例。反查可直接使用返回的带实例对象引用；一个模块定义对象有多个实例时，必须指定 `--instance` 或使用带实例引用。

契约可以合并同一次运行中不同实例的区域，重新计算跨模块电气边界、全部输出和共享义务。常量使用原始赋值端点及实际实例输出绑定，不能因为值都为 0 就把其他实例的输出列为共同使用者。导出结果是独立门级模块，当前不重建选中区域的原始模块树。

两种模式可能得到不同门数：保留层次时两个实例有各自的门；显式展平后，相同输入的两个表达式可能被合并。后一种情况下查询任意实例都可能得到同一个门，契约必须保留所有实际输出使用者。不同运行的契约不能合并。

## 验收与限制

`tests/test_hierarchy.py` 使用真实工具链覆盖两种模式的两层双实例、不同参数宽度、组合过程、直连/常量、共享、跨实例契约合并、反查、离线查询、导出等价、keep_hierarchy 混合模式及证据损坏。原版和插桩版必须具有相同的完整 JSON modules。

层次支持不会升级底层精度。`coverage=partial`、`end_to_end_exact=false`、`safe_for_incremental_reuse=false` 保留。契约验证仍限定为旧区域函数、边界和声明的来源证据，不等于任意原始 RTL 的完整语义证明或未来修改安全性证明。前端展开、复杂算术和未覆盖 ABC 变换继续按独立案例推进。
