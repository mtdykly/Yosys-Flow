"""Evidence for opt_dff's successful bitwise mux bypasses and FF slices.

The raw cell union stays intact. Only checked, simple control rewrites may
supply finer output proofs to the query projection; unknown controls fall back.
"""
from .common import require


def absorb(trace, row, live):
    root, donor = row['input'], row['donor']
    require(root in trace.objects and donor in live, 'unknown FF absorption endpoint')
    source, mux = trace.objects[root]['state'], trace.objects[donor]['state']
    require(source['module'] == mux['module'], 'FF absorption crosses modules')
    i, j = row['source_offset'], row['donor_offset']
    ports = source['connections']
    require(isinstance(i, int) and 0 <= i < len(ports.get('\\Q', [])) and
            row['q'] == ports['\\Q'][i:i+1], 'invalid FF absorption Q bit')
    mp = mux['connections']
    require(mux['type'] in {'$mux', '$_MUX_'} and isinstance(j, int) and
            0 <= j < len(mp.get('\\Y', [])), 'invalid FF absorption mux bit')
    require(len(mp['\\S']) == 1 and row['before'] == mp['\\Y'][j:j+1], 'FF absorption input differs from mux output')
    before = ports['\\D'][i:i+1]
    for prior in trace.ff_absorptions.values():
        if prior['input'] == root and prior['source_offset'] == i and prior['step_id'] == row['step_id']:
            before = prior['after']
    require(row['before'] == before, 'discontinuous FF absorption data path')
    a, b = mp['\\A'][j:j+1], mp['\\B'][j:j+1]
    require(row['mode'] in {'ce', 'srst'} and row['after'] in (a, b), 'invalid FF absorption mode or selected data')
    other = b if row['after'] == a else a
    require(other == row['q'] if row['mode'] == 'ce' else other in (['0'], ['1']),
            'FF absorption lacks feedback or reset constant')
    trace.ff_absorptions[row['seq']] = row


def simple_controls(source, target, links, absorptions, objects):
    """Do not narrow compound feedback or generated control logic yet."""
    allowed = {'$dff', '$dffe', '$sdff', '$sdffe', '$sdffce', '$adff', '$adffe'}
    if source['type'] not in allowed or target['type'] not in allowed:
        return False
    sp, tp = source['connections'], target['connections']
    sparams, tparams = source['parameters'], target['parameters']
    if sp.get('\\CLK') != tp.get('\\CLK') or int(sparams.get('\\CLK_POLARITY', '0'), 2) != int(tparams.get('\\CLK_POLARITY', '0'), 2):
        return False
    for port in set(sp) | set(tp):
        if port in {'\\D', '\\Q', '\\EN', '\\SRST'}:
            continue
        if sp.get(port) != tp.get(port):
            return False
    for key, val in sparams.items():
        if key in {'\\WIDTH', '\\CLK_POLARITY', '\\SRST_VALUE', '\\ARST_VALUE', '\\EN_POLARITY', '\\SRST_POLARITY'}:
            continue
        if key not in tparams or tparams[key].lstrip('0') != val.lstrip('0'):
            return False
    for port, mode in (('\\EN', 'ce'), ('\\SRST', 'srst')):
        polarity = port + '_POLARITY'
        if port in sp:
            if sp[port] != tp.get(port) or int(sparams.get(polarity, '0'), 2) != int(tparams.get(polarity, '0'), 2):
                return False
        for link in links:
            events = [absorptions[n] for n in link['absorptions'] if absorptions[n]['mode'] == mode]
            if not events:
                if (port in sp) != (port in tp):
                    return False
                continue
            for event in events:
                mp = objects[event['donor']]['state']['connections']
                # Selecting B requires S=1; selecting A requires S=0.
                data_is_b = event['after'] == mp['\\B'][event['donor_offset']:event['donor_offset']+1]
                pol = data_is_b if mode == 'ce' else not data_is_b
                if mp['\\S'] != tp.get(port) or int(tparams.get(polarity, '0'), 2) != int(pol):
                    return False
                if mode == 'srst':
                    reset = mp['\\A'] if data_is_b else mp['\\B']
                    value = tparams.get('\\SRST_VALUE', '')[::-1]
                    if value[link['offset']:link['offset']+1] != reset[event['donor_offset']]:
                        return False
    for param in ('\\ARST_VALUE', '\\SRST_VALUE'):
        if param in sparams:
            old, new = sparams[param][::-1], tparams.get(param, '')[::-1]
            if any(new[l['offset']:l['offset']+1] != old[l['source_offset']:l['source_offset']+1] for l in links):
                return False
    return True


def bit_map(trace, row, live):
    root, ref = row['input'], row['ref']
    require(root in trace.objects and ref in live and root != ref, 'unknown FF bit map endpoint')
    source, target = trace.objects[root], trace.objects[ref]
    require(target.get('operation') == 'opt_dff', 'FF bit map outside opt_dff rewrite')
    # The original source must be an actual ancestor of the emitted FF.
    ancestors = {ref}
    for edge in reversed(trace.edges):
        if edge['to'] in ancestors:
            ancestors.add(edge['from'])
    require(root in ancestors, 'FF bit map lacks derivation')
    sp, tp = source['state']['connections'], target['state']['connections']
    links = row['links']
    require([l['offset'] for l in links] == list(range(len(tp.get('\\Q', [])))) and bool(links), 'incomplete FF bit map')
    require(len({l['source_offset'] for l in links}) == len(links), 'duplicate FF source bit')
    proofs = []
    for link in links:
        i, j = link['source_offset'], link['offset']
        require(isinstance(i, int) and 0 <= i < len(sp.get('\\Q', [])), 'invalid FF source bit')
        require(link['q'] == sp['\\Q'][i:i+1] == tp['\\Q'][j:j+1], 'FF bit map Q correspondence differs')
        events = [r for r in trace.ff_absorptions.values() if r['input'] == root and
                  r['source_offset'] == i and r['step_id'] == row['step_id']]
        require(link['absorptions'] == [r['seq'] for r in events], 'FF bit map absorption evidence differs')
        d = events[-1]['after'] if events else sp['\\D'][i:i+1]
        require(link['d'] == d == tp['\\D'][j:j+1], 'FF bit map D correspondence differs')
        refs = set(source['output_proofs'][i] if source.get('output_proofs') else [root])
        refs.update(r['donor'] for r in events)
        proofs.append(sorted(refs))
    applicable = simple_controls(source['state'], target['state'], links, trace.ff_absorptions, trace.objects)
    if applicable:
        target['output_proofs'] = proofs
        target['state_proof_events'] = (source.get('state_proof_events', []) if source.get('output_proofs') else []) + [row['seq']]
    trace.ff_bit_maps.append({**row, 'applied': applicable,
                              'fallback_reason': '' if applicable else 'unsupported_ff_control_rewrite'})


def query_evidence(trace, supporting):
    ids = set().union(*(set(trace.ff_projection_evidence.get(ref, [])) for ref in supporting))
    maps = [r for r in trace.ff_bit_maps if r['seq'] in ids or r['ref'] in supporting]
    absorption_ids = {seq for r in maps for link in r['links'] for seq in link['absorptions']}
    return {
        'ff_bit_maps': maps,
        'ff_mux_absorptions': [trace.ff_absorptions[n] for n in sorted(absorption_ids)],
        'ff_alias_maps': [r for r in trace.ff_aliases.values() if r['seq'] in ids],
    }
