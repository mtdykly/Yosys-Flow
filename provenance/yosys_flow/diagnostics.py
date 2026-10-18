"""Read-only diagnostics of an existing run; no synthesis or replacement verdict.

Electrical cut completeness is distinct from source-selection completeness.
Recorded group closure is conservative and never silently changes a selection.
"""
from collections import Counter, defaultdict, deque
import json
from pathlib import Path
import time

from .common import digest, require, unescape
from .extract import slice_module
from .runner import load_run, query_loaded


def endpoints(module):
    drivers, users = defaultdict(list), defaultdict(list)
    for name, cell in module['cells'].items():
        for port, bits in cell['connections'].items():
            direction = cell['port_directions'][port]
            for offset, bit in enumerate(bits):
                if not isinstance(bit, int): continue
                endpoint = {'kind':'cell', 'name':name, 'port':port, 'offset':offset}
                if direction in {'input','inout'}: users[bit].append(endpoint)
                if direction in {'output','inout'}: drivers[bit].append(endpoint)
    for name, port in module['ports'].items():
        for offset, bit in enumerate(port['bits']):
            if not isinstance(bit, int): continue  # Constants are values, not shared electrical identities.
            endpoint = {'kind':'module_port', 'name':name, 'offset':offset}
            if port['direction'] in {'input','inout'}: drivers[bit].append(endpoint)
            if port['direction'] in {'output','inout'}: users[bit].append(endpoint)
    return drivers, users


def audit_cut(module, names, boundary):
    """Independently enumerate cut edges for selected CELLS, including all users.

Connection-only outputs retain slice_module's explicit original_lhs identities;
equal constant values must not invent aliasing between unrelated outputs.
"""
    selected = set(names)
    drivers, users = endpoints(module)
    ports = {(b['direction'], b['original_bit']) for b in boundary}
    violations, obligations = [], []
    inside = lambda e: e['kind']=='cell' and e['name'] in selected
    for bit, sinks in users.items():
        if any(inside(e) for e in sinks) and not any(inside(e) for e in drivers[bit]):
            if ('input',bit) not in ports: violations.append({'reason':'missing_input', 'bit':bit})
    for bit, source in drivers.items():
        if not any(inside(e) for e in source): continue
        external = [e for e in users[bit] if not inside(e)]
        if external:
            obligations.append({'original_bit':bit, 'external_users':external})
            if ('output',bit) not in ports: violations.append({'reason':'missing_output', 'bit':bit})
            else:
                reported = [e for b in boundary if b['direction']=='output' and b['original_bit']==bit
                            for e in b.get('external_users', [])]
                if any(e not in reported for e in external):
                    violations.append({'reason':'missing_external_user', 'bit':bit})
    return {'selected_cell_cut_complete':not violations, 'violations':violations,
            'output_obligations':obligations,
            'scope':'electrical_edges_of_selected_cells_only', 'source_completeness_proven':False}


def group_closure(trace, selected_refs):
    """Carry actual recorded group members through version/merge edges to final cells."""
    associations = trace.abc_associations
    groups, members = {}, defaultdict(set)
    for item in associations:
        for group in item.get('joint_groups', []):
            key = (item['invocation_id'],group['group_id'])
            groups[key] = group
    adjacency = defaultdict(set)
    for edge in trace.edges: adjacency[edge['from']].add(edge['to'])
    cache = {}
    def descendants(ref):
        if ref not in cache:
            seen, queue = {ref}, deque([ref])
            while queue:
                for child in adjacency[queue.popleft()]:
                    if child not in seen: seen.add(child); queue.append(child)
            cache[ref] = seen & trace.final
        return cache[ref]
    unresolved = defaultdict(set)
    for key, group in groups.items():
        for node in group['mapped_nodes']:
            seeds = {e['ref'] for e in associations if e['invocation_id']==key[0] and
                     e['mapped_node']==node and e['ref'] in trace.objects}
            final = set().union(*(descendants(ref) for ref in seeds))
            members[key].update(final)
            if not final: unresolved[key].add(node)
    closure, touched = set(selected_refs), set()
    changed = True
    while changed:
        changed = False
        for key, refs in members.items():
            if refs & closure:
                touched.add(key)
                if not refs <= closure: closure.update(refs); changed = True
    return {'selected_refs':sorted(selected_refs), 'required_final_cell_refs':sorted(closure),
            'additional_required_cell_refs':sorted(closure-set(selected_refs)),
            'complete_for_recorded_groups':closure==set(selected_refs) and not any(unresolved[k] for k in touched),
            'groups':[{'invocation_id':k[0], 'group_id':k[1], 'final_cell_refs':sorted(members[k]),
                       'members_without_final_cell':sorted(unresolved[k])} for k in sorted(touched)],
            'scope':'recorded_groups_and_recorded_descendants_only', 'selection_expanded':False}


