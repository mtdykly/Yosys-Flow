"""Controlled F / P-module / P-fine comparisons; independent M is excluded."""
from collections import Counter, defaultdict
import csv
import hashlib
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


def suite_definition(suite, requested=None):
    if suite == 'epfl-functional':
        from . import epfl
        return epfl.CASES, epfl.select_cases(requested), epfl.prepare, epfl.MANIFEST
    from . import experiment_cases
    return (experiment_cases.CASES, experiment_cases.select_cases(suite, requested),
            experiment_cases.prepare, ROOT/'benchmarks/anubis/comparison-cases.json')


def selected_methods(suite, requested=None):
    methods = list(requested) if requested is not None else (
        ['F', 'P-fine'] if suite == 'epfl-functional' else ['F', 'P-module', 'P-fine'])
    require(bool(methods) and len(methods) == len(set(methods)), 'methods must be nonempty and unique')
    require(set(methods) <= {'F', 'P-module', 'P-fine'}, 'unknown comparison method')
    require('F' in methods, 'comparison methods must include F as the reference')
    return methods


def baseline_identity(suite, fixture, mode):
    """Share only byte-identical old inputs under the same top and flow."""
    identity = {'suite': suite, 'source_sha256': digest(fixture['base_source']),
                'top': fixture['top'], 'mode': mode}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def directory_bytes(directory):
    """Logical bytes under this job only; do not follow symlinks elsewhere."""
    size = 0
    for root, _, names in os.walk(directory, followlinks=False):
        for name in names:
            path = Path(root)/name
            try:
                if not path.is_symlink():
                    size += path.stat().st_size
            except FileNotFoundError:  # concurrent atomic output replacement
                pass
    return size


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
    peak, timed_out, limited = 0, False, False
    byte_limit = job.get('archive_byte_limit')
    last_size_check, peak_bytes = started, 0
    with (directory/'worker.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'incremental.experiment_worker',
                                    str(directory/'job.json')], cwd=ROOT, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while process.poll() is None:
            rss, children = process_tree(process.pid)
            peak = max(peak, rss)
            now = time.perf_counter()
            timed_out = now-started > timeout
            if byte_limit is not None and now-last_size_check >= 1:
                peak_bytes = max(peak_bytes, directory_bytes(directory))
                last_size_check = now
                limited = peak_bytes > byte_limit
            if timed_out or limited:
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
    if byte_limit is not None:
        peak_bytes = max(peak_bytes, directory_bytes(directory))
        limited = limited or peak_bytes > byte_limit
    path = directory/'worker-result.json'
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        result = {'status': 'failed', 'failure': {'code': 'worker_result_missing_or_invalid', 'detail': 'See worker.log'}}
    if limited:
        result.update(status='resource_limit', failure={'code': 'measurement_output_byte_limit',
            'limit_bytes': byte_limit, 'observed_bytes': peak_bytes}, netlist=None)
    elif timed_out:
        result.update(status='timeout', failure={'code': 'measurement_wall_timeout', 'seconds': timeout}, netlist=None)
    elif process.returncode:
        result.update(status='failed', failure={'code': 'worker_exit', 'returncode': process.returncode}, netlist=None)
    result.update(measured_wall_seconds=elapsed, sampled_process_tree_rss_kib=peak,
                  measurement_directory=str(directory))
    if job.get('kind') == 'baseline':
        from .experiment_worker import archive_sizes
        result.update(archive_sizes(directory/'base-run'))
        result['archive_complete'] = result['status'] == 'passed'
    if byte_limit is not None:
        result.update(output_byte_limit=byte_limit, sampled_output_bytes=peak_bytes)
    write_json(directory/'measurement.json', result)
    return result


def outcome(result):
    if result['status'] in SUCCESS:
        return 'success'
    failure = result.get('failure', {})
    code = failure.get('code', '')
    text = str(failure).lower()
    if result['status'] == 'resource_limit' or code == 'measurement_output_byte_limit':
        return 'resource_limit'
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


