"""Prove simulation-proposed correspondences in dependency/SCC order.

Simulation is only a search hint. Each component is proved on independent
inputs representing ALREADY PROVED dependencies. Cyclic candidate matches are
expanded together, never assumed equal. No proof is accepted on a timeout.
"""
from collections import deque
import json
import time

from yosys_flow.boolean import BDD, ProofBudgetExceeded


def components(graph):
    """Iterative Kosaraju; edges point to proof dependencies."""
    visited, order = set(), []
    reverse = {n: [] for n in graph}
    for n, deps in graph.items():
        for dep in deps:
            if dep in reverse:
                reverse[dep].append(n)
    for root in graph:
        stack = [(root, False)]
        while stack:
            n, done = stack.pop()
            if done:
                order.append(n)
            elif n not in visited:
                visited.add(n)
                stack.append((n, True))
                stack.extend((d, False) for d in graph[n] if d in graph and d not in visited)
    result, owner = [], {}
    for root in reversed(order):
        if root in owner:
            continue
        group, stack = [], [root]
        while stack:
            n = stack.pop()
            if n in owner:
                continue
            owner[n] = len(result)
            group.append(n)
            stack.extend(reverse[n])
        result.append(group)
    return result, owner


def prove(manager, roots, patterns):
    """Prove every member in each class, not just one representative/output."""
    if all(len(set(values)) == 1 for values in roots):
        return 'structural_cut_identity'
    variables = [n for n, term in enumerate(manager.nodes) if term and term[0] == 'input']
    count = len(variables)
    if count <= 12:
        full = (1 << (1 << count)) - 1
        if count not in patterns:
            patterns[count] = [sum(1 << row for row in range(1 << count) if row >> i & 1)
                               for i in range(count)]
        values = {0: 0, **dict(zip(variables, patterns[count]))}
        def value(lit):
            return values[lit >> 1] ^ (full if lit & 1 else 0)
        for n, term in enumerate(manager.nodes):
            if term and term[0] == 'and':
                values[n] = value(term[1]) & value(term[2])
        return 'exhaustive_cut_identity' if all(len({value(r) for r in group}) == 1 for group in roots) else None
    if count > 128:
        return None
    try:
        bdd = BDD(node_limit=4096, work_limit=20000)
        values = {0: bdd.one ^ bdd.one, **dict(zip(variables, bdd.variables(count)))}
        def value(lit):
            return values[lit >> 1] ^ int(lit & 1)
        for n, term in enumerate(manager.nodes):
            if term and term[0] == 'and':
                values[n] = value(term[1]) & value(term[2])
        return 'robdd_cut_identity' if all(len({value(r).node for r in group}) == 1 for group in roots) else None
    except ProofBudgetExceeded:
        return None


