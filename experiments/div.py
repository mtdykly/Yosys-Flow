"""One pinned large EPFL experiment; synthesis/planning stay in the core CLIs.

prepare checks a concrete full-top difference without synthesizing the design.
run keeps old-only preparation separate from the first new-RTL invocation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

from experiments.epfl import _evaluate, _network, _ranked_targets
from yosys_flow.common import ROOT, PIN, digest, require, token, unescape, write_json

CATALOG = ROOT / 'benchmarks/epfl/div-local.json'
STAGES = ('archive', 'evidence', 'full', 'incremental')


def read(path):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else {}


def fetch_bytes(case, name, expected):
    for path in (ROOT / 'build/epfl/upstream' / name,
                 ROOT / 'build/epfl-large-screen' / (
                     'LICENSE.epfl' if name == 'LICENSE' else 'sources/' + Path(name).name)):
        if path.is_file() and digest(path) == expected:
            return path.read_bytes()
    url = f'https://raw.githubusercontent.com/lsils/benchmarks/{case["commit"]}/{name}'
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    require(hashlib.sha256(data).hexdigest() == expected, 'upstream checksum mismatch: ' + name)
    return data


def prepare(args):
    case = read(CATALOG)
    out = Path(args.out).resolve()
    require(not out.exists(), 'fixture output already exists: ' + str(out))
    raw = fetch_bytes(case, case['upstream_file'], case['base_sha256'])
    license_bytes = fetch_bytes(case, 'LICENSE', case['license_sha256'])
    text = raw.decode()
    graph = _network(text)
    target = _ranked_targets(graph)['local']
    for key, value in case['mutation'].items():
        if key != 'replacement':
            require(target[key] == value, 'frozen source target changed: ' + key)
    require(len(text.splitlines()) == case['physical_lines'] and
            len(graph['assignments']) == case['assignments'], 'source size changed')
    require((len(graph['inputs']), len(graph['outputs'])) ==
            (case['input_bits'], case['output_bits']), 'source ports changed')
    lines = text.splitlines(keepends=True)
    lines[target['line'] - 1] = lines[target['line'] - 1].replace('&', '|', 1)
    revised = ''.join(lines).encode()
    require(hashlib.sha256(revised).hexdigest() == case['new_sha256'], 'revised source checksum mismatch')
    inputs = {f'{port}[{i}]': (int(case['witness'][port + '_hex'], 16) >> i) & 1
              for port in ('a', 'b') for i in range(64)}
    outputs = {'base': _evaluate(graph, inputs), 'new': _evaluate(_network(revised.decode()), inputs)}
    differences = {name: {kind: values[name] for kind, values in outputs.items()}
                   for name in outputs['base'] if outputs['base'][name] != outputs['new'][name]}
    require(differences == case['witness']['differing_outputs'], 'frozen functional witness changed')
    out.mkdir(parents=True)
    (out / 'base.v').write_bytes(raw)
    (out / 'new.v').write_bytes(revised)
    (out / 'LICENSE').write_bytes(license_bytes)
    write_json(out / 'fixture.json', {**case, 'catalog_sha256': digest(CATALOG),
        'qualification': 'pending', 'keep_attributes_or_wrapper_added': False})
    # Replay the SAME concrete vector using an independent Yosys evaluator.
    # This establishes non-equivalence by a witness, not equivalence by simulation.
    sets = ' '.join(f'-set {name} {inputs[unescape(name)]}' for name in graph['inputs'])
    shows = ' '.join('-show ' + name for name in graph['outputs'])
    for kind in ('base', 'new'):
        script = out / (kind + '-witness.ys')
        script.write_text(f'read_verilog {token(out / (kind + ".v"))}\n'
                          f'hierarchy -check -top top\nproc\ncheck -assert\neval {sets} {shows}\n')
        logfile = out / (kind + '-witness.log')
        with logfile.open('w') as stream:
            subprocess.run([str(Path(args.yosys).resolve()), '-Q', '-T', '-s', str(script)],
                           stdout=stream, stderr=subprocess.STDOUT, timeout=120, check=True)
        values = {unescape(name): int(bit) for name, bit in
                  re.findall(r"Eval result:\s+(\S+)\s+=\s+1'([01])\.", logfile.read_text())}
        require(values == outputs[kind], 'Yosys and source witness disagree: ' + str(logfile))
    write_json(out / 'witness.json', {'inputs': inputs, 'outputs': outputs, 'differences': differences,
        'yosys_replay': 'passed', 'source_replay': 'passed',
        'meaning': 'concrete full-top non-equivalence witness; not a source-to-netlist proof'})
    fixture = read(out / 'fixture.json')
    fixture['qualification'] = 'passed'
    write_json(out / 'fixture.json', fixture)
    print(json.dumps({'fixture': str(out), 'qualification': 'passed', 'differences': differences}, indent=2))


def validate_fixture(path):
    case = read(CATALOG)
    fixture = read(path / 'fixture.json')
    require(fixture.get('catalog_sha256') == digest(CATALOG) and
            fixture.get('qualification') == 'passed', 'prepare and qualify the pinned fixture first')
    for name, expected in [('base.v', case['base_sha256']), ('new.v', case['new_sha256'])]:
        require(digest(path / name) == expected, 'fixture changed: ' + name)


def commands(root, fixture, stock, native, capture=None):
    python = sys.executable
    plan = {
        'archive': [python, '-u', '-m', 'yosys_flow', 'run', '--flow', 'synth',
            '--source', str(fixture / 'base.v'), '--top', 'top', '--yosys', stock,
            '--native-yosys', native, '--index-backend', 'sqlite', '--prepare-evidence',
            '--evidence-out', str(root / 'proofs'), '--source-timeout', '600', '--out', str(root / 'base')],
        'evidence': [python, '-u', '-m', 'yosys_flow', 'prepare-evidence', str(root / 'base'),
            '--out', str(root / 'proofs'), '--resume', '--boundary-budget', '0'],
        'full': [stock, '-Q', '-T', '-s', str(root / 'full.ys')],
        'incremental': [python, '-u', '-m', 'incremental', 'auto', '--base-run', str(root / 'base'),
            '--proof-evidence', str(root / 'proofs'), '--new-source', str(fixture / 'new.v'),
            '--yosys', stock, '--native-yosys', native, '--selection-mode', 'fine',
            '--no-equivalence-precheck', '--yosys-timeout', '3600', '--whole-equivalence-timeout', '1200',
            '--region-proof-budget', '60', '--region-node-limit', '4096', '--out', str(root / 'incremental')],
    }
    if capture is not None:
        plan['archive'] = [python, '-u', '-m', 'yosys_flow', 'resume', str(capture),
            '--out', str(root / 'base'), '--yosys', stock, '--native-yosys', native,
            '--index-backend', 'sqlite', '--prepare-evidence', '--evidence-out', str(root / 'proofs'),
            '--source-timeout', '600']
    return plan


def summarize(root):
    archive = read(root / 'base/manifest.json')
    auto = read(root / 'incremental/report.json')
    evidence = read(root / 'proofs/report.json')
    stages = {name: read(root / (name + '.stage.json')) for name in STAGES}
    at = archive.get('timings_seconds', {})
    reused = bool(archive.get('resume', {}).get('reused_capture'))
    capture_seconds = (archive.get('resume', {}).get('source_timings_seconds', {}).get('traced')
                       if reused else at.get('traced'))
    it = auto.get('timings', {})
    replacement = it.get('replacement', {})
    metrics = auto.get('proof_evidence', {}).get('metrics', {})
    full = stages['full'].get('seconds') if stages['full'].get('status') == 'passed' else None
    generation = it.get('netlist_generation_seconds')
    passed = auto.get('status') == 'passed' and stages['incremental'].get('status') == 'passed'
    first = metrics.get('initial_region_cache_entries') == 0 and metrics.get('cache_hits') == 0
    failed_stages = {name: row for name, row in stages.items()
                     if row.get('status') in ('failed', 'timeout', 'interrupted')}
    result = {
        'case': 'epfl_div_local', 'status': 'failed' if failed_stages else
                'passed' if passed else auto.get('status', 'incomplete'),
        'true_first_incremental': first if auto else None, 'stages': stages,
        'instrumented_synthesis_reused': reused,
        'seconds': {
            'instrumented_synthesis_and_writes': capture_seconds,
            'instrumented_synthesis_in_this_run': 0.0 if reused else at.get('traced'),
            'archive_including_capture_and_base_evidence': stages['archive'].get('seconds') if not reused else None,
            'archive_after_reused_capture': stages['archive'].get('seconds') if reused else None,
            'record_validation_including_witness_capture': at.get('record_validation'),
            'index_build': at.get('verified_index_build'),
            'old_stock_reference': at.get('baseline'),
            'old_structural_equivalence': at.get('equivalence'),
            'additional_old_only_evidence': stages['evidence'].get('seconds'),
            'F_complete_new_RTL_to_netlist': full,
            'P_complete_new_RTL_to_candidate': generation,
            'necessary_online_old_region_proof_included_in_P': it.get('required_old_region_proof_seconds'),
            'post_generation_validation': it.get('post_generation_validation_seconds'),
            'whole_equivalence_subset_of_validation': replacement.get('whole_equivalence_seconds'),
            'new_reference_subset_of_validation': replacement.get('new_full_synth_seconds'),
            'incremental_total_checked': it.get('total_seconds'),
        },
        'online_breakdown': it, 'archive_breakdown': at,
        'evidence_status': evidence.get('status'), 'proof_metrics': metrics,
        'failure': auto.get('failure'),
        'failed_stages': failed_stages,
        'speedup_F_over_P': full / generation if passed and first and not failed_stages and full and generation else None,
        'timing_note': 'Nested subitems overlap. F is command wall; P is core application timer. '
                       'Required online proofs stay in P; old preparation and post-validation are separate.',
        'outputs': {'full': str(root / 'full/netlist.v'), 'incremental': str(root / 'incremental/incremental.v')},
    }
    write_json(root / 'summary.json', result)
    return result


def require_first_incremental(root):
    evidence = read(root / 'proofs/report.json')
    require(evidence.get('status') == 'ready' and evidence.get('new_rtl_used') is False and
            evidence.get('edit_target_used') is False, 'old-only evidence is not ready')
    require(not list((root / 'proofs').glob('regions*.sqlite3*')),
            'region cache already exists: refuse to call this a first increment')
    require(not (root / 'incremental').exists(), 'incremental output already exists')


def execute_stage(root, name, command, cpu, timeout, env):
    # Fresh process per stage; never shell-interpolate paths or inherit a region context.
    measured = ['/usr/bin/time', '-f', 'wall_seconds=%e\nuser_seconds=%U\nsystem_seconds=%S\nmax_rss_kib=%M\nexit=%x',
                '-o', str(root / (name + '.time.txt')), 'timeout', '--kill-after=15s', str(timeout) + 's',
                'taskset', '-c', str(cpu), *command]
    print(f'[{name}] starting; timeout {timeout}s; log: {root / (name + ".log")}', flush=True)
    started = time.perf_counter()
    record = {'status': 'running', 'command': command, 'timeout_seconds': timeout, 'cpu': cpu}
    write_json(root / (name + '.stage.json'), record)
    with (root / (name + '.log')).open('w') as logfile:
        process = subprocess.Popen(measured, cwd=ROOT, env=env, stdout=logfile,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                try:
                    code = process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    print(f'[{name}] running, {time.perf_counter() - started:.0f}s; see log/progress files', flush=True)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            record.update(status='interrupted', seconds=time.perf_counter() - started)
            write_json(root / (name + '.stage.json'), record)
            raise
    record.update(status='passed' if code == 0 else 'timeout' if code in (124, 137) else 'failed',
                  returncode=code, seconds=time.perf_counter() - started)
    write_json(root / (name + '.stage.json'), record)
    print(f'[{name}] {record["status"]}, {record["seconds"]:.3f}s', flush=True)
    require(code == 0, 'stage did not complete: ' + name + '; inspect ' + str(root / (name + '.log')))


def run(args):
    root, fixture = Path(args.out).resolve(), Path(args.fixture).resolve()
    stock, native = str(Path(args.yosys).resolve()), str(Path(args.native_yosys).resolve())
    validate_fixture(fixture)
    saved = read(root / 'experiment.json')
    capture_argument = getattr(args, 'resume_capture', None) or saved.get('resume_capture', {}).get('directory')
    capture = Path(capture_argument).resolve() if capture_argument else None
    capture_binding = None
    if capture:
        manifest = read(capture / 'manifest.json')
        config = manifest.get('configuration', {})
        require(manifest.get('flow') == 'synth' and config.get('top') == 'top' and
                config.get('sources') == [str(fixture / 'base.v')] and
                config.get('passes') == ['synth -top top'] and not config.get('defines') and
                not config.get('parameters') and not config.get('include_dirs') and
                manifest.get('capture'), 'resume capture does not match the fixed div baseline')
        require(digest(capture / 'capture.json') == manifest['capture']['sha256'], 'capture descriptor changed')
        capture_binding = {'directory': str(capture), 'manifest_sha256': digest(capture / 'manifest.json'),
                           'capture_sha256': manifest['capture']['sha256']}
    plan = commands(root, fixture, stock, native, capture)
    selected = STAGES if args.stage == 'all' else (args.stage,)
    cpu = min(os.sched_getaffinity(0)) if args.cpu is None else args.cpu
    require(cpu in os.sched_getaffinity(0), 'CPU is outside current affinity')
    if args.dry_run:
        for name in selected:
            print(f'[{name}] ' + shlex.join(plan[name]))
        print('Preview only: no synthesis, proof preparation or output directory created.')
        return
    binding = {'fixture': str(fixture), 'catalog_sha256': digest(CATALOG), 'cpu': cpu,
               'stock': stock, 'stock_sha256': digest(stock), 'native': native, 'native_sha256': digest(native),
               'stream_replay': False, 'new_RTL_supplied_to_old_preparation': False}
    if capture_binding:
        binding['resume_capture'] = capture_binding
    for binary in (stock, native):
        require(PIN in subprocess.check_output([binary, '-V'], text=True), 'unexpected Yosys version')
    if root.exists():
        require(args.stage != 'all', 'use a new output directory for a complete first run')
        require(read(root / 'experiment.json') == binding, 'experiment inputs/tools differ')
    else:
        require(args.stage in ('all', 'archive'), 'start with archive or all')
        root.mkdir(parents=True)
        write_json(root / 'experiment.json', binding)
    print('EPFL div full workflow: ' + str(root), flush=True)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('YOSYS_FLOW_'):
            env.pop(key)
    env.update(YOSYS_FLOW_STREAM_REPLAY='0', YOSYS_FLOW_CAPTURE_PROFILE='1',
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    # Native binary uses its compiled-in instrumented ABC. Stock keeps its own ABC.
    env.pop('ABC', None)
    try:
        for name in selected:
            if name == 'evidence':
                require(read(root / 'base/manifest.json').get('status') == 'complete', 'old archive incomplete')
                evidence = read(root / 'proofs/report.json')
                if (evidence.get('status') == 'ready' and evidence.get('new_rtl_used') is False
                        and evidence.get('edit_target_used') is False):
                    write_json(root / 'evidence.stage.json', {'status': 'reused', 'seconds': 0.0,
                        'reason': 'integrated archive preparation already ready; no second archive load'})
                    print('[evidence] integrated old-only preparation already ready; reused (0 additional seconds)', flush=True)
                    continue
                require((root / 'proofs/work/state.json').is_file(), 'no resumable old proof preparation')
            if name == 'full':
                (root / 'full').mkdir(exist_ok=False)
                (root / 'full.ys').write_text(
                    f'read_verilog -sv -defer {token(fixture / "new.v")}\nhierarchy -check -top top\nsynth -top top\n' +
                    ''.join(f'{cmd} {token(root / "full" / ("netlist." + ext))}\n'
                            for cmd, ext in [('write_json', 'json'), ('write_rtlil', 'il'), ('write_verilog -noattr', 'v')]))
            if name == 'incremental':
                require_first_incremental(root)
            execute_stage(root, name, plan[name], cpu, args.timeout, env)
    finally:
        summary = summarize(root)
        print('Summary: ' + str(root / 'summary.json'), flush=True)
    if args.stage in ('all', 'incremental'):
        require(summary['status'] == 'passed' and summary['true_first_incremental'],
                'first incremental not validated; inspect summary and core report')
    print(json.dumps(summary['seconds'], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='prepare fixed sources and independently replay a functional-change witness')
    prep.add_argument('--out', default=str(ROOT / 'build/epfl-div'))
    runner = sub.add_parser('run', help='old-only archive/evidence, full new synthesis, then true first incremental')
    runner.add_argument('--fixture', default=str(ROOT / 'build/epfl-div'))
    runner.add_argument('--out', required=True)
    runner.add_argument('--stage', choices=('all', *STAGES), default='all')
    runner.add_argument('--dry-run', action='store_true')
    runner.add_argument('--cpu', type=int)
    runner.add_argument('--timeout', type=int, default=7200, help='wall limit per stage; whole equivalence has a 1200 s sublimit')
    runner.add_argument('--native-yosys', default=str(ROOT / 'build/native/install/bin/yosys'))
    runner.add_argument('--resume-capture', help='reuse a complete old capture in a NEW experiment directory; no instrumented synthesis')
    for command in (prep, runner):
        command.add_argument('--yosys', default='/opt/oss-cad-suite/bin/yosys')
    summary = sub.add_parser('summary')
    summary.add_argument('directory')
    args = parser.parse_args()
    if getattr(args, 'timeout', 1) <= 0:
        parser.error('timeout must be positive')
    try:
        if args.command == 'prepare':
            prepare(args)
        elif args.command == 'run':
            run(args)
        else:
            print(json.dumps(summarize(Path(args.directory).resolve()), indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('div experiment: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
