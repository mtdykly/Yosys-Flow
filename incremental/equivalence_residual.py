"""Discharge remaining outputs using the *proved* quotient of both networks.

The caller substitutes only independently proved identities. Common functions
can then be independent cut inputs: UNSAT/equivalence is sufficient, but a
counterexample is not. The concrete fallback keeps the substitutions and
restores every cut's definition down to original primary inputs.
"""
import json
import math
import time

from yosys_flow.common import digest, write_json
from yosys_flow.structural import Expression


def write_blif(path, manager, roots, cells, inputs, deadline):
    """Write exact AIG semantics, including complemented edges and constants."""
    with path.open('w') as stream:
        stream.write('.model residual\n.inputs ' + ' '.join(f'n{n}' for n in sorted(inputs)) + '\n')
        stream.write('.outputs ' + ' '.join(f'o{i}' for i in range(len(roots))) + '\n')
        for index, n in enumerate(sorted(cells)):
            if index % 4096 == 0:
                deadline.check()
            _, a, b = manager.nodes[n]
            # Reduced AIG AND nodes never have constant fanins.
            stream.write(f'.names n{a >> 1} n{b >> 1} n{n}\n{1-(a & 1)}{1-(b & 1)} 1\n')
        for i, root in enumerate(roots):
            if root < 2:
                stream.write(f'.names o{i}\n' + ('1\n' if root else ''))
            else:
                stream.write(f'.names n{root >> 1} o{i}\n{1-(root & 1)} 1\n')
        stream.write('.end\n')


def residual_attempt(manager, pairs, cuts, binary, abc, out, name, deadline, budget, report):
    # Imports stay here: netlist_equivalence is also the public API and calls us.
    from .netlist_equivalence import proof_result, run_tool, yosys_job

    started = time.monotonic()
    end = min(deadline.end, started + budget)
    remaining = lambda: max(0.0, end - time.monotonic())
    left = [a for a, b in pairs]
    right = [b for a, b in pairs]
    lc, li = manager.cone(left, cuts, None, deadline)
    rc, ri = manager.cone(right, cuts, None, deadline)
    inputs = li | ri
    abstract = {n for n in inputs if manager.nodes[n][0] != 'input'}
    detail = {'name': name, 'outputs': len(pairs), 'gold_and_nodes': len(lc),
              'candidate_and_nodes': len(rc), 'shared_and_nodes': len(lc & rc),
              'input_nodes': len(inputs), 'abstract_cut_nodes': len(abstract),
              'proved_substitutions_reused': True, 'status': 'unknown', 'jobs': []}
    report.setdefault('residuals', []).append(detail)
    # Explicit binding, not wire names or an unproved simulation match. This
    # manifest plus functional-matches.jsonl describes the proof composition.
    bindings = {'schema_version': 1, 'obligations': [list(pair) for pair in pairs],
                'inputs': [{'node': n, 'kind': 'proved_common_function' if n in abstract else 'primary_input',
                            'primary_input': manager.nodes[n][1] if n not in abstract else None}
                           for n in sorted(inputs)],
                'source_inputs': report['inputs'],
                'matching_evidence': report.get('functional_matching', {}).get('sha256'),
                'abstract_counterexample_is_design_counterexample': not bool(abstract)}
    write_json(out / (name + '-bindings.json'), bindings)
    detail['bindings'] = name + '-bindings.json'
    detail['bindings_sha256'] = digest(out / detail['bindings'])
    report.update(stage=name, status='running')
    write_json(out/'report.json', report)

    state, method = 'unknown', None
    if abc and remaining() > 0:
        for side, roots, cells in (('gold', left, lc), ('gate', right, rc)):
            path = out / f'{name}-{side}.blif'
            write_blif(path, manager, roots, cells, inputs, deadline)
            detail[side + '_blif_sha256'] = digest(path)
        if remaining() > 0:
            # Reserve time for SAT, using the SAME reduced problem.
            seconds = remaining() * 0.75
            script = f'cec -T {max(1, int(seconds))} {name}-gold.blif {name}-gate.blif'
            (out/(name+'-cec.script')).write_text(script+'\n')
            report['stage'] = name + '_cec'
            write_json(out/'report.json', report)
            job = run_tool([abc, '-c', script], out, name+'-cec', seconds)
            state = proof_result(out, job, 'cec')
            method = 'abc_cec_proved_residual'
            detail['jobs'].append({**job, 'status': state})
    if state == 'unknown' and remaining() > 0:
        trigger = Expression(manager, 0)
        for a, b in pairs:
            trigger = trigger | (Expression(manager, a) ^ Expression(manager, b))
        cone = manager.cone([trigger.literal], cuts, None, deadline)
        detail['sat_and_nodes'] = len(cone[0])
        path = out/(name+'-miter.json')
        # Proof netlists can contain hundreds of thousands of gates. Pretty
        # printing duplicates large strings and can dominate a short SAT budget.
        path.write_text(json.dumps(manager.miter_json(trigger.literal, cone), separators=(',', ':'))+'\n')
        detail['miter_sha256'] = digest(path)
        if remaining() > 0:
            report['stage'] = name + '_sat'
            write_json(out/'report.json', report)
            script = (f'read_json {path.name}\nhierarchy -check -top miter\n'
                      'check -assert\nopt_merge\nopt_expr -keepdc\nopt_clean\n'
                      f'sat -timeout {max(1, math.ceil(remaining()))} -prove trigger 0 '
                      f'-set-def-inputs -show-inputs -show-outputs -dump_json {name}-cex.json')
            job = yosys_job(binary, out, name+'-sat', script, remaining())
            state = proof_result(out, job, 'sat')
            method = 'sat_proved_residual'
            detail['jobs'].append({**job, 'status': state})
    detail.update(status=state, seconds=time.monotonic()-started,
                  accepted_counterexample=state == 'different' and not abstract)
    if abstract and state == 'different':
        detail['reason'] = 'abstract_counterexample_requires_expansion'
        state = 'unknown'
    write_json(out/'report.json', report)
    return state, method, abstract


