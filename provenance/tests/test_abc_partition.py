import unittest
from unittest.mock import patch
from yosys_flow.boolean import ProofBudgetExceeded

from yosys_flow.abc_partition import Classes, Graph, Partitioner, WindowLimit
from yosys_flow.common import TraceError


def graph(body, outputs='y'):
    return Graph('.model test\n.inputs a b c d\n.outputs '+outputs+'\n'+body+'\n.end\n')


class PartitionTests(unittest.TestCase):
    def test_recorded_solver_lemma_propagates_through_congruence_and_checks_phase(self):
        from yosys_flow.abc_lemmas import install
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q t\n1- 1\n-1 1\n.names t d y\n11 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r u\n11 1\n.names u d y\n11 1\n')
        lemma=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q __checkpoint_1\n1- 1\n-1 1\n'
                    '.names b c r\n1- 1\n-1 1\n.names a r __checkpoint_2\n11 1\n.names a y\n1 1\n')
        event={'kind':'proved_equivalence','reason':'dch_sat_merge','old':1,'new_literal':4}
        descriptor={'rule':'pinned_dch_success_events_universal_substitution_v1','count':1}
        q=Partitioner(a,b)
        self.assertNotEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))
        install(q,lemma,[event],descriptor)
        self.assertEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))
        with self.assertRaisesRegex(TraceError,'polarity'):
            install(Partitioner(a,b),lemma,[{**event,'new_literal':5}],descriptor)
        with self.assertRaisesRegex(TraceError,'unproved event'):
            install(Partitioner(a,b),lemma,[{**event,'kind':'aig_replace'}],descriptor)

    def test_congruence_closure_handles_a_deep_parent_chain_without_recursion(self):
        from yosys_flow.abc_lemmas import congruence
        from yosys_flow.structural import Expressions
        manager=Expressions();a=manager.variable('a');b=manager.variable('b');c=manager.variable('c')
        left=(a&b)|(a&c);right=a&(b|c);equation=(left.literal,right.literal)
        for index in range(2500):
            x=manager.variable(index)
            left=left^x;right=right^x
        classes=Classes({'root':left.literal},{'root':right.literal})
        congruence(manager,classes,[equation])
        self.assertEqual(classes.find(left.literal),classes.find(right.literal))

    def test_recorded_lemma_representative_may_be_a_renamed_primary_input(self):
        from yosys_flow.abc_lemmas import install
        a=graph('.names a b u\n11 1\n.names a b v\n10 1\n.names u v y\n1- 1\n-1 1\n')
        b=graph('.names a y\n1 1\n')
        lemma=graph('.names a b u\n11 1\n.names a b v\n10 1\n.names u v __checkpoint_9\n1- 1\n-1 1\n.names a y\n1 1\n')
        q=Partitioner(a,b)
        install(q,lemma,[{'kind':'proved_equivalence','reason':'dch_structural_merge','old':9,'new_literal':2}],
                {'rule':'pinned_dch_success_events_universal_substitution_v1','count':1,
                 'pi_substitution':[{'snapshot_pi':1,'variable':'a'}]})
        self.assertEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))

    def test_checkpoint_bridges_reprove_each_hop_and_reject_wrong_chain(self):
        from yosys_flow.abc_checkpoints import bridge
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q y\n1- 1\n-1 1\n')
        middle=graph('.names b c t\n00 1\n.names a t y\n10 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r y\n11 1\n')
        q=Partitioner(a,b)
        proof=bridge(q,[('y','y',0)],[(middle,{'seq':1}),(b,{'endpoint':'final'})])
        self.assertEqual(proof['accepted'],[['y','y',0]])
        self.assertEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))
        wrong=graph('.names a b y\n11 1\n')
        q=Partitioner(a,b)
        proof=bridge(q,[('y','y',0)],[(wrong,{'seq':1}),(b,{'endpoint':'final'})])
        self.assertEqual(proof['accepted'],[])
        self.assertNotEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))

    def test_checkpoint_encoder_keeps_fanin_polarity_and_pi_names(self):
        from types import SimpleNamespace
        from yosys_flow.abc_checkpoints import network_blif
        net=SimpleNamespace(names=['a','b'],pis=[1,2],pos=[4],record={'po_names':['y']},nodes={
            1:{'op':'pi','fanins':[]},2:{'op':'pi','fanins':[]},
            3:{'op':'and','fanins':[2,5]},4:{'op':'buf','fanins':[7]}})
        actual=Graph(network_blif(net))
        for a in range(2):
            for b in range(2):
                self.assertEqual(actual.values(actual.order,{'a':a,'b':b},1)['y'],(a & (b^1))^1)

    def test_parity_union_find_preserves_all_complements(self):
        c=Classes({'a':2,'b':4,'c':6},{'x':8})
        c.join(6,9);c.join(4,7);c.join(2,4)
        self.assertEqual(c.find(2),c.find(8))
        self.assertEqual(c.find(6),c.find(8)^1)
        for n in range(2,10): self.assertEqual(c.find(n)^1,c.find(n^1))
        with self.assertRaises(TraceError):c.join(2,3)

    def test_simulation_match_is_only_a_candidate(self):
        a=graph('.names a b y\n11 1\n')
        b=graph('.names a b y\n1- 1\n-1 1\n')
        q=Partitioner(a,b,sample_bits=1)
        self.assertFalse(q.prove('y','y',0))
        self.assertEqual(q.proofs,[])
        with self.assertRaisesRegex(TraceError,'not equivalent'):
            q.replay([dict(proof='independent_frontier_bdd',entry_root='y',export_root='y',inverted=False)])

    def test_complementary_boundaries_absorb_only_the_inverter_and_check_its_side_output(self):
        header='.model test\n.inputs a b c d\n.outputs y side\n'
        a=Graph(header+'.names a b side\n1- 1\n-1 1\n.names side n\n0 1\n'
                '.names n c t\n11 1\n.names side d w\n11 1\n.names t w y\n1- 1\n-1 1\n.end\n')
        b=Graph(header+'.names a b k\n00 1\n.names k side\n0 1\n'
                '.names k c t2\n11 1\n.names side d w2\n11 1\n.names t2 w2 y\n1- 1\n-1 1\n.end\n')
        q=Partitioner(a,b)
        proof=q.region(['t'],source_nodes={'side'},limit=20)
        self.assertTrue(proof['all_external_outputs'])
        self.assertIn('side',proof['export_nodes'])
        self.assertNotIn('k',proof['export_nodes'])
        self.assertEqual({r['export'] for r in proof['outputs']},{'side','y'})
        self.assertIn({'export_node':'k','entry_nodes':['side'],'inverted':True},proof['boundary'])

    def test_window_reports_the_failing_side_and_frontier(self):
        a=graph('.names a b t\n11 1\n.names t c y\n11 1\n')
        with self.assertRaises(WindowLimit) as caught:a.window(['y'],set(a.pis),limit=1,side='entry')
        self.assertEqual(caught.exception.detail['side'],'entry')
        self.assertEqual(caught.exception.detail['limit'],1)
        with self.assertRaisesRegex(TraceError,'cycle'):
            graph('.names y a t\n11 1\n.names t b y\n11 1\n')

    def test_proved_pair_can_cut_a_later_rewrite(self):
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q t\n1- 1\n-1 1\n.names t d y\n11 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r u\n11 1\n.names u d y\n11 1\n')
        q=Partitioner(a,b)
        self.assertTrue(q.prove('t','u',0))
        self.assertTrue(q.prove('y','y',0))
        self.assertEqual(q.proofs[-1]['entry_nodes'],1)
        r=Partitioner(a,b);r.replay(q.proofs)
        self.assertEqual(r.classes.find(r.a['y']),r.classes.find(r.b['y']))

    def test_region_checks_all_external_outputs_and_shared_users(self):
        a=graph('.names a b t\n11 1\n.names t c y\n11 1\n.names t d z\n11 1\n','y z')
        b=graph('.names a b s\n11 1\n.names s c y\n11 1\n.names s d z\n11 1\n','y z')
        q=Partitioner(a,b)
        proof=q.region(['t'])
        self.assertEqual({p['export'] for p in proof['outputs']},{'y','z'})
        self.assertTrue(proof['all_external_outputs'])
        changed=graph('.names a b s\n11 1\n.names s c y\n11 1\n.names s d z\n1- 1\n-1 1\n','y z')
        with self.assertRaises(Exception):Partitioner(a,changed).region(['t'])

    def test_actual_target_filter_does_not_prove_unrelated_output(self):
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q y\n1- 1\n-1 1\n.names b d z\n11 1\n','y z')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r y\n11 1\n.names b d z\n1- 1\n-1 1\n','y z')
        q=Partitioner(a,b);q.refine(['y'],limits=(64,))
        self.assertTrue(q.proofs)
        self.assertFalse(any(p['entry_root']=='z' for p in q.proofs))

    def test_sat_fallback_proves_only_unsatisfiable_miters(self):
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q y\n1- 1\n-1 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r y\n11 1\n')
        q=Partitioner(a,b)
        with patch('yosys_flow.abc_partition.BDD',side_effect=ProofBudgetExceeded('injected')):
            self.assertTrue(q.prove('y','y',0))
        wrong=graph('.names a b y\n11 1\n')
        r=Partitioner(a,wrong)
        with patch('yosys_flow.abc_partition.BDD',side_effect=ProofBudgetExceeded('injected')):
            self.assertFalse(r.prove('y','y',0))
        for final,expected in [(b,True),(wrong,False)]:
            r=Partitioner(a,final,sat_backend='abc_cec',sat_timeout_ms=10000)
            with patch('yosys_flow.abc_partition.BDD',side_effect=ProofBudgetExceeded('injected')):
                self.assertEqual(r.prove('y','y',0),expected)

    def test_cec_structural_success_is_accepted_but_unknown_and_counterexample_are_not(self):
        from yosys_flow.abc_partition import cec_verified
        self.assertTrue(cec_verified('Networks are equivalent after structural hashing.  Time = 0.00 sec\n'))
        self.assertTrue(cec_verified('Networks are equivalent.\n'))
        self.assertFalse(cec_verified('Networks are NOT EQUIVALENT.\n'))
        self.assertFalse(cec_verified('Networks are undecided.\n'))

    def test_previously_shared_internal_root_can_be_an_independent_boundary(self):
        a=graph('.names a b t\n11 1\n.names t c u\n11 1\n.names t u y\n1- 1\n-1 1\n')
        b=graph('.names a b t\n11 1\n.names t y\n1 1\n')
        q=Partitioner(a,b)
        self.assertTrue(q.prove('y','y',0,limit=2))
        wrong=graph('.names a b t\n11 1\n.names t c u\n11 1\n.names t u y\n10 1\n01 1\n')
        self.assertFalse(Partitioner(a,wrong).prove('y','y',0))

    def test_conditional_candidates_are_resolved_before_accepting(self):
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q t\n1- 1\n-1 1\n.names t d y\n11 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r u\n11 1\n.names u d y\n11 1\n')
        q=Partitioner(a,b)
        result=q.targeted(['y'])
        self.assertTrue(result['outputs']['y'])
        again=Partitioner(a,b);again.replay(q.proofs)
        self.assertEqual(again.classes.find(again.a['y']),again.classes.find(again.b['y']))

    def test_global_boundary_cec_binds_all_primary_inputs_and_polarity(self):
        a=graph('.names a b p\n11 1\n.names a c q\n11 1\n.names p q y\n1- 1\n-1 1\n')
        b=graph('.names b c r\n1- 1\n-1 1\n.names a r y\n11 1\n')
        q=Partitioner(a,b)
        self.assertTrue(q.global_cec('y','y',0))
        self.assertEqual(q.proofs[0]['proof'],'captured_pi_cec')
        wrong=Partitioner(a,b)
        self.assertFalse(wrong.global_cec('y','y',1))
        self.assertEqual(wrong.proofs,[])
        replay=Partitioner(a,b);replay.replay(q.proofs)
        self.assertEqual(replay.classes.find(replay.a['y']),replay.classes.find(replay.b['y']))
