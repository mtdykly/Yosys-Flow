"""Plan a submodule region from changed assignments and recorded boundaries."""
from collections import defaultdict
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote

from yosys_flow.common import unescape, write_json
from yosys_flow.contracts import SELECTOR_KEYS, fingerprint
from . import ir_graph, rtlil


def selector(location,path):
    return rtlil.source_selector(SimpleNamespace(source=location,name=path),path)


def merge(context,parents):
    unique={p['contract_id']:p for p in parents};parents=list(unique.values())
    ir_graph.check(parents,'no mapped old source objects for changed assignment')
    # Source queries often overlap after group closure. Their selectors remain
    # in statement-plan.json; embedding identical/subset regions as parents only
    # duplicates entire ABC evidence histories without enlarging the region.
    retained=[];covered=[]
    for parent in sorted(parents,key=lambda p:-(len(p['region']['cells'])+len(p['region']['connections']))):
        refs={r['ref'] for r in parent['region']['cells']+parent['region']['connections']}
        if any(refs<=other for other in covered):continue
        retained.append(parent);covered.append(refs)
    parents=retained
    while len(parents)>1:
        parents=[context.build({'kind':'union','parents':parents[i:i+32]}) if len(parents[i:i+32])>1 else parents[i]
                 for i in range(0,len(parents),32)]
    return parents[0]


def source_selectors(context,graph,change,path):
    bit=(change['wire'],change['offset']);kind,obj,_=graph.definition(bit)
    if kind=='cell':
        ir_graph.check(ir_graph.source(obj),'expression has no recorded source range')
        return [selector(ir_graph.source(obj),path)]
    ids=set()
    if kind=='process':
        for action in context.trace.process_actions.values():
            if action['process']!=obj['name']:continue
            if 'instance' in action and path not in action.get('instance_aliases',[action['instance']]):continue
            if any(isinstance(t,dict) and t['wire']==bit[0] and t['offset']==bit[1] for t in action['target']):
                ids.update(action['origins'])
    else:
        names={unescape(bit[0]),path+'/'+quote(unescape(bit[0]),safe='')}
        for row in context.trace.assignments.values():
            if any(isinstance(t,dict) and unescape(t['wire']) in names and t['offset']==bit[1]
                   for t in row['original_lhs']):ids.update(row['origins'])
    result=[]
    for sid in sorted(ids):
        row=context.trace.sources[sid]
        if row['kind'] not in {'AST_ASSIGN','AST_ASSIGN_EQ','AST_ASSIGN_LE'}:continue
        item=dict.fromkeys(SELECTOR_KEYS)
        item.update(source=row['file'],line=row['line'],column=row['column'],
                    end_line=row['end_line'],end_column=row['end_column'],instance=path)
        result.append(item)
    ir_graph.check(result,'no recorded assignment action for '+str(bit))
    return result


