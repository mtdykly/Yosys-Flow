"""NOC diagnostics at the pinned upstream Yosys top and configuration.

Structural identity includes the entire active hierarchy and real specialized
module types. A local SAT counterexample never determines the top-level verdict.
This survey does not deliver a reused netlist or relax production proof guards.
"""
from collections import Counter
import csv
import json
from pathlib import Path
import re
import shutil
import time

from yosys_flow.common import TraceError, digest, require, token, write_json
from . import equivalence, nochange, rtlil, sequential


def configurations(catalog, inputs):
    config = (inputs/'scripts/Makefile.config').read_text()
    makefile = (inputs/'scripts/yosys/Makefile').read_text()
    require('DEFS_' not in makefile, 'upstream Yosys define selection changed')
    result = {}
    for design in catalog['counts_by_design']:
        top = re.search(r'^TOP_'+design.upper()+r'=(\w+)$', config, re.M)
        clock = re.search(r'^'+design.upper()+r'_CLK=(\w+)$', config, re.M)
        require(top and clock, 'missing upstream top/clock')
        name = f'scripts/yosys/{design}_files.tcl'
        commands, sources = [], []
        for line in (inputs/name).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('read_verilog '):
                parts = line.split()
                require(parts[1:-1] in ([], ['-sv']), 'unsupported upstream reader flags')
                require(parts[-1].startswith('../../'), 'unexpected upstream source path')
                source = parts[-1][6:]
                require(source in catalog['input_files'], 'unlocked upstream source: '+source)
                sources.append(source)
                commands.append(' '.join(parts[:-1])+' '+token(inputs/source))
            else:
                match = re.fullmatch(r'verilog_defaults -add -I\.\./\.\./([\w/.-]+)', line)
                require(match, 'unsupported upstream file-list command: '+line)
                commands.append('verilog_defaults -add -I'+str(inputs/match[1]))
        result[design] = {'top':top[1], 'clock':clock[1], 'parameters':{}, 'defines':[],
            'file_list':name, 'file_list_sha256':digest(inputs/name), 'sources':sources,
            'reader_commands':commands,
            'configuration_rule':'upstream scripts/yosys file order and top; default RTL parameters; no additional feature defines',
            'clock_note':'clock name is metadata until sequential scope is validated; no startup reset assumed'}
    return result


def canonical(module):
    # Keep child cell TYPES, parameters and port bindings. In particular, never
    # use canonical(all_modules), which replaces distinct child types by a
    # generic marker. Normalize generated memory identifiers AND their MEMID
    # references, without dropping initialization or memory contents.
    generated = {}
    for line in module.lines:
        words = rtlil.tokens(line)
        if words and words[0] in {'wire','cell','process','memory'} and words[-1].startswith('$'):
            generated.setdefault(words[-1], '@generated_'+str(len(generated)))
    rows = []
    for line in [*module.attributes, *module.lines]:
        words = rtlil.tokens(line)
        if not words or words[0].startswith('#'):
            continue
        if words[0] == 'attribute' and words[1] in rtlil.IGNORED:
            continue
        if words[0] == 'module':
            words = ['module','@self']
        if words[:2] == ['parameter','\\MEMID'] and words[-1].startswith('"'):
            value = json.loads(words[-1])
            if value in generated:
                words[-1] = json.dumps(generated[value])
        rows.append([generated.get(w, w) for w in words])
    return rows


def differences(old, new):
    return sorted(n for n in set(old) | set(new)
                  if n not in old or n not in new or canonical(old[n]) != canonical(new[n]))


def elaborate(config, macro, binary, directory, timeout):
    directory.mkdir(parents=True)
    script = 'verilog_defaults -add -defer'+(' -D'+macro+'=1' if macro else '')+'\n'
    script += '\n'.join(config['reader_commands'])
    script += '\nhierarchy -check -top '+config['top']+'\nwrite_rtlil frontend.il\n'
    sequential.yosys(binary, directory, 'elaborate', script, time.monotonic()+timeout)
    return rtlil.parse((directory/'frontend.il').read_text())


