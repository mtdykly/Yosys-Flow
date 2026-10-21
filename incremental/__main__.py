"""Derive and validate local replacements from recorded synthesis."""
import argparse
import json
import subprocess
import sys

from yosys_flow.common import ROOT, TraceError
from . import demo, replace, anubis, automatic


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
    for command in (experiment,execute,benchmark,auto):
        command.add_argument('--out',required=True,help='新的实验结果目录')
        command.add_argument('--yosys',default='yosys')
    return cli


def main():
    args = parser().parse_args()
    try:
        result = {'demo':demo.run,'anubis':anubis.run,'auto':automatic.run,'run':replace.run}[args.command](args)
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
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 1 if result.get('status') in {'failed','blocked'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
