"""Run explicit local-synthesis/replacement experiments."""
import argparse
import json
import subprocess
import sys

from yosys_flow.common import ROOT, TraceError
from . import demo, replace


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='command',required=True)
    experiment = commands.add_parser('demo',help='四通道组合 RTL 的局部替换实验')
    experiment.add_argument('--mode',choices=['retained','flatten','both'],default='both')
    experiment.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    execute = commands.add_parser('run',help='以旧区域契约、显式新版局部 RTL 和边界绑定进行替换')
    execute.add_argument('--base-run',required=True)
    execute.add_argument('--contract',required=True)
    execute.add_argument('--bindings',required=True)
    execute.add_argument('--patch-rtl',required=True)
    execute.add_argument('--patch-top',default='patch')
    execute.add_argument('--new-source',action='append',required=True)
    for command in (experiment,execute):
        command.add_argument('--out',required=True,help='新的实验结果目录')
        command.add_argument('--yosys',default='yosys')
    return cli


def main():
    args = parser().parse_args()
    try:
        result = demo.run(args) if args.command=='demo' else replace.run(args)
    except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print('incremental: '+str(exc),file=sys.stderr)
        return 1
    if args.command=='run':
        result = {'status':result['status'],'checks':result['checks'],'report':str(args.out)+'/report.json'}
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
