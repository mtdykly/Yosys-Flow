"""Recover unproved OLD rewrite events over their actual snapshot's PIs.

The inventory comes from replay, never an ECO selector. Recorded replacement is
not itself an equality: only a successful proof adds a function equation.
"""
import gzip
import json
import time
from types import SimpleNamespace

from .abc_internal import ABC_COMMIT, NETWORKS, decode_network
from .abc_checkpoints import network_blif
from .abc_partition import Graph, Partitioner
from .abc_lemmas import congruence
from .boolean import ProofBudgetExceeded
from .common import digest, require, write_json
from .structural import Expressions

RULE='recovered_recorded_rewrite_functions_v1'


def snapshots(path, inventory, decoded=None):
    requested={r['seq']:r for r in inventory};wanted={r['snapshot'] for r in inventory}
    if not requested:return {},{}
    if decoded is not None:
        rows,nets=decoded[:2]
        events={seq:rows[seq-1] for seq in requested}
        return {seq:nets[seq] for seq in wanted},events
    nets={};epochs={};kept={};events={}
    with path.open() as stream:
        for seq,line in enumerate(stream,1):
            row=json.loads(line);require(row['seq']==seq,'rewrite recovery sequence differs')
            if seq==1:
                header=row
                require(header['kind']=='begin' and header['abc_commit']==ABC_COMMIT,
                        'unsupported rewrite recovery producer')
            if row['kind'] in NETWORKS|{'history_network'}:
                net=decode_network(row,nets,epochs,header['schema'],header.get('features',()))
                nets[seq]=net
                if seq in wanted:kept[seq]=net
                # Retain immutable requested versions and only the current
                # decoding bases; do not materialize the later DCH history.
                latest=set(epochs.values())|{seq}
                nets={i:n for i,n in nets.items() if i in latest}
            if seq in requested:events[seq]=row
            if seq==max(requested):break
    require(set(kept)==wanted and set(events)==set(requested),'missing rewrite recovery snapshots')
    return kept,events


def recover(path, document, input_names, *, decoded=None, cec_timeout=20, budget=300, progress=None):
    require(cec_timeout>0 and budget>=0,'invalid rewrite recovery budget')
    inventory=document.get('unknown_rewrite_examples',[])
    require(len({r['seq'] for r in inventory})==len(inventory),'duplicate unproved rewrite inventory')
    started=time.perf_counter();nets,events=snapshots(path,inventory,decoded)
    algebra=Expressions();equations=[];unknown=[];attempted=0
    for row in inventory:
        if budget and time.perf_counter()-started>=budget:
            unknown.append({'event':row['seq'],'reason':'recovery_budget'});continue
        event=events[row['seq']];net=nets[row['snapshot']]
        require(event['kind']=='aig_replace' and all(event[k]==row[k] for k in ('seq','snapshot','old','new_literal')),
                'rewrite recovery inventory differs from actual event')
        require(net.record['kind']=='history_network' and net.record['representation']=='aig' and
                net.record['seq']==row['snapshot'] and row['snapshot']<event['seq'] and
                net.record['command_id']==event['command_id'] and
                len(net.pis)==len(input_names),'rewrite recovery snapshot scope differs')
        a,b=event['old'],event['new_literal']//2;phase=event['new_literal']&1
        require(a in net.nodes and b in net.nodes and a!=b,'invalid recovery roots')
        view=SimpleNamespace(nodes=net.nodes,pis=net.pis,pos=[a,b],names=list(input_names),
                             record={'po_names':['__recovery_old','__recovery_new']})
        graph=Graph(network_blif(view));engine=Partitioner(graph,graph,
            cec_timeout_seconds=cec_timeout,cec_attempt_limit=1)
        attempted+=1;t=time.perf_counter()
        try:ok=engine.global_cec('__recovery_old','__recovery_new',phase)
        except ProofBudgetExceeded:ok=False
        if ok:
            values=graph.values(graph.order,{n:algebra.variable(n) for n in graph.pis},algebra.one)
            equations.append({'event':event['seq'],'snapshot':row['snapshot'],'old':a,'new_literal':event['new_literal'],
                'left':values['__recovery_old'].literal,'right':values['__recovery_new'].literal^phase,
                'proof':engine.proofs[-1] if engine.proofs else {'proof':'structural_identity'},
                'status':'verified','input_domain':'all_snapshot_pis_independent'})
        else:unknown.append({'event':event['seq'],'reason':'not_proved'})
        if progress:progress('recover_old_rewrite',event=event['seq'],verified=ok,proof_seconds=time.perf_counter()-t,
                             completed=attempted,total=len(inventory))
    total=document.get('statistics',{}).get('rewrite_unknown',len(inventory))
    return {'rule':RULE,'terms':[{'literal':lit,'term':list(key)} for key,lit in algebra.unique.items()],
            'equations':equations,'unknown':unknown,'summary':{'inventory':len(inventory),'reported_unknown':total,
                'inventory_truncated':total>len(inventory),'attempted':attempted,'verified':len(equations),
                'unknown':len(unknown),'cec_timeout_seconds':cec_timeout,'budget_seconds':budget,
                'seconds':time.perf_counter()-started,'edit_target_used':False,'complete_causal_history':False}}


def capture(directory,region,manifest,out,decoded=None,progress=None):
    path=out/'abc-rewrite-lemmas.json.gz'
    if not path.exists():return
    with gzip.open(path,'rt') as stream:document=json.load(stream)
    raw=directory/region['artifacts']['input.blif.flow.jsonl']
    require(digest(raw)==manifest['artifacts'][raw.name]['sha256'],'rewrite recovery raw evidence changed')
    first=Graph((directory/region['artifacts']['input.blif']).read_text())
    recovered=recover(raw,document,first.pis,decoded=decoded,progress=progress)
    artifact=out/'recovered-rewrite-functions.json.gz'
    with gzip.open(artifact,'wt') as stream:json.dump(recovered,stream,separators=(',',':'),allow_nan=False)
    metadata=json.loads((out/'checkpoints.json').read_text())
    metadata['rewrite_recovery']={'rule':RULE,'artifact':artifact.name,'sha256':digest(artifact),
                                 'summary':recovered['summary'],'raw_sha256':digest(raw)}
    write_json(out/'checkpoints.json',metadata)


def install(engine,document,descriptor):
    require(document['rule']==RULE,'unknown recovered rewrite rule')
    mapping={0:0,1:1}
    def mapped(lit):return mapping[lit//2*2]^(lit&1)
    for row in document['terms']:
        lit,term=row['literal'],row['term']
        require(lit%2==0 and lit not in mapping,'duplicate recovered rewrite term')
        if term[0]=='input':
            require(term[1] in engine.first.inputs,'recovered rewrite PI differs')
            value=engine.algebra.variable(term[1]).literal
        else:
            require(term[0]=='and' and len(term)==3,'unsupported recovered rewrite term')
            value=engine.algebra.conjunction(mapped(term[1]),mapped(term[2]))
        mapping[lit]=value
    equations=[]
    for row in document['equations']:
        require(row['status']=='verified' and row['input_domain']=='all_snapshot_pis_independent' and
                row['proof']['proof'] in {'captured_pi_cec','structural_identity'},'unproved recovered rewrite equation')
        equations.append((mapped(row['left']),mapped(row['right'])))
    unions=congruence(engine.algebra,engine.classes,equations) if equations else 0
    engine.proofs.append({'proof':RULE,'witness':descriptor,'summary':document['summary'],
                          'congruence_unions':unions,'complete_causal_history':False})
    return unions
