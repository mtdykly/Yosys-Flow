"""Symbolic witnesses checked against independent exhaustive Boolean results."""
import random
import unittest

from yosys_flow.abc_internal import Network, cut_domain, prove_cut
from yosys_flow.boolean import BDD, ProofBudgetExceeded


def assignment_value(function, row):
    node = function.node
    while node > 1:
        variable, low, high = function.manager.nodes[node]
        node = high if row & (1 << variable) else low
    return node


class BooleanTests(unittest.TestCase):
    def test_all_three_input_functions_and_boolean_operations(self):
        bdd = BDD()
        inputs = bdd.variables(3)
        functions = []
        # Construct every 3-input function from independent minterms; then
        # compare canonical equality and operations with all truth assignments.
        for truth in range(256):
            value = bdd.one ^ bdd.one
            for row in range(8):
                if truth & (1 << row):
                    term = bdd.one
                    for i, var in enumerate(inputs):
                        term &= var if row & (1 << i) else bdd.one ^ var
                    value |= term
            functions.append(value)
            self.assertEqual(sum(assignment_value(value,r) << r for r in range(8)), truth)
        self.assertEqual(len(set(functions)), 256)
        rng = random.Random(719)
        for _ in range(1000):
            a, b = rng.randrange(256), rng.randrange(256)
            self.assertEqual(functions[a] & functions[b], functions[a & b])
            self.assertEqual(functions[a] | functions[b], functions[a | b])
            self.assertEqual(functions[a] ^ functions[b], functions[a ^ b])
            self.assertEqual(bdd.one ^ functions[a], functions[255 ^ a])

    def test_network_ops_polarity_constants_and_reordered_inputs(self):
        rng = random.Random(563)
        nodes = [{'id':i,'op':'pi','fanins':[]} for i in range(5)]
        ops = [('one',0),('zero',0),('buf',1),('and',2),('xor',2),('mux',3),('sop',3)]
        for i in range(5,75):
            op, arity = ops[(i-5)%len(ops)]
            node = {'id':i,'op':op,'fanins':[2*rng.randrange(i)+rng.randrange(2) for _ in range(arity)]}
            if op == 'sop': node['sop'] = '10- 0\n-11 0\n'
            nodes.append(node)
        net = Network({'nodes':nodes})
        truth, mask = net.truth()
        bdd = BDD()
        symbolic, _ = net.truth(bdd=bdd)
        for i, value in symbolic.items():
            self.assertEqual(sum(assignment_value(value,row) << row for row in range(32)), truth[i])
        reordered = Network({'nodes':nodes, 'pis':[3,0,4,1,2]})
        values, _ = reordered.truth([symbolic[i] for i in reordered.pis])
        self.assertEqual(values, symbolic)
        self.assertNotEqual(reordered.truth(bdd=bdd)[0], symbolic)
        # Constants also work in a symbolic domain with no PIs.
        constants = Network({'nodes':[{'id':0,'op':'zero','fanins':[]}, {'id':1,'op':'one','fanins':[]}]})
        self.assertEqual(constants.truth(bdd=bdd)[0], {0:bdd.one^bdd.one, 1:bdd.one})

    def test_wide_independent_cut_rejects_correlated_boundary_shortcut(self):
        # x=a&b, z=a&~b: OR(x,z)==XOR(x,z) globally, but NOT at an
        # independent cut. Nine further inputs force symbolic local proof too.
        nodes = [{'id':i,'op':'pi','fanins':[]} for i in range(11)]
        nodes += [{'id':11,'op':'and','fanins':[0,2]}, {'id':12,'op':'and','fanins':[0,3]},
                  {'id':13,'op':'xor','fanins':[22,24]}]
        root = 13
        for i in range(2,11):
            nodes.append({'id':len(nodes),'op':'xor','fanins':[2*root,2*i]})
            root = len(nodes)-1
        net = Network({'nodes':nodes})
        values, mask = net.truth()
        boundary = [values[11],values[12],*[values[i] for i in range(2,11)]]
        correlated_or = values[11] | values[12]
        for var in boundary[2:]: correlated_or ^= var
        self.assertEqual(values[root], correlated_or)
        inputs, local_mask = cut_domain(len(boundary))
        correct, wrong = inputs[0]^inputs[1], inputs[0]|inputs[1]
        for var in inputs[2:]:
            correct ^= var
            wrong ^= var
        match = prove_cut(net, root, values, boundary, None, 0, mask, local_truth=correct)
        self.assertIsNotNone(match)
        self.assertNotIn(11, match[0]); self.assertNotIn(12, match[0])
        self.assertIsNone(prove_cut(net, root, values, boundary, None, 0, mask, local_truth=wrong))
        self.assertIsNotNone(prove_cut(net, root, values, boundary, None, 1, mask, local_truth=local_mask^correct))

    def test_node_and_work_budgets_fail_explicitly(self):
        for manager, reason in ((BDD(node_limit=2), 'bdd_node_limit_exceeded'),
                                (BDD(work_limit=0), 'bdd_work_limit_exceeded')):
            a,b = manager.variables(2)
            with self.assertRaisesRegex(ProofBudgetExceeded, reason): a & b

    def test_deep_symbolic_functions_do_not_depend_on_python_recursion(self):
        bdd = BDD()
        variables = bdd.variables(1200)
        value = bdd.one
        for var in reversed(variables): value &= var
        complement = bdd.one ^ value
        self.assertEqual(assignment_value(complement, (1<<1200)-1),0)
        self.assertEqual(assignment_value(complement, (1<<1200)-2),1)
        self.assertLess(bdd.statistics()['nodes'], 5000)
