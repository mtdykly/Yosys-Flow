import argparse
import json
import subprocess
import sys

from .common import ROOT, TraceError
from .runner import load_run, query, run
from .extract import extract
from .diagnostics import diagnose
from . import contracts


def parser():
    cli = argparse.ArgumentParser(description="组合 RTL 的 Yosys 默认 synth 来源追踪与子网表导出")
    # 添加子命令
    commands = cli.add_subparsers(dest="command", required=True)
    evidence = commands.add_parser('prepare-evidence', help='仅根据旧建档准备可复用函数证明库，不指定修改位置')
    evidence.add_argument('directory')
    evidence.add_argument('--out', help='新目录，默认 <directory>.proofs')
    evidence.add_argument('--cec-timeout', type=float, default=60)
    evidence.add_argument('--structural-only', action='store_true', help='仅持久化结构恒等关系，不运行检查点证明')
    evidence.add_argument('--refine-outputs', action='store_true', help='离线尝试细化所有旧输出的缺失边界，可能耗时较长')
    evidence.add_argument('--resume',action='store_true',help='从同一 --out 的已认证检查点继续；不接受旧版未签名临时材料')
    evidence.add_argument('--enhance-budget',type=float,default=3600,help='本次增强计算墙钟预算；默认 3600 秒，0 不限；基础准备另计')
    evidence.add_argument('--batch-outputs',type=int,default=8,help='每批旧输出数量，完成后提交证据，默认 8')
    evidence.add_argument('--cache-engines',type=int,default=1,help='内存保留的桥接计算状态数量，默认 1，其余使用已认证磁盘状态')
    evidence.add_argument('--no-boundary-scan',action='store_true',help='跳过通用相邻检查点局部函数扫描（默认启用）')
    evidence.add_argument('--boundary-budget',type=float,default=600,help='通用边界扫描预算秒，默认 600，0 不限；预算停止仍保存已证明关系')
    evidence.add_argument('--source-timeout',type=float,default=600,help='离线准备旧 RTL 组合表示的工具上限，默认 600 秒')
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
    execute.add_argument("--flatten", action="store_true", help="显式执行 synth -flatten；默认保留层次")
    execute.add_argument("--index-backend", choices=("sqlite", "gzip"), default="sqlite",
                         help="验证后生成的来源索引：默认 SQLite 按需读取；gzip 使用相同去重数据，整包加载到内存")
    development = execute.add_argument_group("开发检查点")
    development.add_argument("--flow", choices=("synth", "m1", "m1-opt"), default="synth",
                             help="默认 synth；m1 为 proc 检查点，m1-opt 为基础优化检查点；共用原生记录器，来源覆盖仍为 partial")
    resume = commands.add_parser("resume", help="复用完整捕获，在新目录完成重放、证明和索引；不重跑插桩综合")
    resume.add_argument("directory", help="原捕获目录，保持不变")
    resume.add_argument("--out", required=True, help="新的续建档目录")
    resume.add_argument("--yosys", help="原版验证工具；默认沿用原 manifest")
    resume.add_argument("--native-yosys", help="仅核对捕获工具身份；须与原二进制哈希一致")
    resume.add_argument("--index-backend", choices=("sqlite", "gzip"), help="默认沿用原索引后端")
    resume.add_argument("--skip-stock-equivalence", action="store_true",
                        help="显式跳过原版与插桩网表的 equiv_simple；保留结构一致性、ABC 证据和索引校验，并记录未执行项")
    rebuild=commands.add_parser('reindex',help='复用已完成的旧归档，重放并重建索引；不调用新旧综合')
    rebuild.add_argument('directory',help='已完成的原始归档，不接受按目标细化的派生归档')
    rebuild.add_argument('--out',required=True,help='新归档目录，原目录保持不变')
    rebuild.add_argument('--index-backend',choices=('sqlite','gzip'))
    for command in (execute,resume,rebuild):
        command.add_argument('--prepare-evidence',action='store_true',help='复用本轮重放和已验证设计，建索引后发布基础函数证据')
        command.add_argument('--evidence-out',help='独立的新证据目录，默认 <out>.proofs；须配合 --prepare-evidence')
        command.add_argument('--evidence-cec-timeout',type=float,default=60)
        command.add_argument('--source-timeout',type=float,default=600)
    validate = commands.add_parser("validate", help="重新检查哈希、事件引用、结构重放及网表")
    validate.add_argument("directory")
    verify_contract = commands.add_parser('contract-validate', help='对照同一次来源记录重新验证契约、全部输出和义务')
    verify_contract.add_argument('directory')
    verify_contract.add_argument('contract')
    merge_contract = commands.add_parser('contract-merge', help='合并同一基准的区域契约，重算边界和全部输出')
    merge_contract.add_argument('directory')
    merge_contract.add_argument('contracts', nargs='+')
    merge_contract.add_argument('--out', required=True, help='新的契约 .json 文件')
    for name, help_text in [("query", "源码正查或对象反查；JSON 输出含完整可达事件子图"),
                            ("extract", "导出来源关联的 cell 子网表及边界、来源说明"),
                            ("diagnose", "只读诊断已记录区域的证明、边界、共享和门组闭合；不重新综合"),
                            ("contract", "从查询生成区域契约，显式补齐已记录门组并验证全部输出")]:
        inspect = commands.add_parser(name, help=help_text)
        inspect.add_argument("directory")
        group = inspect.add_mutually_exclusive_group(required=True) # 创建“互斥参数组”
        group.add_argument("--source")
        group.add_argument("--object", help="原生对象引用，如 native:cell_17@1 或 native:connection_18")
        if name == 'extract':
            group.add_argument('--contract', help='验证并导出该契约的完整区域，支持合并后的契约')
        inspect.add_argument("--line", type=int)
        inspect.add_argument("--end-line", type=int)
        inspect.add_argument("--column", type=int, help="1 起始列号；与行号共同限定源码位置")
        inspect.add_argument("--end-column", type=int, help="包含在查询内的末列，默认同起始列")
        inspect.add_argument("--instance", help="实例路径，如 demo/u0/u_leaf；默认查询全部实例")
        if name == 'contract':
            inspect.add_argument('--out', required=True, help='新的契约 .json 文件')
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
        elif args.command == 'prepare-evidence':
            from .prepare_evidence import prepare
            result = prepare(args)
        elif args.command == "resume":
            from .capture import resume
            result = resume(args)
        elif args.command == 'reindex':
            from .reindex import reindex
            result=reindex(args)
        elif args.command == "query":
            result = query(args)
        elif args.command == "extract":
            result = extract(args)
        elif args.command == "diagnose":
            result = diagnose(args)
        elif args.command == 'contract':
            result = contracts.generate(args)
        elif args.command == 'contract-validate':
            result = contracts.validate(args)
        elif args.command == 'contract-merge':
            result = contracts.merge(args)
        else:
            _, _, result = load_run(args.directory, use_index=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TraceError, OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        print(f"yosys-flow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
