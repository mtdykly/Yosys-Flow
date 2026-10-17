"""Bounded multi-gate cut correspondence, with explicit group-level precision.

A vanished statement boundary cannot justify assigning individual rewritten gates
exclusively to that statement. A proved group keeps the union of its interior
sources and excludes the surviving boundary's upstream sources.
"""
from .abc_internal import MAX_INPUTS, Unsupported, evaluate, prove_cut, variables


def joint_cuts(first, final, original, initial, final_values, mask, gate_by_name,
               single_proofs, mapping_events, witnesses):
    proved_gates = {gate_by_name[name] for name in single_proofs}
    name_by_gate = {gate: name for name, gate in gate_by_name.items()}
    groups = []
    for name, target in gate_by_name.items():
        if name in single_proofs or not mapping_events.get(name):
            continue
        inside, boundary = set(), set()
        seen = set()
        def collect(i):
            if i in seen: return
            seen.add(i)
            node = final.nodes[i]
            if i in final.pis or (i != target and i in proved_gates):
                boundary.add(i)
                return
            if node.get('gate'):
                inside.add(i)
            for lit in node['fanins']: collect(lit//2)
        collect(target)
        # A useful joint group is smaller than a whole output cone: retain at
        # least one independently proved internal boundary, and no proved gate
        # in its interior. Never absorb another output just because it shares PIs.
        if len(inside) < 2 or not boundary.intersection(proved_gates) or len(boundary) > MAX_INPUTS:
            continue
        names = sorted(name_by_gate[i] for i in inside)
        if any(not mapping_events.get(n) for n in names): continue
        boundary = sorted(boundary)
        local_vars = variables(len(boundary))
        local_mask = (1 << (1 << len(boundary))) - 1
        values = dict(zip(boundary, local_vars))
        def visit(i):
            if i not in values:
                node = final.nodes[i]
                if node['op']=='pi': raise Unsupported('unbound_joint_input')
                args = [visit(lit//2) ^ (local_mask if lit&1 else 0) for lit in node['fanins']]
                values[i] = evaluate(node, args, local_mask)
            return values[i]
        truth = visit(target)
        matches = []
        for root in original:
            if initial[root] not in {final_values[target], mask^final_values[target]}: continue
            stages = witnesses(root)
            if not stages: continue
            polarity = int(initial[root] != final_values[target])
            match = prove_cut(first, root, initial, [final_values[i] for i in boundary],
                              None, polarity, mask, local_truth=truth)
            if match is None: continue
            covered, boundaries = match
            members = sorted({original[i] for i in covered if i in original})
            if len(members) < 2: continue
            matches.append({'entry_root': root, 'input_cells': members,
                            'output_inverted': bool(polarity), 'boundary_matches': boundaries,
                            'stage_witnesses': stages})
        if not matches: continue
        groups.append({'group_id': f"joint:{final.record['seq']}:{target}",
            'inputs': sorted({c for m in matches for c in m['input_cells']}),
            'matches': matches, 'mapped_nodes': names, 'export_nodes': sorted(inside),
            'export_root': target, 'boundary_nodes': boundary,
            'boundary_functions_independent': True,
            'mapping_events': sorted({e for n in names for e in mapping_events[n]})})
    result = {}
    for name in gate_by_name:
        joined = [g for g in groups if name in g['mapped_nodes']]
        if not joined: continue
        result[name] = {'inputs': sorted({c for g in joined for c in g['inputs']}),
            'joint_groups': joined, 'export_snapshot': final.record['seq'],
            'export_node': gate_by_name[name], 'method': 'verified_independent_boundary_joint_cut',
            'precision': 'abc_internal_joint_cut', 'attribution_scope': 'joint_group',
            'per_gate_source_exact': False, 'complete_rewrite_history': False}
    return result


def equivalence_cuts(first, final, original, initial, final_values, mask,
                     gate_by_name, single_proofs, mapping_events, witnesses, lineage):
    """Prove the shared implementation of roots joined by an actual fraig merge.

Unlike the retained-boundary rule, a proved equivalent root pair supplies the
anchor here. Boundaries must exist in BOTH original cones; a factor present in
only one spelling is part of the shared implementation, not a cut input.
    """
    from .common import require
    name_by_gate = {gate:name for name,gate in gate_by_name.items()}
    proved_gates = {gate_by_name[n] for n,p in single_proofs.items() if p['precision']=='abc_internal_cut'}
    groups = []
    for pair in lineage['pairs']:
        roots = pair['entry_roots']
        if not all(root in original for root in roots): continue
        require(initial[roots[0]] == (initial[roots[1]] ^ (mask if pair['inverted'] else 0)),
                'ABC entry equivalence phase differs from original roots')
        ancestor_values = []
        for root in roots:
            seen, stack = set(), [lit//2 for lit in first.nodes[root]['fanins']]
            while stack:
                i = stack.pop()
                if i in seen: continue
                seen.add(i)
                stack.extend(lit//2 for lit in first.nodes[i]['fanins'])
            ancestor_values.append({initial[i] for i in seen})
        for name, target in gate_by_name.items():
            if final_values[target] not in {initial[roots[0]],mask^initial[roots[0]]}: continue
            if not mapping_events.get(name): continue
            # Stop only at a retained internal function present in both cones.
            stops = {i for i in proved_gates if i!=target and
                     all(final_values[i] in funcs or (mask^final_values[i]) in funcs for funcs in ancestor_values)}
            inside, boundary, seen = set(), set(), set()
            def collect(i):
                if i in seen: return
                seen.add(i)
                if i in final.pis or i in stops:
                    boundary.add(i); return
                node = final.nodes[i]
                if node.get('gate'): inside.add(i)
                for lit in node['fanins']: collect(lit//2)
            collect(target)
            if not inside or not inside <= name_by_gate.keys() or len(boundary)>MAX_INPUTS: continue
            names = sorted(name_by_gate[i] for i in inside)
            if any(not mapping_events.get(n) for n in names): continue
            boundary = sorted(boundary)
            local_mask = (1 << (1 << len(boundary))) - 1
            values = dict(zip(boundary, variables(len(boundary))))
            def visit(i):
                if i not in values:
                    node = final.nodes[i]
                    if node['op']=='pi': raise Unsupported('unbound_equivalence_cut_input')
                    args = [visit(lit//2) ^ (local_mask if lit&1 else 0) for lit in node['fanins']]
                    values[i] = evaluate(node,args,local_mask)
                return values[i]
            truth = visit(target)
            matches = []
            for root in roots:
                stages = witnesses(root)
                if not stages: break
                polarity = int(initial[root]!=final_values[target])
                match = prove_cut(first,root,initial,[final_values[i] for i in boundary],
                                  None,polarity,mask,local_truth=truth)
                if match is None: break
                covered,boundaries = match
                members = sorted({original[i] for i in covered if i in original})
                if not members: break
                matches.append({'entry_root':root,'input_cells':members,'output_inverted':bool(polarity),
                                'boundary_matches':boundaries,'stage_witnesses':stages})
            if len(matches)!=len(roots): continue
            groups.append({'group_id':f"equivalence:{pair['equivalence_event']}:{target}",
                'basis':'recorded_fraig_equivalence', 'inputs':sorted({c for m in matches for c in m['input_cells']}),
                'matches':matches, 'mapped_nodes':names, 'export_nodes':sorted(inside),
                'export_root':target, 'boundary_nodes':boundary, 'boundary_functions_independent':True,
                'mapping_events':sorted({e for n in names for e in mapping_events[n]}),
                'entry_lineage':pair})
    result = {}
    for name in gate_by_name:
        joined = [g for g in groups if name in g['mapped_nodes']]
        if not joined: continue
        # A gate can also have an independent local cut or a previously proved
        # joint group. Keep that evidence and its sources when adding sharing.
        prior = single_proofs.get(name, {})
        inputs = {c for g in joined for c in g['inputs']} | set(prior.get('inputs',[]))
        result[name] = {**prior, 'inputs':sorted(inputs),
            'joint_groups':[*prior.get('joint_groups',[]),*joined],
            'export_snapshot':final.record['seq'],'export_node':gate_by_name[name],
            'method':'entry_lineage_and_verified_equivalence_cut',
            'precision':'abc_internal_joint_cut','attribution_scope':'joint_group',
            'per_gate_source_exact':False,'complete_rewrite_history':False}
    return result
