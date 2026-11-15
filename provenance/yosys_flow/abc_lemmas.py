"""Reuse successful, authenticated ABC solver events as Boolean lemmas.

This is proof-result replay (like accepting an UNSAT solver return), not a DRAT
checker. Only the pinned DCH success sites are accepted. A lemma is universally
quantified over its snapshot PIs; uniform PI renaming is substitution, not an
invented historical object mapping. Congruence applies the lemma to expressions.
"""
from collections import defaultdict, deque
from types import SimpleNamespace
import random

from .abc_checkpoints import network_blif
from .common import require, digest, write_json


REASONS = frozenset({'dch_sat_merge', 'dch_structural_merge'})


def export(rows, nets, summary, input_names, out, progress=None):
    groups = {}
    for row in rows:
        if row['kind'] != 'proved_equivalence' or row.get('reason') not in REASONS:
            continue
        require(summary['commands'][row['command_id'] - 1] == '&dch', 'DCH lemma outside DCH command')
        net = nets[row['snapshot']]
        require(len(net.pis) == len(input_names), 'DCH lemma PI arity mismatch')
        key = (id(net.nodes), tuple(net.pis))
        if key not in groups:
            groups[key] = (net, [])
        groups[key][1].append(row)
    results = []
    for ordinal, (net, events) in enumerate(groups.values()):
        # Internal managers have no preserved public PI names. The successful
        # equivalence is universal, so this explicit uniform renaming is valid
        # regardless of any historical manager-to-manager correspondence.
        graph = SimpleNamespace(names=list(input_names), pis=net.pis, pos=[],
                                record={'po_names': []}, nodes=net.nodes)
        path = out / ('lemma-' + str(ordinal) + '.blif')
        path.write_text(network_blif(graph))
        event_path = out / ('lemma-' + str(ordinal) + '.json')
        write_json(event_path, events)
        descriptor = dict(snapshot=net.record['seq'], graph=path.name, sha256=digest(path),
                          events=event_path.name, events_sha256=digest(event_path), count=len(events),
                          pi_substitution=[{'snapshot_pi':i,'variable':n} for i,n in zip(net.pis,input_names)],
                          rule='pinned_dch_success_events_universal_substitution_v1')
        results.append(descriptor)
        if progress:
            progress('recorded_solver_lemmas_exported', snapshot=descriptor['snapshot'], events=len(events))
    return results


