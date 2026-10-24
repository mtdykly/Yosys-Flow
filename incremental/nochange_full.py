"""Raw ANUBIS NOC survey including sequential design contexts.

The containing design is the proof boundary, not a claim about reachability at
the original CPU top. Only the old design is synthesized. Sequential results
exercise seq-baseline/seq-reuse, with exactly their existing guards and proof
obligations. Unsupported designs remain visible in the denominator.
"""
from collections import Counter
import csv
import difflib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import time
from types import SimpleNamespace

from yosys_flow.common import TraceError, digest, require, write_json
from . import equivalence, nochange, rtlil, sequential


def contexts(catalog):
    path = nochange.CORPUS/'nochange-contexts.json'
    data = json.loads(path.read_text())
    expected = {c['id'] for c in catalog['cases'] if c['execution'] != 'leaf'}
    require(set(data['contexts']) == expected, 'contexts must cover every formerly excluded NOC macro')
    for context in data['contexts'].values():
        require(context['profiles'], 'empty context profiles')
        require(context['reset_cycles'] >= 1 and context['reset_active'] in (0, 1), 'invalid reset policy')
    return data, digest(path)


def source_files(case, catalog):
    # Headers are included by the raw source. Do not parse testbenches or C
    # headers, or replace undefined values/configurations to make proofs pass.
    prefix = case['design']+('/rtl/' if case['design'] in {'fpu','mor1kx','or1200'} else '/')
    return sorted(n for n in catalog['input_files'] if n.startswith(prefix)
                  and n.endswith('.v') and re.search(r'^\s*module\s+\w+',
                      (nochange.CORPUS/'upstream'/n).read_text(), re.M))


def common_args(args, case, context, profile, catalog, inputs):
    sources = [str(inputs/n) for n in source_files(case, catalog)]
    require(sources, 'no containing design sources')
    return SimpleNamespace(source=sources, define=profile['defines'],
        include=sorted({str(Path(p).parent) for p in sources}), yosys=args.yosys, eqy=args.eqy,
        frontend_timeout=args.frontend_timeout, proof_timeout=args.case_timeout,
        synthesis_timeout=args.synthesis_timeout, top=context['top'], clock=context['clock'],
        reset=context['reset'], reset_active=context['reset_active'], reset_cycles=context['reset_cycles'],
        param=[f'{k}={v}' for k,v in profile['parameters'].items()])


def prepare(row, args, case, context, profile, catalog, inputs, tools):
    directory = Path(row['directory'])
    directory.mkdir(parents=True)
    common = common_args(args, case, context, profile, catalog, inputs)
    config = sequential.configuration(common)
    row.update(config=config, sources=common.source, defines=profile['defines'],
               proof_boundary=context['top'], adaptations=[], frontend_seconds=0.0)
    lowered, modules, texts, scopes = [], [], [], []
    for side in ('old', 'new'):
        local = SimpleNamespace(**(vars(common) | {'define':common.define+([case['macro']] if side == 'new' else [])}))
        target = directory/side
        begin = time.monotonic()
        rejected = None
        try:
            sequential.frontend(local, config, target, tools['yosys']['path'], begin+args.frontend_timeout)
        except (TraceError, subprocess.SubprocessError) as exc:
            rejected = str(exc)
        finally:
            row['frontend_seconds'] += time.monotonic()-begin
        if not (target/'coarse.json').is_file():
            logs = sorted(target.glob('*.log'))
            row.update(status='unknown', reason='frontend_rejected', detail=rejected,
                       diagnostic_logs=[str(p) for p in logs])
            return
        design = json.loads((target/'coarse.json').read_text())['modules']
        require(set(design) == {config['top']}, 'unresolved hierarchy in survey')
        module = design[config['top']]
        lowered.append(module)
        modules.append(rtlil.parse((target/'coarse.il').read_text())[config['top']])
        texts.append((target/'snapshot.v').read_text())
        try:
            equivalence.guard_coarse(module, modules[-1].ports())
            scopes.append(('combinational', None))
        except TraceError:
            scopes.append(('sequential' if rejected is None else 'unsupported', rejected))
    require(sequential.interface(lowered[0]) == sequential.interface(lowered[1]), 'NOC changed interface')
    pp_equal = nochange.verilog_tokens(texts[0]) == nochange.verilog_tokens(texts[1])
    ir_equal = modules[0].canonical({modules[0].name:modules[0]}) == modules[1].canonical({modules[1].name:modules[1]})
    row.update(preprocessed_tokens_equal=pp_equal, frontend_ir_equal=ir_equal,
               edit_effect='preprocessor_tokens_same' if pp_equal else
                           'elaboration_normalized_or_inactive' if ir_equal else 'changed_frontend_IR',
               scope_checks=dict(zip(('old','new'), scopes)),
               coarse_cells=[len(m['cells']) for m in lowered])
    (directory/'preprocessed.diff').write_text(''.join(difflib.unified_diff(
        texts[0].splitlines(True), texts[1].splitlines(True), fromfile='old', tofile='new')))
    if any(s[0] == 'unsupported' for s in scopes):
        row.update(status='out_of_scope', reason='sequential_scope_rejected',
                   detail='; '.join(f'{side}: {scope[1]}' for side,scope in zip(('old','new'), scopes) if scope[1]))
    elif scopes[0][0] != scopes[1][0]:
        row.update(status='out_of_scope', reason='state_structure_scope_changed')
    else:
        row.update(status='prepared', backend='EQY' if scopes[0][0] == 'sequential' else 'SAT',
                   execution='sequential' if scopes[0][0] == 'sequential' else 'flattened_comb_diagnostic')