def normalize(binary, directory, timeout):
    # No full synth/ABC, no added input assumptions and no setundef. Memory
    # collection preserves the effective INIT while resolving source ordering.
    script = ('read_rtlil frontend.il\nproc\nopt_expr\nmemory_collect\n'
              'opt_clean -purge\nrename -hide w:* c:*\nwrite_rtlil normalized.il\n')
    sequential.yosys(binary, directory, 'normalize', script, time.monotonic()+timeout)
    return rtlil.parse((directory/'normalized.il').read_text())


def normalize_context(binary, directory, timeout):
    # Parent connections can render a changed child output unobservable. Keep
    # actual clocks, state, memory INIT and all top ports; this does not cut the
    # state or replace memories by independent inputs.
    script = ('read_rtlil frontend.il\nproc\nflatten\nopt_expr\nmemory_collect\n'
              'opt_clean -purge\nrename -hide w:* c:*\nwrite_rtlil context.il\n')
    sequential.yosys(binary, directory, 'context', script, time.monotonic()+timeout)
    return rtlil.parse((directory/'context.il').read_text())


def lower(binary, source, top, directory, timeout):
    directory.mkdir(parents=True)
    script = ('read_rtlil '+token(source)+'\nhierarchy -check -top '+top+
              '\nproc\nflatten\nmemory_map\nopt_expr\nopt_clean\n'
              'check -assert\nwrite_rtlil coarse.il\nwrite_json coarse.json\n')
    sequential.yosys(binary, directory, 'lower', script, time.monotonic()+timeout)
    design = json.loads((directory/'coarse.json').read_text())['modules']
    require(set(design) == {top}, 'unresolved top-level proof hierarchy')
    return rtlil.parse((directory/'coarse.il').read_text())[top], design[top]


def local_checks(old, new, names, binary, directory, args):
    checks = []
    for i, name in enumerate(names):
        row = {'module':name, 'proof_scope':'isolated module at actual elaborated parameters',
               'top_verdict_implied':False}
        checks.append(row)
        if name not in old or name not in new:
            row.update(status='unknown', reason='module_inventory_changed')
            continue
        if old[name].children(old) or new[name].children(new):
            row.update(status='unknown', reason='nonleaf_requires_parent_context')
            continue
        result = equivalence.prove(old[name], new[name], binary, directory/f'local-{i}',
                                   time.perf_counter()+args.case_timeout)
        row.update(status=result['status'], reason=result['reason'], evidence=result)
    return checks


def top_check(config, old_directory, new_directory, directory, tools, args):
    """Only this complete boundary can produce a design-level counterexample."""
    directory.mkdir()
    row = {'status':'unknown', 'proof_boundary':config['top'], 'samples':[],
           'observations':'all top-level outputs', 'local_counterexample_promoted':False}
    started = time.monotonic()
    try:
        pairs = [lower(tools['yosys']['path'], source/'frontend.il', config['top'],
                       directory/side, args.frontend_timeout)
                 for source,side in ((old_directory,'old'),(new_directory,'new'))]
        require(pairs[0][0].ports() == pairs[1][0].ports(), 'top interface changed')
        try:
            for module, design in pairs:
                equivalence.guard_coarse(design, module.ports())
            backend = 'SAT'
        except TraceError:
            backend = 'EQY'
        row['backend'] = backend
        if backend == 'EQY':
            proof_config = {'top':config['top'], 'clock':config['clock'], 'parameters':{},
                'reset':None, 'reset_active':1, 'reset_cycles':2}
            for _, design in pairs:
                sequential.guard(design, proof_config)
        for i in range(args.repeats):
            if backend == 'SAT':
                result = equivalence.prove(pairs[0][0], pairs[1][0], tools['yosys']['path'],
                    directory/f'proof-{i}', time.perf_counter()+args.case_timeout)
            else:
                # RTL-to-RTL diagnostic, not archived-netlist certification.
                # Reuse the unchanged sequential guards and proof obligations.
                result = sequential.prove(directory/'new/coarse.il', directory/'old/coarse.il',
                    proof_config, sequential.interface(pairs[0][1]), tools,
                    directory/f'proof-{i}', args.case_timeout)
            row['samples'].append(result)
            row.update(status=result['status'], reason=result['reason'])
            if result['status'] != 'equivalent':
                break
        if row['status'] == 'different':
            row['counterexample_scope'] = 'complete_upstream_top'
            row['counterexample_proof_directory'] = str(directory/f'proof-{len(row["samples"])-1}')
    except (TraceError, OSError, ValueError, KeyError) as exc:
        row.update(status='unknown', reason='top_frontend_or_proof_scope_rejected', detail=str(exc))
    row['elapsed_seconds'] = time.monotonic()-started
    write_json(directory/'report.json', row)
    return row


