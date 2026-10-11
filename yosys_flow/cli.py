import argparse
import json
from pathlib import Path
import subprocess
import sys

from .runner import ROOT, load_run, query, run
from .trace import TraceError


def parser():
    cli = argparse.ArgumentParser(description="Yosys JSONL 转换追踪：opt_merge 实验闭环")
    commands = cli.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run", help="运行前端、插桩合并、原版对照和等价检查")
    execute.add_argument("--source", action="append", required=True, help="RTL 源文件，可重复")
    execute.add_argument("--top", required=True)
    execute.add_argument("--out", required=True, help="新的运行目录，不覆盖已有结果")
    execute.add_argument("--yosys", default="yosys")
    execute.add_argument("--plugin", default=str(ROOT / "build/provenance.so"))
    execute.add_argument("-I", "--include", action="append", default=[])
    execute.add_argument("-D", "--define", action="append", default=[])
    execute.add_argument("--param", action="append", default=[], help="顶层参数 NAME=VALUE")
    execute.add_argument("--steps", type=int, default=1, help="重复调用 opt_merge 的次数")
    execute.add_argument("--nomux", action="store_true")
    validate = commands.add_parser("validate", help="重新检查哈希、事件引用、结构重放及网表")
    validate.add_argument("directory")
    inspect = commands.add_parser("query", help="源码正查或对象反查；JSON 输出含完整可达事件子图")
    inspect.add_argument("directory")
    group = inspect.add_mutually_exclusive_group(required=True)
    group.add_argument("--source")
    group.add_argument("--object", help="完整版本引用，如 yosys:object_17@1")
    inspect.add_argument("--line", type=int)
    inspect.add_argument("--end-line", type=int)
    inspect.add_argument("--instance", help="第一阶段为顶层模块名")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run(args)
        elif args.command == "query":
            result = query(args)
        else:
            _, _, result = load_run(args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TraceError, OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        print(f"yosys-flow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