def times(samples, key):
    values = [s[key] for s in samples if s.get(key) is not None]
    if not values:
        return None
    return {'first':values[0], 'min':min(values), 'median':statistics.median(values),
            'max':max(values), 'samples':len(values)}


def prove_prepared(row, args, case, context, profile, catalog, inputs, tools):
    directory = Path(row['directory'])
    if row['backend'] == 'SAT':
        if row['frontend_ir_equal']:
            row.update(status='structural_same', reason='same_frontend_IR_no_SAT_needed')
            return
        modules = [rtlil.parse((directory/side/'coarse.il').read_text())[context['top']]
                   for side in ('old','new')]
        for i in range(args.repeats):
            sample = equivalence.prove(*modules, tools['yosys']['path'], directory/f'proof-{i}',
                                       time.perf_counter()+args.case_timeout)
            row['samples'].append(sample)
            row.update(status=sample['status'], reason=sample['reason'])
            if sample['status'] != 'equivalent':
                break
    else:
        common = common_args(args, case, context, profile, catalog, inputs)
        base = directory/'baseline'
        result = sequential.baseline(SimpleNamespace(**vars(common), out=str(base)))
        row['baseline'] = {'status':result['status'], 'elapsed_seconds':result['elapsed_seconds'],
                           'proof_seconds':result.get('proof', {}).get('elapsed_seconds'),
                           'failure':result.get('failure'), 'report':str(base/'baseline.json')}
        if result['status'] != 'ready':
            row.update(status='unknown', reason='old_baseline_not_proven',
                       detail=result.get('failure', result.get('proof', {}).get('reason')))
            return
        for i in range(args.repeats):
            output = directory/f'reuse-{i}'
            result = sequential.reuse(SimpleNamespace(**(vars(common) | {'define':common.define+[case['macro']]}),
                base_run=str(base), out=str(output)))
            proof = result.get('proof', {})
            sample = {'status':result['status'], 'reason':proof.get('reason', result.get('failure')),
                      'elapsed_seconds':proof.get('elapsed_seconds'), 'total_seconds':result['elapsed_seconds'],
                      'frontend_seconds':result.get('frontend', {}).get('elapsed_seconds'),
                      'reused':result['reused'], 'report':str(output/'report.json'),
                      'new_full_synthesis_used':result['new_full_synthesis_used']}
            row['samples'].append(sample)
            row.update(status=result['status'], reason=sample['reason'])
            if result['status'] != 'equivalent':
                break
    if len({s['status'] for s in row['samples']}) > 1:
        row.update(status='unknown', reason='inconsistent_repetitions')
    row['proof_seconds'] = times(row['samples'], 'elapsed_seconds')
    row['reuse_total_seconds'] = times(row['samples'], 'total_seconds')


