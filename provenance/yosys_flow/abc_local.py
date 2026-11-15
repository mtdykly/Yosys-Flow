"""Independent ABC cut proofs when whole-region Boolean signatures fail.

Structural expressions supply positive function matches only. Selected mapping
cuts and source cuts are then checked under independent boundary variables.
No object identity, causal edge, or missing function is inferred from a name.
"""
from collections import Counter, defaultdict

from .abc_internal import Unsupported, cut_domain, evaluate, prove_cut, verify_input_blif
from .boolean import BDD, ProofBudgetExceeded
from .common import require
from .structural import Expressions

MAX_LOCAL_NODES = 4096
MAX_LOCAL_INPUTS = 64
MAX_ROOT_MATCHES = 32
LOCAL_BDD_NODES = 10000
LOCAL_BDD_WORK = 100000


def cone(net, root, stops, *, limit=MAX_LOCAL_NODES):
    """A bounded, iterative postorder; stop nodes are independent inputs."""
    order, boundary, seen, active = [], set(), set(), set()
    stack = [(root, False)]
    while stack:
        i, done = stack.pop()
        if i in seen: continue
        if i != root and i in stops:
            boundary.add(i); seen.add(i); continue
        if done:
            active.remove(i); seen.add(i); order.append(i); continue
        require(i not in active, 'cycle in ABC local cut')
        if len(seen)+len(active) >= limit:
            raise ProofBudgetExceeded('local_cut_node_limit_exceeded')
        node = net.nodes[i]
        if node['op']=='pi':
            if i not in stops: raise Unsupported('unbound_local_input')
            boundary.add(i); seen.add(i); continue
        active.add(i); stack.append((i, True))
        stack.extend((f//2, False) for f in reversed(node['fanins']))
    return order, boundary


def value(net, order, bindings, mask):
    values = dict(bindings)
    for i in order:
        n = net.nodes[i]
        values[i] = evaluate(n, [values[f//2] ^ (mask if f&1 else 0) for f in n['fanins']], mask)
    return values


def domain(count, resources):
    if count > MAX_LOCAL_INPUTS: raise ProofBudgetExceeded('local_cut_input_limit_exceeded')
    managers = []
    try:
        if count > 10:
            manager=BDD(node_limit=LOCAL_BDD_NODES,work_limit=LOCAL_BDD_WORK)
            managers.append(manager)
            return manager.variables(count),manager.one
        return cut_domain(count, bdds=managers)
    finally:
        # Managers are retained only until this region finishes; callers append
        # their statistics, not the full unique tables, to the report.
        resources.extend(managers)


def mapping_context(rows, nets, cuts, imports):
    """Bind exported objects through actual copies, before any global proof."""
    conversions = [r for r in rows if r['kind']=='gia_to_ntk']
    exports = [r for r in rows if r['kind']=='ntk_to_netlist']
    if len(conversions)!=1 or len(exports)!=1: raise Unsupported('ambiguous_mapping_conversion')
    gm, nm = conversions[0], exports[0]
    require(len({b for a,b in gm['links']})==len(gm['links']) and
            len({a for a,b in nm['links']})==len(nm['links']) and
            len({b for a,b in nm['links']})==len(nm['links']), 'ABC conversion duplicates an object binding')
    final = next(n for n in nets.values() if n.record['kind']=='export_network')
    def shape(net): return {i:{k:v for k,v in n.items() if k!='name'} for i,n in net.nodes.items()}
    require(shape(nets[gm['after']])==shape(nets[nm['before']]), 'ABC mapped network changed before export')
    require(shape(nets[nm['after']])==shape(final), 'ABC netlist changed during export')
    mapped = nets[gm['after']]
    expected = {lit for lit,i in gm['links'] if mapped.nodes[i]['op']=='sop' and mapped.nodes[i]['fanins']}
    require({c['root_literal'] for c in cuts}==expected and len(cuts)==len(expected),
            'ABC selected mapping cut set differs from converted gates')
    literal_by_node = {i:lit for lit,i in gm['links']}
    clones = {r['seq']:r for r in rows if r['kind']=='mapped_clone'}
    require({event for old,new,event in gm.get('clones',[])}==set(clones), 'ABC mapped clone links are incomplete')
    for old,new,event in gm.get('clones',[]):
        r=clones[event]
        require(r['old']==old and r['new']==new and old in literal_by_node and new not in literal_by_node,
                'ABC mapped clone lacks actual parent')
        require(all({k:v for k,v in nets[r['snapshot']].nodes[i].items() if k!='name'}==
                    {k:v for k,v in mapped.nodes.get(i,{}).items() if k!='name'} for i in (old,new)),
                'ABC mapped clone changed before conversion')
        literal_by_node[new]=literal_by_node[old]
    copied = dict(nm['links'])
    literals = {copied[i]:lit for i,lit in literal_by_node.items() if i in copied}
    gates = {}
    for n in final.nodes.values():
        if n.get('name') and n['op']=='buf' and len(n['fanins'])==1:
            driver=n['fanins'][0]//2
            if final.nodes[driver].get('gate'): gates[n['name']]=driver
    by_name = {}
    for name, imported in imports.items():
        bits=imported['ports'].get('\\Y',[])
        if len(bits)!=1 or not isinstance(bits[0],dict): continue
        wire=bits[0]['wire'].lstrip('\\')
        require(wire in gates, 'unbound ABC exported gate')
        target=gates[wire]
        require(final.nodes[target]['gate']==imported['type'].lstrip('\\'), 'ABC exported gate type mismatch')
        by_name[name]=target
    checked=set()
    for cut in cuts:
        if cut['snapshot'] not in checked:
            require(shape(nets[cut['snapshot']])==shape(nets[gm['before']]), 'ABC GIA changed after mapping')
            checked.add(cut['snapshot'])
    return final, by_name, literals, {c['root_literal']:c for c in cuts}, [gm['seq'],nm['seq'],*clones]


def check_mapping(target, final, literals, cut, nets, resources):
    """Prove the selected GIA cut and exported gate with independent leaves."""
    def mapped_literal(lit):
        i, phase = lit//2, lit&1
        seen=set()
        while i not in literals:
            require(i not in seen, 'cycle in ABC export conversion')
            seen.add(i); n=final.nodes[i]
            if n['op']!='buf' or len(n['fanins'])!=1: raise Unsupported('unbound_mapping_fanin')
            phase ^= n['fanins'][0]&1; i=n['fanins'][0]//2
        return literals[i]^phase
    gate=final.nodes[target]
    fanins=[mapped_literal(f) for f in gate['fanins']]
    leaves=cut['leaves']
    # A different literal can represent an equivalent leaf, but that requires
    # additional evidence. Do not upgrade it using a guessed object binding.
    if Counter(fanins)!=Counter(leaves): raise Unsupported('mapping_leaf_binding_requires_function_proof')
    ids=sorted({f//2 for f in leaves})
    variables,mask=domain(len(ids),resources)
    bindings=dict(zip(ids,variables))
    net=nets[cut['snapshot']]
    try:
        order,_=cone(net,cut['root_literal']//2,set(ids))
    except Unsupported as exc:
        if str(exc)!='unbound_local_input': raise
        # A selected choice cut need not dominate the original GIA root.
        # Prove that root and its captured mapped leaves agree over their
        # bounded union of PI support. This is a local function check, never an
        # invented copy edge or a claim that those leaves are independent.
        orders=[];support=set();seen=set()
        for root in [cut['root_literal']//2,*ids]:
            order,leaves=cone(net,root,set(net.pis))
            support.update(leaves)
            for i in order:
                if i not in seen: seen.add(i);orders.append(i)
            if len(seen)>MAX_LOCAL_NODES: raise ProofBudgetExceeded('local_cut_node_limit_exceeded')
        variables,mask=domain(len(support),resources)
        evaluated=value(net,orders,dict(zip(sorted(support),variables)),mask)
        actual=evaluated[cut['root_literal']//2] ^ (mask if cut['root_literal']&1 else 0)
        expected=evaluate(gate,[evaluated[f//2] ^ (mask if f&1 else 0) for f in fanins],mask)
        require(actual==expected, 'ABC selected choice cut changes bounded function')
        return
    evaluated=value(net,order,bindings,mask)
    actual=evaluated[cut['root_literal']//2] ^ (mask if cut['root_literal']&1 else 0)
    expected=evaluate(gate,[bindings[f//2] ^ (mask if f&1 else 0) for f in fanins],mask)
    require(actual==expected, 'ABC selected mapping cut changes independent function')


def analyze_local(rows, nets, cuts, region, imports, input_blif=None, progress=None, *, exclude=()):
    proof, reasons, attempts, resources = {}, {}, [], []
    report={'method':'independent_structural_boundaries', 'complete_rewrite_history':False,
            'global_function_proof_required':False, 'attempts':attempts,
            'limits':{'local_nodes':MAX_LOCAL_NODES,'local_inputs':MAX_LOCAL_INPUTS,
                      'root_matches':MAX_ROOT_MATCHES,'bdd_nodes_per_region':LOCAL_BDD_NODES,
                      'bdd_work_per_region':LOCAL_BDD_WORK}}
    def reject(name, exc, phase):
        reasons[name]=[{'reason':str(exc),'phase':phase}]
    try:
        if progress: progress('abc_independent_mapping')
        first=next(n for n in nets.values() if n.record['kind']=='network')
        require(set(first.names)=={f'ys__n{i}' for i in region['input_ports']}, 'ABC internal input boundary mismatch')
        if input_blif is not None: verify_input_blif(first,input_blif)
        original_names={n.get('name'):i for i,n in first.nodes.items() if n.get('name')}
        original={original_names[f'ys__n{n["id"]}']:n['cell'] for n in region['input_nodes'] if n['cell']}
        require(set(original.values())==set(region['inputs']), 'ABC initial objects differ from extracted cells')
        final,gate_by_name,literals,by_literal,conversions=mapping_context(rows,nets,cuts,imports)
        mapping={}
        for name,target in gate_by_name.items():
            if name in exclude: continue
            managers=[]
            try:
                cut=by_literal.get(literals.get(target))
                if cut is None: raise Unsupported('selected_mapping_relation_missing')
                check_mapping(target,final,literals,cut,nets,managers)
                mapping[name]=[cut['seq']]
            except (Unsupported,ProofBudgetExceeded) as exc: reject(name,exc,'independent_mapping')
            finally: resources.extend(m.statistics() for m in managers)
        report['verified_mapping_cuts']=len(mapping)
        if progress: progress('abc_structural_boundaries')
        algebra=Expressions()
        bindings={n:algebra.variable(n) for n in first.names}
        names=final.names
        if not all(names):
            aliases=defaultdict(set)
            for n in final.nodes.values():
                if n.get('name') in bindings and n['op']=='buf' and len(n['fanins'])==1:
                    aliases[n['fanins'][0]].add(n['name'])
            require(all(len(aliases[2*i])==1 for i in final.pis), 'ABC exported input alias binding mismatch')
            names=[next(iter(aliases[2*i])) for i in final.pis]
        require(len(names)==len(bindings) and set(names)==set(bindings), 'ABC exported input binding mismatch')
        initial=first.functions([bindings[n] for n in first.names],algebra.one)
        fv=final.functions([bindings[n] for n in names],algebra.one)
        roots=defaultdict(list)
        for root in original: roots[initial[root].literal//2].append(root)
        report['structural_expression_nodes']=len(algebra.unique)
        if progress: progress('abc_independent_source_cuts')
        for name,target in gate_by_name.items():
            if name not in mapping: continue
            candidates=roots.get(fv[target].literal//2,())
            if len(candidates)>MAX_ROOT_MATCHES:
                reject(name,'local_root_match_limit_exceeded','source_cut'); continue
            matches=[]
            for root in candidates:
                local_managers=[]; failures=[]
                polarity=int(initial[root]!=fv[target])
                gate=final.nodes[target]
                boundary=[fv[f//2] ^ (algebra.one if f&1 else 0) for f in gate['fanins']]
                match=prove_cut(first,root,initial,boundary,gate,polarity,algebra.one,
                                local_bdds=local_managers,failures=failures,node_limit=MAX_LOCAL_NODES)
                attempts.append({'kind':'single','mapped_node':name,'entry_root':root,
                                 'status':'verified' if match else 'unknown','failures':failures})
                resources.extend(m.statistics() for m in local_managers)
                if match:
                    covered,boundaries=match
                    members=sorted({original[i] for i in covered if i in original})
                    if members: matches.append({'entry_root':root,'input_cells':members,'output_inverted':bool(polarity),
                        'boundary_matches':boundaries,'mapping_events':mapping[name],
                        'function_binding':'exact_structural_expression_at_captured_pis'})
            if matches:
                proof[name]={'inputs':sorted({c for m in matches for c in m['input_cells']}),'matches':matches,
                    'export_snapshot':final.record['seq'],'export_node':target,'conversion_events':conversions,
                    'method':'structural_boundary_and_independent_mapping_cut','precision':'abc_internal_cut',
                    'complete_rewrite_history':False}
            else: reasons.setdefault(name,[{'reason':'no_independent_structural_cut','phase':'source_cut'}])
        # An output's identity is captured by extraction/export, not guessed by
        # internal name. It anchors a bounded joint proof when rewrites erased
        # an internal root. Every retained frontier function is checked below.
        if progress: progress('abc_independent_output_groups')
        joint=output_groups(first,final,original,initial,fv,algebra,gate_by_name,mapping,proof,
                            region,attempts,resources)
        for name,item in joint.items():
            item['conversion_events']=conversions
            proof[name]=item
    except (Unsupported,ProofBudgetExceeded) as exc:
        report['fallback_reason']=str(exc)
    report.update(proved_import_nodes=len(proof),function_resources=resources,
                  unproved_reasons={n:reasons.get(n,[{'reason':report.get('fallback_reason','no_local_proof'),
                       'phase':'independent_local'}]) for n in imports if n not in proof and n not in exclude})
    return proof,report


def output_groups(first, final, original, initial, fv, algebra, gate_by_name, mapping, singles,
                  region, attempts, resources):
    entry_outputs={first.nodes[i].get('name'):i for i in first.pos}
    final_outputs={final.nodes[i].get('name'):i for i in final.pos}
    # Export netlists sometimes store named output buffers without pos metadata.
    for i,n in final.nodes.items():
        if n.get('name') in entry_outputs: final_outputs.setdefault(n['name'],i)
    name_by_gate={i:n for n,i in gate_by_name.items()}
    common={v.literal//2 for v in initial.values()} & {v.literal//2 for v in fv.values()}
    common.discard(0)
    class Frontier:
        def __init__(self, values, excluded): self.values,self.excluded=values,excluded
        def __contains__(self, i):
            identity=self.values[i].literal//2
            return identity in common and identity not in self.excluded
    groups=[]
    for output in region['output_ports']:
        label=f'ys__n{output}'
        if label not in entry_outputs or label not in final_outputs: continue
        a,b=entry_outputs[label],final_outputs[label]
        managers=[]; attempt={'kind':'output_group','output':output,'status':'unknown'}
        try:
            # Root aliases must not cut the very function being proved.
            excluded={initial[a].literal//2,fv[b].literal//2}
            stops_a=Frontier(initial,excluded)
            stops_b=Frontier(fv,excluded)
            ao,ab=cone(first,a,stops_a); bo,bb=cone(final,b,stops_b)
            names=sorted(name_by_gate[i] for i in bo if i in name_by_gate)
            if not names or all(n in singles for n in names):
                attempt.update(status='not_needed',reason='no_unproved_export_gates');continue
            if any(n not in mapping for n in names): raise Unsupported('joint_mapping_relation_missing')
            electrical={}
            for i in bb:
                # Only actual buffer aliases may denote the same independent
                # electrical input. Equal/complement FUNCTIONS at distinct
                # drivers cannot be merged: that would assume a correlation
                # and incorrectly prove, for example, OR == XOR on x and ~x.
                driver=i;phase=0;seen=set()
                while final.nodes[driver]['op']=='buf':
                    require(driver not in seen,'cycle in ABC boundary alias')
                    seen.add(driver);fanin=final.nodes[driver]['fanins'][0]
                    phase ^= fanin&1;driver=fanin//2
                identity=fv[i].literal//2
                literal=2*driver+phase
                if identity in electrical and electrical[identity]!=literal:
                    raise Unsupported('correlated_distinct_boundary_inputs')
                electrical[identity]=literal
            # Both cuts use the same independent functions, including polarity.
            identities=sorted({initial[i].literal//2 for i in ab}|{fv[i].literal//2 for i in bb})
            variables,mask=domain(len(identities),managers)
            variables=dict(zip(identities,variables))
            def bind(vals,ids):
                return {i:variables[vals[i].literal//2] ^ (mask if vals[i].literal&1 else 0) for i in ids}
            av=value(first,ao,bind(initial,ab),mask)[a]
            bv=value(final,bo,bind(fv,bb),mask)[b]
            if av!=bv: raise Unsupported('independent_output_boundary_not_proven')
            members=sorted({original[i] for i in ao if i in original})
            if not members: raise Unsupported('no_recorded_source_members')
            boundaries=[{'identity':k,'entry_nodes':[i for i in sorted(ab) if initial[i].literal//2==k],
                         'export_nodes':[i for i in sorted(bb) if fv[i].literal//2==k]} for k in identities]
            groups.append({'group_id':f'independent-output:{final.record["seq"]}:{output}',
                'basis':'captured_output_and_verified_structural_frontier','inputs':members,
                'mapped_nodes':names,'export_nodes':sorted(i for i in bo if i in name_by_gate),
                'export_root':b,'entry_root':a,'boundary_nodes':sorted(bb),'boundary_matches':boundaries,
                'boundary_functions_independent':True,'mapping_events':sorted({e for n in names for e in mapping[n]})})
            attempt.update(status='verified',entry_cells=len(members),mapped_gates=len(names),boundary_inputs=len(identities))
        except (Unsupported,ProofBudgetExceeded) as exc: attempt['reason']=str(exc)
        finally:
            attempts.append(attempt); resources.extend(m.statistics() for m in managers)
    joined=defaultdict(list)
    for g in groups:
        for name in g['mapped_nodes']: joined[name].append(g)
    return {name:{**singles.get(name,{}),
        'inputs':sorted({c for g in gs for c in g['inputs']}|set(singles.get(name,{}).get('inputs',[]))),
        'joint_groups':gs,'export_snapshot':final.record['seq'],'export_node':gate_by_name[name],
        'method':'captured_output_and_independent_boundary_joint_cut','precision':'abc_internal_joint_cut',
        'attribution_scope':'joint_group','per_gate_source_exact':False,'complete_rewrite_history':False}
        for name,gs in joined.items()}
