"""Retain ABC selection diagnostics and cost samples; synthesize once per sample."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from yosys_flow.abc_cases import cases, expectation, expected_boundary
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import TraceError, digest, require, unescape, write_json
from yosys_flow.diagnostics import diagnose_loaded, inventory
from yosys_flow.extract import extract
from yosys_flow.runner import run, load_run, query_loaded


def sample(case, directory, native, stock):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=False)
    source=directory/'input.v'; source.write_text(case['rtl'])
    out=directory/'run'
    started=time.perf_counter()
    run_result=run(flow_parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                                          '--native-yosys',native,'--yosys',stock]))
    run_seconds=time.perf_counter()-started
    require(all(run_result['checks'].values()),'run validation failed')
    timings={}; started=time.perf_counter()
    manifest,trace,validation=load_run(out,timings=timings)
    timings['load_and_validate_total']=time.perf_counter()-started
    module=json.loads((out/'after.json').read_text())['modules']['demo']
    costs=inventory(out,trace)
    max_inputs=max((r['input_count'] for r in costs['abc_regions']),default=0)
    if 'declared_input_count' in case:
        require(len(costs['abc_regions'])==1 and max_inputs==case['declared_input_count'],
                'scale fixture no longer forms the intended single ABC region')
    queries=[]
    for index,spec in enumerate(case['queries']):
        args=flow_parser().parse_args(['query',str(out),'--source',str(source),'--line',str(spec['line'])])
        started=time.perf_counter(); selected=query_loaded(args,manifest,trace); query_seconds=time.perf_counter()-started
        started=time.perf_counter(); diag=diagnose_loaded(args,manifest,trace,selected); diag_seconds=time.perf_counter()-started
        expected=expectation(case,spec,module); actual={unescape(c['name']) for c in selected['final_cells']}
        missing,extra=sorted(expected-actual),sorted(actual-expected)
        origins={s['line'] for c in selected['final_cells'] for s in c['all_origins']}
        inputs,outputs=expected_boundary(module,expected,spec)
        boundary_ok=({b['original_bit'] for b in diag['boundary'] if b['direction']=='input'}==inputs and
                     {b['original_bit'] for b in diag['boundary'] if b['direction']=='output'}==outputs)
        reverse_ok=True; started=time.perf_counter()
        for cell in selected['final_cells']:
            reverse=flow_parser().parse_args(['query',str(out),'--object',cell['ref']])
            response=query_loaded(reverse,manifest,trace)
            reverse_ok &= {s['id'] for s in response['sources']}==set(cell['origin_ids'])
            reverse_ok &= {s['line'] for s in response['sources']}==set(spec['origin_lines'])
        reverse_seconds=time.perf_counter()-started
        prefix=directory/f'line-{spec["line"]}'
        started=time.perf_counter()
        exported=extract(flow_parser().parse_args(['extract',str(out),'--source',str(source),'--line',str(spec['line']),
                                                  '--out',str(prefix.with_suffix('.v')),'--yosys',stock]))
        export_seconds=time.perf_counter()-started
        export_report=json.loads(prefix.with_suffix('.provenance.json').read_text())
        require(export_report['boundary']==diag['boundary'],'diagnostic and export boundary differ')
        require({c['ref'] for c in export_report['selected_cells']}=={c['ref'] for c in selected['final_cells']},
                'export differs from query')
        exact=not missing and not extra
        precision=Counter(c['precision'] for c in selected['final_cells'])
        origin_ok=origins==set(spec['origin_lines']) if actual else spec['rule']=='constant'
        # The scale fixtures are now precision contracts too. Wider candidates
        # remain visible in the report, and fail this milestone's acceptance.
        checks={'record_validation':validation['valid'], 'reverse_queries':reverse_ok,
                'electrical_cut':diag['electrical_cut']['selected_cell_cut_complete'],
                'recorded_group_closed':diag['recorded_group_closure']['complete_for_recorded_groups'],
                'export_validation':all(exported['validation'].values()), 'expected_cells_not_omitted':not missing}
        checks.update(exact_selection=exact,expected_boundary=boundary_ok,expected_origins=origin_ok)
        if actual: checks['no_candidate_cells']=not selected['candidate_cell_refs']
        if max_inputs>10:
            checks['symbolic_functions_verified']=all(r['internal']['function_proof']['backend']=='robdd' and
                r['internal']['function_proof']['status']=='verified' for r in costs['abc_regions'])
        query_report={'line':spec['line'],'expected_cells':sorted(expected),'selected_cells':sorted(actual),
                      'missing_cells':missing,'extra_cells':extra,'exact_cell_selection':exact,
                      'expected_boundary_matches':boundary_ok,'expected_origin_lines':spec['origin_lines'],
                      'actual_origin_lines':sorted(origins),'origin_lines_match':origin_ok,
                      'precision_counts':dict(precision),'candidate_cell_count':len(selected['candidate_cell_refs']),
                      'cut_proven_count':len(selected['cut_proven_refs']), 'joint_proven_count':len(selected['joint_cut_proven_refs']),
                      'checks':checks,'contract_passed':all(checks.values()),
                      'timings_seconds':{'query_loaded':query_seconds,'region_diagnosis':diag_seconds,
                                         'reverse_queries_loaded':reverse_seconds,'export_including_reload_and_validation':export_seconds},
                      'diagnostic':prefix.with_suffix('.diagnostic.json').name,
                      'export':prefix.with_suffix('.v').name}
        write_json(prefix.with_suffix('.diagnostic.json'),diag)
        write_json(prefix.with_suffix('.query.json'),selected)
        queries.append(query_report)
    result={'case':case['name'],'run_directory':str(out),'traced_synthesis_invocations':1,
            'stock_reference_synthesis_invocations':1, 'final_cell_count':len(module['cells']),
            'input_sha256':digest(source),'tools':manifest['tool'],'scripts':{n:digest(out/n) for n in ('traced.ys','baseline.ys')},
            'timings_seconds':{'run_total_including_validation_and_stock':run_seconds,**manifest['timings_seconds'],**timings},
            'inventory':costs,'queries':queries,'contract_passed':all(q['contract_passed'] for q in queries),
            'safe_for_incremental_reuse':False}
    write_json(directory/'sample.json',result)
    return result


def summarize(samples):
    groups=defaultdict(list)
    for s in samples:
        if 'error' not in s: groups[s['case']].append(s)
    result=[]
    for name,rows in groups.items():
        median=lambda values: statistics.median(values)
        timings={k:median([r['timings_seconds'][k] for r in rows]) for k in rows[0]['timings_seconds']}
        result.append({'case':name,'samples':len(rows),'contract_passed':all(r['contract_passed'] for r in rows),
            'abc_input_counts':[r['input_count'] for r in rows[0]['inventory']['abc_regions']],
            'native_trace_bytes_median':median([r['inventory']['native_trace_bytes'] for r in rows]),
            'abc_trace_bytes_median':median([r['inventory']['abc_trace_bytes'] for r in rows]),
            'snapshot_count_median':median([sum(a['snapshot_count'] for a in r['inventory']['abc_regions']) for r in rows]),
            'sample_max_rss_kib_median':median([r['sample_max_rss_kib'] for r in rows if r.get('sample_max_rss_kib') is not None])
                                       if any(r.get('sample_max_rss_kib') is not None for r in rows) else None,
            'timings_seconds_median':timings,
            'queries':[{'line':q['line'],'exact_in_all_samples':all(r['queries'][i]['exact_cell_selection'] for r in rows),
                        'missing_cells_count':len(q['missing_cells']),'extra_cells_count':len(q['extra_cells']),
                        'candidate_cell_count':q['candidate_cell_count'],'precision_counts':q['precision_counts'],
                        'query_loaded_seconds_median':median([r['queries'][i]['timings_seconds']['query_loaded'] for r in rows])}
                       for i,q in enumerate(rows[0]['queries'])]})
    return result


def main(argv=None):
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--out',type=Path,required=True,help='new report directory')
    cli.add_argument('--sizes',type=int,nargs='*',default=[8,10,12,16,32])
    cli.add_argument('--repeats',type=int,default=3)
    cli.add_argument('--case',action='append',help='run only named fixtures; repeatable')
    cli.add_argument('--native-yosys',default=os.environ.get('YOSYS_FLOW_NATIVE',str(ROOT/'build/native/install/bin/yosys')))
    cli.add_argument('--yosys',default='yosys')
    cli.add_argument('--worker',help=argparse.SUPPRESS)
    args=cli.parse_args(argv)
    args.out=args.out.resolve()
    if args.repeats<1 or any(n<4 for n in args.sizes): cli.error('repeats must be positive and sizes >= 4')
    fixtures=cases(args.sizes)
    if args.worker:
        sample(next(c for c in fixtures if c['name']==args.worker),args.out,args.native_yosys,args.yosys)
        return 0
    if args.case:
        if set(args.case)-{c['name'] for c in fixtures}: cli.error('unknown case; include its size with --sizes for scale cases')
        fixtures=[c for c in fixtures if c['name'] in args.case]
    require(not args.out.exists(),'report directory already exists')
    args.out.mkdir(parents=True)
    timer=Path('/usr/bin/time')
    timer_available=platform.system()=='Linux' and timer.is_file()
    report={'schema_version':1,'status':'running','scope':'single_module_combinational_default_synth',
            'timestamp_utc':datetime.now(timezone.utc).isoformat(),
            'configuration':{'repeats':args.repeats,'sizes':args.sizes,'cases':[c['name'] for c in fixtures],
                             'concurrency':1,'command':[sys.executable,*sys.argv]},
            'environment':{'platform':platform.platform(),'python':sys.version,'cpu_count':os.cpu_count(),
                           'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                           'working_tree':subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True)},
            'implementation_sha256':{str(p.relative_to(ROOT)):digest(p) for p in
                [Path(__file__).resolve(),*sorted((ROOT/'yosys_flow').glob('*.py'))]},
            'measurement_notes':{'rss':'GNU time per-sample maximum RSS including waited children; not simultaneous process-tree memory',
                                 'rss_available':timer_available,'run_total':'Includes tracing, record proof, stock comparison and equivalence; not instrumentation overhead',
                                 'query_loaded':'Validated trace reused; no Yosys invocation',
                                 'export':'Includes fresh record load/proof and export format equivalence, no full synth'},
            'samples':[],'safe_for_incremental_reuse':False}
    write_json(args.out/'report.json',report)
    for case in fixtures:
        for repeat in range(args.repeats):
            target=args.out/f'{case["name"]}-{repeat+1}'
            cmd=[sys.executable,str(Path(__file__).resolve()),'--worker',case['name'],'--out',str(target),
                 '--native-yosys',args.native_yosys,'--yosys',args.yosys,'--sizes',*map(str,args.sizes)]
            rss=args.out/(target.name+'.rss')
            if timer_available: cmd=[str(timer),'-f','%M','-o',str(rss),*cmd]
            with (args.out/(target.name+'.log')).open('w') as log:
                completed=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            if completed.returncode==0:
                result=json.loads((target/'sample.json').read_text())
                result['sample_max_rss_kib']=int(rss.read_text().strip()) if timer_available else None
            else:
                result={'case':case['name'],'error':'sample failed; see '+target.name+'.log','returncode':completed.returncode,'contract_passed':False}
            result['sample_directory']=str(target)
            report['samples'].append(result)
            write_json(args.out/'report.json',report)
            print(f'{target.name}: '+('ok' if result['contract_passed'] else 'FAILED'),flush=True)
    report['summary']=summarize(report['samples'])
    report['status']='complete' if all(s['contract_passed'] for s in report['samples']) else 'failed'
    report['note']='Completion means fixture contracts passed; inspect selection deltas and candidates separately. No replacement-safety claim.'
    write_json(args.out/'report.json',report)
    print(args.out/'report.json')
    return 0 if report['status']=='complete' else 1


if __name__=='__main__':
    try: raise SystemExit(main())
    except (TraceError,OSError,ValueError) as exc:
        print(f'benchmark-abc: {exc}',file=sys.stderr); raise SystemExit(1)
