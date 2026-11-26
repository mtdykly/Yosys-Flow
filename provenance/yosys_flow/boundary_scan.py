"""Old-only, bounded correspondence closure across captured ABC checkpoints.

Samples only propose candidates. Each union follows an independent local proof;
checkpoint names and equal output names are never an equivalence assumption.
"""
from collections import Counter, defaultdict
import random

from .abc_partition import Classes, Partitioner
from .abc_lemmas import congruence
from .boolean import ProofBudgetExceeded
from .common import require


def hop_engine(endpoint, first, final, *, sat_timeout_ms=50):
    require(set(first.pis)==set(final.pis)==set(endpoint.first.pis),
            'boundary scan captured input interface differs')
    # Output sets can differ (e.g. an internal mapping checkpoint). Inputs,
    # equations, and independently verified functions define correspondence.
    engine=Partitioner.__new__(Partitioner)
    engine.first,engine.final=first,final
    engine.algebra=endpoint.algebra
    engine.initial_terms=len(endpoint.algebra.unique)
    for graph,field in ((first,'a'),(final,'b')):
        values=graph.values(graph.order,{n:engine.algebra.variable(n) for n in graph.pis},engine.algebra.one)
        setattr(engine,field,{n:endpoint.classes.find(v.literal) for n,v in values.items()})
    final.nodes[None]=dict(op='sop',sop='',fanins=[])
    engine.b[None]=0
    engine.classes=Classes(engine.a,engine.b)
    from .boundary_forms import install as install_forms
    engine.form_summary=install_forms(engine)
    engine.mask=(1<<256)-1;rng=random.Random(73129)
    samples={n:rng.getrandbits(256) for n in sorted(first.pis)}
    engine.sa=first.values(first.order,samples,engine.mask)
    engine.sb=final.values(final.order,samples,engine.mask);engine.sb[None]=0
    engine.candidates=defaultdict(list)
    for n in final.order:
        sig=engine.sb[n];engine.candidates[min(sig,sig^engine.mask)].append(n)
    engine.candidates[0].insert(0,None)
    engine.sat_timeout_ms=sat_timeout_ms;engine.sat_backend='abc'
    engine.cec_timeout_seconds=1;engine.cec_attempt_limit=0;engine.cec_attempts={}
    engine.proofs=[];engine.diagnostics=[];engine.attempts=Counter()
    return engine


