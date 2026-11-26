from pathlib import Path
import unittest
from unittest.mock import patch

from test_abc_copy import fixture
from yosys_flow.abc_partition import Graph,Partitioner
from yosys_flow.abc_checkpoints import network_blif
from yosys_flow.common import TraceError
from yosys_flow.rewrite_recovery import recover,install
from types import SimpleNamespace


def data():
    net,_,_,_=fixture()
    net.record={'seq':1,'kind':'history_network','command_id':1,'representation':'aig'}
    event={'seq':2,'kind':'aig_replace','command_id':1,'snapshot':1,'old':5,'new_literal':3}
    inventory={k:event[k] for k in ('seq','snapshot','old','new_literal')}
    document={'unknown_rewrite_examples':[inventory],'statistics':{'rewrite_unknown':1}}
    return net,event,document


class RewriteRecoveryTests(unittest.TestCase):
    def test_old_unknown_event_is_proved_without_edit_targets_and_installed(self):
        net,event,document=data()
        result=recover(Path('/unused'),document,['a','b'],decoded=([{},event],{1:net}),budget=30)
        self.assertEqual(result['summary']['verified'],1)
        self.assertFalse(result['summary']['edit_target_used'])
        graph=Graph(network_blif(SimpleNamespace(nodes=net.nodes,pis=net.pis,pos=[],names=['a','b'],record={'po_names':[]})))
        engine=Partitioner(graph,graph)
        self.assertNotEqual(engine.classes.find(engine.a['__checkpoint_5']),engine.classes.find(engine.a['a'])^1)
        install(engine,result,{'test':'old-only'})
        self.assertEqual(engine.classes.find(engine.a['__checkpoint_5']),engine.classes.find(engine.a['a'])^1)

    def test_timeout_or_nonproof_is_not_imported(self):
        net,event,document=data()
        with patch.object(Partitioner,'global_cec',return_value=False):
            result=recover(Path('/unused'),document,['a','b'],decoded=([{},event],{1:net}))
        self.assertEqual(result['equations'],[])
        self.assertEqual(result['summary']['unknown'],1)
        self.assertEqual(result['unknown'][0]['reason'],'not_proved')

    def test_budget_and_inventory_truncation_remain_visible(self):
        net,event,document=data();document['statistics']['rewrite_unknown']=8
        with patch.object(Partitioner,'global_cec',side_effect=AssertionError('budget was exhausted')):
            result=recover(Path('/unused'),document,['a','b'],decoded=([{},event],{1:net}),budget=1e-12)
        self.assertEqual(result['summary']['attempted'],0)
        self.assertTrue(result['summary']['inventory_truncated'])
        self.assertEqual(result['unknown'][0]['reason'],'recovery_budget')

    def test_candidate_must_be_the_actual_recorded_replacement(self):
        net,event,document=data();event={**event,'new_literal':2}
        with self.assertRaisesRegex(TraceError,'actual event'):
            recover(Path('/unused'),document,['a','b'],decoded=([{},event],{1:net}))

    def test_wrong_snapshot_owner_or_future_snapshot_is_rejected(self):
        for field,value in [('command_id',2),('seq',3),('representation','gia')]:
            net,event,document=data();net.record[field]=value
            with self.assertRaisesRegex(TraceError,'scope'):
                recover(Path('/unused'),document,['a','b'],decoded=([{},event],{1:net}))
