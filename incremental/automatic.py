"""Derive concrete patches from an archived baseline and complete new RTL.

Changed pre-proc assignments seed local regions; missing boundaries can expand
them and module instances remain a fallback. Candidate provenance never becomes
exact by planning. New full synthesis is only the final validation oracle.
"""
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import quote
import uuid

from yosys_flow.common import PIN, digest, require, require_combinational, token, unescape, write_json
from yosys_flow.contracts import ContractContext
from yosys_flow.hierarchy import electrical_design
from . import rtlil, replace
from .failures import classify


class PlanningBlocked(Exception):
    def __init__(self, code, detail): self.code,self.detail=code,detail;super().__init__(detail)


def needed(condition,code,detail):
    if not condition: raise PlanningBlocked(code,detail)


def prepare(context, args, work):
    """Elaborate and check scope, without synth/techmap/ABC on the new design."""
    config=context.manifest['configuration'];top=config['top']
    native=shutil.which(args.native_yosys);stock=shutil.which(args.yosys)
    require(native and stock,'automatic mode requires the pinned stock and native Yosys')
    require(all(PIN in subprocess.check_output([tool,'-V'],text=True) for tool in (native,stock)), 'automatic mode requires pinned Yosys')
    sources=[Path(s).resolve(strict=True) for s in args.new_source]
    options=['-D'+s for s in config['defines']]+['-I'+s for s in config['include_dirs']]
    parameters=[]
    for parameter in config['parameters']:
        name,value=parameter.split('=',1);parameters+=['-chparam',name,value]
    # Options were validated when the recorded run was created. Recheck that
    # none can become an extra Yosys command in this invocation.
    for value in [*options,*parameters]: token(value)
    frontend='read_verilog -sv -defer '+' '.join([*options,*map(token,sources)])
    frontend+='\nhierarchy -check -top '+top+' '+' '.join(parameters)
    started_ns=time.time_ns()
    replace.run_yosys(native,work,'new-frontend',
        'flow_start new-frontend.jsonl '+uuid.uuid4().hex+'\n'+frontend+
        '\nflow_hierarchy\nwrite_rtlil new.frontend.il\nflow_stop')
    rows=[json.loads(s) for s in (work/'new-frontend.jsonl').read_text().splitlines()]
    files=set(sources)|{(Path(p) if Path(p).is_absolute() else work/p).resolve() for p in rows[-1]['input_files']}
    files.discard((work/'new-frontend.ys').resolve())
    require(all(p.stat().st_mtime_ns<=started_ns for p in files),'new input changed during frontend elaboration')
    inputs={str(p):digest(p) for p in sorted(files)}
    replace.run_yosys(stock,work,'new-combinational-check',
        'read_rtlil new.frontend.il\nproc\ncheck -assert\nwrite_json new.coarse.json')
    coarse=json.loads((work/'new.coarse.json').read_text())['modules']
    for module in coarse.values():require_combinational(module,coarse)
    return rtlil.parse((work/'new.frontend.il').read_text()),coarse,inputs


