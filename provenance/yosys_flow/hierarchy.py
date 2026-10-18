"""Read-only instance expansion of the recorded final hierarchy.

This is electrical elaboration, not synthesis: gate parameters and pin order are
copied, ports are unified bit by bit, and every projected identity names its
recorded definition and instance. The original hierarchical artifact is intact.
"""
from copy import copy, deepcopy
from urllib.parse import quote

from .common import require, unescape, require_combinational


def path_name(parts):
    return '/'.join(quote(unescape(p),safe='') for p in parts)


def instance_catalog(trace, top):
    snapshot = trace.hierarchy
    if not snapshot:
        return {path_name([top]):{'module':top,'parts':[top],'parameters':{},'bindings':{}}}
    modules = snapshot['modules']; result = {}
    def walk(module,parts,ancestors,bindings):
        require(len(parts)<=64 and module not in ancestors, 'recursive or excessively deep hierarchy')
        require(len(result)<10000, 'hierarchy instance budget exceeded')
        item = modules[module]
        path = path_name(parts)
        result[path] = {'module':unescape(module),'parts':parts,'parameters':item['parameters'],
                        'bindings':bindings,'snapshot_event':snapshot['seq']}
        for name,instance in sorted(item['instances'].items()):
            walk(instance['state']['type'],[*parts,unescape(name)],ancestors|{module},
                 {'instance_ref':instance['ref'],'parent':path,'ports':instance['state']['connections']})
    require(unescape(snapshot['top'])==top, 'hierarchy snapshot top differs from run')
    walk(snapshot['top'],[top],set(),{})
    return result


def electrical_design(trace, top):
    """Serializable gate network used for the independent whole-design proof."""
    module=deepcopy({k:v for k,v in trace.design.items() if not k.startswith('_')})
    for net in module['netnames'].values(): net.pop('definition',None)
    return {'modules':{top:module}}


