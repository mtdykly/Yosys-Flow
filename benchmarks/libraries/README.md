# 面积比较使用的工艺库

当前使用固定版本 OpenROAD-flow-scripts 中的 Nangate45 typical 库，来源、工艺角和文件摘要见 [nangate45.json](nangate45.json)。它用于研究和 EDA 流程测试，上游明确说明该平台不可制造。这里只估算标准单元面积，不报告布局布线后的芯片面积。

库来自 [OpenROAD 的固定提交](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/tree/3b1b103a27caa9f3b95a7dde6b29e512e9b2c070/flow/platforms/nangate45)，同时保留该平台分发的 Apache-2.0 `LICENSE`、`README.md` 和原始文件声明。工艺角为 typical / TypTyp、1.1 V、25 °C。大的库文件放在已忽略的 `build/technology/nangate45/`，不提交进 Git；实验归档需要保存其使用的库及摘要。

在仓库根目录执行以下代码，可按锁文件下载并校验全部来源文件：

```bash
python3 - <<'PY'
import hashlib
import json
import urllib.request
from pathlib import Path

lock = json.loads(Path("benchmarks/libraries/nangate45.json").read_text())
base = (
    "https://raw.githubusercontent.com/The-OpenROAD-Project/"
    f"OpenROAD-flow-scripts/{lock['commit']}/{lock['source_directory']}/"
)
for name, expected in lock["files"].items():
    target = Path(lock["cache_directory"]) / name
    data = target.read_bytes() if target.exists() else urllib.request.urlopen(
        base + name, timeout=60
    ).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f"SHA-256 mismatch: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(target)
PY
```

面积按映射后各类单元的实例数量乘以 Liberty 中的 `area` 求和。单位依据是同版 LEF 的单元宽、高：例如 `AND2_X1` 为 `0.76 × 1.4 = 1.064 µm²`，与 Liberty 的 `1.064000` 一致。135 个库单元均有对应 LEF，134 个面积精确匹配；唯一例外是没有逻辑功能的物理填充单元 `FILLCELL_X2`，其 Liberty 面积为 `0.266`，LEF 面积为 `0.532 µm²`。ABC 逻辑映射不使用这个填充单元，锁文件保留该差异。

面积评估对全量和增量生成的完整 generic 网表使用相同的展平、库映射条件。它不代表已经实现标准单元网表的增量替换。映射时间和验证时间应与原有 generic 网表生成时间分别记录；面积只在映射完整且映射前后功能验证通过后计入比较。
