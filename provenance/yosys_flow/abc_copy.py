"""Check actual DCH copy links using scoped objects and proved source merges.

Source SAT facts are not destination copy facts. Each link is checked against
the two actual networks, using only previously certified links as cut inputs.
This avoids expanding every object into a function of all primary inputs.
"""
from types import SimpleNamespace

from .abc_checkpoints import network_blif
from .abc_lemmas import REASONS, congruence
from .abc_partition import Classes, Graph, Partitioner
from .boolean import ProofBudgetExceeded
from .common import digest, require, write_json


def graphs(before,after,links,input_names):
    require(len(before.pis)==len(input_names),'copy input arity differs')
    require(len(before.pos)==len(after.pos),'copy output arity differs')
    mapping=dict(links)
    require(len(mapping)==len(links),'duplicate copy source')
    substitution={}
    for ident,name in zip(before.pis,input_names):
        target=mapping.get(ident)
        require(isinstance(target,int) and target%2==0 and target//2 in after.pis,
                'copy PI is missing, inverted, or not an input')
        require(target//2 not in substitution,'copy input is not bijective')
        substitution[target//2]=name
    require(set(substitution)==set(after.pis),'copy omits a destination input')
    result=[]
    for net,names in ((before,list(input_names)),(after,[substitution[i] for i in after.pis])):
        view=SimpleNamespace(nodes=net.nodes,pis=net.pis,pos=net.pos,names=names,
                             record={'po_names':['copy_output_'+str(i) for i in range(len(net.pos))]})
        result.append(network_blif(view))
    return result


def verify(before,after,row,facts,input_names,progress=None):
    require(row['reason']=='dch_choice_copy','unsupported copy proof')
    links=row['links']
    require({a for a,b in links}==set(before.nodes)-set(before.pos),'copy source coverage differs')
    require(all(b>=0 and b//2 in after.nodes for a,b in links),'copy target is absent')
    texts=graphs(before,after,links,input_names)
    first,final=map(Graph,texts);engine=Partitioner(first,final,sat_timeout_ms=1000)
    pi_names=[dict(zip(n.pis,g.pis)) for n,g in ((before,first),(after,final))]
    def name(side,ident):return pi_names[side].get(ident,'__checkpoint_'+str(ident))
    # Identity is scoped to the snapshot; equal integer IDs in two managers
    # provide no relationship. Only PIs/constants start with a shared identity.
    engine.a={n:2*(i+1) for i,n in enumerate(engine.a)}
    engine.b={n:2*(len(engine.a)+i+1) for i,n in enumerate(engine.b)}
    engine.b[None]=0;engine.classes=Classes(engine.a,engine.b)
    for n in first.pis:engine.classes.join(engine.a[n],engine.b[n])
    for side,(net,values) in enumerate(((before,engine.a),(after,engine.b))):
        for ident,node in net.nodes.items():
            if node['op'] in {'one','zero'}:
                engine.classes.join(values[name(side,ident)],int(node['op']=='one'))
    for fact in facts:
        require(fact['kind']=='proved_equivalence' and fact['reason'] in REASONS,
                'copy source relation was not a successful solver event')
        a=name(0,fact['old']);b=name(0,fact['new_literal']//2);phase=fact['new_literal']&1
        require(a in engine.a and b in engine.a,'copy source relation is outside its snapshot')
        require(engine.sa[a]==(engine.sa[b]^(engine.mask if phase else 0)),
                'copy source solver fact contradicts its function/polarity')
        engine.classes.join(engine.a[a],engine.a[b]^phase)
    rank={n:i for i,n in enumerate(first.pis+first.order)}
    accepted=[];unknown=[];explicit=0
    for ordinal,(old,new) in enumerate(sorted(links,key=lambda p:rank[name(0,p[0])])):
        try:
            ok=engine.prove(name(0,old),name(1,new//2),new&1,limit=256)
            reason='independent_functions_differ'
        except ProofBudgetExceeded as exc:ok=False;reason=str(exc)
        if ok:accepted.append([old,new])
        else:unknown.append({'old':old,'new_literal':new,'reason':reason})
        explicit+=len(engine.proofs);engine.proofs.clear()
        if progress and ordinal%50000==0:
            progress('verify_actual_dch_copy',visited=ordinal,total=len(links),verified=len(accepted),unknown=len(unknown))
    outputs=[];unknown_outputs=[]
    for ordinal in range(len(before.pos)):
        target='copy_output_'+str(ordinal)
        try:ok=engine.prove(target,target,0,limit=256)
        except ProofBudgetExceeded:ok=False
        (outputs if ok else unknown_outputs).append(target)
    proof={'rule':'scoped_dch_copy_independent_cuts_v1','event':row['seq'],
           'before':row['before'],'after':row['after'],'source_facts':len(facts),
           'verified':len(accepted),'total':len(links),'explicit_local_proofs':explicit,
           'unknown':unknown,'complete':not unknown and not unknown_outputs,'links':accepted,
           'outputs':outputs,'unknown_outputs':unknown_outputs}
    return texts,proof


def export(rows,nets,summary,input_names,out,progress=None):
    descriptors=[]
    for row in rows:
        if row['kind']!='history_copy' or row.get('reason')!='dch_choice_copy':continue
        require(summary['commands'][row['command_id']-1]=='&dch','choice copy outside DCH')
        before,after=nets[row['before']],nets[row['after']]
        facts=[];checked=set()
        for fact in rows:
            if fact['kind']!='proved_equivalence' or fact.get('reason') not in REASONS or fact['command_id']!=row['command_id']:continue
            require(fact['seq']<row['seq'],'copy depends on a future solver fact')
            source=nets[fact['snapshot']];key=id(source.nodes)
            if key not in checked:
                require(source.pis==before.pis and source.nodes==before.nodes,
                        'solver facts do not describe the actual copy source network')
                checked.add(key)
            facts.append(fact)
        texts,proof=verify(before,after,row,facts,input_names,progress)
        descriptor={'event':row['seq'],'rule':proof['rule'],'verified':proof['verified'],'total':proof['total']}
        for key,text in zip(('before','after'),texts):
            path=out/('copy-'+str(row['seq'])+'-'+key+'.blif');path.write_text(text)
            descriptor[key]={'artifact':path.name,'sha256':digest(path),
                             'pi_substitution':dict(zip((before if key=='before' else after).pis,Graph(text).pis))}
        path=out/('copy-'+str(row['seq'])+'-proof.json');write_json(path,proof)
        descriptor['proof']={'artifact':path.name,'sha256':digest(path)};descriptors.append(descriptor)
    return descriptors


def install(engine,first,final,proof,descriptor):
    require(proof['rule']=='scoped_dch_copy_independent_cuts_v1','unknown copy rule')
    require(proof['verified']==len(proof['links']),'copy proof inventory differs')
    values=[]
    for graph in (first,final):
        require(set(graph.pis)==set(engine.first.pis),'copy theorem has different inputs')
        values.append(graph.values(graph.order,{n:engine.algebra.variable(n) for n in graph.pis},engine.algebra.one))
    names=[{int(i):n for i,n in descriptor[key]['pi_substitution'].items()} for key in ('before','after')]
    equations=[]
    for old,new in proof['links']:
        a=names[0].get(old,'__checkpoint_'+str(old));b=names[1].get(new//2,'__checkpoint_'+str(new//2))
        require(a in values[0] and b in values[1],'certified copy root absent')
        equations.append((values[0][a].literal,values[1][b].literal^(new&1)))
    for name in proof.get('outputs',[]):
        require(name in first.pos and name in final.pos,'certified copy output is absent')
        equations.append((values[0][name].literal,values[1][name].literal))
    unions=congruence(engine.algebra,engine.classes,equations)
    engine.proofs.append({'proof':'verified_actual_dch_copy','witness':descriptor,
                          'verified':proof['verified'],'total':proof['total'],
                          'congruence_unions':unions,'complete_causal_history':False})
