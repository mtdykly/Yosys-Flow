"""Import adopted, pinned FRAIG success events as universal function lemmas.

These are recorded solver results, not fresh SAT proofs or a claim that the
remaining ABC history is complete. The sweep adoption and cleanup are checked.
"""
import json
import random
import time
from collections import defaultdict
from types import SimpleNamespace

from .abc_internal import ABC_COMMIT, NETWORKS, decode_network, validate_cleanup
from .abc_lineage import shape
from .abc_checkpoints import network_blif
from .abc_partition import Graph
from .abc_lemmas import congruence
from .abc_snapshots import SnapshotMap
from .common import digest, require, write_json

REASONS=frozenset({'fraig_sat_merge','fraig_structural_merge'})
RULE='pinned_fraig_adopted_success_universal_substitution_v1'


def prefix(path):
    """Decode only through the first FRAIG; never parse the later DCH history."""
    rows=[];nets={};epochs={};current=None;fraig=None
    with path.open() as stream:
        for ordinal,line in enumerate(stream,1):
            row=json.loads(line);require(row['seq']==ordinal,'FRAIG prefix sequence differs')
            if ordinal==1:
                require(row['kind']=='begin' and row['abc_commit']==ABC_COMMIT and row['schema'] in {3,4,5},
                        'unsupported FRAIG evidence producer')
            rows.append(row)
            if row['kind']=='command_begin':
                current=row['argv'][0]
                if current=='&fraig':fraig=row['command_id']
            if row['kind'] in NETWORKS|{'history_network'}:
                nets[ordinal]=decode_network(row,nets,epochs,rows[0]['schema'],rows[0].get('features',()))
            if row['kind']=='command_end' and row['command_id']==fraig:
                require(row['status']==0,'FRAIG command failed');break
    return rows,nets


class _SourceShape:
    """Cache only within one export and one adopted source network.

    Decoded SnapshotMap versions are immutable. A changed version has a different
    object, even when it shares its history. Snapshot metadata is NOT cached:
    empty deltas may share nodes while changing ports or representation.
    """
    def __init__(self, source, statistics):
        self.source=source;self.statistics=statistics
        self.source_shape=None;self.checked=set()

    def matches(self, snapshot):
        source=self.source
        if (source.record.get('representation')!=snapshot.record.get('representation') or
                source.pis!=snapshot.pis or source.pos!=snapshot.pos):return False
        if source.nodes is snapshot.nodes:
            self.statistics['shared_node_storage_checks']+=1
            return True
        if isinstance(source.nodes,SnapshotMap) and isinstance(snapshot.nodes,SnapshotMap):
            # nets holds all versions alive for the export, so these IDs cannot
            # be recycled. Do not key by snapshot number or underlying history.
            key=id(snapshot.nodes)
            if key in self.checked:
                self.statistics['shape_cache_hits']+=1
                return True
            if self.source_shape is None:
                self.source_shape=shape(source);self.statistics['shape_builds']+=1
            self.statistics['shape_builds']+=1
            if self.source_shape!=shape(snapshot):return False
            self.checked.add(key)
            return True
        # External callers/tests may supply mutable dictionaries. Keep their
        # original comparison behavior; they do not get a persistent cache.
        self.statistics['shape_builds']+=2
        return shape(source)==shape(snapshot)


