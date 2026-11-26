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


def cones(trace, region, progress=None):
    nodes = region['input_nodes']
    require([n['id'] for n in nodes] == list(range(len(region['signals']))), 'invalid ABC input node IDs')
    require(all(all(isinstance(i, int) and 0 <= i < len(nodes) for i in n['fanins']) for n in nodes),
            'invalid ABC input fanin')
    require({n['cell'] for n in nodes if n['cell']} == set(region['inputs']), 'ABC input nodes differ from members')
    # The set equality above already checks every member in linear time.
    input_ports = set(region['input_ports'])
    input_bits = {(f'\\ys__n{i}', 0) for i in input_ports}
    removals = {r['ref']: r for r in trace.removals if r.get('abc_invocation') == region['invocation_id']}
    for n in nodes:
        if n['cell']:
            require(region['signals'][n['id']] in removals[n['cell']]['abc_outputs'], 'ABC node is not an extracted cell output')
    before = {}
    for ordinal, output in enumerate(region['output_ports']):
        if progress: progress('abc_input_cones', output=ordinal, outputs=len(region['output_ports']))
        seen, stack, members = set(), [output], set()
        while stack:
            nid = stack.pop()
            if nid in seen: continue
            seen.add(nid)
            n = nodes[nid]
            if n['cell']: members.add(n['cell'])
            if nid not in input_ports: stack.extend(n['fanins'])
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
    for ordinal, output in enumerate(region['output_ports']):
        if progress: progress('abc_output_cones', output=ordinal, outputs=len(region['output_ports']))
        stack, seen = [(f'\\ys__n{output}', 0)], set()
        while stack:
            bit = stack.pop()
            if bit in seen: continue
            seen.add(bit)
            name = drivers.get(bit)
            if name is None:
                # A direct PI/constant output is legitimate. Aliases or unknown
                # drivers must not silently remove sources from an imported gate.
                if isinstance(bit, tuple) and bit not in input_bits:
                    return before, {}, 'unresolved_abc_output'
                continue
            after[name].add(output)
            for port, bits in imported[name]['ports'].items():
                if port != '\\Y': stack.extend(bit_key(b) for b in bits)
    return before, after, None


_BYTE_BITS = tuple(tuple(i for i in range(8) if value & (1 << i)) for value in range(256))


