"""Independently verify selected gates over their captured source GIA.

Choice leaves need not dominate the original root. Use a common ancestor cut
inside ONE snapshot, rather than expanding two complete mapped cones to PIs.
Only proved equalities are exported; a selected cut alone is not a proof.
"""
from collections import Counter
from types import SimpleNamespace

from .abc_checkpoints import network_blif
from .abc_lemmas import congruence
from .abc_partition import Graph, Partitioner
from .abc_rewrites import Replay
from .boolean import ProofBudgetExceeded
from .common import digest, require, write_json


RULE = 'selected_mapping_same_snapshot_independent_cuts_v1'


def verify(source, mapped, conversion, cuts, input_names, progress=None, *, cec_timeout=0, cec_attempts=8):
    require(len(source.pis)==len(input_names), 'mapping PI arity differs')
    literals={target:literal for literal,target in conversion['links']}
    require(len(literals)==len(conversion['links']), 'duplicate mapped target')
    for old,new,event in conversion.get('clones',[]):
        require(old in literals and new not in literals, 'unbound mapped clone')
        literals[new]=literals[old]
    by_root={cut['root_literal']:cut for cut in cuts}
    expected={literal for target,literal in literals.items()
              if mapped.nodes[target]['op']=='sop' and mapped.nodes[target]['fanins']}
    require(len(by_root)==len(cuts) and set(by_root)==expected, 'mapping cut inventory differs')
    replay=Replay([]); replay.nodes=dict(source.nodes)
    # Synthetic gates use disjoint IDs and original GIA leaf literals. They
    # never refer to another synthetic gate, so these proofs are independent.
    temporary=max(source.nodes,default=-1)+1
    pairs=[]; unknown=[]; gates=[]; pending=[]
    for target,literal in literals.items():
        if literal not in by_root:continue
        cut=by_root[literal]; node=mapped.nodes[target]
        require(literal//2 in source.nodes, 'selected mapping root absent')
        require(all(f//2 in literals for f in node['fanins']), 'mapped leaf lacks actual binding')
        fanins=[literals[f//2]^(f&1) for f in node['fanins']]
        require(Counter(fanins)==Counter(cut['leaves']), 'selected mapping leaf binding differs')
        require(all(f//2 in source.nodes for f in fanins), 'selected mapping leaf absent')
        gate={**node,'id':temporary,'fanins':fanins}
        replay.nodes[temporary]=gate
        proof=replay.local(literal//2,temporary,literal&1)
        if proof:
            pairs.append({'source_literal':literal,'gate':temporary,'mapped_node':target,
                          'event':cut['seq'],'proof':proof})
            gates.append(gate)
        else:
            unknown.append({'root_literal':literal,'mapped_node':target,'event':cut['seq']})
            pending.append((unknown[-1],gate))
        del replay.nodes[temporary];temporary+=1
        if progress and (len(pairs)+len(unknown))%25000==0:
            progress('verify_selected_mapping_cuts',verified=len(pairs),unknown=len(unknown),total=len(cuts))
    if pending and cec_timeout>0 and cec_attempts>0:
        # A few nonlocal choice relations can be cheap to prove when BOTH
        # sides retain the same source GIA. No mapped-cone equivalence is
        # assumed. Shared upstream logic stays identical in the two miters.
        selected=pending[:cec_attempts]
        view=SimpleNamespace(nodes={**source.nodes,**{n['id']:n for _,n in selected}},
                             pis=source.pis,names=list(input_names),pos=[],record={'po_names':[]})
        graph=Graph(network_blif(view)); engine=Partitioner(graph,graph,
            cec_timeout_seconds=cec_timeout,cec_attempt_limit=cec_attempts)
        pi_names=dict(zip(source.pis,input_names))
        for row,gate in selected:
            lit=row['root_literal'];a=pi_names.get(lit//2,'__checkpoint_'+str(lit//2))
            b='__checkpoint_'+str(gate['id'])
            before=len(engine.proofs)
            try:ok=engine.global_cec(a,b,lit&1,progress)
            except ProofBudgetExceeded:ok=False
            if ok:
                pairs.append({'source_literal':lit,'gate':gate['id'],'mapped_node':row['mapped_node'],
                              'event':row['event'],'proof':engine.proofs[-1] if len(engine.proofs)>before else
                              {'rule':'structural_identity_in_captured_graph'}})
                gates.append(gate);unknown.remove(row)
    # Preserve the mapped gate's own truth table and input polarities when
    # emitting its theorem. Folding GIA leaf complements into that table is
    # functionally valid, but hides the gate application from congruence.
    # Explicit, shared inverter nodes make substitution compositional.
    nodes=dict(source.nodes); aliases={}
    for pair in pairs:
        node=mapped.nodes[pair['mapped_node']]; fanins=[]
        for f in node['fanins']:
            literal=literals[f//2]
            if literal&1:
                if literal not in aliases:
                    aliases[literal]=temporary
                    nodes[temporary]={'id':temporary,'op':'buf','fanins':[literal]}
                    temporary+=1
                literal=2*aliases[literal]
            fanins.append(literal^(f&1))
        nodes[pair['gate']]={**node,'id':pair['gate'],'fanins':fanins}
    view=SimpleNamespace(nodes=nodes,
                         pis=source.pis,names=list(input_names),pos=[],record={'po_names':[]})
    text=network_blif(view)
    proof={'rule':RULE,'conversion':conversion['seq'],'snapshot':source.record['seq'],
           'pairs':pairs,'verified':len(pairs),'total':len(pairs)+len(unknown),'unknown':unknown,
           'pi_substitution':dict(zip(source.pis,input_names))}
    return text,proof


def export(rows,nets,cuts,input_names,out,progress=None, *, cec_timeout=60, cec_attempts=8):
    if not cuts:return []
    # Also check actual GIA -> mapped NTK -> exported netlist bindings, cloned
    # objects and unchanged snapshot shapes before accepting their cut operands.
    from .abc_local import mapping_context
    mapping_context(rows,nets,cuts,{})
    conversion=next(r for r in rows if r['kind']=='gia_to_ntk')
    source=nets[conversion['before']]; mapped=nets[conversion['after']]
    text,proof=verify(source,mapped,conversion,cuts,input_names,progress,
                      cec_timeout=cec_timeout,cec_attempts=cec_attempts)
    graph=out/'mapping-lemmas.blif';graph.write_text(text)
    events=out/'mapping-lemmas.json';write_json(events,proof)
    descriptor={'rule':RULE,'graph':graph.name,'sha256':digest(graph),
                'events':events.name,'events_sha256':digest(events),
                'verified':proof['verified'],'total':proof['total']}
    if progress:progress('selected_mapping_lemmas_exported',verified=proof['verified'],unknown=len(proof['unknown']))
    return [descriptor]


def install(engine,graph,proof,descriptor):
    require(proof['rule']==RULE and proof['verified']==len(proof['pairs']), 'mapping lemma inventory differs')
    require(set(graph.pis)==set(engine.first.pis), 'mapping lemma inputs differ')
    values=graph.values(graph.order,{n:engine.algebra.variable(n) for n in graph.pis},engine.algebra.one)
    names={int(i):n for i,n in proof['pi_substitution'].items()}
    equations=[]
    for pair in proof['pairs']:
        lit=pair['source_literal'];a=names.get(lit//2,'__checkpoint_'+str(lit//2))
        b='__checkpoint_'+str(pair['gate'])
        require(a in values and b in values, 'mapping lemma root absent')
        equations.append((values[a].literal^(lit&1),values[b].literal))
    unions=congruence(engine.algebra,engine.classes,equations)
    engine.proofs.append({'proof':RULE,'witness':descriptor,'verified':proof['verified'],
                          'unknown':len(proof['unknown']),'congruence_unions':unions,
                          'complete_causal_history':False})
