"""Exact gate-function DAG forms shared by SOP and mapped-gate checkpoints.

Interned forms are identities, not simulation signatures. A gate is evaluated
over ALL assignments of its independent operands; composition then preserves
the identity at the captured PIs. Larger gates remain scoped opaque objects.
"""
from .abc_internal import evaluate, variables
from .region_functions import gate_function


class Forms:
    def __init__(self):
        self.terms = {}
        self.domains = {}
        self.skipped = 0

    def term(self, key):
        if key not in self.terms:
            self.terms[key] = 2 * len(self.terms) + 2
        return self.terms[key]

    def graph(self, graph, scope):
        values = {n: self.term(('input', n)) for n in graph.pis}
        for name in graph.order:
            node = graph.nodes[name]
            args = [values[f] for f in node['fanins']]
            ids = sorted({v // 2 for v in args if v >= 2})
            count = len(ids)
            if count > 6:
                self.skipped += 1
                values[name] = self.term(('opaque', scope, name))
                continue
            one = (1 << (1 << count)) - 1
            if count not in self.domains:
                self.domains[count] = variables(count)
            inputs = dict(zip(ids, self.domains[count]))
            bits = [inputs.get(v // 2, 0) ^ (one if v & 1 else 0) for v in args]
            table = (evaluate(node, bits, one) if node['op'] == 'sop' else
                     gate_function('$_' + node['op'] + '_', dict(zip(node['ports'], bits)), one))
            # Remove an operand only when both exhaustive cofactors coincide.
            for i in reversed(range(len(ids))):
                offsets = [(j & ((1 << i) - 1)) | ((j >> i) << (i + 1))
                           for j in range(1 << (len(ids) - 1))]
                low = sum(((table >> k) & 1) << j for j, k in enumerate(offsets))
                high = sum(((table >> (k | (1 << i))) & 1) << j for j, k in enumerate(offsets))
                if low == high:
                    ids.pop(i)
                    table = low
            if not ids:
                value = int(table != 0)
            elif len(ids) == 1:
                value = 2 * ids[0] ^ int(table == 1)
            else:
                one = (1 << (1 << len(ids))) - 1
                value = self.term(('gate', tuple(ids), min(table, table ^ one))) ^ int(table > (table ^ one))
            values[name] = value
        return values


def install(engine):
    """Normalize the entire captured pair, without a target or SAT call."""
    forms = Forms()
    representatives = {0: 0}
    unions = 0
    for scope, graph, literals in (('entry', engine.first, engine.a), ('export', engine.final, engine.b)):
        for name, value in forms.graph(graph, scope).items():
            positive = literals[name] ^ (value & 1)
            prior = representatives.setdefault(value // 2, positive)
            if engine.classes.find(prior) != engine.classes.find(positive):
                engine.classes.join(prior, positive)
                unions += 1
    return dict(rule='exhaustive_gate_function_dag_v1', unions=unions,
                forms=len(forms.terms), opaque_gates=forms.skipped, operand_limit=6)
