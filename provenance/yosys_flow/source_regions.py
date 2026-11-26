"""Independent old-source cut proofs, not a claim of complete ABC history.

The reference is reconstructed only from manifest-bound frontend.il.  A
candidate gate region is acceptable only when every cut output has an actual
recorded source anchor and SAT proves equality and definedness at independent
cut inputs.  No new RTL or full-new synthesis is used by this certificate.
"""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import quote

from .common import PIN, TraceError, digest, require, require_combinational, token
from .source_endpoints import Endpoints
from .region_functions import Circuit, LogicDAG, UnsupportedFunction


class MissingInputs(TraceError):
    def __init__(self,bits):
        self.bits=sorted(bits)
        super().__init__('source cut input anchors unavailable: '+str(self.bits))


def command(binary, directory, script):
    from .online_evidence import current
    policy=current();timeout=min(60,policy.remaining()) if policy else 60
    try:
        result = subprocess.run([binary, '-Q', '-T', '-p', script], cwd=directory,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if policy:policy.block('old_region_tool_timeout',timeout_seconds=timeout)
        raise
    require(result.returncode == 0, 'source cut tool error: '+result.stderr[-1200:])
    return result.stdout


def flatten(modules, top):
    """Electrical expansion with instance-qualified anchors; no name guessing."""
    cells, aliases, inputs, parent = {}, {}, set(), {}
    def allocate():
        value=len(parent)+2;parent[value]=value;return value
    def root(value):
        if isinstance(value,str):return value
        path=[]
        while isinstance(value,int) and parent[value]!=value:
            path.append(value);value=parent[value]
        for old in path:parent[old]=value
        return value
    def join(a,b):
        a,b=root(a),root(b)
        if a==b:return
        if isinstance(a,str) and isinstance(b,str):
            require(False,'inconsistent constant source bindings')
        if isinstance(a,int):parent[a]=b
        else:parent[b]=a
    def visit(name,path,binding,active):
        require(name not in active,'recursive source hierarchy')
        module=modules[name];require_combinational(module,modules);mapping={}
        def bit(value):
            if isinstance(value,str):return value
            if value not in mapping:mapping[value]=allocate()
            return mapping[value]
        for port,info in module['ports'].items():
            require(info['direction'] in {'input','output'},'source inout is unsupported')
            if binding is None:continue
            require(port in binding and len(binding[port])==len(info['bits']),'source port width differs')
            for a,b in zip(info['bits'],binding[port]):join(bit(a),b)
        for wire,net in module['netnames'].items():
            if not net.get('hide_name',False):
                for offset,value in enumerate(net['bits']):aliases[path,wire,offset]=bit(value)
        if binding is None:
            for port in module['ports'].values():
                if port['direction']=='input':inputs.update(bit(b) for b in port['bits'])
        for local,cell in module['cells'].items():
            connections={p:[bit(b) for b in bs] for p,bs in cell['connections'].items()}
            child=path+'/'+quote(local,safe='')
            if cell['type'] in modules:visit(cell['type'],child,connections,active|{name})
            else:cells[child]={**deepcopy(cell),'connections':connections}
    visit(top,quote(top,safe=''),None,set())
    for cell in cells.values():
        cell['connections']={p:[root(b) for b in bs] for p,bs in cell['connections'].items()}
    return cells,{k:root(v) for k,v in aliases.items()},{root(b) for b in inputs}


def archived(context, directory, binary):
    if hasattr(context,'_source_coarse'):return context._source_coarse
    store=getattr(context,'proof_evidence',None)
    if store is not None and getattr(store,'source',None) is not None:
        store.source.metrics['lowering_reused']+=1
        context._source_coarse=store.source
        return store.source
    path=context.directory/'frontend.il'
    require(digest(path)==context.baseline['artifacts']['frontend.il']['sha256'],
            'archived source frontend changed')
    command(binary,directory,'read_rtlil '+token(path)+
            '; proc; opt_expr; techmap; opt_expr; opt_clean; check -assert; write_json coarse.json')
    modules=json.loads((directory/'coarse.json').read_text())['modules']
    context._source_coarse=flatten(modules,context.baseline['module'])
    return context._source_coarse


def make_cut(context, document, source):
    from .source_cache import StoredSource
    indexed=isinstance(source,StoredSource)
    if indexed:
        cells,primary=source.cells,source.primary
        by_final,anchors=source.boundary(context,[r['original_bit'] for r in document['boundary']])
    else:
        cells,aliases,primary=source
        resolver=Endpoints(context);by_final=defaultdict(set);anchors=defaultdict(list)
        for (path,wire,offset),value in sorted(aliases.items()):
            for final,inverted in resolver.signals(path,wire,offset):
                if isinstance(final,int):
                    by_final[final].add((value,inverted))
                    anchors[final].append({'instance':path,'wire':wire,'offset':offset,'source_bit':value,'inverted':inverted})
    inputs=[b for b in document['boundary'] if b['direction']=='input']
    outputs=[b for b in document['boundary'] if b['direction']=='output']
    require(inputs and outputs,'source cut requires inputs and outputs')
    require(not document['region']['connections'],'source cut connection rebinding is unsupported')
    ports={};cuts={};next_bit=2;mapping={};actual_mapping={};reference_outputs={};evidence=[];inverters={}
    manager=LogicDAG()
    primaries=sorted(b for b in primary if isinstance(b,int))
    full=source.circuit(manager) if indexed else Circuit(cells,dict(zip(primaries,manager.variables(len(primaries)))),manager)
    def allocate():
        nonlocal next_bit
        value=next_bit;next_bit+=1;return value
    def invert(value):
        if isinstance(value,str):return {'0':'1','1':'0','x':'x','z':'x'}[value]
        if value not in inverters:inverters[value]=allocate()
        return inverters[value]
    for row in inputs:
        final=row['original_bit'];values=by_final[final]
        if not values or not all(isinstance(b,int) for b,phase in values):raise MissingInputs([final])
        if len(values)>1:
            try:equal=len({full.value(b)^int(phase) for b,phase in values})==1
            except (TraceError,UnsupportedFunction,RecursionError):equal=False
            if not equal:raise MissingInputs([final])
        value=allocate()
        require(not any(b in cuts for b,phase in values),'distinct boundary inputs alias in source')
        for old,phase in values:cuts[old]=invert(value) if phase else value
        actual_mapping[final]=value
        ports[row['port']]={'direction':'input','bits':[value]}
        evidence.append({'port':row['port'],'direction':'input','final_bit':final,'anchors':anchors[final]})
    for row in outputs:
        final=row['original_bit'];values=by_final[final]
        require(values,'source output identity unavailable: '+row['port'])
        for i,(old,phase) in enumerate(sorted(values,key=str)):
            name=row['port']+'__anchor_'+str(i)
            reference_outputs[name]=old
        evidence.append({'port':row['port'],'direction':'output','final_bit':final,'anchors':anchors[final]})
    drivers=source.drivers if indexed else {}
    if not indexed:
        for name,cell in cells.items():
            for port,bs in cell['connections'].items():
                if cell['port_directions'][port]!='output':continue
                for bit in bs:
                    if isinstance(bit,int):
                        require(bit not in drivers,'source has multiple drivers');drivers[bit]=name
    selected=set();active=set();seen=set()
    stack=[(bit,False) for bit in reversed(list(reference_outputs.values()))]
    while stack:
        bit,done=stack.pop()
        if done:
            active.remove(bit);seen.add(bit);continue
        if isinstance(bit,str):
            require(bit in {'0','1','x','z'},'invalid source constant');continue
        if bit in cuts or bit in seen:continue
        require(bit not in active,'cycle in source cut')
        if bit in primary:
            # A coarse operator can carry unused bits. Quantify any remaining
            # old PI independently on BOTH sides; SAT must prove the output
            # does not depend on it, rather than silently tying it to zero.
            value=allocate();cuts[bit]=value
            ports['aux_'+str(bit)]={'direction':'input','bits':[value]}
            continue
        require(bit in drivers,'source cut has an undriven signal')
        active.add(bit);name=drivers[bit];selected.add(name)
        from .online_evidence import current
        if current():current().check_nodes(len(selected),'source_cut_cells')
        dependencies=[b for p,bs in cells[name]['connections'].items()
                      if cells[name]['port_directions'][p]=='input' for b in bs]
        stack.append((bit,True))
        stack.extend((child,False) for child in reversed(dependencies))
    # Never cut a bit driven by a copied vector cell: that creates an input
    # driver and would constrain the independent proof inputs accidentally.
    require(not any(drivers.get(b) in selected for b in cuts),'source vector output overlaps independent cut')
    def signal(bit):
        if isinstance(bit,str):return bit
        if bit in cuts:return cuts[bit]
        if bit not in mapping:mapping[bit]=allocate()
        return mapping[bit]
    gold={'attributes':{},'ports':deepcopy(ports),'cells':{},'netnames':{}}
    gate={'attributes':{},'ports':deepcopy(ports),'cells':{},'netnames':{}}
    for i,name in enumerate(sorted(selected)):
        cell=deepcopy(cells[name]);cell['connections']={p:[signal(b) for b in bs] for p,bs in cell['connections'].items()}
        gold['cells']['c'+str(i)]=cell
    def actual(bit):
        if isinstance(bit,str):return bit
        if bit not in actual_mapping:actual_mapping[bit]=allocate()
        return actual_mapping[bit]
    for name,cell in document['region']['recorded_cell_functions'].items():
        gate['cells'][name]={**deepcopy(cell),'attributes':{},'hide_name':1,
                            'connections':{p:[actual(b) for b in bs] for p,bs in cell['connections'].items()}}
    for row in outputs:
        for i,(old,phase) in enumerate(sorted(by_final[row['original_bit']],key=str)):
            name=row['port']+'__anchor_'+str(i)
            value=signal(old)
            gold['ports'][name]={'direction':'output','bits':[invert(value) if phase else value]}
            gate['ports'][name]={'direction':'output','bits':[actual(row['original_bit'])]}
    for value,target in inverters.items():
        gold['cells']['__anchor_not_'+str(target)]={'type':'$_NOT_','parameters':{},'attributes':{},'hide_name':1,
            'port_directions':{'A':'input','Y':'output'},'connections':{'A':[value],'Y':[target]}}
    for module in (gold,gate):
        module['netnames']={n:{'hide_name':0,'bits':p['bits'],'attributes':{}} for n,p in module['ports'].items()}
    return {'modules':{'gold':gold,'gate':gate}},evidence


def prove_design(binary, directory, design):
    """Prove all shared-interface outputs equal AND defined, with no assumptions."""
    (directory/'cut.json').write_text(json.dumps(design))
    command(binary,directory,'read_json cut.json; miter -equiv -make_outputs -flatten gold gate miter; '
            'hierarchy -check -top miter; check -assert; write_json miter.json')
    miter=json.loads((directory/'miter.json').read_text())
    module=miter['modules']['miter']
    values=[b for p,v in module['ports'].items() if p.startswith(('gold_','gate_')) for b in v['bits']]
    ints=[b for c in module['cells'].values() for bs in c['connections'].values() for b in bs if isinstance(b,int)]
    ints.extend(b for rows in (module['ports'],module['netnames']) for row in rows.values()
                for b in row['bits'] if isinstance(b,int))
    reduced,flag=max(ints,default=1)+1,max(ints,default=1)+2
    params=lambda **kw:{k:format(v,'032b') for k,v in kw.items()}
    module['cells']['__defined_reduce']={'type':'$reduce_xor','parameters':params(A_SIGNED=0,A_WIDTH=len(values),Y_WIDTH=1),
        'port_directions':{'A':'input','Y':'output'},'connections':{'A':values,'Y':[reduced]},'attributes':{},'hide_name':1}
    module['cells']['__defined_check']={'type':'$eqx','parameters':params(A_SIGNED=0,B_SIGNED=0,A_WIDTH=1,B_WIDTH=1,Y_WIDTH=1),
        'port_directions':{'A':'input','B':'input','Y':'output'},'connections':{'A':[reduced],'B':['x'],'Y':[flag]},'attributes':{},'hide_name':1}
    module['ports']['undefined']={'direction':'output','bits':[flag]}
    module['netnames']['undefined']={'hide_name':0,'bits':[flag],'attributes':{}}
    (directory/'checked.json').write_text(json.dumps(miter))
    from .online_evidence import current
    seconds=max(1,min(50,int(current().remaining()))) if current() else 50
    log=command(binary,directory,'read_json checked.json; hierarchy -top miter; check -assert; '
        f'sat -enable_undef -timeout {seconds} -set-def-inputs -prove trigger 0 -prove undefined 0')
    require(log.splitlines().count('SAT proof finished - no model found: SUCCESS!')==1,
            'source cut SAT did not prove equality and definedness')


def prove(context, document):
    from .contracts import fingerprint
    from .partition_store import region_certificate
    certificate=region_certificate(context,{c['ref'] for c in document['region'].get('cells',[])})
    if certificate and certificate.get('source_proof',{}).get('status')=='verified' and \
            certificate.get('source_cut_sha256')==cut_signature(document):
        saved=certificate['source_proof']
        require(saved['all_outputs'] and saved['frontend_sha256']==context.baseline['artifacts']['frontend.il']['sha256'],
                'cached source proof is bound to a different frontend')
        return {**saved,'reused_from_authenticated_partition':certificate['id']}
    binary=context.manifest['tool']['stock_executable']
    result={'rule':'archived_source_independent_cut_sat_v1','status':'not_proven',
            'frontend_sha256':context.baseline['artifacts']['frontend.il']['sha256'],
            'all_outputs':False,'complete_causal_history':False,
            'semantic_domain':'defined_two_state_combinational',
            'command_timeout_seconds':60,'solver_timeout_seconds':50,
            'source_lowering':'proc; opt_expr; techmap; opt_expr; opt_clean',
            'abc_invocations':0}
    @contextmanager
    def timing(name):
        started=time.perf_counter()
        try:yield
        finally:
            values=context.__dict__.setdefault('proof_timings',{})
            values[name]=values.get(name,0.0)+time.perf_counter()-started
    try:
        version=subprocess.check_output([binary,'-V'],text=True,timeout=10).strip()
        require(PIN in version,'source proof requires pinned Yosys')
        result['tool_sha256']=digest(binary)
        with tempfile.TemporaryDirectory(prefix='source-cut-') as tmp:
            directory=Path(tmp)
            with timing('old_source_representation_seconds'):
                source=archived(context,directory,binary)
            with timing('old_source_cut_assembly_seconds'):
                design,anchors=make_cut(context,document,source)
            with timing('old_source_miter_and_sat_seconds'):
                prove_design(binary,directory,design)
            result.update(status='verified',all_outputs=True,anchors=anchors,
                          cut_sha256=fingerprint(design),output_count=sum(p['direction']=='output'
                              for p in design['modules']['gold']['ports'].values()))
    except (TraceError,OSError,subprocess.SubprocessError,RecursionError) as exc:
        result['reason']=str(exc) if not isinstance(exc,RecursionError) else 'source_function_depth_limit'
        if isinstance(exc,MissingInputs):result['missing_input_bits']=exc.bits
    return result


def cut_signature(document):
    from .contracts import fingerprint
    return fingerprint({'boundary':[{k:r[k] for k in ('port','direction','original_bit')}
                                     for r in document['boundary']],
        'connections':document['region'].get('connections',[]),
        'cells':{n:{k:c[k] for k in ('type','parameters','port_directions','connections')}
                 for n,c in document['region']['recorded_cell_functions'].items()}})
