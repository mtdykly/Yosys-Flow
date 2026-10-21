"""Reproducible, explicitly adapted ANUBIS combinational edit experiments."""
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from yosys_flow.common import ROOT, digest, require, write_json
from yosys_flow.contracts import ContractContext, SELECTOR_KEYS
from yosys_flow.cli import parser as flow_parser
from yosys_flow.runner import run as baseline_run
from .replace import run as replace_run, run_yosys, equivalence_script
from .failures import classify
from . import automatic

CORPUS = ROOT/'benchmarks/anubis'
CASES = {
    'bypass_noc1': {'file':'bypass_ex.v','module':'bypass_ex_bug','macro':'ANUBIS_NOC_1',
        'inputs':{'IRex':32,'IRmem':32},'outputs':{'SMDRsel':1},'expect':'equivalent'},
    'compare_noc2': {'file':'quick_compare.v','module':'quick_compare_bug','macro':'ANUBIS_NOC_2',
        'inputs':{'RSbus':32,'RTbus':32,'QCsel':6},'outputs':{'Result':1},'expect':'equivalent'},
    'compare_local9': {'file':'quick_compare.v','module':'quick_compare_bug','macro':'ANUBIS_LOCAL_9',
        'inputs':{'RSbus':32,'RTbus':32,'QCsel':6},'outputs':{'Result':1},'expect':'functional_change'},
    'control_local2': {'file':'alu_control.v','module':'alu_control_bug','macro':'ANUBIS_LOCAL_2',
        'inputs':{'IR':32},'outputs':{'ALUsel':8,'UseImm':1},'expect':'functional_change'},
    'writeback_local10': {'file':'wb_control.v','module':'wb_control_bug','macro':'ANUBIS_LOCAL_10',
        'inputs':{'IR':32},'outputs':{'WBsel':3},'expect':'reject_state'},
    'bypass_with_adder': {'file':'bypass_ex.v','module':'bypass_ex_bug','macro':'ANUBIS_NOC_1',
        'inputs':{'IRex':32,'IRmem':32},'outputs':{'SMDRsel':1},'expect':'equivalent',
        'neighbor':'dlx/alu.v:add_branch'},
}


def verify_corpus():
    lock = json.loads((CORPUS/'lock.json').read_text())
    for name, expected in lock['files'].items():
        require(digest(CORPUS/'upstream'/name)==expected, 'ANUBIS upstream checksum mismatch: '+name)
    return lock


def materialize(text, enabled, module, renamed):
    """Only select ANUBIS conditional branches; preserve original line numbers.

    No wildcard rewriting: two-state adaptation affects DEFAULT ASSIGNMENTS
    only, never dc6 patterns in casex or the upstream macro definitions.
    """
    active = True; stack = []; lines = []; changes = []
    for number,line in enumerate(text.splitlines(),1):
        original = line
        match = re.match(r'\s*`(ifdef|ifndef)\s+(ANUBIS_\w+)\s*$',line)
        if match:
            condition = (match[2] in enabled) == (match[1]=='ifdef')
            stack.append((active,condition)); active = active and condition; line = ''
        elif re.match(r'\s*`else\s*$',line):
            require(bool(stack), 'unbalanced benchmark macro')
            parent,condition = stack[-1]; active = parent and not condition; line = ''
        elif re.match(r'\s*`endif\s*$',line):
            require(bool(stack), 'unbalanced benchmark macro')
            active,_ = stack.pop(); line = ''
        elif not active: line = ''
        if line:
            line = re.sub(r'\bmodule\s+'+re.escape(module)+r'\b','module '+renamed,line)
            line = re.sub(r'(default\s*:\s*Result\s*=\s*)`dc\b',r"\g<1>1'b0",line)
            line = re.sub(r'(default\s*:\s*ALUsel\s*=\s*)`dc8\b',r"\g<1>8'b0",line)
        if line != original:
            changes.append({'line':number,'before':original,'after':line,
                            'kind':'default_output_two_state' if 'default' in original and '`dc' in original else 'macro_selection_or_module_rename'})
        lines.append(line)
    require(not stack, 'unclosed benchmark macro')
    return '\n'.join(lines)+'\n',changes


