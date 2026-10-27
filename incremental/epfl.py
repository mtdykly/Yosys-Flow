"""Pinned EPFL source mutations and offline top-level difference qualification.

The mutation catalog is fixed before an incremental run. Its source coordinates
and witnesses are experiment evidence, never planner selectors or patch inputs.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request

from yosys_flow.common import ROOT, digest, require, token, unescape, write_json


CORPUS = ROOT / 'benchmarks/epfl'
CACHE = ROOT / 'build/epfl/upstream'
LOCK = CORPUS / 'lock.json'
MANIFEST = CORPUS / 'cases.json'
CATALOG = json.loads(MANIFEST.read_text())
CASES = {row['name']: row for row in CATALOG['cases']}
require(len(CASES) == len(CATALOG['cases']), 'duplicate EPFL case names')
_ID = r'(?:\\[^\s,;]+|[A-Za-z_][A-Za-z0-9_$]*)'
_TERM = r"(~?)\s*(" + _ID + r"|1'b[01])"
_EXPR = re.compile(_TERM + r'(?:\s*([&|^])\s*' + _TERM + r')?\s*$')


def select_cases(requested=None):
    names = list(CASES if requested is None else requested)
    require(names and len(names) == len(set(names)) and set(names) <= CASES.keys(),
            'unknown or repeated EPFL cases')
    return names


def fetch(directory=CACHE, timeout=30):
    """Explicit download command; normal preparation uses the verified cache."""
    directory = Path(directory)
    lock = json.loads(LOCK.read_text())
    for name, expected in lock['files'].items():
        target = directory / name
        if target.is_file() and digest(target) == expected:
            continue
        url = f'https://raw.githubusercontent.com/lsils/benchmarks/{lock["commit"]}/{name}'
        with urllib.request.urlopen(url, timeout=timeout) as response:
            contents = response.read()
        require(hashlib.sha256(contents).hexdigest() == expected,
                'EPFL download checksum mismatch: ' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_bytes(contents)
        temporary.replace(target)
    return verify_corpus(directory)


def verify_corpus(directory=CACHE):
    lock = json.loads(LOCK.read_text())
    for name, expected in lock['files'].items():
        path = Path(directory) / name
        require(path.is_file(), 'EPFL source missing; run python3 -m incremental.epfl fetch: ' + name)
        require(digest(path) == expected, 'EPFL upstream checksum mismatch: ' + name)
    return lock


def _network(text):
    """Strict scalar-assign parser for the pinned structural sources.

    This is only a fixture audit/witness simulator. It is not the incremental
    front end and deliberately rejects constructs outside the pinned format.
    """
    require(re.search(r'\bmodule\s+top\s*\(', text) is not None,
            'EPFL fixture must keep its original top module')
    ports = {}
    for direction in ('input', 'output'):
        ports[direction] = []
        for declaration in re.finditer(r'\b' + direction + r'\s+([^;]+);', text):
            for piece in declaration[1].split(','):
                name = piece.strip()
                require(re.fullmatch(_ID, name) is not None, 'unsupported EPFL port declaration')
                ports[direction].append(name)
    require(ports['input'] and ports['output'], 'missing EPFL port declarations')
    known = set(ports['input']) | {"1'b0", "1'b1"}
    assignments = []
    for line, original in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r'\s*assign\s+(' + _ID + r')\s*=\s*(.*?)\s*;\s*', original)
        if not match:
            require('assign' not in original, 'unsupported EPFL assignment')
            continue
        expression = _EXPR.fullmatch(match[2])
        require(expression is not None, 'unsupported EPFL Boolean expression')
        inv_a, a, op, inv_b, b = expression.groups()
        dependencies = [a] + ([b] if b else [])
        require(match[1] not in known and all(d in known for d in dependencies),
                'EPFL assignment has a cycle, unknown source or multiple driver')
        known.add(match[1])
        assignments.append({'line': line, 'target': match[1], 'original': original,
                            'a': a, 'b': b, 'invert_a': bool(inv_a),
                            'invert_b': bool(inv_b), 'operator': op})
    require(set(ports['output']) <= known, 'undriven EPFL output')
    require(len(assignments) == text.count('assign '), 'incomplete EPFL assignment parse')
    return {'inputs': ports['input'], 'outputs': ports['output'], 'assignments': assignments}


def _ranked_targets(network):
    """Rank by source graph only, independently of any synthesis/selection."""
    reachable = {name: 1 << i for i, name in enumerate(network['outputs'])}
    distance = {name: 0 for name in network['outputs']}
    fanout = Counter()
    for row in reversed(network['assignments']):
        name = row['target']
        for dependency in [row['a']] + ([row['b']] if row['b'] else []):
            fanout[dependency] += 1
            reachable[dependency] = reachable.get(dependency, 0) | reachable.get(name, 0)
            if name in distance:
                distance[dependency] = max(distance.get(dependency, 0), distance[name] + 1)
    candidates = [row for row in network['assignments']
                  if re.fullmatch(r'n\d+', row['target']) and row['operator'] == '&'
                  and reachable.get(row['target'], 0)]
    def counts(row):
        name = row['target']
        return reachable[name].bit_count(), distance[name], fanout[name]
    local = min(candidates, key=lambda row: (counts(row)[0], counts(row)[1], row['line']))
    shared = max(candidates, key=lambda row: (counts(row)[0], counts(row)[2], -row['line']))
    result = {}
    for kind, row in [('local', local), ('shared', shared)]:
        outputs, depth, users = counts(row)
        result[kind] = {**row, 'reachable_output_count': outputs,
                        'max_source_distance_to_output': depth, 'direct_users': users}
    require(result['shared']['reachable_output_count'] > 1, 'EPFL shared mutation is not shared')
    return result


def prepare(name, directory):
    require(name in CASES, 'unknown EPFL case: ' + str(name))
    lock = verify_corpus()
    case = deepcopy(CASES[name])
    source = (CACHE / case['upstream_file']).read_bytes()
    text = source.decode('utf-8')
    graph = _network(text)
    target = _ranked_targets(graph)[case['variant']]
    for key in ('line', 'target', 'original', 'reachable_output_count',
                'max_source_distance_to_output', 'direct_users'):
        require(target[key] == case['mutation'][key], 'EPFL frozen topology selection mismatch: ' + key)
    lines = text.splitlines(keepends=True)
    old = lines[target['line'] - 1]
    require(old.count('&') == 1, 'EPFL mutation must change one binary AND operator')
    lines[target['line'] - 1] = old.replace('&', '|', 1)
    changed = ''.join(lines).encode('utf-8')
    require(hashlib.sha256(changed).hexdigest() == case['new_source_sha256'],
            'EPFL mutated source checksum mismatch')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'base.v').write_bytes(source)
    (directory / 'new.v').write_bytes(changed)
    (directory / 'LICENSE').write_bytes((CACHE / 'LICENSE').read_bytes())
    manifest = {
        'schema_version': 1, 'comparison_case': case, 'upstream': lock,
        'comparison_catalog_sha256': digest(MANIFEST),
        'comparison_generator_sha256': digest(__file__),
        'scope': 'EPFL_original_single_module_with_one_fixed_operator_mutation',
        'top': 'top', 'baseline_key': 'epfl_' + case['circuit'],
        'planner_inputs': 'archived_old_run_and_complete_new_RTL_only',
        'original_historical_commit_pair_claimed': False,
        'expected_change_is_not_measured_verdict': True,
        'input_bits': len(graph['inputs']), 'output_bits': len(graph['outputs']),
        'source_assignments': len(graph['assignments']), 'physical_lines': len(text.splitlines()),
        'changed_line_count': 1, 'port_or_module_rename': False,
        'added_keep_or_wrapper': False,
        'fixture_inputs': {p.name: digest(p) for p in sorted(directory.iterdir()) if p.is_file()},
    }
    write_json(directory / 'fixture.json', manifest)
    return {'case': name, 'top': 'top', 'base_source': str(directory / 'base.v'),
            'new_source': str(directory / 'new.v'), 'fixture_manifest': str(directory / 'fixture.json'),
            'baseline_key': manifest['baseline_key'], 'family': case['family'],
            'category': case['category'], 'expected_change': case['expected_change'],
            'source_hashes': {'base': digest(directory / 'base.v'), 'new': digest(directory / 'new.v')}}


def _evaluate(network, inputs):
    require(set(inputs) == {unescape(n) for n in network['inputs']}, 'witness input ports do not match')
    require(all(type(value) is int and value in (0, 1) for value in inputs.values()),
            'witness requires concrete two-state input bits')
    values = {name: inputs[unescape(name)] for name in network['inputs']}
    values.update({"1'b0": 0, "1'b1": 1})
    for row in network['assignments']:
        left = values[row['a']] ^ row['invert_a']
        if row['operator']:
            right = values[row['b']] ^ row['invert_b']
            left = {'&': left & right, '|': left | right, '^': left ^ right}[row['operator']]
        values[row['target']] = int(left)
    return {unescape(name): values[name] for name in network['outputs']}


def _wave_bits(wave):
    values = {}
    for signal in wave['signal']:
        require(isinstance(signal, dict) and 'name' in signal, 'unsupported SAT witness signal')
        name = unescape(signal['name'])
        if name == 'clk':
            continue
        sequence = signal.get('wave', '')
        value = signal.get('data', [''])[0] if sequence.startswith('=') else sequence[:1]
        require(value in ('0', '1'), 'SAT witness contains unknown or non-scalar values')
        require(name not in values, 'duplicate SAT witness signal')
        values[name] = int(value)
    return values


def qualify(fixture, directory, yosys, timeout=60):
    """Find and independently replay a concrete full-top output difference.

    No synth/techmap/ABC optimization is needed. Only fixed-source frontend,
    proc/flatten and a two-state SAT miter run; this is offline case eligibility.
    Failure/timeout/unknown never qualifies a functional-change experiment.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    result = {'status': 'failed', 'method': 'full_top_binary_miter_SAT_plus_source_replay',
              'offline_not_incremental_generation': True, 'timeout_seconds': timeout}
    try:
        require(fixture['top'] == 'top', 'EPFL qualification expects original top')
        base, new = Path(fixture['base_source']), Path(fixture['new_source'])
        original_hashes = {'base': digest(base), 'new': digest(new)}
        require(original_hashes == fixture['source_hashes'], 'EPFL fixture changed before qualification')
        base_graph, new_graph = _network(base.read_text()), _network(new.read_text())
        require(base_graph['inputs'] == new_graph['inputs'] and base_graph['outputs'] == new_graph['outputs'],
                'EPFL mutation changed module interface')
        script = '\n'.join([
            f'read_verilog {token(base)}', 'hierarchy -check -top top', 'proc', 'rename top epfl_gold',
            'design -stash gold', f'read_verilog {token(new)}', 'hierarchy -check -top top',
            'proc', 'rename top epfl_gate', 'design -stash gate',
            'design -copy-from gold epfl_gold', 'design -copy-from gate epfl_gate',
            'miter -equiv -make_outputs -flatten epfl_gold epfl_gate epfl_miter',
            'hierarchy -top epfl_miter', 'check -assert',
            'sat -set trigger 1 -set-def-inputs -show-inputs -show-outputs '
            + f'-timeout {max(1, int(timeout))} -dump_json {token(directory / "sat-witness.json")}', '',
        ])
        (directory / 'qualify.ys').write_text(script)
        with (directory / 'yosys.log').open('w') as log:
            process = subprocess.run([str(yosys), '-Q', '-T', '-s', str(directory / 'qualify.ys')],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        require(process.returncode == 0, 'EPFL qualification tool failed; inspect yosys.log')
        require((directory / 'sat-witness.json').is_file(), 'no concrete top-level counterexample found')
        log = (directory / 'yosys.log').read_text()
        require('SAT solving finished - model found:' in log, 'SAT did not report a concrete model')
        signals = _wave_bits(json.loads((directory / 'sat-witness.json').read_text()))
        require(signals.get('trigger') == 1, 'SAT witness does not assert top-level difference')
        inputs = {unescape(name): signals['in_' + unescape(name)] for name in base_graph['inputs']}
        before, after = _evaluate(base_graph, inputs), _evaluate(new_graph, inputs)
        differences = {name: {'base': before[name], 'new': after[name]}
                       for name in before if before[name] != after[name]}
        require(differences, 'SAT witness has no difference when independently replayed on source')
        for prefix, outputs in [('gold_', before), ('gate_', after)]:
            require(all(signals.get(prefix + name) == value for name, value in outputs.items()),
                    'SAT witness and independent source simulation disagree')
        require(original_hashes == {'base': digest(base), 'new': digest(new)},
                'EPFL sources changed during qualification')
        witness = {'inputs': inputs, 'base_outputs': before, 'new_outputs': after,
                   'differing_outputs': differences, 'source_hashes': original_hashes,
                   'all_inputs_explicit_two_state': True, 'all_top_outputs_replayed': True}
        write_json(directory / 'counterexample.json', witness)
        result.update(status='passed', differing_output_count=len(differences),
                      source_hashes=original_hashes, source_replay='passed',
                      counterexample=str(directory / 'counterexample.json'))
    except subprocess.TimeoutExpired:
        result.update(status='timeout', reason='qualification wall-clock timeout; no eligibility claim')
    except (ValueError, KeyError, OSError, TypeError) as error:
        result['reason'] = str(error)
    result.update(seconds=time.perf_counter() - started, directory=str(directory),
                  script=str(directory / 'qualify.ys'), log=str(directory / 'yosys.log'))
    write_json(directory / 'report.json', result)
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['fetch'])
    options = parser.parse_args()
    print(json.dumps(fetch(), indent=2))
