"""Exact, bounded reduced ordered BDDs for ABC function witnesses.

Variable numbers denote explicitly bound inputs, never ABC object IDs. A shared
manager makes function equality canonical. Resource exhaustion is unknown, not
inequality; callers must retain candidate provenance in that case.
"""

MAX_BDD_NODES = 100_000
MAX_BDD_WORK = 1_000_000


class ProofBudgetExceeded(Exception):
    pass


class Function:
    __slots__ = ('manager', 'node')

    def __init__(self, manager, node):
        self.manager, self.node = manager, node

    def __hash__(self):
        return hash((self.manager, self.node))

    def __eq__(self, other):
        return isinstance(other, Function) and self.manager is other.manager and self.node == other.node

    def _apply(self, op, other):
        if isinstance(other, int) and other in (0, 1):
            node = other
        elif isinstance(other, Function) and other.manager is self.manager:
            node = other.node
        else:
            raise ValueError('Boolean functions must share an input binding/manager')
        return Function(self.manager, self.manager.apply(op, self.node, node))

    def __and__(self, other): return self._apply('and', other)
    def __or__(self, other): return self._apply('or', other)
    def __xor__(self, other): return self._apply('xor', other)
    __rand__ = __and__
    __ror__ = __or__
    __rxor__ = __xor__


class BDD:
    def __init__(self, *, node_limit=None, work_limit=None):
        self.node_limit = MAX_BDD_NODES if node_limit is None else node_limit
        self.work_limit = MAX_BDD_WORK if work_limit is None else work_limit
        self.nodes = [None, None]  # false and true
        self.unique, self.cache = {}, {}
        self.work = 0

    @property
    def one(self): return Function(self, 1)

    def variables(self, count):
        return [Function(self, self._node(i, 0, 1)) for i in range(count)]

    def _node(self, variable, low, high):
        if low == high: return low
        key = (variable, low, high)
        if key not in self.unique:
            if len(self.unique) >= self.node_limit:
                raise ProofBudgetExceeded('bdd_node_limit_exceeded')
            self.unique[key] = len(self.nodes)
            self.nodes.append(key)
        return self.unique[key]

    @staticmethod
    def _key(op, a, b):
        return (op, min(a, b), max(a, b))

    def _known(self, key):
        op, a, b = key
        if op == 'and':
            if a == 0: return 0
            if a == 1 or a == b: return b
        elif op == 'or':
            if a == 1: return 1
            if a == 0 or a == b: return b
        elif op == 'xor':
            if a == 0: return b
            if a == b: return 0
        return self.cache.get(key)

    def _children(self, key):
        op, a, b = key
        va = self.nodes[a][0] if a > 1 else float('inf')
        vb = self.nodes[b][0] if b > 1 else float('inf')
        variable = min(va, vb)
        al, ah = self.nodes[a][1:] if va == variable else (a, a)
        bl, bh = self.nodes[b][1:] if vb == variable else (b, b)
        return variable, self._key(op, al, bl), self._key(op, ah, bh)

    def apply(self, op, a, b):
        key = self._key(op, a, b)
        # Iterative Shannon expansion: BDD depth need not fit Python's stack.
        stack = [(key, False)]
        while stack:
            current, expanded = stack.pop()
            if self._known(current) is not None: continue
            variable, low, high = self._children(current)
            if expanded:
                self.cache[current] = self._node(variable, self._known(low), self._known(high))
            else:
                if self.work >= self.work_limit:
                    raise ProofBudgetExceeded('bdd_work_limit_exceeded')
                self.work += 1
                stack.extend(((current, True), (high, False), (low, False)))
        return self._known(key)

    def statistics(self):
        return {'backend': 'robdd', 'nodes': len(self.unique), 'apply_steps': self.work,
                'cached_operations': len(self.cache), 'node_limit': self.node_limit,
                'work_limit': self.work_limit}
