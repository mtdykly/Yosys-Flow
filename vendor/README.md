# 固定的上游代码

`opt_merge.cc` 来自 YosysHQ/yosys，提交前缀 `0e82bbefe`，路径 `passes/opt/opt_merge.cc`：

https://github.com/YosysHQ/yosys/blob/0e82bbefe/passes/opt/opt_merge.cc

下载的文件未修改，版权和 ISC 许可保留在文件头。`opt_merge.sha256` 保存精确字节摘要。`tools/instrument.py` 校验后生成 `build/trace_opt_merge.cc`：只改命令注册名和帮助、增加统一记录器头文件，以及在 pass 开始/结束、真实合并前后插入调用。

升级 Yosys 时需重新核对实现和所有钩子，更新固定版本与摘要，重新运行测试；不能只替换提交字符串后宣称支持。