def budget_observations(rows):
    result = {}
    for backend in ('SAT', 'EQY'):
        positive = [r for r in rows if r.get('backend') == backend and r['status'] == 'equivalent'
                    and r.get('edit_effect') == 'changed_frontend_IR' and r.get('proof_seconds')]
        result[backend] = {'eligible_profiles':[f"{r['case']}/profile-{r['profile']}" for r in positive],
            'eligible_count':len(positive),
            'observed_max_seconds':max((r['proof_seconds']['max'] for r in positive), default=None),
            'coverage':[{'seconds':t, 'covered':sum(r['proof_seconds']['max'] <= t for r in positive),
                         'eligible':len(positive)} for t in (.25,1,2,5,15,30,60,120,180,300,600,1200)]}
    return {'backends':result, 'default_values_changed':False,
        'positive_population':'raw containing designs, changed frontend IR, successful equality and definedness',
        'timing_metric':'maximum proof wall time across serial repetitions; sequential old baseline excluded',
        'shared_total_budget_calibrated':False,
        'limitations':['Independent single-design proofs do not measure multiple changed modules sharing a budget.',
                      'SAT and sequential EQY populations cannot share a calibrated latency distribution.',
                      'Unchanged/normalized edits, counterexamples, unknowns and scope rejections are not positive samples.',
                      'Small non-random corpus; not a general P95/P99 or incremental speedup measurement.']}