def refine(manager, gold, candidate, deadline, budget, evidence, progress=None):
    start = time.monotonic()
    end = start + budget
    left = manager.reachable(gold.values(), deadline)
    right = manager.reachable(candidate.values(), deadline)
    shared = left & right
    buckets = {}
    phase = {}
    for n in sorted(left | right):
        signature = manager.sample(2*n)
        phase[n] = int(signature > (manager.mask ^ signature))
        signature ^= manager.mask if phase[n] else 0
        # Rare-event functions that simulate as constants are bad match seeds.
        # Actual identical nodes still get structural cut identities below.
        if signature:
            buckets.setdefault(signature, []).append(n)
    classes, node_class = {}, {}
    for nodes in buckets.values():
        if len(nodes) > 8 or not left.intersection(nodes) or not right.intersection(nodes):
            continue
        group = len(classes)
        classes[group] = [2*n + phase[n] for n in nodes]
        node_class.update((n, group) for n in nodes)
    for n in sorted(left & right):
        if n not in node_class:
            node_class[n] = len(classes)
            classes[len(classes)] = [2*n]
            phase[n] = 0
    proven = {g: roots[0] for g, roots in classes.items() if len(roots) == 1}
    pending = {g for g in classes if g not in proven}
    graph, oversized = {}, set()
    stats = {'proposed_classes': len(pending), 'structural_classes': len(proven),
             'proved_classes': 0, 'proved_components': 0, 'cyclic_components': 0,
             'unproved_components': 0, 'simulation_is_proof': False,
             'blocked_dependencies': 0, 'local_proof_failures': 0, 'oversized_components': 0,
             'method': 'dependency_ordered_independent_cut_identity'}
    for g in sorted(pending):
        if time.monotonic() >= end:
            stats.update(reason='matching_budget', seconds=time.monotonic()-start)
            return gold, candidate, stats
        deps, seen = set(), set()
        stack = [r >> 1 for r in classes[g]]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            other = node_class.get(n)
            if other is not None and other != g:
                if n in shared:
                    continue  # this exact function needs no tentative match
                if other in pending:
                    deps.add(other)
                continue
            if len(seen) > 2048:
                oversized.add(g)
                break
            term = manager.nodes[n]
            if term[0] == 'and':
                stack.extend(v >> 1 for v in term[1:] if v > 1)
        graph[g] = deps
    groups, owner = components(graph)
    dependencies = [{owner[d] for g in group for d in graph[g] if owner[d] != i}
                    for i, group in enumerate(groups)]
    users = [[] for _ in groups]
    for i, deps in enumerate(dependencies):
        for dep in deps:
            users[dep].append(i)
    ready = deque(i for i, deps in enumerate(dependencies) if not deps)
    deferred = deque()
    retrying = False
    stats.update(deferred_components=0, retried_components=0)
    patterns = {}
    last_progress = time.monotonic()
    processed = 0
    # First visit all independent candidates. Expanding a failed dependency in
    # thousands of downstream candidates repeats the same expensive cone.
    # Defer that work until the independent frontier has been exhausted.
    while (ready or deferred) and time.monotonic() < end:
        deadline.check()
        if not ready:
            ready, deferred = deferred, deque()
            retrying = True
            last_progress = time.monotonic()
        if retrying and time.monotonic()-last_progress >= 5:
            break
        index = ready.popleft()
        processed += 1
        group = groups[index]
        internal = set(group)
        stats['cyclic_components'] += int(len(group) > 1) if not retrying else 0
        usable = not internal.intersection(oversized)
        blocked = any(dep not in internal and dep not in proven for g in group for dep in graph[g])
        if blocked and not retrying:
            stats['blocked_dependencies'] += 1
            stats['deferred_components'] += 1
            deferred.append(index)
            usable = False
        if retrying:
            stats['retried_components'] += 1
        if internal.intersection(oversized) and not retrying:
            stats['oversized_components'] += 1
        method = None
        if usable:
            expanded_classes = set(internal)
            for expansion in range(3):
                local = type(manager)()
                remap, cuts = {0: 0}, set()
                used_proven, used_structural = set(), set()
                todo = [(r >> 1, False) for g in group for r in classes[g]]
                work = 0
                while todo and len(local.nodes) <= 2048 and work < 10000:
                    n, done = todo.pop()
                    work += 1
                    if n in remap:
                        continue
                    c = node_class.get(n)
                    if c is not None and c not in expanded_classes and c in proven:
                        remap[n] = local.variable(('proved_class', c)).literal ^ phase[n]
                        cuts.add(c)
                        used_proven.add(c)
                        continue
                    if n in shared and c not in expanded_classes:
                        remap[n] = local.variable(('structural_node', n)).literal
                        cuts.add(c)
                        used_structural.add(n)
                        continue
                    term = manager.nodes[n]
                    if term[0] == 'input':
                        remap[n] = local.variable(('pi', term[1])).literal
                    elif done:
                        remap[n] = local.conjunction(remap[term[1] >> 1] ^ (term[1] & 1),
                                                      remap[term[2] >> 1] ^ (term[2] & 1))
                    else:
                        todo.append((n, True))
                        todo.extend((v >> 1, False) for v in term[1:] if v > 1)
                if not todo:
                    roots = [[remap[r >> 1] ^ (r & 1) for r in classes[g]] for g in group]
                    method = prove(local, roots, patterns)
                if method or not cuts or time.monotonic() >= end:
                    break
                # Remove assumptions about correlations between previously
                # proved cut functions by exposing their actual definitions.
                expanded_classes.update(cuts)
            if not method:
                stats['local_proof_failures'] += 1
            if method:
                last_progress = time.monotonic()
                for g in group:
                    # The representative has the smallest original node ID;
                    # downstream canonicalization cannot introduce a cycle.
                    proven[g] = min(classes[g])
                stats['proved_classes'] += len(group)
                stats['proved_components'] += 1
                evidence.write(json.dumps({'classes': [classes[g] for g in group],
                    'method': method, 'nodes': len(local.nodes)-1,
                    'expansion': expansion, 'proved_dependencies': sorted(used_proven),
                    'structural_cut_nodes': sorted(used_structural)})+'\n')
        if not method and not retrying:
            stats['unproved_components'] += 1
        elif method and retrying:
            stats['unproved_components'] -= 1
        if progress and processed % 1024 == 0:
            stats['seconds'] = time.monotonic()-start
            progress(stats)
        if not retrying:
            for user in users[index]:
                dependencies[user].remove(index)
                if not dependencies[user]:
                    ready.append(user)
    # Substitute only proved equalities. All other logic stays intact.
    canonical = {0: 0}
    for n in sorted(left | right):
        if n % 4096 == 0:
            deadline.check()
        c = node_class.get(n)
        representative = proven.get(c)
        if representative is not None and representative >> 1 != n:
            canonical[n] = canonical[representative >> 1] ^ (representative & 1) ^ phase[n]
        else:
            term = manager.nodes[n]
            canonical[n] = (2*n if term[0] == 'input' else manager.conjunction(
                canonical[term[1] >> 1] ^ (term[1] & 1), canonical[term[2] >> 1] ^ (term[2] & 1)))
    stats.update(seconds=time.monotonic()-start, budget_exhausted=bool(ready or deferred) and time.monotonic() >= end,
                 pending_components=len(ready)+len(deferred), queue_exhausted=not bool(ready or deferred),
                 stalled_retry=retrying and bool(ready) and time.monotonic()-last_progress >= 5,
                 seconds_since_last_proof=time.monotonic()-last_progress)
    rewrite = lambda roots: {key: canonical[r >> 1] ^ (r & 1) for key, r in roots.items()}
    return rewrite(gold), rewrite(candidate), stats