def evaluate(case, config, old, new, old_directory, directory, tools, args):
    row = {'case':case['id'], 'macro':case['macro'], 'top':config['top'], 'parameters':{},
           'defines':[case['macro']], 'status':'unknown', 'local_checks':[],
           'reuse_authorized':False, 'output_definedness_proven':False}
    changed = differences(old, new)
    row.update(changed_modules=changed, active_module_count=len(new))
    if not changed:
        row.update(status='structural_same', reason='same_elaborated_active_hierarchy')
        return row
    if not (old_directory/'normalized.il').exists():
        normalize(tools['yosys']['path'], old_directory, args.frontend_timeout)
    a = rtlil.parse((old_directory/'normalized.il').read_text())
    b = normalize(tools['yosys']['path'], directory, args.frontend_timeout)
    row['normalized_changed_modules'] = differences(a, b)
    if not row['normalized_changed_modules']:
        row.update(status='structural_same', reason='same_normalized_active_hierarchy')
        return row
    # Positive local checks are useful evidence; no blanket upgrade of binary
    # leaf equality across an unproved (possibly undefined) sequential context.
    row['local_checks'] = local_checks(old, new, changed, tools['yosys']['path'], directory, args)
    if not (old_directory/'context.il').exists():
        normalize_context(tools['yosys']['path'], old_directory, args.frontend_timeout)
    old_context = rtlil.parse((old_directory/'context.il').read_text())
    new_context = normalize_context(tools['yosys']['path'], directory, args.frontend_timeout)
    row['context_changed_modules'] = differences(old_context, new_context)
    if not row['context_changed_modules']:
        row.update(status='structural_same', reason='same_normalized_complete_top_context')
        return row
    row['top_proof'] = top_check(config, old_directory, directory, directory/'top-proof', tools, args)
    row.update(status=row['top_proof']['status'], reason=row['top_proof']['reason'])
    if row['status'] == 'equivalent':
        row['output_definedness_proven'] = True
    return row


def publish(out, report):
    report['counts'] = dict(Counter(r['status'] for r in report['profiles']))
    report['cases_accounted_for'] = len(report['profiles'])
    report['top_counterexamples'] = [r['case'] for r in report['profiles'] if r['status'] == 'different']
    write_json(out/'report.json', report)
    with (out/'timings.csv').open('w', newline='') as stream:
        fields = ['case','top','status','reason','frontend_seconds','elapsed_seconds','changed_modules']
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['profiles'])