class Slice:
    def __init__(self,context,units,coarse,endpoints):
        self.context=context;self.units=units;self.coarse=coarse;self.endpoints=endpoints
        self.aliases=defaultdict(list);self.by_new=defaultdict(list);self.drivers={};self.constants={}
        for path,(old,new) in units.items():
            module=coarse[new.module.name]
            for name,wire in old.wires.items():
                if not wire['public'] or name not in new.wires or wire['width']!=new.wires[name]['width']:continue
                net=module['netnames'].get(unescape(name))
                if not net or len(net['bits'])!=wire['width']:continue
                for offset,bit in enumerate(net['bits']):
                    original=endpoints.bit(path,name,offset)
                    if original in {'0','1'} and (name,offset) in old.inputs and (name,offset) in new.inputs:
                        self.constants[path,bit]=original
                    if not isinstance(original,int):continue
                    # Name/layout aligns a semantic anchor across versions;
                    # its old electrical identity comes from captured records.
                    # Full-cone equality is required before REUSING it as input.
                    old_hash=old.value((name,offset),root=True)
                    new_hash=new.value((name,offset),root=True)
                    row={'instance':path,'wire':name,'offset':offset,'old_bit':original,'new_bit':bit,
                         'old_function_fingerprint':old_hash,'new_function_fingerprint':new_hash,
                         'unchanged_full_definition':old_hash==new_hash}
                    self.aliases[original].append(row);self.by_new[path,bit].append(row)
            for name,cell in module['cells'].items():
                ir_graph.check(cell['type'] not in coarse,'hierarchical coarse cell in local slice')
                for port,bits in cell['connections'].items():
                    if cell['port_directions'][port]!='output':continue
                    for bit in bits:
                        if isinstance(bit,int):
                            key=(path,bit)
                            ir_graph.check(key not in self.drivers,'multiple drivers in coarse module')
                            self.drivers[key]=(path,name)

    def output(self,row):
        candidates=self.aliases.get(row['original_bit'],[])
        if not candidates:return None
        # An old shared wire cannot be rebound to two different new functions.
        ir_graph.check(len({c['new_function_fingerprint'] for c in candidates})==1,
                       'old shared signal has divergent new consumers: '+row['port'])
        candidate=candidates[0]
        return candidate['instance'],candidate['new_bit']

    def build(self,contract):
        context=self.context
        selected={unescape(c['name']) for c in contract['region']['cells']}
        old_driven={b for n in selected for p,bs in context.module['cells'][n]['connections'].items()
                    if context.module['cells'][n]['port_directions'][p]=='output' for b in bs}
        outputs={};observers={};missing=[];binding_evidence=[]
        for row in contract['boundary']:
            if row['direction']!='output':continue
            bit=row['original_bit']
            if row['reason']=='selected_source_connection' and not row['external_users'] and bit not in old_driven:
                observers[row['port']]=bit;continue
            value=self.output(row)
            if value is None:missing.append(row)
            else:outputs[row['port']]=value;binding_evidence.extend(self.aliases[bit])
        if missing:return None,missing

        cuts={};used_cuts={};chosen=set();active=set();visited=set()
        for key,rows in self.by_new.items():
            options={r['old_bit'] for r in rows if r['unchanged_full_definition'] and r['old_bit'] not in old_driven}
            if len(options)==1 and isinstance(key[1],int):cuts[key]=next(iter(options))
        def visit(key):
            if isinstance(key[1],str):return
            if key in self.constants:return
            if key in cuts:
                used_cuts[key]=cuts[key];return
            if key in visited:return
            ir_graph.check(key not in active,'cycle in new local dependency cone');active.add(key)
            ir_graph.check(key in self.drivers,'new dependency has no unchanged recorded input cut: '+str(key))
            cell_key=self.drivers[key];path,name=cell_key
            cell=self.coarse[self.units[path][1].module.name]['cells'][name]
            chosen.add(cell_key)
            for port,bits in cell['connections'].items():
                if cell['port_directions'][port]=='input':
                    for bit in bits:visit((path,bit))
            active.remove(key);visited.add(key)
        for value in outputs.values():visit(value)
        # Copy whole coarse cells. Another output of a copied vector cell may
        # have initially been chosen as an input cut. Internalize that cut and
        # rebuild dependencies; never create an extra driver on an input port.
        for key in list(used_cuts):
            if self.drivers.get(key) in chosen:del used_cuts[key]
        needed_inputs=set(used_cuts.values())|{b for b in observers.values() if isinstance(b,int)}
        boundary_inputs={b['original_bit'] for b in contract['boundary'] if b['direction']=='input'}
        extra=sorted(needed_inputs-boundary_inputs)
        if extra:contract=context.build({'kind':'extend_inputs','parent':contract,'input_bits':extra})
        input_rows={b['original_bit']:b for b in contract['boundary'] if b['direction']=='input'}
        module={'attributes':{},'ports':{},'cells':{},'netnames':{}};next_bit=2;signals={};external={}
        def allocate():
            nonlocal next_bit
            value=next_bit;next_bit+=1
            module['netnames']['__slice_'+str(value)]={'hide_name':1,'bits':[value],'attributes':{}}
            return value
        for bit,row in input_rows.items():
            external[bit]=allocate()
            module['ports'][row['port']]={'direction':'input','bits':[external[bit]]}
        for key,bit in used_cuts.items():
            signals[key]=external[bit]
            binding_evidence.extend(r for r in self.by_new[key] if r['old_bit']==bit and r['unchanged_full_definition'])
        def signal(key):
            if isinstance(key[1],str):return key[1]
            if key in self.constants:return self.constants[key]
            if key not in signals:signals[key]=allocate()
            return signals[key]
        for i,(path,name) in enumerate(sorted(chosen)):
            cell=deepcopy(self.coarse[self.units[path][1].module.name]['cells'][name])
            cell['connections']={p:[signal((path,b)) for b in bits] for p,bits in cell['connections'].items()}
            module['cells']['__slice_cell_'+str(i)]=cell
        for row in contract['boundary']:
            port=row['port']
            if row['direction']=='output':
                bit=observers.get(port)
                value=(external[bit] if isinstance(bit,int) else bit) if port in observers else signal(outputs[port])
                module['ports'][port]={'direction':'output','bits':[value]}
            module['netnames'][port]={'hide_name':0,'bits':module['ports'][port]['bits'],'attributes':{}}
        bindings={'schema_version':1,'contract_id':contract['contract_id'],
            'inputs':{b['port']:[b['port']] for b in contract['boundary'] if b['direction']=='input'},
            'outputs':{b['port']:[b['port']] for b in contract['boundary'] if b['direction']=='output'}}
        spec={'selection_strategy':'statement_dependency_slice','scopes':sorted(self.units),
              'additional_input_bits':extra,'endpoint_evidence':self.endpoints.evidence,
              'semantic_anchor_bindings':binding_evidence,'reused_input_cut_count':len(used_cuts),
              'recorded_constant_inputs':[{'instance':p,'new_bit':b,'value':v} for (p,b),v in self.constants.items()],
              'local_coarse_cells':[{'instance':p,'name':n} for p,n in sorted(chosen)],
              'local_coarse_cell_count':len(chosen),
              'whole_changed_modules_coarse_cell_count':sum(len(self.coarse[n.module.name]['cells']) for _,n in self.units.values()),
              'process_semantics':'Yosys proc output cone; conditions, priority and data dependencies retained',
              'minimal_region_claimed':False}
        return (contract,{'modules':{'__auto_patch':module}},bindings,spec),[]


