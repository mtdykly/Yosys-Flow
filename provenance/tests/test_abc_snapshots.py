"""Historical snapshots remain exact while deltas avoid whole-network copies."""
import random
import unittest
from unittest.mock import patch

from yosys_flow.abc_internal import Network, decode_network
from yosys_flow.abc_snapshots import SnapshotMap
from yosys_flow.common import TraceError


class SnapshotTests(unittest.TestCase):
    def test_future_writes_deletion_and_recreation_do_not_change_old_views(self):
        rng = random.Random(20261109)
        expected = {i: i for i in range(80)}
        current = SnapshotMap(expected)
        versions = [(current, dict(expected))]
        for step in range(300):
            key = rng.randrange(120)
            if key in expected and step % 3 == 0:
                del expected[key]
                current = current.updated({}, [key])
            else:
                expected[key] = step
                current = current.updated({key: step})
            versions.append((current, dict(expected)))
        for snapshot, reference in versions:
            self.assertEqual(dict(snapshot), reference)
            self.assertEqual(len(snapshot), len(reference))
        with self.assertRaises(ValueError): versions[0][0].updated({1: -1})
        with self.assertRaises(TypeError): current[0] = -1

    def test_changed_snapshots_match_full_validation_without_iterating_old_graph(self):
        nodes = {0: {'id':0,'op':'one','fanins':[]},
                 **{i:{'id':i,'op':'pi','fanins':[]} for i in range(1,5)},
                 **{i:{'id':i,'op':'and','fanins':[2,4]} for i in range(5,105)}}
        row = {'kind':'history_network','seq':1,'command_id':1,'representation':'aig',
               'encoding':'checkpoint','network_epoch':1,'pis':[1,2,3,4],'pos':[104],
               'nodes':list(nodes.values()),'deleted_nodes':[]}
        nets, epochs = {}, {}
        nets[1] = decode_network(row, nets, epochs, 5)
        initial = nets[1]
        rng = random.Random(15)
        for seq in range(2,102):
            key = rng.randrange(5,105)
            new = {'id':key, 'op':rng.choice(['and','xor']),
                   'fanins':[rng.randrange(10),rng.randrange(10)]}
            nodes[key] = new
            delta = {**row,'seq':seq,'encoding':'delta','base_snapshot':seq-1,'nodes':[new]}
            with patch.object(SnapshotMap,'__iter__',side_effect=AssertionError('full graph scan')):
                nets[seq] = decode_network(delta, nets, epochs, 5)
            reference = Network({**row,'nodes':list(nodes.values())})
            self.assertEqual(nets[seq].nodes, reference.nodes)
            self.assertEqual(nets[seq].truth(), reference.truth())
        self.assertEqual(initial.nodes[104]['op'], 'and')

    def test_failed_delta_does_not_publish_and_retained_users_block_deletion(self):
        row = {'kind':'history_network','seq':1,'command_id':1,'representation':'aig',
               'encoding':'checkpoint','network_epoch':1,'pis':[1],'pos':[3],
               'nodes':[{'id':1,'op':'pi','fanins':[]},
                        {'id':2,'op':'buf','fanins':[2]},
                        {'id':3,'op':'buf','fanins':[4]}], 'deleted_nodes':[]}
        nets, epochs = {}, {}
        nets[1] = decode_network(row, nets, epochs, 5)
        delta = {**row,'seq':2,'encoding':'delta','base_snapshot':1,
                 'nodes':[],'deleted_nodes':[2]}
        with self.assertRaises(TraceError): decode_network(delta, nets, epochs, 5)
        good = {**delta,'nodes':[{'id':3,'op':'buf','fanins':[3]}]}
        after = decode_network(good, nets, epochs, 5)
        self.assertEqual(after.nodes[3]['fanins'], [3])
        self.assertIn(2, nets[1].nodes)
        self.assertNotIn(2, after.nodes)
