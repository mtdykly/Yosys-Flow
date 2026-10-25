"""Indexed traversal with the exact chronological semantics of log replay."""
from collections import defaultdict
import heapq


class EdgeList(list):
    def adjacency(self, reverse=False):
        name = '_incoming' if reverse else '_outgoing'
        if not hasattr(self, name):
            index = defaultdict(list)
            for i, edge in enumerate(self):
                index[edge['to' if reverse else 'from']].append((i, edge))
            setattr(self, name, index)
        return getattr(self, name)

    def adjacent(self, node, reverse=False):
        return self.adjacency(reverse).get(node, ())


def walk(edges, seeds, *, reverse=False):
    """Equivalent to one forward/reverse scan, including out-of-order edges.

    A node first reached at event i can only enable later (earlier for reverse)
    edges. An unrestricted transitive closure would change existing evidence.
    """
    if not hasattr(edges, 'adjacent'):
        edges = EdgeList(edges)
    reached, selected, heap, queued = set(seeds), [], [], set()
    def push(node, bound):
        for i, edge in edges.adjacent(node, reverse):
            if i not in queued and (i < bound if reverse else i > bound):
                queued.add(i)
                heapq.heappush(heap, (-i if reverse else i, i, edge))
    for node in reached:
        push(node, len(edges) if reverse else -1)
    while heap:
        _, i, edge = heapq.heappop(heap)
        selected.append((i, edge))
        target = edge['from' if reverse else 'to']
        if target not in reached:
            reached.add(target)
            push(target, i)
    return reached, sorted(selected)


def incoming(edges, nodes):
    if not hasattr(edges, 'adjacent'):
        edges = EdgeList(edges)
    selected = {i: edge for node in nodes for i, edge in edges.adjacent(node, True)}
    return sorted(selected.items())


def has_path(edges, sources, target):
    """Existence only: stop at the first chronological source path.

    This is the compact cell check, not the full supporting-evidence export.
    A larger reverse event bound subsumes all visits with a smaller bound.
    """
    sources=set(sources)
    if target in sources:return True
    if not hasattr(edges,'adjacent'):edges=EdgeList(edges)
    queue=[(target,len(edges))];seen={target:len(edges)}
    while queue:
        node,bound=queue.pop()
        for i,edge in edges.adjacent(node,True):
            if i>=bound:continue
            parent=edge['from']
            if parent in sources:return True
            if seen.get(parent,-1)<i:
                seen[parent]=i;queue.append((parent,i))
    return False
