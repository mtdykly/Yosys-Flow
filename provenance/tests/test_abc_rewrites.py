"""Local rewrite replay is scoped, iterative, and independent of simulation."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yosys_flow.abc_partition import Graph, Partitioner
from yosys_flow.abc_rewrites import Replay, build, install
from yosys_flow.common import TraceError
from yosys_flow.boolean import ProofBudgetExceeded


def fixture():
    nodes={1:{'id':1,'op':'pi','fanins':[]},2:{'id':2,'op':'pi','fanins':[]},
           3:{'id':3,'op':'and','fanins':[2,4]},4:{'id':4,'op':'and','fanins':[2,5]},
           5:{'id':5,'op':'and','fanins':[7,9]},6:{'id':6,'op':'buf','fanins':[10]}}
    row={'seq':1,'kind':'history_network','representation':'aig','encoding':'checkpoint','network_epoch':1,'nodes':list(nodes.values())}
    net=SimpleNamespace(record=row,nodes=nodes,pis=[1,2],names=['',''])
    event={'seq':2,'kind':'aig_replace','snapshot':1,'old':5,'new_literal':3}
    return nodes,row,net,event


class RewriteReplayTests(unittest.TestCase):
    def test_actual_cut_proof_is_reusable_as_a_functional_lemma(self):
        nodes,row,net,event=fixture()
        doc=build([row,event],{1:net},['a','b'])
        self.assertEqual(doc['statistics']['accepted'],1)
        self.assertEqual(doc['equations'][0]['rule'],'same_snapshot_independent_cut')
        self.assertEqual(doc['equations'][0]['leaves'],[1,2])
        old=Graph('.model old\n.inputs a b\n.outputs y\n.names a b t\n11 1\n.names a b u\n10 1\n.names t u y\n00 1\n.end\n')
        new=Graph('.model new\n.inputs a b\n.outputs y\n.names a y\n0 1\n.end\n')
        p=Partitioner(old,new);install(p,doc)
        self.assertEqual(p.classes.find(p.a['y']),p.classes.find(p.b['y']))

    def test_rewiring_and_reused_node_id_do_not_keep_an_obsolete_function(self):
        nodes,row,net,event=fixture();replay=Replay(['a','b']);replay.observe(net);replay.event(event)
        change={'id':6,'op':'buf','fanins':[3]}
        next_row={**row,'seq':3,'encoding':'delta','base_snapshot':1,'nodes':[change],'deleted_nodes':[3,4,5]}
        current={1:nodes[1],2:nodes[2],6:change}
        replay.observe(SimpleNamespace(record=next_row,nodes=current,pis=[1,2],names=['','']))
        self.assertEqual(replay.classes.find(replay.values[6]),replay.classes.find(replay.bindings['a']^1))
        reused={'id':5,'op':'buf','fanins':[4]};changed={'id':6,'op':'buf','fanins':[10]}
        next_row={**row,'seq':4,'encoding':'delta','base_snapshot':3,'nodes':[reused,changed],'deleted_nodes':[]}
        current={**current,5:reused,6:changed}
        replay.observe(SimpleNamespace(record=next_row,nodes=current,pis=[1,2],names=['','']))
        self.assertEqual(replay.classes.find(replay.values[6]),replay.classes.find(replay.bindings['b']))
        with self.assertRaisesRegex(TraceError,'contradicts snapshot'):
            replay.event({'seq':5,'kind':'proved_equivalence','reason':'dch_sat_merge','snapshot':4,'old':6,'new_literal':3})

    def test_failed_local_equivalence_never_adds_an_equation(self):
        nodes,row,net,event=fixture()
        doc=build([row,{**event,'new_literal':4}],{1:net},['a','b'])
        self.assertEqual(doc['equations'],[])
        self.assertEqual(doc['statistics']['rewrite_not_globally_equal'],1)

    def test_correlated_cut_inputs_are_expanded_not_assumed_independent(self):
        # (a & b) | a == a. The first shared frontier contains a, b and
        # t=a&b; accepting the correlation without expanding t is forbidden.
        nodes={1:{'id':1,'op':'pi','fanins':[]},2:{'id':2,'op':'pi','fanins':[]},
               3:{'id':3,'op':'and','fanins':[2,4]},
               4:{'id':4,'op':'and','fanins':[7,3]},
               5:{'id':5,'op':'and','fanins':[6,2]},
               6:{'id':6,'op':'and','fanins':[11,3]}}
        row={'seq':1,'representation':'aig','encoding':'checkpoint','network_epoch':1}
        replay=Replay(['a','b'])
        replay.observe(SimpleNamespace(record=row,nodes=nodes,pis=[1,2],names=['','']))
        proof=replay.local(4,6,0)
        self.assertIsNotNone(proof)
        self.assertGreater(proof['expansion'],0)
        self.assertEqual(proof['leaves'],[1,2])

    def test_cycles_are_detected_without_recursive_evaluation(self):
        nodes,row,net,event=fixture();nodes[3]={**nodes[3],'fanins':[12,4]}
        with self.assertRaisesRegex(TraceError,'cycle'):Replay(['a','b']).observe(net)

    def test_deep_anchor_search_keeps_the_actual_proof_small(self):
        replay=Replay(['a'])
        replay.nodes={1:{'id':1,'op':'pi','fanins':[]}}
        for ident in range(2,413):
            replay.nodes[ident]={'id':ident,'op':'buf','fanins':[2*(ident-1)]}
        replay.nodes[500]={'id':500,'op':'buf','fanins':[800]}
        proof=replay.local(412,500,0)
        self.assertIsNotNone(proof)
        self.assertGreater(proof['depth'],8)
        self.assertLess(proof['nodes'],20)

    def test_sat_fallback_proves_the_same_cut_and_rejects_wrong_polarity(self):
        nodes,row,net,event=fixture();replay=Replay(['a','b']);replay.observe(net)
        # Node 5 is !a; the mismatch must be UNSAT only for complemented a.
        with patch('yosys_flow.abc_partition.BDD',side_effect=ProofBudgetExceeded('force SAT')):
            self.assertEqual(replay.sat_cut(5,1,1,[3,4,5],[1,2]),(True,'abc_sat'))
            self.assertEqual(replay.sat_cut(5,1,0,[3,4,5],[1,2]),(False,'abc_sat'))

    def test_sat_timeout_is_unknown_and_cannot_become_a_rewrite_fact(self):
        import subprocess
        nodes,row,net,event=fixture();replay=Replay(['a','b']);replay.observe(net)
        with patch('yosys_flow.abc_partition.BDD',side_effect=ProofBudgetExceeded('force SAT')), \
             patch('yosys_flow.abc_partition.subprocess.run',side_effect=subprocess.TimeoutExpired('abc',1)):
            with self.assertRaisesRegex(ProofBudgetExceeded,'timeout'):
                replay.sat_cut(5,1,1,[3,4,5],[1,2])
        self.assertEqual(replay.equations,[])

    def test_export_retains_actual_to_normalized_context_equalities(self):
        nodes,row,net,event=fixture();nodes[6]={'id':6,'op':'and','fanins':[10,4]}
        later={**row,'seq':3,'network_epoch':2}
        doc=build([row,event,later],{1:net,3:SimpleNamespace(record=later,nodes=nodes,pis=[1,2],names=['',''])},['a','b'])
        self.assertGreater(doc['statistics'].get('context_equations',0),0)
        from yosys_flow.abc_internal import variables
        patterns=dict(zip(['a','b'],variables(2)));truth={0:0,1:15}
        def value(lit):return truth[lit//2*2]^(15 if lit&1 else 0)
        for term in doc['terms']:
            key=term['term'];truth[term['literal']]=patterns[key[1]] if key[0]=='input' else value(key[1])&value(key[2])
        for equation in doc['equations']:
            self.assertEqual(value(equation['left']),value(equation['right']))
