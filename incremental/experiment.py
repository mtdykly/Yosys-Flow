"""Controlled F / P-module / P-fine comparisons; independent M is excluded."""
from collections import Counter, defaultdict
import csv
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import signal
import statistics
import subprocess
import sys
import time

from yosys_flow.common import PIN, ROOT, digest, require, write_json

SUCCESS = {'passed', 'reused_equivalent', 'unchanged'}
THREAD_ENV = {n: '1' for n in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}


def hashes_match(files):
    try:
        return all(digest(path) == sha for path, sha in files.items())
    except OSError:
        return False


def process_tree(pid):
    """Linux sampled RSS sum; shared pages may be counted more than once."""
    pending, seen, rss = [pid], set(), 0
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        try:
            text = Path(f'/proc/{child}/status').read_text()
            rss += next((int(line.split()[1]) for line in text.splitlines() if line.startswith('VmRSS:')), 0)
            for task in Path(f'/proc/{child}/task').iterdir():
                pending.extend(map(int, (task/'children').read_text().split()))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return rss, seen


def measured(job, directory, timeout):
    directory.mkdir(parents=True)
    write_json(directory/'job.json', job)
    env = os.environ.copy()
    env.update(THREAD_ENV)
    env.pop('YOSYS_FLOW_ABC_TRACE', None)
    env['PYTHONHASHSEED'] = '0'
    started = time.perf_counter()
    peak, timed_out = 0, False
    with (directory/'worker.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'incremental.experiment_worker',
                                    str(directory/'job.json')], cwd=ROOT, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while process.poll() is None:
            rss, children = process_tree(process.pid)
            peak = max(peak, rss)
            if time.perf_counter()-started > timeout:
                timed_out = True
                # Kill observed descendants as well as the job group: a child
                # tool may create a new session. No unrelated processes touched.
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.02)
        process.wait()
    elapsed = time.perf_counter()-started
    path = directory/'worker-result.json'
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        result = {'status': 'failed', 'failure': {'code': 'worker_result_missing_or_invalid', 'detail': 'See worker.log'}}
    if timed_out:
        result.update(status='timeout', failure={'code': 'measurement_wall_timeout', 'seconds': timeout}, netlist=None)
    elif process.returncode:
        result.update(status='failed', failure={'code': 'worker_exit', 'returncode': process.returncode}, netlist=None)
    result.update(measured_wall_seconds=elapsed, sampled_process_tree_rss_kib=peak,
                  measurement_directory=str(directory))
    write_json(directory/'measurement.json', result)
    return result


def outcome(result):
    if result['status'] in SUCCESS:
        return 'success'
    failure = result.get('failure', {})
    code = failure.get('code', '')
    text = str(failure).lower()
    if 'timeout' in code or result['status'] == 'timeout':
        return 'timeout'
    if code == 'state_outside_combinational_scope' or any(s in text for s in ('latch', 'clocked', 'state cell')):
        return 'out_of_scope'
    if code in {'new_design_equivalence_failed', 'invalid_recorded_evidence'}:
        return 'validation_failed'
    if result['status'] == 'blocked' or code in {'proof_budget_exhausted', 'source_correspondence_not_proven'}:
        return 'blocked'
    return 'error'


def flatten_timings(value, prefix=''):
    output = {}
    for key, item in value.items():
        name = prefix+key
        if isinstance(item, dict):
            output.update(flatten_timings(item, name+'.'))
        elif isinstance(item, (float, int)):
            output[name] = item
    return output