AREA_FIELDS = ('mapped_cell_area', 'mapped_cell_count', 'mapping_seconds',
               'mapping_validation_seconds', 'mapped_netlist_generation_seconds')


def area_summary(samples):
    """Do not mix libraries/units, failed mappings, or blocked generic results."""
    valid = [r for r in samples if r['outcome'] == 'success' and r.get('area_status') == 'passed']
    identities = {(r.get('library_sha256'), r.get('area_unit')) for r in valid}
    uniform = len(identities) == 1 and all(next(iter(identities)))
    result = {'outcomes': dict(Counter(r.get('area_status', 'not_requested') for r in samples)),
              'complete_successful_series': bool(samples) and len(valid) == len(samples) and uniform,
              'successful_medians': {field: None for field in AREA_FIELDS},
              'successful_means': {field: None for field in AREA_FIELDS}}
    if uniform:
        result.update(library_sha256=valid[0]['library_sha256'], unit=valid[0]['area_unit'])
        result['successful_medians'] = {
            field: statistics.median([r[field] for r in valid if r.get(field) is not None])
            if any(r.get(field) is not None for r in valid) else None for field in AREA_FIELDS}
        result['successful_means'] = {
            field: statistics.mean([r[field] for r in valid if r.get(field) is not None])
            if any(r.get(field) is not None for r in valid) else None for field in AREA_FIELDS}
    return result


def area_columns(result, generation_seconds):
    area = result.get('library_area')
    if not area:
        return {'area_status': 'not_attempted'}
    passed = area.get('status') == 'passed'
    mapping_seconds = area.get('mapping_seconds')
    return {'area_status': area['status'], 'library_sha256': area.get('library_sha256'),
            'area_unit': area.get('unit'),
            'mapped_cell_area': area.get('cell_area') if passed else None,
            'mapped_cell_count': area.get('cell_count') if passed else None,
            'mapping_seconds': mapping_seconds,
            'mapping_validation_seconds': area.get('validation_seconds'),
            'mapped_netlist_generation_seconds': generation_seconds+mapping_seconds
            if passed and generation_seconds is not None and mapping_seconds is not None else None,
            'library_area': area}


def prepare_area_library(path, directory, yosys):
    from .liberty_area import prepare_library
    path = Path(path).resolve()
    sha = digest(path)
    lock_path = ROOT/'benchmarks/libraries/nangate45.json'
    lock = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
    known = sha == lock.get('liberty', {}).get('sha256')
    library = prepare_library(path, directory, yosys,
        unit=lock['area']['unit'] if known else 'library_area_units',
        provenance=lock if known else None)
    if known:
        shutil.copyfile(lock_path, directory/'source-lock.json')
        for name, expected in lock['files'].items():
            if name == lock['liberty']['path']:
                continue  # already frozen as library.lib
            source = ROOT/lock['cache_directory']/name
            require(source.is_file(), 'known library provenance file missing: '+str(source)+
                    '; fetch the locked files using benchmarks/libraries/README.md')
            require(digest(source) == expected, 'library provenance hash mismatch: '+name)
            destination = directory/'upstream'/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    library['frozen_files'] = {str(p): digest(p) for p in directory.rglob('*')
                               if p.is_file() and p.name != 'library-metadata.json'}
    write_json(directory/'library-metadata.json', library)
    return library


