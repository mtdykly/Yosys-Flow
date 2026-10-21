"""Local synthesis and checked splicing using an explicit old-region contract.

No automatic RTL diff or dependency discovery: callers supply a local RTL spec,
all port bindings and the complete new RTL as an independent validation oracle.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import time

from yosys_flow.common import PIN, TraceError, digest, identifier, require, require_combinational, token, unescape, write_json
from yosys_flow.contracts import ContractContext
from yosys_flow.hierarchy import electrical_design
from yosys_flow.region_functions import Circuit, UnsupportedFunction, ordered_inputs
from yosys_flow.boolean import BDD, ProofBudgetExceeded
from .failures import classify


def all_bits(module):
    for port in module['ports'].values(): yield from port['bits']
    for net in module['netnames'].values(): yield from net['bits']
    for cell in module['cells'].values():
        for bits in cell['connections'].values(): yield from bits


def check_network(module):
    """Fail closed on state, unknown semantics, undriven pins and cycles."""
    require_combinational(module)
    require(all(isinstance(b, int) or b in {'0', '1'} for b in all_bits(module)),
            'replacement requires fully defined two-state signals')
    driven = {}
    for name, port in module['ports'].items():
        require(port['direction'] in {'input', 'output'}, 'inout is not supported')
        if port['direction'] == 'input':
            for bit in port['bits']:
                require(isinstance(bit, int) and bit not in driven, 'aliased input or duplicate driver')
                driven[bit] = None
    dependencies = {}
    for name, cell in module['cells'].items():
        require(set(cell['port_directions']) == set(cell['connections']), 'missing pin direction')
        require(all(d in {'input', 'output'} for d in cell['port_directions'].values()), 'inout cell')
        # Verify supported gate shape independently of the selected region.
        require(not cell['parameters'] and all(len(bs) == 1 for bs in cell['connections'].values())
                and {p for p,d in cell['port_directions'].items() if d == 'output'} == {'Y'},
                'replacement expects generic one-bit combinational gates')
        for p, bs in cell['connections'].items():
            if cell['port_directions'][p] != 'output': continue
            for bit in bs:
                require(isinstance(bit, int) and bit not in driven, 'duplicate or constant output driver')
                driven[bit] = name
        dependencies[name] = [b for p,bs in cell['connections'].items()
                              if cell['port_directions'][p] == 'input' for b in bs if isinstance(b,int)]
    for bits in dependencies.values():
        require(all(b in driven for b in bits), 'undriven input in replacement network')
    for port in module['ports'].values():
        if port['direction'] == 'output':
            require(all(not isinstance(b,int) or b in driven for b in port['bits']), 'undriven module output')
    # Kahn's algorithm also checks disconnected loops, without recursion limits.
    pending = {n:{driven[b] for b in bs if driven[b] is not None} for n,bs in dependencies.items()}
    users = {n:[] for n in pending}
    for n, ds in pending.items():
        for d in ds: users[d].append(n)
    ready = [n for n,ds in pending.items() if not ds]; count = 0
    while ready:
        n = ready.pop(); count += 1
        for user in users[n]:
            pending[user].remove(n)
            if not pending[user]: ready.append(user)
    require(count == len(pending), 'replacement introduces a combinational cycle')


def validate_bindings(contract, patch, bindings):
    require(bindings.get('schema_version') == 1 and bindings.get('contract_id') == contract['contract_id'],
            'bindings must name this exact region contract')
    require(set(bindings) == {'schema_version','contract_id','inputs','outputs'}, 'unknown binding fields')
    boundary = {b['port']:b for b in contract['boundary']}
    result = {}
    for direction, key in [('input','inputs'), ('output','outputs')]:
        ports = {n:p for n,p in patch['ports'].items() if p['direction'] == direction}
        mapping = bindings[key]
        require(set(mapping) == set(ports), f'{direction} local RTL ports do not match bindings')
        seen = []
        for name, port in ports.items():
            labels = mapping[name]
            require(isinstance(labels, list) and len(labels) == len(port['bits']), 'binding width mismatch: '+name)
            for offset, binding in enumerate(labels):
                if isinstance(binding, str): binding = {'port':binding,'inverted':False}
                require(isinstance(binding,dict) and set(binding)=={'port','inverted'}
                        and isinstance(binding['inverted'],bool), 'invalid bit binding')
                label = binding['port']
                require(label in boundary and boundary[label]['direction'] == direction, 'unknown or wrong-direction boundary port')
                require(direction=='input' or not binding['inverted'], 'output inversion must be part of the explicit local RTL')
                seen.append(label)
                result[name,offset] = (boundary[label]['original_bit'],binding['inverted'])
        required = {n for n,b in boundary.items() if b['direction'] == direction}
        require(set(seen) == required and len(seen) == len(set(seen)),
                f'every contract {direction} must be bound exactly once')
    return result


def splice(module, contract, patch, bindings):
    """Keep every outside gate and pin unchanged; only replace selected drivers.

    Buffers bind local outputs to old wires, including constant, passthrough and
    shared local outputs. Never globally alias an old wire to a new input.
    """
    require(contract['verification']['status'] == 'verified_old_region',
            'experiment requires a verified old region; candidate evidence must be resolved first')
    selected = {unescape(c['name']) for c in contract['region']['cells']}
    require(selected and selected <= module['cells'].keys(), 'replacement requires selected final gates')
    check_network(module); check_network(patch)
    bound = validate_bindings(contract, patch, bindings)
    old_driven = {b for n in selected for p,bs in module['cells'][n]['connections'].items()
                  if module['cells'][n]['port_directions'][p] == 'output' for b in bs}
    all_outputs = [b for b in contract['boundary'] if b['direction'] == 'output']
    observers = [b for b in all_outputs if b['reason']=='selected_source_connection'
                 and not b['external_users'] and b['original_bit'] not in old_driven]
    outputs = [b for b in all_outputs if b not in observers]
    require(all(isinstance(b['original_bit'],int) and b['original_bit'] in old_driven for b in outputs),
            'connection-only outputs require a separate consumer-rebinding implementation')
    require(len({b['original_bit'] for b in outputs}) == len(outputs), 'ambiguous output identities')
    result = deepcopy(module)
    for n in selected: del result['cells'][n]
    next_bit = max(b for b in all_bits(module) if isinstance(b,int)) + 1
    bit_map = {}; input_inverters = []
    for name, port in patch['ports'].items():
        if port['direction'] != 'input': continue
        for i,b in enumerate(port['bits']):
            old_bit,inverted = bound[name,i]
            require(b not in bit_map, 'aliased local inputs have different bindings')
            bit_map[b] = next_bit if inverted else old_bit
            if inverted:
                input_inverters.append((old_bit,next_bit)); next_bit += 1
    for b in sorted({b for b in all_bits(patch) if isinstance(b,int)} - bit_map.keys()):
        bit_map[b] = next_bit; next_bit += 1
    def mapped(b): return bit_map[b] if isinstance(b,int) else b
    new_names = []; mapping = {}
    def insert(cell, hint):
        name = '$incremental$'+hint
        require(name not in module['cells'] and name not in result['cells'], 'incremental cell name collision')
        result['cells'][name] = cell; new_names.append(name)
        return name
    for i,(name,cell) in enumerate(sorted(patch['cells'].items())):
        cell = deepcopy(cell)
        cell['connections'] = {p:[mapped(b) for b in bs] for p,bs in cell['connections'].items()}
        mapping[name] = insert(cell, str(i))
    for i,(a,y) in enumerate(input_inverters):
        insert({'hide_name':1,'type':'$_NOT_','parameters':{},'attributes':{},
                'port_directions':{'A':'input','Y':'output'},'connections':{'A':[a],'Y':[y]}},f'input${i}')
    for name,port in patch['ports'].items():
        if port['direction'] != 'output': continue
        for i,b in enumerate(port['bits']):
            label = bindings['outputs'][name][i]
            if isinstance(label,dict): label=label['port']
            if label in {o['port'] for o in observers}: continue
            insert({'hide_name':1,'type':'$_BUF_','parameters':{},'attributes':{},
                    'port_directions':{'A':'input','Y':'output'},
                    'connections':{'A':[mapped(b)],'Y':[bound[name,i][0]]}}, f'output${name}${i}')
    internal_removed = old_driven - {b['original_bit'] for b in outputs}
    removed_aliases = [n for n,net in result['netnames'].items() if internal_removed.intersection(net['bits'])]
    for n in removed_aliases: del result['netnames'][n]
    for b in bit_map.values():
        name = f'$incremental$net${b}'
        require(name not in result['netnames'], 'incremental net name collision')
        result['netnames'][name] = {'hide_name':1,'bits':[b],'attributes':{}}
    check_network(result)
    outside = set(module['cells']) - selected
    require(all(module['cells'][n] == result['cells'][n] for n in outside), 'outside cell was modified')
    require(module['ports'] == result['ports'], 'outside module port was modified')
    # Check every external obligation explicitly, including secondary exits.
    for row in outputs:
        for user in row['external_users']:
            if user['kind'] == 'module_port': actual = result['ports'][user['name']]['bits'][user['offset']]
            else: actual = result['cells'][user['name']]['connections'][user['port']][user['offset']]
            require(actual == row['original_bit'], 'external consumer binding changed')
    return result, {'base_cells':len(module['cells']), 'removed_cells':len(selected),
        'reused_cells':len(outside), 'local_synthesized_cells':len(patch['cells']),
        'output_binding_buffers':len(outputs), 'input_polarity_inverters':len(input_inverters), 'result_cells':len(result['cells']),
        'removed_names':sorted(selected), 'reused_names':sorted(outside), 'added_names':new_names,
        'local_cell_map':mapping, 'removed_internal_aliases':removed_aliases,
        'preserved_observer_outputs':[b['port'] for b in observers],
        'outside_cells_identical':True, 'all_external_consumers_preserved':True,
        'shared_consumer_policy':'all_consumers_follow_the_bound_output; consumer splitting is unsupported'}


def run_yosys(binary, out, name, script):
    (out/(name+'.ys')).write_text(script+'\n')
    start = time.perf_counter()
    try:
        result = subprocess.run([binary,'-Q','-T','-s',name+'.ys'],cwd=out,capture_output=True,text=True,timeout=120)
    except subprocess.TimeoutExpired as exc:
        (out/(name+'.log')).write_text(str(exc)+'\n'+str(exc.stdout or '')+str(exc.stderr or ''))
        raise
    (out/(name+'.log')).write_text(result.stdout+result.stderr)
    require(result.returncode == 0, f'{name} failed; see {out/(name+".log")}')
    return time.perf_counter()-start


def equivalence_script(gold_path, gate_path, top):
    """One top-output SAT miter, without matched internal-name assumptions."""
    commands = []
    for path,name in [(gold_path,'gold'),(gate_path,'gate')]:
        commands.extend([f'read_json {token(path)}',f'hierarchy -check -top {top}',
            'setattr -mod -unset keep_hierarchy', 'setattr -unset keep_hierarchy',
            'flatten', 'check -assert', f'rename {top} {name}', f'design -stash {name}'])
    commands.extend(['design -copy-from gold -as gold gold','design -copy-from gate -as gate gate',
        'miter -equiv -flatten gold gate miter', 'hierarchy -check -top miter',
        'sat -verify -prove trigger 0 -set-def-inputs -show-inputs -show-outputs'])
    return '\n'.join(commands)


def prove_local_outputs(stitched, contract, patch, bindings, added_names):
    """Independent boundary proof: every spliced output implements the patch."""
    bound = validate_bindings(contract,patch,bindings)
    manager = BDD()
    boundary = ordered_inputs(contract['boundary'])
    inputs = dict(zip([b['original_bit'] for b in boundary],manager.variables(len(boundary))))
    local_inputs = {b:inputs[bound[n,i][0]] ^ int(bound[n,i][1]) for n,p in patch['ports'].items() if p['direction']=='input'
                    for i,b in enumerate(p['bits'])}
    expected = Circuit(patch['cells'],local_inputs,manager)
    actual = Circuit({n:stitched['cells'][n] for n in added_names},inputs,manager)
    checked = []
    try:
        for n,p in patch['ports'].items():
            if p['direction'] != 'output': continue
            for i,b in enumerate(p['bits']):
                require(expected.value(b) == actual.value(bound[n,i][0]), 'spliced output differs from local specification')
                checked.append(bindings['outputs'][n][i])
    except (UnsupportedFunction,ProofBudgetExceeded,RecursionError) as exc:
        raise TraceError('local output proof not completed: '+str(exc)) from exc
    return {'all_outputs_verified':True, 'ports':checked, 'domain':'two_state_independent_boundary',
            'reference':'synthesized_explicit_new_local_RTL', 'resources':manager.statistics()}


def run(args):
    started = time.perf_counter()
    timings = {}
    out = Path(args.out).resolve()
    require(not out.exists(), 'refusing to overwrite experiment directory: '+str(out))
    context = ContractContext(args.base_run)
    timings['baseline_load_and_evidence_replay_seconds'] = time.perf_counter()-started
    step = time.perf_counter()
    contract = context.validate(json.loads(Path(args.contract).read_text()))
    timings['contract_validation_seconds'] = time.perf_counter()-step
    archived=getattr(args,'baseline_from_archive',False)
    if archived:
        require('frontend.il' in context.manifest['artifacts'], 'automatic replacement requires an archived frontend')
        require(getattr(args,'new_frontend',None), 'archived replacement requires a frozen new frontend')
    else:
        require(not context.input_changes(), 'base inputs changed or missing; use a frozen baseline for this experiment')
    bindings = json.loads(Path(args.bindings).read_text())
    top = identifier(contract['baseline']['module']); local_top = identifier(args.patch_top)
    binary = shutil.which(args.yosys)
    require(binary is not None, 'Yosys executable not found')
    version = subprocess.check_output([binary,'-V'],text=True)
    require(PIN in version, 'experiment requires the pinned stock Yosys')
    patch_source = Path(args.patch_rtl).resolve()
    new_sources = [Path(s).resolve() for s in args.new_source]
    source_hashes = {str(p):digest(p) for p in [patch_source,*new_sources]}
    new_frontend=getattr(args,'new_frontend',None)
    if new_frontend:source_hashes[str(Path(new_frontend).resolve())]=digest(new_frontend)
    additional_hashes=getattr(args,'additional_input_hashes',{})
    require(all(digest(p)==h for p,h in additional_hashes.items()),'new input changed since frontend elaboration')
    source_hashes.update(additional_hashes)
    out.mkdir(parents=True)
    work = out/'work'; work.mkdir()
    report = {'schema_version':1, 'status':'running', 'base_run':str(context.directory),
        'contract_id':contract['contract_id'], 'baseline':contract['baseline'],
        'input_sha256':source_hashes, 'bindings':bindings, 'yosys':version.strip(),
        'yosys_sha256':digest(binary), 'semantic_domain':'two_state_combinational',
        'source_selection':'old_recorded_region_contract', 'automatic_RTL_diff':False,
        'base_netlist_view':context.trace.view_rule, 'output_representation':'flat_generic_gate_netlist',
        'complete_causal_history':False, 'safe_for_arbitrary_future_edits':False,
        'new_design_synthesis_used_only_as_validation_oracle':True, 'timings':timings}
    if archived:
        report['baseline_input_policy']='archived_hashed_frontend_and_netlist'
        report['changed_or_missing_old_files']=context.input_changes()
    write_json(out/'contract.json',contract)
    write_json(out/'bindings.json',bindings)
    stage = 'local_synthesis'
    try:
        base = electrical_design(context.trace,top)
        write_json(work/'base-view.json',base)
        read_patch='read_rtlil' if getattr(args,'patch_is_rtlil',False) else 'read_verilog -sv'
        report['timings']['local_synth_seconds'] = run_yosys(binary,work,'local-synth',
            f'{read_patch} {token(patch_source)}\nsynth -top {local_top} -flatten\ncheck -assert\nwrite_json local.json\nwrite_verilog -noattr local.v')
        patch_design = json.loads((work/'local.json').read_text())
        require(set(patch_design['modules']) == {local_top}, 'local spec must synthesize to one module')
        patch = patch_design['modules'][local_top]
        metadata = {n:c for n,c in patch['cells'].items() if c['type']=='$scopeinfo'}
        require(all(not c['connections'] for c in metadata.values()), 'scope metadata unexpectedly has electrical pins')
        patch = deepcopy(patch)
        patch['cells'] = {n:c for n,c in patch['cells'].items() if n not in metadata}
        report['local_non_electrical_scopeinfo'] = sorted(metadata)
        stage = 'binding_and_splice'; step = time.perf_counter()
        module, stats = splice(base['modules'][top],contract,patch,bindings)
        timings['binding_validation_and_splice_seconds'] = time.perf_counter()-step
        report['splice'] = stats
        stage = 'local_output_proof'; step = time.perf_counter()
        report['local_output_proof'] = prove_local_outputs(module,contract,patch,bindings,stats['added_names'])
        timings['local_output_proof_seconds'] = time.perf_counter()-step
        write_json(work/'candidate.json',{'modules':{top:module}})
        stage = 'candidate_checks'
        timings['candidate_check_and_export_seconds'] = run_yosys(binary,work,'candidate-check',f'read_json candidate.json\nhierarchy -check -top {top}\ncheck -assert\nscc -expect 0\nwrite_verilog -noattr candidate.v')
        timings['replacement_ready_seconds'] = time.perf_counter()-started
        # Build the reference only AFTER the candidate has been produced. It is
        # never read by splice(), and no new full-netlist gates are copied.
        frontend = 'read_rtlil '+token(new_frontend) if new_frontend else '\n'.join(f'read_verilog -sv {token(p)}' for p in new_sources)
        flatten = ' -flatten' if context.manifest.get('flatten',False) or '-flatten' in context.manifest['endpoint'] else ''
        stage = 'new_full_reference'
        report['timings']['new_full_synth_seconds'] = run_yosys(binary,work,'new-reference',
            f'{frontend}\nsynth -top {top}{flatten}\ncheck -assert\nwrite_json new-reference.json')
        reference = json.loads((work/'new-reference.json').read_text())
        for m in reference['modules'].values():
            require_combinational(m,reference['modules'])
            require(all(isinstance(b,int) or b in {'0','1'} for b in all_bits(m)), 'new reference contains undefined signals')
        require(reference['modules'][top]['ports'].keys() == module['ports'].keys(), 'new top interface changed')
        for n,p in module['ports'].items():
            q = reference['modules'][top]['ports'][n]
            require(p['direction']==q['direction'] and len(p['bits'])==len(q['bits']), 'new top interface changed')
        stage = 'whole_design_equivalence'
        report['timings']['whole_equivalence_seconds'] = run_yosys(binary,work,'whole-equivalence',
            equivalence_script('new-reference.json','candidate.json',top))
        # Check the deliverable Verilog, not just the in-memory JSON candidate.
        stage = 'deliverable_validation'; step = time.perf_counter()
        run_yosys(binary,work,'verilog-readback',
            f'read_verilog candidate.v\nhierarchy -check -top {top}\nproc\ntechmap\ncheck -assert\nwrite_json readback.json')
        run_yosys(binary,work,'verilog-equivalence',equivalence_script('candidate.json','readback.json',top))
        timings['verilog_roundtrip_seconds'] = time.perf_counter()-step
        require(all(digest(p) == h for p,h in source_hashes.items()), 'experiment source changed while running')
        require(digest(context.directory/'manifest.json') == contract['baseline']['manifest_sha256'], 'baseline changed')
        # Revalidate every archived artifact; never mutate the baseline run.
        stage = 'baseline_revalidation'; step = time.perf_counter()
        ContractContext(args.base_run).validate(contract)
        timings['final_baseline_revalidation_seconds'] = time.perf_counter()-step
        for extension in ('json','v'):
            shutil.copyfile(work/('candidate.'+extension),out/('incremental.'+extension))
        report.update(status='passed', checks={'contract_replayed':True,'outside_cells_identical':True,
            'all_boundary_outputs_implement_local_RTL':True,'all_external_consumers_preserved':True,
            'single_drivers_and_no_cycles':True,'all_top_outputs_match_new_full_synthesis':True,
            'emitted_Verilog_equivalent':True,'baseline_unchanged':True})
        report['artifacts'] = {str(p.relative_to(out)):digest(p) for p in sorted(out.rglob('*')) if p.is_file()}
        timings['total_checked_experiment_seconds'] = time.perf_counter()-started
        report['timing_scope'] = {'replacement_ready_seconds':'replay, contract check, binding, local synthesis, splice and candidate checks; excludes caller query/contract generation',
            'total_checked_experiment_seconds':'includes independent new full synthesis and all validation; not an incremental speedup claim'}
        write_json(out/'report.json',report)
        return report
    except Exception as exc:
        timings['failed_elapsed_seconds'] = time.perf_counter()-started
        report.update(status='failed', error=str(exc), failure=classify(stage,exc), published_incremental_netlist=False)
        write_json(out/'report.json',report)
        raise
