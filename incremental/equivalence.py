"""Budgeted, all-output combinational proofs before region selection.

Only leaf interfaces are matched. Internal names are never proof assumptions;
SAT must prove both equality and defined outputs for every binary input.
Failure, timeout, unsupported cells and missing proof markers mean no reuse.
"""
import json
import math
from pathlib import Path
import subprocess
import time

from yosys_flow.common import TraceError, digest, require, require_combinational, write_json

SUCCESS = 'SAT proof finished - no model found: SUCCESS!'
DIFFERENT = 'SAT proof finished - model found: FAIL!'
TIMEOUT = 'Interrupted SAT solver: TIMEOUT!'
UNDEFINED = '__flow_undefined'
TOTAL_CELLS = {'$not','$pos','$neg','$and','$or','$xor','$xnor','$reduce_and','$reduce_or',
    '$reduce_xor','$reduce_xnor','$reduce_bool','$logic_not','$logic_and','$logic_or',
    '$lt','$le','$eq','$ne','$eqx','$nex','$ge','$gt','$add','$sub','$mul',
    '$shl','$shr','$sshl','$sshr','$shift','$mux','$slice','$concat'}


def binary_context(coarse, proven_modules):
    """Check the unchanged context for unconditional whole-netlist reuse.

    Leaf SAT constrains leaf inputs to binary values. A tied X, tri-state, or
    e.g. division by zero in its context must not silently violate that premise.
    Proven leaf definitions are total by SAT; other modules must contain only
    total binary primitives and binary literals. This deliberately declines
    some valid contexts instead of attempting an unbudgeted whole-design proof.
    """
    for name,module in coarse.items():
        if name in proven_modules:continue
        values=[b for p in module['ports'].values() for b in p['bits']]
        for cell in module['cells'].values():
            if cell['type'] not in coarse and cell['type'] not in TOTAL_CELLS:
                return {'verified':False,'reason':'context_cell_not_known_total','module':name,'type':cell['type']}
            values.extend(b for bits in cell['connections'].values() for b in bits)
        if any(isinstance(b,str) and b not in {'0','1'} for b in values):
            return {'verified':False,'reason':'undefined_context_signal','module':name}
    return {'verified':True,'rule':'SAT_total_leaves_in_total_binary_combinational_context'}


