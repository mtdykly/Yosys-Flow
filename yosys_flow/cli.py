import argparse
import json
import subprocess
import sys

from .common import ROOT, TraceError
from .runner import load_run, query, run
from .extract import extract


def parser():
    cli = argparse.ArgumentParser(description="组合 RTL 的 Yosys 默认 synth 来源追踪与子网表导出")
    # 添加子命令
    commands = cli.add_subparsers(dest="command", required=True)
    # 定义 run 命令及其基本参数
    execute = commands.add_parser("run", help="仅组合 RTL：默认 synth、来源记录、原版对照和等价检查")
    execute.add_argument("--source", action="append", required=True, help="RTL 源文件，可重复")
    execute.add_argument("--top", required=True)
    execute.add_argument("--out", required=True, help="新的运行目录，不覆盖已有结果")
    execute.add_argument("--yosys", default="yosys", help="用于独立对照和等价验证的原版 Yosys")
    execute.add_argument("--native-yosys", default=str(ROOT / "build/native/install/bin/yosys"), help="用于记录来源的原生插桩版 Yosys")
    execute.add_argument("-I", "--include", action="append", default=[])
    execute.add_argument("-D", "--define", action="append", default=[])
    execute.add_argument("--param", action="append", default=[], help="顶层参数 NAME=VALUE")
    development = execute.add_argument_group("开发检查点")
    development.add_argument("--flow", choices=("synth", "m1", "m1-opt"), default="synth",
                             help="默认 synth；m1 为 proc 检查点，m1-opt 为基础优化检查点；共用原生记录器，来源覆盖仍为 partial")
    validate = commands.add_parser("validate", help="重新检查哈希、事件引用、结构重放及网表")
    validate.add_argument("directory")
    for name, help_text in [("query", "源码正查或对象反查；JSON 输出含完整可达事件子图"),
                            ("extract", "导出来源关联的 cell 子网表及边界、来源说明")]:
        inspect = commands.add_parser(name, help=help_text)
        inspect.add_argument("directory")
        group = inspect.add_mutually_exclusive_group(required=True) # 创建“互斥参数组”
        group.add_argument("--source")
        group.add_argument("--object", help="原生对象引用，如 native:cell_17@1 或 native:connection_18")
        inspect.add_argument("--line", type=int)
        inspect.add_argument("--end-line", type=int)
        inspect.add_argument("--column", type=int, help="1 起始列号；与行号共同限定源码位置")
        inspect.add_argument("--end-column", type=int, help="包含在查询内的末列，默认同起始列")
        inspect.add_argument("--instance", help="当前仅支持顶层模块名")
        if name == "extract":
            inspect.add_argument("--out", required=True, help="新建 .v 文件，附带 .il/.json/.provenance.json")
            inspect.add_argument("--module-name", default="selected", help="导出子模块名称")
            inspect.add_argument("--yosys", default="yosys")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run(args)
        elif args.command == "query":
            result = query(args)
        elif args.command == "extract":
            result = extract(args)
        else:
            _, _, result = load_run(args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TraceError, OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        print(f"yosys-flow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
