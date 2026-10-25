"""Checkpoint/delta decoding must preserve every captured ABC object state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.abc_internal import decode_network, read_evidence
from yosys_flow.cli import parser
from yosys_flow.common import TraceError
from yosys_flow.runner import run, load_run


class DeltaDecoderTests(unittest.TestCase):
    def checkpoint(self):
        return {'kind':'history_network','seq':2,'command_id':1,'representation':'aig',
                'encoding':'checkpoint','network_epoch':1,'pis':[1,2],'pos':[],
                'deleted_nodes':[],'nodes':[{'id':0,'op':'one','fanins':[]},
                    {'id':1,'op':'pi','fanins':[]},{'id':2,'op':'pi','fanins':[]},
                    {'id':3,'op':'and','fanins':[2,4]}]}

    def delta(self):
        return {'kind':'history_network','seq':4,'command_id':1,'representation':'aig',
                'encoding':'delta','network_epoch':1,'base_snapshot':2,'pis':[1,2],'pos':[],
                'deleted_nodes':[3],'nodes':[{'id':4,'op':'and','fanins':[3,4]}]}

    def test_creation_deletion_and_polarity_preserve_earlier_snapshot(self):
        nets={};epochs={};nets[2]=decode_network(self.checkpoint(),nets,epochs,5)
        later=decode_network(self.delta(),nets,epochs,5)
        self.assertEqual(later.nodes[4]['fanins'],[3,4])
        self.assertNotIn(3,later.nodes);self.assertIn(3,nets[2].nodes)
        self.assertIs(later.nodes[1],nets[2].nodes[1])
        nets[4]=later
        identical={**self.delta(),'seq':6,'base_snapshot':4,'deleted_nodes':[],'nodes':[]}
        self.assertIs(decode_network(identical,nets,epochs,5).nodes,later.nodes)

    def test_corrupt_scope_base_deletion_fanin_and_sequence_are_rejected(self):
        for change in ({'base_snapshot':99},{'command_id':2},{'network_epoch':2},
                       {'representation':'gia'},{'deleted_nodes':[99]},
                       {'deleted_nodes':[3,3]}, {'deleted_nodes':[1]},
                       {'nodes':[{'id':4,'op':'and','fanins':[999,4]}]}):
            with self.subTest(change=change):
                nets={};epochs={};nets[2]=decode_network(self.checkpoint(),nets,epochs,5)
                with self.assertRaises(TraceError):decode_network({**self.delta(),**change},nets,epochs,5)
        nets={};epochs={};nets[2]=decode_network(self.checkpoint(),nets,epochs,5)
        nets[4]=decode_network(self.delta(),nets,epochs,5)
        with self.assertRaisesRegex(TraceError,'stale base'):
            decode_network({**self.delta(),'seq':6},nets,epochs,5)


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class ActualDeltaTests(unittest.TestCase):
    def test_actual_full_and_delta_capture_identical_networks_and_final_design(self):
        with tempfile.TemporaryDirectory(prefix='abc-delta-') as temp:
            root=Path(temp);source=root/'adder.v'
            source.write_text('module demo(input [15:0] a,b, output [16:0] y);\nassign y={1\'b0,a}+{1\'b0,b};\nendmodule\n')
            decoded=[];designs=[];sizes=[]
            for full in (True,False):
                out=root/('full' if full else 'delta')
                with patch.dict(os.environ,{'YOSYS_FLOW_ABC_FULL_SNAPSHOTS':'1' if full else '0'}):
                    run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out)]))
                _,trace,_=load_run(out)
                files=trace.regions[0]['artifacts'];path=out/files['input.blif.flow.jsonl']
                rows,nets,_,_=read_evidence(path,(out/files['script']).read_text())
                if not full:self.assertTrue(any(r.get('encoding')=='delta' for r in rows))
                decoded.append({seq:(n.pis,n.pos,n.nodes) for seq,n in nets.items()})
                designs.append(json.loads((out/'after.json').read_text())['modules'])
                sizes.append(path.stat().st_size)
            self.assertEqual(decoded[0],decoded[1])
            self.assertEqual(designs[0],designs[1])
            self.assertLess(sizes[1],sizes[0])