def export(rows,nets,input_names,out,progress=None,*,statistics=None):
    started=time.perf_counter()
    stats=statistics if statistics is not None else {}
    stats.update(records_indexed=0,facts_total=0,facts_checked=0,copy_maps_built=0,
                 cleanup_validations=0,shared_node_storage_checks=0,shape_builds=0,shape_cache_hits=0)
    phases={};stats['phase_seconds']=phases
    facts=[];commands={};copies=defaultdict(list);cleanups=defaultdict(list)
    if progress:progress('fraig_evidence_index',records=0,total=len(rows))
    for ordinal,row in enumerate(rows,1):
        if row['kind']=='proved_equivalence' and row.get('reason') in REASONS:facts.append(row)
        if row['kind']=='command_begin':commands[row['command_id']]=row['argv'][0]
        if row['kind']=='history_copy' and row.get('reason')=='fraig_sweep_copy':
            copies[row['command_id']].append(row)
        if row.get('reason')=='fraig_cleanup_copy':cleanups[row.get('before')].append(row)
        if progress and ordinal%10000==0:progress('fraig_evidence_index',records=ordinal,total=len(rows))
    stats.update(records_indexed=len(rows),facts_total=len(facts))
    phases['index']=time.perf_counter()-started
    groups={};step=time.perf_counter()
    if progress:progress('fraig_evidence_validate',checked=0,total=len(facts))
    for ordinal,fact in enumerate(facts,1):
        command=fact['command_id']
        require(commands.get(command)=='&fraig','FRAIG theorem outside FRAIG')
        if command not in groups:
            require(len(copies[command])==1,'FRAIG theorem lacks unique adopted sweep')
            copy=copies[command][0];source=nets[copy['before']]
            links=dict(copy['links']);stats['copy_maps_built']+=1
            cleanup=cleanups[copy['after']]
            require(len(cleanup)<=1,'ambiguous FRAIG cleanup')
            if cleanup:
                validate_cleanup(cleanup[0],nets[cleanup[0]['before']],nets[cleanup[0]['after']])
                stats['cleanup_validations']+=1
            groups[command]=(source,[],copy,cleanup,links,_SourceShape(source,stats))
        source,events,copy,cleanup,links,checker=groups[command]
        snapshot=nets[fact['snapshot']]
        require(fact['seq']<copy['seq'] and snapshot.record['command_id']==command and
                snapshot.record['kind']=='history_network' and checker.matches(snapshot),
                'FRAIG theorem has wrong source snapshot')
        old=fact['old'];new=fact['new_literal']
        require(old in source.nodes and new//2 in source.nodes and old in links and new//2 in links,
                'FRAIG theorem has absent adopted nodes')
        require(links[old]==(links[new//2]^(new&1)),'FRAIG theorem was not adopted with its polarity')
        events.append(fact);stats['facts_checked']=ordinal
        if progress and ordinal%1000==0:
            progress('fraig_evidence_validate',checked=ordinal,total=len(facts),command_id=command)
    phases['validate']=time.perf_counter()-step
    result=[];step=time.perf_counter()
    if progress:progress('fraig_evidence_write',commands=len(groups),facts=len(facts))
    for command,(net,events,copy,cleanup,_,_) in groups.items():
        require(len(net.pis)==len(input_names),'FRAIG theorem PI arity differs')
        graph=SimpleNamespace(names=list(input_names),pis=net.pis,pos=[],record={'po_names':[]},nodes=net.nodes)
        path=out/('fraig-'+str(command)+'.blif');path.write_text(network_blif(graph))
        payload=out/('fraig-'+str(command)+'.json');write_json(payload,events)
        result.append({'rule':RULE,'graph':path.name,'sha256':digest(path),'events':payload.name,
            'events_sha256':digest(payload),'count':len(events),'sweep_event':copy['seq'],
            'cleanup_event':cleanup[0]['seq'] if cleanup else None,
            'pi_substitution':dict(zip(net.pis,input_names))})
    phases['write']=time.perf_counter()-step
    stats['total_seconds']=time.perf_counter()-started
    if progress:progress('fraig_evidence_complete',statistics=stats)
    return result


def capture(directory,region,manifest,out,decoded=None,progress=None):
    started=time.perf_counter()
    if progress:progress('fraig_evidence_load',decoded_reused=decoded is not None)
    path=directory/region['artifacts']['input.blif.flow.jsonl']
    require(digest(path)==manifest['artifacts'][path.name]['sha256'],'FRAIG raw evidence changed')
    rows,nets=(decoded[:2] if decoded is not None else prefix(path))
    first=Graph((directory/region['artifacts']['input.blif']).read_text())
    statistics={'load_seconds':time.perf_counter()-started,'decoded_reused':decoded is not None}
    descriptors=export(rows,nets,first.pis,out,progress,statistics=statistics)
    metadata=json.loads((out/'checkpoints.json').read_text());metadata['fraig']=descriptors
    statistics['capture_seconds']=time.perf_counter()-started
    metadata['fraig_export_statistics']=statistics
    write_json(out/'checkpoints.json',metadata)


def install(engine,graph,events,descriptor):
    require(descriptor['rule']==RULE and descriptor['count']==len(events),'FRAIG theorem inventory differs')
    require(set(graph.pis)==set(engine.first.pis),'FRAIG theorem inputs differ')
    values=graph.values(graph.order,{n:engine.algebra.variable(n) for n in graph.pis},engine.algebra.one)
    rng=random.Random(76183);mask=(1<<256)-1
    samples=graph.values(graph.order,{n:rng.getrandbits(256) for n in sorted(graph.pis)},mask)
    names={int(i):n for i,n in descriptor['pi_substitution'].items()};equations=[]
    for event in events:
        require(event['kind']=='proved_equivalence' and event['reason'] in REASONS,'unproved FRAIG event')
        a=names.get(event['old'],'__checkpoint_'+str(event['old']))
        lit=event['new_literal'];b=names.get(lit//2,'__checkpoint_'+str(lit//2))
        require(a in values and b in values,'FRAIG theorem root absent')
        require(samples[a]==(samples[b]^(mask if lit&1 else 0)),'FRAIG theorem contradicts its snapshot')
        equations.append((values[a].literal,values[b].literal^(lit&1)))
    unions=congruence(engine.algebra,engine.classes,equations)
    engine.proofs.append({'proof':'recorded_fraig_adopted_solver_results','witness':descriptor,
        'events':len(events),'congruence_unions':unions,'independently_resolved_again':False,
        'complete_causal_history':False})
