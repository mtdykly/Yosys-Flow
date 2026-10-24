"""Audit all pinned ANUBIS NOC macros and measure the existing leaf SAT backend.

This is a proof experiment, not an incremental-netlist delivery command. Raw
source semantics are preserved, including X outputs. Stateful containing modules
are listed with evidence, never silently cut or converted to combinational RTL.
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

from yosys_flow.common import PIN, TraceError, digest, require, token, write_json
from . import anubis, equivalence, rtlil

CORPUS = anubis.CORPUS
MACRO = re.compile(r'^\s*`(?:ifdef|ifndef)\s+(ANUBIS_NOC_(\d+))\b')


def inventory(corpus=CORPUS):
    """Re-scan every vendored macro location and check against the full scan."""
    lock = json.loads((corpus/'nochange-lock.json').read_text())
    cases = json.loads((corpus/'nochange-cases.json').read_text())['cases']
    occurrences = {}
    for name, expected in lock['files'].items():
        path = corpus/'upstream'/name
        require(path.resolve().is_relative_to((corpus/'upstream').resolve()), 'invalid corpus path')
        require(digest(path) == expected, 'ANUBIS nochange checksum mismatch: '+name)
        if path.suffix not in {'.v', '.sv', '.vh', '.h'}:
            continue
        require(lock['source_inventory'].get(name) == expected, 'source scan hash mismatch: '+name)
        for line, text in enumerate(path.read_text().splitlines(), 1):
            match = MACRO.match(text)
            if match:
                key = name.split('/')[0]+'_noc'+match[2]
                occurrences.setdefault(key, []).append({'file':name, 'line':line, 'macro':match[1]})
    require(occurrences == lock['macro_occurrences'], 'nochange macro inventory differs from pinned scan')
    require(len(cases) == len({c['id'] for c in cases}) and {c['id'] for c in cases} == set(occurrences),
            'nochange catalog must account for every macro exactly once')
    rows = []
    for case in cases:
        require(case['execution'] in {'leaf', 'out_of_scope'}, 'invalid experiment scope')
        found = occurrences[case['id']]
        require(all(o['file'] == case['file'] and o['macro'] == case['macro'] for o in found),
                'catalog macro/file mismatch')
        evidence = [{'file':case['file'], 'line':i, 'text':line.strip()}
                    for i, line in enumerate((corpus/'upstream'/case['file']).read_text().splitlines(), 1)
                    if re.search(r'\b(posedge|negedge|initial)\b', line) and not line.lstrip().startswith('//')]
        rows.append({**case, 'occurrences':found, 'state_syntax_evidence':evidence})
    config = (corpus/'upstream/scripts/Makefile.config').read_text()
    targets = (corpus/'upstream/scripts/Makefile.targets').read_text()
    require('seq 0 ${NOC_$1}' in targets, 'upstream target enumeration changed')
    script_targets = {design.lower()+'_noc'+str(i)
                      for design, maximum in re.findall(r'^NOC_(\w+)=(\d+)$', config, re.M)
                      for i in range(int(maximum)+1)}
    return {'schema_version':1, 'repository':lock['repository'], 'commit':lock['commit'],
            'archive_sha256':lock['archive_sha256'], 'unique_macro_count':len(rows),
            'counts_by_design':dict(Counter(c['design'] for c in rows)), 'cases':rows,
            'upstream_script_targets_without_macro':sorted(script_targets-set(occurrences)),
            'macros_not_enumerated_by_script':sorted(set(occurrences)-script_targets),
            'input_files':lock['files'],
            'catalog_sha256':digest(corpus/'nochange-cases.json'),
            'lock_sha256':digest(corpus/'nochange-lock.json')}


def verilog_tokens(text):
    # Compare preprocessing effects only; never use this as a semantic proof.
    items = re.findall(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|\s+|.', text, re.S)
    return [s for s in items if not s.isspace() and not s.startswith(('//', '/*'))]


def frontend(case, parameters, binary, inputs, directory, timeout):
    modules = []
    coarse = []
    elapsed = 0.0
    preprocessed = []
    for side in ('old', 'new'):
        source = inputs/case['file']
        # Yosys resolves quoted includes relative to the source file. Quoting
        # an entire -I option would make its command parser treat it as a file.
        command = 'read_verilog -sv -defer -ppdump'
        if side == 'new':
            command += ' -D'+case['macro']
        command += ' '+token(source)
        options = ''.join(f' -chparam {k} {v}' for k, v in parameters.items())
        command += '\nhierarchy -check -top '+case['top']+options
        # Keep pre-proc IR for the exact same proof backend as auto mode.
        command += '\nwrite_rtlil '+side+'.il\nproc\nopt_expr\nopt_clean\nwrite_json '+side+'.json\ncheck -assert'
        started = time.perf_counter()
        log, reason = equivalence.run_phase(binary, directory, side+'-frontend', command, started+timeout)
        elapsed += time.perf_counter()-started
        if reason:
            path = directory/(side+'-frontend.log')
            detail = path.read_text()[-2000:] if path.is_file() else 'frontend budget exhausted before tool launch'
            return None, {'status':'unknown', 'reason':'frontend_'+reason, 'frontend_seconds':elapsed,
                          'detail':detail}
        dumps = re.findall(r'-- Verilog code after preprocessor --\n(.*?)-- END OF DUMP --', log, re.S)
        require(dumps, 'Yosys did not emit the requested preprocessor evidence')
        text = '\n'.join(dumps)
        (directory/(side+'.preprocessed.v')).write_text(text)
        preprocessed.append(text)
        parsed = rtlil.parse((directory/(side+'.il')).read_text())
        module = parsed[case['top']]
        if module.children(parsed):
            return None, {'status':'out_of_scope', 'reason':'non_leaf_module', 'frontend_seconds':elapsed}
        lowered = json.loads((directory/(side+'.json')).read_text())['modules'][case['top']]
        try:
            equivalence.guard_coarse(lowered, module.ports())
        except TraceError as exc:
            return None, {'status':'out_of_scope', 'reason':'coarse_scope_rejected',
                          'detail':str(exc), 'frontend_seconds':elapsed}
        modules.append(module)
        coarse.append(lowered)
    require(modules[0].ports() == modules[1].ports(), 'NOC edit changed module interface')
    require(any(p['direction'] == 'output' for p in modules[0].ports().values()), 'no outputs to compare')
    require(all(p['direction'] in {'input','output'} for p in modules[0].ports().values()), 'inout interface')
    (directory/'preprocessed.diff').write_text(''.join(difflib.unified_diff(
        preprocessed[0].splitlines(True), preprocessed[1].splitlines(True), fromfile='old', tofile='new')))
    old_ir, new_ir = (m.canonical({m.name:m}) for m in modules)
    pp_equal = verilog_tokens(preprocessed[0]) == verilog_tokens(preprocessed[1])
    return modules, {'frontend_seconds':elapsed, 'preprocessed_tokens_equal':pp_equal,
        'frontend_ir_equal':old_ir == new_ir,
        'edit_effect':'preprocessor_tokens_same' if pp_equal else
                      'elaboration_normalized_or_inactive' if old_ir == new_ir else 'changed_frontend_IR',
        'ports':modules[0].ports(), 'coarse_cells':[len(m['cells']) for m in coarse]}


def measure(case, parameters, index, args, binary, inputs, root):
    directory = root/case['id']/('profile-'+str(index))
    directory.mkdir(parents=True)
    row = {'case':case['id'], 'profile':index, 'parameters':parameters, 'source':'raw_upstream',
           'execution':case['execution'], 'scope_note':case['scope_note'], 'directory':str(directory),
           'product_leaf_eligibility_claimed':False, 'samples':[], 'status':'unknown'}
    started = time.perf_counter()
    try:
        modules, info = frontend(case, parameters, binary, inputs, directory, args.frontend_timeout)
        row.update(info)
        if modules is None:
            return row
        if info['frontend_ir_equal']:
            row.update(status='structural_same', reason='same_frontend_IR_no_SAT_needed')
            return row
        for repeat in range(args.repeats):
            # No shared 15-second budget, and no cached proof across samples.
            proof = equivalence.prove(*modules, binary, directory/('proof-'+str(repeat)),
                                      time.perf_counter()+args.case_timeout)
            row['samples'].append(proof)
            row.update(status=proof['status'], reason=proof['reason'])
            if proof['status'] != 'equivalent':
                break
        if len({s['status'] for s in row['samples']}) > 1:
            row.update(status='unknown', reason='inconsistent_repetitions')
        times = [s['elapsed_seconds'] for s in row['samples']]
        row['proof_seconds'] = {'first':times[0], 'min':min(times), 'median':statistics.median(times),
                                'max':max(times), 'samples':len(times)}
        row['phase_medians'] = {key:statistics.median(s['timings'][key] for s in row['samples'] if key in s['timings'])
                               for key in {k for s in row['samples'] for k in s['timings']}}
    except (TraceError, OSError, ValueError, KeyError, TypeError) as exc:
        row.update(status='failed', reason='experiment_error', detail=str(exc))
    finally:
        row['experiment_seconds'] = time.perf_counter()-started
        row['artifacts'] = {p.relative_to(directory).as_posix():digest(p)
                            for p in directory.rglob('*') if p.is_file() and p != directory/'report.json'}
        write_json(directory/'report.json', row)
    return row


def budget_observations(rows):
    # Exclude inactive/normalized edits, adaptations, unknowns and
    # counterexamples from the positive timing set.
    positive = [r for r in rows if r['status'] == 'equivalent' and r['execution'] == 'leaf'
                and r.get('edit_effect') == 'changed_frontend_IR' and r['source'] == 'raw_upstream']
    return {'population':'raw leaf pairs with changed frontend IR, all-output equality AND definedness proved',
        'eligible_profiles':[r['case']+'/profile-'+str(r['profile']) for r in positive],
        'eligible_count':len(positive), 'timing_metric':'maximum proof wall time across repetitions',
        'coverage':[{'seconds':limit, 'covered':sum(r['proof_seconds']['max'] <= limit for r in positive),
                     'eligible':len(positive)} for limit in (.05,.1,.25,.5,1,2,5,10,30,60,600)],
        'observed_max_seconds':max((r['proof_seconds']['max'] for r in positive), default=None),
        'default_values_changed':False,
        'decision':'retain 5s/15s pending representative negative/unknown and multiple-changed-module workloads',
        'limitations':['small non-random sample; not a universal percentile or speedup claim',
                      'source parsing measured separately; proof includes proc/miter/import/SAT, not SAT alone',
                      'shared total budget cannot be calibrated from independent single-module proofs',
                      'no new full synthesis or netlist reuse experiment in this command']}


def publish(out, report):
    report['counts'] = dict(Counter(r['status'] for r in report['profiles']))
    report['counting_unit'] = 'parameter profiles plus one entry per excluded macro; not unique macros'
    by_case = {}
    for row in report['profiles']:
        by_case.setdefault(row['case'], set()).add(row['status'])
    report['case_outcomes'] = {name:sorted(states) for name, states in by_case.items()}
    report['cases_accounted_for'] = len(by_case)
    report['budget_observations'] = budget_observations(report['profiles'])
    write_json(out/'report.json', report)
    columns = ['case','profile','parameters','status','reason','edit_effect','execution',
               'frontend_seconds','proof_first','proof_median','proof_max','samples','experiment_seconds','directory']
    with (out/'timings.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in report['profiles']:
            result = {k:row.get(k) for k in columns}
            result['parameters'] = json.dumps(row.get('parameters', {}), sort_keys=True)
            times = row.get('proof_seconds', {})
            result.update(proof_first=times.get('first'), proof_median=times.get('median'),
                          proof_max=times.get('max'), samples=times.get('samples', 0))
            writer.writerow(result)


def run(args):
    if getattr(args, 'upstream_top', False):
        require(not getattr(args, 'with_sequential', False),
                '--upstream-top and --with-sequential select different benchmark boundaries')
        from . import nochange_top
        return nochange_top.run(args)
    if getattr(args, 'with_sequential', False):
        from . import nochange_full
        return nochange_full.run(args)
    require(math.isfinite(args.case_timeout) and args.case_timeout > 0, 'invalid per-proof timeout')
    require(math.isfinite(args.frontend_timeout) and args.frontend_timeout > 0, 'invalid frontend timeout')
    require(args.repeats >= 1, 'at least one repetition is required')
    catalog = inventory()
    selected = set(args.cases or [c['id'] for c in catalog['cases']])
    require(selected <= {c['id'] for c in catalog['cases']}, 'unknown nochange case ID')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_json(out/'inventory.json', catalog)
    report = {'schema_version':1, 'status':'running', 'inventory':str(out/'inventory.json'),
        'commit':catalog['commit'], 'unique_macros_in_inventory':catalog['unique_macro_count'],
        'selected_cases':sorted(selected), 'profiles':[], 'per_proof_budget_seconds':args.case_timeout,
        'frontend_budget_seconds':args.frontend_timeout, 'shared_proof_budget_seconds':None,
        'repetitions_for_successes':args.repeats, 'measurement_order':'serial catalog order; first and subsequent samples retained',
        'proof_backend':'incremental.equivalence.prove / Yosys miter + sat',
        'scope':'containing combinational modules, all ports unconstrained binary; not complete CPU/FPU designs',
        'original_design_reachability_checked':False,
        'boundary_warning':'a leaf counterexample does not establish a difference observable at the original design top',
        'product_scope_warning':'stock frontend diagnostics do not establish native provenance/automatic-reuse support',
        'adaptations':[], 'host':{'platform':platform.platform(), 'python':platform.python_version(),
            'logical_cpus':os.cpu_count()},
        'implementation_hashes':{str(p.relative_to(CORPUS.parents[1])):digest(p) for p in
            (Path(__file__).resolve(), Path(equivalence.__file__).resolve(), Path(rtlil.__file__).resolve())}}
    if args.inventory_only:
        report['status'] = 'inventory_only'
        publish(out, report)
        return report
    binary = shutil.which(args.yosys)
    require(binary, 'Yosys is required')
    version = subprocess.check_output([binary, '-V'], text=True).strip()
    require(PIN in version, 'nochange experiment requires the pinned Yosys version')
    report['yosys'] = {'path':binary, 'version':version, 'sha256':digest(binary)}
    # Immutable input copy, with no X/default changes or branch extraction.
    inputs = out/'inputs'
    for name in catalog['input_files']:
        destination = inputs/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS/'upstream'/name, destination)
    started = time.perf_counter()
    for case in catalog['cases']:
        if case['id'] not in selected:
            continue
        if case['execution'] == 'out_of_scope':
            report['profiles'].append({'case':case['id'], 'status':'out_of_scope', 'execution':case['execution'],
                'reason':case['scope_note'], 'source':'raw_upstream', 'samples':[],
                'occurrences':case['occurrences'], 'state_syntax_evidence':case['state_syntax_evidence']})
        else:
            for index, parameters in enumerate(case['parameters']):
                row = measure(case, parameters, index, args, binary, inputs, out)
                report['profiles'].append(row)
                publish(out, report)
                print(case['id'], parameters, row['status'], row.get('reason', ''), flush=True)
        publish(out, report)
    integrity = all(digest(inputs/name) == expected for name, expected in catalog['input_files'].items())
    binary_intact = digest(binary) == report['yosys']['sha256']
    if not integrity or not binary_intact:
        report.update(status='failed', inputs_unchanged=integrity,
                      failure='nochange inputs changed during experiment' if not integrity else 'Yosys executable changed')
        publish(out, report)
        return report
    report['inputs_unchanged'] = True
    report['elapsed_seconds'] = time.perf_counter()-started
    report['status'] = 'failed' if any(r['status'] == 'failed' for r in report['profiles']) else 'completed_with_findings'
    publish(out, report)
    return report
