"""Validate passive ABC internal evidence and prove bounded functional cuts.

A cut match proves a local Boolean correspondence, NOT a complete causal rewrite
history. Boundary input dependencies are not attributed to the mapped gate.
"""
from collections import Counter
import json
import shlex

from .common import require

ABC_COMMIT = 'e026ed5380f3bdc3beea2ff9ffc23236fc549d5b'
MAX_INPUTS = 10
NETWORKS = {'network', 'mapping_network', 'mapped_gia', 'mapped_ntk',
            'netlist_source', 'netlist_target', 'export_network'}
DEFAULT_COMMANDS = ['read_blif', 'read_library', 'strash', '&get', '&fraig', '&put',
                    'scorr', 'dc2', 'dretime', 'strash', '&get', '&dch', '&nf', '&put', 'write_blif']


class Unsupported(Exception):
    pass


def variables(count):
    return [sum(1 << row for row in range(1 << count) if row & (1 << i)) for i in range(count)]


def evaluate(node, args, mask):
    op = node['op']
    if op == 'one': return mask
    if op == 'zero': return 0
    if op == 'buf': return args[0]
    if op == 'and': return args[0] & args[1]
    if op == 'xor': return args[0] ^ args[1]
    # GIA fanins: data1, data0, select.
    if op == 'mux': return (args[2] & args[0]) | ((mask ^ args[2]) & args[1])
    if op != 'sop': raise Unsupported('unsupported_internal_node:' + op)
    cubes = [line.split() for line in node['sop'].splitlines() if line.strip()]
    if not cubes: return 0
    result, phase = 0, None
    for cube in cubes:
        pattern, output = cube if len(cube) == 2 else ('', cube[0])
        require(len(pattern) == len(args) and set(pattern) <= {'0', '1', '-'} and output in {'0', '1'}, 'invalid ABC internal SOP')
        require(phase in {None, output}, 'mixed ABC SOP polarity')
        phase, term = output, mask
        for bit, arg in zip(pattern, args):
            if bit != '-': term &= arg if bit == '1' else mask ^ arg
        result |= term
    return result if phase == '1' else mask ^ result