class ConeMembers:
    """Lossless bit sets for unions of large, heavily overlapping output cones."""
    def __init__(self, before, members):
        self.before = before
        self.names = sorted(members)
        self.index = {name:i for i,name in enumerate(self.names)}
        self.width = (len(self.names)+7)//8
        self.masks = {}

    def union(self, outputs):
        combined = 0
        for output in outputs:
            if output not in self.masks:
                bits = bytearray(self.width)
                for name in self.before[output]:
                    i = self.index[name]
                    bits[i//8] |= 1 << (i%8)
                self.masks[output] = int.from_bytes(bits, 'little')
            combined |= self.masks[output]
        # Decode bytes, not repeated wide-integer shifts for every member.
        return tuple(self.names[8*i+b] for i,value in enumerate(combined.to_bytes(self.width, 'little'))
                     for b in _BYTE_BITS[value])


def refine(trace, internal=None, progress=None):
    internal = internal or {}
    replacements = {}
    member_lists = {}
    # Keep one tuple/list pair per distinct member set. Per-gate replacements
    # retain the canonical pair rather than another enormous, equal tuple.
    def intern(members, ordered=False):
        key = tuple(members) if ordered else tuple(sorted(members))
        if key not in member_lists:
            member_lists[key] = (key, list(key))
        return member_lists[key]
    trace.abc_associations = []
    trace.abc_fallbacks = []
    for region in trace.regions:
        if 'input_nodes' not in region: continue
        before, after, fallback = cones(trace, region, progress)
        if fallback:
            trace.abc_fallbacks.append({"invocation_id": region["invocation_id"], "reason": fallback})
        captured = internal.get(region['invocation_id'], {})
        boundary_nodes = {}
        for name, node in trace.abc_imports.get(region['invocation_id'], {}).items():
            bits = node['ports'].get('\\Y', [])
            if len(bits) == 1:
                boundary_nodes.setdefault(bit_key(bits[0]), name)
        cone_members, cut_members = {}, {}
        packed = ConeMembers(before, region['inputs'])
        if progress: progress('abc_candidates', invocation=region['invocation_id'])
        # Replay already retained these immutable captured events. Avoid parsing
        # the whole raw file again (and repeating it for every ABC invocation).
        for ordinal, row in enumerate(trace.events):
            if progress and ordinal % 10000 == 0:
                progress('abc_candidates', events=ordinal, cached_cones=len(cone_members))
            if row['record_type'] not in {'cell_create', 'assignment'} or row.get('abc_invocation') != region['invocation_id']:
                continue
            boundary = row.get('abc_boundary', -1)
            outputs = {boundary} if boundary in before else after.get(row.get('abc_node'), set())
            output_key = tuple(sorted(outputs))
            mapped = row.get('abc_node', '')
            if not mapped and boundary >= 0:
                mapped = boundary_nodes.get((f'\\ys__n{boundary}', 0), '')
            cut = captured.get(mapped)
            if cut:
                identity = id(cut['inputs'])
                if identity not in cut_members:
                    cut_members[identity] = intern(set(cut['inputs']))
                member_key, shared_members = cut_members[identity]
            else:
                if output_key not in cone_members:
                    members = packed.union(output_key) if output_key and not fallback else ()
                    cone_members[output_key] = intern(members, ordered=True)
                member_key, shared_members = cone_members[output_key]
            if not member_key:
                continue  # Keep original region candidate, never invent an empty proof.
            evidence = {'ref': row['ref'], 'invocation_id': region['invocation_id'],
                        'mapped_node': mapped, 'output_boundaries': sorted(outputs),
                        'inputs': shared_members, 'precision': 'abc_output_cone_candidate',
                        'method': 'preserved_output_boundary_cones', 'internal_rewrite_provenance': False}
            if cut:
                evidence.update(cut)
                evidence['internal_rewrite_provenance'] = False  # functional cut proof, not complete lineage
            trace.abc_associations.append(evidence)
            replacements[row['ref']] = (member_key, row['seq'], cut['precision'] if cut else 'abc_output_cone', region)
        del before, after, packed, cone_members, cut_members, boundary_nodes
    if not replacements: return
    if progress: progress('abc_source_propagation', replacements=len(replacements), member_sets=len(member_lists))
    trace.edges = [e for e in trace.edges if e['to'] not in replacements and not e.get('abc_member_group')]
    shared_groups, shared_blocks, member_positions = {}, {}, {}
    def member_edges(inputs, node, region):
        common = {'event_id': region['seq'], 'capture': 'abc_member_set',
                  'abc_member_group': True, 'precision': 'abc_region_candidate'}
        if len(inputs) < 1024:
            return [{'from':i, 'to':node, **common} for i in inputs]
        iid = region['invocation_id']
        if iid not in member_positions:
            member_positions[iid] = {name:i for i,name in enumerate(sorted(region['inputs']))}
        positions = member_positions[iid]
        buckets = defaultdict(list)
        for ref in inputs:
            buckets[positions[ref]//256].append(ref)
        edges, links = [], []
        for bucket in buckets.values():
            if len(bucket) < 32:
                links.extend({'from':i, 'to':node, **common} for i in bucket)
                continue
            key = (iid, tuple(bucket))
            if key not in shared_blocks:
                block = iid + ':member_block_' + str(len(shared_blocks)+1)
                shared_blocks[key] = block
                edges.extend({'from':i, 'to':block, **common} for i in bucket)
            links.append({'from':shared_blocks[key], 'to':node, **common})
        # Publish all parents before the first edge into the union node.
        return edges + links
    for ref, (inputs, sequence, capture, region) in replacements.items():
        # Factor only identical member sets in the same invocation. The virtual
        # node preserves all paths and timestamps; it does not upgrade precision.
        if len(inputs) >= 32:
            key = (region['invocation_id'], id(inputs))
            if key not in shared_groups:
                node = region['invocation_id'] + ':members_' + str(len(shared_groups)+1)
                shared_groups[key] = node
                trace.edges.extend(member_edges(inputs, node, region))
            inputs = (shared_groups[key],)
        trace.edges.extend({'from': i, 'to': ref, 'event_id': sequence, 'capture': capture,
                            'precision': 'abc_output_cone_candidate' if capture == 'abc_output_cone' else capture} for i in sorted(inputs))
    trace.edges.sort(key=lambda e: e['event_id'])
    groups = {}
    for edge in trace.edges: groups.setdefault(edge['to'], []).append(edge)
    empty = frozenset()
    origins = {sid: frozenset({sid}) for sid in trace.sources}
    source_lists = {}
    precisions = {sid: 'cell' for sid in trace.sources}
    rank = {'cell': 0, 'bit_range': 0, 'process_action': 1, 'abc_internal_cut': 2,
            'abc_internal_joint_cut': 3, 'cell_group_candidate': 4, 'process_signal_candidate': 5, 'abc_output_cone_candidate': 6, 'abc_region_candidate': 7,
            'process_pending': -1}
    objects = {**trace.objects, **trace.assignments, **trace.process_nodes}
    for ordinal, (ref, edges) in enumerate(groups.items()):
        if progress and ordinal % 10000 == 0:
            progress('abc_source_propagation', objects=ordinal, total_objects=len(groups))
        # Sources with no incoming edges (pending cells, empty constant routes).
        parents = [origins.get(e['from'], empty) for e in edges]
        sources = parents[0] if all(p is parents[0] for p in parents) else frozenset().union(*parents)
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
            if id(sources) not in source_lists: source_lists[id(sources)] = sorted(sources)
            obj['origins'] = source_lists[id(sources)]
            obj['precision'] = p
        for e in edges: e['precision'] = p
    from .optimization import propagate_gaps
    propagate_gaps(trace, groups)


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