class Endpoints:
    """Resolve original interface bits using actual recorded port/copy links."""
    def __init__(self,context): self.context=context;self.cache={};self.evidence=[]

    def bit(self,path,wire,offset,seen=frozenset()):
        key=(path,wire,offset)
        if key in self.cache:return self.cache[key]
        if key in seen:return None
        c=self.context;wire=unescape(wire);net=None;rule=None
        if c.trace.view_rule=='electrical_instance_expansion_v1':
            candidate=c.module['netnames'].get(path+'/'+quote(wire,safe=''))
            if candidate and candidate.get('definition',{}).get('instance')==path:
                net=candidate;rule='validated_instance_port_or_wire'
        elif path==quote(c.baseline['module'],safe=''):
            net=c.module['netnames'].get(wire);rule='recorded_top_wire'
        if net and offset<len(net['bits']): result=net['bits'][offset]
        else:
            info=c.trace.instance_catalog.get(path);result=None
            if not info:return None
            binding=info.get('bindings',{})
            ports=binding.get('ports',{})
            if '\\'+wire in ports and offset<len(ports['\\'+wire]):
                bit=ports['\\'+wire][offset]
                if isinstance(bit,str):result=bit
                else:result=self.bit(binding['parent'],bit['wire'],bit['offset'],seen|{key})
                rule='recorded_hierarchy_port_binding'
            if result is None and binding:
                events=[e for e in c.trace.flatten_events.values() if e['instance']==binding['instance_ref']]
                if len(events)==1 and '\\'+wire in events[0]['wire_map']:
                    result=self.bit(binding['parent'],events[0]['wire_map']['\\'+wire],offset,seen|{key})
                    rule='recorded_flatten_wire_map:'+events[0]['ref']
        if result is not None:
            self.cache[key]=result
            self.evidence.append({'instance':path,'wire':wire,'offset':offset,'final_bit':result,'rule':rule})
        return result


def input_support(modules, name):
    """Conservative coarse-IR dependencies, including hierarchical pin bindings.

    Builtin multi-bit cells conservatively depend on all input bits. This may
    enlarge a patch but cannot classify a genuinely used input as unused.
    """
    module=modules[name];drivers={};memo={};active=set()
    for cell in module['cells'].values():
        for p,d in cell['port_directions'].items():
            if d=='output':
                for i,b in enumerate(cell['connections'][p]):
                    if isinstance(b,int):
                        require(b not in drivers,'multiple coarse IR drivers');drivers[b]=(cell,p,i)
    inputs={b for p in module['ports'].values() if p['direction']=='input' for b in p['bits']}
    child_support={}
    def visit(bit):
        if isinstance(bit,str):return set()
        if bit in inputs:return {bit}
        if bit in memo:return memo[bit]
        require(bit not in active,'cycle in new coarse IR');active.add(bit)
        require(bit in drivers,'undriven output in new coarse IR')
        cell,port,index=drivers[bit]
        if cell['type'] in modules:
            if cell['type'] not in child_support:child_support[cell['type']]=input_support(modules,cell['type'])
            supports=child_support[cell['type']][port,index]
            dependencies=[cell['connections'][p][i] for p,i in supports]
        else:dependencies=[b for p,bs in cell['connections'].items() if cell['port_directions'][p]=='input' for b in bs]
        result=set().union(*(visit(b) for b in dependencies));active.remove(bit);memo[bit]=result
        return result
    by_bit={}
    for p,info in module['ports'].items():
        if info['direction']=='input':
            for i,b in enumerate(info['bits']):by_bit.setdefault(b,set()).add((p,i))
    return {(p,i):set().union(*(by_bit[b] for b in visit(bit))) for p,info in module['ports'].items()
            if info['direction']=='output' for i,bit in enumerate(info['bits'])}


def contract_for(context,old,catalog,scopes):
    parents=[];selections=[]
    for path,module in sorted(catalog.items()):
        if not any(path==s or path.startswith(s+'/') for s in scopes):continue
        selector=rtlil.source_selector(old[module],path)
        query=context.query(selector)
        selections.append({'selector':selector,'cell_count':len(query['final_cells']),
                           'connection_count':len(query['final_connections'])})
        if query['final_cells'] or query['final_connections']:
            doc=context.build({'kind':'selection','selector':selector})
            if all(p['contract_id']!=doc['contract_id'] for p in parents):parents.append(doc)
    needed(parents,'no_recorded_region','No recorded final objects for changed instance scopes')
    while len(parents)>1:
        parents=[context.build({'kind':'union','parents':parents[i:i+32]}) if len(parents[i:i+32])>1 else parents[i]
                 for i in range(0,len(parents),32)]
    return parents[0],selections


