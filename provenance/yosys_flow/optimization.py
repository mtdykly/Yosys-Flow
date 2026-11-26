"""Replay semantic optimization events; state snapshots alone are not lineage.

These checks authenticate event incidence, versions, dispositions and complete
port changes, not an arbitrary optimizer's functional correctness. The stock
comparison and independent source-selection tests remain separate checks.
"""
from .common import require

RULES = {'opt_share_operators': 'opt_share', 'wreduce_cell': 'wreduce',
         'wreduce_special': 'wreduce', 'opt_expr_mux_invert': 'opt_expr',
         'opt_expr_double_invert': 'opt_expr', 'opt_expr_const_logic': 'opt_expr',
         'opt_expr_replace': 'opt_expr'}
RANK = {'cell': 0, 'bit_range': 0, 'process_action': 1, 'cell_group_candidate': 2,
        'process_signal_candidate': 3, 'abc_region_candidate': 4, 'process_pending': 5}


def coverage_fields(row):
    return {'source_path_status': 'incomplete' if row.get('source_gaps') else 'not_established',
            'source_gap_ids': row.get('source_gaps', [])}


def input_object(trace, ref):
    return trace.objects[ref] if ref in trace.objects else trace.assignments[ref]


def precision(trace, inputs, *, group=False):
    values = [input_object(trace, r)['precision'] for r in inputs]
    value = max([('cell_group_candidate' if group else 'cell'), *values], key=RANK.__getitem__)
    return 'cell' if value == 'bit_range' else value


def replay(trace, row, live, retired, pass_name):
    rule, inputs = row['rule'], row['inputs']
    require(rule in RULES and RULES[rule] == pass_name, 'optimization rule/pass mismatch')
    require(inputs and len(inputs) == len(set(inputs)) and
            all(r in trace.objects or r in trace.assignments for r in inputs), 'unknown optimization inputs')
    start = row['start_seq']
    require(type(start) is int and 0 < start < row['seq'] and
            all(input_object(trace, r)['seq'] <= start for r in inputs), 'invalid optimization capture interval')
    premises = row['premises']
    require(set(premises) <= set(inputs), 'optimization premise not in inputs')
    for r, revision in premises.items():
        require((r in trace.objects and revision == -1) or
                (r in trace.assignments and revision == trace.assignments[r]['revision']), 'stale optimization premise')
    targets = set(inputs) - set(premises)
    require(targets and targets <= trace.objects.keys(), 'optimization has no cell targets')
    origins = sorted(set().union(*(set(input_object(trace, r)['origins']) for r in inputs)))
    require(list(row['origins']) == origins and row['precision'] == precision(trace, inputs, group=len(targets)>1),
            'optimization sources/precision differ from inputs')
    bindings = row['bindings']
    require(len({b['input'] for b in bindings}) == len(bindings), 'duplicate optimization binding')
    changed = {}
    for b in bindings:
        old, new = b['input'], b['ref']
        stem, version = old.rsplit('@', 1)
        source = next((r for r in targets if r.rsplit('@', 1)[0] == stem), None)
        require(old in live and source is not None and new == stem+'@'+str(int(version)+1) and
                new not in trace.objects, 'invalid optimization output version')
        require(trace.objects[source]['state'] != trace.objects[old]['state'], 'unchanged optimization binding')
        obj = dict(trace.objects[old])
        obj.update(ref=new, origins=row['origins'], precision=row['precision'], seq=row['seq'])
        trace.objects[new] = obj
        live.remove(old); retired.add(old); live.add(new)
        changed[source] = new
        trace.edges.extend({'from': r, 'to': new, 'event_id': row['seq'], 'capture': rule,
                            'precision': row['precision']} for r in dict.fromkeys([old, *inputs]))
    preserved, removed = set(row['preserved']), set(row['removed'])
    require(len(preserved) == len(row['preserved']) and len(removed) == len(row['removed']) and
            not (preserved & removed or preserved & changed.keys() or removed & changed.keys()) and
            preserved | removed | changed.keys() == targets, 'incomplete optimization input disposition')
    require(all(r in live for r in preserved), 'optimization preserved input changed')
    expected_ports = [{'before': old, 'after': new,
                       'old_ports': trace.objects[old]['state']['connections'],
                       'new_ports': trace.objects[new]['state']['connections'],
                       'old_parameters': trace.objects[old]['state']['parameters'],
                       'new_parameters': trace.objects[new]['state']['parameters']} for old, new in changed.items()]
    require(sorted(row['port_changes'], key=lambda p:p['before']) == sorted(expected_ports, key=lambda p:p['before']),
            'optimization port/width mapping differs from actual states')
    # Inventory just this decision's event interval, never the whole trace.
    created, connections, removed_stems, latest = set(), set(), set(), {}
    for event in reversed(trace.events):
        if event['seq'] <= start: break
        if event['record_type'] == 'cell_create': created.add(event['ref'].split('@')[0])
        if event['record_type'] == 'assignment': connections.add(event['ref'])
        if event['record_type'] == 'cell_remove': removed_stems.add(event['ref'].split('@')[0])
        if event['record_type'] in {'cell_create', 'cell_state'}:
            latest.setdefault(event['ref'].split('@')[0], event['ref'])
    require(all(r.split('@')[0] in removed_stems for r in removed), 'optimization removed input lacks retirement')
    outputs = row['outputs']
    require(len(outputs) == len(set(outputs)) and set(outputs) <= live and
            set(outputs) == set(changed.values()) | {latest[s] for s in created if latest[s] in live},
            'optimization output inventory differs from actual creation')
    require(len(row['connections']) == len(set(row['connections'])) and set(row['connections']) == connections,
            'optimization replacement connection inventory differs')
    for r in [*outputs, *connections]:
        require(list(input_object(trace, r)['origins']) == origins, 'optimization output lost input sources')
    trace.events.append(row)


def propagate_gaps(trace, groups=None):
    """Share sparse gap sets along evidence edges, including ABC virtual groups."""
    if not getattr(trace, 'object_gaps', {}): return
    cache, lists = {}, {}
    def obj(ref):
        for objects in (trace.objects, trace.assignments, trace.process_nodes):
            value = objects.get(ref)
            if value is not None: return value
    for objects in (trace.objects, trace.assignments, trace.process_nodes):
        for value in objects.values(): value.pop('source_gaps', None)
    if groups is None:
        groups = {}
        for edge in trace.edges: groups.setdefault(edge['to'], []).append(edge)
    empty = frozenset()
    def gaps(ref):
        if ref not in cache:
            value = obj(ref)
            direct = value.get('direct_source_gaps', ()) if value else ()
            if direct: cache[ref] = frozenset(direct)
        return cache.get(ref, empty)
    # Grouped in recorded topological order, just like source propagation.
    # Union a many-input ABC group once, then share it with all its outputs;
    # rebuilding an immutable union after every incoming edge is quadratic.
    for target, edges in groups.items():
        parents = {id(value):value for value in [gaps(target), *(gaps(e['from']) for e in edges)] if value}
        if not parents: continue
        combined = next(iter(parents.values())) if len(parents) == 1 else frozenset().union(*parents.values())
        cache[target] = combined
        own = obj(target)
        if own is not None:
            if combined not in lists: lists[combined] = sorted(combined)
            own['source_gaps'] = lists[combined]
    for gap in trace.object_gaps.values():
        value = obj(gap['ref'])
        if value is not None and not value.get('source_gaps'):
            value['source_gaps'] = value.get('direct_source_gaps', [])
