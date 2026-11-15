"""Fail-closed equivalence of two complete, two-state combinational netlists.

Names inside a design are never equivalence assumptions. An interned AIG first
proves common functions from identically bound primary inputs. Independent-cut
SAT partitions may prove more outputs; a counterexample at an abstract cut is
only a reason to expand, never a design counterexample. Residual ABC CEC/SAT
reuse proved substitutions; their final fallback expands cuts to primary inputs.
"""
from dataclasses import dataclass
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from yosys_flow.common import TraceError, digest, identifier, require, require_combinational, token, write_json
from yosys_flow.region_functions import gate_function
from yosys_flow.structural import Expressions, Expression


METHOD = 'proved_residual_cec_sat_v2'
GATE_INPUTS = {
    **{f'$_{n}_': 'AB' for n in ('AND', 'NAND', 'OR', 'NOR', 'XOR', 'XNOR', 'ANDNOT', 'ORNOT')},
    '$_BUF_': 'A', '$_NOT_': 'A', '$_AOI3_': 'ABC', '$_OAI3_': 'ABC',
    '$_AOI4_': 'ABCD', '$_OAI4_': 'ABCD', '$_MUX_': 'ABS', '$_NMUX_': 'ABS',
    '$_MUX4_': 'ABCDST', '$_MUX8_': 'ABCDEFGHSTU', '$_MUX16_': 'ABCDEFGHIJKLMNOPSTUV',
}


@dataclass(frozen=True)
class Options:
    timeout: float = 1200.0
    partition_timeout: float = 5.0
    partition_budget: float = 30.0
    matching_budget: float = 360.0
    max_partition_nodes: int = 20000
    abc: str | None = None  # None: discover beside Yosys; 'none': SAT only
    functional_matching: bool = True
    fast_sat: bool = True

    def validate(self):
        for name in ('timeout', 'partition_timeout', 'partition_budget', 'matching_budget'):
            value = getattr(self, name)
            require(math.isfinite(value) and value > 0, name + ' must be positive finite seconds')
        require(self.max_partition_nodes > 0, 'max_partition_nodes must be positive')


class Deadline:
    def __init__(self, seconds):
        self.started = time.monotonic()
        self.end = self.started + seconds

    def remaining(self):
        return max(0.0, self.end - time.monotonic())

    def check(self):
        if not self.remaining():
            raise TimeoutError('whole-netlist equivalence wall-clock budget exhausted')


def executable(binary):
    found = shutil.which(str(binary))
    require(found is not None, 'executable not found: ' + str(binary))
    return str(Path(found).resolve())