def instance_view(trace, modules, top):
    """Return trace and a deterministic gate-only electrical view of its design."""
    if hasattr(trace,'design'): return trace
    catalog = instance_catalog(trace,top)
    require(top in modules, 'missing final top module')
    for module in modules.values(): require_combinational(module,modules)
    root_path = path_name([top])
    hierarchical = any(c['type'] in modules for m in modules.values() for c in m['cells'].values())
    if not hierarchical:
        trace.design = deepcopy(modules[top]); trace.instance_catalog = catalog
        trace.view_rule = 'recorded_final_module'
        # $scopeinfo describes removed scopes; it has no electrical function.
        trace.final = {r for r in trace.final if unescape(trace.objects[r]['state']['type'])!='$scopeinfo'}
        trace.design['cells'] = {n:c for n,c in trace.design['cells'].items() if c['type']!='$scopeinfo'}
        if len(catalog)>1: trace.sources=deepcopy(trace.sources)
        for source in trace.sources.values() if len(catalog)>1 else ():
            context = unescape(source.get('context_module',source['module']))
            if context==top:
                source['instance'] = path_name([top,*source.get('instance_path',[])])
                require(source['instance'] in catalog, 'flatten source names an unknown original instance')
        for ref in trace.final if len(catalog)>1 else ():
            trace.objects[ref]['instance'] = root_path
        if trace.flatten_events:
            cache = {}
            def connection_targets(ref):
                if ref in cache: return cache[ref]
                row = trace.assignments[ref]
                result = row['original_lhs']
                if row.get('flatten_event'):
                    event = trace.flatten_events[row['flatten_event']]
                    prior = connection_targets(row['cloned_from'])
                    old = trace.assignments[row['cloned_from']]
                    # Cloning captures the surviving offsets of the source.
                    offsets = row.get('source_offsets',list(range(len(result))) if len(prior)==len(result) else old['offsets'])
                    require(len(offsets)==len(result),'flatten connection endpoint offsets unavailable')
                    ports = trace.hierarchy['modules'][event['child_module']]['ports']
                    binding = trace.objects[event['instance']]['state']['connections']
                    result = []
                    for i in offsets:
                        b = prior[i]
                        if isinstance(b,dict):
                            p=ports.get(b['wire'])
                            if p and p['direction']=='output' and b['wire'] in binding and b['offset']<len(binding[b['wire']]):
                                b=binding[b['wire']][b['offset']]
                            else: b={'wire':event['wire_map'][b['wire']],'offset':b['offset']}
                        result.append(b)
                cache[ref]=result
                return result
            trace.design['_connection_users'] = {}
            for ref,row in trace.assignments.items():
                if not row['lhs'] or unescape(row['module'])!=top: continue
                for i,b in enumerate(connection_targets(ref)):
                    if not isinstance(b,dict): continue
                    port=unescape(b['wire']); p=trace.design['ports'].get(port)
                    if p and p['direction']=='output' and b['offset']<len(p['bits']):
                        trace.design['_connection_users'][ref,i]=[{'kind':'module_port','name':port,'offset':b['offset']}]
        return trace

    scopes, parents = {}, {}
    def visit(name,path,ancestors):
        require(name not in ancestors and len(scopes)<10000, 'recursive or oversized final hierarchy')
        module = modules[name]
        scopes[path] = {'module':name,'netlist':module}
        for cell_name,cell in sorted(module['cells'].items()):
            if cell['type'] not in modules: continue
            child = path+'/'+quote(cell_name,safe='')
            parents[child] = (path,cell_name,cell)
            visit(cell['type'],child,ancestors|{name})
    visit(top,root_path,set())

    # No name matching establishes a source relation: retained instance identity
    # is checked against the actual captured instance cell and lifecycle graph.
    adjacency = {}
    for edge in trace.edges: adjacency.setdefault(edge['from'],set()).add(edge['to'])
    def survivors(ref):
        seen,queue = {ref},[ref]
        while queue:
            for out in adjacency.get(queue.pop(),()):
                if out not in seen: seen.add(out); queue.append(out)
        return seen & trace.final
    final_instances = {(unescape(trace.objects[r]['state']['module']),unescape(trace.objects[r]['state']['name'])):r
                       for r in trace.final if unescape(trace.objects[r]['state']['type']) in modules}
    aliases = {root_path:[root_path]}
    for original,info in sorted(catalog.items(),key=lambda p:len(p[1]['parts'])):
        if original==root_path: continue
        parent_original = info['bindings']['parent']
        targets = survivors(info['bindings']['instance_ref'])
        for parent_path,parent_aliases in list(aliases.items()):
            if parent_original not in parent_aliases: continue
            parent_module = scopes[parent_path]['module']
            for ref in targets:
                state = trace.objects[ref]['state']
                if unescape(state['module'])!=parent_module: continue
                child = parent_path+'/'+quote(unescape(state['name']),safe='')
                if child in scopes: aliases.setdefault(child,[]).append(original)
    # -flatten respects user keep_hierarchy. A retained cell may itself have
    # been copied through removed ancestors; follow its recorded source remaps.
    def base_source(s):
        while trace.sources[s].get('flatten_event'): s=trace.sources[s]['cloned_from']
        return s
    for child,(parent,n,_) in sorted(parents.items(),key=lambda p:p[0].count('/')):
        if child in aliases or parent not in aliases: continue
        ref=final_instances[scopes[parent]['module'],n]
        found=[]
        for original,info in catalog.items():
            if 'instance_ref' not in info['bindings']: continue
            base=trace.objects[info['bindings']['instance_ref']]['origins']
            for s in trace.objects[ref]['origins']:
                source=trace.sources[s]
                if base_source(s) not in base: continue
                for ancestor in aliases[parent]:
                    candidate=path_name([*catalog[ancestor]['parts'],*source.get('instance_path',[])])
                    if candidate==info['bindings']['parent']: found.append(original)
        if found: aliases[child]=sorted(set(found))
    require(set(aliases)==set(scopes), 'final instance has no recorded hierarchy identity')

    # Union local electrical IDs ONLY across the actual instance pin bindings.
    uf = {}
    def find(x):
        uf.setdefault(x,x)
        path = []
        while uf[x]!=x: path.append(x); x=uf[x]
        for a in path: uf[a]=x
        return x
    def key(path,b): return ('const',b) if isinstance(b,str) else (path,b)
    def union(a,b):
        a,b = find(a),find(b)
        if a==b: return
        require(not (a[0]=='const' and b[0]=='const'), 'contradictory constants in hierarchy port binding')
        if b[0]=='const' or (a[0]!='const' and repr(b)<repr(a)): a,b=b,a
        uf[b]=a
    for path,item in scopes.items():
        module = item['netlist']
        require(all(p['direction'] in {'input','output'} for p in module['ports'].values()), 'hierarchical inout is unsupported')
        for net in list(module['netnames'].values())+list(module['ports'].values()):
            for b in net['bits']: find(key(path,b))
        for cell in module['cells'].values():
            for bs in cell['connections'].values():
                for b in bs: find(key(path,b))
    for child,(parent,name,cell) in parents.items():
        ports = scopes[child]['netlist']['ports']
        require(set(cell['connections'])<=set(ports), 'unknown hierarchical port')
        require(cell['port_directions']=={p:ports[p]['direction'] for p in cell['connections']}, 'hierarchy port direction mismatch')
        for p,port in ports.items():
            if not cell['connections'].get(p) and port['direction']=='output': continue
            require(p in cell['connections'], 'unconnected hierarchical input is unsupported')
            require(len(port['bits'])==len(cell['connections'][p]), 'hierarchy port width mismatch')
            for a,b in zip(port['bits'],cell['connections'][p]): union(key(child,a),key(parent,b))
    numbers = {}; next_bit = 2
    def bit(path,b):
        nonlocal next_bit
        root = find(key(path,b))
        if root[0]=='const': return root[1]
        if root not in numbers: numbers[root]=next_bit; next_bit+=1
        return numbers[root]
    for k in sorted(uf,key=repr): bit(*k) if k[0]!='const' else None
    def name(path,n): return path+'/'+quote(unescape(n),safe='')
    design = {'attributes':deepcopy(modules[top].get('attributes',{})),'ports':{},'cells':{},'netnames':{}}
    for path,item in sorted(scopes.items()):
        module = item['netlist']
        for n,net in module['netnames'].items():
            design['netnames'][name(path,n)] = {**deepcopy(net),'bits':[bit(path,b) for b in net['bits']],
                'definition':{'module':item['module'],'wire':n,'bits':net['bits'],'instance':path}}
        if path==root_path:
            for n,p in module['ports'].items(): design['ports'][n] = {**deepcopy(p),'bits':[bit(path,b) for b in p['bits']]}
        for n,c in module['cells'].items():
            if c['type'] in modules or c['type']=='$scopeinfo': continue
            design['cells'][name(path,n)] = {**deepcopy(c),'connections':{p:[bit(path,b) for b in bs] for p,bs in c['connections'].items()}}

    view = copy(trace)
    view.design = design; view.instance_catalog = catalog; view.view_rule = 'electrical_instance_expansion_v1'
    view.objects,view.assignments,view.sources,view.process_nodes,view.process_actions = {},{},{},{},{}
    view.final = set(); view.edges=[]; view.events=[]; view.removals=[]; view.process_discards=[]
    view.abc_associations=[]; view.abc_internal=[]; view.regions=[]; view.techmap_bits=[]
    # Equal constant values do not identify an output endpoint. Follow the
    # selected child output's actual port binding before alias unification.
    def port_users(path,port,offset):
        if path==root_path: return [{'kind':'module_port','name':port,'offset':offset}]
        parent,_,inst = parents[path]
        binding = inst['connections'].get(port,[])
        if offset>=len(binding): return []
        initial = binding[offset]
        if not isinstance(initial,int): return None
        queue,seen,result = [(parent,initial)],set(),[]
        while queue:
            current,b = queue.pop()
            if (current,b) in seen: continue
            seen.add((current,b)); mod = scopes[current]['netlist']
            for n,c in mod['cells'].items():
                for p,bs in c['connections'].items():
                    if c['port_directions'][p]!='input': continue
                    for i,v in enumerate(bs):
                        if v!=b: continue
                        if c['type'] in modules:
                            child = current+'/'+quote(n,safe='')
                            child_bit = scopes[child]['netlist']['ports'][p]['bits'][i]
                            if isinstance(child_bit,int): queue.append((child,child_bit))
                        else: result.append({'kind':'cell','name':name(current,n),'port':p,'offset':i})
            for p,pinfo in mod['ports'].items():
                if pinfo['direction']!='output': continue
                for i,v in enumerate(pinfo['bits']):
                    if v!=b: continue
                    if current==root_path: result.append({'kind':'module_port','name':p,'offset':i})
                    else:
                        up,_,inst = parents[current]; bs=inst['connections'].get(p,[])
                        if i<len(bs) and isinstance(bs[i],int): queue.append((up,bs[i]))
        return sorted(result,key=repr)
    design['_connection_users'] = {}
    sid = 0
    for path,item in sorted(scopes.items()):
        module = item['module']
        def local(row): return unescape(row.get('state',row).get('module',''))==module
        roots = {r for r,o in trace.objects.items() if local(o)} | {r for r,o in trace.assignments.items() if local(o)}
        needed = set(roots)
        for e in reversed(trace.edges):
            if e['to'] in needed: needed.add(e['from'])
        needed.update(trace.process_nodes[r]['action'] for r in list(needed)
                      if r in trace.process_nodes and 'action' in trace.process_nodes[r])
        used_sources = {s for s in needed if isinstance(s,int)}
        for objects in (trace.objects,trace.assignments,trace.process_nodes,trace.process_actions):
            for r,o in objects.items():
                if r in needed:
                    used_sources.update(o.get('origins',[])); used_sources.update(o.get('recorded_origins',[]))
        ids = sorted(used_sources)
        source_map = {}
        for s in ids: sid+=1; source_map[s]=sid
        refmap = {r:r+'#'+path for r in needed if isinstance(r,str)}
        invocation_map = {r['invocation_id']:r['invocation_id']+'#'+path for r in trace.regions if unescape(r['module'])==module}
        def rewrite(value,key=None):
            if isinstance(value,str): return refmap.get(value,invocation_map.get(value,value))
            if isinstance(value,list):
                if key in {'origins','recorded_origins','enclosing_controls'}: return [source_map[s] for s in value]
                return [rewrite(x) for x in value]
            if isinstance(value,dict):
                return {k:({role:[source_map[s] for s in ss] for role,ss in v.items()} if k=='roles' else rewrite(v,k)) for k,v in value.items()}
            return value
        def signal(bs): return [{'wire':name(path,b['wire']),'offset':b['offset']} if isinstance(b,dict) else b for b in bs]
        for s in ids:
            row = rewrite(trace.sources[s]); row['id']=source_map[s]
            instances = [path_name([*catalog[p]['parts'],*row.get('instance_path',[])]) for p in aliases[path]]
            require(all(p in catalog for p in instances),'source context has no recorded instance')
            row.update(definition_source_id=s,instance=instances[0],instance_aliases=sorted(instances))
            view.sources[row['id']]=row
        for attr in ('objects','assignments','process_nodes','process_actions'):
            for r,o in getattr(trace,attr).items():
                if r not in needed: continue
                row = rewrite(o); row.update(definition_ref=r,instance=path,instance_aliases=sorted(aliases[path]))
                if attr=='objects' and r in trace.final and local(o) and unescape(o['state']['type']) not in {*modules,'$scopeinfo'}:
                    row['definition_module']=unescape(o['state']['module']); row['definition_name']=unescape(o['state']['name'])
                    state = row['state']; state['module']=top; state['name']=name(path,state['name'])
                    state['connections'] = {p:signal(bs) for p,bs in state['connections'].items()}
                    view.final.add(refmap[r])
                if attr=='assignments' and local(o):
                    row['module']=top
                    for i,lhs in enumerate(o['original_lhs']):
                        if not isinstance(lhs,dict): continue
                        port = unescape(lhs['wire']); info=item['netlist']['ports'].get(port)
                        if info and info['direction']=='output':
                            users=port_users(path,port,lhs['offset'])
                            if users is not None: design['_connection_users'][(refmap[r],i)]=users
                    for k in ('lhs','rhs','original_lhs'): row[k]=signal(row[k])
                elif attr=='assignments': row['lhs']=[]; row['rhs']=[]
                getattr(view,attr)[refmap[r]]=row
        for edge in trace.edges:
            if edge['to'] not in needed: continue
            view.edges.append({**rewrite(edge),'from':source_map[edge['from']] if isinstance(edge['from'],int) else refmap[edge['from']], 'instance':path})
        for attr in ('events','removals','process_discards','techmap_bits','abc_associations'):
            for row in getattr(trace,attr):
                if row.get('ref',row.get('output')) in needed:
                    getattr(view,attr).append({**rewrite(row),'instance':path})
        for attr in ('abc_internal','regions'):
            for row in getattr(trace,attr):
                if row['invocation_id'] in invocation_map:
                    getattr(view,attr).append({**rewrite(row),'instance':path,'definition_invocation_id':row['invocation_id']})
    # Projected creation order is per instance, each with the original event IDs.
    view.edges.sort(key=lambda e:e['event_id'])
    return view
