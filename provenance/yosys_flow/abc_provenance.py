"""Boundary-based ABC candidates, kept distinct from internal rewrite history.

Correspondence is captured at extraction/import sites. Cones restrict possible
associations by preserved output boundaries; they are never called exact lineage.
"""
from collections import Counter, defaultdict
from .common import require, unescape

GATES = {'ZERO', 'ONE', 'BUF', 'NOT', 'AND', 'NAND', 'OR', 'NOR', 'XOR', 'XNOR',
         'ANDNOT', 'ORNOT', 'MUX', 'NMUX', 'MUX4', 'MUX8', 'MUX16', 'AOI3', 'OAI3', 'AOI4', 'OAI4'}


def bit_key(bit):
    return (bit['wire'], bit['offset']) if isinstance(bit, dict) else bit


def cones(trace, region):
    nodes = region['input_nodes']
    require([n['id'] for n in nodes] == list(range(len(region['signals']))), 'invalid ABC input node IDs')
    require(all(all(isinstance(i, int) and 0 <= i < len(nodes) for i in n['fanins']) for n in nodes),
            'invalid ABC input fanin')
    require({n['cell'] for n in nodes if n['cell']} == set(region['inputs']), 'ABC input nodes differ from members')
    require(all(not n['cell'] or n['cell'] in region['inputs'] for n in nodes), 'unknown ABC node cell')
    removals = {r['ref']: r for r in trace.removals if r.get('abc_invocation') == region['invocation_id']}
    for n in nodes:
        if n['cell']:
            require(region['signals'][n['id']] in removals[n['cell']]['abc_outputs'], 'ABC node is not an extracted cell output')
    before = {}
    for output in region['output_ports']:
        seen, stack, members = set(), [output], set()
        while stack:
            nid = stack.pop()
            if nid in seen: continue
            seen.add(nid)
            n = nodes[nid]
            if n['cell']: members.add(n['cell'])
            if nid not in region['input_ports']: stack.extend(n['fanins'])
        before[output] = members
    imported = trace.abc_imports.get(region['invocation_id'], {})
    drivers = {}
    for name, node in imported.items():
        if unescape(node['type']) not in GATES or '\\Y' not in node['ports']:
            return before, {}, 'unsupported_abc_gate'
        for bit in node['ports']['\\Y']:
            key = bit_key(bit)
            if key in drivers: return before, {}, 'multiple_abc_drivers'
            drivers[key] = name
    after = defaultdict(set)
    for output in region['output_ports']:
        stack, seen = [(f'\\ys__n{output}', 0)], set()
        while stack:
            bit = stack.pop()
            if bit in seen: continue
            seen.add(bit)
            name = drivers.get(bit)
            if name is None:
                # A direct PI/constant output is legitimate. Aliases or unknown
                # drivers must not silently remove sources from an imported gate.
                if isinstance(bit, tuple) and bit not in {(f'\\ys__n{i}', 0) for i in region['input_ports']}:
                    return before, {}, 'unresolved_abc_output'
                continue
            after[name].add(output)
            for port, bits in imported[name]['ports'].items():
                if port != '\\Y': stack.extend(bit_key(b) for b in bits)
    return before, after, None


