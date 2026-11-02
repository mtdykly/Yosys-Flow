# 最大已选 EPFL 案例的 proc 时间占比

2026-11-02，对当前已选六个 EPFL 电路中最大的 `arbiter` 新版（`epfl_arbiter_local`）执行一次原版完整综合。源码 13,189 行，单模块、256 位输入、129 位输出；最终 11,839 个 generic 门。

沿用之前冻结的局部运算符修改，源码 SHA-256 为 `8580e70c29d82329b25ee33a5bd994d04cfe5b98928c9f041126e8de1b316d6a`；Yosys 为 `0.67+24 / 0e82bbefe`。固定 CPU 2、墙钟上限 360 秒，实际成功结束。未清空操作系统缓存，只有一次测量。

```yosys
read_verilog -sv -defer new.v
hierarchy -check -top top
synth -top top
write_json netlist.json
write_rtlil netlist.il
write_verilog -noattr netlist.v
```

保留一个完整 `synth` 调用，通过 `-t` 日志时间戳确定边界；没有拆分 synth，也没有使用插桩版。`proc` 的结束取下一项同级 `opt_expr` 开始，包含 `proc` 内部的 `opt_expr`。日志边界有微小调度/输出开销；没有插入网表快照打断综合。

| 测量项 | 墙钟秒 | 口径 |
| --- | ---: | --- |
| 前端读取、展开 | 0.755848 | `read_verilog -defer` + 外部 `hierarchy`，含 AST 到 RTLIL 的展开 |
| synth 内部、proc 之前 | 0.029276 | synth 的初始 hierarchy 等开销 |
| proc 本身（含子 pass） | 0.135877 | 占完整 synth 的 1.53% |
| synth 开始到 proc 结束 | 0.165153 | 占完整 synth 的 1.86% |
| RTL 读取开始到 proc 结束 | 0.921001 | 占“读取、展开 + synth”的 9.55% |
| proc 结束后的 synth 剩余部分 | 8.720634 | 包含后续优化、techmap、ABC 及最后检查 |
| 完整 synth | 8.885787 | 不含外部前端和文件写出 |
| RTL 读取、展开 + 完整 synth | 9.641635 | 对应上述从新版 RTL 开始的分母 |
| 三种网表文件写出 | 0.097455 | JSON、RTLIL、Verilog |
| RTL 读取到完整网表写出 | 9.739090 | 不含工具启动/退出 |

本例从新版 RTL 走到 `proc` 完成约需 **0.921 秒，占读入加完整综合的 9.55%**。其中 `proc` 本身约 0.136 秒。综合后的 `check` 报告 0 个问题；完整日志和最终网表已经保留。

该电路只有连续 `assign`、没有 `always`，日志显示没有待转换的过程。`proc` 仍会运行清理、检查及内部常量折叠，因此耗时不为零。这次结果不能代表复杂 `always`/`if`/`case` 的过程转换成本，也不是当前增量流程全部前端、差分和 patch 提取的计时。

本次 ABC pass（包含交接和子进程）为 3.932 秒，占 synth 的 44.25%。辅助 `--perffile` 给出的是各 pass 扣除子调用的 CPU 时间，不能直接把其中 `proc` 或 `synth` 的单行当成完整耗时；本表全部使用墙钟时间。

原始产物目录：`runs/proc-timing-arbiter-20261102/`。包含 `report.json`、`full.log`、`full.ys`、`resources.txt`、`pass-performance.json`、源码、最终网表，以及可复核的 `measure.py` 和 `analyze.py`。这些运行产物仍由 Git 忽略。