def patch_for(context,old,new,oc,nc,coarse,scopes,contract):
    endpoint=Endpoints(context);outputs={};units=[];extra=set();unused=[]
    boundary_inputs={b['original_bit'] for b in contract['boundary'] if b['direction']=='input'}
    old_driven={b for c in contract['region']['cells'] for p,bs in context.module['cells'][unescape(c['name'])]['connections'].items()
                if context.module['cells'][unescape(c['name'])]['port_directions'][p]=='output' for b in bs}
    for index,path in enumerate(scopes):
        a,b=old[oc[path]],new[nc[path]]
        needed(a.ports()==b.ports(),'changed_local_interface','Module interface changed at '+path)
        support=set().union(*input_support(coarse,b.name).values())
        unit={'module':b,'instance':path,'ports':b.ports(),'inputs':{},'outputs':{}}
        for number,(port,info) in enumerate(b.ports().items()):
            needed(info['direction'] in {'input','output'},'unsupported_interface','Inout port at '+path)
            if info['direction']!='output':continue
            wire='\\__auto_out_'+str(index)+'_'+str(number)
            unit['outputs'][port]=wire
            for i in range(info['width']):
                bit=endpoint.bit(path,port,i)
                if isinstance(bit,int):outputs.setdefault(bit,[]).append(f'{wire} [{i}]')
        units.append(unit)
        for port,info in b.ports().items():
            if info['direction']!='input':continue
            values=[]
            for i in range(info['width']):
                bit=endpoint.bit(path,port,i)
                if (port,i) not in support:
                    values.append(('constant','0'));unused.append({'instance':path,'port':port,'offset':i})
                else:
                    needed(bit is not None,'boundary_identity_unavailable',f'No recorded endpoint for {path}.{port}[{i}]')
                    needed(bit not in {'x','z'},'non_binary_boundary',f'Undefined boundary for {path}.{port}[{i}]')
                    values.append(('bit',bit))
                    if isinstance(bit,int) and bit not in boundary_inputs and bit not in old_driven:extra.add(bit)
            unit['inputs'][port]=values
    if extra:contract=context.build({'kind':'extend_inputs','parent':contract,'input_bits':sorted(extra)})
    by_bit={b['original_bit']:b['port'] for b in contract['boundary'] if b['direction']=='input'}
    def expression(bit):
        if isinstance(bit,str):return "1'"+bit
        if bit in by_bit:return '\\'+by_bit[bit]
        needed(len(outputs.get(bit,[]))==1,'boundary_identity_unavailable','Input is neither external boundary nor unique selected output: '+str(bit))
        return outputs[bit][0]
    lines=['module \\__auto_patch']
    for i,row in enumerate(contract['boundary'],1):lines.append(f"  wire {row['direction']} {i} \\{row['port']}")
    for index,unit in enumerate(units):
        for port,wire in unit['outputs'].items():lines.append(f"  wire width {unit['ports'][port]['width']} {wire}")
        # Derived parameter modules use a $paramod identifier, not an escaped
        # user identifier. Preserve the actual writer token for both forms.
        kind=rtlil.tokens(unit['module'].lines[0])[1]
        lines.append(f"  cell {kind} \\__auto_instance_{index}")
        for port,values in unit['inputs'].items():
            bits=["1'0" if kind=='constant' else expression(bit) for kind,bit in values]
            lines.append('    connect \\'+port+' { '+' '.join(reversed(bits))+' }')
        for port,wire in unit['outputs'].items():lines.append('    connect \\'+port+' '+wire)
        lines.append('  end')
    for row in contract['boundary']:
        if row['direction']!='output':continue
        bit=row['original_bit']
        if row['reason']=='selected_source_connection' and not row['external_users'] and bit not in old_driven:
            value=expression(bit)
        else:
            needed(len(outputs.get(bit,[]))==1,'output_requires_region_expansion_or_consumer_split',
                   'No unique new interface output for old boundary '+row['port'])
            value=outputs[bit][0]
        lines.append('  connect \\'+row['port']+' '+value)
    lines.append('end')
    needed('__auto_patch' not in new,'reserved_module_collision','New RTL uses reserved __auto_patch name')
    text=rtlil.extract(new,[u['module'].name for u in units])+'\n'+'\n'.join(lines)+'\n'
    bindings={'schema_version':1,'contract_id':contract['contract_id'],
        'inputs':{b['port']:[b['port']] for b in contract['boundary'] if b['direction']=='input'},
        'outputs':{b['port']:[b['port']] for b in contract['boundary'] if b['direction']=='output'}}
    return contract,text,bindings,{'endpoint_evidence':endpoint.evidence,'additional_input_bits':sorted(extra),
        'unused_inputs_tied_zero':unused,'unused_input_proof':'conservative_new_coarse_IR_support',
        'local_modules':[u['module'].name for u in units],'scopes':scopes}


