"""Revalidate a completed raw archive and rebuild its index without synthesis."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import time

from .common import digest, require, write_json
from .persistent_index import INDEX_NAMES, write_index, artifact_inventory
from .progress import ArchiveProgress


def reindex(args):
    origin=Path(args.directory).resolve(strict=True);out=Path(args.out).resolve()
    require(not out.exists() and not out.is_relative_to(origin),'reindex requires a new directory outside the original archive')
    original=json.loads((origin/'manifest.json').read_text())
    require(original.get('status')=='complete' and original.get('schema_version')==2 and
            original.get('input_scope')=='combinational','reindex requires a completed native combinational archive')
    require(not original.get('functional_partitions'),'reindex requires the raw archive, not a target-refined derived archive')
    require(not getattr(args,'evidence_out',None) or getattr(args,'prepare_evidence',False),
            '--evidence-out requires --prepare-evidence')
    if getattr(args,'prepare_evidence',False):
        require(original['flow']=='synth','evidence preparation requires default synth')
        require(0<getattr(args,'evidence_cec_timeout',60)<=3600,'invalid evidence CEC budget')
        require(0<getattr(args,'source_timeout',600)<=86400,'invalid source preparation timeout')
    started=time.perf_counter();timings={};out.mkdir(parents=True)
    report={'status':'running','origin':str(origin),'run_directory':str(out),'timings':timings,
            'instrumented_synthesis_executed':False,'stock_synthesis_executed':False,
            'saved_stock_reference_rechecked':False}
    manifest=deepcopy(original);manifest.pop('verified_index',None)
    manifest['artifacts']={n:m for n,m in original['artifacts'].items() if n not in INDEX_NAMES}
    manifest['reindex']={'source_directory':str(origin),'source_manifest_sha256':digest(origin/'manifest.json'),
                        'source_timings_seconds':original.get('timings_seconds',{}),
                        'reused_capture':True,'reused_saved_reference':True}
    manifest['configuration']['index_backend']=args.index_backend or original['configuration'].get('index_backend','sqlite')
    manifest['timings_seconds']=timings
    progress=ArchiveProgress(out.with_name(out.name+'.progress.json'))
    complete=False
    try:
        write_json(out/'manifest.json',{**manifest,'status':'running'})
        progress('copy_completed_archive');step=time.perf_counter()
        for name,meta in manifest['artifacts'].items():
            require(Path(name).name==name,'invalid archive artifact path')
            shutil.copy2(origin/name,out/name)
            require(digest(out/name)==meta['sha256'],'copied archive artifact mismatch: '+name)
            progress('copy_completed_archive',artifact=name)
        timings['archive_copy_seconds']=time.perf_counter()-step
        from .proof_pipeline import EvidencePipeline
        pipeline=EvidencePipeline(args,out) if getattr(args,'prepare_evidence',False) else None
        from .native import load_native
        progress('revalidate_completed_archive');step=time.perf_counter()
        _,trace,validation=load_native(out,manifest,timings,use_index=False,evidence_pipeline=pipeline,progress=progress)
        timings['record_revalidation_seconds']=time.perf_counter()-step
        report['saved_stock_reference_rechecked']=True
        if pipeline:
            timings['proof_witness_capture_seconds']=pipeline.seconds
            timings['record_revalidation_excluding_witnesses_seconds']=timings['record_revalidation_seconds']-pipeline.seconds
        progress('verified_index_build');step=time.perf_counter()
        manifest['verified_index']=write_index(out,manifest,trace,verified_artifacts=artifact_inventory(out),progress=progress)
        timings['verified_index_build_seconds']=time.perf_counter()-step
        manifest['artifacts']={p.name:{'sha256':digest(p),'size_bytes':p.stat().st_size}
                              for p in out.iterdir() if p.is_file() and p.name!='manifest.json'}
        manifest['status']='complete';write_json(out/'manifest.json',manifest);complete=True
        report['archive_status']='complete';progress.finish()
        if pipeline:report['proof_evidence']=pipeline.finish(manifest,trace)
        report['status']='complete'
    except BaseException as exc:
        report.update(status='failed',error=str(exc),archive_status='complete' if complete else 'failed')
        if not complete:
            manifest.update(status='failed',error=str(exc));write_json(out/'manifest.json',manifest)
            progress.finish('failed',error=str(exc))
        raise
    finally:
        # Keep post-publication timing outside manifest: evidence binds its hash.
        report['total_seconds']=time.perf_counter()-started
        write_json(out.with_name(out.name+'.reindex.json'),report)
    return report
