"""Version-bound region contracts derived from one validated provenance run.

Contracts expose recorded evidence; they do not promote functional witnesses to
complete causal history. Union changes the boundary and rechecks every output.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from .common import digest, require, unescape
from .diagnostics import endpoints, group_closure, audit_cut
from .extract import slice_module, extend_inputs
from .native import resolve_bit
from .region_functions import verify_outputs
from .runner import load_run, query_loaded

SELECTOR_KEYS = ('source','line','end_line','column','end_column','object','instance')
EXACT_PRECISIONS = {'cell','bit_range','process_action','abc_internal_cut','abc_internal_joint_cut'}
RULE = 'recorded_region_contract_v1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def seal(document):
    body = {k:v for k,v in document.items() if k!='contract_id'}
    return {**body, 'contract_id':'sha256:'+fingerprint(body)}


def connection_identity(row):
    if row.get('reason') == 'selected_source_connection':
        return ('connection',row['connection_ref'],row['connection_offset'])
    return ('bit',row['original_bit'])


def boundary_identities(row):
    identities = {connection_identity(row)}
    identities.update(('connection',s['ref'],s['offset']) for s in row.get('source_connections',[]))
    return identities


def outside_paths(module, names, boundary):
    """Expose paths leaving and returning to a non-convex region."""
    _,users = endpoints(module)
    selected = set(names)
    inputs = {b['original_bit']:b['port'] for b in boundary if b['direction']=='input'}
    paths = []
    for out in (b for b in boundary if b['direction']=='output' and isinstance(b['original_bit'],int)):
        queue,seen = [(out['original_bit'],[])],set()
        while queue:
            bit,path = queue.pop()
            if bit in seen: continue
            seen.add(bit)
            if bit in inputs and path:
                paths.append({'output_port':out['port'],'returning_input_port':inputs[bit],'outside_cells':path})
            for user in users[bit]:
                if user['kind']!='cell' or user['name'] in selected: continue
                cell = module['cells'][user['name']]
                for port,direction in cell['port_directions'].items():
                    if direction=='output':
                        queue.extend((b,[*path,user['name']]) for b in cell['connections'][port] if isinstance(b,int))
    return sorted(paths,key=lambda r:(r['output_port'],r['returning_input_port']))


class ContractContext:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        manifest_hash = digest(self.directory/'manifest.json')
        self.manifest,self.trace,_ = load_run(self.directory)
        require(self.manifest['flow']=='synth', 'region contracts require the default synth endpoint')
        netlist = (self.directory/'after.json').read_bytes()
        require(hashlib.sha256(netlist).hexdigest()==self.manifest['artifacts']['after.json']['sha256'],
                'after.json changed during contract creation')
        require(manifest_hash==digest(self.directory/'manifest.json'), 'manifest changed during contract creation')
        self.module = self.trace.design
        self.baseline = {'run_id':self.manifest['run_id'], 'endpoint':self.manifest['endpoint'],
                         'module':self.manifest['configuration']['top'],
                         'manifest_sha256':manifest_hash,
                         'artifacts':self.manifest['artifacts'], 'inputs':self.manifest['inputs']}
        self.validated, self.queries = {}, {}

    def input_changes(self):
        return [r['path'] for r in self.manifest['inputs'] if not Path(r['path']).is_file() or digest(r['path'])!=r['sha256']]

    def query(self, selector):
        require(set(selector)==set(SELECTOR_KEYS), 'invalid contract selector fields')
        key = fingerprint(selector)
        if key not in self.queries:
            args = SimpleNamespace(directory=str(self.directory),**selector)
            self.queries[key] = query_loaded(args,self.manifest,self.trace)
        return self.queries[key]

    def cell(self, ref):
        require(ref in self.trace.final, 'contract cell is not live in this baseline')
        selector = dict.fromkeys(SELECTOR_KEYS)
        selector['object'] = ref
        return self.query(selector)['final_cells'][0]

    def connection(self, ref):
        require(ref in self.trace.assignments and self.trace.assignments[ref]['lhs'], 'contract connection is not live')
        row = self.trace.assignments[ref]
        return {**row,'lhs_bits':[resolve_bit(b,self.module) for b in row['lhs']],
                'rhs_bits':[resolve_bit(b,self.module) for b in row['rhs']]}

    def validate(self, document, depth=0):
        require(depth<=16, 'contract parent nesting limit exceeded')
        require(document.get('schema_version')==1 and document.get('proof_rule')==RULE,
                'unsupported region contract schema or proof rule')
        require(document.get('contract_id')==seal(document)['contract_id'], 'contract content hash mismatch')
        require(document.get('baseline')==self.baseline, 'contract baseline differs from this run')
        key = document['contract_id']
        if key not in self.validated:
            expected = self.build(document['derivation'],depth)
            require(document==expected, 'contract differs from replayed region, outputs, evidence or obligations')
            self.validated[key] = True
        return document

    def build(self, derivation, depth=0):
        require(depth<=16, 'contract parent nesting limit exceeded')
        parents = []
        additional_inputs = []
        kind = derivation.get('kind')
        if kind=='selection':
            require(set(derivation)=={'kind','selector'}, 'invalid selection derivation')
            selection = self.query(derivation['selector'])
            requested = {c['ref'] for c in selection['final_cells']}
            connections = {c['ref'] for c in selection['final_connections']}
            source_ids = set(selection['seeds'])
            starting = requested
        elif kind=='union':
            require(set(derivation)=={'kind','parents'} and 2<=len(derivation['parents'])<=32,
                    'contract union requires 2 to 32 distinct parents')
            parents = [self.validate(p,depth+1) for p in derivation['parents']]
            require(len({p['contract_id'] for p in parents})==len(parents), 'duplicate parent contract')
            parents.sort(key=lambda p:p['contract_id'])
            derivation = {'kind':'union','parents':parents}
            requested = {r for p in parents for r in p['request']['cell_refs']}
            connections = {c['ref'] for p in parents for c in p['region']['connections']}
            source_ids = {r for p in parents for r in p['request']['source_ids']}
            starting = {c['ref'] for p in parents for c in p['region']['cells']}
            additional_inputs = [b['original_bit'] for p in parents for b in p['boundary'] if b['reason']=='new_edit_dependency']
        elif kind=='extend_inputs':
            require(set(derivation)=={'kind','parent','input_bits'} and isinstance(derivation['input_bits'],list)
                    and all(type(b) is int for b in derivation['input_bits']), 'invalid edit input extension')
            require(derivation['input_bits']==sorted(set(derivation['input_bits'])), 'edit input extension must be sorted and unique')
            parent=self.validate(derivation['parent'],depth+1);parents=[parent]
            requested=set(parent['request']['cell_refs']);connections={c['ref'] for c in parent['region']['connections']}
            source_ids=set(parent['request']['source_ids']);starting={c['ref'] for c in parent['region']['cells']}
            additional_inputs=derivation['input_bits']+[b['original_bit'] for b in parent['boundary'] if b['reason']=='new_edit_dependency']
        else:
            require(False, 'unknown contract derivation')
        require(starting or connections, 'no mapped final objects for a region contract; this does not prove no RTL effect')
        closure = group_closure(self.trace,starting)
        refs = set(closure['required_final_cell_refs'])
        closed = group_closure(self.trace,refs)
        cells = [self.cell(ref) for ref in sorted(refs)]
        connection_rows = [self.connection(ref) for ref in sorted(connections)]
        names = [unescape(c['name']) for c in cells]
        sliced,boundary = slice_module(self.module,names,'region',connection_rows)
        if kind=='union':
            # An extra input inherited from a parent ceases to be external
            # when its driver is included by another parent.
            driven={b for name in names for port,bits in self.module['cells'][name]['connections'].items()
                    if self.module['cells'][name]['port_directions'][port]=='output' for b in bits}
            additional_inputs=[b for b in additional_inputs if b not in driven]
        extend_inputs(self.module,names,sliced,boundary,additional_inputs)
        # Constants are VALUES, not identities. Only the captured LHS can bind
        # a constant connection to a top port; never collect all equal literals.
        unresolved_connections = []
        for row in boundary:
            if 'external_users' in row: continue
            bit = row['original_bit']
            if isinstance(bit,int):
                row['external_users'] = [u for u in row['users'] if u['kind']=='module_port' or u['name'] not in names]
            else:
                lhs = row['original_lhs']; name = unescape(lhs['wire'])
                port = self.module['ports'].get(name)
                if port and port['direction']=='output' and port['bits'][lhs['offset']]==bit:
                    row['external_users'] = [{'kind':'module_port','name':name,'offset':lhs['offset']}]
                else:
                    row['external_users'] = []
                    unresolved_connections.append({'connection_ref':row['connection_ref'],
                                                   'reason':'constant_lhs_has_no_observable_endpoint_binding'})
        cut = audit_cut(self.module,names,boundary)
        require(cut['selected_cell_cut_complete'], 'contract electrical boundary is incomplete')
        recorded = {}
        for cell in cells:
            state = self.trace.objects[cell['ref']]['state']
            recorded[unescape(state['name'])] = {'type':unescape(state['type']),
                'parameters':{unescape(k):v for k,v in state['parameters'].items()},
                'port_directions':{unescape(k):v for k,v in state['port_directions'].items()},
                'connections':{unescape(k):[resolve_bit(b,self.module) for b in bs] for k,bs in state['connections'].items()}}
        functions = verify_outputs(recorded,sliced['modules']['region']['cells'],boundary)
        supporting = refs | connections
        supporting = set(supporting)
        for edge in reversed(self.trace.edges):
            if edge['to'] in supporting: supporting.add(edge['from'])
        edges = [e for e in self.trace.edges if e['to'] in supporting]
        associations = [r for r in self.trace.abc_associations if r['ref'] in supporting]
        origin_ids = {s for c in cells for s in c['origin_ids']} | {s for c in connection_rows for s in c['origins']}
        provenance = {'sources':[self.trace.sources[s] for s in sorted(origin_ids)],
            'supporting_edges':edges, 'record_event_ids':sorted({e['event_id'] for e in edges}),
            'abc_associations':associations,
            'abc_internal':[r for r in self.trace.abc_internal if r['invocation_id'] in {a['invocation_id'] for a in associations}],
            'coverage':self.manifest['coverage'], 'coverage_gaps':self.manifest['coverage_gaps'],
            'source_completeness_proven':False, 'complete_causal_history':False}
        by_name = {unescape(c['name']):c for c in cells}
        outputs = []
        for proof in functions['outputs']:
            boundary_row = next(b for b in boundary if b['port']==proof['port'])
            contributing = [by_name[n] for n in proof['cone_cells']]
            connection_refs = {s['ref'] for s in boundary_row.get('source_connections',[])}
            if 'connection_ref' in boundary_row: connection_refs.add(boundary_row['connection_ref'])
            contributing_connections = [c for c in connection_rows if c['ref'] in connection_refs]
            sources = {s for c in contributing for s in c['origin_ids']} | {s for c in contributing_connections for s in c['origins']}
            candidates = [c['ref'] for c in contributing+contributing_connections if c['precision'] not in EXACT_PRECISIONS]
            output_refs = {c['ref'] for c in contributing} | connection_refs
            for edge in reversed(edges):
                if edge['to'] in output_refs: output_refs.add(edge['from'])
            evidence = [a['ref'] for a in associations if a['ref'] in output_refs]
            outputs.append({**proof, 'external_users':boundary_row['external_users'],
                'connection_refs':sorted(connection_refs), 'contributing_cell_refs':[c['ref'] for c in contributing],
                'recorded_source_ids':sorted(sources), 'candidate_refs':candidates,
                'association_evidence_refs':evidence,
                'source_correspondence':'candidate' if candidates else 'recorded_relations_and_declared_local_proofs',
                'source_completeness_proven':False, 'rtl_function_of_this_output_proven':False,
                'obligation':'preserve_for_every_external_consumer_unless_explicitly_reassigned_by_a_validated_edit'})
        candidates = sorted(c['ref'] for c in cells+connection_rows if c['precision'] not in EXACT_PRECISIONS)
        conditions = {'baseline_must_match':True, 'input_binding':'actual_one_bit_net_signals_with_recorded_polarity',
            'inputs_are_independent_in_function_proof':True,
            'equivalent_implementation_requirements':['implement_every_certified_output_function',
                'preserve_all_external_consumer_bindings','preserve_input_bits_and_polarities',
                'check_single_drivers_and_no_combinational_cycles'],
            'functional_edit_requirements':['provide_new_function_specification','account_for_all_shared_source_obligations',
                'check_new_dependencies_fit_boundary','validate_any_changed_consumer_binding'],
            'functional_edit_assessed':False,
            'outside_return_paths':outside_paths(self.module,names,boundary)}
        blockers = []
        if not functions['all_outputs_verified']: blockers.append('output_functions_not_fully_proven')
        if not closed['complete_for_recorded_groups']: blockers.append('recorded_group_not_closed')
        if candidates: blockers.append('candidate_source_associations')
        if unresolved_connections: blockers.append('connection_consumers_not_fully_bound')
        result = {'schema_version':1, 'report_type':'region_contract', 'proof_rule':RULE,
            'baseline':self.baseline, 'derivation':derivation,
            'request':{'cell_refs':sorted(requested), 'connection_refs':sorted(connections), 'source_ids':sorted(source_ids)},
            'region':{'cells':cells,'connections':connection_rows,'recorded_cell_functions':recorded},
            'expansion':{'added_cell_refs':sorted(refs-requested), 'reason':'recorded_joint_group_closure',
                         'upstream_cone_added':False},
            'boundary':boundary, 'outputs':outputs, 'electrical_cut':cut, 'recorded_group_closure':closed,
            'other_recorded_sources':[self.trace.sources[s] for s in sorted(origin_ids-source_ids)],
            'provenance':provenance, 'conditions':conditions,
            'verification':{'status':'verified_old_region' if not blockers else 'partial_old_region',
                'blockers':blockers, 'all_output_functions':functions,
                'candidate_refs':candidates, 'unresolved_connection_users':unresolved_connections,
                'scope':'baseline_region_functions_and_reported_evidence_only'},
            'safe_for_incremental_reuse':False, 'end_to_end_exact':False, 'synthesis_invocations':0}
        if len(self.trace.instance_catalog)>1:
            result['hierarchy'] = {'netlist_view':self.trace.view_rule,'instances':self.trace.instance_catalog,
                'recorded_flatten_events':list(self.trace.flatten_events.values()),
                'shared_definition_is_not_shared_physical_logic':True}
        if parents:
            counts = {}
            for p in parents:
                for c in p['region']['cells']: counts[c['ref']] = counts.get(c['ref'],0)+1
            mappings = []
            for p in parents:
                for old in p['boundary']:
                    new = next((b for b in boundary if b['direction']==old['direction'] and
                                connection_identity(old) in boundary_identities(b)),None)
                    mappings.append({'parent_id':p['contract_id'],'parent_port':old['port'],
                        'direction':old['direction'],'original_bit':old['original_bit'],
                        'new_port':new['port'] if new else None,
                        'disposition':'retained_boundary' if new else 'internalized'})
            result['union'] = {'parent_ids':[p['contract_id'] for p in parents],
                'overlapping_cell_refs':sorted(r for r,count in counts.items() if count>1),
                'parent_boundary_mapping':mappings, 'all_outputs_recomputed':True,
                'reused_association_refs':sorted({a['ref'] for p in parents for a in p['provenance']['abc_associations']})}
        return seal(deepcopy(result))


def publish(path, document):
    path = Path(path).absolute()
    require(path.suffix=='.json', 'contract output must be a new .json file')
    require(not path.exists() and not path.is_symlink(), 'refusing to overwrite '+str(path))
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.region-contract-',dir=path.parent) as tmp:
        staged = Path(tmp)/'contract.json'
        staged.write_text(json.dumps(document,indent=2,ensure_ascii=False)+'\n')
        os.link(staged,path)
    return {'out':str(path), 'contract_id':document['contract_id'],
            'status':document['verification']['status'], 'validated':True,
            'selected_cell_count':len(document['region']['cells']), 'output_count':len(document['outputs']),
            'safe_for_incremental_reuse':False}


def generate(args):
    context = ContractContext(args.directory)
    selector = {key:getattr(args,key,None) for key in SELECTOR_KEYS}
    if selector['source']: selector['source'] = str(Path(selector['source']).resolve())
    document = context.build({'kind':'selection','selector':selector})
    context.validate(document)
    return {**publish(args.out,document),'changed_or_missing_base_inputs':context.input_changes()}


def export_selection(context, document):
    """Reuse the existing netlist exporter for an exact (possibly union) region."""
    require(context.validate(document)==document, 'unvalidated export contract')
    refs = {c['ref'] for c in document['region']['cells']+document['region']['connections']}
    supporting = set(refs)
    for edge in reversed(context.trace.edges):
        if edge['to'] in supporting: supporting.add(edge['from'])
    nodes = [r for ref,r in context.trace.process_nodes.items() if ref in supporting]
    actions = {r['action'] for r in nodes if 'action' in r}
    provenance = document['provenance']
    invocations = {r['invocation_id'] for r in provenance['abc_associations']}
    changed = context.input_changes()
    cells,connections = document['region']['cells'],document['region']['connections']
    return {'final_cells':cells, 'final_connections':connections, 'resolution':'contract_region',
        'association_precision':sorted({r['precision'] for r in cells+connections}),
        'seeds':document['request']['source_ids'], 'sources':provenance['sources'],
        'edges':provenance['supporting_edges'],
        'events':[r for r in context.trace.events if r['seq'] in provenance['record_event_ids']],
        'process_actions':[r for ref,r in context.trace.process_actions.items() if ref in actions],
        'process_proof_nodes':nodes, 'process_discards':[],
        'techmap_bit_maps':[r for r in context.trace.techmap_bits if r['ref'] in supporting],
        'coverage':provenance['coverage'], 'coverage_gaps':provenance['coverage_gaps'],
        'abc_associations':provenance['abc_associations'], 'abc_internal':provenance['abc_internal'],
        'abc_regions':[r for r in context.trace.regions if r['invocation_id'] in invocations],
        'abc_refinement_fallbacks':context.trace.abc_fallbacks,
        'cut_proven_refs':[c['ref'] for c in cells if c['precision']=='abc_internal_cut'],
        'joint_cut_proven_refs':[c['ref'] for c in cells if c['precision']=='abc_internal_joint_cut'],
        'changed_or_missing_base_inputs':changed, 'end_to_end_exact':False}


def validate(args):
    context = ContractContext(args.directory)
    document = context.validate(json.loads(Path(args.contract).read_text()))
    changed = context.input_changes()
    return {'valid':True, 'contract_id':document['contract_id'], 'status':document['verification']['status'],
            'changed_or_missing_base_inputs':changed, 'current_input_files_match_baseline':not changed,
            'all_output_functions_verified':document['verification']['all_output_functions']['all_outputs_verified'],
            'safe_for_incremental_reuse':False, 'synthesis_invocations':0}


def merge(args):
    require(len(args.contracts)>=2, 'contract union requires at least two contracts')
    context = ContractContext(args.directory)
    parents = [context.validate(json.loads(Path(p).read_text())) for p in args.contracts]
    document = context.build({'kind':'union','parents':parents})
    context.validate(document)
    return {**publish(args.out,document),'changed_or_missing_base_inputs':context.input_changes()}