def run_tool(argv, out, name, seconds):
    """Stream output to disk; bound and terminate the complete process group."""
    start = time.monotonic()
    timed_out = False
    with (out / (name + '.log')).open('w') as stream:
        child = subprocess.Popen(argv, cwd=out, stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            child.wait(timeout=max(0.001, seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
    return {'returncode': child.returncode, 'timed_out': timed_out,
            'seconds': time.monotonic() - start, 'log': name + '.log'}


def yosys_job(binary, out, name, script, seconds):
    (out / (name + '.ys')).write_text(script + '\n')
    return run_tool([binary, '-Q', '-T', '-s', name + '.ys'], out, name, seconds)


def proof_result(out, job, backend):
    """Exit zero alone is NOT proof (in particular ABC uses it on read errors)."""
    if job['timed_out']:
        return 'unknown'
    success = failure = False
    error = job['returncode'] != 0
    with (out / job['log']).open(errors='replace') as stream:
        for line in stream:
            if backend == 'sat':
                success |= line.startswith('SAT proof finished - no model found: SUCCESS!')
                failure |= line.startswith('SAT proof finished - model found: FAIL!')
            else:
                success |= bool(re.match(r'^Networks are equivalent\.', line))
                failure |= bool(re.match(r'^Networks are NOT EQUIVALENT\.', line))
            error |= any(s in line for s in ('ERROR:', 'Reading network from file has failed',
                                             'Cannot find the model', 'TIMEOUT', 'timed out'))
    if error or success == failure:
        return 'unknown'
    return 'equivalent' if success else 'different'


class AIG(Expressions):
    """Exact tuple interning, not digest equality or simulation signatures."""
    def __init__(self):
        super().__init__()
        self.nodes = [None]
        self.simulation = [0]
        self.mask = (1 << 256) - 1

    def intern(self, key):
        value = self.unique.get(key)
        if value is None:
            value = 2 * len(self.nodes)
            self.unique[key] = value
            self.nodes.append(key)
            if key[0] == 'input':
                sim = int.from_bytes(hashlib.sha256(repr(key).encode()).digest(), 'big')
            else:
                sim = self.sample(key[1]) & self.sample(key[2])
            self.simulation.append(sim)
        return value

    def sample(self, literal):
        return self.simulation[literal >> 1] ^ (self.mask if literal & 1 else 0)

    def reachable(self, roots, deadline):
        seen, stack = set(), [r >> 1 for r in roots if r > 1]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if len(seen) % 4096 == 0:
                deadline.check()
            term = self.nodes[n]
            if term[0] == 'and':
                stack.extend(v >> 1 for v in term[1:] if v > 1)
        return seen

    def partition(self, root, common, expanded, limit, deadline):
        """Return a cone and its independent inputs, or None at the size cap."""
        return self.cone([root], common - expanded, limit, deadline)

    def cone(self, roots, cuts, limit, deadline):
        """Collect multiple outputs once, stopping only at explicitly supplied cuts."""
        cells, inputs, seen = set(), set(), set()
        stack = [root >> 1 for root in roots if root > 1]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if len(seen) % 4096 == 0:
                deadline.check()
            if limit is not None and len(seen) > limit:
                return None
            term = self.nodes[n]
            if term[0] == 'input' or n in cuts:
                inputs.add(n)
            else:
                cells.add(n)
                stack.extend(v >> 1 for v in term[1:] if v > 1)
        return cells, inputs

    def miter_json(self, root, cone):
        cells, inputs = cone
        bits = {n: i + 2 for i, n in enumerate(sorted(cells | inputs))}
        ports = {f'v_{n}': {'direction': 'input', 'bits': [bits[n]]} for n in sorted(inputs)}
        gates, inverted = {}, {}
        def wire(literal):
            if literal < 2:
                return str(literal)
            n = literal >> 1
            if not literal & 1:
                return bits[n]
            if n not in inverted:
                inverted[n] = len(bits) + len(inverted) + 2
                gates[f'inv_{n}'] = gate('$_NOT_', {'A': bits[n]}, inverted[n])
            return inverted[n]
        for n in sorted(cells):
            _, a, b = self.nodes[n]
            gates[f'and_{n}'] = gate('$_AND_', {'A': wire(a), 'B': wire(b)}, bits[n])
        ports['trigger'] = {'direction': 'output', 'bits': [wire(root)]}
        return {'modules': {'miter': {'ports': ports, 'cells': gates, 'netnames': {}, 'attributes': {}}}}


def gate(kind, inputs, output):
    return {'type': kind, 'parameters': {}, 'attributes': {}, 'hide_name': 1,
            'port_directions': {**{p: 'input' for p in inputs}, 'Y': 'output'},
            'connections': {**{p: [b] for p, b in inputs.items()}, 'Y': [output]}}


def validate_module(module):
    """Reject unknown gates, unused state, hidden X, extra pins and bad drivers."""
    require_combinational(module)
    validate_port_aliases(module)
    driven = set()
    for p in module['ports'].values():
        require(p['direction'] in ('input', 'output'), 'inout is outside combinational proof scope')
        if p['direction'] == 'input':
            for b in p['bits']:
                require(type(b) is int and b not in driven, 'aliased input or duplicate driver')
                driven.add(b)
    cells = {}
    for name, c in module['cells'].items():
        if c['type'] == '$scopeinfo' and not c['connections']:
            continue
        pins = GATE_INPUTS.get(c['type'])
        require(pins is not None, 'unsupported combinational gate: ' + c['type'])
        expected = {**{p: 'input' for p in pins}, 'Y': 'output'}
        require(not c['parameters'] and c['port_directions'] == expected and
                set(c['connections']) == set(expected) and
                all(len(bs) == 1 for bs in c['connections'].values()), 'unsupported gate shape: ' + name)
        b = c['connections']['Y'][0]
        require(type(b) is int and b not in driven, 'duplicate or invalid output driver: ' + name)
        driven.add(b)
        cells[name] = c
    for bits in ([p['bits'] for p in module['ports'].values()] +
                 [bs for c in cells.values() for bs in c['connections'].values()]):
        for b in bits:
            require(type(b) is int and b in driven or type(b) is str and b in ('0', '1'),
                    'undriven or non-two-state signal')
    return cells


def validate_port_aliases(module):
    # Yosys read_json reconstructs wires through netnames. Contradictory port
    # aliases would otherwise make Python and external tools prove different
    # circuits (e.g. a changed output bit with an unchanged same-name net).
    for name, port in module['ports'].items():
        alias = module.get('netnames', {}).get(name)
        if alias is not None:
            require(alias['bits'] == port['bits'], 'port/netname bit mapping conflict: ' + name)
            for key in ('offset', 'upto'):
                require(alias.get(key, 0) == port.get(key, 0), 'port/netname range conflict: ' + name)


def interface(module):
    return {n: (p['direction'], len(p['bits']), p.get('offset', 0), p.get('upto', 0))
            for n, p in module['ports'].items()}


def encode(module, manager, deadline):
    cells = validate_module(module)
    inputs = {b: manager.variable((n, i)) for n, p in sorted(module['ports'].items())
              if p['direction'] == 'input' for i, b in enumerate(p['bits'])}
    values = {**inputs, '0': Expression(manager, 0), '1': Expression(manager, 1)}
    drivers = {c['connections']['Y'][0]: n for n, c in cells.items()}
    dependencies = {n: {drivers[b] for p, bs in c['connections'].items() if p != 'Y'
                        for b in bs if b in drivers} for n, c in cells.items()}
    users = {n: [] for n in cells}
    for n, deps in dependencies.items():
        for dep in deps:
            users[dep].append(n)
    ready = deque(n for n, deps in dependencies.items() if not deps)
    count = 0
    while ready:
        name = ready.popleft()
        if count % 4096 == 0:
            deadline.check()
        cell = cells[name]
        ports = {p: values[bs[0]] for p, bs in cell['connections'].items() if p != 'Y'}
        function = gate_function(cell['type'], ports, manager.one)
        values[cell['connections']['Y'][0]] = function
        count += 1
        for user in users[name]:
            dependencies[user].remove(name)
            if not dependencies[user]:
                ready.append(user)
    require(count == len(cells), 'combinational cycle in netlist')
    return {(n, i): values[b].literal for n, p in sorted(module['ports'].items())
            if p['direction'] == 'output' for i, b in enumerate(p['bits'])}


def read_design(path, top, binary, out, name, deadline, report):
    design = json.loads(path.read_text())
    require(top in design['modules'], 'missing top module: ' + top)
    modules = design['modules']
    for module in modules.values():
        validate_port_aliases(module)
    # Retained hierarchy is normalized solely for verification, not synthesis.
    if any(c['type'] in modules for c in modules[top]['cells'].values()):
        script = f'read_json {token(path)}\nhierarchy -check -top {top}\n' + \
                 'setattr -mod -unset keep_hierarchy\nsetattr -unset keep_hierarchy\nflatten\ncheck -assert\n' + \
                 f'write_json {name}-flat.json'
        job = yosys_job(binary, out, name + '-flatten', script, deadline.remaining())
        report['jobs'].append(job)
        if job['timed_out']:
            raise TimeoutError('hierarchy normalization timed out')
        require(job['returncode'] == 0, 'hierarchy normalization failed; see ' + job['log'])
        design = json.loads((out / (name + '-flat.json')).read_text())
    deadline.check()
    return design['modules'][top]


def source_commands(gold, candidate, top):
    commands = []
    for path, name in ((gold, 'gold'), (candidate, 'gate')):
        commands += [f'read_json {token(path)}', f'hierarchy -check -top {top}',
                     'setattr -mod -unset keep_hierarchy', 'setattr -unset keep_hierarchy',
                     'flatten', 'check -assert', f'rename {top} {name}', f'design -stash {name}']
    return commands + ['design -copy-from gold -as gold gold', 'design -copy-from gate -as gate gate']


def discover_abc(binary, requested):
    if requested == 'none':
        return None
    if requested:
        return executable(requested)
    sibling = Path(binary).parent / 'yosys-abc'
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which('yosys-abc')
    return str(Path(found).resolve()) if found else None


def partition_proofs(manager, gold, candidate, deadline, binary, out, options, report):
    if all(r['status'] == 'equivalent' for r in report['outputs']):
        return 'equivalent'
    common = manager.reachable(gold.values(), deadline) & manager.reachable(candidate.values(), deadline)
    report['structural']['shared_aig_functions'] = len(common)
    stage_end = time.monotonic() + min(options.partition_budget, deadline.remaining() * 0.25)
    results = {}
    for row in report['outputs']:
        if row['status'] == 'equivalent':
            continue
        key = (row['port'], row['bit'])
        pair = tuple(sorted((gold[key], candidate[key])))
        if pair in results:
            row.update(results[pair])
            continue
        if time.monotonic() >= stage_end:
            row['reason'] = 'partition_stage_budget'
            continue
        root = (Expression(manager, pair[0]) ^ Expression(manager, pair[1])).literal
        expanded = set()
        for level in range(2):
            budget = min(options.partition_timeout, stage_end - time.monotonic(), deadline.remaining())
            if budget <= 0:
                break
            cone = manager.partition(root, common, expanded, options.max_partition_nodes, deadline)
            if cone is None:
                row['reason'] = 'partition_size_limit'
                break
            name = f'partition-{len(report["partitions"]):04d}'
            write_json(out / (name + '.json'), manager.miter_json(root, cone))
            cuts = cone[1] & common
            abstract = any(manager.nodes[n][0] != 'input' for n in cuts)
            seconds = max(1, math.ceil(budget))
            script = f'read_json {name}.json\nhierarchy -check -top miter\ncheck -assert\n' + \
                     f'sat -timeout {seconds} -prove trigger 0 -set-def-inputs -show-inputs -show-outputs -dump_json {name}-cex.json'
            job = yosys_job(binary, out, name, script, budget)
            state = proof_result(out, job, 'sat')
            detail = {**job, 'output': key, 'expansion': level, 'and_nodes': len(cone[0]),
                      'inputs': len(cone[1]), 'abstract_cuts': abstract, 'status': state}
            report['partitions'].append(detail)
            if state == 'equivalent':
                row.update(status='equivalent', method='independent_cut_sat', proof=name)
                results[pair] = {k: row[k] for k in ('status', 'method', 'proof')}
                break
            if state == 'different' and not abstract:
                row.update(status='different', method='primary_input_sat', proof=name)
                return 'different'
            # Abstract counterexamples may violate correlations between cuts.
            # Expose their drivers once; otherwise escalate to the full design.
            row['reason'] = 'abstract_counterexample' if state == 'different' else 'partition_unknown'
            expanded.update(cuts)
    return 'equivalent' if all(r['status'] == 'equivalent' for r in report['outputs']) else 'unknown'


def sat_original(gold, candidate, top, binary, out, budget, report, name):
    commands = source_commands(gold, candidate, top)
    commands += ['miter -equiv -flatten gold gate miter', 'hierarchy -check -top miter',
                 'opt_merge', 'opt_expr -keepdc', 'opt_clean',
                 f'sat -timeout {max(1, int(budget))} -prove trigger 0 -set-def-inputs '
                 f'-show-inputs -show-outputs -dump_json {name}-cex.json']
    job = yosys_job(binary, out, name, '\n'.join(commands), budget)
    state = proof_result(out, job, 'sat')
    report['jobs'].append({**job, 'status': state})
    return state


class EquivalenceError(TraceError):
    def __init__(self, name, result, path):
        self.equivalence_status = result['status']
        self.equivalence_reason = result.get('reason')
        super().__init__(f'{name} failed ({result["status"]}); see {path}')


def require_equivalent(name, result, path):
    if result['status'] != 'equivalent' or not result['all_top_outputs_verified']:
        raise EquivalenceError(name, result, path)


def verify(gold, candidate, top, out, *, yosys='yosys', options=None):
    """Return an auditable result; only status=='equivalent' authorizes acceptance.

    Files are immutable proof inputs. There is no cross-run proof cache and no
    synthesis or publication of a replacement in this API.
    """
    options = options or Options()
    options.validate()
    top = identifier(top)
    gold, candidate, out = Path(gold).resolve(), Path(candidate).resolve(), Path(out).resolve()
    require(not out.exists(), 'refusing to overwrite equivalence directory: ' + str(out))
    out.mkdir(parents=True)
    deadline = Deadline(options.timeout)
    report = {'schema_version': 1, 'method': METHOD, 'status': 'running', 'stage': 'prepare', 'top': top,
              'semantic_domain': 'fully_defined_two_state_combinational', 'options': vars(options),
              'inputs': {}, 'outputs': [], 'partitions': [], 'jobs': [], 'structural': {},
              'internal_names_assumed_equal': False, 'all_top_outputs_verified': False}
    write_json(out / 'report.json', report)
    try:
        binary = executable(yosys)
        report['yosys'] = {'path': binary, 'sha256': digest(binary)}
        report['inputs'] = {name: {'path': str(path), 'sha256': digest(path)}
                            for name, path in (('gold', gold), ('candidate', candidate))}
        a = read_design(gold, top, binary, out, 'gold', deadline, report)
        b = read_design(candidate, top, binary, out, 'gate', deadline, report)
        require(interface(a) == interface(b), 'top interface mismatch (names/directions/widths/ranges)')
        manager = AIG()
        lhs = encode(a, manager, deadline)
        rhs = encode(b, manager, deadline)
        report['encoding_seconds'] = time.monotonic()-deadline.started
        fast = 'unknown'
        if options.fast_sat and len(a['cells'])+len(b['cells']) <= 5000 and any(lhs[k] != rhs[k] for k in lhs):
            report['stage'] = 'fast_sat'
            write_json(out/'report.json', report)
            fast = sat_original(gold, candidate, top, binary, out, min(2, deadline.remaining()*0.1), report, 'fast-sat')
            if fast != 'unknown':
                report['full_design_method'] = 'sat_full_design_fast_path'
        proved = 0
        if fast == 'unknown' and options.functional_matching and any(lhs[k] != rhs[k] for k in lhs):
            from .equivalence_matches import refine
            report['stage'] = 'functional_matching'
            write_json(out/'report.json', report)
            def progress(stats):
                report['functional_matching'] = dict(stats)
                write_json(out/'report.json', report)
            with (out/'functional-matches.jsonl').open('w') as evidence:
                lhs, rhs, matches = refine(manager, lhs, rhs, deadline,
                    min(options.matching_budget, deadline.remaining()*0.6), evidence, progress)
            proved = matches['proved_classes']
            report['functional_matching'] = {**matches, 'evidence': 'functional-matches.jsonl',
                                              'sha256': digest(out/'functional-matches.jsonl')}
        require(lhs, 'no top outputs to verify')
        report['structural'] = {'aig_nodes': len(manager.nodes) - 1,
                                'gold_cells': len(a['cells']), 'candidate_cells': len(b['cells']),
                                'matched_outputs': sum(lhs[k] == rhs[k] for k in lhs)}
        del a, b
        report['outputs'] = [{'port': n, 'bit': i, 'status': 'equivalent' if lhs[n, i] == rhs[n, i] else 'unknown',
                              'method': ('proved_functional_correspondence' if proved else 'structural_boolean_identity')
                              if lhs[n, i] == rhs[n, i] else None}
                             for n, i in lhs]
        if fast == 'equivalent':
            for row in report['outputs']:
                row.update(status='equivalent', method='sat_full_design_fast_path')
        report['preparation_seconds'] = time.monotonic() - deadline.started
        report['stage'] = 'partitions'
        write_json(out / 'report.json', report)
        state = fast if fast != 'unknown' else partition_proofs(manager, lhs, rhs, deadline, binary, out, options, report)
        report['partition_stage_finished_seconds'] = time.monotonic() - deadline.started
        report['status'] = state
        write_json(out / 'report.json', report)
        if state == 'unknown':
            from .equivalence_residual import prove_residual
            report['status'], report['full_design_method'] = prove_residual(
                manager, lhs, rhs, binary, out, deadline, options, report)
            if report['status'] == 'equivalent':
                for row in report['outputs']:
                    if row['status'] != 'equivalent':
                        row.update(status='equivalent', method=report['full_design_method'])
        del manager, lhs, rhs
        # Bind the entire result to both files, even for a structural-only pass.
        require(all(digest(row['path']) == row['sha256'] for row in report['inputs'].values()),
                'proof input changed while checking')
        report['all_top_outputs_verified'] = (report['status'] == 'equivalent' and
                                              all(r['status'] == 'equivalent' for r in report['outputs']))
        if report['status'] == 'unknown':
            report['reason'] = 'proof_not_completed_within_budget_or_backend_error'
    except TimeoutError as exc:
        report.update(status='unknown', reason='timeout', detail=str(exc))
    except (TraceError, KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        report.update(status='invalid', reason='invalid_or_unsupported_input', detail=str(exc)[:2000])
    report['total_seconds'] = time.monotonic() - deadline.started
    report['stage'] = 'finished'
    report['all_top_outputs_verified'] &= report['status'] == 'equivalent'
    write_json(out / 'report.json', report)
    return report


def run(args):
    return verify(args.gold, args.gate, args.top, args.out, yosys=args.yosys,
                  options=Options(timeout=args.timeout, partition_timeout=args.partition_timeout,
                                  matching_budget=args.matching_budget, abc=args.abc))