def inventory(directory, trace):
    directory = Path(directory)
    result = []
    summaries = {r['invocation_id']:r for r in getattr(trace,'abc_internal',[])}
    for region in trace.regions:
        row = {'invocation_id':region['invocation_id'], 'input_count':len(region['input_ports']),
               'output_count':len(region['output_ports']), 'input_cell_count':len(region['inputs']),
               'internal':summaries.get(region['invocation_id']), 'event_counts':{}, 'snapshot_count':0,
               'snapshot_node_count_sum':0, 'snapshot_node_count_max':0, 'trace_bytes':0}
        name = region.get('artifacts',{}).get('input.blif.flow.jsonl')
        if name:
            counts = Counter(); path = directory/name
            row['trace_bytes'] = path.stat().st_size
            with path.open() as stream:
                for line in stream:
                    event = json.loads(line); counts[event['kind']] += 1
                    if 'nodes' in event:
                        row['snapshot_count'] += 1
                        row['snapshot_node_count_sum'] += len(event['nodes'])
                        row['snapshot_node_count_max'] = max(row['snapshot_node_count_max'],len(event['nodes']))
            row['event_counts'] = dict(sorted(counts.items()))
        result.append(row)
    return {'native_trace_bytes':(directory/'yosys.jsonl').stat().st_size,
            'native_event_counts':dict(Counter(r['record_type'] for r in trace.records)),
            'abc_trace_bytes':sum(r['trace_bytes'] for r in result), 'abc_regions':result}


def diagnose_loaded(args, manifest, trace, selection=None):
    """Caller must have validated this run via load_run; never starts external tools."""
    started = time.perf_counter()
    supplied_selection = selection is not None
    selection = selection if supplied_selection else query_loaded(args,manifest,trace)
    query_seconds = None if supplied_selection else time.perf_counter()-started
    path = Path(args.directory)/'after.json'
    require(digest(path)==manifest['artifacts']['after.json']['sha256'], 'after.json changed during diagnosis')
    module = trace.design
    names = [unescape(c['name']) for c in selection['final_cells']]
    boundary = []
    if names or selection['final_connections']:
        _, boundary = slice_module(module,names,'diagnosed',selection['final_connections'])
    selected_refs = {c['ref'] for c in selection['final_cells']}
    closure = group_closure(trace, selected_refs)
    cells = selection['final_cells']; seeds = set(selection['seeds'])
    other = sorted({sid for c in cells for sid in c['origin_ids']} - seeds)
    candidate_connections = [c['ref'] for c in selection['final_connections'] if 'candidate' in c['precision']]
    blockers = ['full_source_coverage_not_proven','new_rtl_dependencies_not_assessed']
    if selection['candidate_cell_refs'] or candidate_connections: blockers.append('candidate_associations')
    if not closure['complete_for_recorded_groups']: blockers.append('recorded_group_not_closed')
    if selection['changed_or_missing_base_inputs']: blockers.append('base_source_changed_or_missing')
    if not names and not selection['final_connections']: blockers.append('no_final_object_with_recorded_binding')
    cut = audit_cut(module,names,boundary)
    if not cut['selected_cell_cut_complete']: blockers.append('incomplete_electrical_cut')
    return {'schema_version':1, 'report_type':'region_diagnostics', 'run_id':manifest['run_id'], 'endpoint':manifest['endpoint'],
            'after_json_sha256':manifest['artifacts']['after.json']['sha256'],
            'selector':{k:getattr(args,k,None) for k in ('source','line','end_line','column','end_column','object','instance')},
            'resolution':selection['resolution'], 'selected_cells':cells,
            'selected_connections':selection['final_connections'], 'boundary':boundary,
            'electrical_cut':cut, 'recorded_group_closure':closure,
            'other_recorded_sources':[trace.sources[s] for s in other],
            'candidate_cell_refs':selection['candidate_cell_refs'], 'candidate_connection_refs':candidate_connections,
            'cut_proven_refs':selection['cut_proven_refs'], 'joint_cut_proven_refs':selection['joint_cut_proven_refs'],
            'abc_associations':selection['abc_associations'], 'abc_internal':selection['abc_internal'],
            'abc_refinement_fallbacks':selection['abc_refinement_fallbacks'],
            'coverage':selection['coverage'], 'coverage_gaps':selection['coverage_gaps'],
            'changed_or_missing_base_inputs':selection['changed_or_missing_base_inputs'],
            'replacement_assessment':{'status':'not_proven', 'blockers':blockers,
                'required_checks':['preserve_all_external_consumers','account_for_shared_sources_and_groups',
                                   'validate_new_input_dependencies_and_polarities','verify_modified_whole_design']},
            'safe_for_incremental_reuse':False, 'end_to_end_exact':False,
            'synthesis_invocations':0, 'timings_seconds':{'query_from_loaded_records':query_seconds},
            'note':'旧网表区域及约束诊断；电气切割完整与记录门组闭合不证明源码覆盖完整或修改后可安全替换。'}


def diagnose(args):
    timings = {}; started = time.perf_counter()
    manifest, trace, _ = load_run(args.directory, timings=timings)
    timings['load_and_validate_total'] = time.perf_counter()-started
    report = diagnose_loaded(args,manifest,trace)
    report['timings_seconds'].update(timings)
    report['inventory'] = inventory(args.directory,trace)
    return report