def expanded(scopes,top):
    parents={s.rsplit('/',1)[0] if '/' in s else quote(top,safe='') for s in scopes}
    return sorted(s for s in parents if not any(s.startswith(p+'/') for p in parents if p!=s))


def run(args):
    out=Path(args.out).resolve();require(not out.exists(),'refusing to overwrite '+str(out));out.mkdir(parents=True)
    work=out/'work';work.mkdir();started=time.perf_counter();stage='baseline'
    report={'schema_version':1,'status':'running','automatic_RTL_diff':True,'automatic_patch_and_bindings':True,
        'granularity':'changed_assignments_with_boundary_closure_and_module_fallback','minimal_region_claimed':False,
        'new_full_synthesis_used_only_as_validation_oracle':True,'timings':{},'attempts':[]}
    try:
        context=ContractContext(args.base_run);top=context.baseline['module']
        require('frontend.il' in context.manifest['artifacts'],'automatic mode requires archived pre-proc frontend.il')
        old=rtlil.parse((context.directory/'frontend.il').read_text())
        report['base_run']=str(context.directory);report['old_frontend_sha256']=digest(context.directory/'frontend.il')
        report['changed_or_missing_old_files']=context.input_changes()
        report['timings']['baseline_replay_seconds']=time.perf_counter()-started
        stage='new_frontend';step=time.perf_counter()
        new,coarse,new_inputs=prepare(context,args,work)
        report['new_inputs_sha256']=new_inputs;report['new_frontend_sha256']=digest(work/'new.frontend.il')
        frozen_inputs={**new_inputs,str(work/'new.frontend.il'):report['new_frontend_sha256']}
        report['timings']['new_frontend_and_scope_check_seconds']=time.perf_counter()-step
        stage='difference';step=time.perf_counter()
        difference,oc,nc=rtlil.compare(old,new,top);write_json(out/'difference.json',difference)
        report['changes']=difference['changes'];report['timings']['IR_difference_seconds']=time.perf_counter()-step
        scopes=sorted(r['instance'] for r in difference['changes'])
        if not scopes:
            stage='unchanged_verification'
            write_json(work/'base.json',electrical_design(context.trace,top))
            replace.run_yosys(args.yosys,work,'unchanged-reference',
                f'read_rtlil new.frontend.il\nsynth -top {top}'+(' -flatten' if context.manifest['configuration']['flatten'] else '')+'\nwrite_json reference.json')
            replace.run_yosys(args.yosys,work,'unchanged-equivalence',replace.equivalence_script('base.json','reference.json',top))
            replace.run_yosys(args.yosys,work,'unchanged-export','read_json base.json\nwrite_verilog -noattr unchanged.v')
            replace.run_yosys(args.yosys,work,'unchanged-readback',
                f'read_verilog unchanged.v\nhierarchy -top {top}\nproc\ntechmap\ncheck -assert\nwrite_json readback.json')
            replace.run_yosys(args.yosys,work,'unchanged-readback-equivalence',replace.equivalence_script('base.json','readback.json',top))
            require(all(digest(p)==h for p,h in frozen_inputs.items()),'new input or frontend changed during automatic run')
            require(ContractContext(args.base_run).baseline==context.baseline,'baseline changed during automatic run')
            shutil.copyfile(work/'base.json',out/'incremental.json');shutil.copyfile(work/'unchanged.v',out/'incremental.v')
            report.update(status='unchanged',checks={'all_top_outputs_match_new_full_synthesis':True,
                'all_old_cells_reused':True,'emitted_Verilog_equivalent':True,'baseline_unchanged':True})
        else:
            stage='region_and_binding';step=time.perf_counter()
            from . import statements
            report['statement_plan']={}
            planned=statements.plan(context,old,new,oc,nc,coarse,scopes,report['statement_plan'],work,args.yosys)
            if planned:
                contract,patch,bindings,spec=planned
                report['selection_strategy']='statement_dependency_slice'
            else:
                report['selection_strategy']='module_fallback'
                for attempt in range(65):
                    row={'scopes':scopes};report['attempts'].append(row)
                    try:
                        contract,selections=contract_for(context,old,oc,scopes)
                        row['selections']=selections;row['contract_status']=contract['verification']
                        needed(contract['verification']['status']=='verified_old_region','old_region_not_proven',str(contract['verification']['blockers']))
                        contract,patch,bindings,spec=patch_for(context,old,new,oc,nc,coarse,scopes,contract)
                        context.validate(contract)
                        row['status']='ready';break
                    except PlanningBlocked as exc:
                        row.update(status='blocked',reason=exc.code,detail=exc.detail)
                        next_scopes=expanded(scopes,top)
                        if scopes==next_scopes:raise
                        scopes=next_scopes
                else:raise PlanningBlocked('expansion_budget','Region expansion limit reached')
            write_json(out/'region.contract.json',contract);write_json(out/'bindings.json',bindings)
            write_json(out/'patch-specification.json',spec);(out/'patch.il').write_text(patch)
            # Verilog is a reviewable form of the local semantics, not a text
            # slice of incomplete always blocks.
            replace.run_yosys(args.yosys,work,'patch-export',
                'read_rtlil '+token(out/'patch.il')+'\nhierarchy -check -top __auto_patch\nproc\nwrite_verilog -noattr '+token(out/'patch.v'))
            report['timings']['region_contract_patch_binding_seconds']=time.perf_counter()-step
            stage='replacement'
            report['timings']['automatic_preparation_seconds']=time.perf_counter()-started
            result=replace.run(SimpleNamespace(base_run=args.base_run,contract=str(out/'region.contract.json'),
                bindings=str(out/'bindings.json'),patch_rtl=str(out/'patch.il'),patch_top='__auto_patch',
                patch_is_rtlil=True,new_frontend=str(work/'new.frontend.il'),baseline_from_archive=True,
                new_source=args.new_source,out=str(out/'replacement'),yosys=args.yosys,
                additional_input_hashes=frozen_inputs))
            report.update(status='passed',selected_scopes=scopes,checks=result['checks'],splice=result['splice'])
            report['timings']['replacement']=result['timings']
            report['timings']['incremental_candidate_ready_seconds']=report['timings']['automatic_preparation_seconds']+result['timings']['replacement_ready_seconds']
            for suffix in ('json','v'):shutil.copyfile(out/'replacement'/('incremental.'+suffix),out/('incremental.'+suffix))
        report['artifacts']={str(p.relative_to(out)):digest(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='report.json'}
    except PlanningBlocked as exc:
        report.update(status='blocked',failure={'stage':stage,'code':exc.code,'detail':exc.detail},published_incremental_netlist=False)
    except Exception as exc:
        report.update(status='failed',failure=classify(stage,exc),published_incremental_netlist=False)
    report['timings']['total_seconds']=time.perf_counter()-started
    write_json(out/'report.json',report)
    return report
