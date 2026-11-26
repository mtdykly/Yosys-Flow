"""Publish base evidence first; resume authenticated, bounded enhancement batches."""
from pathlib import Path
import json
import hmac
import signal
import time
import uuid
import fcntl
from contextlib import contextmanager

from .abc_partition import Graph
from .common import digest, require, write_json
from .contracts import ContractContext
from .proof_store import Writer, signature
from .proof_checkpoint import CheckpointStore, BoundedPartitioner, StopPreparation, atomic_json


@contextmanager
def termination_guard():
    previous=signal.getsignal(signal.SIGTERM)
    def stop(signum,frame):raise StopPreparation('terminated; resume from the last committed batch')
    signal.signal(signal.SIGTERM,stop)
    try:yield
    finally:signal.signal(signal.SIGTERM,previous)


def publish(context, directory, store, regions, timings, live=None):
    refs={r['invocation_id']:store.entries['endpoint:'+r['invocation_id']]['sha256']
          for r in regions if 'endpoint:'+r['invocation_id'] in store.entries}
    if not refs:return None
    path=directory/'manifest.json'
    if path.exists():
        current=json.loads(path.read_text());auth=current.pop('authentication','')
        require(hmac.compare_digest(auth,signature(current)),'published evidence authentication mismatch')
        if current.get('checkpoint_refs')==refs:return current
    generation=uuid.uuid4().hex
    writer=Writer(context,directory/'generations'/generation)
    try:
        for region in regions:
            key='endpoint:'+region['invocation_id']
            if key not in store.entries:continue
            engine=live if live is not None and key==getattr(live,'checkpoint_key',None) else store.load(key)
            metadata=store.entries[key]['metadata']
            writer.add(region,engine,{'target_independent':True,'completed_work':metadata})
            del engine
        document=writer.finish(timings)
    finally:writer.db.close()
    document.update(database='generations/'+generation+'/functions.sqlite3',checkpoint_refs=refs,
                    publication_rule='immutable_generation_v1')
    atomic_json(path,{**document,'authentication':signature(document)})
    return document