def prepare_baseline(suite, fixture, mode, directory, common, timeout, cache, tools):
    """The second edit of one original design references the first archive."""
    identity = baseline_identity(suite, fixture, mode)
    key = (identity, common.get('index_backend', 'sqlite'), common.get('baseline_repeat', 1))
    if key in cache:
        return cache[key]
    old_job = {**common, 'kind': 'stock_old', 'source': fixture['base_source'],
               'top': fixture['top'], 'mode': mode}
    old = measured(old_job, directory/'stock-old', timeout)
    base = measured({**old_job, 'kind': 'baseline'}, directory/'traced-old', timeout)
    if base['status'] == 'passed':
        manifest = json.loads((Path(base['base_run'])/'manifest.json').read_text())
        for tool in manifest['tool'].get('abc', []):
            tools['recorded_abc:'+tool['executable']] = {
                'path': tool['executable'], 'sha256': tool['sha256'], 'version': tool['version_output']}
    baseline = {'baseline_id': identity, 'baseline_reference': str(directory),
                'index_backend': key[1], 'baseline_repeat': key[2],
                'source_sha256': digest(fixture['base_source']), 'stock': old, 'recorded': base,
                'base_cells': None}
    if old['status'] == 'passed' and old.get('netlist'):
        from .experiment_metrics import measure_netlist
        try:
            baseline['base_cells'] = measure_netlist(old['netlist'], fixture['top'])['cell_count']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            baseline['metric_error'] = str(exc)
    if base['status'] == old['status'] == 'passed':
        baseline['validated_archive_overhead_seconds'] = base['measured_wall_seconds']-old['measured_wall_seconds']
        baseline['traced_tool_overhead_seconds'] = base['baseline_stages_seconds']['traced']-old['rtl_to_netlist_seconds']
    cache[key] = baseline
    return baseline


def reuse_columns(base_cells, splice):
    return {'base_cells': base_cells,
            **{name+'_fraction': splice.get(name+'_cells')/base_cells
               if base_cells and splice.get(name+'_cells') is not None else None
               for name in ('removed', 'reused')}}


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
                                      'candidate_seconds', 'end_to_end_seconds', 'cell_count', 'logic_depth',
                                      'base_cells', 'removed_fraction', 'reused_fraction')}
    summaries = []
    area = {key: area_summary(samples) for key, samples in groups.items()}
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
        successful = [r for r in samples if r['outcome'] == 'success']
        entry['successful_means'] = {
            field: statistics.mean([r[field] for r in successful if r.get(field) is not None])
            if any(r.get(field) is not None for r in successful) else None for field in stats}
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
        if any('area_status' in row for row in samples):
            entry['library_area'] = area[key]
            reference = area.get((case, mode, 'F', None), {})
            mapped = area[key]
            same_library = (mapped.get('library_sha256'), mapped.get('unit')) == (
                reference.get('library_sha256'), reference.get('unit'))
            if method != 'F' and mapped['complete_successful_series'] and reference.get('complete_successful_series') and same_library:
                current = mapped['successful_medians']
                reference_values = reference['successful_medians']
                if reference_values['mapped_cell_area']:
                    entry['mapped_cell_area_overhead_percent'] = 100*(current['mapped_cell_area']/reference_values['mapped_cell_area']-1)
                if current['mapped_netlist_generation_seconds'] and reference_values['mapped_netlist_generation_seconds']:
                    entry['mapped_generation_speedup'] = reference_values['mapped_netlist_generation_seconds']/current['mapped_netlist_generation_seconds']
        summaries.append(entry)
    return summaries


