# 原生构建与组合过程开发检查点

主流程是完整默认 `synth`，仅支持组合 RTL。`--flow m1` 执行 `proc -noopt; opt_merge`；`--flow m1-opt` 执行 `proc; opt_expr; opt_clean; opt_merge; opt_clean`。检查点用于隔离来源丢失位置，不能替代最终网表验收。

## 构建

保留原版固定版本 Yosys（提交 `0e82bbefe`）用于独立对照。原生版安装在仓库的 `build/native/install`，不覆盖系统安装。默认 `make` 与 `make native` 都构建这套原生版。

构建需要 Python 3.10+、C++20 编译器、CMake ≥ 3.28、Ninja、Bison ≥ 3.6、Flex ≥ 2.6、m4、zlib 开发文件，以及独立原版 Yosys 和其 ABC。Slang、Tcl、readline/libedit 和 libffi 在本构建中关闭。构建脚本同时构建固定插桩 ABC（`provenance/native/abc/lock.json`），默认 synth 调用它；两个开发检查点不调用 ABC。

```bash
make native NATIVE_JOBS=4
make test-native
make test
```

`provenance/tools/build_native.py` 按 `provenance/native/lock.json` 下载完整提交固定的 Yosys 源码和必要子模块，先验证归档 SHA256，再应用固定锚点插桩；锚点或哈希不匹配即失败。`build/native/prepared.json` 保存插桩输入摘要和修改文件摘要。通过 `#line` 保持上游位置，避免 `NEW_ID` 中的 C++ 行号因插桩改变。已有未修改的源码文件保持时间戳，支持增量编译。

系统 CMake 较旧时可本地安装：

```bash
python3 -m pip install --target build/native/python cmake==3.31.6
```

脚本优先使用这个本地 CMake，也可直接调用 `python3 provenance/tools/build_native.py --cmake /path/to/cmake`。`--abc` 只用于指定已有的兼容内部插桩 ABC，普通 ABC 缺少内部日志会被拒绝。Ubuntu 环境缺少生成器且不希望安装到系统时，可用 `apt-get download bison flex m4 libsigsegv2` 下载本机发行版的包，然后以 `dpkg-deb -x <包> build/native/toolchain` 解压。脚本识别这里的工具和运行库；这些宿主构建工具不属于固定的 Yosys/子模块源码锁。

## 检查点用法

```bash
python3 -m yosys_flow run --flow m1 \
  --source provenance/examples/comb.v --top demo --out runs/comb-checkpoint
python3 -m yosys_flow query runs/comb-checkpoint --source provenance/examples/comb.v --line 4
python3 -m yosys_flow extract runs/comb-checkpoint \
  --source provenance/examples/comb.v --line 4 --out runs/comb-checkpoint/selected.v
```

此时第 4 行可选择 AND 分支运算和其生成的 mux，并排除另一分支的 OR。完整 synth 后结果会经过 ABC，可能是同一输出锥内的候选集合，不能沿用检查点的对象预期。直连/常量分别位于第 8、9 行，也可独立导出。

`--native-yosys` 指定插桩版，`--yosys` 指定独立原版；两者不能为同一文件。源码坐标固定在本次运行版本。保留原生 JSONL v2，旧插件 v1 与时序来源支持已退役。

前端语句/表达式、组合过程动作、条件和临时路由的钩子继续保留；状态输入在前端或状态单元生成时明确拒绝。详细范围见 [native-synth.md](native-synth.md)，动作证据见 [process-provenance.md](process-provenance.md)。
