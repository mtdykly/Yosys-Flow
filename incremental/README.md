# 使用区域契约的局部替换实验

本目录验证已有来源记录能否用于一次真实的局部修改：旧 RTL 只做一次插桩全量综合，之后依靠旧记录选择区域；新版局部 RTL 独立综合后接回旧网表。新版完整综合仅作为验证参考，不用于寻找或复制新门。

## 四通道例子

[base.v](examples/four_lanes/base.v) 包含 `chip → lane → mix` 三层模块，四条独立 8 位通道共用 `lane`/`mix` 定义。每条通道先计算 `pre = data ^ key`，核心输出为 `(pre & mask) ^ salt`，另外保留 `side = pre & salt`；顶层还有汇总四条核心输出的 `summary`。

[new.v](examples/four_lanes/new.v) 仅将实例 `chip/u1/core` 的 AND 改成 OR。为只修改一个实例，新版显式提供 `lane_edited` 和 `mix_edited`。其余实例、`side` 和汇总逻辑保持原 RTL 描述。[patch.v](examples/four_lanes/patch.v) 是显式给定的新版局部功能，而非自动从 `new.v` 提取。

```bash
# 在仓库根目录执行；结果目录必须尚不存在
python3 -m incremental demo --out runs/incremental-demo

# 也可单独验证一种模式
python3 -m incremental demo --mode retained --out runs/incremental-retained
python3 -m incremental demo --mode flatten --out runs/incremental-flat
```

两种模式的旧电路均为 152 个门。契约并集选中 `mix` 第 3、4 行在 `chip/u1/core` 中的 16 个门，边界有 24 个输入、8 个输出。每个输出还服务于区域外的汇总门，契约必须保留这部分义务。

1. `base-run/` 保存原有插桩综合及独立原版验收产物。
2. `line-3.contract.json`、`line-4.contract.json` 和 `region.contract.json` 由旧记录生成，不执行新综合。
3. `boundary-proof.json` 验证显式边界功能：`a = data[15:8] ^ key[15:8]`、`b = mask[15:8]`、`c = salt[15:8]`。用旧网表的实际边界位计算函数并核对极性，原线名消失时也不猜测绑定。
4. `replacement/work/local-synth.ys` 只读取 `patch.v`，执行默认 `synth -top patch -flatten`。
5. 替换器仅删除所选旧门，复制新局部门。输出用显式缓冲门连接旧信号；有反相输入时添加极性适配门。所有未选中门的类型、参数、属性及连接必须逐项不变。
6. 独立边界函数检查覆盖每个新输出；单驱动、无环及全部外部消费者检查必须通过。
7. 此后才综合完整 `new.v`，构建全输出 SAT miter。检查所有 72 个输出位，包括未修改通道、共享上游逻辑的 `side` 和区域外的 `summary`；没有按内部同名信号假设等价。
8. Verilog 重读等价和基准归档复核通过后，才发布 `replacement/incremental.json`、`incremental.v` 和成功的 `report.json`。

两种模式当前均复用 136 个门，局部综合产生 16 个门，加 8 个边界输出缓冲门后共 160 个门。此实验不在拼接后运行全局优化，因此不声称门数最优或已有性能收益。报告分别记录局部综合、参考全量综合和整体验证耗时。

保留层次模式以已独立验证的电气实例视图进行实例级替换，**交付网表为展平的通用门网表**。原始基准层次网表不变；尚未实现通过克隆模块定义来保持拼接结果的层次外形。显式展平模式直接采用其最终电气网表。两种路径均不会将一个模块定义的所有实例一起修改。

## 独立运行替换器

```bash
python3 -m incremental run \
  --base-run runs/incremental-demo/retained/base-run \
  --contract runs/incremental-demo/retained/region.contract.json \
  --bindings runs/incremental-demo/retained/bindings.json \
  --patch-rtl incremental/examples/four_lanes/patch.v --patch-top patch \
  --new-source incremental/examples/four_lanes/new.v \
  --out runs/another-replacement
```

`bindings.json` 绑定该契约的内容 ID，按 Yosys JSON 的从低位到高位顺序列出每个局部端口对应的契约端口。输入项为 `{"port":"in_...","inverted":false}`，也接受无反相的字符串简写；输出项为 `"out_..."`。所有输入和输出恰好绑定一次。CLI 不根据新版综合网表搜索绑定；例子的自动填表仅使用显式功能要求和旧网表，其他修改需要给出自己的局部功能与绑定。

独立运行始终重新验证契约，要求 `verified_old_region`、基准输入未变化、替换输出由所选门驱动，以及新的局部端口完全位于旧边界内。局部输入增加而边界未扩展会拒绝。失败日志和 `status=failed` 报告保留在新的实验目录，但不发布 `incremental.v/json`。原运行和已有结果不覆盖。

## 已验证与未完成

测试同时包含成功路径和以下反例：错误位序、错误输入极性、遗漏输出、新增外部依赖、非凸区域产生组合环，以及共享旧门的两个消费者中只允许一个改变功能。后者需要拆分消费者或扩大/重建区域，目前由全部输出验证拒绝，未实现自动修复。

局部拼接函数验证只证明新门正确实现了**给定的局部规范与绑定**。错误绑定仍可能通过这层检查，所以必须再检查与完整新 RTL 的全部输出等价。具体版本对的验证成功，不改变旧契约中的 `safe_for_incremental_reuse=false`，也不把 ABC 局部功能证明升级为完整因果历史。

本例没有遇到需要新增 ABC 内部钩子的证据断点；补充的是优化后边界功能/极性绑定和拼接验收。尚未完成自动 RTL 差分、任意新局部 RTL 的生成、消费者拆分、层次形态保持、PPA 优化、时序逻辑和替换后的连续来源归档。新门映射保存在实验报告，旧记录不会冒充新设计的完整来源日志。