def run(args):
    catalog = nochange.inventory()
    selected = set(args.cases or [c['id'] for c in catalog['cases']])
    require(selected <= {c['id'] for c in catalog['cases']}, 'unknown nochange case ID')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    inputs = out/'inputs'
    for name in catalog['input_files']:
        target = inputs/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(nochange.CORPUS/'upstream'/name, target)
    configs = configurations(catalog, inputs)
    write_json(out/'inventory.json', catalog)
    write_json(out/'configurations.json', configs)
    sources = [Path(__file__), Path(nochange.__file__), Path(equivalence.__file__),
               Path(sequential.__file__), Path(rtlil.__file__)]
    hashes = {str(p.resolve()):digest(p) for p in sources}
    report = {'schema_version':3, 'status':'running', 'scope':'upstream_yosys_top',
        'commit':catalog['commit'], 'unique_macros_in_inventory':catalog['unique_macro_count'],
        'selected_cases':sorted(selected), 'counting_unit':'one upstream configuration per NOC macro',
        'per_proof_budget_seconds':args.case_timeout, 'shared_proof_budget_seconds':None,
        'repetitions_for_successes':args.repeats, 'implementation_hashes':hashes,
        'reuse_authorized':False, 'adaptations':[], 'profiles':[],
        'budget_observations':{'default_values_changed':False, 'shared_total_budget_calibrated':False,
            'note':'Structural equality is not a solver timing sample; local diagnostics cannot calibrate top-level proof budgets.'},
        'semantic_notes':['Structural equality does not certify output definedness or an archived netlist.',
            'Local counterexamples never count as design-level differences.',
            'No reset protocol, feature define, parameter override, or X replacement is added.',
            'Declared DEFS_MOR1KX is not consumed by the upstream Yosys Makefile; no extra define is inferred.']}
    if args.inventory_only:
        report['profiles'] = [{'case':c['id'], 'top':configs[c['design']]['top'], 'status':'pending'}
                              for c in catalog['cases'] if c['id'] in selected]
        report['status'] = 'inventory_only'
        publish(out, report)
        return report
    tools = sequential.toolchain(args)
    report['tools'] = tools
    bases = {}
    started = time.monotonic()
    for case in catalog['cases']:
        if case['id'] not in selected:
            continue
        begin = time.monotonic()
        design = case['design']
        config = configs[design]
        directory = out/case['id']
        old_directory = out/'baselines'/design
        try:
            if design not in bases:
                bases[design] = elaborate(config, None, tools['yosys']['path'], old_directory, args.frontend_timeout)
            front = time.monotonic()
            new = elaborate(config, case['macro'], tools['yosys']['path'], directory, args.frontend_timeout)
            frontend_seconds = time.monotonic()-front
            row = evaluate(case, config, bases[design], new, old_directory, directory, tools, args)
            row['frontend_seconds'] = frontend_seconds
        except (TraceError, OSError, ValueError, KeyError) as exc:
            directory.mkdir(parents=True, exist_ok=True)
            row = {'case':case['id'], 'top':config['top'], 'status':'unknown',
                   'reason':'frontend_or_scope_error', 'detail':str(exc)}
        row.update(elapsed_seconds=time.monotonic()-begin, directory=str(directory))
        report['profiles'].append(row)
        write_json(directory/'report.json', row)
        publish(out, report)
        print(case['id'], config['top'], row['status'], row['reason'], flush=True)
    report['inputs_unchanged'] = all(digest(inputs/n) == sha for n,sha in catalog['input_files'].items())
    report['implementation_unchanged'] = all(digest(Path(p)) == sha for p,sha in hashes.items())
    report['tools_unchanged'] = all(digest(t['path']) == t['sha256'] for t in tools.values())
    report['evidence_files'] = sequential.fingerprint(out)
    intact = all(report[k] for k in ('inputs_unchanged','implementation_unchanged','tools_unchanged'))
    report['status'] = 'completed_with_findings' if intact else 'failed'
    report['elapsed_seconds'] = time.monotonic()-started
    # Do not include self-referential hashes of reports rewritten by publish().
    for name in ('report.json','timings.csv'):
        report['evidence_files'].pop(name, None)
    publish(out, report)
    return report
