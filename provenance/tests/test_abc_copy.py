from copy import deepcopy
from types import SimpleNamespace
import unittest

from yosys_flow.abc_copy import verify
from yosys_flow.common import TraceError


def fixture():
    nodes={0:{'id':0,'op':'one','fanins':[]},1:{'id':1,'op':'pi','fanins':[]},
           2:{'id':2,'op':'pi','fanins':[]},3:{'id':3,'op':'and','fanins':[2,4]},
           4:{'id':4,'op':'and','fanins':[2,5]},5:{'id':5,'op':'and','fanins':[7,9]},
           6:{'id':6,'op':'buf','fanins':[10]}}
    before=SimpleNamespace(nodes=nodes,pis=[1,2],pos=[6])
    after=SimpleNamespace(nodes={**{i:nodes[i] for i in range(5)},
                                5:{'id':5,'op':'buf','fanins':[3]}},pis=[1,2],pos=[5])
    row={'seq':10,'reason':'dch_choice_copy','before':8,'after':9,
         'links':[[0,0],[1,2],[2,4],[3,6],[4,8],[5,3]]}
    facts=[{'kind':'proved_equivalence','reason':'dch_sat_merge','old':5,'new_literal':3}]
    return before,after,row,facts


class ActualCopyTests(unittest.TestCase):
    def test_actual_links_use_scoped_identity_and_proved_polarity(self):
        before,after,row,facts=fixture()
        _,proof=verify(before,after,row,facts,['a','b'])
        self.assertTrue(proof['complete'])
        self.assertEqual(proof['verified'],6)
        self.assertIn([5,3],proof['links'])

    def test_changed_link_is_not_accepted_as_a_copy_fact(self):
        before,after,row,facts=fixture();row['links'][-1]=[5,4]
        _,proof=verify(before,after,row,facts,['a','b'])
        self.assertFalse(proof['complete'])
        self.assertNotIn([5,4],proof['links'])

    def test_all_copy_outputs_are_checked_after_internal_links(self):
        before,after,row,facts=fixture()
        after.nodes[5]={**after.nodes[5],'fanins':[2]}
        _,proof=verify(before,after,row,facts,['a','b'])
        self.assertEqual(proof['verified'],6)
        self.assertFalse(proof['complete'])
        self.assertEqual(proof['unknown_outputs'],['copy_output_0'])

    def test_wrong_solver_polarity_is_rejected(self):
        before,after,row,facts=fixture();facts[0]['new_literal']=2
        with self.assertRaisesRegex(TraceError,'contradicts'):
            verify(before,after,row,facts,['a','b'])

    def test_missing_or_aliased_input_copy_is_rejected(self):
        for links in ([[0,0],[1,2],[3,6],[4,8],[5,3]],
                      [[0,0],[1,2],[2,2],[3,6],[4,8],[5,3]]):
            before,after,row,facts=fixture();row['links']=links
            with self.assertRaises(TraceError):verify(before,after,row,facts,['a','b'])


class AssociativeClosureTests(unittest.TestCase):
    def test_original_and_shape_survives_a_complemented_representative(self):
        from yosys_flow.structural import Expressions
        from yosys_flow.abc_partition import Classes
        from yosys_flow.abc_lemmas import associative_congruence
        m=Expressions();k=m.variable('k');a=m.variable('a');b=m.variable('b');c=m.variable('c')
        ab=a&b;left=ab&c;right=a&(b&c);wrong=a&(b|c)
        classes=Classes({},{});classes.join(ab.literal,k.literal^1)
        associative_congruence(m,classes)
        self.assertEqual(classes.find(left.literal),classes.find(right.literal))
        self.assertNotEqual(classes.find(left.literal),classes.find(wrong.literal))

    def test_every_merged_random_expression_has_identical_truth_table(self):
        import random
        from yosys_flow.structural import Expressions
        from yosys_flow.abc_internal import variables
        from yosys_flow.abc_partition import Classes
        from yosys_flow.abc_lemmas import associative_congruence
        rng=random.Random(12);m=Expressions();literals=[m.variable(str(i)).literal for i in range(5)]+[0,1]
        for _ in range(500):literals.append(m.conjunction(rng.choice(literals)^rng.randrange(2),rng.choice(literals)^rng.randrange(2)))
        tables={0:0,1:(1<<32)-1};patterns=variables(5)
        def value(lit):return tables[lit//2*2]^(tables[1] if lit&1 else 0)
        for key,lit in m.unique.items():tables[lit]=patterns[int(key[1])] if key[0]=='input' else value(key[1])&value(key[2])
        classes=Classes({},{})
        known={}
        for lit in literals:
            signature=value(lit)
            if signature in known:classes.join(lit,known[signature])
            else:known[signature]=lit
        associative_congruence(m,classes,leaf_limit=4)
        for lit in literals:self.assertEqual(value(lit),value(classes.find(lit)))