def plan(context,old,new,oc,nc,coarse,scopes,report,work,yosys):
    # Import at call time: automatic owns the public fallback policy.
    from .automatic import Endpoints
    from .replace import run_yosys
    units={};parents=[];mapped_selectors={}
    report.update(rule='changed_pre_proc_assignments_v1',seeds=[],expansions=[])
    try:
        endpoints=Endpoints(context)
        for path in scopes:
            a,b=ir_graph.Graph(old[oc[path]],old),ir_graph.Graph(new[nc[path]],new)
            ir_graph.check(a.module.ports()==b.module.ports(),'local interface changed')
            units[path]=(a,b)
            for change in ir_graph.differences(a,b):
                endpoint=endpoints.bit(path,change['wire'],change['offset'])
                ir_graph.check(endpoint not in {'0','1','x','z'},
                               'changed constant assignment requires consumer rebinding')
                selectors=source_selectors(context,a,change,path)
                report['seeds'].append({**change,'instance':path,'selectors':selectors})
                mapped=False
                for item in selectors:
                    key=fingerprint(item)
                    if key not in mapped_selectors:
                        query=context.query(item)
                        mapped_selectors[key]=bool(query['final_cells'])
                        if mapped_selectors[key]:
                            parents.append(context.build({'kind':'source_cells','selector':item}))
                    mapped |= mapped_selectors[key]
                ir_graph.check(mapped,'changed assignment has no mapped final gates; consumer rebinding or module fallback required')
        contract=merge(context,parents);report['seed_cell_count']=len(contract['region']['cells'])
        slicer=Slice(context,units,coarse,endpoints)
        refs={unescape(context.trace.objects[r]['state']['name']):r for r in context.trace.final}
        for _ in range(32):
            ir_graph.check(contract['verification']['status']=='verified_old_region',
                           'old statement region is not proven: '+str(contract['verification']['blockers']))
            result,missing=slicer.build(contract)
            if result:
                contract,design,bindings,spec=result;context.validate(contract)
                write_json(work/'statement-slice.json',design)
                run_yosys(yosys,work,'statement-slice',
                    'read_json statement-slice.json\nhierarchy -check -top __auto_patch\ncheck -assert\nwrite_rtlil statement-slice.il')
                report.update(status='ready',selected_cell_count=len(contract['region']['cells']),
                              copied_coarse_cell_count=spec['local_coarse_cell_count'])
                return contract,(work/'statement-slice.il').read_text(),bindings,spec
            selected={c['ref'] for c in contract['region']['cells']};added=set()
            for row in missing:
                added.update(refs[u['name']] for u in row['external_users'] if u['kind']=='cell' and u['name'] in refs)
            added-=selected
            ir_graph.check(added,'unavailable old output identity cannot be closed within the statement scope')
            report['expansions'].append({'reason':'old_output_cut_disappeared',
                'ports':[r['port'] for r in missing],'added_cell_refs':sorted(added)})
            extra=[]
            for ref in sorted(added):
                item=dict.fromkeys(SELECTOR_KEYS);item['object']=ref
                extra.append(context.build({'kind':'selection','selector':item}))
            contract=merge(context,[contract,*extra])
        raise ir_graph.Unsupported('statement region expansion budget exceeded')
    except ir_graph.Unsupported as exc:
        report.update(status='module_fallback',reason=str(exc))
        return None
    finally:write_json(work.parent/'statement-plan.json',report)
