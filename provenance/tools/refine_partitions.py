"""Targeted ABC endpoint proofs and optional publication, without synthesis.

Cached pairs are re-proved. Only verified regions, including an independent
archived-RTL proof of all external outputs, may enter a new signed SQLite index.
The old archive and its candidate provenance are preserved.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from yosys_flow.abc_partition import Graph, Partitioner, WindowLimit
from yosys_flow.common import digest, require, write_json, unescape
from yosys_flow.contracts import ContractContext, fingerprint
from yosys_flow.extract import slice_module
from yosys_flow.diagnostics import audit_cut
from yosys_flow.partition_archive import bind, publish
from yosys_flow.source_regions import prove, cut_signature
from yosys_flow.persistent_index import implementation_fingerprint
from yosys_flow.index_compatibility import compatible


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--invocation')
    target=parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--input-cell',action='append')
    target.add_argument('--input-wire',action='append',help='resolve an actual captured ABC input wire; reject ambiguous bindings')
    parser.add_argument('--input-module',help='module owning --input-wire; default recorded top')
    parser.add_argument('--input-type',help='optional required captured cell type')
    parser.add_argument('--reuse-pairs',type=Path)
    parser.add_argument('--evidence-out',type=Path,help='save normalized proved relations for later lazy reuse; no archive copy')
    parser.add_argument('--node-limits',type=int,nargs='+',default=[64,256,4096])
    parser.add_argument('--sat-timeout',type=float,default=1,help='seconds per local SAT attempt; timeout remains unknown')
    parser.add_argument('--local-backend',choices=['abc','abc_cec','z3'],default='abc')
    parser.add_argument('--cec-timeout',type=float,default=60,help='seconds for a blocking boundary at captured PIs')
    parser.add_argument('--cec-attempts',type=int,default=32,help='maximum global boundary attempts; unknown is never accepted')
    parser.add_argument('--captured-output-anchors',action='store_true',help='prove captured output anchors together by CEC')
    parser.add_argument('--output-cec-timeout',type=float,default=360)
    parser.add_argument('--checkpoint-bridges',action='store_true',help='prove target boundaries through captured intermediate ABC networks')
    parser.add_argument('--recorded-rewrites',action='store_true',help='independently check and replay actual local AIG replacements')
    parser.add_argument('--publish',type=Path,help='new archive directory; never overwrite the baseline')
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=False)
    start=time.perf_counter();report={'status':'running','synthesis_invocations':0,'new_rtl_used':False}
    implementation_at_start=implementation_fingerprint()
    phase_times={};active_phase=[None,time.perf_counter()]
    def progress(phase,**details):
        now=time.perf_counter()
        if active_phase[0]:phase_times[active_phase[0]]=phase_times.get(active_phase[0],0.0)+now-active_phase[1]
        active_phase[:]=[phase,now]
        row=dict(phase=phase,elapsed_seconds=time.perf_counter()-start,**details)
        print('[partition] '+json.dumps(row,ensure_ascii=False),flush=True)
        write_json(args.out/'progress.json',row)
    try:
        progress('authenticated_archive_load')
        manifest=json.loads((args.directory/'manifest.json').read_text())
        descriptor=manifest.get('verified_index',{})
        require(descriptor.get('artifact')=='verified-index.sqlite3' and
                compatible(descriptor.get('implementation',{}),implementation_at_start),
                'refinement needs a compatible SQLite reader; refusing an implicit full raw replay')
        context=ContractContext(args.directory,compact=True)
        require(context.load_timings.get('verified_index_status')=='hit','refinement requires a compatible authenticated index')
        if args.input_wire:
            from yosys_flow.partition_targets import resolve
            args.input_cell=resolve(context.trace,args.input_wire,args.input_module or manifest['configuration']['top'],
                                    args.input_type,args.invocation)
            progress('resolved_capture_targets', input_cells=args.input_cell, wires=args.input_wire)
        regions=[r for r in context.trace.regions if (not args.invocation or r['invocation_id']==args.invocation)
                 and set(args.input_cell)<=set(r['inputs'])]
        require(len(regions)==1,'targets must identify exactly one captured ABC invocation')
        region=regions[0];seed_refs=set(args.input_cell)
        seeds=['ys__n'+str(n['id']) for n in region['input_nodes'] if n['cell'] in seed_refs]
        paths={key:context.directory/region['artifacts'][key] for key in ('input.blif','output.blif')}
        for path in paths.values():
            require(digest(path)==context.manifest['artifacts'][path.name]['sha256'],'ABC endpoint changed')
        progress('endpoint_graphs')
        first=Graph(paths['input.blif'].read_text());final=Graph(paths['output.blif'].read_text())
        partitioner=Partitioner(first,final,sat_timeout_ms=int(args.sat_timeout*1000),
                                sat_backend=args.local_backend,
                                cec_timeout_seconds=args.cec_timeout,cec_attempt_limit=args.cec_attempts)
        affected=set();stack=list(seeds)
        while stack:
            node=stack.pop()
            if node in affected:continue
            affected.add(node);stack.extend(first.users[node])
        outputs=sorted(affected & set(first.pos))
        require(outputs,'no observable outputs for this target')
        report.update(baseline_manifest_sha256=context.baseline['manifest_sha256'],
                      invocation=region['invocation_id'],input_cells=args.input_cell,
                      requested_outputs=outputs,skipped_outputs=sorted(set(first.pos)-set(outputs)),
                      before=[partitioner.report_window(n) for n in outputs])
        write_json(args.out/'before.json',report['before'])
        if args.reuse_pairs:
            progress('reprove_cached_pairs')
            data=json.loads(args.reuse_pairs.read_text());pairs=data if isinstance(data,list) else data['proofs']
            partitioner.replay(pairs)
        if args.captured_output_anchors:
            require(partitioner.outputs_cec(timeout=args.output_cec_timeout,progress=progress),
                    'captured output anchors were not proven')
        progress('targeted_partition_refinement')
        if not args.reuse_pairs:
            partitioner.refine(outputs,limits=args.node_limits[:1],progress=progress,
                               checkpoint=lambda pairs:write_json(args.out/'pairs.json',pairs))
        checkpoint_state={};supplementary_artifacts={}
        def checkpoint_prover(requests):
            from yosys_flow.abc_checkpoints import bridge
            from yosys_flow.checkpoint_evidence import Checkpoints
            if not requests:return
            if 'witnesses' not in checkpoint_state:
                witnesses=Checkpoints(context,region,args.out/'checkpoints',progress,
                    rewrites=args.recorded_rewrites,cec_timeout=args.cec_timeout,
                    cec_attempts=min(8,args.cec_attempts))
                witnesses.install(partitioner,progress)
                checkpoint_state['witnesses']=witnesses
                supplementary_artifacts.update(witnesses.artifacts)
            witnesses=checkpoint_state['witnesses']
            requests=[(a,b,p) for a,b,p in requests if partitioner.classes.find(partitioner.a[a])!=
                      (partitioner.classes.find(partitioner.b[b])^p)]
            if not requests:return
            result=bridge(partitioner,requests,witnesses.graphs(final,digest(paths['output.blif'])),
                          limit=max(args.node_limits),progress=progress,
                          solver_lemmas=witnesses.lemmas,rewrite_lemmas=witnesses.rewrites,
                          copy_lemmas=witnesses.copies,mapping_lemmas=witnesses.mappings)
            write_json(args.out/'checkpoint-proof.json',result)
        report['targeted_dependencies']=partitioner.targeted(outputs,limit=max(args.node_limits),progress=progress,
            frontier_prover=checkpoint_prover if args.checkpoint_bridges or args.recorded_rewrites else None)
        if args.evidence_out:
            from yosys_flow.proof_store import Writer
            progress('persist_proved_function_classes')
            writer=Writer(context,args.evidence_out)
            writer.add(region,partitioner,{'target_independent':False,
                'preparation_seed_cells':args.input_cell,
                'coverage_note':'target-guided proof preparation; do not count as cold offline preparation'})
            writer.finish()
            report['proof_evidence_directory']=str(args.evidence_out.resolve())
        write_json(args.out/'pairs.json',partitioner.proofs)
        report.update(proved_pairs=len(partitioner.proofs),attempts=dict(partitioner.attempts),
                      after=[partitioner.report_window(n) for n in outputs])
        public={'ys__n'+str(i) for i,bit in enumerate(region['signals'])
                if isinstance(bit,dict) and bit['wire'].startswith('\\')}
        progress('all_external_output_partition')
        proof=partitioner.region(seeds,source_nodes=public,limit=max(args.node_limits))
        certificate=bind(context,region,proof)
        context._functional_partitions=[certificate]
        names=[unescape(context.trace.objects[r]['state']['name']) for r in certificate['final_cell_refs']]
        _,boundary=slice_module(context.module,names,'region',[])
        cut=audit_cut(context.module,names,boundary)
        require(cut['selected_cell_cut_complete'],'published final boundary incomplete')
        cells={n:context.module['cells'][n] for n in names}
        document={'boundary':boundary,'region':{'cells':[{'ref':r} for r in certificate['final_cell_refs']],
                                               'connections':[],'recorded_cell_functions':cells}}
        progress('archived_rtl_all_external_outputs',gates=len(names),ports=len(boundary))
        source_proof=prove(context,document)
        certificate.update(source_proof=source_proof,source_cut_sha256=cut_signature(document),boundary=boundary)
        write_json(args.out/'certificate.json',certificate)
        require(source_proof['status']=='verified' and source_proof['all_outputs'],
                'archived RTL does not yet certify every output: '+source_proof.get('reason','unknown'))
        if args.evidence_out:
            from yosys_flow.proof_store import Reader
            from yosys_flow.proof_regions import remember
            library=Reader(context,args.evidence_out)
            try:remember(library,certificate)
            finally:library.close()
        report.update(status='verified',selected_gates=len(names),boundary_ports=len(boundary),
                      all_external_outputs=True,source_proof=source_proof)
        if args.publish:
            require(implementation_fingerprint()==implementation_at_start,
                    'proof implementation changed during refinement; rerun before publishing')
            progress('publish_new_sqlite_archive')
            evidence={'endpoint_hashes':{k:digest(v) for k,v in paths.items()},
                'pair_proofs':partitioner.proofs,'limits':args.node_limits,
                'method':'chronological_functional_lemmas_then_independent_all_output_cut',
                'complete_rewrite_history':False}
            publish(context,args.publish,[certificate],evidence,progress=progress,
                    supplementary_artifacts=supplementary_artifacts)
            progress('verify_published_archive')
            check=ContractContext(args.publish,compact=True)
            require(check.load_timings.get('verified_index_status')=='hit','published index was not reusable')
            from yosys_flow.partition_store import certificates
            require(certificates(check)==[certificate],'published certificate differs')
            report.update(published_archive=str(args.publish.resolve()),published_index_verified=True)
    except Exception as exc:
        report.update(status='blocked',reason=str(exc),exception=type(exc).__name__)
        if isinstance(exc,WindowLimit):report['window_diagnostic']=exc.detail
        (args.out/'traceback.txt').write_text(traceback.format_exc())
    if active_phase[0]:phase_times[active_phase[0]]=phase_times.get(active_phase[0],0.0)+time.perf_counter()-active_phase[1]
    report['phase_wall_seconds']=phase_times
    report['phase_timing_scope']='exclusive progress intervals including preparation/I/O; not pure solver CPU time; target-guided work counts online in an unknown-edit experiment'
    report['elapsed_seconds']=time.perf_counter()-start
    write_json(args.out/'report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k in {'status','reason','selected_gates','published_archive','elapsed_seconds'}},ensure_ascii=False))
    return 0 if report['status']=='verified' else 2


if __name__=='__main__':raise SystemExit(main())
