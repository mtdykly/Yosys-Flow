"""Replay the explicitly recorded BLIF -> strash -> AIG -> GIA entry path.

Only this segment has continuous object-copy evidence. Later functional witnesses
must not be described as a complete causal ABC history.
"""
from .common import require


def shape(net):
    return (net.record.get('representation'), net.pis, net.pos,
            {i:{k:v for k,v in n.items() if k!='name'} for i,n in net.nodes.items()})


def entry_lineage(rows, nets):
    copies = [r for r in rows if r['kind']=='entry_copy']
    summary = {'available':False, 'scope':'entry_to_fraig', 'copy_events':[], 'merged_root_pairs':0}
    if not copies:
        return {'summary':summary, 'pairs':[]}
    reasons = ['entry_strash','entry_strash_survivors','entry_ntk_to_aig','entry_aig_to_gia']
    require([r['reason'] for r in copies]==reasons, 'ABC entry copy chain is incomplete')
    require(all(a['after']==b['before'] for a,b in zip(copies,copies[1:])), 'ABC entry copy chain is disconnected')
    first = next(n for n in nets.values() if n.record['kind']=='network')
    require(copies[0]['before']==first.record['seq'], 'ABC entry copy chain has wrong start')
    # Source IDs map to literals. Complement flags are composed, never discarded.
    links = {i:2*i for i in first.nodes if i not in first.pos}
    paths = {i:[] for i in links}
    for event in copies:
        mapping = dict(event['links'])
        for source, lit in list(links.items()):
            if lit//2 not in mapping:
                del links[source]; del paths[source]
                continue
            mapped = mapping[lit//2] ^ (lit&1)
            paths[source].append({'event':event['seq'], 'from_literal':lit, 'to_literal':mapped})
            links[source] = mapped
    gia_snapshot = copies[-1]['after']
    entry = nets[gia_snapshot]
    sweep = [r for r in rows if r['kind']=='history_copy' and r['reason']=='fraig_sweep_copy'
             and r.get('entry_snapshot')==gia_snapshot]
    require(len(sweep)==1, 'ABC entry chain has no unique fraig result copy')
    sweep = sweep[0]
    require(shape(entry)==shape(nets[sweep['before']]), 'ABC fraig copy source differs from entry manager')
    result_map = dict(sweep['links'])
    cleanup = next((r for r in rows if r.get('reason')=='fraig_cleanup_copy' and r['before']==sweep['after']),None)
    cleanup_map = dict(cleanup['links']) if cleanup else None
    pairs = []
    for event in rows:
        if event['kind']!='proved_equivalence' or 'entry_snapshot' not in event: continue
        require(event['reason'] in {'fraig_sat_merge','fraig_structural_merge'} and event['entry_snapshot']==gia_snapshot,
                'ABC equivalence has invalid entry binding')
        require(shape(entry)==shape(nets[event['snapshot']]), 'ABC equivalence source differs from entry manager')
        left = [i for i,lit in links.items() if lit//2==event['old']]
        right = [i for i,lit in links.items() if lit//2==event['new_literal']//2]
        for a in left:
            for b in right:
                if a==b: continue
                phase = (links[a] ^ links[b] ^ event['new_literal']) & 1
                la = result_map[links[a]//2] ^ (links[a]&1)
                lb = result_map[links[b]//2] ^ (links[b]&1)
                require(la==(lb^phase), 'ABC proved equivalence was not adopted by sweep copy')
                cleaned = (cleanup_map[la//2]^(la&1)) if cleanup_map is not None and la//2 in cleanup_map else None
                pairs.append({'entry_roots':[a,b], 'inverted':bool(phase), 'equivalence_event':event['seq'],
                              'sweep_copy_event':sweep['seq'], 'entry_snapshot':first.record['seq'],
                              'gia_snapshot':gia_snapshot, 'paths':[paths[a],paths[b]],
                              'merged_literal':la, **({'cleanup_copy_event':cleanup['seq'],
                              'cleaned_literal':cleaned,'retired_by_cleanup':cleaned is None} if cleanup else {})})
    summary.update(available=True, copy_events=[r['seq'] for r in copies],
                   mapped_entry_objects=len(links), merged_root_pairs=len(pairs))
    if cleanup:
        summary.update(scope='entry_to_fraig_cleanup', cleanup_copy_event=cleanup['seq'],
                       cleanup_deleted_objects=len(cleanup['deleted']))
    return {'summary':summary, 'pairs':pairs}
