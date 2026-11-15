"""Recheck independent ABC cuts from an archive, without synthesis or publication.

The output is reviewable evidence, not a replacement for the authenticated index.
Use resume to replay and publish a refined archive after reviewing the results.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from yosys_flow.abc_internal import read_evidence
from yosys_flow.abc_local import analyze_local
from yosys_flow.common import digest, require, write_json
from yosys_flow.persistent_index import implementation_fingerprint
from yosys_flow.runner import load_run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--invocation',help='one captured invocation ID; default all')
    parser.add_argument('--input-cell',action='append',default=[],help='report matches for this captured ABC input ref')
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    start=time.perf_counter();timings={};results=[]
    report={'status':'running','scope':'independent_abc_correspondence_only',
            'updates_baseline_index':False,'synthesis_invocations':0,'regions':results}
    def progress(phase):
        elapsed=time.perf_counter()-start
        print(f'[abc-recheck] {phase}: {elapsed:.1f}s',flush=True)
        write_json(args.out/'progress.json',{'phase':phase,'elapsed_seconds':elapsed})
    try:
        progress('archive_load')
        manifest_hash=digest(args.directory/'manifest.json')
        manifest,trace,_=load_run(args.directory,timings=timings)
        regions=[r for r in trace.regions if not args.invocation or r['invocation_id']==args.invocation]
        require(regions,'no matching captured ABC invocation')
        report.update(baseline={'run_id':manifest['run_id'],'manifest_sha256':manifest_hash},
                      implementation=implementation_fingerprint(),archive_load=timings)
        for ordinal,region in enumerate(regions,1):
            prefix=f'abc-{ordinal}'
            paths={k:args.directory/region['artifacts'][k] for k in ('script','input.blif','input.blif.flow.jsonl')}
            def check_artifacts():
                require(digest(args.directory/'manifest.json')==manifest_hash,'baseline manifest changed during recheck')
                for path in paths.values():
                    require(digest(path)==manifest['artifacts'][path.name]['sha256'], 'ABC artifact changed during recheck')
            check_artifacts()
            progress(prefix+'_read_evidence')
            rows,nets,cuts,captured=read_evidence(paths['input.blif.flow.jsonl'],paths['script'].read_text())
            analysis_start=time.perf_counter()
            proof,summary=analyze_local(rows,nets,cuts,region,trace.abc_imports[region['invocation_id']],
                                       paths['input.blif'].read_text(),progress)
            analysis_seconds=time.perf_counter()-analysis_start
            check_artifacts()
            write_json(args.out/(prefix+'.proof.json'),proof)
            write_json(args.out/(prefix+'.analysis.json'),summary)
            results.append({'invocation_id':region['invocation_id'],'analysis_seconds':analysis_seconds,
                'imported_gates':len(trace.abc_imports[region['invocation_id']]),
                'proved_gates':len(proof),'unproved_gates':len(summary['unproved_reasons']),
                'unproved_reasons':dict(Counter(r['reason'] for rs in summary['unproved_reasons'].values() for r in rs)),
                'input_cell_matches':{ref:[n for n,p in proof.items() if ref in p['inputs']] for ref in args.input_cell},
                'proof_file':prefix+'.proof.json','analysis_file':prefix+'.analysis.json'})
            del rows,nets,cuts,proof,summary
        report['status']='completed'
    except Exception as exc:
        report.update(status='failed',failure={'type':type(exc).__name__,'detail':str(exc)[:2000],
                                              'traceback':'failure-traceback.txt'})
        (args.out/'failure-traceback.txt').write_text(traceback.format_exc())
    report.update(elapsed_seconds=time.perf_counter()-start,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    write_json(args.out/'report.json',report)
    print(json.dumps({'status':report['status'],'report':str(args.out/'report.json'),
                      'regions':[{'invocation':r['invocation_id'],'proved':r['proved_gates'],
                                  'unproved':r['unproved_gates']} for r in results]},ensure_ascii=False))
    return 0 if report['status']=='completed' else 1


if __name__=='__main__': raise SystemExit(main())
