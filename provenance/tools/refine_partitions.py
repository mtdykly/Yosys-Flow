"""Targeted ABC endpoint proofs and optional publication, without synthesis.

Cached pairs are re-proved. Only verified regions, including an independent
archived-RTL proof of all external outputs, may enter a new signed SQLite index.
The old archive and its candidate provenance are preserved.
"""
import argparse
import gzip
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
    parser.add_argument('--input-cell',action='append',required=True)
    parser.add_argument('--reuse-pairs',type=Path)
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
    def progress(phase,**details):
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
            from yosys_flow.abc_checkpoints import capture, bridge
            from yosys_flow.abc_lemmas import install
            if not requests:return
            stage_dir=args.out/'checkpoints'
            if 'stages' not in checkpoint_state:
                checkpoint_state['stages']=capture(context.directory,region,context.manifest,stage_dir,progress,
                                                  include_rewrites=args.recorded_rewrites,
                                                  mapping_cec_timeout=args.cec_timeout,
                                                  mapping_cec_attempts=min(8,args.cec_attempts))
            stages=checkpoint_state['stages']
            metadata=json.loads((stage_dir/'checkpoints.json').read_text())
            # The derived archive must carry every referenced witness, not
            # depend on the temporary diagnostic directory remaining present.
            for row in stages:
                path=stage_dir/row['file']
                require(digest(path)==row['sha256'],'checkpoint evidence changed')
                supplementary_artifacts[path.name]=(path,row['sha256'])
            supplementary_artifacts['checkpoints.json']=(stage_dir/'checkpoints.json',digest(stage_dir/'checkpoints.json'))
            copy_data=[]
            mapping_data=[]
            for descriptor in metadata.get('mappings',[]):
                graph_path=stage_dir/descriptor['graph'];event_path=stage_dir/descriptor['events']
                require(digest(graph_path)==descriptor['sha256'] and digest(event_path)==descriptor['events_sha256'],
                        'selected mapping evidence changed')
                supplementary_artifacts[graph_path.name]=(graph_path,descriptor['sha256'])
                supplementary_artifacts[event_path.name]=(event_path,descriptor['events_sha256'])
                mapping_data.append((Graph(graph_path.read_text()),json.loads(event_path.read_text()),descriptor))
            for descriptor in metadata.get('copies',[]):
                copy_paths=[]
                for key in ('before','after','proof'):
                    item=descriptor[key];path=stage_dir/item['artifact']
                    require(digest(path)==item['sha256'],'verified copy evidence changed')
                    supplementary_artifacts[path.name]=(path,item['sha256']);copy_paths.append(path)
                copy_data.append((Graph(copy_paths[0].read_text()),Graph(copy_paths[1].read_text()),
                                  json.loads(copy_paths[2].read_text()),descriptor))
            rewrite_lemmas=None
            if metadata.get('rewrites'):
                descriptor=metadata['rewrites'];path=stage_dir/descriptor['artifact']
                require(digest(path)==descriptor['sha256'],'recorded rewrite evidence changed')
                with gzip.open(path,'rt') as stream:document=json.load(stream)
                rewrite_lemmas=(document,descriptor)
                supplementary_artifacts[path.name]=(path,descriptor['sha256'])
                if not checkpoint_state.get('rewrites_installed'):
                    from yosys_flow.abc_rewrites import install as install_rewrites
                    progress('install_local_rewrite_functions',statistics=document['statistics'])
                    install_rewrites(partitioner,*rewrite_lemmas)
                    checkpoint_state['rewrites_installed']=True
            lemma_data=[]
            for row in metadata['lemmas']:
                graph_path=stage_dir/row['graph'];event_path=stage_dir/row['events']
                require(digest(graph_path)==row['sha256'] and digest(event_path)==row['events_sha256'],
                        'captured solver lemma changed during proof')
                supplementary_artifacts[graph_path.name]=(graph_path,row['sha256'])
                supplementary_artifacts[event_path.name]=(event_path,row['events_sha256'])
                lemma_data.append((Graph(graph_path.read_text()),json.loads(event_path.read_text()),row))
            if not checkpoint_state.get('lemmas_installed'):
                for graph,events,row in lemma_data:
                    progress('reuse_recorded_solver_results',events=len(events),snapshot=row['snapshot'])
                    install(partitioner,graph,events,row)
                checkpoint_state['lemmas_installed']=True
            if not checkpoint_state.get('copies_installed'):
                from yosys_flow.abc_copy import install as install_copy
                for first_graph,last_graph,proof,witness in copy_data:
                    progress('install_verified_copy_functions',verified=proof['verified'],total=proof['total'])
                    install_copy(partitioner,first_graph,last_graph,proof,witness)
                checkpoint_state['copies_installed']=True
            if not checkpoint_state.get('mappings_installed'):
                from yosys_flow.abc_mapping import install as install_mapping
                for graph,proof,witness in mapping_data:
                    progress('install_verified_mapping_functions',verified=proof['verified'],total=proof['total'])
                    install_mapping(partitioner,graph,proof,witness)
                checkpoint_state['mappings_installed']=True
            if not checkpoint_state.get('associative_closed'):
                from yosys_flow.abc_lemmas import associative_congruence
                result=associative_congruence(partitioner.algebra,partitioner.classes,progress=progress)
                partitioner.proofs.append({'proof':'and_associativity',**result})
                checkpoint_state['associative_closed']=True
            requests=[(a,b,p) for a,b,p in requests if partitioner.classes.find(partitioner.a[a])!=
                      (partitioner.classes.find(partitioner.b[b])^p)]
            if not requests:return
            def graphs():
                for row in stages:
                    path=stage_dir/row['file']
                    require(digest(path)==row['sha256'],'checkpoint witness changed during proof')
                    yield Graph(path.read_text()),row
                yield final,{'endpoint':'captured_output','sha256':digest(paths['output.blif'])}
            result=bridge(partitioner,requests,graphs(),limit=max(args.node_limits),progress=progress,
                          solver_lemmas=lemma_data,rewrite_lemmas=rewrite_lemmas,copy_lemmas=copy_data,
                          mapping_lemmas=mapping_data)
            write_json(args.out/'checkpoint-proof.json',result)
        report['targeted_dependencies']=partitioner.targeted(outputs,limit=max(args.node_limits),progress=progress,
            frontier_prover=checkpoint_prover if args.checkpoint_bridges or args.recorded_rewrites else None)
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
    report['elapsed_seconds']=time.perf_counter()-start
    write_json(args.out/'report.json',report)
    print(json.dumps({k:v for k,v in report.items() if k in {'status','reason','selected_gates','published_archive','elapsed_seconds'}},ensure_ascii=False))
    return 0 if report['status']=='verified' else 2


if __name__=='__main__':raise SystemExit(main())