def publish(out, report):
    report['summary'] = summarize(report['measurements'], report['baselines'])
    report['counts'] = dict(Counter(row['outcome'] for row in report['measurements']))
    if report.get('library'):
        report['area_counts'] = dict(Counter(row.get('area_status', 'not_attempted') for row in report['measurements']))
    write_json(out/'report.json', report)
    fields = ['case', 'group', 'family', 'mode', 'method', 'precheck', 'repeat', 'order', 'status', 'outcome',
              'netlist_generation_seconds', 'post_generation_validation_seconds',
              'candidate_seconds', 'end_to_end_seconds', 'sampled_process_tree_rss_kib',
              'max_process_peak_rss_kib', 'cell_count', 'logic_depth', 'removed_cells', 'reused_cells',
              'base_cells', 'removed_fraction', 'reused_fraction', 'baseline_id', 'qualification_status',
              'local_synthesized_cells', 'input_polarity_inverters', 'output_binding_buffers',
              'area_status', 'library_sha256', 'area_unit', *AREA_FIELDS,
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
    from .experiment_metrics import measure_netlist
    out = Path(args.out).resolve()
    require(not out.exists(), 'refusing to overwrite '+str(out))
    require(args.repeats >= 1, 'comparison requires at least 1 repeat')
    baseline_repeats = getattr(args, 'baseline_repeats', 1)
    require(1 <= baseline_repeats <= args.repeats, 'baseline repeats must be between 1 and online repeats')
    index_backend = getattr(args, 'index_backend', 'sqlite')
    suite = getattr(args, 'suite', 'anubis-functional')
    cases, names, prepare, catalog = suite_definition(suite, args.cases)
    methods = selected_methods(suite, getattr(args, 'methods', None))
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
    library = None
    if getattr(args, 'liberty', None):
        library = prepare_area_library(args.liberty, out/'technology', tools['yosys']['path'])
    code = {str(p.relative_to(ROOT)): digest(p) for folder in ('incremental', 'provenance/yosys_flow')
            for p in (ROOT/folder).rglob('*.py')}
    for name in code:
        snapshot = out/'implementation'/name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, snapshot)
    shutil.copyfile(catalog, out/'catalog.json')
    report = {'schema_version': 2, 'status': 'running', 'methods': methods,
              'independent_module_method_M': 'excluded_by_user',
              'suite': suite,
              'scope': ('EPFL_derived_functional_combinational_edits' if suite == 'epfl-functional'
                        else 'ANUBIS_derived_functional_combinational_edits' if suite == 'anubis-functional'
                        else 'diagnostics_only_not_primary_performance_or_area_comparison'),
              'cases': names, 'repeats': args.repeats, 'seed': args.seed, 'tools': tools,
              'index_backend': index_backend, 'baseline_repeats': baseline_repeats,
              'baseline_trials': {},
              'machine': {'platform': platform.platform(), 'python': sys.version, 'cpu': cpu,
                          'allowed_cpus': sorted(os.sched_getaffinity(0)), 'thread_environment': THREAD_ENV,
                          'cpu_model': next((s.strip() for s in Path('/proc/cpuinfo').read_text().splitlines()
                                             if s.startswith('model name')), 'unknown')},
              'implementation_sha256': code, 'catalog_path': str(catalog), 'catalog_sha256': digest(catalog),
              'fixtures': {}, 'qualifications': {}, 'baselines': {}, 'measurements': [],
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
                  'reuse_fractions': 'removed_cells and reused_cells divided by base_cells: instantiated generic leaf count of stock old netlist; null when unavailable or zero',
                  'baseline_reuse': 'suite, top, mode, old RTL SHA256, backend and baseline repeat must match; edits share the same old archive within each independent trial; online repeat uses its paired baseline',
                  'statistics': 'successful_means are arithmetic means of successful samples only; complete_successful_series is required for comparisons; failures and timeouts are not netlist generation times',
                  'qualification': 'EPFL edits must have an offline two-valued top-output counterexample; failed or unknown qualification keeps every scheduled row as blocked; no qualification cost in generation time',
                  'output_byte_limit': 'optional per-job directory logical-byte guard, sampled every second plus final check; can overshoot; resource_limit is separate from proof failure',
                  'ordering': 'serial seeded shuffle of all methods/repeats; fresh processes, same CPU, no parallel jobs or cold-cache claim',
                  'stage_sums': 'aggregate times overlap component fields; do not sum all columns',
                  'failures': 'all attempts retained; no speedup for failed/blocked candidates; optional actual full fallback measured separately',
                  'new_full_reference_is_still_executed': True,
                  'M_comparison_available': False,
                  'continuous_multi_edit_updates_tested': False},
              'fallback_full_enabled': args.fallback_full,
              'job_timeout': args.job_timeout,
              'archive_byte_limit': getattr(args, 'archive_byte_limit', None)}
    if library:
        report['library'] = library
        report['measurement_rules'].update(
            library_area='uniform whole-netlist flatten and ABC Liberty mapping of each successful generic result; sum library area over all mapped cells; no placement/routing area or incremental mapped-cell reuse claim',
            mapping_time='mapping_seconds ends after complete mapped JSON/RTLIL/Verilog writes; library preparation is once per experiment',
            mapped_generation_time='netlist_generation_seconds + mapping_seconds per sample; excludes generic and mapped post-generation validation',
            mapping_validation='full-output equivalence of mapped JSON and Verilog readback to generic input, no unknown/unmapped cells; failed measurements have null area',
            end_to_end='isolated worker launch through exit, includes generic validations plus optional library mapping and mapping validation')
    common = {'yosys': tools['yosys']['path'], 'native_yosys': tools['native_yosys']['path'], 'cpu': cpu,
              'index_backend': index_backend,
              'equivalence_timeout': args.equivalence_timeout, 'equivalence_budget': args.equivalence_budget,
              'archive_byte_limit': getattr(args, 'archive_byte_limit', None)}
    modes = ['retained', 'flatten'] if args.mode == 'both' else [args.mode]
    tasks = []
    baseline_cache = {}
    started = time.perf_counter()
    for name in names:
        fixture = prepare(name, out/'fixtures'/name)
        fixture['source_size'] = {version: {
            'physical_lines': len(Path(fixture[version+'_source']).read_text().splitlines()),
            'bytes': Path(fixture[version+'_source']).stat().st_size}
            for version in ('base', 'new')}
        fixture['frozen_files'] = {str(p): digest(p) for p in Path(fixture['fixture_manifest']).parent.rglob('*') if p.is_file()}
        report['fixtures'][name] = fixture
        if suite == 'epfl-functional':
            report['qualifications'][name] = measured({**common, 'kind': 'qualification',
                'fixture': fixture, 'qualification_timeout': args.job_timeout},
                out/'qualification'/name, args.job_timeout)
            print(f'qualified {name}: {report["qualifications"][name]["status"]}', flush=True)
        for mode in modes:
            directory = out/'preparation'/fixture.get('baseline_key', name)/mode
            if suite == 'epfl-functional':
                directory /= baseline_identity(suite, fixture, mode)[:16]
            trials = []
            for trial in range(1, baseline_repeats+1):
                trial_directory = directory if baseline_repeats == 1 else directory/f'trial-{trial}'
                if report['qualifications'].get(name, {}).get('status', 'passed') == 'passed':
                    baseline = prepare_baseline(suite, fixture, mode, trial_directory,
                        {**common, 'baseline_repeat': trial}, args.job_timeout, baseline_cache, tools)
                else:
                    baseline = {'baseline_id': baseline_identity(suite, fixture, mode),
                                'base_cells': None,
                                'stock': {'status': 'blocked'}, 'recorded': {'status': 'blocked',
                                    'failure': {'code': 'edit_qualification_not_proven'}}}
                trials.append(baseline)
                print(f'prepared {name} {mode} trial={trial}: old={baseline["stock"]["status"]}, archive={baseline["recorded"]["status"]}', flush=True)
            baseline = trials[0]
            report['baseline_trials'][name+'/'+mode] = trials
            report['baselines'][name+'/'+mode] = baseline
            report['unique_baseline_count'] = len(baseline_cache)
            for repeat in range(1, args.repeats+1):
                if 'F' in methods:
                    tasks.append((name, mode, 'F', None, repeat))
                settings = [True, False] if args.precheck == 'both' and cases[name]['category'] == 'nochange' else [args.precheck != 'off']
                for precheck in settings:
                    tasks.extend((name, mode, method, precheck, repeat) for method in methods if method != 'F')
            publish(out, report)
            print(f'prepared {name} {mode}: old={baseline["stock"]["status"]}, archive={baseline["recorded"]["status"]}', flush=True)
    random.Random(args.seed).shuffle(tasks)
    write_json(out/'schedule.json', tasks)
    for order, (name, mode, method, precheck, repeat) in enumerate(tasks, 1):
        fixture = report['fixtures'][name]
        baseline = report['baseline_trials'][name+'/'+mode][(repeat-1) % baseline_repeats]
        base = baseline['recorded']
        qualification = report['qualifications'].get(name)
        job = {**common, 'source': fixture['new_source'], 'top': fixture['top'], 'mode': mode,
               'kind': 'full' if method == 'F' else 'incremental', 'precheck': precheck,
               'selection_mode': 'module' if method == 'P-module' else 'fine', 'base_run': base.get('base_run')}
        if library:
            job['library'] = library
        directory = out/'samples'/f'{order:04d}-{name}-{mode}-{method}-{precheck}-{repeat}'
        if qualification and qualification['status'] != 'passed':
            result = {'status': 'blocked', 'failure': {'code': 'edit_qualification_not_proven',
                      'qualification': qualification}, 'measured_wall_seconds': None}
            directory.mkdir(parents=True)
            write_json(directory/'measurement.json', result)
        elif method != 'F' and base['status'] != 'passed':
            result = {'status': 'blocked', 'failure': {'code': 'baseline_unavailable', 'baseline': base},
                      'measured_wall_seconds': None}
            directory.mkdir(parents=True)
            write_json(directory/'measurement.json', result)
        else:
            result = measured(job, directory, args.job_timeout)
        row = {'case': name, 'group': cases[name]['category'], 'family': cases[name]['family'],
               'mode': mode, 'method': method,
               'baseline_id': baseline['baseline_id'],
               'baseline_reference': baseline.get('baseline_reference'),
               'qualification_status': qualification['status'] if qualification else 'not_required',
               'precheck': precheck, 'repeat': repeat, 'order': order, 'status': result['status'],
               'outcome': outcome(result), 'end_to_end_seconds': result['measured_wall_seconds'],
               'netlist_generation_seconds': None, 'post_generation_validation_seconds': None,
               'candidate_seconds': None, 'failure': result.get('failure'),
               'stages': flatten_timings(result.get('timings', {})),
               'report_path': str(directory/'measurement.json'),
               'max_process_peak_rss_kib': result.get('max_process_peak_rss_kib'),
               'sampled_process_tree_rss_kib': result.get('sampled_process_tree_rss_kib'),
               'selection_strategy': result.get('selection_strategy'),
               'selected_scopes': result.get('selected_scopes'),
               'selection_attempts': result.get('attempts'),
               'statement_plan': result.get('statement_plan'),
               'auto_report': result.get('auto_report'),
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
        if library:
            row.update(area_columns(result, row['netlist_generation_seconds']))
        row.update(reuse_columns(baseline.get('base_cells'), row))
        report['measurements'].append(row)
        publish(out, report)
        print(f'{order}/{len(tasks)} {name} {mode} {method} precheck={precheck}: {row["outcome"]}', flush=True)
    unchanged = hashes_match({ROOT/name: sha for name, sha in code.items()}) and hashes_match({
        tool['path']: tool['sha256'] for tool in tools.values()}) and all(
        hashes_match(fixture['frozen_files']) for fixture in report['fixtures'].values()) and hashes_match({
        catalog: report['catalog_sha256']})
    if library:
        unchanged = unchanged and hashes_match(library['frozen_files'])
    report['implementation_and_tools_unchanged'] = unchanged
    report['implementation_snapshot_matches'] = hashes_match({out/'implementation'/name: sha for name, sha in code.items()})
    report['evidence_files'] = {str(p.relative_to(out)): digest(p) for p in out.rglob('*')
                              if p.is_file() and p != out/'report.json' and p.suffix != '.tmp'}
    report['total_seconds'] = time.perf_counter()-started
    report['status'] = 'completed' if unchanged and report['implementation_snapshot_matches'] else 'invalidated'
    publish(out, report)
    return report
