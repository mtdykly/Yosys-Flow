"""Diagnose existing capture without synthesis, index publication or archive promotion."""
import argparse
import faulthandler
import json
from pathlib import Path
import resource
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from yosys_flow.native import NativeTrace
from yosys_flow.progress import ArchiveProgress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--out', type=Path, required=True, help='new diagnostic directory')
    parser.add_argument('--with-abc', action='store_true', help='also validate internal ABC evidence and correspondence')
    parser.add_argument('--timeout', type=int, default=360, help='diagnostic budget in seconds')
    parser.add_argument('--memory-gib', type=int, default=32, help='address-space cap for this diagnostic process')
    args = parser.parse_args()
    if min(args.timeout, args.memory_gib) <= 0:
        parser.error('budgets must be positive')
    if args.out.resolve().is_relative_to(args.directory.resolve()):
        parser.error('--out must be outside the original capture directory')
    args.out.mkdir(parents=True, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_AS, (args.memory_gib*2**30, args.memory_gib*2**30))
    progress = ArchiveProgress(args.out/'progress.json')
    started = time.perf_counter()
    def interrupted(number, frame):
        faulthandler.dump_traceback()
        raise TimeoutError(f'diagnostic interrupted by signal {number}')
    signal.signal(signal.SIGALRM, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    faulthandler.register(signal.SIGUSR1)
    signal.alarm(args.timeout)
    report = {'diagnostic_only': True, 'archive_published': False,
              'input_directory': str(args.directory.resolve()), 'abc_requested': args.with_abc,
              'checks': {}}
    try:
        trace = NativeTrace(args.directory/'yosys.jsonl', progress=progress)
        report['checks']['native_replay'] = True
        progress('final_netlist_check')
        trace.compare_netlist(args.directory/'after.json')
        report['checks']['final_netlist_match'] = True
        if args.with_abc:
            trace.compare_abc_artifacts(args.directory, progress=progress)
            report['checks']['abc_artifacts'] = True
        report.update(status='passed', records=len(trace.records), final_cells=len(trace.final))
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}', failed_stage=progress.stage)
    finally:
        signal.alarm(0)
        report.update(elapsed_seconds=time.perf_counter()-started,
                      peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        progress.finish('complete' if report['status']=='passed' else 'failed')
        (args.out/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
