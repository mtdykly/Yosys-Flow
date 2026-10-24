"""Raw ANUBIS ALPHA register-file sequential proof/reuse experiment."""
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import time

from yosys_flow.common import require, write_json
from . import nochange, sequential


def run(args):
    inventory = nochange.inventory()
    source = nochange.CORPUS/'upstream/alpha/regfile.v'
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    common = dict(source=[str(source)], define=[], include=[], yosys=args.yosys, eqy=args.eqy,
                  frontend_timeout=args.frontend_timeout, proof_timeout=args.proof_timeout)
    base = out/'baseline'
    old = sequential.baseline(SimpleNamespace(**common, out=str(base), top='regfile', clock='wr_clk',
        param=[], reset=None, reset_active=1, reset_cycles=2, synthesis_timeout=600))
    rows = []
    if old['status'] == 'ready':
        for name,macro,expected in [('alpha_noc5','ANUBIS_NOC_5','equivalent'),
                                    ('alpha_local0','ANUBIS_LOCAL_0','different'),
                                    ('alpha_local2','ANUBIS_LOCAL_2','different')]:
            result = sequential.reuse(SimpleNamespace(**(common | {'define':[macro]}),
                                       base_run=str(base), out=str(out/name)))
            rows.append({'case':name, 'macro':macro, 'expected':expected, 'status':result['status'],
                         'reused':result['reused'], 'report':str(out/name/'report.json'),
                         'proof_seconds':result.get('proof', {}).get('elapsed_seconds'),
                         'total_seconds':result['elapsed_seconds'],
                         'source':'raw_upstream', 'adaptations':[]})
    require(nochange.inventory()['input_files'] == inventory['input_files'], 'ANUBIS inputs changed')
    passed = old['status'] == 'ready' and len(rows) == 3 and all(r['status'] == r['expected'] for r in rows)
    report = {'status':'passed' if passed else 'failed', 'baseline_status':old['status'],
              'repository':inventory['repository'], 'commit':inventory['commit'],
              'source_file':'alpha/regfile.v', 'source_sha256':inventory['input_files']['alpha/regfile.v'],
              'scope':'one raw initialized single-clock register file; not all sequential NOC macros',
              'cases':rows, 'counts':dict(Counter(r['status'] for r in rows)),
              'elapsed_seconds':time.monotonic()-started, 'adaptations':[]}
    write_json(out/'report.json', report)
    return report
