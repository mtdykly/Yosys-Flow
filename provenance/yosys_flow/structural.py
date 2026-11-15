"""Exact, interned AND/complement expressions for cheap positive matches.

Equality is sufficient for Boolean equality; inequality says nothing. These
expressions are neither a canonical Boolean representation nor causal lineage.
Tuple interning compares keys, so hash collisions cannot establish equality.
"""


class Expression:
    __slots__ = ('manager', 'literal')

    def __init__(self, manager, literal):
        self.manager, self.literal = manager, literal

    def __hash__(self): return hash((self.manager, self.literal))

    def __eq__(self, other):
        return isinstance(other, Expression) and self.manager is other.manager and self.literal == other.literal

    def _literal(self, other):
        if isinstance(other, int) and other in (0, 1): return other
        if isinstance(other, Expression) and self.manager is other.manager: return other.literal
        raise ValueError('structural expressions must share input bindings')

    def __and__(self, other):
        return Expression(self.manager, self.manager.conjunction(self.literal, self._literal(other)))

    def __or__(self, other):
        return Expression(self.manager, self.manager.conjunction(self.literal ^ 1, self._literal(other) ^ 1) ^ 1)

    def __xor__(self, other):
        a, b = self.literal, self._literal(other)
        if a in (0, 1): return Expression(self.manager, b ^ a)
        if b in (0, 1): return Expression(self.manager, a ^ b)
        left = self.manager.conjunction(a, b ^ 1)
        right = self.manager.conjunction(a ^ 1, b)
        return Expression(self.manager, self.manager.conjunction(left ^ 1, right ^ 1) ^ 1)

    __rand__ = __and__
    __ror__ = __or__
    __rxor__ = __xor__


class Expressions:
    def __init__(self): self.unique = {}

    @property
    def one(self): return Expression(self, 1)

    def intern(self, key):
        if key not in self.unique: self.unique[key] = 2 * (len(self.unique) + 1)
        return self.unique[key]

    def variable(self, identity): return Expression(self, self.intern(('input', identity)))

    def conjunction(self, a, b):
        a, b = sorted((a, b))
        if a == 0 or a == (b ^ 1): return 0
        if a == 1 or a == b: return b
        return self.intern(('and', a, b))
