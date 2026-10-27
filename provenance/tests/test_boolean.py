"""Symbolic witnesses checked against independent exhaustive Boolean results."""
import random
import unittest

from yosys_flow.abc_internal import Network, cut_domain, prove_cut
from yosys_flow.boolean import BDD, ProofBudgetExceeded, signal_order
from yosys_flow.region_functions import verify_outputs
from yosys_flow.common import TraceError


def assignment_value(function, row):
    node = function.node
    while node > 1:
        variable, low, high = function.manager.nodes[node]
        node = high if row & (1 << variable) else low
    return node


def evaluate_certificate(function, inputs):
    if function['encoding']=='boolean_dag_v1':
        values={0:0,1:1}
        for node in function['nodes']:
            op=node['op']
            if op=='input':value=inputs[node['input_port']]
            else:
                args=[values[n] for n in node['args']]
                if op=='not':value=1-args[0]
                elif op=='and':value=args[0]&args[1]
                elif op=='or':value=args[0]|args[1]
                elif op=='xor':value=args[0]^args[1]
                else:raise AssertionError(op)
            values[node['id']]=value
        return values[function['root']]
    nodes={n['id']:n for n in function['nodes']};value=function['root']
    while value>1:
        node=nodes[value];value=node['high'] if inputs[node['input_port']] else node['low']
    return value


class BooleanTests(unittest.TestCase):
    def test_barrel_outputs_use_independent_control_first_functions(self):
        width = 32
        data = list(range(2, width+2)); selects = list(range(width+2, width+7))
        boundary = [{'direction':'input','original_bit':bit,'port':f'{stem}_{i}',
                     'aliases':[{'name':f'{stem}[{i}]','offset':0,'hide_name':False}]}
                    for stem, bits in [('a',data),('s',selects)] for i, bit in enumerate(bits)]
        cells = {}; outputs = data; counter = width+7
        for i, select in enumerate(selects):
            previous = outputs; outputs = []
            for j in range(width):
                cells[str(counter)] = {'type':'$_MUX_','parameters':{},
                    'port_directions':{'A':'input','B':'input','S':'input','Y':'output'},
                    'connections':{'A':[previous[j]],'B':[previous[(j+(1<<i))%width]],'S':[select],'Y':[counter]}}
                outputs.append(counter); counter += 1
        boundary += [{'direction':'output','original_bit':bit,'port':f'y_{i}'} for i,bit in enumerate(outputs)]
        proof = verify_outputs(cells,cells,boundary)
        self.assertTrue(proof['all_outputs_verified'])
        self.assertEqual(set(proof['input_order'][:5]), {f's_{i}' for i in range(5)})
        for shift in range(width):
            for bit in range(width):
                values = {f'a_{i}':int(i==bit) for i in range(width)}
                values.update({f's_{i}':(shift>>i)&1 for i in range(5)})
                for i,row in enumerate(proof['outputs']):
                    f=row['function']; nodes={n['id']:n for n in f['nodes']}; node=f['root']
                    while node>1:
                        n=nodes[node]; node=n['high'] if values[n['input_port']] else n['low']
                    self.assertEqual(node, int((i+shift)%width==bit))

    def test_escaped_scalar_adder_order_and_independent_arithmetic(self):
        width=128
        boundary=[];cells={};outputs=[];counter=2
        a=[];b=[]
        for stem,bits in (('a',a),('b',b)):
            for i in range(width):
                bits.append(counter)
                boundary.append({'direction':'input','original_bit':counter,'port':f'{stem}_{i}',
                    'aliases':[{'name':f'\\{stem}[{i}]','offset':0,'source_index':0,'hide_name':False}]})
                counter+=1
        def gate(kind,left,right):
            nonlocal counter
            output=counter;counter+=1
            cells[str(output)]={'type':kind,'parameters':{},'connections':{'A':[left],'B':[right],'Y':[output]},
                                'port_directions':{'A':'input','B':'input','Y':'output'}}
            return output
        carry='0'
        for i in range(width):
            prop=gate('$_XOR_',a[i],b[i])
            outputs.append(gate('$_XOR_',prop,carry))
            carry=gate('$_OR_',gate('$_AND_',a[i],b[i]),gate('$_AND_',prop,carry))
        outputs.append(carry)
        boundary += [{'direction':'output','original_bit':bit,'port':f'y_{i}'} for i,bit in enumerate(outputs)]
        proof=verify_outputs(cells,cells,boundary)
        self.assertTrue(proof['all_outputs_verified'])
        self.assertEqual(proof['input_order'][:6],['a_127','b_127','a_126','b_126','a_125','b_125'])
        self.assertLess(proof['resources']['nodes'],100_000)
        rng=random.Random(94)
        for left,right in [(0,0),((1<<width)-1,1),*((rng.getrandbits(width),rng.getrandbits(width)) for _ in range(16))]:
            values={f'{stem}_{i}':(value>>i)&1 for stem,value in (('a',left),('b',right)) for i in range(width)}
            actual=0
            for i,row in enumerate(proof['outputs']):
                node=evaluate_certificate(row['function'],values)
                actual|=node<<i
            self.assertEqual(actual,left+right)
        wrong={k:dict(v) for k,v in cells.items()};wrong[str(outputs[0])]['type']='$_OR_'
        # Alter the data XOR too: XOR(a,b) is not OR(a,b), even at bit zero.
        wrong[str(outputs[0]-1)]['type']='$_OR_'
        self.assertFalse(verify_outputs(cells,wrong,boundary)['all_outputs_verified'])
        self.assertGreater(signal_order('\\a[2]'),signal_order('\\a[10]'))

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
