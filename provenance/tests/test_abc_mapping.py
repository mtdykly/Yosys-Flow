from types import SimpleNamespace
import unittest

from yosys_flow.abc_mapping import install, verify
from yosys_flow.abc_partition import Graph, Partitioner
from yosys_flow.common import TraceError


def fixture():
    # (a & b) | (a & ~b) == a. The selected choice leaf a does
    # not dominate the original AND root; both views share the actual GIA.
    nodes={1:{'id':1,'op':'pi','fanins':[]},2:{'id':2,'op':'pi','fanins':[]},
           3:{'id':3,'op':'and','fanins':[2,4]},4:{'id':4,'op':'and','fanins':[2,5]},
           5:{'id':5,'op':'and','fanins':[7,9]}}
    source=SimpleNamespace(nodes=nodes,pis=[1,2],record={'seq':10})
    mapped=SimpleNamespace(nodes={11:{'id':11,'op':'pi','fanins':[]},
        12:{'id':12,'op':'pi','fanins':[]},13:{'id':13,'op':'sop','sop':'1 1\n','fanins':[22],'gate':'BUF'}})
    conversion={'seq':12,'links':[[2,11],[4,12],[11,13]]}
    cuts=[{'seq':11,'root_literal':11,'leaves':[2]}]
    return source,mapped,conversion,cuts


class MappingCutTests(unittest.TestCase):
    def test_complemented_leaf_lemma_composes_through_mapped_xor(self):
        from yosys_flow.abc_checkpoints import network_blif
        source=SimpleNamespace(nodes={
            1:{'id':1,'op':'pi','fanins':[]},2:{'id':2,'op':'pi','fanins':[]},
            3:{'id':3,'op':'and','fanins':[2,5]},4:{'id':4,'op':'and','fanins':[3,4]},
            5:{'id':5,'op':'and','fanins':[7,9]}},pis=[1,2],pos=[],names=['a','b'],record={'seq':10,'po_names':[]})
        mapped=SimpleNamespace(nodes={
            11:{'id':11,'op':'pi','fanins':[]},12:{'id':12,'op':'pi','fanins':[]},
            13:{'id':13,'op':'sop','sop':'0 1\n','fanins':[22],'gate':'NOT'},
            14:{'id':14,'op':'sop','sop':'01 1\n10 1\n','fanins':[26,24],'gate':'XOR'}},
            pis=[11,12],pos=[],names=['a','b'],record={'po_names':[]})
        conversion={'seq':14,'links':[[2,11],[4,12],[3,13],[10,14]]}
        cuts=[{'seq':12,'root_literal':3,'leaves':[2]},
              {'seq':13,'root_literal':10,'leaves':[3,4]}]
        text,proof=verify(source,mapped,conversion,cuts,['a','b'])
        self.assertEqual(proof['verified'],2)
        engine=Partitioner(Graph(network_blif(source)),Graph(network_blif(mapped)))
        install(engine,Graph(text),proof,{})
        self.assertEqual(engine.classes.find(engine.a['__checkpoint_5']),
                         engine.classes.find(engine.b['__checkpoint_14']))

    def test_choice_cut_is_proved_against_same_source_snapshot(self):
        source,mapped,conversion,cuts=fixture()
        text,proof=verify(source,mapped,conversion,cuts,['a','b'])
        self.assertEqual((proof['verified'],proof['unknown']),(1,[]))
        graph=Graph(text);engine=Partitioner(graph,graph)
        install(engine,graph,proof,{})
        a=engine.a['__checkpoint_5'];b=engine.a['__checkpoint_6']
        self.assertEqual(engine.classes.find(a),engine.classes.find(b)^1)

    def test_wrong_gate_or_root_polarity_does_not_create_lemma(self):
        for change in ('gate','phase'):
            source,mapped,conversion,cuts=fixture()
            if change=='gate':mapped.nodes[13]['sop']='0 1\n'
            else:cuts[0]['root_literal']=10;conversion['links'][-1][0]=10
            _,proof=verify(source,mapped,conversion,cuts,['a','b'])
            self.assertEqual(proof['verified'],0)
            self.assertEqual(len(proof['unknown']),1)

    def test_wrong_leaf_binding_and_duplicate_targets_are_rejected(self):
        source,mapped,conversion,cuts=fixture();cuts[0]['leaves']=[4]
        with self.assertRaisesRegex(TraceError,'leaf binding'):
            verify(source,mapped,conversion,cuts,['a','b'])
        source,mapped,conversion,cuts=fixture();conversion['links'].append([6,11])
        with self.assertRaisesRegex(TraceError,'duplicate mapped target'):
            verify(source,mapped,conversion,cuts,['a','b'])

    def test_discovery_budget_failure_stays_unknown(self):
        from unittest.mock import patch
        source,mapped,conversion,cuts=fixture()
        with patch('yosys_flow.abc_mapping.Replay.local',return_value=None):
            _,proof=verify(source,mapped,conversion,cuts,['a','b'])
        self.assertEqual(proof['verified'],0)
        self.assertEqual(proof['pairs'],[])

    def test_source_graph_cec_fallback_proves_only_equal_functions(self):
        from unittest.mock import patch
        for wrong in (False,True):
            source,mapped,conversion,cuts=fixture()
            if wrong:mapped.nodes[13]['sop']='0 1\n'
            with patch('yosys_flow.abc_mapping.Replay.local',return_value=None):
                _,proof=verify(source,mapped,conversion,cuts,['a','b'],cec_timeout=2)
            self.assertEqual(proof['verified'],int(not wrong))

    def test_source_graph_cec_unknown_is_not_a_lemma(self):
        from unittest.mock import patch
        from yosys_flow.boolean import ProofBudgetExceeded
        source,mapped,conversion,cuts=fixture()
        with patch('yosys_flow.abc_mapping.Replay.local',return_value=None), \
             patch('yosys_flow.abc_mapping.Partitioner.global_cec',side_effect=ProofBudgetExceeded('timeout')):
            _,proof=verify(source,mapped,conversion,cuts,['a','b'],cec_timeout=2)
        self.assertEqual(proof['verified'],0)