def width(bits): return f'[{bits-1}:0] ' if bits>1 else ''


def wrapper(case, changed):
    ports=[]
    for prefix in ('target','other'):
        ports.extend('input '+width(bits)+prefix+'_'+name for name,bits in case['inputs'].items())
        ports.extend('output '+width(bits)+prefix+'_'+name for name,bits in case['outputs'].items())
    ports.extend('output '+width(bits)+'summary_'+name for name,bits in case['outputs'].items())
    if case.get('neighbor'):
        ports.extend(['input [31:0] neighbor_RSbus, neighbor_Y','output [31:0] neighbor_sum'])
    lines=['module benchmark('+', '.join(ports)+');']
    for prefix in ('target','other'):
        module = 'anubis_edited' if changed and prefix=='target' else 'anubis_base'
        bindings=', '.join(f'.{name}({prefix}_{name})' for name in [*case['inputs'],*case['outputs']])
        lines.append(f'  {module} u_{prefix}({bindings});')
    lines.extend(f'  assign summary_{n} = target_{n} ^ other_{n};' for n in case['outputs'])
    if case.get('neighbor'):
        lines.append('  anubis_add u_neighbor(.RSbus(neighbor_RSbus), .Y(neighbor_Y), .ALUout(neighbor_sum));')
    return '\n'.join([*lines,'endmodule',''])


def prepare(name, directory):
    lock=verify_corpus(); case=CASES[name]; directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    text=(CORPUS/'upstream/dlx'/case['file']).read_text()
    artifacts={}
    for filename,enabled,module in [('base-leaf.v',set(),'anubis_base'),
                                    ('edited-leaf.v',{case['macro']},'anubis_edited')]:
        body,changes=materialize(text,enabled,case['module'],module)
        (directory/filename).write_text(body)
        artifacts[filename]={'upstream_file':'dlx/'+case['file'], 'sha256':digest(directory/filename),
            'enabled_macros':sorted(enabled),'line_mapping':'identity_including_blank_removed_lines',
            'changes':changes}
    (directory/'dlx_defs.v').write_bytes((CORPUS/'upstream/dlx/dlx_defs.v').read_bytes())
    (directory/'base-top.v').write_text(wrapper(case,False))
    (directory/'new-top.v').write_text(wrapper(case,True))
    if case.get('neighbor'):
        # A declared extraction of the ORIGINAL 32-bit ADD branch, not a claim
        # that the whole ALU (including its temporary state/X defaults) is supported.
        original=(CORPUS/'upstream/dlx/alu.v').read_text().splitlines()
        start=next(i for i,s in enumerate(original) if s.startswith('module alu_bug'))
        add=next(i for i,s in enumerate(original) if 'ALUout = RSbus + Y;' in s)
        end=next(i for i,s in enumerate(original) if s.startswith('endmodule'))
        lines=['']*len(original)
        lines[:start-1]=original[:start-1]  # preserve the upstream notice, not its include
        lines[start]='module anubis_add(input [31:0] RSbus, Y, output [31:0] ALUout);'
        lines[add]='assign '+original[add].split(':',1)[1].strip()
        lines[end]='endmodule'
        (directory/'neighbor.v').write_text('\n'.join(lines)+'\n')
        artifacts['neighbor.v']={'upstream_file':'dlx/alu.v','sha256':digest(directory/'neighbor.v'),
            'source_line':add+1,'line_mapping':'identity_with_explicit_branch_extraction',
            'adaptation':'32-bit add branch only; operands independent; complete ALU not claimed'}
    # Include explicit absolute paths so tools run from separate work directories.
    for name,files in [('base.v',['base-leaf.v','base-top.v']),
                       ('new.v',['base-leaf.v','edited-leaf.v','new-top.v'])]:
        (directory/name).write_text('\n'.join(f'`include "{directory/f}"' for f in
            (['neighbor.v'] if case.get('neighbor') else [])+files)+'\n')
    manifest={'schema_version':1,'case':case,'upstream':lock,'adaptations':artifacts,
              'fixture_inputs':{p.name:digest(p) for p in directory.iterdir() if p.is_file()},
              'scope':'ANUBIS_macro_edits_in_explicit_two_instance_combinational_wrapper',
              'original_historical_commit_pair_claimed':False}
    write_json(directory/'fixture.json',manifest)
    return case