class Network:
    def __init__(self, record):
        self.record = record
        self.nodes = {n['id']: n for n in record['nodes']}
        require(len(self.nodes) == len(record['nodes']) and all(isinstance(i, int) and i >= 0 for i in self.nodes), 'duplicate/invalid ABC internal object')
        self.pis = record.get('pis', sorted(i for i, n in self.nodes.items() if n['op'] == 'pi'))
        self.pos = record.get('pos', [])
        require(len(self.pis) == len(set(self.pis)) and set(self.pis) == {i for i, n in self.nodes.items() if n['op'] == 'pi'}, 'invalid ABC internal inputs')
        require(set(self.pos) <= self.nodes.keys(), 'invalid ABC internal outputs')
        arities = {'pi': 0, 'one': 0, 'zero': 0, 'buf': 1, 'and': 2, 'xor': 2, 'mux': 3}
        for n in self.nodes.values():
            require(all(isinstance(lit, int) and lit >= 0 and lit//2 in self.nodes for lit in n['fanins']), 'unknown ABC internal fanin')
            if n['op'] in arities: require(len(n['fanins']) == arities[n['op']], 'ABC internal arity mismatch')
        self.names = record.get('pi_names', [self.nodes[i].get('name', '') for i in self.pis])

    def truth(self, inputs=None):
        if len(self.pis) > MAX_INPUTS: raise Unsupported('input_limit_exceeded')
        mask = (1 << (1 << len(self.pis))) - 1
        values = dict(zip(self.pis, inputs if inputs is not None else variables(len(self.pis))))
        active = set()
        def visit(i):
            if i in values: return values[i]
            require(i not in active, 'cycle in ABC internal evidence')
            active.add(i)
            n = self.nodes[i]
            args = [visit(lit//2) ^ (mask if lit & 1 else 0) for lit in n['fanins']]
            values[i] = evaluate(n, args, mask)
            active.remove(i)
            return values[i]
        for i in self.nodes: visit(i)
        return values, mask


def script_commands(script):
    lexer = shlex.shlex(script, posix=True, punctuation_chars=';')
    lexer.whitespace_split = True
    command, result = [], []
    for token in lexer:
        if token == ';':
            if command and command[0] != 'echo': result.append(command)
            command = []
        else: command.append(token)
    if command and command[0] != 'echo': result.append(command)
    return result


def read_evidence(path, script):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    require(bool(rows) and rows[0]['kind'] == 'begin' and rows[0].get('schema') in {1, 2, 3} and
            rows[0].get('abc_commit') == ABC_COMMIT, 'unsupported ABC internal trace')
    require(rows[-1]['kind'] == 'end' and rows[-1].get('status') == 0, 'incomplete ABC internal trace')
    networks, commands, cuts = {}, [], []
    current, command_id, rewrites = None, 0, 0
    for seq, row in enumerate(rows, 1):
        require(row['seq'] == seq, 'ABC internal event sequence mismatch')
        kind = row['kind']
        if kind == 'command_begin':
            require(current is None and row['command_id'] == command_id+1, 'ABC internal command nesting mismatch')
            command_id += 1
            current = row['argv']
            commands.append(current)
        else:
            require(row['command_id'] == command_id, 'ABC internal command ID mismatch')
        if kind in NETWORKS | {'history_network'}:
            require(current is not None, 'ABC internal network outside command')
            networks[seq] = Network(row)
        elif kind == 'mapped_cut':
            require(current is not None and current[0] == '&nf', 'ABC mapping cut outside &nf')
            require(row['snapshot'] in networks and networks[row['snapshot']].record['kind'] == 'mapping_network', 'ABC cut has unknown network')
            net = networks[row['snapshot']]
            require(all(isinstance(x, int) and x >= 0 and x//2 in net.nodes for x in [row['root_literal'], *row['leaves']]), 'ABC mapping cut references unknown node')
            cuts.append(row)
        elif kind in {'gia_to_ntk', 'ntk_to_netlist'}:
            require(current is not None and row['before'] in networks and row['after'] in networks, 'ABC conversion has unknown network')
            before, after = networks[row['before']], networks[row['after']]
            divisor = 2 if kind == 'gia_to_ntk' else 1
            require(all(a//divisor in before.nodes and b in after.nodes for a,b in row['links']), 'ABC conversion has unknown object')
        elif kind in {'aig_replace', 'proved_equivalence'}:
            require(current is not None, 'ABC replacement outside command')
            if rows[0]['schema'] == 1 and kind == 'aig_replace':
                net = Network(row)
            else:
                require(row.get('snapshot') in networks, 'ABC relation has unknown snapshot')
                net = networks[row['snapshot']]
                require(net.record['kind'] == 'history_network' and net.record['command_id'] == command_id,
                        'ABC relation has wrong snapshot scope')
            require(row['old'] in net.nodes and isinstance(row['new_literal'], int) and row['new_literal'] >= 0
                    and row['new_literal']//2 in net.nodes, 'ABC replacement has unknown node')
            if kind == 'proved_equivalence':
                reasons = {'fraig_structural_merge': '&fraig', 'fraig_sat_merge': '&fraig',
                           'dch_structural_merge': '&dch', 'dch_sat_merge': '&dch',
                           'dch_representative': '&dch', 'dch_choice_link': '&dch'}
                require(reasons.get(row.get('reason')) == current[0], 'ABC equivalence has wrong reason/command')
            else:
                rewrites += 1
        elif kind == 'mapped_clone':
            require(rows[0]['schema']>=3 and current is not None and current[0]=='&put' and row['snapshot'] in networks,
                    'ABC mapped clone has invalid scope')
            net=networks[row['snapshot']]
            require(net.record['kind']=='history_network' and net.record['command_id']==command_id and
                    row['old'] in net.nodes and row['new'] in net.nodes and row['old']!=row['new'],
                    'ABC mapped clone has invalid objects')
            require({k:v for k,v in net.nodes[row['old']].items() if k not in {'id','name'}} ==
                    {k:v for k,v in net.nodes[row['new']].items() if k not in {'id','name'}},
                    'ABC mapped clone differs from donor')
        elif kind == 'entry_copy':
            require(current is not None and row['before'] in networks and row['after'] in networks,
                    'ABC entry copy has unknown network')
            before, after = networks[row['before']], networks[row['after']]
            expected_command = {'entry_strash':'strash', 'entry_strash_survivors':'&get',
                                'entry_ntk_to_aig':'&get', 'entry_aig_to_gia':'&get'}
            require(rows[0]['schema'] >= 3 and expected_command.get(row.get('reason')) == current[0],
                    'ABC entry copy has wrong reason/command')
            require(after.record['kind']=='history_network' and after.record['command_id']==command_id,
                    'ABC entry copy has wrong snapshot scope')
            require(len({a for a,b in row['links']})==len(row['links']) and
                    all(a in before.nodes and isinstance(b,int) and b>=0 and b//2 in after.nodes for a,b in row['links']),
                    'ABC entry copy has invalid object links')
            if row['reason']=='entry_strash_survivors':
                require(row['links']==[[i,2*i] for i in after.nodes] and set(after.nodes)<=set(before.nodes),
                        'ABC strash survivors have invalid identity links')
                require(all(before.nodes[i]==after.nodes[i] for i in after.nodes), 'ABC strash survivor changed')
            else:
                require({a for a,b in row['links']}==set(before.nodes)-set(before.pos),
                        'ABC entry copy lost object links')
        elif kind == 'history_copy':
            require(current is not None and row['before'] in networks and row['after'] in networks,
                    'ABC history copy has unknown network')
            before, after = networks[row['before']], networks[row['after']]
            require(all(n.record['kind']=='history_network' and n.record['command_id']==command_id for n in (before, after)),
                    'ABC history copy has wrong snapshot scope')
            require({'fraig_sweep_copy': '&fraig', 'dch_choice_copy': '&dch'}.get(row.get('reason')) == current[0],
                    'ABC history copy has wrong reason/command')
            require(len({a for a,b in row['links']}) == len(row['links']) and
                    all(a in before.nodes and isinstance(b,int) and b>=0 and b//2 in after.nodes for a,b in row['links']),
                    'ABC history copy has invalid object links')
            expected_sources = set(before.nodes) - (set(before.pos) if row['reason']=='dch_choice_copy' else set())
            require({a for a,b in row['links']} == expected_sources, 'ABC history copy lost object links')
        elif kind == 'command_end':
            require(current is not None and row['status'] == 0, 'ABC internal command failed')
            if rows[0]['schema'] >= 2:
                for key, event in (('equivalences', 'proved_equivalence'), ('history_copies', 'history_copy'), ('replacements', 'aig_replace')):
                    require(row.get(key)==sum(r['kind']==event and r['command_id']==command_id for r in rows),
                            'ABC command history event count mismatch')
            if rows[0]['schema'] >= 3:
                require(row.get('mapped_clones')==sum(r['kind']=='mapped_clone' and r['command_id']==command_id for r in rows), 'ABC command clone count mismatch')
                require(row.get('entry_copies')==sum(r['kind']=='entry_copy' and r['command_id']==command_id for r in rows),
                        'ABC command entry event count mismatch')
            current = None
        elif kind in {'begin', 'end'}:
            require(seq == 1 if kind == 'begin' else seq == len(rows) and current is None, 'unexpected ABC internal trace boundary')
        elif kind != 'command_begin': require(False, 'unknown ABC internal event: '+kind)
    used_history = set()
    for row in rows:
        if row['kind'] in {'aig_replace', 'proved_equivalence', 'mapped_clone'} and 'snapshot' in row:
            used_history.add(row['snapshot'])
        elif row['kind'] in {'history_copy', 'entry_copy'}:
            used_history.update(i for i in (row['before'],row['after']) if networks[i].record['kind']=='history_network')
    require(used_history=={seq for seq,n in networks.items() if n.record['kind']=='history_network'},
            'ABC internal history has unreferenced snapshots')
    for cid, command in enumerate(commands, 1):
        count = sum(n.record['command_id']==cid and n.record['kind']=='network' for n in networks.values())
        require(count == (0 if command[0] in {'read_library','write_blif'} else 1), 'ABC command network snapshot missing')
    require(current is None and commands == script_commands(script), 'ABC internal commands differ from executed script')
    require(sum(n.record['kind'] == 'export_network' for n in networks.values()) == 1, 'ABC exported internal network missing')
    return rows, networks, cuts, {'schema': rows[0]['schema'], 'abc_commit': ABC_COMMIT, 'command_count': len(commands),
        'network_count': len(networks), 'aig_replacements': rewrites,
        'equivalence_events': dict(Counter(r['reason'] for r in rows if r['kind']=='proved_equivalence')),
        'mapped_clones': sum(r['kind']=='mapped_clone' for r in rows),
        'entry_copies': sum(r['kind']=='entry_copy' for r in rows),
        'history_copies': sum(r['kind']=='history_copy' for r in rows), 'mapped_cuts': len(cuts),
        'commands': [c[0] for c in commands], 'internal_hook_coverage': 'partial',
        'complete_rewrite_history': False, 'proof_input_limit': MAX_INPUTS}


def analyze(path, script, region, imports, input_blif=None):
    rows, nets, cuts, report = read_evidence(path, script)
    proof, fallback = {}, None
    report['verified_aig_replacements'] = 0
    report['verified_equivalences'] = 0
    report['verified_history_copies'] = 0
    report['verified_entry_copies'] = 0
    try:
        if report['commands'] != DEFAULT_COMMANDS: raise Unsupported('unsupported_abc_script')
        stages = [n for n in nets.values() if n.record['kind'] == 'network']
        first = stages[0]
        if len(first.pis) > MAX_INPUTS: raise Unsupported('input_limit_exceeded')
        require(set(first.names) == {f'ys__n{i}' for i in region['input_ports']}, 'ABC internal input boundary mismatch')
        input_names = first.names
        outputs = {f'ys__n{i}' for i in region['output_ports']}
        # Bind interfaces by captured names and actual conversion links. Never
        # infer a PI identity from functional similarity or object allocation IDs.
        evaluated = {}
        for seq, net in nets.items():
            if net.record['kind']=='history_network': continue
            if len(net.pis) != len(input_names): raise Unsupported('internal_interface_changed')
            if not all(net.names) and net.record['kind'] in {'netlist_target', 'export_network'}:
                net.names = [next((n.get('name') for n in net.nodes.values() if n['fanins']==[2*i] and n.get('name') in input_names), '') for i in net.pis]
            if all(net.names) and set(net.names) == set(input_names):
                ordered = [variables(len(input_names))[input_names.index(n)] for n in net.names]
            elif net.record['kind'] == 'mapped_ntk':
                conversion = next((r for r in rows if r['kind']=='gia_to_ntk' and r['after']==seq), None)
                if conversion is None: raise Unsupported('missing_input_conversion')
                source= nets[conversion['before']]
                source_values,_=evaluated[conversion['before']]
                pi_links={b:a for a,b in conversion['links'] if b in net.pis}
                require(set(pi_links)==set(net.pis) and all(a%2==0 and a//2 in source.pis for a in pi_links.values()), 'invalid ABC input conversion')
                ordered=[source_values[pi_links[i]//2] for i in net.pis]
            else: raise Unsupported('unbound_internal_interface')
            evaluated[seq] = net.truth(ordered)
        initial, mask = evaluated[first.record['seq']]
        initial_outputs = {first.nodes[i].get('name'): initial[i] for i in first.pos}
        require(set(initial_outputs) == outputs, 'ABC internal output boundary mismatch')
        for net in stages[1:]:
            vals, _ = evaluated[net.record['seq']]
            names = net.record.get('po_names', [net.nodes[i].get('name') for i in net.pos])
            if set(names) == outputs:
                require({n: vals[i] for n,i in zip(names,net.pos)} == initial_outputs, 'ABC internal stage changes output function')
            else:
                raise Unsupported('unbound_internal_outputs')
        for row in rows:
            if row['kind'] in {'aig_replace', 'proved_equivalence'}:
                net = nets[row['snapshot']] if 'snapshot' in row else Network(row)
                vals, local_mask = net.truth()
                require(vals[row['old']] == (vals[row['new_literal']//2] ^ (local_mask if row['new_literal']&1 else 0)),
                        'ABC internal replacement/equivalence changes function')
                key = 'verified_aig_replacements' if row['kind']=='aig_replace' else 'verified_equivalences'
                report[key] += 1
            elif row['kind'] in {'history_copy', 'entry_copy'}:
                before, after = nets[row['before']], nets[row['after']]
                av, local_mask = before.truth()
                pi_links = {b//2: a for a,b in row['links'] if a in before.pis and not b&1 and b//2 in after.pis}
                require(set(pi_links)==set(after.pis) and len(pi_links)==len(before.pis), 'ABC history copy input correspondence changed')
                bv, _ = after.truth([av[pi_links[i]] for i in after.pis])
                require(all(av[a] == (bv[b//2] ^ (local_mask if b&1 else 0)) for a,b in row['links']),
                        'ABC history copy changes function')
                report['verified_entry_copies' if row['kind']=='entry_copy' else 'verified_history_copies'] += 1
        from .abc_lineage import entry_lineage
        lineage = entry_lineage(rows, nets)
        report['entry_lineage'] = lineage['summary']
        # Verify the captured real object conversion links, including polarity.
        for row in rows:
            if row['kind'] not in {'gia_to_ntk', 'ntk_to_netlist'}: continue
            av, _ = evaluated[row['before']]; bv, _ = evaluated[row['after']]
            for a,b in row['links']:
                expected = av[a//2] ^ (mask if a&1 else 0) if row['kind']=='gia_to_ntk' else av[a]
                require(expected == bv[b], 'ABC internal object conversion changes function')
        final = next(n for n in nets.values() if n.record['kind'] == 'export_network')
        fv, _ = evaluated[final.record['seq']]
        by_name = {n.get('name'): i for i,n in first.nodes.items() if n.get('name')}
        if input_blif is not None:
            verify_input_blif(first, input_blif)
        original = {by_name[f'ys__n{n["id"]}']: n['cell'] for n in region['input_nodes'] if n['cell'] and f'ys__n{n["id"]}' in by_name}
        require(set(original.values()) == set(region['inputs']), 'ABC initial objects differ from extracted cells')
        # .gate output wire is the net driven by the actual exported gate object.
        gates = {}
        for i,n in final.nodes.items():
            if n.get('name') and n['op']=='buf' and len(n['fanins'])==1:
                driver=n['fanins'][0]//2
                if final.nodes[driver].get('gate'): gates[n['name']]=driver
        # Join the captured REAL object copies: GIA literal -> mapped ABC node
        # -> exported netlist gate. Functions alone never establish this link.
        conversions = [r for r in rows if r['kind']=='gia_to_ntk']
        netlist_maps = [r for r in rows if r['kind']=='ntk_to_netlist']
        if len(conversions)!=1 or len(netlist_maps)!=1: raise Unsupported('ambiguous_mapping_conversion')
        gm, nm = conversions[0], netlist_maps[0]
        mapped_ntk, source_ntk = nets[gm['after']], nets[nm['before']]
        def structure(net):
            return {i:{k:v for k,v in n.items() if k!='name'} for i,n in net.nodes.items()}
        require(structure(mapped_ntk)==structure(source_ntk), 'ABC mapped network changed before export')
        require(structure(nets[nm['after']])==structure(final), 'ABC netlist changed during export')
        expected_roots = {lit for lit,node in gm['links'] if mapped_ntk.nodes[node]['op']=='sop' and mapped_ntk.nodes[node]['fanins']}
        require({c['root_literal'] for c in cuts} == expected_roots and len(cuts)==len(expected_roots), 'ABC selected mapping cut set differs from converted gates')
        copied = dict(nm['links'])
        literal_by_node = {node:lit for lit,node in gm['links']}
        clone_events = {r['seq']:r for r in rows if r['kind']=='mapped_clone'}
        require({event for old,new,event in gm.get('clones',[])}==set(clone_events), 'ABC mapped clone links are incomplete')
        for old,new,event in gm.get('clones',[]):
            r=clone_events[event]
            require(r['old']==old and r['new']==new and old in literal_by_node and new not in literal_by_node,
                    'ABC mapped clone lacks actual parent')
            captured=nets[r['snapshot']]
            require(all({k:v for k,v in captured.nodes[i].items() if k!='name'} ==
                        {k:v for k,v in mapped_ntk.nodes.get(i,{}).items() if k!='name'} for i in (old,new)),
                    'ABC mapped clone changed before conversion')
            literal_by_node[new]=literal_by_node[old]
        literal_by_gate = {copied[node]: lit for node,lit in literal_by_node.items() if node in copied}
        def witnesses(root):
            result = []
            for stage in stages:
                vals, _ = evaluated[stage.record['seq']]
                nodes = [i for i,v in vals.items() if v in {initial[root], mask^initial[root]}]
                if not nodes: return []
                result.append({'snapshot': stage.record['seq'], 'nodes': nodes})
            return result
        gate_by_name, mapping_by_name = {}, {}
        for name, imported in imports.items():
            out = imported['ports'].get('\\Y', [])
            if len(out)!=1 or not isinstance(out[0],dict): continue
            wire = out[0]['wire'].lstrip('\\')
            if wire not in gates: raise Unsupported('unbound_export_gate')
            target = gates[wire]
            gate = final.nodes[target]
            require(gate['gate'] == imported['type'].lstrip('\\'), 'ABC exported gate type mismatch')
            input_functions = [fv[lit//2] ^ (mask if lit&1 else 0) for lit in gate['fanins']]
            mapping=[]
            for cut in cuts:
                if cut['root_literal'] != literal_by_gate.get(target): continue
                require(structure(nets[cut['snapshot']]) == structure(nets[gm['before']]), 'ABC GIA changed after mapping')
                vals,_=evaluated[cut['snapshot']]
                value=vals[cut['root_literal']//2] ^ (mask if cut['root_literal']&1 else 0)
                leaves=[vals[lit//2] ^ (mask if lit&1 else 0) for lit in cut['leaves']]
                require(value==fv[target] and Counter(leaves)==Counter(input_functions), 'ABC chosen cut differs from exported gate')
                mapping.append(cut['seq'])
            gate_by_name[name], mapping_by_name[name] = target, mapping
            matches = []
            for root, cell in original.items():
                if initial[root] not in {fv[target], mask^fv[target]}: continue
                polarity = int(initial[root] != fv[target])
                match = prove_cut(first, root, initial, input_functions, gate, polarity, mask)
                if match is None: continue
                covered, boundaries = match
                members = sorted({original[i] for i in covered if i in original})
                if not members: continue
                # Every matched internal root must have a functional witness at
                # each actual command result. This is correspondence, not lineage.
                stage_witnesses = witnesses(root)
                if not stage_witnesses: continue
                if not mapping: continue
                matches.append({'entry_root':root,'input_cells':members,'output_inverted':bool(polarity),
                    'boundary_matches':boundaries,'stage_witnesses':stage_witnesses,'mapping_events':mapping})
            if matches:
                proof[name]={'inputs':sorted({c for m in matches for c in m['input_cells']}),
                    'matches':matches,'export_snapshot':final.record['seq'],'export_node':target,
                    'conversion_events':[gm['seq'],nm['seq'],*clone_events],
                    'method':'internal_stage_witnesses_and_verified_local_cut',
                    'precision':'abc_internal_cut','complete_rewrite_history':False}
        from .abc_joint import joint_cuts, equivalence_cuts
        joint = joint_cuts(first, final, original, initial, fv, mask, gate_by_name,
                           proof, mapping_by_name, witnesses)
        for item in joint.values():
            item['conversion_events'] = [gm['seq'], nm['seq'], *clone_events]
        proof.update(joint)
        equivalent = equivalence_cuts(first,final,original,initial,fv,mask,gate_by_name,
                                      proof,mapping_by_name,witnesses,lineage)
        for item in equivalent.values():
            item['conversion_events'] = [gm['seq'],nm['seq'],*clone_events]
        proof.update(equivalent)
    except Unsupported as exc:
        proof, fallback = {}, str(exc)
    report['proved_import_nodes']=sum(p['precision']=='abc_internal_cut' for p in proof.values())
    report['joint_proved_import_nodes']=sum(p['precision']=='abc_internal_joint_cut' for p in proof.values())
    report['joint_groups']=len({g['group_id'] for p in proof.values() for g in p.get('joint_groups', [])})
    report['equivalence_groups']=len({g['group_id'] for p in proof.values() for g in p.get('joint_groups',[]) if g.get('basis')=='recorded_fraig_equivalence'})
    report['unproved_import_nodes']=sorted(set(imports)-set(proof))
    report['fallback_reason']=fallback
    return proof, report


def prove_cut(net, root, functions, boundary_functions, gate, polarity, global_mask, local_truth=None):
    """Compare under INDEPENDENT cut inputs, not just reachable PI assignments."""
    if len(boundary_functions)>MAX_INPUTS: return None
    local_mask=(1 << (1 << len(boundary_functions)))-1
    values=variables(len(boundary_functions)); covered=set(); boundaries=[]; memo={}; active=set()
    def visit(i):
        if i in memo: return memo[i]
        if i != root:
            options=[(j,0) for j,f in enumerate(boundary_functions) if f==functions[i]]
            options += [(j,1) for j,f in enumerate(boundary_functions) if f==(global_mask^functions[i])]
            if options:
                if len(options)!=1: raise Unsupported('ambiguous_cut_boundary')
                j,inv=options[0]; boundaries.append({'entry_node':i,'mapped_input':j,'inverted':bool(inv)})
                memo[i]=values[j] ^ (local_mask if inv else 0); return memo[i]
        n=net.nodes[i]
        if n['op']=='pi': raise Unsupported('unmatched_cut_input')
        if i in active: raise Unsupported('cyclic_cut')
        active.add(i); covered.add(i)
        args=[visit(lit//2) ^ (local_mask if lit&1 else 0) for lit in n['fanins']]
        value=evaluate(n,args,local_mask)
        active.remove(i); memo[i]=value; return value
    try:
        expected=visit(root) ^ (local_mask if polarity else 0)
        if expected!=(evaluate(gate,values,local_mask) if local_truth is None else local_truth): return None
        return covered, boundaries
    except Unsupported:
        return None


def verify_input_blif(net, text):
    """Bind actual entry objects and their local functions to archived input BLIF."""
    lines = [l.split('#',1)[0].strip() for l in text.replace('\\\n',' ').splitlines()]
    nodes = {n.get('name'): n for n in net.nodes.values() if n['op']=='sop'}
    i = 0
    while i < len(lines):
        words=lines[i].split(); i+=1
        if not words or words[0]!='.names': continue
        names=words[1:]; cubes=[]
        while i<len(lines) and not lines[i].startswith('.'):
            if lines[i]: cubes.append(lines[i])
            i+=1
        require(names[-1] in nodes, 'ABC entry object missing from BLIF')
        node=nodes[names[-1]]
        fanins=[net.nodes[lit//2].get('name') for lit in node['fanins']]
        require(fanins==names[:-1] and all(lit%2==0 for lit in node['fanins']), 'ABC entry fanins differ from BLIF')
        local_mask=(1 << (1 << len(fanins)))-1
        args=variables(len(fanins))
        require(evaluate(node,args,local_mask)==evaluate({'op':'sop','sop':'\n'.join(cubes)},args,local_mask),
                'ABC entry function differs from BLIF')
