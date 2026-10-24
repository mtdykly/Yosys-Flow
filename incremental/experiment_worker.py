"""Isolated measurement worker; invoked by the comparison runner, not a CLI API."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
from types import SimpleNamespace

from yosys_flow.common import identifier, token, write_json
from .failures import classify


def archive_sizes(base):
    files = [p for p in base.rglob('*') if p.is_file()]
    return {'archive_bytes': sum(p.stat().st_size for p in files),
            'jsonl_bytes': sum(p.stat().st_size for p in files if p.suffix == '.jsonl'),
            # Native artifact names are yosys.jsonl.abc_N.*, not abc-*.
            'abc_evidence_bytes': sum(p.stat().st_size for p in files if '.abc_' in p.name)}


def full(job, directory):
    """F always starts from complete RTL, never the automatic frontend archive."""
    directory.mkdir()
    top = identifier(job['top'])
    source = token(job['source'])
    flatten = ' -flatten' if job['mode'] == 'flatten' else ''
    script = (f'read_verilog -sv -defer {source}\nhierarchy -check -top {top}\n'
              f'synth -top {top}{flatten}\n'
              'write_json netlist.json\nwrite_rtlil netlist.il\nwrite_verilog -noattr netlist.v\n')
    (directory/'full.ys').write_text(script)
    started = time.perf_counter()
    with (directory/'full.log').open('w') as log:
        result = subprocess.run([job['yosys'], '-Q', '-T', '-s', 'full.ys'],
                                cwd=directory, stdout=log, stderr=subprocess.STDOUT)
    seconds = time.perf_counter()-started
    validation_seconds = None
    validation_code = None
    if result.returncode == 0:
        (directory/'post-check.ys').write_text(
            f'read_json netlist.json\nhierarchy -check -top {top}\ncheck -assert\n')
        validation_started = time.perf_counter()
        with (directory/'post-check.log').open('w') as log:
            checked = subprocess.run([job['yosys'], '-Q', '-T', '-s', 'post-check.ys'],
                                     cwd=directory, stdout=log, stderr=subprocess.STDOUT)
        validation_seconds = time.perf_counter()-validation_started
        validation_code = checked.returncode
    passed = result.returncode == 0 and validation_code == 0
    return {'status': 'passed' if passed else 'failed',
            'returncode': result.returncode, 'rtl_to_netlist_seconds': seconds,
            'netlist_generation_seconds': seconds,
            'post_generation_validation_seconds': validation_seconds,
            'validation_returncode': validation_code,
            'netlist': str(directory/'netlist.json') if passed else None,
            'input_representation': 'complete_new_RTL' if job['kind'] == 'full' else 'complete_old_RTL'}


def execute(job, directory):
    if job.get('cpu') is not None:
        os.sched_setaffinity(0, {job['cpu']})
    started = time.perf_counter()
    try:
        if job['kind'] in {'full', 'stock_old'}:
            result = full(job, directory/'full')
        elif job['kind'] == 'baseline':
            from yosys_flow.cli import parser
            from yosys_flow.runner import run
            base = directory/'base-run'
            run(parser().parse_args(['run', '--source', job['source'], '--top', job['top'],
                '--out', str(base), '--yosys', job['yosys'], '--native-yosys', job['native_yosys'],
                *(['--flatten'] if job['mode'] == 'flatten' else [])]))
            manifest = json.loads((base/'manifest.json').read_text())
            result = {'status': 'passed', 'base_run': str(base),
                      'baseline_stages_seconds': manifest['timings_seconds'],
                      **archive_sizes(base)}
        else:
            from .automatic import run
            result = run(SimpleNamespace(base_run=job['base_run'], new_source=[job['source']],
                out=str(directory/'auto'), yosys=job['yosys'], native_yosys=job['native_yosys'],
                selection_mode=job['selection_mode'], no_equivalence_precheck=not job['precheck'],
                equivalence_timeout=job['equivalence_timeout'], equivalence_budget=job['equivalence_budget']))
            result['auto_report'] = str(directory/'auto/report.json')
            result['netlist'] = str(directory/'auto/incremental.json') if result['status'] in {
                'passed', 'reused_equivalent', 'unchanged'} else None
    except Exception as exc:
        result = {'status': 'failed', 'failure': classify(job['kind'], exc)}
    result['worker_seconds'] = time.perf_counter()-started
    # ru_maxrss is a per-process high water mark, NOT simultaneous tree usage.
    result['max_process_peak_rss_kib'] = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                           resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    write_json(directory/'worker-result.json', result)
    return result


if __name__ == '__main__':
    path = Path(sys.argv[1]).resolve()
    execute(json.loads(path.read_text()), path.parent)