def local_spec(context, contract, case, fixture, directory):
    """Explicit whole-leaf patch, constrained to the recorded boundary.

    No gates from the new full design are read. Unavailable original input bits
    are tied to zero and disclosed; the whole-new-design miter must validate
    that no new dependency was hidden. This is not automatic statement slicing.
    """
    module=context.module
    inputs=[b for b in contract['boundary'] if b['direction']=='input']
    outputs=[b for b in contract['boundary'] if b['direction']=='output']
    by_bit={b['original_bit']:b['port'] for b in inputs}
    lines=[f'`include "{fixture / "edited-leaf.v"}"',
           'module patch('+', '.join(['input '+b['port'] for b in inputs]+['output '+b['port'] for b in outputs])+');']
    connected=[]; tied=[]
    for name,bits in case['inputs'].items():
        values=[]
        for i,bit in enumerate(module['ports']['target_'+name]['bits']):
            if bit in by_bit: values.append(by_bit[bit])
            else:
                values.append("1'b0"); tied.append({'port':name,'offset':i,'old_bit':bit})
        connected.append('.'+name+'({'+', '.join(reversed(values))+'})')
    output_bits={}
    for name,bits in case['outputs'].items():
        lines.append('wire '+width(bits)+'new_'+name+';')
        connected.append(f'.{name}(new_{name})')
        for i,bit in enumerate(module['ports']['target_'+name]['bits']):
            if isinstance(bit,int): output_bits.setdefault(bit,[]).append(f'new_{name}[{i}]' if bits>1 else 'new_'+name)
    lines.append('anubis_edited changed('+', '.join(connected)+');')
    observers=[]
    for row in outputs:
        bit=row['original_bit']
        if row['reason']=='selected_source_connection' and not row['external_users']:
            require(bit in by_bit or bit in {'0','1'}, 'internal observer requires unavailable boundary function')
            expression=by_bit[bit] if bit in by_bit else "1'b"+bit
            observers.append(row['port'])
        else:
            require(bit in output_bits and len(output_bits[bit])==1,
                    'shared consumers or internal exit require explicit new output specification')
            expression=output_bits[bit][0]
        lines.append('assign '+row['port']+' = '+expression+';')
    lines.append('endmodule')
    (directory/'patch.v').write_text('\n'.join(lines)+'\n')
    bindings={'schema_version':1,'contract_id':contract['contract_id'],
              'inputs':{b['port']:[b['port']] for b in inputs},'outputs':{b['port']:[b['port']] for b in outputs}}
    write_json(directory/'bindings.json',bindings)
    write_json(directory/'patch-specification.json',{'scope':'explicitly_resynthesize_entire_target_leaf',
        'not_automatic_RTL_difference_extraction':True,'tied_unavailable_inputs':tied,
        'unchanged_internal_observation_outputs':observers,
        'must_validate_against_all_new_top_outputs':True})