def congruence(algebra, classes, equalities):
    """Iterative congruence closure of exact AND/complement terms and lemmas."""
    terms = {lit//2:key[1:] for key,lit in algebra.unique.items() if key[0]=='and'}
    parents = defaultdict(set)
    for ident,(a,b) in terms.items():
        parents[classes.find(a)//2].add(ident)
        parents[classes.find(b)//2].add(ident)
    queue=deque(terms);pending=set(terms);unions=0
    def join(a,b):
        nonlocal unions
        a,b=classes.find(a),classes.find(b)
        if a==b:return
        require(a!=(b^1),'recorded Boolean lemmas are contradictory')
        old=max(a//2,b//2);new=min(a//2,b//2)
        affected=parents.pop(old,set())
        parents[new].update(affected)
        classes.join(a,b);unions+=1
        for node in affected:
            if node not in pending:queue.append(node);pending.add(node)
    for a,b in equalities:join(a,b)
    unique={}
    while queue:
        ident=queue.popleft();pending.remove(ident)
        a,b=sorted(classes.find(n) for n in terms[ident])
        if a==0 or a==(b^1):target=0
        elif a==1 or a==b:target=b
        else:target=unique.setdefault((a,b),2*ident)
        join(2*ident,target)
    return unions


def _raw_associative_congruence(algebra,classes,*,leaf_limit=1024,progress=None):
    """Exact AND associativity, idempotence and complement identities.

    Flatten the original positive AND structure BEFORE replacing leaves by
    proved representatives. Replacing internal nodes first can hide an AND
    behind a complemented representative and lose a valid balance identity.
    A limit truncates normalization, never establishes an unproved equality.
    """
    require(leaf_limit>=2,'invalid associative normalization bound')
    flats={};parents=defaultdict(set);truncated=0
    for key,lit in algebra.unique.items():
        if key[0]!='and':continue
        leaves=set()
        for value in key[1:]:
            leaves.update(flats.get(value,{value}) if not value&1 else {value})
        if len(leaves)>leaf_limit:
            leaves=set(key[1:]);truncated+=1
        flats[lit]=frozenset(leaves)
        for value in leaves:parents[classes.find(value)//2].add(lit)
    queue=deque(flats);pending=set(flats);unique={};unions=0;visited=0
    while queue:
        literal=queue.popleft();pending.remove(literal);visited+=1
        leaves={classes.find(value) for value in flats[literal]};leaves.discard(1)
        if 0 in leaves or any(value^1 in leaves for value in leaves):target=0
        elif not leaves:target=1
        elif len(leaves)==1:target=next(iter(leaves))
        else:target=unique.setdefault(frozenset(leaves),literal)
        a,b=classes.find(literal),classes.find(target)
        if a!=b:
            require(a!=(b^1),'associative Boolean facts are contradictory')
            old=max(a//2,b//2);new=min(a//2,b//2)
            affected=parents.pop(old,set());parents[new].update(affected)
            classes.join(a,b);unions+=1
            for node in affected:
                if node not in pending:queue.append(node);pending.add(node)
        if progress and visited%500000==0:
            progress('and_associativity_closure',visited=visited,pending=len(queue),unions=unions)
    return {'rule':'and_associativity_proved_leaf_classes_v1','unions':unions,'visited':visited,
            'leaf_limit':leaf_limit,'truncated_flattening':truncated}


def _normalized_associative_congruence(algebra,classes,*,leaf_limit=64,progress=None):
    """Balance the AND forms of already proved representatives as well.

    Raw syntax and normalized syntax expose different valid factorizations.
    Every flattened form is equal to its own term; no sampled correspondence
    or speculative cut participates. Work/size bounds only stop discovery.
    """
    terms={lit//2:key[1:] for key,lit in algebra.unique.items() if key[0]=='and'}
    parents=defaultdict(set)
    for ident,args in terms.items():
        for value in args:parents[classes.find(value)//2].add(ident)
    queue=deque(terms);pending=set(terms);flat={};unique={};unions=0;visited=0
    work_limit=max(100000,32*len(terms))
    def push(nodes):
        for node in nodes:
            if node not in pending:queue.append(node);pending.add(node)
    while queue and visited<work_limit:
        ident=queue.popleft();pending.remove(ident);visited+=1
        leaves=set()
        for value in terms[ident]:
            value=classes.find(value)
            leaves.update(flat.get(value,{value}) if not value&1 else {value})
        leaves={classes.find(value) for value in leaves};leaves.discard(1)
        if 0 in leaves or any(value^1 in leaves for value in leaves):target=0;form=frozenset({0})
        elif not leaves:target=1;form=frozenset({1})
        elif len(leaves)==1:target=next(iter(leaves));form=frozenset(leaves)
        elif len(leaves)<=leaf_limit:
            form=frozenset(leaves);target=unique.setdefault(form,2*ident)
        else:form=frozenset({2*ident});target=2*ident
        if flat.get(2*ident)!=form:
            flat[2*ident]=form
            if classes.find(2*ident)==2*ident:push(parents[ident]-{ident})
        for value in leaves:parents[value//2].add(ident)
        a,b=classes.find(2*ident),classes.find(target)
        if a!=b:
            require(a!=(b^1),'normalized associative Boolean facts are contradictory')
            old=max(a//2,b//2);new=min(a//2,b//2)
            affected=parents.pop(old,set());parents[new].update(affected)
            classes.join(a,b);unions+=1;push(affected)
        if progress and visited%500000==0:
            progress('normalized_and_associativity',visited=visited,pending=len(queue),unions=unions)
    return dict(unions=unions,visited=visited,leaf_limit=leaf_limit,work_limit=work_limit,
                discovery_complete=not queue)


def associative_congruence(algebra,classes,*,leaf_limit=1024,progress=None):
    """Alternate raw and proved-normalized AND identities, with bounded rounds."""
    rounds=[_raw_associative_congruence(algebra,classes,leaf_limit=leaf_limit,progress=progress)]
    for _ in range(2):
        normalized=_normalized_associative_congruence(algebra,classes,progress=progress)
        rounds.append(normalized)
        if not normalized['unions']:break
        raw=_raw_associative_congruence(algebra,classes,leaf_limit=leaf_limit,progress=progress)
        rounds.append(raw)
        if not raw['unions']:break
    return dict(rule='raw_and_proved_normalized_associativity_v1',rounds=rounds,
                unions=sum(r['unions'] for r in rounds),visited=sum(r['visited'] for r in rounds))


def install(engine, graph, events, descriptor):
    """Caller must derive graph/events from the authenticated raw capture."""
    require(descriptor['rule']=='pinned_dch_success_events_universal_substitution_v1','unknown solver lemma rule')
    require(set(graph.pis)==set(engine.first.pis), 'solver lemma substitution has different inputs')
    require(len(events)==descriptor['count'], 'solver lemma count mismatch')
    algebra=engine.algebra
    values=graph.values(graph.order,{n:algebra.variable(n) for n in graph.pis},algebra.one)
    rng=random.Random(76183);mask=(1<<256)-1
    samples=graph.values(graph.order,{n:rng.getrandbits(256) for n in sorted(graph.pis)},mask)
    equations=[]
    pi_names={r['snapshot_pi']:r['variable'] for r in descriptor.get('pi_substitution',[])}
    def name(ident):return pi_names.get(ident,'__checkpoint_'+str(ident))
    for event in events:
        require(event['kind']=='proved_equivalence' and event['reason'] in REASONS,'unproved event cannot become a lemma')
        a=name(event['old']);b=name(event['new_literal']//2)
        require(a in values and b in values,'solver lemma root absent from snapshot')
        require(samples[a]==(samples[b]^(mask if event['new_literal']&1 else 0)),
                'recorded solver result contradicts its snapshot function/polarity')
        equations.append((values[a].literal,values[b].literal^(event['new_literal']&1)))
    count=congruence(algebra,engine.classes,equations)
    engine.proofs.append(dict(proof='recorded_dch_solver_results',witness=descriptor,
                             events=len(events),congruence_unions=count,independently_resolved_again=False))
    return count