def refine(trace, internal=None):
    internal = internal or {}
    replacements = {}
    trace.abc_associations = []
    trace.abc_fallbacks = []
    for region in trace.regions:
        if 'input_nodes' not in region: continue
        before, after, fallback = cones(trace, region)
        if fallback:
            trace.abc_fallbacks.append({"invocation_id": region["invocation_id"], "reason": fallback})
        for row in trace.records:
            if row['record_type'] not in {'cell_create', 'assignment'} or row.get('abc_invocation') != region['invocation_id']:
                continue
            boundary = row.get('abc_boundary', -1)
            outputs = {boundary} if boundary in before else after.get(row.get('abc_node'), set())
            members = set().union(*(before[i] for i in outputs)) if outputs and not fallback else set()
            captured = internal.get(region['invocation_id'], {})
            mapped = row.get('abc_node', '')
            if not mapped and boundary >= 0:
                for name, node in trace.abc_imports.get(region['invocation_id'], {}).items():
                    if node['ports'].get('\\Y') == [{'wire': f'\\ys__n{boundary}', 'offset': 0}]:
                        mapped = name
                        break
            cut = captured.get(mapped)
            if cut:
                members = set(cut['inputs'])
            if not members:
                continue  # Keep original region candidate, never invent an empty proof.
            evidence = {'ref': row['ref'], 'invocation_id': region['invocation_id'],
                        'mapped_node': mapped, 'output_boundaries': sorted(outputs),
                        'inputs': sorted(members), 'precision': 'abc_output_cone_candidate',
                        'method': 'preserved_output_boundary_cones', 'internal_rewrite_provenance': False}
            if cut:
                evidence.update(cut)
                evidence['internal_rewrite_provenance'] = False  # functional cut proof, not complete lineage
            trace.abc_associations.append(evidence)
            replacements[row['ref']] = (members, row['seq'], cut['precision'] if cut else 'abc_output_cone')
    if not replacements: return
    trace.edges = [e for e in trace.edges if e['to'] not in replacements]
    for ref, (inputs, sequence, capture) in replacements.items():
        trace.edges.extend({'from': i, 'to': ref, 'event_id': sequence, 'capture': capture,
                            'precision': 'abc_output_cone_candidate' if capture == 'abc_output_cone' else capture} for i in sorted(inputs))
    trace.edges.sort(key=lambda e: e['event_id'])
    groups = {}
    for edge in trace.edges: groups.setdefault(edge['to'], []).append(edge)
    origins = {sid: {sid} for sid in trace.sources}
    precisions = {sid: 'cell' for sid in trace.sources}
    rank = {'cell': 0, 'bit_range': 0, 'process_action': 1, 'abc_internal_cut': 2,
            'abc_internal_joint_cut': 3, 'cell_group_candidate': 4, 'process_signal_candidate': 5, 'abc_output_cone_candidate': 6, 'abc_region_candidate': 7,
            'process_pending': -1}
    objects = {**trace.objects, **trace.assignments, **trace.process_nodes}
    for ref, edges in groups.items():
        # Sources with no incoming edges (pending cells, empty constant routes).
        sources = set().union(*(origins.get(e['from'], set()) for e in edges))
        obj = objects.get(ref)
        raw = obj.get('recorded_precision', obj['precision']) if obj else 'cell'
        p = max((precisions.get(e['from'], 'cell') for e in edges), key=rank.__getitem__)
        if ref in replacements and any(e['capture']=='abc_internal_joint_cut' for e in edges): p = max((p, 'abc_internal_joint_cut'), key=rank.__getitem__)
        elif ref in replacements and any(e['capture']=='abc_internal_cut' for e in edges): p = max((p, 'abc_internal_cut'), key=rank.__getitem__)
        elif ref in replacements and any(e['capture']=='abc_output_cone' for e in edges): p = max((p, 'abc_output_cone_candidate'), key=rank.__getitem__)
        elif obj and obj.get('abc_invocation') and obj['record_type'] in {'cell_create', 'assignment'} and ref not in replacements:
            p = max((p, 'abc_region_candidate'), key=rank.__getitem__)
        elif raw not in {'abc_region_candidate', 'process_pending'}: p = max((raw, p), key=rank.__getitem__)
        origins[ref], precisions[ref] = sources, p
        if obj:
            obj.setdefault('recorded_origins', obj['origins'])
            obj.setdefault('recorded_precision', obj['precision'])
            obj['origins'] = sorted(sources)
            obj['precision'] = p
        for e in edges: e['precision'] = p


def validate_artifacts(trace, region, files):
    if 'input_nodes' not in region: return
    # The actual BLIF pins are the authoritative node mapping, not cell names.
    fanins = {}
    for line in files['input.blif'].read_text().splitlines():
        words = line.split()
        if words and words[0] == '.names':
            require(all(w.startswith('ys__n') and w[5:].isdigit() for w in words[1:]), 'unknown ABC input net')
            fanins[int(words[-1][5:])] = [int(w[5:]) for w in words[1:-1]]
    for n in region['input_nodes']:
        if n['cell']:
            require(n['id'] in fanins and fanins[n['id']] == n['fanins'], 'ABC node fanins differ from input artifact')
    expected = Counter()
    for line in files['output.blif'].read_text().splitlines():
        words = line.split()
        if words and words[0] == '.gate':
            expected[(words[1], tuple(sorted(tuple(w.split('=', 1)) for w in words[2:])))] += 1
    actual = Counter()
    for node in trace.abc_imports.get(region['invocation_id'], {}).values():
        if unescape(node['type']) not in GATES: continue
        ports = []
        for port, bits in node['ports'].items():
            require(len(bits) == 1 and isinstance(bits[0], dict) and bits[0]['offset'] == 0, 'invalid ABC imported gate pin')
            ports.append((unescape(port), unescape(bits[0]['wire'])))
        actual[(unescape(node['type']), tuple(sorted(ports)))] += 1
    if expected: require(actual == expected, 'ABC imported nodes differ from output artifact')
