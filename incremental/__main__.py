"""Derive and validate local replacements from recorded synthesis."""
import argparse
import json
import math
import subprocess
import sys

from yosys_flow.common import ROOT, TraceError
from . import replace, automatic, sequential
from . import netlist_equivalence


def positive_seconds(value):
    seconds=float(value)
    if not math.isfinite(seconds) or seconds<=0:
        raise argparse.ArgumentTypeError('must be positive finite seconds')
    return seconds


def positive_count(value):
    count=int(value)
    if count<1:raise argparse.ArgumentTypeError('must be at least 1')
    return count


def parser(include_experiments=True):
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='command',required=True)
    verify = commands.add_parser('verify-netlists',help='独立验证两份完整组合网表；不重跑综合、不发布增量结果')
    verify.add_argument('--gold',required=True,help='参考 Yosys JSON 网表')
    verify.add_argument('--gate',required=True,help='候选 Yosys JSON 网表')
    verify.add_argument('--top',required=True)
    verify.add_argument('--timeout',type=positive_seconds,default=1200.0,
                        help='验证各阶段共享的墙钟预算，默认 1200 秒')
    verify.add_argument('--partition-timeout',type=positive_seconds,default=5.0)
    verify.add_argument('--matching-budget',type=positive_seconds,default=360.0,
                        help='内部对应证明上限；同时不超过剩余总预算的 60%%，默认 360 秒')
    verify.add_argument('--abc',help='默认使用 Yosys 同目录的 yosys-abc；none 仅使用 SAT')
    seq_base = commands.add_parser('seq-baseline',help='独立归档并证明单时钟时序旧网表（不启用时序增量）')
    seq_base.add_argument('--source',action='append',required=True)
    seq_base.add_argument('--top',required=True)
    seq_base.add_argument('--clock',required=True)
    seq_base.add_argument('--param',action='append',help='固定顶层参数 NAME=INTEGER')
    seq_base.add_argument('--reset',help='显式约定启动同步复位；省略则要求状态有已知初始化')
    seq_base.add_argument('--reset-active',type=int,choices=[0,1],default=1)
    seq_base.add_argument('--reset-cycles',type=positive_count,default=2)
    seq_base.add_argument('--synthesis-timeout',type=positive_seconds,default=600.0)
    seq_reuse = commands.add_parser('seq-reuse',help='EQY 证明完整新版 RTL 满足条件后，逐字节复用归档旧网表')
    seq_reuse.add_argument('--base-run',required=True)
    seq_reuse.add_argument('--new-source',dest='source',action='append',required=True)
    for command in (seq_base,seq_reuse):
        command.add_argument('--define',action='append',help='本版 RTL 的预处理宏 NAME[=VALUE]')
        command.add_argument('--include',action='append',help='RTL include 搜索目录')
    for command in (seq_base,seq_reuse):
        command.add_argument('--eqy',default='eqy')
        command.add_argument('--proof-timeout',type=positive_seconds,default=120.0,
                             help='全部时序证明义务共享的墙钟上限，默认 120 秒；超时不复用')
        command.add_argument('--frontend-timeout',type=positive_seconds,default=120.0)
    execute = commands.add_parser('run',help='以旧区域契约、显式新版局部 RTL 和边界绑定进行替换')
    execute.add_argument('--base-run',required=True)
    execute.add_argument('--contract',required=True)
    execute.add_argument('--bindings',required=True)
    execute.add_argument('--patch-rtl',required=True)
    execute.add_argument('--patch-top',default='patch')
    execute.add_argument('--new-source',action='append',required=True)
    auto=commands.add_parser('auto',help='从旧综合记录和完整新版 RTL 自动推导修改区域、局部代码及绑定')
    auto.add_argument('--base-run',required=True)
    auto.add_argument('--new-source',action='append',required=True)
    auto.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    auto.add_argument('--selection-mode',choices=['fine','module'],default='fine',
                      help='module 保留当前记录、契约和拼接，只禁用模块内切片，用于消融实验')
    for command in (auto,execute):
        command.add_argument('--yosys-timeout',type=positive_seconds,default=120.0,
            help='前端/局部综合/全量参考生成的单次工具上限（秒，默认 120）；独立于整网验证及预检查预算')
        command.add_argument('--whole-equivalence-timeout',type=positive_seconds,default=1200.0,
            help='每次整网验证各阶段共享的墙钟预算（秒，默认 1200）')
    auto.add_argument('--no-equivalence-precheck',action='store_true',help='关闭叶模块组合等价预检查')
    auto.add_argument('--equivalence-timeout',type=positive_seconds,default=5.0)
    auto.add_argument('--equivalence-budget',type=positive_seconds,default=15.0)
    for command in (execute,auto,seq_base,seq_reuse,verify):
        command.add_argument('--out',required=True,help='新的运行目录')
        command.add_argument('--yosys',default='yosys')
    # Preserve the historical parser API for callers/tests. Normal core CLI
    # execution below excludes experiment imports and registrations.
    if include_experiments:
        from experiments.__main__ import register
        register(commands)
    return cli


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if argv and argv[0] in {'demo','anubis','compare','anubis-nochange','anubis-sequential'}:
        # Compatibility for existing terminal commands; the implementation
        # and preferred entry point now live in the experiments package.
        from experiments.__main__ import main as experimental_main
        return experimental_main(argv)
    args=parser(include_experiments=False).parse_args(argv)
    if args.command=='verify-netlists':
        print('开始整网验证；进度与结果：'+str(args.out)+'/report.json',file=sys.stderr,flush=True)
    try:
        result={'auto':automatic.run,'run':replace.run,'seq-baseline':sequential.baseline,
                'seq-reuse':sequential.reuse,'verify-netlists':netlist_equivalence.run}[args.command](args)
    except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print('incremental: '+str(exc),file=sys.stderr)
        return 1
    if args.command=='verify-netlists':
        result={k:result[k] for k in ('status','all_top_outputs_verified','total_seconds')}
    elif args.command=='run':
        result={'status':result['status'],'checks':result['checks']}
    elif args.command=='auto':
        result={k:result[k] for k in ('status','selected_scopes','checks','failure') if k in result}
    elif args.command in {'seq-baseline','seq-reuse'}:
        result={k:result[k] for k in ('status','reused','next_action','failure') if k in result}
    result['report']=str(args.out)+('/baseline.json' if args.command=='seq-baseline' else '/report.json')
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 1 if result.get('status') in {'failed','blocked','unknown','different','invalid','invalidated'} else 0


if __name__=='__main__':
    raise SystemExit(main())