def prove_residual(manager, gold, candidate, binary, out, deadline, options, report):
    from .netlist_equivalence import discover_abc

    # Merge overlapping/identical output obligations into one multi-output
    # residual. Shared logic is exported once, not re-expanded for each bit.
    pending = [r for r in report['outputs'] if r['status'] != 'equivalent']
    pairs = {}
    for row in pending:
        key = row['port'], row['bit']
        pair = tuple(sorted((gold[key], candidate[key])))
        pairs.setdefault(pair, []).append(list(key))
    report['residual_output_map'] = [{'literals': list(p), 'outputs': v} for p, v in pairs.items()]
    abc = discover_abc(binary, options.abc)
    if abc:
        report['abc'] = {'path': abc, 'sha256': digest(abc)}
    common = manager.reachable((p[0] for p in pairs), deadline) & manager.reachable((p[1] for p in pairs), deadline)
    cuts = {n for n in common if manager.nodes[n][0] != 'input'}
    if cuts:
        # Restore only the current frontier's definitions, keeping upstream
        # common functions as cuts. Correlated cutpoints need not force an
        # immediate return to the entire primary-input cone.
        stage_end = time.monotonic() + min(120.0, deadline.remaining()*0.25)
        for level in range(4):
            budget = min(30.0, stage_end-time.monotonic(), deadline.remaining())
            if budget <= 0:
                break
            name = 'residual-cut' + (f'-expand{level}' if level else '')
            state, method, abstract = residual_attempt(manager, pairs, cuts, binary, abc, out,
                name, deadline, budget, report)
            report['residuals'][-1]['frontier_expansion'] = level
            if state != 'unknown' or not abstract:
                return state, method
            cuts.difference_update(abstract)
    deadline.check()
    # Expand every cut, but KEEP proved node substitutions. This is a complete
    # primary-input proof of the remaining outputs, not an assumed-cut proof.
    state, method, _ = residual_attempt(manager, pairs, set(), binary, abc, out,
        'residual-concrete', deadline, deadline.remaining(), report)
    return state, method
