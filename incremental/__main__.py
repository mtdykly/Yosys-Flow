"""Derive and validate local replacements from recorded synthesis."""
import argparse
import json
import math
import subprocess
import sys

from yosys_flow.common import ROOT, TraceError
from . import demo, replace, anubis, automatic, nochange, sequential, seq_anubis


def positive_seconds(value):
    seconds=float(value)
    if not math.isfinite(seconds) or seconds<=0:
        raise argparse.ArgumentTypeError('must be positive finite seconds')
    return seconds


def positive_count(value):
    count=int(value)
    if count<1:raise argparse.ArgumentTypeError('must be at least 1')
    return count


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='command',required=True)
    experiment = commands.add_parser('demo',help='四通道组合 RTL 的局部替换实验')
    experiment.add_argument('--mode',choices=['retained','flatten','both'],default='both')
    experiment.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    benchmark = commands.add_parser('anubis',help='固定 ANUBIS 宏修改的双实例组合基准与失败归因')
    benchmark.add_argument('--mode',choices=['retained','flatten','both'],default='both')
    benchmark.add_argument('--cases',nargs='+',choices=list(anubis.CASES))
    benchmark.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    benchmark.add_argument('--automatic',action='store_true',help='仅把旧记录和完整新版交给 auto，不提供目标实例或 patch')
    survey = commands.add_parser('anubis-nochange',help='盘点全部 ANUBIS NOC 宏，单独测量组合模块等价证明')
    survey.add_argument('--cases',nargs='+',help='清单 ID，例如 dlx_noc1 fpu_noc3；默认全部 30 个宏')
    survey.add_argument('--inventory-only',action='store_true',help='只校验并输出完整变体清单')
    survey.add_argument('--case-timeout',type=positive_seconds,default=600.0,
                        help='每次证明的独立墙钟保护上限，默认 600 秒；无共享预算')
    survey.add_argument('--frontend-timeout',type=positive_seconds,default=120.0,
                        help='每版 RTL 前端处理保护上限，默认 120 秒')
    survey.add_argument('--repeats',type=positive_count,default=3,help='成功证明的独立重复次数，默认 3')
    survey.add_argument('--with-sequential',action='store_true',
                        help='展开全部 NOC 的设计上下文，组合用 SAT，支持的时序用 EQY 和旧网表复用')
    survey.add_argument('--eqy',default='eqy')
    survey.add_argument('--synthesis-timeout',type=positive_seconds,default=600.0,
                        help='扩展评测中旧版基准综合的保护上限；不综合新版')
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
    seq_bench = commands.add_parser('anubis-sequential',help='原始 ANUBIS ALPHA 寄存器文件的时序等价、反例和整网表复用实验')
    for command in (seq_base,seq_reuse):
        command.add_argument('--define',action='append',help='本版 RTL 的预处理宏 NAME[=VALUE]')
        command.add_argument('--include',action='append',help='RTL include 搜索目录')
    for command in (seq_base,seq_reuse,seq_bench):
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
    for command in (auto,benchmark):
        command.add_argument('--no-equivalence-precheck',action='store_true',
            help='关闭叶模块组合等价预检查，直接走原来的局部提取流程')
        command.add_argument('--equivalence-timeout',type=positive_seconds,default=5.0,
            help='每个等价证明的墙钟预算（秒，包含表示转换，默认 5）')
        command.add_argument('--equivalence-budget',type=positive_seconds,default=15.0,
            help='本次自动流程全部等价预检查的共享墙钟预算（秒，默认 15）')
    for command in (experiment,execute,benchmark,auto,survey,seq_base,seq_reuse,seq_bench):
        command.add_argument('--out',required=True,help='新的实验结果目录')
        command.add_argument('--yosys',default='yosys')
    return cli


def main():
    args = parser().parse_args()
    try:
        result = {'demo':demo.run,'anubis':anubis.run,'auto':automatic.run,'run':replace.run,
                  'anubis-nochange':nochange.run,'seq-baseline':sequential.baseline,
                  'seq-reuse':sequential.reuse,'anubis-sequential':seq_anubis.run}[args.command](args)
    except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print('incremental: '+str(exc),file=sys.stderr)
        return 1
    if args.command=='run':
        result = {'status':result['status'],'checks':result['checks'],'report':str(args.out)+'/report.json'}
    elif args.command=='anubis':
        result = {'status':result['status'],'counts':result['counts'],
                  'cases':[{k:r[k] for k in ('case','mode','status')} for r in result['cases']],
                  'report':str(args.out)+'/report.json'}
    elif args.command=='auto':
        result={k:result[k] for k in ('status','selected_scopes','checks','failure') if k in result}|{'report':str(args.out)+'/report.json'}
    elif args.command=='anubis-nochange':
        result={k:result[k] for k in ('status','unique_macros_in_inventory','counts','budget_observations')}
        result['report']=str(args.out)+'/report.json'
    elif args.command in {'seq-baseline','seq-reuse'}:
        result={k:result[k] for k in ('status','reused','next_action','failure') if k in result}
        result['report']=str(args.out)+('/baseline.json' if args.command=='seq-baseline' else '/report.json')
    elif args.command=='anubis-sequential':
        result={k:result[k] for k in ('status','baseline_status','counts')}
        result['report']=str(args.out)+'/report.json'
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 1 if result.get('status') in {'failed','blocked','unknown','different'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