def summarize(rows, baselines):
    """No pooling successes with refusals, or NOC with functional changes."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row['case'], row['mode'], row['method'], row['precheck'])].append(row)
    medians = {}
    for key, samples in groups.items():
        ok = [r for r in samples if r['outcome'] == 'success']
        medians[key] = {field: statistics.median([r[field] for r in ok if r.get(field) is not None])
                        if any(r.get(field) is not None for r in ok) else None
                        for field in ('netlist_generation_seconds', 'post_generation_validation_seconds',
                                      'candidate_seconds', 'end_to_end_seconds', 'cell_count', 'logic_depth')}
    summaries = []
    for (case, mode, method, precheck), samples in sorted(groups.items(), key=lambda v: str(v[0])):
        key = case, mode, method, precheck
        stats = medians[key]
        full = medians.get((case, mode, 'F', None), {})
        base = baselines.get(case+'/'+mode, {})
        entry = {'case': case, 'mode': mode, 'group': samples[0]['group'],
                 'family': samples[0].get('family'), 'method': method,
                 'precheck': precheck, 'samples': len(samples),
                 'outcomes': dict(Counter(r['outcome'] for r in samples)), 'successful_medians': stats,
                 'complete_successful_series': all(r['outcome'] == 'success' for r in samples)}
        comparable = entry['complete_successful_series'] and all(r['outcome'] == 'success'
            for r in groups.get((case, mode, 'F', None), []))
        if method != 'F' and comparable and stats['candidate_seconds'] and full.get('candidate_seconds'):
            entry.update(candidate_speedup=full['candidate_seconds']/stats['candidate_seconds'],
                         end_to_end_speedup=full['end_to_end_seconds']/stats['end_to_end_seconds'],
                         candidate_seconds_saved=full['candidate_seconds']-stats['candidate_seconds'])
        if method != 'F' and comparable and stats['netlist_generation_seconds'] and full.get('netlist_generation_seconds'):
            saved = full['netlist_generation_seconds']-stats['netlist_generation_seconds']
            entry.update(generation_speedup=full['netlist_generation_seconds']/stats['netlist_generation_seconds'],
                         generation_seconds_saved=saved, break_even_edits=None)
            overhead = base.get('validated_archive_overhead_seconds')
            if saved > 0 and overhead is not None:
                entry['break_even_edits'] = math.ceil(max(0, overhead)/saved)
            entry['break_even_basis'] = 'one measured validated archive overhead / median netlist generation savings; not a multi-edit experiment'
        if method != 'F' and comparable and stats['cell_count'] is not None and full.get('cell_count'):
            entry['generic_cell_overhead_percent'] = 100*(stats['cell_count']/full['cell_count']-1)
        summaries.append(entry)
    return summaries


def publish(out, report):
    report['summary'] = summarize(report['measurements'], report['baselines'])
    report['counts'] = dict(Counter(row['outcome'] for row in report['measurements']))
    write_json(out/'report.json', report)
    fields = ['case', 'group', 'family', 'mode', 'method', 'precheck', 'repeat', 'order', 'status', 'outcome',
              'netlist_generation_seconds', 'post_generation_validation_seconds',
              'candidate_seconds', 'end_to_end_seconds', 'sampled_process_tree_rss_kib',
              'max_process_peak_rss_kib', 'cell_count', 'logic_depth', 'removed_cells', 'reused_cells',
              'local_synthesized_cells', 'input_polarity_inverters', 'output_binding_buffers',
              'fallback_seconds', 'attempt_plus_fallback_seconds', 'report_path']
    with (out/'timings.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['measurements'])
    with (out/'stages.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['case', 'mode', 'method', 'precheck', 'repeat', 'stage', 'seconds'])
        for row in report['measurements']:
            for key, seconds in row['stages'].items():
                writer.writerow([row[n] for n in ('case', 'mode', 'method', 'precheck', 'repeat')]+[key, seconds])


def run(args):
    from .experiment_cases import CASES, prepare, select_cases
    from .experiment_metrics import measure_netlist
    out = Path(args.out).resolve()
    require(not out.exists(), 'refusing to overwrite '+str(out))
    require(args.repeats >= 3, 'comparison requires at least 3 repeats')
    suite = getattr(args, 'suite', 'anubis-functional')
    names = select_cases(suite, args.cases)
    cpu = min(os.sched_getaffinity(0)) if args.cpu is None else args.cpu
    require(cpu in os.sched_getaffinity(0), 'requested CPU is not in current affinity')
    tools = {}
    for name, supplied in [('yosys', args.yosys), ('native_yosys', args.native_yosys)]:
        binary = shutil.which(supplied)
        require(binary, 'tool missing: '+supplied)
        version = subprocess.check_output([binary, '-V'], text=True).strip()
        require(PIN in version, 'comparison requires pinned Yosys: '+version)
        tools[name] = {'path': str(Path(binary).resolve()), 'version': version, 'sha256': digest(binary)}
    stock_abc = Path(tools['yosys']['path']).parent/'yosys-abc'
    require(stock_abc.is_file(), 'stock Yosys sibling ABC is required for the fixed experiment')
    tools['stock_abc'] = {'path': str(stock_abc.resolve()), 'sha256': digest(stock_abc),
                          'version': subprocess.check_output([str(stock_abc), '-c', 'version'], text=True).strip()}
    out.mkdir(parents=True)
    code = {str(p.relative_to(ROOT)): digest(p) for folder in ('incremental', 'provenance/yosys_flow')
            for p in (ROOT/folder).rglob('*.py')}
    for name in code:
        snapshot = out/'implementation'/name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, snapshot)
    shutil.copyfile(ROOT/'benchmarks/anubis/comparison-cases.json', out/'catalog.json')
    report = {'schema_version': 2, 'status': 'running', 'methods': ['F', 'P-module', 'P-fine'],
              'independent_module_method_M': 'excluded_by_user',
              'suite': suite,
              'scope': ('ANUBIS_derived_functional_combinational_edits' if suite == 'anubis-functional'
                        else 'diagnostics_only_not_primary_performance_or_area_comparison'),
              'cases': names, 'repeats': args.repeats, 'seed': args.seed, 'tools': tools,
              'machine': {'platform': platform.platform(), 'python': sys.version, 'cpu': cpu,
                          'allowed_cpus': sorted(os.sched_getaffinity(0)), 'thread_environment': THREAD_ENV,
                          'cpu_model': next((s.strip() for s in Path('/proc/cpuinfo').read_text().splitlines()
                                             if s.startswith('model name')), 'unknown')},
              'implementation_sha256': code, 'catalog_sha256': digest(ROOT/'benchmarks/anubis/comparison-cases.json'),
              'fixtures': {}, 'baselines': {}, 'measurements': [],
              'measurement_rules': {
                  'primary_time_metric': 'netlist_generation_seconds: complete new RTL plus old archive through stitched complete JSON/IL/Verilog; actual cutoff before post-generation validation',
                  'algorithm_costs_included': 'archive loading/replay, new frontend, diff, enabled precheck, region/contract/boundary decisions, patch export, local synthesis, binding and splice guards',
                  'post_generation_validation': 'separately measured; never included in generation speedup; still required before a sample is admitted as a correct result',
                  'reuse_generation_time': 'null for diagnostic direct-reuse path, whose proof and copying phases are interleaved; do not infer by subtracting timings',
                  'candidate': 'all automatic work up to candidate checks; includes record replay, proof and local synthesis; excludes full-new reference and final validation',
                  'end_to_end': 'isolated worker launch through exit, includes current full reference synthesis and validations when executed',
                  'F': 'independent stock Yosys from complete RTL to generic netlist; no new.frontend.il input',
                  'memory': '20ms sampled sum of live process-tree RSS (shared pages may double-count), plus maximum individual process high-water RSS',
                  'counts': 'unweighted instantiated generic leaf cells as a structural area proxy, not Liberty or physical area; unit-gate depth is not physical delay',
                  'ordering': 'serial seeded shuffle of all methods/repeats; fresh processes, same CPU, no parallel jobs or cold-cache claim',
                  'stage_sums': 'aggregate times overlap component fields; do not sum all columns',
                  'failures': 'all attempts retained; no speedup for failed/blocked candidates; optional actual full fallback measured separately',
                  'new_full_reference_is_still_executed': True,
                  'M_comparison_available': False,
                  'continuous_multi_edit_updates_tested': False},
              'fallback_full_enabled': args.fallback_full}
    common = {'yosys': tools['yosys']['path'], 'native_yosys': tools['native_yosys']['path'], 'cpu': cpu,
              'equivalence_timeout': args.equivalence_timeout, 'equivalence_budget': args.equivalence_budget}
    modes = ['retained', 'flatten'] if args.mode == 'both' else [args.mode]
    tasks = []
    started = time.perf_counter()
    for name in names:
        fixture = prepare(name, out/'fixtures'/name)
        fixture['frozen_files'] = {str(p): digest(p) for p in Path(fixture['fixture_manifest']).parent.rglob('*') if p.is_file()}
        report['fixtures'][name] = fixture
        for mode in modes:
            directory = out/'preparation'/name/mode
            old_job = {**common, 'kind': 'stock_old', 'source': fixture['base_source'], 'top': fixture['top'], 'mode': mode}
            old = measured(old_job, directory/'stock-old', args.job_timeout)
            base = measured({**old_job, 'kind': 'baseline'}, directory/'traced-old', args.job_timeout)
            if base['status'] == 'passed':
                manifest = json.loads((Path(base['base_run'])/'manifest.json').read_text())
                for tool in manifest['tool'].get('abc', []):
                    tools['recorded_abc:'+tool['executable']] = {
                        'path': tool['executable'], 'sha256': tool['sha256'], 'version': tool['version_output']}
            baseline = {'stock': old, 'recorded': base}
            if base['status'] == old['status'] == 'passed':
                baseline['validated_archive_overhead_seconds'] = base['measured_wall_seconds']-old['measured_wall_seconds']
                baseline['traced_tool_overhead_seconds'] = base['baseline_stages_seconds']['traced']-old['rtl_to_netlist_seconds']
            report['baselines'][name+'/'+mode] = baseline
            for repeat in range(1, args.repeats+1):
                tasks.append((name, mode, 'F', None, repeat))
                settings = [True, False] if args.precheck == 'both' and CASES[name]['category'] == 'nochange' else [args.precheck != 'off']
                for precheck in settings:
                    tasks.extend((name, mode, method, precheck, repeat) for method in ('P-module', 'P-fine'))
            publish(out, report)
            print(f'prepared {name} {mode}: old={old["status"]}, archive={base["status"]}', flush=True)
    random.Random(args.seed).shuffle(tasks)
    write_json(out/'schedule.json', tasks)
    for order, (name, mode, method, precheck, repeat) in enumerate(tasks, 1):
        fixture = report['fixtures'][name]
        base = report['baselines'][name+'/'+mode]['recorded']
        job = {**common, 'source': fixture['new_source'], 'top': fixture['top'], 'mode': mode,
               'kind': 'full' if method == 'F' else 'incremental', 'precheck': precheck,
               'selection_mode': 'module' if method == 'P-module' else 'fine', 'base_run': base.get('base_run')}
        directory = out/'samples'/f'{order:04d}-{name}-{mode}-{method}-{precheck}-{repeat}'
        if method != 'F' and base['status'] != 'passed':
            result = {'status': 'blocked', 'failure': {'code': 'baseline_unavailable', 'baseline': base},
                      'measured_wall_seconds': None}
            directory.mkdir(parents=True)
            write_json(directory/'measurement.json', result)
        else:
            result = measured(job, directory, args.job_timeout)
        row = {'case': name, 'group': CASES[name]['category'], 'family': CASES[name]['family'],
               'mode': mode, 'method': method,
               'precheck': precheck, 'repeat': repeat, 'order': order, 'status': result['status'],
               'outcome': outcome(result), 'end_to_end_seconds': result['measured_wall_seconds'],
               'netlist_generation_seconds': None, 'post_generation_validation_seconds': None,
               'candidate_seconds': None, 'failure': result.get('failure'),
               'stages': flatten_timings(result.get('timings', {})),
               'report_path': str(directory/'measurement.json'),
               'max_process_peak_rss_kib': result.get('max_process_peak_rss_kib'),
               'sampled_process_tree_rss_kib': result.get('sampled_process_tree_rss_kib'),
               'selection_strategy': result.get('selection_strategy'),
               'equivalence_precheck': result.get('equivalence_precheck')}
        if method == 'F' and result.get('rtl_to_netlist_seconds') is not None:
            row['stages']['rtl_to_netlist_seconds'] = result['rtl_to_netlist_seconds']
        if row['outcome'] == 'success':
            timing_source = result if method == 'F' else result.get('timings', {})
            for field in ('netlist_generation_seconds', 'post_generation_validation_seconds'):
                row[field] = timing_source.get(field)
            row['candidate_seconds'] = result.get('rtl_to_netlist_seconds') if method == 'F' else result['timings'].get('incremental_candidate_ready_seconds')
            try:
                row['netlist_metrics'] = measure_netlist(result['netlist'], fixture['top'])
                row['cell_count'] = row['netlist_metrics']['cell_count']
                row['logic_depth'] = row['netlist_metrics']['logic_depth']
            except (OSError, ValueError, KeyError, TypeError) as exc:
                row.update(outcome='measurement_error', metric_error=str(exc))
            row.update({k: v for k, v in result.get('splice', {}).items() if isinstance(v, int)})
            if result['status'] in {'reused_equivalent', 'unchanged'}:
                row.update(removed_cells=0, reused_cells=row.get('cell_count'), local_synthesized_cells=0,
                           input_polarity_inverters=0, output_binding_buffers=0)
        elif args.fallback_full and method != 'F' and result['measured_wall_seconds'] is not None:
            fallback = measured({**job, 'kind': 'full'}, directory/'fallback', args.job_timeout)
            row['fallback_status'] = fallback['status']
            row['fallback_seconds'] = fallback['measured_wall_seconds']
            row['attempt_plus_fallback_seconds'] = result['measured_wall_seconds']+fallback['measured_wall_seconds']
        report['measurements'].append(row)
        publish(out, report)
        print(f'{order}/{len(tasks)} {name} {mode} {method} precheck={precheck}: {row["outcome"]}', flush=True)
    unchanged = hashes_match({ROOT/name: sha for name, sha in code.items()}) and hashes_match({
        tool['path']: tool['sha256'] for tool in tools.values()}) and all(
        hashes_match(fixture['frozen_files']) for fixture in report['fixtures'].values()) and hashes_match({
        ROOT/'benchmarks/anubis/comparison-cases.json': report['catalog_sha256']})
    report['implementation_and_tools_unchanged'] = unchanged
    report['implementation_snapshot_matches'] = hashes_match({out/'implementation'/name: sha for name, sha in code.items()})
    report['evidence_files'] = {str(p.relative_to(out)): digest(p) for p in out.rglob('*')
                              if p.is_file() and p != out/'report.json' and p.suffix != '.tmp'}
    report['total_seconds'] = time.perf_counter()-started
    report['status'] = 'completed' if unchanged and report['implementation_snapshot_matches'] else 'invalidated'
    publish(out, report)
    return report