def run_phase(binary, directory, name, script, deadline):
    """Wall budget includes frontend lowering/import, not only SAT solving."""
    (directory/(name+'.ys')).write_text(script+'\n')
    remaining=deadline-time.perf_counter()
    if remaining<=0:return None,'wall_timeout'
    try:
        result=subprocess.run([binary,'-Q','-T','-s',name+'.ys'],cwd=directory,
                              capture_output=True,text=True,timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        def text(value):return value.decode(errors='replace') if isinstance(value,bytes) else value or ''
        (directory/(name+'.log')).write_text(text(exc.stdout)+text(exc.stderr)+'\nWALL BUDGET EXHAUSTED\n')
        return None,'wall_timeout'
    log=result.stdout+result.stderr
    (directory/(name+'.log')).write_text(log)
    if result.returncode:return None,'tool_error'
    return log,None


def render(module, name):
    lines=module.render().splitlines()
    index=next(i for i,s in enumerate(lines) if s.startswith('module '))
    lines[index]='module \\'+name
    return '\n'.join(lines)+'\n'


def guard_coarse(module, ports):
    require_combinational(module)
    actual={n:{'direction':p['direction'],'width':len(p['bits']),
               'offset':p.get('offset',0),'upto':bool(p.get('upto',0)),
               'signed':bool(p.get('signed',0))} for n,p in module['ports'].items()}
    require(actual==ports,'lowered proof interface differs from matched leaf interface')
    require(all(c['type'] not in {'$anyconst','$anyseq','$allconst','$allseq','$anyinit',
                                  '$assert','$assume','$cover','$live','$fair'}
                for c in module['cells'].values()),'formal/nondeterministic cells are outside reuse semantics')


def definedness_miter(design, ports):
    """miter uses eqx; two matching X values must NOT certify binary reuse.

    Reduction XOR is undefined iff any compared output bit is undefined. An
    explicit eqx against X exposes this as a defined one-bit proof obligation.
    SAT models undef and proves this flag zero along with the usual trigger.
    """
    module=design['modules']['miter']
    outputs=[]
    for name,p in sorted(ports.items()):
        if p['direction']!='output':continue
        for prefix in ('gold_','gate_'):
            value=module['ports'][prefix+name]
            require(value['direction']=='output' and len(value['bits'])==p['width'],
                    'miter is missing a compared output')
            outputs.extend(value['bits'])
    require(outputs,'no observable leaf outputs')
    bits=[b for n in module['netnames'].values() for b in n['bits'] if type(b) is int]
    reduced,flag=max(bits,default=1)+1,max(bits,default=1)+2
    require(UNDEFINED not in module['netnames'] and UNDEFINED not in module['ports'],'reserved miter signal')
    module['ports'][UNDEFINED]={'direction':'output','bits':[flag]}
    module['netnames'][UNDEFINED]={'hide_name':0,'bits':[flag],'attributes':{}}
    module['cells'][UNDEFINED+'$reduce']={
        'type':'$reduce_xor','hide_name':1,'attributes':{},
        'parameters':{'A_SIGNED':'0','A_WIDTH':format(len(outputs),'032b'),'Y_WIDTH':format(1,'032b')},
        'port_directions':{'A':'input','Y':'output'},'connections':{'A':outputs,'Y':[reduced]}}
    module['cells'][UNDEFINED+'$check']={
        'type':'$eqx','hide_name':1,'attributes':{},
        'parameters':{'A_SIGNED':'0','B_SIGNED':'0','A_WIDTH':format(1,'032b'),
                      'B_WIDTH':format(1,'032b'),'Y_WIDTH':format(1,'032b')},
        'port_directions':{'A':'input','B':'input','Y':'output'},
        'connections':{'A':[reduced],'B':['x'],'Y':[flag]}}
    return design


def verdict(log, counterexample):
    lines=log.splitlines()
    if TIMEOUT in lines:return 'unknown','solver_timeout'
    if lines.count(SUCCESS)==1 and DIFFERENT not in lines:return 'equivalent','all_outputs_equal_and_defined'
    if lines.count(DIFFERENT)==1 and SUCCESS not in lines:
        if not counterexample.is_file():return 'unknown','counterexample_missing'
        signals={r['name']:r for r in json.loads(counterexample.read_text())['signal']}
        # Single-bit WaveJSON values are encoded in the first wave character.
        values={n:r.get('wave','')[:1] for n,r in signals.items()}
        if values.get(UNDEFINED)=='0' and values.get('trigger')=='1':
            return 'different','binary_counterexample'
        return 'unknown','undefined_output_or_unrecognized_counterexample'
    return 'unknown','proof_marker_missing'


def prove(old, new, binary, directory, deadline):
    directory.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter()
    row={'status':'unknown','proof_rule':'all_leaf_outputs_equal_and_defined_v1',
         'directory':str(directory),'ports':old.ports(),'assumptions':'all input bits defined; no internal matches',
         'full_synthesis_used':False,'timings':{}}
    try:
        for module,name in ((old,'gold'),(new,'gate')):
            (directory/(name+'.il')).write_text(render(module,name))
        original={name:digest(directory/name) for name in ('gold.il','gate.il')}
        commands=[]
        for name in ('gold','gate'):
            commands += ['read_rtlil '+name+'.il','hierarchy -check -top '+name,
                'proc','opt_expr','opt_clean','check -assert','write_json '+name+'.json','design -stash '+name]
        commands += ['design -copy-from gold gold','design -copy-from gate gate',
            'miter -equiv -make_outputs -flatten gold gate miter','hierarchy -check -top miter',
            'check -assert','write_json miter.json']
        phase_started=time.perf_counter()
        _,reason=run_phase(binary,directory,'prepare','\n'.join(commands),deadline)
        row['timings']['prepare_miter_seconds']=time.perf_counter()-phase_started
        if reason:row['reason']=reason;return row
        phase_started=time.perf_counter()
        for name in ('gold','gate'):
            guard_coarse(json.loads((directory/(name+'.json')).read_text())['modules'][name],old.ports())
        design=definedness_miter(json.loads((directory/'miter.json').read_text()),old.ports())
        write_json(directory/'checked-miter.json',design)
        frozen={**original,'checked-miter.json':digest(directory/'checked-miter.json')}
        row['timings']['guard_and_definedness_seconds']=time.perf_counter()-phase_started
        seconds=max(1,math.ceil(deadline-time.perf_counter()))
        script=('read_json checked-miter.json\nhierarchy -check -top miter\ncheck -assert\n'
                f'sat -enable_undef -timeout {seconds} -prove trigger 0 -prove {UNDEFINED} 0 '
                '-set-def-inputs -show-inputs -show-outputs -dump_json counterexample.json')
        phase_started=time.perf_counter()
        log,reason=run_phase(binary,directory,'prove',script,deadline)
        row['timings']['sat_command_seconds']=time.perf_counter()-phase_started
        if reason:row['reason']=reason;return row
        require(all(digest(directory/n)==h for n,h in frozen.items()),'proof inputs changed during precheck')
        row['status'],row['reason']=verdict(log,directory/'counterexample.json')
        if (directory/'counterexample.json').is_file():row['counterexample']=str(directory/'counterexample.json')
    except (TraceError,OSError,ValueError,KeyError,TypeError) as exc:
        # This optimization may decline a proof, but may never bypass normal
        # synthesis/validation after an incomplete or unsupported comparison.
        row.update(status='unknown',reason='unsupported_or_invalid_proof',detail=str(exc))
    finally:
        row['elapsed_seconds']=time.perf_counter()-started
        row['artifacts']={p.name:digest(p) for p in directory.iterdir() if p.is_file() and p.name!='report.json'}
        write_json(directory/'report.json',row)
    return row


def precheck(old,new,difference,oc,nc,work,args,coarse):
    started=time.perf_counter()
    enabled=not getattr(args,'no_equivalence_precheck',False)
    per_proof=float(getattr(args,'equivalence_timeout',5.0))
    budget=float(getattr(args,'equivalence_budget',15.0))
    require(math.isfinite(per_proof) and per_proof>0 and math.isfinite(budget) and budget>0,
            'equivalence budgets must be positive finite seconds')
    summary={'enabled':enabled,'solver':'yosys_miter_sat','per_proof_wall_seconds':per_proof,
        'total_wall_budget_seconds':budget,'checks':[],'skipped_equivalent_scopes':[],
        'remaining_scopes':[],'proof_scope':'unchanged-interface leaves in structurally unchanged instance contexts',
        'PPA_improvement_preserved':False}
    cache={};deadline=started+budget
    for change in difference['changes']:
        path=change['instance'];a,b=old[oc[path]],new[nc[path]]
        row={'instance':path,'old_module':a.name,'new_module':b.name,'status':'unknown'}
        reason=None
        if not enabled:reason='disabled'
        elif a.children(old) or b.children(new):reason='not_a_leaf_pair'
        elif a.ports()!=b.ports():reason='interface_changed'
        elif not any(p['direction']=='output' for p in a.ports().values()):reason='no_outputs'
        elif any(p['direction'] not in {'input','output'} for p in a.ports().values()):reason='inout_interface'
        if reason:row['reason']=reason
        else:
            key=(a.name,b.name)
            if key not in cache and time.perf_counter()>=deadline:row['reason']='total_budget_exhausted'
            else:
                cached=key in cache
                if not cached:
                    target=work/'equivalence'/('proof-'+str(len(cache)))
                    cache[key]=prove(a,b,args.yosys,target,min(deadline,time.perf_counter()+per_proof))
                proof=cache[key]
                row.update(status=proof['status'],reason=proof['reason'],cache_hit=cached,
                    proof_directory=proof['directory'],proof_report_sha256=digest(Path(proof['directory'])/'report.json'),
                    old_definition_fingerprint=change['old_fingerprint'],new_definition_fingerprint=change['new_fingerprint'])
        summary['checks'].append(row)
    if summary['checks'] and all(r['status']=='equivalent' for r in summary['checks']):
        proven={r['new_module'] for r in summary['checks']}
        for r in summary['checks']:
            name=r['old_module']
            if name in new and old[name].canonical(old)==new[name].canonical(new):proven.add(name)
        summary['whole_reuse_context']=binary_context(coarse,proven)
        if not summary['whole_reuse_context']['verified']:
            for row in summary['checks']:
                row.update(leaf_proof_status='equivalent',status='unknown',reason='binary_context_not_certified')
    for row in summary['checks']:
        summary['skipped_equivalent_scopes' if row['status']=='equivalent' else 'remaining_scopes'].append(row['instance'])
    summary['unique_proof_count']=len(cache)
    summary['elapsed_seconds']=time.perf_counter()-started
    summary['counts']={state:sum(r['status']==state for r in summary['checks']) for state in ('equivalent','different','unknown')}
    write_json(work.parent/'equivalence-precheck.json',summary)
    return summary