def experiment(name, mode, fixture, directory, args):
    case=CASES[name]; directory.mkdir(parents=True)
    row={'case':name,'mode':mode,'expected_change':case['expect'],'status':'running','timings':{}}
    started=time.perf_counter(); stage='baseline_synthesis'
    try:
        base=directory/'base-run'
        base_args=['run','--source',str(fixture/'base.v'),'--top','benchmark','--out',str(base),
                   '--yosys',args.yosys,'--native-yosys',args.native_yosys,*(['--flatten'] if mode=='flatten' else [])]
        baseline_run(flow_parser().parse_args(base_args))
        row['timings']['one_time_baseline_and_stock_validation_seconds']=time.perf_counter()-started
        if getattr(args,'automatic',False):
            stage='automatic_derivation'
            result=automatic.run(SimpleNamespace(base_run=str(base),new_source=[str(fixture/'new.v')],
                out=str(directory/'automatic'),yosys=args.yosys,native_yosys=args.native_yosys))
            row['automatic_report']=str(directory/'automatic/report.json')
            row['timings']['automatic']=result['timings']
            row['status']=result['status']
            if result['status'] in {'blocked','failed'}:
                row['failure']=result['failure']
                if case['expect']=='reject_state' and result['failure'].get('code')=='state_outside_combinational_scope':
                    row['status']='expected_rejection'
                return row
            require(case['expect']!='reject_state','automatic flow accepted latch-introducing edit')
            row['checks']=result['checks'];row['selected_scopes']=result.get('selected_scopes',[])
            row['splice']=result.get('splice',{})
            if result['status']=='unchanged':
                work=directory/'automatic/work';script=equivalence_script('base.json','reference.json','benchmark')
            else:
                work=directory/'automatic/replacement/work';script=equivalence_script('base-view.json','new-reference.json','benchmark')
            stage='change_classification'
            if case['expect']=='equivalent':
                run_yosys(args.yosys,work,'base-new-equivalent',script)
                row['base_new_relation']='equivalent_all_outputs'
            else:
                run_yosys(args.yosys,work,'base-new-difference',script.replace('sat -verify -prove trigger 0','sat -set trigger 1'))
                require('model found' in (work/'base-new-difference.log').read_text(),'functional edit lacks a difference witness')
                row['base_new_relation']='different_with_sat_witness'
            row['status']='passed'
            return row
        if case['expect']=='reject_state':
            stage='new_input_scope'
            try:
                new_args=base_args.copy();new_args[new_args.index(str(fixture/'base.v'))]=str(fixture/'new.v')
                new_args[new_args.index(str(base))]=str(directory/'rejected-new-run')
                baseline_run(flow_parser().parse_args(new_args))
            except Exception as exc:
                failure=classify(stage,exc)
                require(failure['code']=='state_outside_combinational_scope','unexpected rejection: '+str(exc))
                row.update(status='expected_rejection',failure=failure)
                return row
            require(False,'latch-introducing ANUBIS edit was unexpectedly accepted')
        stage='source_query'; online_start=time.perf_counter(); step=online_start
        context=ContractContext(base)
        row['timings']['record_load_seconds']=time.perf_counter()-step
        row['abc_resources']=[{k:a.get(k) for k in ('invocation_id','region_input_count','function_proof',
            'fallback_reason','failure_category','failure_phase','input_variable_order','cleanup_copies','cleanup_deleted_objects')}
            for a in context.trace.abc_internal]
        row['baseline_record_bytes']=sum(p.stat().st_size for p in base.rglob('*.jsonl'))
        selector=dict.fromkeys(SELECTOR_KEYS)
        selector.update(source=str(fixture/'base-leaf.v'),line=1,
                        end_line=len((fixture/'base-leaf.v').read_text().splitlines()),instance='benchmark/u_target')
        # Record the minimal macro branch query BEFORE an explicit whole-leaf
        # expansion. The benchmark does not label whole-leaf replacement minimal.
        original=(CORPUS/'upstream/dlx'/case['file']).read_text().splitlines()
        first=next(i+1 for i,s in enumerate(original) if '`ifndef '+case['macro'] in s)
        last=next(i+1 for i,s in enumerate(original[first:],first) if '`endif' in s)
        edit_selector=selector|{'line':first,'end_line':last}
        step=time.perf_counter(); edit_query=context.query(edit_selector)
        write_json(directory/'edit-query.json',edit_query)
        row['timings']['source_query_seconds']=time.perf_counter()-step
        stage='contract'; step=time.perf_counter()
        contract=context.build({'kind':'selection','selector':selector});context.validate(contract)
        write_json(directory/'region.contract.json',contract)
        row['timings']['contract_generation_and_check_seconds']=time.perf_counter()-step
        row['selection']={'edit_query_cells':len(edit_query['final_cells']),
            'contract_cells':len(contract['region']['cells']), 'whole_design_cells':len(context.module['cells']),
            'expansion_policy':'explicit_whole_target_leaf','instance':'benchmark/u_target',
            'minimal_region_claimed':False}
        row['contract_status']=contract['verification']['status']
        if row['contract_status']!='verified_old_region':
            row.update(status='blocked',failure={'code':'old_region_not_proven','stage':stage,
                'blockers':contract['verification']['blockers'],
                'abc':[{k:a.get(k) for k in ('invocation_id','fallback_reason','unproved_import_reasons',
                                            'stage_witness_failures','local_proof_failures')}
                       for a in context.trace.abc_internal]})
            return row
        stage='patch_and_binding';step=time.perf_counter()
        local_spec(context,contract,case,fixture,directory)
        row['timings']['patch_specification_and_binding_seconds']=time.perf_counter()-step
        stage='replacement'
        result=replace_run(SimpleNamespace(base_run=str(base),contract=str(directory/'region.contract.json'),
            bindings=str(directory/'bindings.json'),patch_rtl=str(directory/'patch.v'),patch_top='patch',
            new_source=[str(fixture/'new.v')],out=str(directory/'replacement'),yosys=args.yosys))
        row['timings']['replacement']=result['timings']
        pre=sum(row['timings'][k] for k in ('record_load_seconds','source_query_seconds',
                                          'contract_generation_and_check_seconds','patch_specification_and_binding_seconds'))
        row['timings']['incremental_candidate_ready_seconds']=pre+result['timings']['replacement_ready_seconds']
        row['timings']['online_checked_total_seconds']=time.perf_counter()-online_start
        row['timings']['includes_whole_reference_in_checked_total']=True
        row['checks']=result['checks'];row['splice']=result['splice']
        stage='change_classification'
        work=directory/'replacement/work'
        script=equivalence_script('base-view.json','new-reference.json','benchmark')
        if case['expect']=='equivalent':
            run_yosys(args.yosys,work,'base-new-equivalent',script)
            row['base_new_relation']='equivalent_all_outputs'
        else:
            script=script.replace('sat -verify -prove trigger 0','sat -set trigger 1')
            run_yosys(args.yosys,work,'base-new-difference',script)
            require('model found' in (work/'base-new-difference.log').read_text(),'functional edit lacks a difference witness')
            row['base_new_relation']='different_with_sat_witness'
        row['status']='passed'
    except Exception as exc:
        row.update(status='failed',failure=classify(stage,exc))
    finally:
        identity=json.loads((fixture/'fixture.json').read_text())
        if any(digest(fixture/n)!=h for n,h in identity['fixture_inputs'].items()):
            row.update(status='failed',failure={'code':'benchmark_input_changed','stage':stage})
        row['timings']['case_total_seconds']=time.perf_counter()-started
        write_json(directory/'case-report.json',row)
    return row


def run(args):
    verify_corpus()
    out=Path(args.out).resolve();require(not out.exists(),'refusing to overwrite '+str(out));out.mkdir(parents=True)
    results=[]
    modes=['retained','flatten'] if args.mode=='both' else [args.mode]
    for name in args.cases or list(CASES):
        require(name in CASES,'unknown ANUBIS case: '+name)
        fixture=out/name/'fixture';prepare(name,fixture)
        for mode in modes:
            results.append(experiment(name,mode,fixture,out/name/mode,args))
            write_json(out/'report.json',{'status':'running','cases':results})
    counts={s:sum(r['status']==s for r in results) for s in ('passed','expected_rejection','blocked','failed')}
    report={'status':'failed' if counts['failed'] else 'completed_with_limitations' if counts['blocked'] else 'passed',
            'upstream':verify_corpus(),'cases':results,'counts':counts,
            'automatic_derivation':getattr(args,'automatic',False),
            'scope':'selected_combinational_ANUBIS_modules_not_the_full_ANUBIS_score',
            'timing_claim':'includes query, replay, contract, binding and splice; no demonstrated speedup claim'}
    write_json(out/'report.json',report)
    return report