def publish(out, report):
    rows = report['profiles']
    report['counts'] = dict(Counter(r['status'] for r in rows))
    report['cases_accounted_for'] = len({r['case'] for r in rows})
    report['case_outcomes'] = {c:sorted({r['status'] for r in rows if r['case'] == c})
                               for c in sorted({r['case'] for r in rows})}
    report['budget_observations'] = budget_observations(rows)
    write_json(out/'report.json', report)
    columns = ['case','profile','parameters','defines','backend','status','reason','edit_effect',
               'frontend_seconds','baseline_seconds','proof_first','proof_median','proof_max','samples',
               'reuse_total_max','experiment_seconds','directory']
    with (out/'timings.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            values = {k:row.get(k) for k in columns}
            for k in ('parameters','defines'):
                values[k] = json.dumps(row.get(k), sort_keys=True)
            proof = row.get('proof_seconds') or {}
            values.update(baseline_seconds=row.get('baseline', {}).get('elapsed_seconds'),
                          proof_first=proof.get('first'), proof_median=proof.get('median'),
                          proof_max=proof.get('max'), samples=len(row.get('samples', [])),
                          reuse_total_max=(row.get('reuse_total_seconds') or {}).get('max'))
            writer.writerow(values)


def run(args):
    require(all(math.isfinite(t) and t > 0 for t in
                (args.case_timeout,args.frontend_timeout,args.synthesis_timeout)), 'invalid survey budget')
    require(args.repeats >= 1, 'at least one repetition required')
    catalog = nochange.inventory()
    context_manifest, context_hash = contexts(catalog)
    selected = set(args.cases or [c['id'] for c in catalog['cases']])
    require(selected <= {c['id'] for c in catalog['cases']}, 'unknown nochange case ID')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_json(out/'inventory.json', catalog)
    write_json(out/'contexts.json', context_manifest)
    report = {'schema_version':2, 'status':'running', 'commit':catalog['commit'],
        'unique_macros_in_inventory':catalog['unique_macro_count'], 'selected_cases':sorted(selected),
        'profiles':[], 'counting_unit':'parameter/feature profiles; multiple profiles can belong to one NOC macro',
        'per_proof_budget_seconds':args.case_timeout, 'frontend_budget_seconds':args.frontend_timeout,
        'shared_proof_budget_seconds':None, 'repetitions_for_successes':args.repeats,
        'measurement_order':'serial; all design contexts checked before proofs',
        'context_manifest_sha256':context_hash, 'adaptations':[],
        'scope':'containing designs; all binary top inputs arbitrary under recorded startup/clock conditions',
        'original_design_reachability_checked':False,
        'product_scope_warning':'flattened combinational diagnostics do not expand auto leaf eligibility; no sequential incremental replacement',
        'host':{'platform':platform.platform(), 'python':platform.python_version(), 'logical_cpus':os.cpu_count()},
        'implementation_hashes':{str(p.relative_to(nochange.CORPUS.parents[1])):digest(p) for p in
            (Path(__file__),Path(nochange.__file__),Path(equivalence.__file__),Path(sequential.__file__),Path(rtlil.__file__))}}
    pending = []
    for case in catalog['cases']:
        if case['id'] not in selected:
            continue
        context = context_manifest['contexts'].get(case['id'])
        profiles = context['profiles'] if context else [{'parameters':p,'defines':[]} for p in case['parameters']]
        for i, profile in enumerate(profiles):
            row = {'case':case['id'], 'profile':i, **profile, 'source':'raw_upstream',
                   'status':'pending', 'execution':case['execution'], 'samples':[],
                   'directory':str(out/case['id']/f'profile-{i}')}
            report['profiles'].append(row)
            pending.append((row,case,context,profile))
    if args.inventory_only:
        report['status'] = 'inventory_only'
        publish(out, report)
        return report
    tools = sequential.toolchain(args)
    report['tools'] = tools
    inputs = out/'inputs'
    for name in catalog['input_files']:
        path = inputs/name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(nochange.CORPUS/'upstream'/name, path)
    started = time.monotonic()
    publish(out, report)
    # First evaluate all formerly excluded contexts, including original async
    # reset designs. Guard failures are evidence, not fabricated proof passes.
    for row,case,context,profile in pending:
        if context is None:
            continue
        begin = time.monotonic()
        try:
            prepare(row,args,case,context,profile,catalog,inputs,tools)
        except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            row.update(status='failed', reason='experiment_error', detail=str(exc))
        row['experiment_seconds'] = time.monotonic()-begin
        write_json(Path(row['directory'])/'report.json', row)
        publish(out, report)
        print('scope', row['case'], row['profile'], row['status'], row.get('detail',''), flush=True)
    for row,case,context,profile in pending:
        begin = time.monotonic()
        try:
            if context is None:
                measured = nochange.measure(case,profile['parameters'],row['profile'],args,
                                           tools['yosys']['path'],inputs,out)
                row.update(measured, backend='SAT')
            elif row['status'] == 'prepared':
                prove_prepared(row,args,case,context,profile,catalog,inputs,tools)
                row['experiment_seconds'] += time.monotonic()-begin
            else:
                continue
        except (TraceError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            row.update(status='failed', reason='experiment_error', detail=str(exc))
        write_json(Path(row['directory'])/'report.json', row)
        publish(out, report)
        print('proof', row['case'], row['profile'], row['status'], row.get('reason',''), flush=True)
    report['inputs_unchanged'] = all(digest(inputs/n) == sha for n,sha in catalog['input_files'].items())
    report['tools_unchanged'] = all(digest(t['path']) == t['sha256'] for t in tools.values())
    report['implementation_unchanged'] = all(digest(nochange.CORPUS.parents[1]/p) == sha
                                            for p,sha in report['implementation_hashes'].items())
    report['contexts_unchanged'] = digest(nochange.CORPUS/'nochange-contexts.json') == context_hash
    intact = all(report[k] for k in ('inputs_unchanged','tools_unchanged','implementation_unchanged','contexts_unchanged'))
    report['elapsed_seconds'] = time.monotonic()-started
    report['status'] = 'completed_with_findings' if intact and not any(r['status'] in
        {'failed','pending','prepared'} for r in report['profiles']) else 'failed'
    publish(out, report)
    return report