def sweep(engine, *, limit=64, check=lambda:None, progress=None):
    """Visit every old node in topological order, regardless of future edits."""
    tried=set();matched=0;unknown=0
    for ordinal,a in enumerate(engine.first.order):
        if ordinal%256==0:check()
        cls=engine.classes
        if cls.sides[cls.find(engine.a[a])//2]==3:
            matched+=1;continue
        sig=engine.sa[a]
        for b in engine.candidates.get(min(sig,sig^engine.mask),())[:32]:
            phase=int(sig!=engine.sb[b]);key=(cls.find(engine.a[a]),cls.find(engine.b[b]),phase)
            if key in tried:continue
            tried.add(key);check()
            try:
                ok=engine.prove(a,b,phase,limit=limit)
                engine.attempts['verified' if ok else 'independent_functions_differ']+=1
                if ok:break
            except ProofBudgetExceeded as exc:engine.attempts[str(exc)]+=1
        if cls.sides[cls.find(engine.a[a])//2]==3:matched+=1
        else:unknown+=1
        if progress and ordinal%5000==0:
            progress('boundary_scan_nodes',visited=ordinal,total=len(engine.first.order),
                     verified=len(engine.proofs),unknown=unknown)
    unresolved=[n for n in engine.first.order if engine.classes.sides[engine.classes.find(engine.a[n])//2]!=3]
    return dict(visited=len(engine.first.order),matched=len(engine.first.order)-len(unresolved),
                unknown=len(unresolved),unknown_preview=unresolved[:16],
                new_proofs=len(engine.proofs),attempts=dict(engine.attempts))


def install(endpoint, hop, descriptor):
    """Compose verified checkpoint functions into the endpoint proof classes."""
    equations=[(2*ident,value) for ident,value in hop.classes.parents.items()]
    unions=(congruence(endpoint.algebra,endpoint.classes,equations)
            if equations or len(endpoint.algebra.unique)!=hop.initial_terms else 0)
    endpoint.proofs.append(dict(proof='bounded_checkpoint_function_closure_v1',
        checkpoint=descriptor,local_proofs=hop.proofs,congruence_unions=unions,
        gate_forms=hop.form_summary,
        target_independent=True,complete_causal_history=False))
    return unions


def prepare(endpoint, witnesses, store, key, metadata, progress, check, *, limit=64, sat_timeout_ms=50):
    """Persist each hop's small signed proof journal, not repeated whole engines.

    Journals are trusted only through CheckpointStore's authenticated manifest.
    On interruption, positive proofs are committed; an unfinished hop is retried.
    No source location, changed node, output subset, or new RTL is accepted here.
    """
    import json
    import time
    import uuid
    from .abc_partition import Graph
    from .common import digest
    from .proof_checkpoint import atomic_json

    descriptors=[*witnesses.stages,{'endpoint':'captured_output'}]
    done=metadata.setdefault('boundary_scan_completed',[])
    reports=metadata.setdefault('boundary_scan_reports',{})
    previous=endpoint.first
    for ordinal,descriptor in enumerate(descriptors):
        check()
        graph=(endpoint.final if 'endpoint' in descriptor else
               Graph(witnesses.file(descriptor['file'],descriptor['sha256']).read_text()))
        label=str(ordinal)
        binding={'before':descriptors[ordinal-1] if ordinal else {'endpoint':'captured_input'},
                 'after':descriptor,'node_limit':limit,'sat_timeout_ms':sat_timeout_ms}
        if label in done:
            previous=graph;continue
        started=time.perf_counter()
        progress('boundary_scan_checkpoint',ordinal=ordinal,total=len(descriptors),checkpoint=descriptor)
        hop=hop_engine(endpoint,previous,graph,sat_timeout_ms=sat_timeout_ms)
        journal_key=key+':boundary:'+label
        cached=store.completed.get(journal_key)
        finished=False;summary={}
        if cached:
            path=store.directory/cached['artifact']
            require(path.parent==store.directory and digest(path)==cached['sha256'],
                    'boundary proof journal hash mismatch')
            document=json.loads(path.read_text())
            require(document['binding']==binding,'boundary proof journal binding differs')
            for row in document['proofs']:
                require(row['proof']=='independent_frontier_function','unsupported boundary proof journal rule')
                a,b=row['entry_root'],row['export_root']
                require(a in hop.a and b in hop.b,'boundary proof journal root missing')
                hop.classes.join(hop.a[a],hop.b[b]^int(row['inverted']))
            hop.proofs.extend(document['proofs'])
            finished=document['complete'];summary=document['summary']
        try:
            if not finished:
                summary=sweep(hop,limit=limit,check=check,progress=progress)
                finished=True
        finally:
            # Only independently proved pairs are in hop.proofs. A budget stop
            # never upgrades a candidate or the unfinished stage to verified.
            if not finished:
                summary=dict(new_proofs=len(hop.proofs),attempts=dict(hop.attempts),interrupted=True)
            payload={'binding':binding,'proofs':hop.proofs,'complete':finished,'summary':summary}
            path=store.directory/('boundary-'+uuid.uuid4().hex+'.json')
            atomic_json(path,payload)
            store.completed[journal_key]={'artifact':path.name,'sha256':digest(path)}
            store.commit()
            if cached:(store.directory/cached['artifact']).unlink(missing_ok=True)
            summary={**summary,'gate_forms':hop.form_summary,'congruence_unions':install(endpoint,hop,binding),
                     'complete':finished,'seconds':time.perf_counter()-started}
            reports[label]=summary
            if finished:done.append(label)
        previous=graph
    metadata['boundary_scan_complete']=len(done)==len(descriptors)
    return {'complete':metadata['boundary_scan_complete'],'checkpoints':len(descriptors),
            'completed':len(done),'stages':reports,'node_limit':limit,'sat_timeout_ms':sat_timeout_ms,
            'target_independent':True,'complete_causal_history':False}