def prepare(args, *, context=None, staged=None):
    destination=(Path(args.out) if args.out else Path(args.directory).resolve().with_name(Path(args.directory).resolve().name+'.proofs'))
    if not getattr(args,'resume',False):destination.mkdir(parents=True,exist_ok=False)
    require(destination.is_dir(),'resume directory does not exist')
    with (destination/'preparation.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:require(False,'another preparation process owns this directory')
        return _prepare(args,destination,context=context,staged=staged)


def _prepare(args,destination,*,context=None,staged=None):
    start=time.perf_counter()
    require(getattr(args,'batch_outputs',8)>0 and getattr(args,'cache_engines',1)>0,'batch/cache sizes must be positive')
    require(getattr(args,'enhance_budget',3600.0)>=0,'enhancement budget must be nonnegative')
    require(getattr(args,'boundary_budget',600)>=0,'boundary budget must be nonnegative')
    scan_boundaries=not args.structural_only and not getattr(args,'no_boundary_scan',False)
    reused=context is not None
    print('[proof-evidence] '+('reuse_validated_archive' if reused else 'authenticated_archive_load'),flush=True)
    context=context or ContractContext(args.directory,compact=True,proof_evidence=False)
    resume=getattr(args,'resume',False)
    policy={'structural_only':args.structural_only,'cec_timeout':args.cec_timeout}
    store=CheckpointStore(destination/'work',context.baseline['manifest_sha256'],policy,resume=resume)
    if staged:
        staged.adopt(context,destination,store)
    timings={'archive_load_seconds':time.perf_counter()-start,'base_evidence_seconds':0.0,
             'boundary_scan_seconds':0.0,'boundary_commit_seconds':0.0,'enhancement_seconds':0.0,'publication_seconds':0.0,'source_preparation_seconds':0.0}
    report={'status':'running','new_rtl_used':False,'edit_target_used':False,'timings':timings,
            'resumed':resume,'invocations':[],'complete_causal_history':False,'evidence_directory':str(destination.resolve()),
            'validated_context_reused':reused,'decoded_abc_reused':len(staged.entries) if staged else 0}
    if staged:report['replay_witness_capture_seconds']=staged.seconds
    session_id=uuid.uuid4().hex
    deadline=None;phase_started=time.perf_counter();active_phase=None;phase_times={}
    active_base_started=None
    def check_budget():
        if deadline is not None and time.perf_counter()>=deadline:raise StopPreparation('enhancement time budget exhausted')
    def progress(phase,**details):
        nonlocal active_phase,phase_started
        now=time.perf_counter()
        if active_phase:phase_times[active_phase]=phase_times.get(active_phase,0)+now-phase_started
        active_phase=phase;phase_started=now
        row=dict(phase=phase,session_id=session_id,timestamp_unix=time.time(),elapsed_seconds=now-start,**details)
        atomic_json(destination/'progress.json',row)
        with (destination/'progress.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
        print('[proof-evidence] '+phase,flush=True)
        check_budget()
    def publication(live):
        step=time.perf_counter()
        document=publish(context,destination,store,context.trace.regions,timings,live)
        timings['publication_seconds']+=time.perf_counter()-step
        if document:report.update(base_available=True,database=document['database'],sha256=document['sha256'])
    try:
        with termination_guard():
            for ordinal,region in enumerate(context.trace.regions):
                key='endpoint:'+region['invocation_id']
                progress('prepare_invocation',invocation=region['invocation_id'])
                step=time.perf_counter();active_base_started=step
                engine=store.load(key,check_budget)
                metadata=dict(store.entries.get(key,{}).get('metadata',{'installed':[],'outputs':[]}))
                if engine is None:
                    graphs=[]
                    for name in ('input.blif','output.blif'):
                        path=context.directory/region['artifacts'][name]
                        require(digest(path)==context.manifest['artifacts'][path.name]['sha256'],'ABC endpoint changed')
                        graphs.append(Graph(path.read_text()))
                    engine=BoundedPartitioner(*graphs,cec_timeout_seconds=args.cec_timeout)
                    engine.check_budget=check_budget;store.stats['engine_builds']+=1
                    store.save(key,engine,metadata)
                engine.checkpoint_key=key
                publication(engine) # Structural positives are useful even if later witnesses fail.
                witnesses=None
                if not args.structural_only:
                    from .checkpoint_evidence import Checkpoints
                    witness_path=destination/store.completed.get(key+':witnesses','checkpoints-'+str(ordinal))
                    need_witnesses=('congruence' not in metadata['installed'] or
                        (args.refine_outputs and set(metadata['outputs'])!=set(engine.first.pos)) or
                        (scan_boundaries and not metadata.get('boundary_scan_complete')))
                    if need_witnesses:
                        witnesses=Checkpoints(context,region,witness_path,progress,cec_timeout=args.cec_timeout,
                                              resume=resume or bool(staged))
                        store.completed[key+':witnesses']=str(witnesses.directory.relative_to(destination));store.commit()
                        def completed(done):
                            metadata['installed']=done;store.save(key,engine,metadata);publication(engine)
                        witnesses.install(engine,progress,done=metadata['installed'],checkpoint=completed,endpoint=True)
                timings['base_evidence_seconds']+=time.perf_counter()-step
                active_base_started=None
                progress('base_evidence_available',invocation=region['invocation_id'])
                if 'typed_evidence' not in report:
                    from .evidence_relations import prepare as prepare_relations
                    descriptor_started=time.perf_counter()
                    progress('prepare_typed_evidence')
                    report['typed_evidence']=prepare_relations(context,destination/'relations')
                    timings['typed_evidence_seconds']=time.perf_counter()-descriptor_started
                if not args.structural_only and 'source_representation' not in report:
                    from .source_cache import prepare as prepare_source
                    progress('prepare_old_source_representation')
                    source_started=time.perf_counter()
                    try:report['source_representation']=prepare_source(context,destination/'source',
                                                                       getattr(args,'source_timeout',600))
                    finally:timings['source_preparation_seconds']+=time.perf_counter()-source_started
                if scan_boundaries and not metadata.get('boundary_scan_complete'):
                    from .boundary_scan import prepare as prepare_boundaries
                    scan_started=time.perf_counter()
                    budget=getattr(args,'boundary_budget',600)
                    scan_deadline=scan_started+max(0,budget-timings['boundary_scan_seconds']) if budget else None
                    def check_scan():
                        if scan_deadline is not None and time.perf_counter()>=scan_deadline:
                            raise StopPreparation('generic boundary scan time budget exhausted')
                    try:
                        report.setdefault('boundary_scans',{})[region['invocation_id']]=prepare_boundaries(
                            engine,witnesses,store,key,metadata,progress,check_scan)
                    finally:
                        timings['boundary_scan_seconds']+=time.perf_counter()-scan_started
                        # Journals commit every hop; one full state + immutable
                        # publication per phase, including cooperative stops.
                        report.setdefault('boundary_scans',{})[region['invocation_id']]={
                            'complete':bool(metadata.get('boundary_scan_complete')),
                            'completed':len(metadata.get('boundary_scan_completed',[])),
                            'stages':metadata.get('boundary_scan_reports',{}),'target_independent':True}
                        commit_started=time.perf_counter()
                        store.save(key,engine,metadata);publication(engine)
                        timings['boundary_commit_seconds']+=time.perf_counter()-commit_started
                elif scan_boundaries:
                    report.setdefault('boundary_scans',{})[region['invocation_id']]={
                        'complete':True,'reused':True,'stages':metadata.get('boundary_scan_reports',{})}
                if args.refine_outputs:
                    step=time.perf_counter()
                    budget=getattr(args,'enhance_budget',3600.0)
                    deadline=time.perf_counter()+max(0,budget-timings['enhancement_seconds']) if budget else None
                    from .proof_bridge import BridgeSession
                    bridge=(BridgeSession(engine,witnesses,store,key,progress,check_budget,
                              cache_engines=getattr(args,'cache_engines',1)) if witnesses else None)
                    remaining=[n for n in engine.first.pos if n not in metadata['outputs']]
                    batch_size=getattr(args,'batch_outputs',8)
                    try:
                        for offset in range(0,len(remaining),batch_size):
                            check_budget();batch=remaining[offset:offset+batch_size]
                            progress('output_batch',targets=batch,completed=len(metadata['outputs']),total=len(engine.first.pos))
                            # Only verified relations are committed. Conditional
                            # assumptions exist solely inside targeted() and are
                            # restored by its finally block on interruption.
                            result=engine.targeted(batch,limit=4096,progress=progress,
                                                   frontier_prover=bridge.prove if bridge else None)
                            result_path=destination/('batch-'+uuid.uuid4().hex+'.json')
                            atomic_json(result_path,result)
                            metadata.setdefault('batches',[]).append({'artifact':result_path.name,
                                'sha256':digest(result_path),'outputs':batch,'proved':result['proved'],'failed':result['failed']})
                            metadata['outputs']=sorted(set(metadata['outputs'])|set(batch))
                            store.save(key,engine,metadata);publication(engine)
                            progress('output_batch_committed',completed=len(metadata['outputs']),total=len(engine.first.pos))
                        if bridge:report.setdefault('bridge_stats',[]).append(dict(bridge.stats))
                    finally:
                        timings['enhancement_seconds']+=time.perf_counter()-step
                        deadline=None
                report['invocations'].append({'id':region['invocation_id'],'proved_relations':len(engine.proofs),
                                              'outputs_attempted':len(metadata['outputs'])})
                del engine,witnesses
            require(report.get('base_available'),'no captured ABC invocation available')
            report.update(status='ready',enhancement_complete=bool(args.refine_outputs))
    except (StopPreparation,KeyboardInterrupt) as exc:
        report.update(status='base_ready_partial' if report.get('base_available') else 'interrupted',
                      enhancement_complete=False,reason=str(exc) or 'interrupted')
    except Exception as exc:
        report.update(status='failed',reason=str(exc));raise
    finally:
        if active_base_started is not None:
            timings['base_evidence_seconds']+=time.perf_counter()-active_base_started
        report['checkpoint_stats']=store.stats
        if 'bridge' in locals() and bridge:
            report['active_bridge_stats']=dict(bridge.stats)
        if active_phase:phase_times[active_phase]=phase_times.get(active_phase,0)+time.perf_counter()-phase_started
        report['phase_wall_seconds']=phase_times
        timings['total_seconds']=time.perf_counter()-start
        atomic_json(destination/'report.json',report)
        atomic_json(destination/('report-'+session_id+'.json'),report)
    return report
