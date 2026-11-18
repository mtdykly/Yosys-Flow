# 来源驱动的局部综合与替换

输入是**旧综合归档与完整新版 RTL**。工具自动找修改、确定旧门区域、提取新版局部功能并绑定边界，输出完整新版网表。在线轻量替换计划引用旧来源证据；完整契约用于独立审查与导出。

## 自动入口

```bash
python3 -m incremental auto \
  --base-run runs/old-run --new-source path/to/new.v --out runs/edit
```

输出目录须不存在；多个文件重复 `--new-source`。顶层、参数、宏及层次模式继承旧归档。SQLite/gzip 自动识别，不需要再次选后端。默认 `--selection-mode fine` 从变化赋值与依赖切片起步；`module` 是同框架中的粒度对照，并非独立模块级增量算法。

默认叶模块等价预检查每项 5 秒、总预算 15 秒；证明功能不变且上下文符合条件时，复用完整旧网表。超时或未知进入增量流程，`--no-equivalence-precheck` 可关闭。结构差分按 cell、连接和完整 process 分块，临时名采用全模块一致映射，重复匹配有计算预算；无法判断的区域保守记为变化。

候选生成后，整网验证复用已证内部对应划分求解，默认共享预算 1200 秒。通过全部验收才发布结果。已有候选与参考可用 `verify-netlists` 单独重试，不必重新综合。当前能力和已知失败见[状态页](../docs/status.md)。

| 说明 | 内容 |
|---|---|
| [自动流程](automatic.md) | 差分、选区、patch、绑定、计划格式、失败条件与计时 |
| [整网验证](netlist-equivalence.md) | 结构匹配、已证切点、分区及 CEC/SAT |
| [时序预检查](sequential.md) | 独立 EQY/SBY 的整份旧网表复用，不提供时序局部替换 |
| [区域契约](../provenance/docs/region-contracts.md) | 显式审查、合并与导出 |

## 显式区域接口

```bash
# 创建四通道组合示例及其区域契约
python3 -m experiments demo --out runs/incremental-demo

python3 -m incremental run \
  --base-run runs/incremental-demo/retained/base-run \
  --contract runs/incremental-demo/retained/region.contract.json \
  --bindings runs/incremental-demo/retained/bindings.json \
  --patch-rtl incremental/examples/four_lanes/patch.v --patch-top patch \
  --new-source incremental/examples/four_lanes/new.v --out runs/manual-edit
```

`run` 接受完整契约或轻量计划；调用者显式提供 patch 和绑定。`auto` 则自动推导。两者均检查边界、极性、所有外部使用者、保留门、单驱动、无环以及全部顶层输出，不能把旧区域证明当作任意修改都安全的保证。

当前支持已覆盖的连续赋值、组合过程和普通组合层次。共享消费者分化、消失切点、常量或直连输出无法绑定时仍可能拒绝；不能保证最小区域。交付网表为平坦结构，替换后的连续来源归档尚未实现。

## 测试与实验

`incremental/tests/` 保留差分、选区、拼接、等价和失败拒绝等主流程回归。`experiments/tests/` 验证基准适配与统计，`make test` 统一执行两者及来源回归。性能比较、EPFL、NOC 调查与耗时归因统一见[实验入口](../experiments/README.md)。
