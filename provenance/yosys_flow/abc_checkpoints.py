"""Functional proof bridges through recorded ABC checkpoints, without synthesis.

Node signatures only choose candidates. Each hop must be structurally equal or
proved, including polarity, before transitivity is applied to endpoint classes.
No causal rewrite history or equivalence of a whole stage is inferred.
"""
import gc
import gzip
import json
from pathlib import Path

from .abc_internal import evaluate, read_evidence
from .abc_partition import Graph, Partitioner
from .boolean import ProofBudgetExceeded
from .common import digest, require, write_json


def network_blif(net):
    require(all(net.names) and len(set(net.names)) == len(net.names),
            'checkpoint lacks unique captured PI names')
    pis = dict(zip(net.pis, net.names))
    pos = dict(zip(net.pos, net.record.get('po_names', [net.nodes[i].get('name') for i in net.pos])))
    require(all(pos.values()) and len(set(pos.values())) == len(pos), 'checkpoint output identity missing')
    names = {i: pis.get(i, '__checkpoint_' + str(i)) for i in net.nodes}
    require(len(set(names.values())) == len(names), 'checkpoint node name collision')
    lines = ['.model checkpoint', '.inputs ' + ' '.join(net.names), '.outputs ' + ' '.join(pos.values())]
    for i, node in net.nodes.items():
        if i in pis:
            continue
        require(len(node['fanins']) <= 6, 'checkpoint truth-table encoding limit')
        lines.append('.names ' + ' '.join([*[names[f // 2] for f in node['fanins']], names[i]]))
        for assignment in range(1 << len(node['fanins'])):
            bits = [(assignment >> k) & 1 for k in range(len(node['fanins']))]
            args = [v ^ (f & 1) for v, f in zip(bits, node['fanins'])]
            if evaluate(node, args, 1):
                lines.append((''.join(map(str, bits)) + ' ' if bits else '') + '1')
    for i, name in pos.items():
        require(name not in names.values(), 'checkpoint output name collision')
        lines.extend(['.names ' + names[i] + ' ' + name, '1 1'])
    return '\n'.join([*lines, '.end']) + '\n'


def capture(directory, region, manifest, out, progress=None, *, include_rewrites=False,
            mapping_cec_timeout=60,mapping_cec_attempts=8, decoded=None, confirmed=None):
    """Derive witnesses from the authenticated raw capture, never a cache flag."""
    directory, out = Path(directory), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    for key in ('script', 'input.blif.flow.jsonl'):
        path = directory / region['artifacts'][key]
        require(digest(path) == manifest['artifacts'][path.name]['sha256'], 'ABC checkpoint artifact changed')
        artifacts[key] = path
    if progress:
        progress('read_captured_checkpoints')
    rows, nets, cuts, summary = (decoded if decoded is not None else
        read_evidence(artifacts['input.blif.flow.jsonl'], artifacts['script'].read_text()))
    stages = []; previous = None
    for seq, net in nets.items():
        if net.record['kind'] != 'network':
            continue
        path = out / (str(seq) + '.blif')
        path.write_text(network_blif(net))
        sha = digest(path)
        if sha == previous:
            path.unlink()
            continue
        previous = sha
        record = dict(seq=seq, command_id=net.record['command_id'],
                      command=summary['commands'][net.record['command_id'] - 1],
                      file=path.name, sha256=sha)
        stages.append(record)
        if progress:
            progress('checkpoint_exported', **record)
    from .abc_lemmas import export
    first=Graph((directory/region['artifacts']['input.blif']).read_text())
    lemmas=export(rows,nets,summary,first.pis,out,progress)
    from .abc_copy import export as export_copies
    copies=export_copies(rows,nets,summary,first.pis,out,progress)
    from .abc_mapping import export as export_mapping
    mappings=export_mapping(rows,nets,cuts,first.pis,out,progress,
                            cec_timeout=mapping_cec_timeout,cec_attempts=mapping_cec_attempts)
    rewrites=None
    if include_rewrites:
        from .abc_rewrites import build
        document=build(rows,nets,first.pis,progress)
        path=out/'abc-rewrite-lemmas.json.gz'
        with gzip.open(path,'wt') as stream:json.dump(document,stream,separators=(',',':'),allow_nan=False)
        rewrites={'artifact':path.name,'sha256':digest(path),'rule':document['rule'],
                  'statistics':document['statistics']}
    replayed=None
    if confirmed:
        from .evidence_relations import equations
        pairs=equations(confirmed['proofs'],confirmed['imports'],nets)
        path=out/'verified-replay-equations.json';write_json(path,pairs)
        replayed={'artifact':path.name,'sha256':digest(path),'count':len(pairs)}
    write_json(out / 'checkpoints.json', {'artifacts': {k: digest(v) for k, v in artifacts.items()},
        'stages': stages,'lemmas':lemmas,'copies':copies,'mappings':mappings,'rewrites':rewrites,'replayed':replayed})
    del rows, nets, cuts
    gc.collect()
    return stages


def bridge(endpoint, requests, stages, *, limit=4096, progress=None, solver_lemmas=(), rewrite_lemmas=None, copy_lemmas=(), mapping_lemmas=()):
    """Prove requested endpoint pairs through (Graph, evidence-descriptor) hops."""
    active = {tuple(key): (key[0], 0) for key in requests}
    first = endpoint.first
    history = [];history_started=False
    for final, descriptor in stages:
        engine = Partitioner(first, final, sat_timeout_ms=endpoint.sat_timeout_ms,
                             sat_backend=endpoint.sat_backend,
                             cec_timeout_seconds=endpoint.cec_timeout_seconds,
                             cec_attempt_limit=endpoint.cec_attempt_limit)
        history_started=history_started or descriptor.get('command')=='&dch' or 'endpoint' in descriptor
        if history_started and rewrite_lemmas:
            from .abc_rewrites import install
            install(engine,*rewrite_lemmas)
        if history_started and solver_lemmas:
            from .abc_lemmas import install
            for graph,events,witness in solver_lemmas:
                install(engine,graph,events,witness)
        if history_started and copy_lemmas:
            from .abc_copy import install
            for first_graph,last_graph,proof,witness in copy_lemmas:
                install(engine,first_graph,last_graph,proof,witness)
        if history_started and mapping_lemmas:
            from .abc_mapping import install
            for graph,proof,witness in mapping_lemmas:
                install(engine,graph,proof,witness)
        if history_started and (copy_lemmas or rewrite_lemmas or solver_lemmas):
            from .abc_lemmas import associative_congruence
            result=associative_congruence(engine.algebra,engine.classes,progress=progress)
            engine.proofs.append({'proof':'and_associativity',**result})
        following = {}; results = []; refined = False
        if progress:
            progress('checkpoint_bridge', checkpoint=descriptor, requested=len(active))
        for key, (a, accumulated) in active.items():
            sig = engine.sa[a]
            candidates = ([key[1]] if final is endpoint.final else
                          [n for n in engine.candidates.get(min(sig, sig ^ engine.mask), ()) if n is not None][:3])
            unique = {}
            for name in candidates:
                unique.setdefault(engine.classes.find(engine.b[name]) // 2, name)
            candidates = list(unique.values())
            row = dict(endpoint=list(key), entry=a, status='unknown')
            for b in candidates:
                phase = int(sig != engine.sb[b])
                if sig not in (engine.sb[b], engine.sb[b] ^ engine.mask):
                    continue
                known = engine.classes.find(engine.a[a]) == (engine.classes.find(engine.b[b]) ^ phase)
                try:
                    try:
                        ok = known or engine.prove(a, b, phase, limit=limit)
                    except ProofBudgetExceeded:
                        ok = False
                    if not ok:
                        ok = engine.global_cec(a, b, phase, progress)
                except ProofBudgetExceeded as exc:
                    ok = False; row['reason'] = str(exc)
                if not ok and not refined:
                    # Repair nearby boundaries bottom-up once for this hop,
                    # rather than retrying the same large PI miter per root.
                    refined = True
                    budget=engine.sat_timeout_ms
                    engine.sat_timeout_ms=min(budget,1000)
                    try:
                        engine.refine(sorted({n for n, phase in active.values()}), limits=(64,), progress=progress)
                    finally:
                        engine.sat_timeout_ms=budget
                if not ok and refined:
                    try:
                        ok = engine.prove(a, b, phase, limit=limit)
                    except ProofBudgetExceeded as exc:
                        row['reason'] = str(exc)
                if not ok:
                    engine.targeted([], pairs=[(a,b,phase)], limit=limit, progress=progress)
                    ok=engine.classes.find(engine.a[a])==(engine.classes.find(engine.b[b])^phase)
                if ok:
                    following[key] = (b, accumulated ^ phase)
                    row.update(status='verified', export=b, inverted=bool(phase), structural=known)
                    break
            results.append(row)
            if progress:
                progress('checkpoint_pair', **row)
        history.append(dict(checkpoint=descriptor, pairs=results, proofs=engine.proofs))
        first = final; active = following
        if not active:
            break
    require(first is endpoint.final or not active, 'checkpoint bridge did not reach final endpoint')
    accepted = []
    for key, (b, phase) in active.items():
        a, target, expected = key
        require(b == target and phase == expected, 'checkpoint chain polarity mismatch')
        endpoint.classes.join(endpoint.a[a], endpoint.b[b] ^ phase)
        accepted.append(list(key))
    evidence = dict(proof='captured_checkpoint_function_chain', requested=[list(k) for k in requests],
                    accepted=accepted, stages=history, complete_causal_history=False)
    endpoint.proofs.append(evidence)
    return evidence
