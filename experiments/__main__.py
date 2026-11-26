"""Run benchmark surveys and performance experiments separately from the core."""
import argparse
import json
import subprocess
import sys

from yosys_flow.common import ROOT, TraceError
from incremental.__main__ import positive_seconds, positive_count
from . import anubis, demo, experiment as comparison, nochange, seq_anubis

COMMANDS = {'demo', 'anubis', 'compare', 'anubis-nochange', 'anubis-sequential'}


def register(commands):
    experiment = commands.add_parser('demo',help='四通道组合 RTL 的局部替换实验')
    experiment.add_argument('--mode',choices=['retained','flatten','both'],default='both')
    experiment.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    benchmark = commands.add_parser('anubis',help='固定 ANUBIS 宏修改的双实例组合基准与失败归因')
    benchmark.add_argument('--mode',choices=['retained','flatten','both'],default='both')
    benchmark.add_argument('--cases',nargs='+',choices=list(anubis.CASES))
    benchmark.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    benchmark.add_argument('--automatic',action='store_true',help='仅把旧记录和完整新版交给 auto，不提供目标实例或 patch')
    compare = commands.add_parser('compare',help='ANUBIS / EPFL 组合功能修改的全量与增量对比（不含独立 M）')
    compare.add_argument('--mode',choices=['retained','flatten','both'],default='retained')
    compare.add_argument('--suite',choices=['anubis-functional','epfl-functional','diagnostic'],default='anubis-functional',
                         help='ANUBIS 或 EPFL 构造的组合功能修改；diagnostic 显式启用 NOC、合成控制与状态负例')
    compare.add_argument('--cases',nargs='+',help='所选 suite 中的固定案例；默认该套件全部主案例')
    compare.add_argument('--methods',nargs='+',choices=['F','P-module','P-fine'],
                         help='必须包含 F；EPFL 默认 F P-fine，其他套件默认三种方法')
    compare.add_argument('--native-yosys',default=str(ROOT/'build/native/install/bin/yosys'))
    compare.add_argument('--repeats',type=positive_count,default=5,help='每个方法至少 1 次，默认 5；单次结果不代表稳定均值')
    compare.add_argument('--baseline-repeats',type=positive_count,default=1,
                         help='离线旧建档独立重复次数，默认 1；与在线重复按序配对，共享相同旧输入')
    compare.add_argument('--index-backend',choices=['sqlite','gzip'],default='sqlite',
                         help='旧建档和在线选区使用的索引后端，默认 sqlite')
    compare.add_argument('--precheck',choices=['on','off','both'],default='off',
                         help='主比较默认关闭等价预检查；on 的运行成本计入生成时间；both 对诊断 NOC 测开/关，其余开启；auto 默认不变')
    compare.add_argument('--seed',type=int,default=20261024)
    compare.add_argument('--cpu',type=int,help='串行绑定一个允许的 CPU；默认取当前 affinity 的最小编号')
    compare.add_argument('--job-timeout',type=positive_seconds,default=600.0)
    compare.add_argument('--archive-byte-limit',type=positive_count,
                         help='可选：每个测量进程输出目录的逻辑字节上限；按秒采样，超限保留部分证据并报告 resource_limit')
    compare.add_argument('--fallback-full',action='store_true',help='失败后实际执行 F，另报尝试加回退成本')
    compare.add_argument('--liberty',help='可选 Liberty 库：对成功的最终 generic 网表统一映射并估算单元面积；映射和验证时间另列')
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
    survey.add_argument('--upstream-top',action='store_true',
                        help='按上游 Yosys 文件清单、真实顶层及默认参数验证；局部反例不冒充顶层反例')
    survey.add_argument('--eqy',default='eqy')
    survey.add_argument('--synthesis-timeout',type=positive_seconds,default=600.0,
                        help='扩展评测中旧版基准综合的保护上限；不综合新版')
    seq_bench = commands.add_parser('anubis-sequential',help='ANUBIS 时序等价与整份旧网表复用实验')
    seq_bench.add_argument('--eqy',default='eqy')
    seq_bench.add_argument('--proof-timeout',type=positive_seconds,default=120.0)
    seq_bench.add_argument('--frontend-timeout',type=positive_seconds,default=120.0)
    compare.add_argument('--equivalence-timeout',type=positive_seconds,default=5.0)
    compare.add_argument('--equivalence-budget',type=positive_seconds,default=15.0)
    benchmark.add_argument('--no-equivalence-precheck',action='store_true',help=argparse.SUPPRESS)
    benchmark.add_argument('--equivalence-timeout',type=positive_seconds,default=5.0,help=argparse.SUPPRESS)
    benchmark.add_argument('--equivalence-budget',type=positive_seconds,default=15.0,help=argparse.SUPPRESS)
    for command in (experiment,benchmark,survey,seq_bench,compare):
        command.add_argument('--out',required=True,help='新的实验结果目录')
        command.add_argument('--yosys',default='yosys')


def parser():
    cli=argparse.ArgumentParser(description=__doc__)
    register(cli.add_subparsers(dest='command',required=True))
    return cli


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result={'demo':demo.run,'anubis':anubis.run,'compare':comparison.run,
                'anubis-nochange':nochange.run,'anubis-sequential':seq_anubis.run}[args.command](args)
    except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print('experiments: '+str(exc),file=sys.stderr)
        return 1
    if args.command=='anubis':
        result={'status':result['status'],'counts':result['counts'],
                'cases':[{k:r[k] for k in ('case','mode','status')} for r in result['cases']]}
    elif args.command=='anubis-nochange':
        result={k:result[k] for k in ('status','unique_macros_in_inventory','counts','budget_observations')}
    elif args.command=='anubis-sequential':
        result={k:result[k] for k in ('status','baseline_status','counts')}
    elif args.command=='compare':
        result={k:result[k] for k in ('status','counts','methods','total_seconds')}
    result['report']=str(args.out)+'/report.json'
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 1 if result.get('status') in {'failed','blocked','unknown','different','invalid','invalidated'} else 0


if __name__=='__main__':
    raise SystemExit(main())
