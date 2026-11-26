# 基准实验与诊断

这个目录保存基准适配、测量和调查工具。实际建档、选区、替换与证明仍调用 `provenance/` 和 `incremental/`；实验工具不授予额外的来源精度或安全保证。

```bash
# ANUBIS 功能修改，比较完整新版网表生成时间
python3 -m experiments compare --repeats 2 --out runs/comparison

# EPFL 六个组合电路的固定修改
python3 -m experiments compare --suite epfl-functional \
  --methods F P-module P-fine --repeats 1 --job-timeout 360 \
  --out runs/epfl-comparison

# 普通组合层次的四通道示例
python3 -m experiments demo --out runs/incremental-demo

# NOC 调查按上游顶层与配置进行，独立于功能修改性能比较
python3 -m experiments anubis-nochange --upstream-top --repeats 1 \
  --frontend-timeout 300 --out runs/anubis-noc-upstream
```

`--out` 必须是新目录。`compare --index-backend sqlite|gzip` 选择旧建档与在线查询后端；`--liberty FILE` 统一映射估计面积。计时截止于整份新版网表写出；旧建档及后验验证单列，失败样本不产生有效加速比。

| 工具/资料 | 用途 |
|---|---|
| [对比协议](docs/comparison.md) | F、P-module、P-fine 的范围、时间和面积口径 |
| [EPFL](docs/epfl.md) | 固定输入和修改、功能资格检查与规模 |
| [双后端结果](docs/backend-results.md) | ANUBIS/六个 EPFL 电路的单次补足复测 |
| [耗时归因](docs/cost-attribution.md) | 跨案例归因与 `python3 -m experiments.profile` |
| [索引微基准](docs/index-backends.md) | 同一验证记录、相同查询下的历史公平对比 |
| [hyp](docs/hyp.md) | 最新保留的大案例结果与复现脚本 |
| [div](docs/div.md) | 第二个大组合电路；旧建档、通用证据、全量对照和真实首次增量 |
| [ANUBIS 清单](../benchmarks/anubis/README.md) | 功能修改、NOC 与配置的固定来源 |

`experiment.py` 调度、`experiment_worker.py` 执行独立测量、`experiment_metrics.py` 统计；`anubis.py/epfl.py` 准备固定案例，`profile.py` 仅在显式启用时采样。`nochange*.py` 和 `seq_anubis.py` 是独立调查入口，EQY 复用功能仍在 `incremental/sequential.py`。

旧的 `python3 -m incremental demo|anubis|compare|anubis-nochange|anubis-sequential` 保留命令转发，Python 导入与新文档统一用 `experiments.*`。不再复制一套旧实验实现。`experiments/tests/` 纳入 `make test`，包括测量口径、超时与资格检查等回归。
