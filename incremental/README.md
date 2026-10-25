# 来源驱动的局部综合与替换

用户提供**旧综合归档和完整新版 RTL**，工具自动发现变化、选择旧网表区域、提取新版局部功能、推导连接并生成完整新版网表。来源记录是依据，在线轻量替换计划描述本次删门、边界和证据引用；完整契约不是自动流程的重复输出。

## 自动入口

```bash
python3 -m incremental auto \
  --base-run runs/old-run --new-source path/to/new.v --out runs/edit

# ANUBIS 准备器仅提供旧归档和完整新版，不提供修改位置或 patch
python3 -m incremental anubis --automatic \
  --cases compare_local9 --mode retained --out runs/automatic-anubis
```

`--out` 须不存在。旧归档包含首次插桩 `synth` 的网表、前端表示和来源证据；新建归档另保存验证索引，已有旧归档可以重放。顶层、参数、宏和层次模式继承归档。默认 `--selection-mode fine` 从变化赋值及依赖切片起步，`module` 仅用于同框架的粒度对照。

默认开启叶模块组合等价预检查：每项 5 秒、共享总预算 15 秒。功能不变且结构上下文满足前提时逐字节复用旧网表；超时或未知继续局部流程，`--no-equivalence-precheck` 可关闭。完整行为、`replacement-plan.json` 格式、计时和限制统一见 [automatic.md](automatic.md)。

局部替换支持已覆盖的普通组合层次、连续赋值和组合过程，交付平坦通用门网表。依赖缺失、来源候选、共享消费者无法绑定或验证失败会拒绝发布。尚未实现任意语法的最小区域、消费者自动拆分、保持拼接层次、时序局部替换或替换后的连续来源归档。

## 性能对比

```bash
python3 -m incremental compare --mode retained --repeats 3 --out runs/comparison
```

默认只比较 ANUBIS 功能修改组合适配例 `compare_local9`、`control_local2`，关闭等价预检查。`F` 是完整新版全量综合，`P-module` 和 `P-fine` 使用同一来源/替换框架的两种选区粒度；独立方法 `M` 不在本项目内。

主要时间截止于局部逻辑接回旧网表、完整 JSON/IL/Verilog 写出。首次建档和生成后验证分别记录；通过验证的样本才参与速度和通用门数量比较。可选 `--liberty` 将成功交付的网表统一映射，报告标准单元面积、独立映射时间及生成加映射时间；固定 Nangate45 库的获取方法见 [工艺库说明](../benchmarks/libraries/README.md)。NOC、负例及人工控制只作诊断。固定案例、统计与测量结果见 [comparison.md](comparison.md)。

扩展到更大组合电路时，使用 `--suite epfl-functional`，默认 F/P-fine、原始单模块、不重复展平模式。三个 EPFL 电路各有两处固定功能修改；离线验证修改有效性，共享三份旧建档，保留来源阻塞和资源超限。运行说明与结果见 [epfl-comparison.md](epfl-comparison.md)。

## 显式区域实验与审查接口

```bash
python3 -m incremental demo --out runs/incremental-demo

python3 -m incremental run \
  --base-run runs/incremental-demo/retained/base-run \
  --contract runs/incremental-demo/retained/region.contract.json \
  --bindings runs/incremental-demo/retained/bindings.json \
  --patch-rtl incremental/examples/four_lanes/patch.v --patch-top patch \
  --new-source incremental/examples/four_lanes/new.v --out runs/manual-edit
```

`demo` 在保留层次和显式展平下验证四条 8 位组合通道，只修改 `chip/u1/core` 的 `(a & b) ^ c` 为 `(a | b) ^ c`。[旧版](examples/four_lanes/base.v)、[新版](examples/four_lanes/new.v)和[显式 patch](examples/four_lanes/patch.v)均保留。旧设计 152 门，选中 16 门，保留 136 门；16 个新门加 8 个边界缓冲后交付 160 门，不对整网表再优化。独立边界证明验证实际功能和极性，整体 SAT 覆盖全部 72 个输出位，包括未修改通道、`side` 及顶层汇总。

显式 `run` 需要调用者给定局部功能与绑定，接受完整 v1 契约或 v2 替换计划并重新验证。`bindings.json` 的 `contract_id` 绑定确切内容，输入项可用 `{"port":"in_...","inverted":false}`，输出项为 `"out_..."`，向量位序从低位到高位，全部边界恰好绑定一次。`auto` 则从新版表示和旧记录推导这些信息。两者都不从完整新版综合网表搜索或复制新门。

所有未选门的类型、参数、属性和连接保持不变。局部函数证明、外部消费者检查、组合环检查、整体新版等价及 Verilog 重读通过后才发布网表；旧局部证明不自动保证任意修改安全。错误位序/极性、遗漏输出、新依赖、共享输出分化等反例由现有测试覆盖。独立契约命令见 [区域契约](../provenance/docs/region-contracts.md)。

## NOC 与时序前置检查

原始 NOC 使用上游顶层及配置，不以独立子模块反例代替顶层结论：

```bash
python3 -m incremental anubis-nochange --upstream-top --repeats 1 \
  --frontend-timeout 300 --out runs/anubis-noc-upstream
```

模块边界调查与 `--with-sequential` 仍作为独立诊断。固定宏清单、参数及上游顶层结果见 [ANUBIS 文档](../benchmarks/anubis/README.md)。`seq-baseline` / `seq-reuse` 使用 EQY/SBY，在已声明的时钟、初始化或启动复位条件下证明整网表可以复用；失败/未知不进入组合增量。命令、语义和待补证明见 [sequential.md](sequential.md)。

运行 `make test` 生成完整回归报告。当前本地结果集中在 `runs/comparison-anubis-functional-v4/`，其中 `validation/` 保留相关回归摘要；其余历史运行产物已清理。`runs/` 不提交。
